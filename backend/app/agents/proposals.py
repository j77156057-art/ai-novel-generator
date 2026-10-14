"""EditProposal 提案状态机 + Version 快照 / 回滚（ADR-2026-10-12 第 5 步第 4 步）。

这是全项目**第一条写入路径**，因此本模块的写法与其它 agent 模块刻意不同：
其它模块是「只读 + 尽量不出错」，这里是「只写经过审批的那一处，且失败不留半截」。

## 四条不可协商的产品红线

1. **Assistant 依然不能写任何东西。**
   本模块的所有写函数（`apply_proposal` / `restore_version`）**只**由
   `routers/assistant_proposals.py` 的用户审批端点调用，且前置状态机校验
   （必须 `approved`）。`app.agents.registry` 的白名单里没有任何写工具，
   Assistant 模型无法触达这里 —— 它只能通过 `/api/assistant/proposals`
   把「想改什么」写成一条 `pending` 提案，然后**停下来等人**。

2. **提案的创建必须由用户显式指定改什么。**
   `build_proposal()` 要求 `type` + `target_kind` + `target_id` + `after_text`
   四者齐全，缺一不可。诊断 issue 可以作为 `reason` / `evidence` 的**素材**
   被引用（`from_issue`），但它**不能**替用户决定改哪儿、改成什么 ——
   那部分只能来自请求体本身。

3. **目标漂移即 stale，绝不静默重算。**
   提案创建时把目标的 sha256 存进 `scope_json.base_content_hash`；
   apply 前重算当前目标哈希，不一致 → 提案置 `stale`（终态）+ HTTP 409，
   要求重新生成提案。任何「看着差不多就自动打补丁」都是错的方向。

4. **要么都成，要么都不成。**
   `apply_proposal` 与 `restore_version` 把「写业务表 + 写 Version 快照 +
   改提案状态」放在**同一个事务**里：全部成功才`db.commit()`，
   任一步抛错立即 `db.rollback()` 并把异常原样冒泡。
   不存在「业务表改了但快照没写」的中间态。

## 为什么不用 LangGraph

审批流的状态是 5 个节点、单向为主、无分支并行、无循环：
`pending → approved → applied`，加两个终态 `rejected` / `stale`，
外加一条 `applied → rolled_back` 的回滚边。这个规模用**显式状态转移表**
（`ALLOWED_TRANSITIONS`）表达得更直接：转移条件是纯函数、可离线复现、
能被单测逐条枚举。为它拉一个图依赖只会把「谁在什么状态下能做什么」
这类必须一眼可查的事实藏进节点连线里。派单里「若纯状态机能完成就不加依赖」
的条件成立，故本步**不引入** LangGraph。

## patch 的能力边界（硬约束 4）

`EditProposal.patch_json` **只能**描述对以下三处的修改：

| target_kind | 落点 | 说明 |
|---|---|---|
| `generation_text` | `generations.text` | 段落正文 |
| `project_plot_bible` | `projects.plot_bible` | 剧情圣经（权威层） |
| `prompt_override` | `prompt_overrides.override_text` | 提示词**运行时覆写表** |

明确**不**提供：删除 Project、删除 Generation、修改 AppSetting（LLM key）。
这三类能力即使将来也不走提案审批 —— LLM key 是运维配置，不是创作产物。
`validate_patch()` 用白名单 + 黑名单双重把关：出现未知键、出现 delete 类op、
出现黑名单目标名，一律`ValueError`（router 转 422）。

改提示词**只**写运行时覆写表，`backend/app/generation/prompts.py` 源文件
一个字节都不动（b58c763 基线）。
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

# ====================================================================
# 取值域常量（fail loud 的第一道闸：非法值在这里就被拒，不进库）
# ====================================================================

#: 提案类型。每一类**恰好**对应一个 target_kind —— 一一映射是刻意的：
#: 多对多会让「这个提案到底改哪儿」变成需要读代码才能回答的问题。
PROPOSAL_TYPES: tuple[str, ...] = ("text_edit", "prompt_edit", "fact_resolution")

#: 提案类型 → target_kind 的唯一映射。
TYPE_TO_TARGET_KIND: dict[str, str] = {
    "text_edit": "generation_text",
    "prompt_edit": "prompt_override",
    "fact_resolution": "project_plot_bible",
}

#: 合法的 target_kind 全集（= set(TYPE_TO_TARGET_KIND.values())）。
TARGET_KINDS: tuple[str, ...] = tuple(TYPE_TO_TARGET_KIND.values())

#: Version.kind 取值域。
VERSION_KINDS: tuple[str, ...] = ("text", "prompt", "bible", "mixed")

#: 风险等级。
RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")

#: 提案状态机的全部状态。
PROPOSAL_STATUSES: tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "applied",
    "stale",
    "rolled_back",
)

#: 状态转移表：`{当前状态: {允许的动作}}`。
#:
#: `restore` 不在这里 —— 它不是提案的动作，而是 Version 的动作，
#: 由 `restore_version()` 单独处理（`applied → rolled_back`）。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"approve", "reject"}),
    "approved": frozenset({"apply"}),
    "applied": frozenset(),
    "rejected": frozenset(),
    "stale": frozenset(),
    "rolled_back": frozenset(),
}

#: patch_json 允许出现的键（白名单）。
ALLOWED_PATCH_KEYS: frozenset[str] = frozenset({"fields", "merge", "prompt_key"})

#: scope_json 里**服务端独占**的键，请求体一律不得指定（P0/P1 修复）。
#:
#: 这四个键共同构成「审批依据 + 漂移判据」：
#: - `base_content_hash`：apply 前漂移比对的基线（= before 的 sha256）。
#:   允许请求体写它 → 传空串即可让 `str(... or "")` 变成 falsy，
#:   再配合漂移判定的 `if expected_hash and ...` 短路，漂移检测**整个失效**，
#:   用户手改内容被静默覆盖。
#: - `diff` / `impact` / `target_snapshot`：审批人看到的「改了什么、影响多大」。
#:   允许请求体写它 → 真实改动是整段替换，审批人看到的是「仅修正一个错别字」。
#:
#: 一句话：这些不是「用户元数据」，是**协议的判定依据**，必须由服务端产出。
SERVER_COMPUTED_SCOPE_KEYS: frozenset[str] = frozenset({
    "base_content_hash",
    "target_snapshot",
    "diff",
    "impact",
})

#: patch_json 里禁止出现的键片段（黑名单）—— 这些名字代表「越过审批流的破坏性操作」。
FORBIDDEN_PATCH_TOKENS: tuple[str, ...] = (
    "delete",
    "drop",
    "truncate_table",
    "app_setting",
    "api_key",
    "llm_key",
    "provider",
    "password",
    "secret",
)

#: 禁止作为修改目标的表名/ 对象名（即便有人伪造 target_kind 也拦一道）。
FORBIDDEN_TARGET_NAMES: frozenset[str] = frozenset({
    "app_settings",
    "app_setting",
    "projects_delete",
    "generations_delete",
    "raw_novels",
    "novel_chunks",
    "kg_nodes",
    "kg_edges",
    "facts",
    "agent_runs",
    "evidence",
})

#: 版本指纹的最大长度（超过即视为异常输入，直接拒）。
MAX_TEXT_LEN = 200_000

#: 单份快照允许的最大字符数（防爆库；正文段落远小于此）。
MAX_SNAPSHOT_LEN = 400_000


# ====================================================================
# 异常类型（router 按类型映射 HTTP 状态码）
# ====================================================================


class ProposalError(Exception):
    """提案域的基类异常。"""


class PatchRejected(ProposalError):
    """patch 越界（未知键 / 删除类 op / 黑名单目标）→ router 转 422。"""


class InvalidTransition(ProposalError):
    """状态机拒绝该转移 → router 转 409。"""

    def __init__(self, current: str, action: str) -> None:
        self.current = current
        self.action = action
        super().__init__(
            f"提案状态 {current!r} 不允许动作 {action!r}；"
            f"合法转移：{sorted(ALLOWED_TRANSITIONS.get(current, frozenset()))}"
        )


class TargetDrifted(ProposalError):
    """目标内容已变（提案基线哈希与当前哈希不一致）→ 提案置 stale + 409。

    `current_hash` / `expected_hash` 放进异常属性，便于测试与排障直接比对，
    不用再去翻库。
    """

    def __init__(self, expected_hash: str, current_hash: str) -> None:
        self.expected_hash = expected_hash
        self.current_hash = current_hash
        super().__init__(
            "目标内容已变化，提案标记为 stale，需重新生成提案："
            f"基线 {expected_hash[:12]}… ≠ 当前 {current_hash[:12]}…"
        )


class TargetMissing(ProposalError):
    """目标行不存在 / 与提案的project 不匹配 → 404。"""


# ====================================================================
# 小工具：哈希 / 规范化 / diff
# ====================================================================


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def canonical_text(value: Any) -> str:
    """把任意目标内容规范成**稳定字符串**，供哈希与 diff 使用。

    - `str` → 原样（正文段落走这条）；
    - `None` → `""`（未设置的 plot_bible 视作空对象，语义上等价）；
    - dict / list → `json.dumps(sort_keys=True, ensure_ascii=False)`。

    `sort_keys=True` 是哈希稳定的前提：同一个 dict 换了插入顺序也必须得到
    同一个 content_hash，否则漂移检测会被无��的序列化噪声触发。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def content_hash(value: Any) -> str:
    """目标内容的 sha256（对 `canonical_text` 的结果取哈希）。"""
    payload = canonical_text(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def request_hash(payload: Any) -> str:
    """请求体哈希（写进 AgentRun.request_hash，同一请求重放可关联）。"""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def unified_diff(before: str, after: str, *, context: int = 2,
                 before_label: str = "before", after_label: str = "after") -> str:
    """生成人类可读的 unified diff（提案详情与 Version 对比都用它）。

    正文很长时不限制输出长度会导致响应体爆掉，因此统一在源头截断到
    200 行；截断标记写进返回文本，**不静默**丢尾巴。
    """
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=before_label,
            tofile=after_label,
            lineterm="",
            n=context,
        )
    )
    if not diff_lines:
        return ""
    truncated = diff_lines[:200]
    if len(diff_lines) > len(truncated):
        truncated.append(f"...（diff 共{len(diff_lines)} 行，已截断）")
    return "\n".join(truncated)


