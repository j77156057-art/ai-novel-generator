"""Assistant 结构化诊断规则引擎（ADR-2026-10-12 第 5 步第 3 步）。

## 为什么把规则从 router 里搬出来

`routers/assistant.py` 只做三件事：校验项目、开 AgentRun、把规则结果落成
Evidence 行。**规则本身**在这里，且全部是确定性纯计算 —— 同一个库跑两次，
结果逐字节相同（L3 纪律：可复现优先于聪明）。

## 规则先行、模型后置

诊断的判据来自库里的既有事实（KG 坑状态 / 事实账本别名 / 哨兵状态表 /
生成正文），这些判据不需要语言模型。模型只在**已经命中规则**或**用户主动
带 query** 时被叫来写解释性文字；模型挂了就退回纯规则结果，请求照常成功。

这也是「Assistant 故障不得波及 Writer」的最后一道防线：模型调用在
`routers/assistant.py` 里被 try 包住，且走 `get_provider_registry().get(
ASSISTANT_ROLE)` —— 与 Writer 单例物理隔离。

## 规则清单

| rule | type | severity | 判据（全部来自库内既有数据） |
|---|---|---|---|
| `open_plothole_accumulation` | `plothole` | warn / error | `get_reminders` 的 `status=="open"` 坑数 ≥1（≥10 升 error） |
| `low_confidence_facts_overflow` | `fact_quality` | warn | Fact 表 `confidence=="low"` 行数 > `LOW_CONFIDENCE_FACT_LIMIT` |
| `name_conflict` | `identity` | warn / error | 账本 aliases 与 KG 角色节点名交叉：别名已独立成节点，或别名与另一规范名撞名 |
| `dead_character_reappearance` | `character_state` | error / warn | 哨兵状态表 `life:X=dead` × 后续生成正文再次出现 X；或 × 未填坑里仍挂着 X |

前两条是第 2 步已有的规则，本步把它们从「一句 reason」升级成结构化 issue；
后两条是本步新增，均可离线复现（只需 Fact / KgNode / Generation 三张表有数据）。

## 证据纪律

- `kind ∈ {generation, chunk, reminder, fact, kg}`；
- `source` 由 `_source_label()` 归一：`generated` 或 `novel:{id}`
  ——**参考语料与生成正文绝不混标**（P0-1 隔离纪律在诊断侧的落点）；
- `snippet` 必须是库中原文的真实片段（生成正文取包含命中名的原样子串，
  坑取 `props.question`，事实取 `Fact.key`），不做任何改写；
- `score ∈ [0,1]`，超出即钳到边界。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# ====================================================================
# 常量：阈值 / 取值域
# ====================================================================

#: 未填坑数达到该值 → 升为 error（堆积到读者已经能感觉出「作者忘了收」的程度）。
OPEN_PLOTHOLE_ERROR_LIMIT = 10

#: 低置信事实条数上限；**超过**（不含等于）才报。
LOW_CONFIDENCE_FACT_LIMIT = 5

#: `severity` 的合法取值。出参逐条校验，非法值直接构造失败（fail loud）。
ALLOWED_SEVERITIES: tuple[str, ...] = ("info", "warn", "error")

#: `evidence.kind` 的合法取值。
#:
#: 第 7 步新增两个 kind：
#: - `"run"`：日志分析（`app.agents.log_audit`）的 issue 引用的是 AgentRun
#:   审计行本身——「这次 run 失败了」的证据就是那条 run；
#: - `"novel"`：语料审核（`app.agents.audit`）的 novel 级结论（如「缺
#:   style_profile」）证据是整本书，不是单个分片。
#:
#: 既有五种 kind 都指向业务数据（生成正文 / 语料分片 / 提醒 / 事实 / 图谱），
#: 没有一种能诚实地描述这两类证据；硬套 `kg` 会让读证据的人以为结论来自
#: 图谱。新增是**加法**：既有消费方按 `in ALLOWED_EVIDENCE_KINDS` 做成员
#: 检查，不受影响。
ALLOWED_EVIDENCE_KINDS: tuple[str, ...] = (
    "generation", "chunk", "reminder", "fact", "kg", "run", "novel",
)

#: issue 文本截断上限（防止把整章正文塞进 claim/suggestion）。
_MAX_TEXT = 300

#: snippet 截断上限（与 `Evidence.snippet` 的 1000 字防线配合，先在源头收窄）。
_MAX_SNIPPET = 400


# ====================================================================
# 数据结构
# ====================================================================


def _clamp_score(value: Any) -> float:
    """把 score 钳进 [0, 1]。

    检索分（RRF 融合分）是 `2/(60+rank+1)` 量级的小数，模型给的置信度可能 >1，
    两条路径都可能越界 —— 统一在**构造点**钳一次，而不是等到消费端。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if num != num:  # NaN
        return 0.0
    return round(min(1.0, max(0.0, num)), 6)


