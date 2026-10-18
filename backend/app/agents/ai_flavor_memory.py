"""AI 腔记忆的持久化读写（AI 腔反馈闭环的最底层基座）。

## 这块在闭环里的位置

助理 agent（Assistant）是主 agent：它调度 Writer（`app.generation.engine`）
生成文本；生成完毕后**由 Assistant 检查**产出、把本次 AI 腔问题总结成一句
人话，经本模块的 `upsert_memory()` 落库；下次生成前，编排层经
`load_memories()` 取回记忆、由 `render_memory_block()` 渲染成提示词块，
注入给 Writer 的**输入**（微调对象是下次的输入，不是重写本次文本）。
检查者与生成者由此分离——Writer 自检自改是本末倒置。

## 三条设计主线（为什么代码长这样）

1. **去重键 = (scope, project_id, issue_key)**。同一个问题模式在多少段里
   反复出现，都只累加一行 `hit_count`：表长由「问题种类」决定，而不是由
   「写作轮数」决定。没有这条，一个 300 段的长篇能把表写爆，提示词也会被
   同一句话复读撑爆。因此 `upsert_memory()` 是幂等的读-改-写：查得到就
   累加，查不到才插新行。

2. **有界膨胀，写入端与读取端同时设防**。只堵一端堵不住：写入端漏进一条
   长文，或读取端出现一个新调用方忘了裁剪，提示词都会被撑爆。所以
   - 写入端：`examples` 最多 `MAX_EXAMPLES` 条（新的挤掉最旧的），单条
     snippet 截断到 `MAX_SNIPPET_LEN`，同一处证据（segment_no+offset）
     不重复记；
   - 读取端：`load_memories()` 按 `limit`（条数）与 `char_budget`（字符
     预算，按**渲染后行长**精确计算）双重裁剪。

3. **软开关 + 合并策略**。`active=False` 停用但不删（历史要能查）；停用是
   显式的人工决策，`upsert_memory()` **不复活**已停用的行，只累加计数。
   `load_memories()` 合并 global + 本项目两个来源，同一 `issue_key` 项目行
   胜出（项目级教训比全局通用教训更具体），避免同一条问题在提示词里出现两次。

## 事务边界

写函数**不 commit**：与 `prompt_overrides.set_active_override()` 同款约定，
由调用方（Assistant 的检查落库流程）持有事务边界，记忆写入与其它审计写入
共用同一个事务，一半失败整体回滚。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# ====================================================================
# 常量（有界膨胀的全部数值集中在这里，调参只改这一处）
# ====================================================================

#: 合法 scope：global = 跨项目通用教训；project = 本项目私有教训。
SCOPES: tuple[str, ...] = ("global", "project")

#: 严重度排序权重（越小越靠前）。未知严重度排最后（`len(SEVERITY_RANK)`）。
SEVERITY_RANK: dict[str, int] = {"high": 0, "mid": 1, "low": 2}

#: 默认严重度：调用方不表态时按 mid 处理（不高估不低估）。
DEFAULT_SEVERITY: str = "mid"

#: 单条记忆最多保留的例句数。3 条的理由：证据的作用是「让人一眼看懂这条
#: 记忆指的是什么」，3 条足以覆盖不同出现位置；更多只是复读同一种模式，
#: 却会把每条记忆的渲染长度推高、挤占其它记忆的字符预算。
MAX_EXAMPLES: int = 3

#: 单条例句 snippet 的字符上限。例句是「证据」不是「原文归档」，200 字足够
#: 定位问题；防的是某次检测把整段正文塞进 examples 把库撑爆。
MAX_SNIPPET_LEN: int = 200

#: issue_key 长度上限，与模型列宽（String(128)）一致。超长直接报错而非
#: 截断——截断可能把两个不同的 key 截成同一个， silently 合并两种问题。
MAX_ISSUE_KEY_LEN: int = 128

#: load_memories 默认条数上限。12 条的依据：提示词块按每条约 60-80 字估算，
#: 12 条约 1000 字上下，与默认字符预算匹配，属于「提醒但不喧宾夺主」。
DEFAULT_LIMIT: int = 12

#: load_memories 默认字符预算（按渲染后文本计）。2000 字的量级参照：
#: 提示词覆写单块上限为 4000 字（prompt_overrides.MAX_OVERRIDE_LEN），
#: 记忆块作为附加说明不应占到一半。
DEFAULT_CHAR_BUDGET: int = 2000


# ====================================================================
# 输入归一化 / 校验（全部集中在瘦函数里，upsert 主干因此保持可读）
# ====================================================================


def _validate_scope(scope: str) -> str:
    """归一并校验 scope；非法值抛 ValueError（让调用方尽早发现口径错误）。"""
    s = (scope or "").strip().lower()
    if s not in SCOPES:
        raise ValueError(f"scope={scope!r} 不合法，须为 {list(SCOPES)} 之一")
    return s


def _validate_project_id(scope: str, project_id: int | None) -> int | None:
    """global 行强制 project_id=None；project 行强制带 project_id。

    为什么强制：global 行挂上 project_id 会让「全局记忆」悄悄变成某个项目
    的私有记忆（别的项目读不到，本项目还以为是普适教训）；project 行缺
    project_id 则会在读取时因 `IS NULL` 匹配到 global 行，造成串读。
    """
    if scope == "global":
        return None
    if project_id is None:
        raise ValueError("scope='project' 必须提供 project_id")
    return int(project_id)


def _validate_issue_key(issue_key: str) -> str:
    """去重键的非空校验 + 长度硬闸（见 MAX_ISSUE_KEY_LEN 注释）。"""
    key = (issue_key or "").strip()
    if not key:
        raise ValueError("issue_key 不能为空（它是去重键）")
    if len(key) > MAX_ISSUE_KEY_LEN:
        raise ValueError(
            f"issue_key 超长（{len(key)} > {MAX_ISSUE_KEY_LEN}），"
            "请先由检测侧归一成短模式名"
        )
    return key


def _normalize_severity(severity: str) -> str:
    """严重度归一为小写；空值回落默认档。未知值原样保留（排序时排最后）。"""
    s = (severity or "").strip().lower()
    return s or DEFAULT_SEVERITY


def _normalize_examples(examples: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把调用方传入的例句归一成 `[{segment_no, offset, snippet}]`。

    - snippet 截断到 MAX_SNIPPET_LEN（防长文撑库）；
    - 同一 (segment_no, offset) 只保留**最后**出现的一条（重跑检测时后写的
      信息更新，且不让同一处证据在列表里复读）；
    - 字段缺失给保守默认值，绝不因一条脏数据整批失败。
    """
    normalized: list[dict[str, Any]] = []
    for raw in examples or []:
        if not isinstance(raw, dict):
            continue
        seg = raw.get("segment_no")
        off = raw.get("offset")
        snippet = str(raw.get("snippet") or "")[:MAX_SNIPPET_LEN]
        normalized.append(
            {"segment_no": seg, "offset": off, "snippet": snippet}
        )
    # 去重保序：后面的挤掉前面的同一处证据
    deduped: list[dict[str, Any]] = []
    taken: set[tuple[Any, Any]] = set()
    for item in reversed(normalized):
        k = (item["segment_no"], item["offset"])
        if k in taken:
            continue
        taken.add(k)
        deduped.append(item)
    deduped.reverse()
    return deduped