# ====================================================================
# patch 校验：能力边界的 enforcement
# ====================================================================


def validate_patch(
    *,
    proposal_type: str,
    target_kind: str,
    target_id: str,
    patch: dict[str, Any] | None,
    prompt_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """校验并归一化 patch_json；不合法抛 `PatchRejected`。

    三重把关：

    1. **类型 ↔ 目标一致性**：`target_kind` 必须是 `TYPE_TO_TARGET_KIND[type]`。
       传 `type="text_edit"` + `target_kind="prompt_override"` 会被拒 ——
       这类组合会让审计表里的 type 与实际落点对不上。
    2. **键白名单**：patch 只允许 `fields` / `merge` / `prompt_key`。
       多余的键（哪怕看着无害）一律拒 —— 请求体已经是 StrictRequest，
       patch_json 是嵌套字典，这一层是唯一的把关点。
    3. **破坏性 token 黑名单**：任何键名或值里出现 `delete` / `api_key` 等
       片段即拒。这不是靠字符串匹配的「安全」，而是防止后续有人顺手加一个
       `{"op": "delete"}` 就把破坏性能力带进来。

    返回归一化后的 patch（键序稳定，便于哈希与快照复现）。
    """
    if proposal_type not in PROPOSAL_TYPES:
        raise PatchRejected(
            f"type 必须是 {PROPOSAL_TYPES} 之一，收到 {proposal_type!r}"
        )
    expected_kind = TYPE_TO_TARGET_KIND[proposal_type]
    if target_kind != expected_kind:
        raise PatchRejected(
            f"type={proposal_type!r} 只能作用于 target_kind={expected_kind!r}，"
            f"收到 {target_kind!r}"
        )
    if not str(target_id or "").strip():
        raise PatchRejected("target_id 不能为空（提案必须指向一个确定的目标）")

    normalized: dict[str, Any] = dict(patch or {})

    # —— 黑名单：键名与字符串值里的破坏性 token——
    for key, value in normalized.items():
        key_l = str(key).lower()
        for token in FORBIDDEN_PATCH_TOKENS:
            if token in key_l:
                raise PatchRejected(
                    f"patch 键 {key!r} 命中禁止片段 {token!r}；"
                    "提案通道不提供删除 / 配置类操作"
                )
        if isinstance(value, str):
            value_l = value.lower()
            for token in FORBIDDEN_PATCH_TOKENS:
                if token in value_l:
                    raise PatchRejected(
                        f"patch[{key!r}] 的值命中禁止片段 {token!r}"
                    )

    # —— 黑名单：目标名 ——
    tid_l = str(target_id).strip().lower()
    if tid_l in FORBIDDEN_TARGET_NAMES:
        raise PatchRejected(
            f"target_id={target_id!r} 是禁止的修改目标；"
            "提案只能改generations.text / projects.plot_bible / 提示词运行时覆写"
        )

    # —— 白名单：键集合 ——
    extra = set(normalized) - ALLOWED_PATCH_KEYS
    if extra:
        raise PatchRejected(
            f"patch 含未知键 {sorted(extra)}；只允许 {sorted(ALLOWED_PATCH_KEYS)}"
        )

    # —— 按type 补齐 /校验语义键——
    if target_kind == "generation_text":
        fields = normalized.get("fields", ["text"])
        if list(fields) != ["text"]:
            raise PatchRejected(
                f"text_edit 只能改 fields=['text']，收到 {fields!r}"
            )
        if not str(target_id).strip().isdigit():
            raise PatchRejected(
                f"text_edit 的 target_id 必须是 generations.id（整数），收到 {target_id!r}"
            )
        normalized["fields"] = ["text"]
    elif target_kind == "project_plot_bible":
        fields = normalized.get("fields", ["plot_bible"])
        if list(fields) != ["plot_bible"]:
            raise PatchRejected(
                f"fact_resolution 只能改 fields=['plot_bible']，收到 {fields!r}"
            )
        if str(target_id).strip() != "plot_bible":
            raise PatchRejected(
                "fact_resolution 的 target_id 必须是 'plot_bible'"
            )
        normalized["fields"] = ["plot_bible"]
    else:  # prompt_override
        allowed_keys = tuple(prompt_keys or ())
        prompt_key = normalized.get("prompt_key") or str(target_id).strip()
        if allowed_keys and prompt_key not in allowed_keys:
            raise PatchRejected(
                f"prompt_key={prompt_key!r} 不在可覆写范围 {list(allowed_keys)} 内"
            )
        normalized["prompt_key"] = prompt_key
        normalized["fields"] = ["override_text"]
        normalized.setdefault("merge", False)

    return normalized


def validate_action(current_status: str, action: str) -> None:
    """状态机闸门：`(current_status, action)` 不合法抛 `InvalidTransition`。"""
    allowed = ALLOWED_TRANSITIONS.get(current_status)
    if allowed is None:
        raise InvalidTransition(current_status, action)
    if action not in allowed:
        raise InvalidTransition(current_status, action)


def validate_risk_level(value: str | None, *, default: str = "low") -> str:
    """风险等级校验；None 走默认值，非法值抛 `PatchRejected`（→ 422）。"""
    if value is None:
        return default
    if value not in RISK_LEVELS:
        raise PatchRejected(
            f"risk_level 必须是 {RISK_LEVELS} 之一，收到 {value!r}"
        )
    return value


# ====================================================================
# 目标读写：三类目标各自的 before/after 语义
# ====================================================================


@dataclass(frozen=True)
class TargetState:
    """某一时刻的目标内容 + 其指纹。"""

    target_kind: str
    target_id: str
    content: str          # canonical_text 之后的字符串
    hash: str             # sha256(content)
    exists: bool          # 目标行是否存在（generation 不存在时为 False）

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "content_hash": self.hash,
            "exists": self.exists,
        }