def _source_label(raw: str | None) -> str:
    """来源标签归一：`generated` 或 `novel:{id}`。

    KG / Fact 的 `source` 列有三种历史写法（`novel` / `novel:123` /
    `novel_123`），而证据必须能一眼看出「这条是参考语料还是我们自己写的」。
    归一后只剩两种形态，诊断响应里不再出现第三种。
    """
    text = (raw or "").strip()
    if not text:
        # 空 source 的老数据按生成侧处理：`kg.queries.get_reminders` 默认就是
        # 取 source=="generated"，空值节点本来也不在任何生成侧清单里。
        return "generated"
    if text.startswith("novel"):
        tail = ""
        if ":" in text:
            tail = text.split(":", 1)[1].strip()
        elif "_" in text:
            tail = text.split("_", 1)[1].strip()
        return f"novel:{tail}" if tail else "novel"
    return text


@dataclass(frozen=True)
class IssueEvidence:
    """一条 issue 挂着的证据。字段与 `Evidence` 表一一对应（落库时不丢信息）。"""

    kind: str
    source: str
    ref_id: str
    snippet: str
    score: float

    def __post_init__(self) -> None:
        if self.kind not in ALLOWED_EVIDENCE_KINDS:
            raise ValueError(
                f"evidence.kind 必须是 {ALLOWED_EVIDENCE_KINDS} 之一，收到 {self.kind!r}"
            )
        if not self.ref_id:
            raise ValueError("evidence.ref_id 不能为空")

    @property
    def dedup_key(self) -> tuple[str, str, str, str]:
        """落库去重键：(kind, source, ref_id, snippet)。

        两条 issue 常引用同一条证据（例：一个别名既撞名又已成节点）；
        去重后 Evidence 表里只留一行，响应里两条 issue 仍各自指向它。
        """
        return (self.kind, self.source, self.ref_id, self.snippet)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "source": self.source,
            "ref_id": self.ref_id,
            "snippet": self.snippet,
            "score": _clamp_score(self.score),
        }


@dataclass(frozen=True)
class DiagnosticIssue:
    """一条结构化诊断结论。

    九个字段全部必填：id / type / severity / title / claim / evidence /
    suggestion / rule。`claim` 与 `suggestion` 分开是刻意的 ——
    claim 只说「库里是什么」，suggestion 只说「人该怎么处理」，
    混在一起就会出现「事实里没有的建议」被当成事实读。
    """

    id: str
    type: str
    severity: str
    title: str
    claim: str
    suggestion: str
    rule: str
    evidence: tuple[IssueEvidence, ...] = ()

    def __post_init__(self) -> None:
        if self.severity not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"severity 必须是 {ALLOWED_SEVERITIES} 之一，收到 {self.severity!r}"
            )
        if not self.id:
            raise ValueError("issue.id 不能为空")
        if not self.rule:
            raise ValueError("issue.rule 不能为空（触发规则名是必填溯源信息）")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "severity": self.severity,
            "title": self.title,
            "claim": self.claim[:_MAX_TEXT],
            "suggestion": self.suggestion[:_MAX_TEXT],
            "rule": self.rule,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass
class DiagnosticReport:
    """一次规则跑批的结果。

    - `issues`：规则命中的结构化结论；
    - `extra_evidence`：不属于任何单条 issue 的检索证据（带 query 时的参考语料
      命中）。**单独成字段而不是塞进某条 issue**，是因为「规则一条没命中但用户
      主动提问」是完全合法的场景 —— 那时证据仍然要回到用户手里，不能因为
      「没有 issue 可挂」而消失；
    - `tool_results`：各只读工具的原始返回，供排障核对取数是否成功；
    """

    issues: list[DiagnosticIssue] = field(default_factory=list)
    extra_evidence: list[IssueEvidence] = field(default_factory=list)
    tool_results: dict[str, Any] = field(default_factory=dict)
    rules_evaluated: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "issues": [i.to_dict() for i in self.issues],
            "extra_evidence": [e.to_dict() for e in self.extra_evidence],
            "rules_evaluated": list(self.rules_evaluated),
            "tool_results": dict(self.tool_results),
        }


# ====================================================================
# 规则上下文：一次取数，四条规则共享
# ====================================================================


class DiagnosticSnapshot:
    """一次诊断所依赖的库内快照。

    **取数只做一次**：四条规则问的是同一批表（KgNode / Fact / Generation），
    每条规则各查一遍会把 SQL 放大四倍，而且会让「同一份数据的四次读取结果
    可能不一致」变成一个真实风险（期间可能有并发写入）。
    每个属性都是惰性 + 缓存，首个访问者触发查询。
    """

    def __init__(self, db: Any, project: Any, scope: str = "all") -> None:
        self._db = db
        self.project = project
        self.project_id = int(project.id)
        self.scope = scope or "all"
        self.tool_results: dict[str, Any] = {}
        self._cache: dict[str, Any] = {}

    # ----- 工具调用（全部经 AssistantToolRegistry 白名单，角色锁不变）-----

    def _invoke(self, name: str, **kwargs: Any) -> dict:
        """经 AssistantToolRegistry 调一个只读工具。

        `PermissionError` **刻意不吞**：它意味着有代码试图让 Assistant 调写工具，
        这是角色锁被绕过的事实，必须像 `/qa` 那样 fail loud（整次 403）而不是
        悄悄返回空 dict —— 静默的权限失败读起来就是「工具坏了」。
        """
        from app.agents.registry import invoke_tool

        try:
            result = invoke_tool(name, self._db, project_id=self.project_id, **kwargs)
        except PermissionError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个工具挂掉不该让整次诊断失败
            self.tool_results[name] = {"error": str(exc)}
            return {}
        self.tool_results[name] = result
        return result

    # ----- 惰性快照 -----

    @property
    def reminders(self) -> dict:
        if "reminders" not in self._cache:
            self._cache["reminders"] = self._invoke("get_reminders")
        return self._cache["reminders"]

    @property
    def facts(self) -> list[dict]:
        """全类型 Fact（不按 type 过滤）。"""
        if "facts" not in self._cache:
            res = self._invoke("get_fact_ledger", limit=200)
            self._cache["facts"] = list(res.get("facts") or [])
        return self._cache["facts"]

    @property
    def generations(self) -> list[dict]:
        """全部生成段（升序）。诊断要跨段比对，所以不能只取最近 limit 段。"""
        if "generations" not in self._cache:
            res = self._invoke("read_generation_segments", limit=200)
            segs = list(res.get("segments") or [])
            segs.sort(key=lambda g: (g.get("segment_no") or 0))
            self._cache["generations"] = segs
        return self._cache["generations"]

    @property
    def character_facts(self) -> list[dict]:
        if "character_facts" not in self._cache:
            self._cache["character_facts"] = [
                f for f in self.facts if (f.get("type") or "") == "character"
            ]
        return self._cache["character_facts"]

    @property
    def open_plotholes(self) -> list[dict]:
        """未填坑（`get_reminders` 的 `unresolved_plotholes`）。"""
        if "open_plotholes" not in self._cache:
            self._cache["open_plotholes"] = list(
                self.reminders.get("unresolved_plotholes") or []
            )
        return self._cache["open_plotholes"]

    @property
    def kg_character_names(self) -> list[str]:
        """KG 侧（生成图）里的角色名，来自 `get_reminders` 的 character_constraints。"""
        if "kg_character_names" not in self._cache:
            names = [
                (c.get("name") or "").strip()
                for c in (self.reminders.get("character_constraints") or [])
            ]
            self._cache["kg_character_names"] = [n for n in names if n]
        return self._cache["kg_character_names"]

    @property
    def dead_entities(self) -> list[tuple[str, dict]]:
        """哨兵状态表里的死亡记录：`[(角色名, 状态 props)]`。

        复用 `kg.sentinel.load_state`（只读；它只 SELECT `Fact`，不写库）。
        """
        if "dead_entities" not in self._cache:
            from app.kg.sentinel import STATE_LIFE, load_state

            try:
                state = load_state(self._db, self.project_id, source="generated")
            except Exception as exc:  # noqa: BLE001
                self.tool_results["sentinel_state"] = {"error": str(exc)}
                state = {}
            self._cache["dead_entities"] = sorted(
                (entity, st)
                for (kind, entity), st in state.items()
                if kind == STATE_LIFE and (st.get("value") == "dead")
            )
        return self._cache["dead_entities"]

    def chunk_hits(self, query: str, k: int = 5) -> tuple[int | None, list[dict]]:
        """参考语料检索（仅当请求带 query 时才走）。

        返回 `(novel_id, hits)` —— novel_id 一并返回是为了给 evidence 打上
        `novel:{id}` 的 source 标签，隔离参考语料与生成正文。
        """
        res = self._invoke("search_novel_chunks", query=query, k=k)
        raw_id = res.get("novel_id")
        return (int(raw_id) if raw_id is not None else None), list(res.get("hits") or [])