def _merge_examples(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并旧证据与新证据并压回 MAX_EXAMPLES 条上限（新的挤掉最旧的）。

    已存在的 (segment_no, offset) 不重复追加——同一处文本再次命中只是
    hit_count 的事，不该在证据列表里复读。
    """
    merged = list(existing or [])
    known = {(e.get("segment_no"), e.get("offset")) for e in merged}
    for item in incoming:
        k = (item.get("segment_no"), item.get("offset"))
        if k in known:
            continue
        merged.append(item)
        known.add(k)
    return merged[-MAX_EXAMPLES:]


# ====================================================================
# 写
# ====================================================================


def upsert_memory(
    db: Any,
    *,
    scope: str,
    project_id: int | None,
    issue_key: str,
    summary: str = "",
    hits_delta: int = 1,
    examples: list[dict[str, Any]] | None = None,
    segment_no: int | None = None,
    severity: str = DEFAULT_SEVERITY,
) -> Any:
    """幂等累加一条 AI 腔记忆；同一去重键**不产生重复行**。

    Args:
        db: SQLAlchemy Session（调用方持有事务，本函数不 commit）。
        scope: "global" | "project"。
        project_id: scope=project 时必填；scope=global 时强制忽略为 NULL。
        issue_key: 命中的 AI 腔模式 / 归一化问题类型，如 "仿佛"。
        summary: 一句人话总结。空串表示「只累加命中，不更新描述」——
            允许调用方在补刀命中时只报数。
        hits_delta: 本次命中次数，须 >= 0（0 = 仅刷新证据/描述）。
        examples: 本次例句 [{segment_no, offset, snippet}]。
        segment_no: 最近一次出现在哪段；None 表示不刷新该字段。
        severity: high | mid | low（未知值排序时排最后）。

    Returns:
        落库（已 flush，未 commit）的 `AIFlavorMemory` 行。

    Raises:
        ValueError: scope / project_id / issue_key / hits_delta 非法。

    为什么不复活已停用的行：`active=False` 是显式的人工停用决策，
    upsert 是自动累加通道，两者口径不同——自动通道悄悄改人工开关
    会让「停用」这个功能失效。停用的行照常累加 hit_count（历史连续），
    但不出现在 `load_memories()` 里；复活留给显式操作。
    """
    from sqlalchemy import select

    from app.models import AIFlavorMemory

    scope_n = _validate_scope(scope)
    pid = _validate_project_id(scope_n, project_id)
    key = _validate_issue_key(issue_key)
    if not isinstance(hits_delta, int) or hits_delta < 0:
        raise ValueError(f"hits_delta 须为非负整数，收到 {hits_delta!r}")
    sev = _normalize_severity(severity)
    new_examples = _normalize_examples(examples)

    # 查重：global 行用 IS NULL 匹配（SQLAlchemy 的 == None 也会转 IS NULL，
    # 这里显式写 is_ 让「NULL 是去重键的一部分」这件事在代码里可见）。
    stmt = select(AIFlavorMemory).where(
        AIFlavorMemory.scope == scope_n,
        AIFlavorMemory.issue_key == key,
    )
    stmt = (
        stmt.where(AIFlavorMemory.project_id.is_(None))
        if pid is None
        else stmt.where(AIFlavorMemory.project_id == pid)
    )
    row = db.scalars(stmt.order_by(AIFlavorMemory.id.desc())).first()

    if row is None:
        row = AIFlavorMemory(
            scope=scope_n,
            project_id=pid,
            issue_key=key,
            summary=(summary or "").strip(),
            hit_count=hits_delta,
            examples=_merge_examples(None, new_examples),
            last_segment_no=segment_no,
            severity=sev,
            active=True,
        )
        db.add(row)
    else:
        row.hit_count = (row.hit_count or 0) + hits_delta
        if (summary or "").strip():
            row.summary = summary.strip()
        row.severity = sev
        if segment_no is not None:
            # 直接刷新为「本次报告的段号」：段号由调用方按写作进度给出，
            # 续写/重生成可能回访更早的段，「只前进不后退」会记错最近出现位置。
            row.last_segment_no = segment_no
        row.examples = _merge_examples(row.examples, new_examples)
    db.flush()
    return row


# ====================================================================
# 读
# ====================================================================


def _severity_rank(severity: str) -> int:
    """严重度 → 排序权重；未知严重度排最后（见 SEVERITY_RANK 注释）。"""
    return SEVERITY_RANK.get((severity or "").lower(), len(SEVERITY_RANK))


def _recency_ts(dt: datetime | None) -> float:
    """updated_at → 排序用的时间戳；None 回落 0.0（排最后）。"""
    return dt.timestamp() if dt is not None else 0.0


def _sort_key(item: dict[str, Any]) -> tuple[int, int, int, float]:
    """排序键：严重度升序 → hit_count 降序 → last_segment_no 降序 → updated_at 降序。

    为什么严重度优先于 hit_count：high 档是「明显破坏文风」的问题
    （如高频机械比喻），mid 档多是润色级提醒。提示词预算有限时，先保证
    真问题在场。hit_count 是同一档内的主排序——反复出现说明 Writer 在
    系统性复读它，最该被提醒。
    """
    return (
        _severity_rank(item.get("severity", "")),
        -int(item.get("hit_count") or 0),
        -int(item.get("last_segment_no") or 0),
        -_recency_ts(item.get("updated_at")),
    )


def _row_to_item(row: Any) -> dict[str, Any]:
    """ORM 行 → 可直接渲染的字典（渲染函数只认这个结构，不认 ORM 对象）。"""
    return {
        "scope": row.scope,
        "project_id": row.project_id,
        "issue_key": row.issue_key,
        "summary": row.summary or "",
        "hit_count": int(row.hit_count or 0),
        "severity": (row.severity or "").lower(),
        "examples": list(row.examples or []),
        "last_segment_no": row.last_segment_no,
        "updated_at": row.updated_at,
    }


def _render_line(item: dict[str, Any]) -> str:
    """单条记忆渲染成一行提示词文本（预算按这一行的实际长度计算）。"""
    sev = item.get("severity") or DEFAULT_SEVERITY
    head = f"- 【{sev}】{item.get('summary') or item.get('issue_key') or ''}"
    tail = f"（累计 {int(item.get('hit_count') or 0)} 次"
    examples = item.get("examples") or []
    if examples:
        snippet = str(examples[-1].get("snippet") or "").strip()
        if snippet:
            tail += f"；例：「{snippet}」"
    tail += "）"
    return head + tail


def render_memory_block(items: list[dict[str, Any]]) -> str:
    """把 `load_memories()` 的结果渲染成提示词块文本；空列表返回空串。

    只追加不解释：调用方把返回文本拼在给 Writer 的输入之后即可，
    列表为空时返回 ""——与 prompt_overrides 的「空覆写等价原行为」同款
    兼容底线：没有记忆时注入链路逐字节等于没有这块。
    """
    if not items:
        return ""
    return "\n".join(_render_line(it) for it in items)


def load_memories(
    db: Any,
    project_id: int | None = None,
    *,
    limit: int = DEFAULT_LIMIT,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """取回注入提示词用的记忆：合并 global + 本项目，去重、排序、裁剪。

    Args:
        db: SQLAlchemy Session。
        project_id: 当前项目 id；None 表示只取 global（无项目上下文时）。
        limit: 条数上限（须 >= 1）。
        char_budget: 字符预算，按**渲染后文本**的累计长度计算（须 >= 0）。

    Returns:
        可直接 `render_memory_block()` 的字典列表，元素结构见 `_row_to_item`。

    设计要点：

    - **合并与去重**：global 行与 project 行都可能是同一个 issue_key 的
      教训（先在别的项目踩过坑，本项目又踩）。同一 key 项目行胜出——
      项目级总结更具体；全局行被跳过，避免同一条问题在提示词里出现两遍。
    - **排序**：见 `_sort_key`（严重度 > 次数 > 最近性）。
    - **双重裁剪**：先按 `limit` 截条数，再按 `char_budget` 截字符。
      预算按渲染后行长精确计算，而不是按 summary 长度估算——估算会在
      「summary 短但例句长」时失准，提示词还是会被撑爆。
    - **首条必收**：若预算连第一条都装不下，仍然返回第一条。一个记忆都
      渲染不出来说明预算/数据口径出了问题，此时返回空块会把故障静默成
      「没有记忆」；收首条至少让调用方看到数据存在、去修预算。
    """
    from sqlalchemy import select

    from app.models import AIFlavorMemory

    if not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit 须为 >= 1 的整数，收到 {limit!r}")
    if not isinstance(char_budget, int) or char_budget < 0:
        raise ValueError(f"char_budget 须为非负整数，收到 {char_budget!r}")

    stmt = select(AIFlavorMemory).where(AIFlavorMemory.active.is_(True))
    if project_id is None:
        stmt = stmt.where(AIFlavorMemory.scope == "global")
    else:
        stmt = stmt.where(
            (AIFlavorMemory.scope == "global")
            | (
                (AIFlavorMemory.scope == "project")
                & (AIFlavorMemory.project_id == int(project_id))
            )
        )
    rows = list(db.scalars(stmt).all())

    # 项目行优先去重（字典保序：先放项目行，global 行同 key 即被跳过）
    project_rows = [r for r in rows if r.scope == "project"]
    global_rows = [r for r in rows if r.scope == "global"]
    merged: dict[str, dict[str, Any]] = {}
    for row in project_rows + global_rows:
        merged.setdefault(row.issue_key, _row_to_item(row))

    items = sorted(merged.values(), key=_sort_key)

    # 双重裁剪：条数 + 字符预算（预算按渲染后行长计）
    trimmed: list[dict[str, Any]] = []
    used = 0
    for item in items:
        if len(trimmed) >= limit:
            break
        cost = len(_render_line(item))
        if trimmed and used + cost > char_budget:
            continue  # 跳过装不下的，给后面更短的内存行机会
        trimmed.append(item)
        used += cost
    return trimmed