def read_target(db: Any, project_id: int, target_kind: str,
                target_id: str) -> TargetState:
    """读目标当前内容并算指纹。**纯读**，不产生任何副作用。

    `prompt_override` 目标读的是「当前生效的覆写文本」；没有覆写时内容是空串
    （= 代码默认），这样「把覆写删掉」等价于「回到代码默认」能被 diff 正确表达。
    """
    if target_kind == "generation_text":
        from app.models import Generation

        gid = int(target_id)
        gen = db.get(Generation, gid)
        if gen is None or int(gen.project_id) != int(project_id):
            raise TargetMissing(f"generations.id={target_id} 不存在或不属于本项目")
        content = canonical_text(gen.text or "")
        return TargetState(target_kind, target_id, content,
                           content_hash(content), True)

    if target_kind == "project_plot_bible":
        from app.models import Project

        proj = db.get(Project, project_id)
        if proj is None:
            raise TargetMissing(f"project {project_id} 不存在")
        content = canonical_text(proj.plot_bible)
        return TargetState(target_kind, target_id or "plot_bible", content,
                           content_hash(content), True)

    if target_kind == "prompt_override":
        from app.agents.prompt_overrides import get_active_override
        from app.agents.prompt_overrides import CODE_DEFAULTS

        key = str(target_id).strip()
        row = get_active_override(db, project_id, key)
        if row is not None:
            content = canonical_text(row.override_text)
        else:
            # 无覆写 → 生效内容是代码默认块
            content = canonical_text(CODE_DEFAULTS.get(key, ""))
        return TargetState(target_kind, key, content, content_hash(content),
                           row is not None)

    raise PatchRejected(f"未知 target_kind={target_kind!r}")