# ====================================================================
# 通用小工具
# ====================================================================


def _window(text: str, needle: str, pad: int = 40) -> str:
    """取包含 `needle` 的原样子串（命中处前后各 pad 字）。

    刻意**不做**任何清洗或省略号拼接：证据的价值就在于「这就是原文那一段」，
    加了省略号它就不再是可复核的引用了。命中不到就返回 needle 本身。
    """
    idx = text.find(needle)
    if idx < 0:
        return needle[:_MAX_SNIPPET]
    start = max(0, idx - pad)
    end = min(len(text), idx + len(needle) + pad)
    return text[start:end]


def _truncate(text: str, limit: int = _MAX_SNIPPET) -> str:
    return (text or "").strip()[:limit]


def _make_id(rule: str, anchor: str, taken: set[str]) -> str:
    """确定性 issue id：`{rule}:{anchor}`，撞号时追加 `#n`。

    不含时间戳 / 随机数 —— 同一份库、同一份 scope 必须跑出同一个 id，
    否则「同一次诊断的两次结果对不上」这种问题没法排查。
    """
    base = f"{rule}:{anchor}" if anchor else rule
    candidate = base
    n = 1
    while candidate in taken:
        n += 1
        candidate = f"{base}#{n}"
    taken.add(candidate)
    return candidate


def make_issue_id(rule: str, anchor: str, taken: set[str]) -> str:
    """`_make_id` 的公开别名（第 7 步：log_audit / audit 模块共用同一套
    确定性 id 语义，避免各写一份导致撞号规则漂移）。"""
    return _make_id(rule, anchor, taken)


def _reminder_ref_id(hole: dict) -> str:
    """未填坑的可复核定位：`plothole:seg{first_chapter}:{question 前 24 字}`。

    `get_reminders` 不返回 KgNode.id（那是既有生成链路的返回形状，不改），
    所以定位用「首现段号 + 问题开头」——两个都是库里的原值，够用户回到原文核对。
    """
    chapter = hole.get("first_chapter")
    question = (hole.get("question") or "").strip()[:24]
    return f"plothole:seg{chapter}:{question}"


# ====================================================================
# 规则 ①：open_plothole_accumulation
# ====================================================================


