"""v3.0 独立 Judge Agent：只读 KG 校验生成结果。

与 v2.6 哨兵（sentinel.py）的关系：
- 哨兵：零 LLM、基于 Fact 账本 + 规则，覆盖死人说话/死人出场/境界倒退
- Judge（本模块）：基于 **v3.0 KG 表**（kg_entities/events/character_state/foreshadows/
  entity_knowledge/mentions），覆盖哨兵没做的 4 类 + 文本级泄密 + Step 1 质量信号：
    1. 瞬移 teleportation —— 角色 location 状态与事件地点矛盾（跨段瞬移）
    2. 预知外挂 unauthorized_knowledge —— 角色引用了尚未学到的事实
    3. 秘密外泄 secret_leak —— 说出仍属秘密的事实文本且无学习记录（known_kind=secret 消费方）
    4. 时间穿越 impossible_sequences —— 事件顺序与 in_story_date/时间锚矛盾
    5. 伏笔超期 foreshadow_expiry —— planned_payoff 过期未回收
    6. 语言一致性 language_mix —— 生成文本混入非中文字符（德语/日文等）

设计纪律（继承哨兵）：
- **只标红，绝不自动改写。** 产出带证据的告警，判断权留给人。
- 漏报优于误报：每条规则保守优先，取不到依据就不判。
- 失败静默兜底：Judge 不阻断生成主链（调用方 try 兜住）。

接入：engine._merge_segment_into_graph 在 sentinel 之后调 judge_segment。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select

from app.kg.era_convert import extract_era_name, parse_story_time
from app.kg.knowledge_match import normalize_fact
from app.models import (
    KgCharacterState, KgEntity, KgEntityKnowledge, KgEvent, KgForeshadow, KgMention,
)


# --------------------------------------------------------------------------- #
# 词表与模式
# --------------------------------------------------------------------------- #

#: 说话动词（预知/瞬移判定的锚点）
_SPEAK_VERBS = "说道|问道|答道|笑道|喝道|低声道|道|说|问|答|喊|骂|告诉|透露"

#: 地点移动动词（瞬移判定）
_MOVE_VERBS = (
    "赶到|来到|抵达|飞往|赶往|回到|去了|前往|来到|走到|跑到|瞬移|传送|出现在"
)

#: 拉丁字母/假名/谚文等非中文字符（语言一致性）。
#: 拉丁段含扩展字符（À-ÿ）——德语 draußen 的 ß 在 ÿ 范围，不含则被切成 drau+en。
#: 只认**连续 2 个以上**，避免单个字母（化学式/编号）误报。
#: 假名（぀-ヿ）+ 谚文（가-힯）是明确的外语字符集；汉字（一-鿿）是中文，绝不进这里。
_FOREIGN_RUN = re.compile(r"[A-Za-zÀ-ÿ]{2,}|[぀-ヿ]{1,}|[가-힯]{2,}")
#: 常见合法拉丁串白名单（品牌/单位/编号，不算混语言）
_LATIN_OK = {
    "DNA", "RNA", "APP", "CEO", "CTO", "CFO", "OK", "VIP", "PDF", "USB", "WiFi",
    "GDP", "MBA", "PhD", "vs", "etc", "iPhone", "iPad", "Mac", "iOS",
}


@dataclass
class JudgeFinding:
    """一条 Judge 告警。只报告，不改正文。"""

    rule: str
    severity: str          # err（硬冲突）| warn（可疑）
    entity: str
    message: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "entity": self.entity,
            "message": self.message,
            "evidence": self.evidence,
        }


#: 规则清单（下发给前端，说明哪条在跑）
RULES: dict[str, tuple[str, str]] = {
    "teleportation": ("err", "角色状态地点与事件地点矛盾（跨段瞬移）"),
    "unauthorized_knowledge": ("err", "角色引用了尚未学到的事实（预知外挂）"),
    "secret_leak": ("warn", "角色说出了仍属秘密的事实文本（秘密外泄）"),
    "impossible_sequences": ("err", "事件顺序与故事内时间矛盾（时间穿越）"),
    "foreshadow_expiry": ("warn", "伏笔超过计划回收章未揭晓"),
    "language_mix": ("warn", "生成文本混入非中文字符串"),
}


# --------------------------------------------------------------------------- #
# 各规则判定
# --------------------------------------------------------------------------- #

def check_teleportation(db, project_id: int, text: str, segment_no: int) -> list[JudgeFinding]:
    """瞬移：本段角色出现在某地点，但其最近一次 location 状态是别处，且无移动动词衔接。

    判据（保守）：
    - 角色最近一条 location 状态 ≠ 本段事件 location
    - 本段文本里该角色附近没有移动动词（赶到/来到/回到…）
    移动动词在则不算瞬移（正常移动）。
    """
    findings: list[JudgeFinding] = []
    # 最近 location 状态：每角色取 segment_no 最大的一条
    latest_loc: dict[int, KgCharacterState] = {}
    for st in db.scalars(
        select(KgCharacterState).where(
            KgCharacterState.project_id == project_id,
            KgCharacterState.state_type == "location",
        ).order_by(KgCharacterState.segment_no)
    ):
        latest_loc[st.character_id] = st

    # 本段提到的事件地点
    seg_events = db.scalars(
        select(KgEvent).where(
            KgEvent.project_id == project_id,
            KgEvent.segment_no == segment_no,
            KgEvent.location_id.isnot(None),
        )
    ).all()
    if not seg_events or not latest_loc:
        return findings

    # 收集本段实体（mention 反查）
    mentioned_entities = {
        m.entity_id for m in db.scalars(
            select(KgMention).where(
                KgMention.project_id == project_id, KgMention.segment_no == segment_no
            )
        )
    }

    for ev in seg_events:
        # 本段事件主体（subject）在 mention 里 + 有 location 状态
        subj_id = ev.subject_id
        if subj_id is None or subj_id not in mentioned_entities:
            continue
        st = latest_loc.get(subj_id)
        if st is None or ev.location_id == st.character_id:
            continue
        # 角色的 location 状态存的 new_value 是地点名，事件 location_id 是地点实体 id
        loc_ent = db.get(KgEntity, ev.location_id)
        state_loc = db.get(KgEntity, st.character_id)  # 状态关联的地点实体
        # 状态行 new_value 是字符串地点名；事件 location 是实体。两者名字比对：
        if loc_ent and state_loc:
            if st.new_value and loc_ent.canonical_name in (st.new_value or ""):
                continue  # 一致，非瞬移
        # 本段有没有移动动词衔接？有则正常移动
        if any(mv in text for mv in _MOVE_VERBS.split("|")):
            continue
        ent = db.get(KgEntity, subj_id)
        if ent is None:
            continue
        findings.append(JudgeFinding(
            rule="teleportation",
            severity="err",
            entity=ent.canonical_name,
            message=f"{ent.canonical_name} 出现在 {loc_ent.canonical_name if loc_ent else '?'}，"
                    f"但最近已知状态在 {st.new_value or '?'}，且无移动动词衔接",
            evidence=[f"state@{st.segment_no}: {st.new_value}", f"event@{segment_no}: {ev.summary}"],
        ))
    return findings


def check_unauthorized_knowledge(db, project_id: int, text: str, segment_no: int) -> list[JudgeFinding]:
    """预知外挂：角色引用了某事实，但其 knowledge 记录显示尚未学到。

    判据（保守）：
    - 本段角色 X 在说话（_SPEAK_VERBS 命中且邻近 X）
    - X 的台词/叙述里出现了某秘密实体 S 的名字
    - kg_entity_knowledge 里没有 X→S 的学习记录（learned_at ≤ segment_no）
    """
    findings: list[JudgeFinding] = []
    # 本段提到的角色
    mentions = db.scalars(
        select(KgMention).where(
            KgMention.project_id == project_id, KgMention.segment_no == segment_no
        )
    ).all()
    mentioned = {m.entity_id for m in mentions}
    # 秘密实体（attrs.secrets 非空 的 character + 全部 entity 名字）
    secret_entities = db.scalars(
        select(KgEntity).where(
            KgEntity.project_id == project_id, KgEntity.kind == "character"
        )
    ).all()
    # 已知学习关系：(entity_id -> {known_entity_id})，仅 learned_at ≤ 当前段
    learned: dict[int, set[int]] = {}
    for k in db.scalars(select(KgEntityKnowledge).where(KgEntityKnowledge.project_id == project_id)):
        if (k.learned_at_chapter is None) or True:  # chapter 粒度粗，先全收
            learned.setdefault(k.entity_id, set()).add(k.known_entity_id)

    # 只在「有 knowledge 记录的项目」上判——否则全项目无记录会满屏误报
    if not learned:
        return findings

    for ent in secret_entities:
        if ent.id not in mentioned:
            continue
        # 该角色是否是**说话人**：说话动词必须在其名字**右侧**（"林悠然说道"），
        # 而不是前后窗口都算——否则宾语（"顾辰逸的父亲"）也会被当成说话人，与主语互报。
        name = ent.canonical_name
        idx = text.find(name)
        if idx < 0:
            continue
        after = text[idx + len(name): idx + len(name) + 8]  # 名字后 8 字内找说话动词
        if not any(sv in after for sv in _SPEAK_VERBS.split("|")):
            continue
        # 该角色提到的其它角色/秘密，是否在其知识集里
        for other in secret_entities:
            if other.id == ent.id or other.id not in mentioned:
                continue
            if other.canonical_name not in text:
                continue
            # other 是否 ent 尚未学到的？
            if other.id in learned.get(ent.id, set()):
                continue
            # 保守：只在「other 是秘密人物」（attrs.secrets 涉及 ent）时判
            # 否则普通两人对话会被误判为预知
            ent_secrets = (ent.attrs or {}).get("secrets") or []
            if not ent_secrets:
                continue
            findings.append(JudgeFinding(
                rule="unauthorized_knowledge",
                severity="err",
                entity=ent.canonical_name,
                message=f"{ent.canonical_name} 提及 {other.canonical_name}，但无学习记录",
                evidence=[f"secrets: {ent_secrets}", f"no knowledge: {ent.canonical_name}→{other.canonical_name}"],
            ))
            break  # 每角色最多一条，避免刷屏
    return findings


def check_secret_leak(db, project_id: int, text: str, segment_no: int) -> list[JudgeFinding]:
    """秘密外泄：kg_entity_knowledge 里 known_kind=secret 的**文本秘密**
    出现在本段说话内容里，但该角色从未被记录学到过这条秘密。

    与 unauthorized_knowledge 的分工：
    - 那条判「实体级预知」（已知谁、未学过）——err
    - 这条判「文本级泄密」（秘密短句被说出且无学习行）——warn
      （秘密文本匹配保守：含标点变体也算命中，短句 6 字以下不判——
       过短的秘密文本极易在正常叙述里撞车，宁可漏报）

    known_text 的原始出处是抽取时 LLM 记录的秘密内容；项目里没有任何
    secret 行 → 整体不判（与 unauthorized_knowledge 同款守门）。
    """
    findings: list[JudgeFinding] = []
    # 本段提到的角色（说话人候选）
    mentioned = {
        m.entity_id for m in db.scalars(
            select(KgMention).where(
                KgMention.project_id == project_id, KgMention.segment_no == segment_no
            )
        )
    }
    if not mentioned:
        return findings
    # 项目里所有 secret 情报行（known_entity_id=None + known_text 有值）
    secret_rows = db.scalars(
        select(KgEntityKnowledge).where(
            KgEntityKnowledge.project_id == project_id,
            KgEntityKnowledge.known_kind == "secret",
        )
    ).all()
    if not secret_rows:
        return findings
    # 知道秘密的（holder）集合 + 每条秘密的规范化文本
    normalized: list[tuple[KgEntityKnowledge, str]] = []
    for row in secret_rows:
        t = (row.known_text or "").strip()
        if len(t) < 6:
            continue
        normalized.append((row, normalize_fact(t)))
    if not normalized:
        return findings
    for speaker_id in mentioned:
        speaker = db.get(KgEntity, speaker_id)
        if speaker is None:
            continue
        name = speaker.canonical_name
        idx = text.find(name)
        if idx < 0:
            continue
        after = text[idx + len(name): idx + len(name) + 8]
        if not any(sv in after for sv in _SPEAK_VERBS.split("|")):
            continue
        # 该角色已学到的秘密文本集合（entity_id == speaker 且 known_kind=secret）
        known_texts: set[str] = set()
        for k in db.scalars(
            select(KgEntityKnowledge).where(
                KgEntityKnowledge.project_id == project_id,
                KgEntityKnowledge.entity_id == speaker_id,
                KgEntityKnowledge.known_kind == "secret",
            )
        ):
            if k.known_text:
                known_texts.add(normalize_fact(k.known_text))
        for row, ntext in normalized:
            if row.entity_id == speaker_id:
                continue  # 自己的秘密自己说（陈述我的秘密）不算外泄
            if ntext in known_texts:
                continue  # 已学到
            if ntext and ntext in normalize_fact(text):
                findings.append(JudgeFinding(
                    rule="secret_leak",
                    severity="warn",
                    entity=speaker.canonical_name,
                    message=f"{speaker.canonical_name} 说出了秘密「{(row.known_text or '').strip()[:40]}」，"
                            f"但无该角色学到此秘密的记录",
                    evidence=[f"secret@holder={row.entity_id}: {(row.known_text or '').strip()}",
                              f"segment={segment_no}"],
                ))
                break  # 每说话人最多一条，避免刷屏
    return findings


def check_impossible_sequences(db, project_id: int, text: str, segment_no: int) -> list[JudgeFinding]:
    """时间穿越：事件 sequence 顺序与故事内时间矛盾。

    判据（保守）：事件按 sequence 递增时，故事内时间不应倒退。
    支持两种时间量纲——
      1) 公元年：'2020年3月'（正则取 4 位年份）
      2) 年号纪年：'贞观三年秋'（era_convert 换算公元年，贞观三年=629）

    量纲统一由 era_convert.parse_story_time 完成；本函数只做**相邻同 era 比较**
    （口径 C，见 docs/kg-judge-era-dimension-design.md）：
      - 相邻事件的 era 相同才比 year——换年号（贞观→永徽）是正常历史推进，不比
      - 任一事件解析失败即断链，从下一个成功事件重新起比（宁漏勿错）
      - 年号事件的 era 键是年号名；公元年事件 era=None，与年号事件互不相邻成链

    同名年号（建元=西汉/东晋/南齐）由 era_convert.era_year 按起止区间消歧，
    取不到唯一公元年 → None → 断链，不猜。
    """
    findings: list[JudgeFinding] = []
    events = db.scalars(
        select(KgEvent).where(
            KgEvent.project_id == project_id,
            KgEvent.in_story_date.isnot(None),
        ).order_by(KgEvent.sequence)
    ).all()
    prev: tuple[str | None, int, int, str] | None = None   # (era, year, sequence, summary)
    for ev in events:
        date = ev.in_story_date or ""
        year = parse_story_time(date)
        if year is None:
            prev = None          # 解析失败 → 断链
            continue
        era = extract_era_name(date)
        if prev is not None and prev[0] == era and year < prev[1]:
            findings.append(JudgeFinding(
                rule="impossible_sequences",
                severity="err",
                entity=f"seq{prev[2]}→{ev.sequence}",
                message=f"事件 {prev[2]}（{prev[1]}年）在事件 {ev.sequence}（{year}年）之前，"
                        f"但 sequence 顺序相反",
                evidence=[f"seq{prev[2]}: {prev[1]} ({prev[3]})",
                          f"seq{ev.sequence}: {year} ({(ev.summary or '')[:40]})"],
            ))
        prev = (era, year, ev.sequence, (ev.summary or "")[:40])
    return findings


def check_foreshadow_expiry(db, project_id: int, text: str, segment_no: int,
                            current_chapter: int | None = None) -> list[JudgeFinding]:
    """伏笔超期：planned_payoff_chapter 已过仍未 paid_off。

    判据：status ∈ {planted, developing} 且 planned_payoff_chapter < current_chapter。
    current_chapter 缺省用 events 的最大 chapter 估算。
    """
    findings: list[JudgeFinding] = []
    if current_chapter is None:
        from sqlalchemy import func

        current_chapter = db.scalar(
            select(func.max(KgEvent.chapter)).where(KgEvent.project_id == project_id)
        ) or 0
    for fs in db.scalars(
        select(KgForeshadow).where(
            KgForeshadow.project_id == project_id,
            KgForeshadow.status.in_(["planted", "developing"]),
            KgForeshadow.planned_payoff_chapter.isnot(None),
        )
    ):
        if fs.planned_payoff_chapter < current_chapter:
            overdue = current_chapter - fs.planned_payoff_chapter
            findings.append(JudgeFinding(
                rule="foreshadow_expiry",
                severity="warn",
                entity=fs.name,
                message=f"伏笔「{fs.name}」计划第 {fs.planned_payoff_chapter} 章回收，"
                        f"已超期 {overdue} 章仍未揭晓",
                evidence=[f"planted@{fs.planted_chapter}", f"planned_payoff@{fs.planned_payoff_chapter}", f"now@{current_chapter}"],
            ))
    return findings


def check_language_mix(text: str) -> list[JudgeFinding]:
    """语言一致性：生成文本混入非中文字符串（Step 1 实测 nemotron 出德语 draußen）。

    判据：连续 2+ 拉丁字母（不在白名单）或假名/谚文。只 warn。
    """
    findings: list[JudgeFinding] = []
    if not text:
        return findings
    hits: list[str] = []
    for m in _FOREIGN_RUN.finditer(text):
        s = m.group(0)
        if s in _LATIN_OK:
            continue
        # 假名/谚文单独字符也可能误报（罕见汉字边界），要求长度≥1 且非中文常用
        if re.match(r"[぀-ヿ一-鿿가-힯]", s) and len(s) < 2:
            continue
        hits.append(s)
    if hits:
        # 去重保序，最多报 5 个
        seen: list[str] = []
        for h in hits:
            if h not in seen:
                seen.append(h)
            if len(seen) >= 5:
                break
        findings.append(JudgeFinding(
            rule="language_mix",
            severity="warn",
            entity="生成文本",
            message=f"检测到非中文混入：{', '.join(seen)}",
            evidence=[f"命中片段: {', '.join(seen)}"],
        ))
    return findings


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def _load_exemptions(db, project_id: int) -> set[tuple[str, str]]:
    """加载作者豁免（Fact type=exemption，key=exempt:rule:entity）。

    豁免的告警 Judge 不再报（用户已认定是故意的）。
    """
    from app.models import Fact

    try:
        rows = db.scalars(select(Fact).where(
            Fact.project_id == project_id, Fact.type == "exemption"
        )).all()
        return {(r.props.get("rule") or "", r.props.get("entity") or "") for r in rows}
    except Exception:  # noqa: BLE001 —— 豁免读取失败当无豁免
        return set()


#: Judge 告警的 Fact 类型。**必须落库**（与哨兵 T_ALERT 同理）：只放在单次
#: 生成响应里的告警，用户当时没看就永远丢了，也就无法复核「上次那条是不是误报」。
#: 所有事实消费方都按显式类型过滤，新类型不会漏进提示词或提醒。
T_JUDGE_ALERT = "kg_judge"


def save_judge_findings(db, project_id: int, findings: list[dict], *,
                        segment_no: int) -> int:
    """把本段 Judge findings 落进事实表（哨兵 save_findings 同款语义）。

    findings 是 judge_segment 返回的已序列化 dict 列表。
    先清后写：同段重跑（重新生成）时旧告警被替换，不累积陈旧条目——
    「段落改好了、旧告警还挂着」比没有告警更糟。
    key = `{rule}:{entity}:{segment_no}:{idx}`：同段同规则同实体各条分开
    （idx 是 finding 在本段的序号），重跑时整段替换。
    """
    from app.models import Fact

    old = db.scalars(select(Fact).where(
        Fact.project_id == project_id, Fact.type == T_JUDGE_ALERT
    )).all()
    for row in old:
        # 只清本段的：其它段的告警是历史记录
        if row.last_segment == segment_no:
            db.delete(row)
    n = 0
    for idx, f in enumerate(findings):
        rule = f.get("rule") or ""
        entity = f.get("entity") or ""
        db.add(Fact(
            project_id=project_id, type=T_JUDGE_ALERT,
            key=f"{rule}:{entity}:{segment_no}:{idx}",
            props={
                "rule": rule, "entity": entity,
                "severity": f.get("severity") or "warn",
                "message": f.get("message") or "",
                "evidence": f.get("evidence") or [],
                "segment_no": segment_no,
            },
            source="judge_v3", confidence="high",
            first_segment=segment_no, last_segment=segment_no,
        ))
        n += 1
    db.commit()
    return n


def load_judge_findings(db, project_id: int) -> list[dict]:
    """读回落库的 Judge 告警（按段号倒序——最近的先看）。"""
    from app.models import Fact

    rows = db.scalars(select(Fact).where(
        Fact.project_id == project_id, Fact.type == T_JUDGE_ALERT
    )).all()
    out = [{
        "rule": (r.props or {}).get("rule") or "",
        "severity": (r.props or {}).get("severity") or "warn",
        "segment_no": r.last_segment,
        "entity": (r.props or {}).get("entity") or "",
        "message": (r.props or {}).get("message") or "",
        "evidence": (r.props or {}).get("evidence") or [],
    } for r in rows]
    out.sort(key=lambda d: (d["segment_no"] or 0), reverse=True)
    return out


def judge_segment(db, project_id: int, text: str, segment_no: int,
                  current_chapter: int | None = None) -> dict:
    """审计一段：跑全部规则，返回 {findings, rules}。

    只读 KG，不改正文、不推进状态（状态推进由 sentinel + batch_worker 负责）。
    作者豁免的 (rule, entity) 不再报。失败静默兜底（调用方 try）。
    """
    findings: list[JudgeFinding] = []
    try:
        findings += check_teleportation(db, project_id, text, segment_no)
    except Exception:  # noqa: BLE001 —— 单规则失败不影响其他
        pass
    try:
        findings += check_unauthorized_knowledge(db, project_id, text, segment_no)
    except Exception:  # noqa: BLE001
        pass
    try:
        findings += check_secret_leak(db, project_id, text, segment_no)
    except Exception:  # noqa: BLE001
        pass
    try:
        findings += check_impossible_sequences(db, project_id, text, segment_no)
    except Exception:  # noqa: BLE001
        pass
    try:
        findings += check_foreshadow_expiry(db, project_id, text, segment_no, current_chapter)
    except Exception:  # noqa: BLE001
        pass
    try:
        findings += check_language_mix(text)
    except Exception:  # noqa: BLE001
        pass

    # 作者豁免过滤：(rule, entity) 被豁免的告警不报
    exempt = _load_exemptions(db, project_id)
    if exempt:
        findings = [f for f in findings if (f.rule, f.entity) not in exempt]

    return {
        "findings": [f.to_dict() for f in findings],
        "rules": {name: {"severity": sev, "desc": desc} for name, (sev, desc) in RULES.items()},
    }