def write_target(db: Any, project_id: int, target_kind: str, target_id: str,
                 new_content: str) -> None:
    """把新内容写入目标（**不 commit** —— 事务边界由调用方掌握）。

    只改一列，不碰其它字段：写 `generations.text` 不会顺手覆盖 summary，
    写 `plot_bible` 不会动 title / outline。提案的爆炸半径就是 patch 说清的那一处。
    """
    if target_kind == "generation_text":
        from app.models import Generation

        gid = int(target_id)
        gen = db.get(Generation, gid)
        if gen is None or int(gen.project_id) != int(project_id):
            raise TargetMissing(f"generations.id={target_id} 不存在或不属于本项目")
        gen.text = new_content
        return

    if target_kind == "project_plot_bible":
        from app.models import Project

        proj = db.get(Project, project_id)
        if proj is None:
            raise TargetMissing(f"project {project_id} 不存在")
        proj.plot_bible = json.loads(new_content) if new_content else None
        return

    if target_kind == "prompt_override":
        from app.agents.prompt_overrides import set_active_override

        set_active_override(
            db,
            project_id=project_id,
            prompt_key=str(target_id).strip(),
            override_text=new_content,
        )
        return

    raise PatchRejected(f"未知 target_kind={target_kind!r}")


def _coerce_after_content(proposal_type: str, after_text: str | None) -> str:
    """校验并归一化 after_text。

    `fact_resolution` 写的是 plot_bible，必须是**合法 JSON** —— 让它在写库前
    就失败，而不是等到下一次读 plot_bible 的代码里炸出一个 KeyError。
    """
    text = after_text or ""
    if len(text) > MAX_TEXT_LEN:
        raise PatchRejected(
            f"after_text 超长（{len(text)} > {MAX_TEXT_LEN}）"
        )
    if proposal_type == "fact_resolution":
        if not text.strip():
            return "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PatchRejected(
                f"fact_resolution 的 after_text 必须是合法 JSON：{exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise PatchRejected(
                "fact_resolution 的 after_text 必须是 JSON **对象**（plot_bible 是 dict）"
            )
        return canonical_text(parsed)
    if proposal_type == "prompt_edit" and not text.strip():
        raise PatchRejected("prompt_edit 的 after_text 不能为空（空串语义是「回到代码默认」，"
                            "那应该用删除覆写的独立流程，不在提案通道里表达）")
    return text