def rule_open_plothole_accumulation(snap: DiagnosticSnapshot, taken: set[str]) -> list[DiagnosticIssue]:
    """未填坑堆积。

    阈值取「≥1 即报」而非「≥N 才报」：一个坑开着不报，用户会以为系统没在工作；
    而这条规则**只标红不改稿**（`kg.sentinel` 的统一纪律），报错的代价远小于漏报。
    堆积到 `OPEN_PLOTHOLE_ERROR_LIMIT` 才升 error —— severity 是给人排优先级用的，
    不该对同一个事实给出两种强度。
    """
    holes = snap.open_plotholes
    if not holes:
        return []
    count = len(holes)
    severity = "error" if count >= OPEN_PLOTHOLE_ERROR_LIMIT else "warn"
    evidence = tuple(
        IssueEvidence(
            kind="reminder",
            source="generated",
            ref_id=_reminder_ref_id(h),
            snippet=_truncate(h.get("question") or "", 200),
            score=0.5,
        )
        for h in holes[:5]
        if (h.get("question") or "").strip()
    )
    if not evidence:
        return []
    first_segs = [h.get("first_chapter") for h in holes if h.get("first_chapter") is not None]
    span = f"最早开坑于第 {min(first_segs)} 段" if first_segs else "开坑段号未知"
    return [
        DiagnosticIssue(
            id=_make_id("open_plothole_accumulation", f"open{count}", taken),
            type="plothole",
            severity=severity,
            title=f"{count} 条伏笔未回收",
            claim=(
                f"当前有 {count} 条伏笔处于 open 状态（{span}），"
                f"其中前 {len(evidence)} 条："
                + "；".join(e.snippet[:60] for e in evidence)
            ),
            suggestion=(
                "先收敛已开坑再展开新伏笔：逐条确认是「计划回收」还是「已废弃」，"
                "计划回收的挂到具体章节，废弃的从 plot_bible 撤下。"
            ),
            rule="open_plothole_accumulation",
            evidence=evidence,
        )
    ]


# ====================================================================
# 规则 ②：low_confidence_facts_overflow
# ====================================================================


def rule_low_confidence_facts_overflow(snap: DiagnosticSnapshot, taken: set[str]) -> list[DiagnosticIssue]:
    """低置信事实堆积。

    统计口径从第 2 步的「只看 character」放宽到**全部 Fact 类型** ——
    规则名叫 `low_confidence_facts_overflow`，口径就该与名字一致。
    放宽是安全的：`kg.sentinel.save_findings` / `replace_state_facts` 写的
    `alert` / `state` 事实一律 `confidence="high"`，不会把这条规则顶起来。

    低置信事实的来源是 `kg.ledger.plan_alias_merges` 的**待裁决提案**
    （自动档只收有零反例证据的两类，其余一律降级为提案）。堆积意味着
    「别名裁决跟不上抽取速度」，角色锁会被同一人的多个名字稀释。
    """
    low = [f for f in snap.facts if (f.get("confidence") or "") == "low"]
    if len(low) <= LOW_CONFIDENCE_FACT_LIMIT:
        return []
    evidence = tuple(
        IssueEvidence(
            kind="fact",
            source=_source_label(f.get("source")),
            ref_id=f"fact:{f.get('key') or ''}",
            snippet=_truncate(f.get("key") or "", 200),
            score=0.4,
        )
        for f in low[:5]
        if (f.get("key") or "").strip()
    )
    if not evidence:
        return []
    kinds = sorted({(f.get("type") or "unknown") for f in low})
    return [
        DiagnosticIssue(
            id=_make_id("low_confidence_facts_overflow", f"low{len(low)}", taken),
            type="fact_quality",
            severity="warn",
            title=f"{len(low)} 条低置信事实待裁决",
            claim=(
                f"事实账本里有 {len(low)} 条 confidence=low 的记录（类型：{'/'.join(kinds)}），"
                f"已超过阈值 {LOW_CONFIDENCE_FACT_LIMIT}。示例："
                + "、".join(e.snippet[:40] for e in evidence)
            ),
            suggestion=(
                "低置信记录来自别名自动归并之外的待裁决提案，逐条人工核对后"
                "升为 high 或丢弃；长期堆积会让同一角色以多个名字进入角色锁定清单。"
            ),
            rule="low_confidence_facts_overflow",
            evidence=evidence,
        )
    ]