# ====================================================================
# 提案构建（写入 pending 行；由用户审批端点调用）
# ====================================================================


def build_proposal(
    db: Any,
    *,
    run_id: int,
    project_id: int,
    proposal_type: str,
    target_kind: str,
    target_id: str,
    after_text: str | None,
    reason: str,
    scope: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    risk_level: str | None = None,
    created_by: str | None = None,
    prompt_keys: Iterable[str] | None = None,
) -> Any:
    """构造一条 `pending` 提案并 `db.add`（**不 commit**，事务由caller 收口）。

    此刻**零业务表写入** —— 只往 `edit_proposals` 落一行待审记录。
    before/after、影响范围、关联证据、风险等级在创建时就固化，
    这样审批人看到的就是将要发生的事，而不是一句「建议优化」。

    `scope_json` 里存 `base_content_hash`（= before 的 sha256）——
    这是第4 条红线（漂移即 stale）的判据来源。
    """
    from app.models import EditProposal

    patch = validate_patch(
        proposal_type=proposal_type,
        target_kind=target_kind,
        target_id=target_id,
        patch=scope.get("patch") if isinstance(scope, dict) else None,
        prompt_keys=prompt_keys,
    )

    before = read_target(db, project_id, target_kind, target_id)
    after = _coerce_after_content(proposal_type, after_text)
    if content_hash(after) == before.hash:
        raise PatchRejected(
            "after_text 与目标当前内容完全一致（无变化）；"
            "不生成空提案，避免审批列表被无意义条目淹没"
        )

    scope_payload: dict[str, Any] = {
        "base_content_hash": before.hash,
        "target_snapshot": before.to_dict(),
        "diff": unified_diff(before.content, after,
                             before_label=f"{target_kind}:{target_id}",
                             after_label="proposed"),
        "impact": _describe_impact(target_kind, target_id),
    }
    if isinstance(scope, dict):
        for key, value in scope.items():
            if key == "patch":
                # patch 是 validate_patch 的输入，不是产物；已被上面消费掉
                continue
            if key in SERVER_COMPUTED_SCOPE_KEYS:
                # 服务端独占键：**显式拒绝**，不静默丢弃。
                #
                # 这四个键是审批人判断「该不该批准」的唯一依据，也是 apply 时
                # 漂移检测的判据。允许请求体覆盖它们 = 把「 diff 说改了一个字，
                # 实际整段替换」和「关掉漂移检测、静默覆盖别人手改的内容」
                # 两条攻击路径敞开给任意调用方。
                #
                # 为什么不静默丢弃：静默丢弃会让调用方以为自己的值生效了 ——
                # 传了空 base_content_hash 以为关闭了检测，实际检测到与否全凭
                # 服务端当次的计算结果。这类「自以为的安全感」比直接报错危险
                # 得多：它会让人放心地依赖一个根本不存在的保证。fail loud 才是
                # 把契约讲清楚的做法。
                raise PatchRejected(
                    f"scope.{key} 是服务端计算的结果，禁止由请求体指定；"
                    f"服务端独占键：{sorted(SERVER_COMPUTED_SCOPE_KEYS)}"
                )
            scope_payload[key] = value
    scope_payload["patch"] = patch

    proposal = EditProposal(
        run_id=run_id,
        project_id=project_id,
        type=proposal_type,
        target_kind=target_kind,
        target_table=_legacy_target_table(target_kind),
        target_id=str(target_id),
        patch_json=patch,
        scope_json=scope_payload,
        before_text=before.content if len(before.content) <= MAX_SNAPSHOT_LEN else "",
        after_text=after,
        reason=reason or "",
        evidence_json=list(evidence or []),
        risk_level=validate_risk_level(risk_level, default=_default_risk(proposal_type)),
        status="pending",
        version_id=None,
        created_by=created_by,
        created_at=_now_utc(),
        approved_by=None,
        approved_at=None,
        applied_at=None,
    )
    db.add(proposal)
    db.flush()
    return proposal


def _legacy_target_table(target_kind: str) -> str:
    """`target_kind` → 旧 `target_table` 字段的镜像值（NOT NULL 兼容用）。"""
    return {
        "generation_text": "generations",
        "project_plot_bible": "projects",
        "prompt_override": "prompt_overrides",
    }[target_kind]


def _default_risk(proposal_type: str) -> str:
    """默认风险等级：改权威层与正文为 high/medium，纯提示词附加块为 low。

    理由：`fact_resolution` 直接改剧情圣经（后续所有段落的约束来源），
    `text_edit` 改已生成正文（用户可能已基于它做了后续决策），
    两者出错的影响面都超出「再加一句话」；
    `prompt_edit` 只是追加一段提示块，最坏结果是文风漂移，可一键回滚。
    """
    return {
        "fact_resolution": "high",
        "text_edit": "medium",
        "prompt_edit": "low",
    }[proposal_type]


def _describe_impact(target_kind: str, target_id: str) -> str:
    """人可读的影响范围（一句话，进scope_json.impact）。"""
    return {
        "generation_text": f"仅改写第 {target_id} 段的正文（generations.text），"
                           f"不影响其它段、summary 与剧情圣经",
        "project_plot_bible": "改写剧情圣经（projects.plot_bible）；"
                              "后续生成的角色锁定 / 伏笔约束会随之变化",
        "prompt_override": f"改写提示词运行时覆写键 {target_id!r}；"
                           f"只影响后续生成的 system/user 附加块，代码默认提示不变",
    }[target_kind]


# ====================================================================
# Version 快照与 apply / restore
# ====================================================================


def insert_version_row(
    db: Any,
    *,
    proposal_id: int,
    project_id: int,
    kind: str,
    snapshot: dict[str, Any],
    content_hash_value: str,
    created_by: str | None,
    source_proposal_id: int | None = None,
    is_restore: bool = False,
) -> Any:
    """写一条 Version 快照行并 `db.flush()`（**不 commit**）。

    独立成函数有两个理由：
    1. apply / restore 共用同一份快照口径，不会出现两处各写一遍渐渐跑偏；
    2. 测试可以 monkeypatch 本函数让它抛错，从而**真实**验证
       「业务表写入后失败 → 整个事务回滚，Version 与业务表都不留痕迹」，
       而不是靠 mock 假装失败。
    """
    from app.models import Version

    version = Version(
        proposal_id=proposal_id,
        project_id=project_id,
        kind=kind,
        snapshot_json=snapshot,
        content_hash=content_hash_value,
        source_proposal_id=source_proposal_id if source_proposal_id is not None else proposal_id,
        created_by=created_by,
        created_at=_now_utc(),
        is_restore=is_restore,
    )
    db.add(version)
    db.flush()
    return version


def _version_kind_for(target_kind: str) -> str:
    return {
        "generation_text": "text",
        "project_plot_bible": "bible",
        "prompt_override": "prompt",
    }[target_kind]


def _build_snapshot(
    *,
    target_kind: str,
    target_id: str,
    proposal_type: str,
    before: TargetState,
    after_content: str,
    reason: str | None,
    evidence: list[dict[str, Any]] | None,
    source_proposal_id: int | None,
) -> dict[str, Any]:
    """组装 Version.snapshot_json（键序稳定 → 同样内容得到同样哈希）。"""
    return {
        "target_kind": target_kind,
        "target_id": target_id,
        "proposal_type": proposal_type,
        "before": before.content,
        "after": after_content,
        "before_hash": before.hash,
        "after_hash": content_hash(after_content),
        "reason": reason or "",
        "evidence": list(evidence or []),
        "source_proposal_id": source_proposal_id,
    }