# ====================================================================
# 规则 ③：name_conflict（新增）
# ====================================================================


def rule_name_conflict(snap: DiagnosticSnapshot, taken: set[str]) -> list[DiagnosticIssue]:
    """名字冲突：账本 aliases 与 KG 角色节点名对不上。

    两种可复现形态：

    1. **别名已独立成节点** —— `Fact(key=规范名, aliases=[别名])` 说这两个名字是
       同一个人，但 KG 生成图里 `别名` 仍是一个独立 character 节点。
       后果是 `kg.queries.get_reminders` 会把同一个人的两个名字当成两个
       「已锁定角色」注入提示词，两份约束互相矛盾（这正是
       `kg.ledger.apply_merges` 要改写图谱的原因 —— 图没跟上就是漏做了一半）。
    2. **别名与另一条规范名撞名** —— `A.aliases` 里有 `B`，而 `B` 自己是一条
       character Fact 的 `key`。此时「归并到 A」和「B 是独立角色」两种解释
       互相排斥，规则无法判定，必须交人裁决（**不自动合并**）。

    两种形态都不做自动修复：`ledger.py` 的实测结论是字符串包含关系给不出
    可接受精度，每一条错误合并都是静默篡改身份。
    """
    char_facts = snap.character_facts
    if not char_facts:
        return []
    kg_names = set(snap.kg_character_names)
    canonical_keys = {(f.get("key") or "").strip() for f in char_facts}
    canonical_keys.discard("")

    issues: list[DiagnosticIssue] = []

    # 形态 ①：别名已独立成 KG 节点
    for fact in sorted(char_facts, key=lambda f: (f.get("key") or "")):
        canon = (fact.get("key") or "").strip()
        aliases = [a.strip() for a in (fact.get("aliases") or []) if isinstance(a, str) and a.strip()]
        leaked = sorted(a for a in aliases if a in kg_names)
        if not leaked:
            continue
        evidence = [
            IssueEvidence(
                kind="fact",
                source=_source_label(fact.get("source")),
                ref_id=f"fact:{canon}",
                snippet=_truncate(canon, 200),
                score=0.55,
            )
        ]
        evidence.extend(
            IssueEvidence(
                kind="kg",
                source="generated",
                ref_id=f"kg:character:{alias}",
                snippet=_truncate(alias, 200),
                score=0.55,
            )
            for alias in leaked
        )
        issues.append(
            DiagnosticIssue(
                id=_make_id("name_conflict", f"split:{canon}", taken),
                type="identity",
                severity="warn",
                title=f"「{canon}」的别名已独立成图谱节点",
                claim=(
                    f"事实账本把 {'、'.join(leaked)} 登记为「{canon}」的别名，"
                    f"但生成图谱里它们仍各自是独立的 character 节点。"
                ),
                suggestion=(
                    f"确认这几个名字是否同一人；若是，按 ledger 的裁决结果改写图谱"
                    f"（重指向边、去重、删被吸收节点），否则角色锁定清单会注入两份矛盾约束。"
                ),
                rule="name_conflict",
                evidence=tuple(evidence),
            )
        )

    # 形态 ②：别名撞上另一条规范名 —— 方向不可判，交人裁决
    for fact in sorted(char_facts, key=lambda f: (f.get("key") or "")):
        canon = (fact.get("key") or "").strip()
        for alias in sorted(
            a.strip() for a in (fact.get("aliases") or []) if isinstance(a, str)
        ):
            if not alias or alias not in canonical_keys or alias == canon:
                continue
            issues.append(
                DiagnosticIssue(
                    id=_make_id("name_conflict", f"collide:{canon}->{alias}", taken),
                    type="identity",
                    severity="error",
                    title=f"「{alias}」同时是别名与独立角色",
                    claim=(
                        f"「{canon}」的 aliases 含「{alias}」，而「{alias}」自己"
                        f"也是一条 character 事实的规范名：合并与独立两种解释互斥。"
                    ),
                    suggestion=(
                        f"人工判定「{alias}」归属：若是别名则合并到「{canon}」，"
                        f"若是独立角色则从「{canon}」的 aliases 移除。规则不自动合并。"
                    ),
                    rule="name_conflict",
                    evidence=(
                        IssueEvidence(
                            kind="fact",
                            source=_source_label(fact.get("source")),
                            ref_id=f"fact:{canon}",
                            snippet=_truncate(canon, 200),
                            score=0.6,
                        ),
                        IssueEvidence(
                            kind="fact",
                            source=_source_label(fact.get("source")),
                            ref_id=f"fact:{alias}",
                            snippet=_truncate(alias, 200),
                            score=0.6,
                        ),
                    ),
                )
            )
    return issues