def apply_proposal(
    db: Any,
    *,
    proposal: Any,
    actor: str | None = None,
    version_writer: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """应用一条 `approved` 提案：写业务表 + 写 Version + 置 applied，**同一事务**。

    步骤与每一步的失败处理：

    1. 状态机校验 `approved --apply--> applied`；不是 approved → 409。
    2. 重算目标哈希，与 `scope_json.base_content_hash` 比对；
       不一致 → 置 `stale`（**单独 commit 这一条状态变更**，
       因为「提案过期」这个事实本身必须留痕，不能随 apply 的失败一起回滚）
       → 抛 `TargetDrifted` → 409。
    3. 写业务表 → 写 Version → 置 `applied` / `version_id` / `applied_at`。
       任一步抛错 → `db.rollback()` → 异常原样冒泡 → 500/409，
       **业务表与 versions 表都不留痕迹**（无半版本）。

    幂等：已 `applied` 的提案再 apply 会被状态机拒（409），不会重复写 Version。

    `version_writer` 默认为 None → 运行期才解析成 `insert_version_row`
    （**刻意不用默认参数绑定**）：默认参数在函数定义时就固化了函数对象，
    测试 monkeypatch `insert_version_row` 将完全不起作用，回滚测试就会
    变成「测了个寂寞」。
    """
    validate_action(proposal.status, "apply")
    writer = version_writer if version_writer is not None else insert_version_row

    project_id = int(proposal.project_id)
    target_kind = str(proposal.target_kind)
    target_id = str(proposal.target_id)
    scope = proposal.scope_json if isinstance(proposal.scope_json, dict) else {}
    # 保留 None 与空串的区分意义不大 —— 两者都意味着「没有基线」，
    # 统一归一成 str 后由下面的 fail-loud 分支接住。
    raw_hash = scope.get("base_content_hash")
    expected_hash = "" if raw_hash is None else str(raw_hash)

    # —— 2) 漂移检查 ——
    current = read_target(db, project_id, target_kind, target_id)

    # 缺基线 → fail loud，**不**当成「无需检测」跳过。
    #
    # 旧写法是 `if expected_hash and current.hash != expected_hash:` ——
    # falsy 的 expected_hash（空串 / None）会让整个漂移分支短路，等于把
    # 「目标漂移即拒绝」这条不变量的开关交给了请求体。base_content_hash
    # 由 build_proposal 无条件写入，它缺失只有一种解释：提案构造有 bug
    # （或被污染），此时正确的动作是拒绝应用并让人来查，而不是放行。
    if not expected_hash.strip():
        raise ProposalError(
            f"提案 {getattr(proposal, 'id', '<unsaved>')} 缺少 "
            "scope_json.base_content_hash（漂移检测基线缺失）；"
            "拒绝应用——没有基线的补丁可能覆盖他人的并发修改。"
        )
    if current.hash != expected_hash:
        proposal.status = "stale"
        db.commit()  # 只提交「过期」这一事实，业务表未被触碰
        raise TargetDrifted(expected_hash, current.hash)

    after_content = _coerce_after_content(
        str(proposal.type), proposal.after_text
    )
    if content_hash(after_content) == current.hash:
        raise PatchRejected(
            "提案的 after 与目标当前内容一致（可能已被其它改动达成）——拒绝重复应用"
        )

    snapshot = _build_snapshot(
        target_kind=target_kind,
        target_id=target_id,
        proposal_type=str(proposal.type),
        before=current,
        after_content=after_content,
        reason=proposal.reason,
        evidence=proposal.evidence_json if isinstance(proposal.evidence_json, list) else [],
        source_proposal_id=proposal.id,
    )

    try:
        # —— 3) 写业务表 → 写 Version → 改状态，全部在一个事务里——
        write_target(db, project_id, target_kind, target_id, after_content)
        db.flush()
        version = writer(
            db,
            proposal_id=proposal.id,
            project_id=project_id,
            kind=_version_kind_for(target_kind),
            snapshot=snapshot,
            content_hash_value=snapshot["after_hash"],
            created_by=actor or proposal.approved_by,
            source_proposal_id=proposal.id,
            is_restore=False,
        )
        proposal.status = "applied"
        proposal.version_id = version.id
        proposal.applied_at = _now_utc()
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "proposal_id": proposal.id,
        "version_id": version.id,
        "kind": version.kind,
        "content_hash": version.content_hash,
        "target_kind": target_kind,
        "target_id": target_id,
        "status": proposal.status,
        "applied_at": proposal.applied_at.isoformat() if proposal.applied_at else None,
    }


def restore_version(
    db: Any,
    *,
    version: Any,
    actor: str | None = None,
    version_writer: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """从 Version 快照恢复目标内容；**恢复本身也生成一条新 Version**。

    语义（三条，缺一不可）：

    - **历史不丢**：不 UPDATE / DELETE 任何既有 Version 行，也不删提案。
      `v0 → v1 → v2 → restore(v1)` 得到 4 条 Version 记录。
    - **回滚是新版本**：`restore` 产出的 Version 记录的是「恢复前」的内容作为
      `before`、「被恢复的内容」作为 `after`，因此再回滚一次能原路返回。
    - **同事务**：写业务表 + 写新 Version + 置提案 `rolled_back` 一起提交；
      中途失败 → 整体回滚，目标内容保持恢复前的状态。

    提案状态：只有 `applied` / `approved` / `rolled_back` 的提案能被置
    `rolled_back`（它们确实落地过）。`pending` / `rejected` / `stale` 的提案
    没有生效过，restore 它们的版本没有意义 → 409。
    """
    from app.models import EditProposal

    writer = version_writer if version_writer is not None else insert_version_row
    snapshot = version.snapshot_json if isinstance(version.snapshot_json, dict) else {}
    target_kind = str(snapshot.get("target_kind") or "")
    target_id = str(snapshot.get("target_id") or "")
    after_content = canonical_text(snapshot.get("after"))
    if not target_kind or not target_id:
        raise ProposalError(
            f"Version.id={version.id} 的 snapshot_json 缺少 target_kind / target_id，无法恢复"
        )

    source_proposal_id = (
        version.source_proposal_id if version.source_proposal_id is not None
        else version.proposal_id
    )
    source_proposal = (
        db.get(EditProposal, source_proposal_id) if source_proposal_id else None
    )
    if source_proposal is not None:
        if source_proposal.status not in {"applied", "approved", "rolled_back"}:
            raise InvalidTransition(source_proposal.status, "restore")

    project_id = int(version.project_id or (
        source_proposal.project_id if source_proposal is not None else 0
    ))
    current = read_target(db, project_id, target_kind, target_id)

    restore_snapshot = _build_snapshot(
        target_kind=target_kind,
        target_id=target_id,
        proposal_type=str(
            (snapshot.get("proposal_type") or "")
            or (source_proposal.type if source_proposal is not None else "")
        ),
        before=current,
        after_content=after_content,
        reason=f"restore from Version.id={version.id}",
        evidence=[],
        source_proposal_id=source_proposal_id,
    )

    try:
        write_target(db, project_id, target_kind, target_id, after_content)
        db.flush()
        new_version = writer(
            db,
            proposal_id=source_proposal_id or version.proposal_id,
            project_id=project_id,
            kind=str(version.kind or _version_kind_for(target_kind)),
            snapshot=restore_snapshot,
            content_hash_value=restore_snapshot["after_hash"],
            created_by=actor,
            source_proposal_id=source_proposal_id,
            is_restore=True,
        )
        if source_proposal is not None:
            source_proposal.status = "rolled_back"
            source_proposal.version_id = new_version.id
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "restored_from_version_id": version.id,
        "version_id": new_version.id,
        "kind": new_version.kind,
        "content_hash": new_version.content_hash,
        "target_kind": target_kind,
        "target_id": target_id,
        "proposal_id": source_proposal_id,
        "proposal_status": source_proposal.status if source_proposal else None,
        "is_restore": True,
    }