# ====================================================================
# 规则 ④：dead_character_reappearance（新增）
# ====================================================================


def rule_dead_character_reappearance(snap: DiagnosticSnapshot, taken: set[str]) -> list[DiagnosticIssue]:
    """已死亡角色又出现：Fact status × 生成正文 / 未填坑 交叉。

    判据全部取自库内既有事实，不做二次推断：

    - 「已死」：`Fact(type="state", key="life:{角色}")` 的 `props.value == "dead"`
      （由 `kg.sentinel` 在生成时落库，`since_segment` 记死亡段号）；
    - 「又出现」：死亡段之后的 `Generation.text` 里再次出现该角色名，
      snippet 取**包含该名字的原样子串**；
    - 「坑仍挂着」：未填坑的 question 里仍提到该角色 —— 剧情上等于要求一个
      已死角色回来收尾，与死亡记录直接冲突。

    与 `kg.sentinel.rule_dead_acts` 的边界：本条**不区分动作与单纯提及**，
    故意放宽召回。哨兵是生成环上的即时增强，宁可漏报；诊断是事后人工复核，
    宁可多报一条让人去看一眼 —— 报错的代价只是「看一眼不是 bug」。
    """
    dead = snap.dead_entities
    if not dead:
        return []
    holes = snap.open_plotholes
    gens = snap.generations
    issues: list[DiagnosticIssue] = []

    for entity, state in dead:
        since = state.get("segment_no")
        death_text = (state.get("text") or "").strip()
        later: list[tuple[dict, str]] = []
        for seg in gens:
            seg_no = seg.get("segment_no")
            if seg_no is None or (since is not None and seg_no <= since):
                continue
            body = seg.get("text") or ""
            if entity not in body:
                continue
            later.append((seg, _window(body, entity)))

        hooked = [h for h in holes if entity in (h.get("question") or "")]
        if not later and not hooked:
            continue

        severity = "error" if later else "warn"
        evidence: list[IssueEvidence] = []
        if death_text:
            evidence.append(
                IssueEvidence(
                    kind="fact",
                    source="generated",
                    ref_id=f"fact:state:life:{entity}",
                    snippet=_truncate(death_text, 200),
                    score=0.7,
                )
            )
        for seg, snippet in later[:3]:
            evidence.append(
                IssueEvidence(
                    kind="generation",
                    source="generated",
                    ref_id=str(seg.get("id") or ""),
                    snippet=_truncate(snippet),
                    score=0.65,
                )
            )
        for hole in hooked[:2]:
            evidence.append(
                IssueEvidence(
                    kind="reminder",
                    source="generated",
                    ref_id=_reminder_ref_id(hole),
                    snippet=_truncate(hole.get("question") or "", 200),
                    score=0.5,
                )
            )
        if not evidence:
            continue

        parts = []
        if later:
            seg_nos = sorted({s.get("segment_no") for s, _ in later})
            parts.append(f"第 {seg_nos} 段正文再次出现「{entity}」")
        if hooked:
            parts.append(f"{len(hooked)} 条未回收伏笔仍以「{entity}」为主角")
        where = f"（死亡记录在第 {since} 段）" if since is not None else ""
        issues.append(
            DiagnosticIssue(
                id=_make_id("dead_character_reappearance", entity, taken),
                type="character_state",
                severity=severity,
                title=f"已死亡角色「{entity}」再次出现",
                claim="；".join(parts) + where,
                suggestion=(
                    f"核对第 {since} 段之后的出现：若为回忆/幻象/他人转述则无需改动；"
                    f"若剧情上确已复活，请在正文补一处明确的复活描写，"
                    f"否则后续段落的角色锁定与哨兵都会按「已死」处理。"
                ),
                rule="dead_character_reappearance",
                evidence=tuple(evidence),
            )
        )
    return issues