# ====================================================================
# 出参构造（proposal / version 的统一 dict 形状）
# ====================================================================


def proposal_to_dict(proposal: Any) -> dict[str, Any]:
    """提案的统一出参形状（含 diff 与目标基线指纹，便于前端直接渲染）。"""
    scope = proposal.scope_json if isinstance(proposal.scope_json, dict) else {}
    return {
        "id": proposal.id,
        "run_id": proposal.run_id,
        "project_id": proposal.project_id,
        "type": proposal.type,
        "target_kind": proposal.target_kind,
        "target_table": proposal.target_table,
        "target_id": proposal.target_id,
        "patch": proposal.patch_json,
        "status": proposal.status,
        "risk_level": proposal.risk_level,
        "reason": proposal.reason,
        "before_text": proposal.before_text,
        "after_text": proposal.after_text,
        "diff": scope.get("diff", ""),
        "impact": scope.get("impact", ""),
        "base_content_hash": scope.get("base_content_hash"),
        "evidence": proposal.evidence_json if isinstance(proposal.evidence_json, list) else [],
        "version_id": proposal.version_id,
        "created_by": proposal.created_by,
        "created_at": _iso(proposal.created_at),
        "approved_by": proposal.approved_by,
        "approved_at": _iso(proposal.approved_at),
        "applied_at": _iso(proposal.applied_at),
    }


def version_to_dict(version: Any) -> dict[str, Any]:
    """Version 的统一出参形状。"""
    snapshot = version.snapshot_json if isinstance(version.snapshot_json, dict) else {}
    return {
        "id": version.id,
        "project_id": version.project_id,
        "proposal_id": version.proposal_id,
        "source_proposal_id": version.source_proposal_id,
        "kind": version.kind,
        "content_hash": version.content_hash,
        "is_restore": bool(version.is_restore),
        "target_kind": snapshot.get("target_kind"),
        "target_id": snapshot.get("target_id"),
        "before_hash": snapshot.get("before_hash"),
        "after_hash": snapshot.get("after_hash"),
        "reason": snapshot.get("reason", ""),
        "created_by": version.created_by,
        "created_at": _iso(version.created_at),
    }


def version_diff(version: Any, other: Any) -> str:
    """两个 Version 之间的正文 diff（历史对比用；内容相同则空串）。"""
    a = version.snapshot_json if isinstance(version.snapshot_json, dict) else {}
    b = other.snapshot_json if isinstance(other.snapshot_json, dict) else {}
    return unified_diff(
        canonical_text(a.get("after")),
        canonical_text(b.get("after")),
        before_label=f"Version {version.id}",
        after_label=f"Version {other.id}",
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


# ====================================================================
# 自检：常量之间的相容性（import 时跑，O(1)，失败即 fail loud）
#
# 这几条断言的价值在于「改了一处忘了改另一处」时在import 阶段就炸，
# 而不是等到某个用户的提案莫名其妙走错分支才被发现。
# ====================================================================

assert set(TYPE_TO_TARGET_KIND) == set(PROPOSAL_TYPES), "type ↔ target_kind 必须一一对应"
assert set(TYPE_TO_TARGET_KIND.values()) == set(TARGET_KINDS)
assert set(PROPOSAL_STATUSES) == set(ALLOWED_TRANSITIONS), "状态集与转移表必须同步"
assert set(_default_risk(t) for t in PROPOSAL_TYPES) <= set(RISK_LEVELS)
assert {_version_kind_for(t) for t in TARGET_KINDS} <= set(VERSION_KINDS)