# ====================================================================
# 规则注册表与入口
# ====================================================================

#: rule 名 → 规则函数。顺序即 issue 的展示顺序（稳定的确定性顺序）。
RULE_REGISTRY: dict[str, Callable[[DiagnosticSnapshot, set[str]], list[DiagnosticIssue]]] = {
    "open_plothole_accumulation": rule_open_plothole_accumulation,
    "low_confidence_facts_overflow": rule_low_confidence_facts_overflow,
    "name_conflict": rule_name_conflict,
    "dead_character_reappearance": rule_dead_character_reappearance,
}

#: 对外暴露的规则顺序（`report.rules_evaluated` 与此一致）。
RULE_ORDER: tuple[str, ...] = tuple(RULE_REGISTRY.keys())


def run_diagnostic_rules(
    db: Any,
    project: Any,
    scope: str = "all",
    *,
    query: str | None = None,
    top_k_chunks: int = 5,
) -> DiagnosticReport:
    """跑全量确定性规则，返回结构化诊断报告。

    **单条规则抛错不影响其余规则**：某条规则依赖的数据缺失（例如哨兵状态表
    还没建）只该让那条规则哑火，不该让整次诊断 500。最后仍返回已拿到的 issue，
    出错的那条规则记进 `tool_results`。

    带 `query` 时额外做一次参考语料检索，把命中分片作为 `kind="chunk"` 证据
    附在报告里 —— `source` 恒为 `novel:{id}`，与生成侧严格隔离。
    """
    snap = DiagnosticSnapshot(db=db, project=project, scope=scope)
    taken: set[str] = set()
    issues: list[DiagnosticIssue] = []

    for name in RULE_ORDER:
        func = RULE_REGISTRY[name]
        try:
            issues.extend(func(snap, taken))
        except PermissionError:
            # 角色锁被绕过：这是安全事件，不是「这条规则的数据缺失」，必须冒泡。
            raise
        except Exception as exc:  # noqa: BLE001 - 单规则失败不拖垮整次诊断
            snap.tool_results[f"rule:{name}"] = {"error": str(exc)}

    chunk_evidence: list[IssueEvidence] = []
    if query and query.strip():
        novel_id, hits = snap.chunk_hits(query, k=top_k_chunks)
        source = _source_label(f"novel:{novel_id}" if novel_id else "novel")
        chunk_evidence = [
            IssueEvidence(
                kind="chunk",
                source=source,
                ref_id=str(hit.get("chunk_id") or ""),
                snippet=_truncate(hit.get("text") or ""),
                score=_clamp_score(hit.get("score")),
            )
            for hit in hits
            if (hit.get("text") or "").strip()
        ]

    return DiagnosticReport(
        issues=issues,
        extra_evidence=chunk_evidence,
        tool_results=dict(snap.tool_results),
        rules_evaluated=list(RULE_ORDER),
    )


def iter_evidence(report: DiagnosticReport) -> Iterable[IssueEvidence]:
    """按出现顺序遍历报告里全部证据（issue 内的在前，检索证据在后）。

    去重由调用方按 `IssueEvidence.dedup_key` 做 —— 这里保持顺序即可。
    """
    for issue in report.issues:
        for ev in issue.evidence:
            yield ev
    for ev in report.extra_evidence:
        yield ev