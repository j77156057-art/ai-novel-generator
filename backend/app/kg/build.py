"""知识图谱构建服务：从小说分片抽取并写入 kg_nodes / kg_edges。

图谱按「来源(source)」隔离，支持多张图共存于同一 project：
- source=f"novel:{id}"  → 某本参考语料的独立图谱（用户选多本时各自成图，不混）
- source="generated"    → 用户**生成后**自己小说的图谱
- source="all"          → 旧版「汇总库内全部语料合并一张图」（保留兼容，新流程不再使用）

每张图按 source 幂等重建（只清该 source 的旧图再写新图），互不污染。
"""

import threading
from contextlib import contextmanager

from sqlalchemy import select

from app.kg.extractor import _RESOLVE_CUES, RuleBasedExtractor, resolve_plotholes
from app.models import Fact, Generation, KgEdge, KgNode, NovelChunk, Project, RawNovel

# 坑节点 props 里占位 cue 的值：extractor 对「直问句但无线索词」的坑写 cue="?"。
# 拿它当检测键等于匹配所有文本，必须在结算检测里跳过。
_PLACEHOLDER_CUE = "?"

# ---------------------------------------------------------------------------
# per-project KG 写锁
#
# 为什么需要：重生成路径的「清空 + 全量 replay」与并发的 `merge_segment_into_kg`
# 会在同一批 KgNode/KgEdge/Fact 上交错——一方清空、另一方写回，结果互相覆盖，
# 留下幽灵数据或缺段。此前全仓无 KG 锁（仅 `app/llm/registry.py` 有 LLM 锁可作先例）。
#
# 为什么是 RLock：replay 全程持锁，而它内部要**反复调用** `merge_segment_into_kg`，
# 后者也持同一把锁——同线程必须可重入，否则 replay 一进 merge 就自死锁。
# 不同线程则天然串行：两种合法交错都正确（replay 先完成 → 新段 merge 增量叠加其上；
# 或新段 merge 先完成 → replay 读取的段列表已含它）。
#
# dict 本身由一个全局锁守护，避免并发首次访问时创建出两把不同的锁。
# ---------------------------------------------------------------------------
_KG_LOCKS_GUARD = threading.Lock()
_KG_LOCKS: dict[int, threading.RLock] = {}


def _kg_lock_for(project_id: int) -> threading.RLock:
    """取该项目的 KG 锁（不存在则创建，创建过程受全局锁保护）。"""
    with _KG_LOCKS_GUARD:
        lock = _KG_LOCKS.get(project_id)
        if lock is None:
            lock = threading.RLock()
            _KG_LOCKS[project_id] = lock
        return lock


@contextmanager
def _project_kg_lock(project_id: int):
    """持该项目 KG 锁的临界区（RLock，同线程可重入）。"""
    with _kg_lock_for(project_id):
        yield


def _clear_graph_by_source(db, project_id: int, source: str) -> None:
    """只清该 source 下的旧图（节点 + 相关边），不影响其它来源的图。"""
    old = db.scalars(
        select(KgNode).where(KgNode.project_id == project_id, KgNode.source == source)
    ).all()
    ids = [n.id for n in old]
    if ids:
        db.query(KgEdge).filter(
            (KgEdge.from_id.in_(ids)) | (KgEdge.to_id.in_(ids))
        ).delete()
        db.query(KgNode).filter(KgNode.id.in_(ids)).delete()
        db.flush()


def _extract_from_chunks(chunks: list) -> object:
    result = RuleBasedExtractor().extract(chunks)
    resolve_plotholes(result, chunks)
    return result


def _write_result(db, project_id: int, result, source: str) -> dict:
    name_to_node: dict[str, int] = {}
    n_char = n_hole = n_rel = 0

    for name, c in result.characters.items():
        node = KgNode(
            project_id=project_id,
            type="character",
            props={"name": name, "mentions": c.mentions, "speaking": c.speaking},
            first_chapter=min(c.chapters) if c.chapters else None,
            last_chapter=max(c.chapters) if c.chapters else None,
            source=source,
        )
        db.add(node)
        db.flush()
        name_to_node[name] = node.id
        n_char += 1

    for ph in result.plotholes.values():
        node = KgNode(
            project_id=project_id,
            type="plothole",
            # key 一并落库：增量路径要靠它做「本段是否结算了旧坑」的检测键。
            # 历史节点没有 key，检测时回落到 props["cue"]（两者语义同值，见
            # _hole_detection_key 的说明）。
            props={
                "key": ph.key,
                "cue": ph.cue,
                "question": ph.question,
                "status": ph.status,
            },
            first_chapter=ph.first_chapter,
            last_chapter=ph.last_chapter,
            source=source,
        )
        db.add(node)
        n_hole += 1

    for (a, b), edge in result.relationships.items():
        aid, bid = name_to_node.get(a), name_to_node.get(b)
        if aid is None or bid is None:
            continue
        db.add(
            KgEdge(
                from_id=aid,
                to_id=bid,
                type="relationship",
                props={"relation": edge.relation, "chapters": sorted(edge.chapters)},
            )
        )
        n_rel += 1

    db.commit()
    return {
        "project_id": project_id,
        "source": source,
        "characters": n_char,
        "plotholes": n_hole,
        "relationships": n_rel,
    }


def build_graph(
    db,
    project_id: int,
    novel_id: int | None = None,
    all_corpora: bool = False,
) -> dict:
    """构建参考语料图谱。

    - all_corpora=True：汇总库内全部语料合并为 source="all" 一张图（旧版）。
    - novel_id 给定：只建该本语料的图，source=f"novel:{novel_id}"（新流程：多本各自成图）。
    - 都不给：取首本（旧版默认）。
    """
    if all_corpora:
        chunks = db.scalars(select(NovelChunk).order_by(NovelChunk.novel_id, NovelChunk.id)).all()
        if not chunks:
            return {"error": "库内无分片，请先导入语料"}
        source = "all"
    elif novel_id is not None:
        chunks = db.scalars(
            select(NovelChunk).where(NovelChunk.novel_id == novel_id)
        ).all()
        if not chunks:
            return {"error": f"novel {novel_id} 无分片"}
        source = f"novel:{novel_id}"
    else:
        novel = db.scalars(select(RawNovel).order_by(RawNovel.id)).first()
        if novel is None:
            return {"error": "无可用 novel，请先上传/种子语料"}
        novel_id = novel.id
        chunks = db.scalars(
            select(NovelChunk).where(NovelChunk.novel_id == novel_id)
        ).all()
        source = f"novel:{novel_id}"

    result = _extract_from_chunks(chunks)
    _clear_graph_by_source(db, project_id, source)
    stats = _write_result(db, project_id, result, source)
    stats["novel_id"] = novel_id
    stats["chunks"] = len(chunks)
    return stats


class _TextChunk:
    """把一段纯文本包装成抽取器可消费的 chunk 形状。"""

    __slots__ = ("text", "chapter_no", "idx", "novel_id")

    def __init__(self, text: str, chapter_no: int | None, idx: int, novel_id=None) -> None:
        self.text = text
        self.chapter_no = chapter_no
        self.idx = idx
        self.novel_id = novel_id


def _split_text_to_chunks(text: str, chunk_chars: int = 1500) -> list[_TextChunk]:
    """把生成文本按长度切成带伪章节号的 chunk，供规则抽取。"""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[_TextChunk] = []
    buf = ""
    idx = 0
    chapter = 1
    for p in paragraphs:
        if buf and len(buf) + len(p) > chunk_chars:
            chunks.append(_TextChunk(buf, chapter, idx))
            idx += 1
            buf = ""
            chapter += 1
        buf = (buf + "\n" + p) if buf else p
    if buf:
        chunks.append(_TextChunk(buf, chapter, idx))
    return chunks


def build_graph_from_text(
    db, project_id: int, text: str, source: str = "generated"
) -> dict:
    """从**生成的小说文本**建图谱（生成后才有的知识图谱）。

    ⚠️ 2026-10-17 起**不推荐用于「全量重建」**：本函数只做**图谱一步**，且
    `_split_text_to_chunks` 落的 chapter 是 **chunk 序号而非真实段号**。任何
    「按已生成各段重建 generated 侧」的需求都应走 `rebuild_generated_kg`——它
    清空 + 按真实段号逐段 replay，图谱/账本/别名/哨兵四步同源、段号正确。
    保留本函数仅为兼容性/历史调用。

    注意：这是「手动全量重建」（先清该 source 再重写）。**每段生成后的自动更新**应调用
    merge_graph_from_text()——它做增量 upsert，不清空已有事实。
    """
    if not text or not text.strip():
        return {"error": "无生成文本可建图"}
    chunks = _split_text_to_chunks(text)
    result = _extract_from_chunks(chunks)
    _clear_graph_by_source(db, project_id, source)
    stats = _write_result(db, project_id, result, source)
    stats["chunks"] = len(chunks)
    return stats


def extract_from_text(text: str, segment_no: int = 1):
    """把「一段文本」切成 chunk 并跑规则抽取，返回 (ExtractionResult, chunks)。

    抽出来做公共入口，是为了让**图谱写入与账本写入共用同一次抽取**：
    两者吃的是同一份 ExtractionResult，跑两遍纯属浪费（真实语料单次抽取约 16s，
    虽然单段文本很小，但这是不该存在的重复计算，也会让两条链路的结果漂移）。
    """
    chunks = _split_text_to_chunks(text)
    for c in chunks:
        c.chapter_no = segment_no
    return _extract_from_chunks(chunks), chunks


def merge_segment_into_kg(
    db, project_id: int, text: str, segment_no: int, source: str = "generated"
) -> dict:
    """把「新生成的一段」**完整**并入 KG：图谱 + 事实账本 + 别名归一 + 规则哨兵。

    ## 为什么必须有这个函数（2026-10-17 live 验证复现的洞）

    此前段级合并的逻辑分散：生成链路（engine._merge_segment_into_graph）做四步，
    而本模块的公开入口 merge_graph_from_text 只做**图谱一步**——Fact 账本不
    随之更新。任何调用方用 merge_graph_from_text，账本就与图谱状态漂移
    （坑在图谱里 resolved 了、账本里还 open，reminders/诊断两边不一致）。

    现在四步编排收归本函数，merge_graph_from_text 与生成链路都走它——
    **只有一个段级合并入口，不存在"只更一半"的路径**。

    四步共用**同一次抽取**（图谱与账本吃同一份 ExtractionResult，跑两遍纯属
    浪费，也会让两条链路的结果漂移）：
    1. 图谱（KgNode/KgEdge，含本段结算旧坑）——下一次生成的 reminders 能
       拿到本段新角色/新伏笔；
    2. 事实账本（Fact）+ 别名归一——把「宁折 / 宁折枝」这类同一人的不同写法
       并到规范名，否则 get_reminders 会把同一人当成两个锁定角色注入提示词；
    3. 图谱侧本段结算掉的坑，账本侧在同一次 merge 里同步 resolved；
    4. 规则哨兵（零 LLM 硬冲突检查，只标红不改正文）——放在账本之后，
       名字表与已认定状态都来自账本，账本先更新哨兵判定才准。

    任何一步失败都绝不阻断主流程：吞异常、写 error 键、回传状态。

    返回形状刻意**保持扁平兼容**：图谱统计（merged/new_characters/…）仍在顶层，
    账本/别名/哨兵挂在 `ledger` / `aliases` / `sentinel` 三个键下——
    `kg_update` 的既有消费方（前端与 API）读顶层字段，不受影响。

    2026-10-17：整个变更段持 **per-project RLock**（见模块头注释）。重生成路径的
    全量 replay 也持同一把锁，两者互相串行；同线程由 RLock 保证可重入。
    """
    with _project_kg_lock(project_id):
        return _merge_segment_into_kg_locked(
            db, project_id, text, segment_no, source=source
        )


def _merge_segment_into_kg_locked(
    db, project_id: int, text: str, segment_no: int, source: str = "generated"
) -> dict:
    """`merge_segment_into_kg` 的实际实现（调用方已持 per-project 锁）。

    与公开入口分体只为「持锁」这一件事：锁必须在整个变更段内持有。逻辑本体重命名
    而非重缩进，是为了不夹带任何行为改动。
    """
    from app.kg.ledger import (
        T_PLOTHOLE,
        facts_from_extraction,
        resolve_aliases,
        upsert_facts,
    )

    # 1) 图谱（含本段结算旧坑：text 一并传入，ExtractionResult 不带原文）
    try:
        result, _ = extract_from_text(text, segment_no)
        stats = dict(
            merge_result_into_graph(
                db, project_id, result, source=source, segment_no=segment_no,
                text=text,
            )
        )
    except Exception as exc:  # noqa: BLE001 —— 图谱失败不阻断主流程
        return {"merged": False, "reason": f"图谱合并失败：{str(exc)[:100]}"}

    # 2) 事实账本 + 3) 结算同步
    try:
        ledger_facts = facts_from_extraction(result, segment_no)
        # 图谱侧本段结算掉的坑，账本侧必须在**同一次 merge**里同步成
        # resolved——只改 KgNode 不改 Fact 会让 reminders/诊断两边状态漂移。
        # patch 刻意只带 status（不带 planted_segment 等 int 字段：
        # upsert 的累加语义会把首现段污染成 1+2=3）。
        for h in stats.get("resolved_holes") or []:
            fact_key = (h.get("question") or "").strip() or (h.get("key") or "").strip()
            if not fact_key:
                continue
            ledger_facts.append(
                {
                    "type": T_PLOTHOLE,
                    "key": fact_key,
                    "props": {"status": "resolved"},
                    "first_segment": h.get("first_chapter") or segment_no,
                    "last_segment": segment_no,
                    "confidence": "high",
                }
            )
        stats["ledger"] = upsert_facts(
            db, project_id, ledger_facts, source=source,
        )
        # 别名归一紧随其后：本段若引入了简称/全称两种写法，下一段就要用规范名
        aliases = resolve_aliases(db, project_id, source=source)
        stats["aliases"] = {
            "auto_merged": len(aliases["auto"]),
            "nodes_removed": aliases["nodes_removed"],
            "review_count": aliases["review_count"],
            "review": aliases["review"],
        }
    except Exception as exc:  # noqa: BLE001 —— 账本失败不阻断主流程
        stats["ledger"] = {"error": str(exc)[:100]}

    # 4) 规则哨兵：零 LLM 的硬冲突检查。**只标红，不改正文。**
    try:
        from app.kg.sentinel import audit_segment

        proj = db.get(Project, project_id)
        stats["sentinel"] = audit_segment(db, proj, text, segment_no)
    except Exception as exc:  # noqa: BLE001 —— 哨兵失败不阻断主流程
        stats["sentinel"] = {"error": str(exc)[:100], "findings": []}

    return stats


def merge_graph_from_text(
    db, project_id: int, text: str, source: str = "generated", segment_no: int = 1
) -> dict:
    """增量合并：把「新生成的一段」抽取的事实并入已有图谱，**不清空、不重建**。

    这是图谱进入生成闭环的关键：旧实现中 build_graph_from_text 只在用户手动点「建图」
    时调用，且每次清空重建 —— 生成过程里图谱既不增量更新、也不被下一次生成可靠使用。

    2026-10-17 起实现改为走 `merge_segment_into_kg`（四步完整合并）：
    此前本函数只做图谱一步、Fact 账本不同步，调用方拿到的是"半份簿记"
    （live 验证实测复现：KgNode resolved 而 Fact 仍 open）。返回形状在原
    图谱统计之上新增 `ledger`/`aliases`/`sentinel` 三个键，顶层字段不变。
    """
    if not text or not text.strip():
        return {"merged": False, "reason": "空文本"}
    return merge_segment_into_kg(db, project_id, text, segment_no, source=source)


def rebuild_generated_kg(
    db, project_id: int, source: str = "generated"
) -> dict:
    """重生成 / 手动建图专用：清空 generated 侧全部派生数据，按段号顺序 replay。

    ## 为什么是「清空 + 全量 replay」而非增量

    重生成第 M 段后，该段旧正文抽出的事实会永久残留（角色 mentions/speaking 是
    **累加值**、坑 status 与哨兵 state 是**覆盖式**、别无「段 M 贡献了多少」的
    分段明细），无法做减法。唯一正确的做法是：清空 generated 侧全部派生数据，
    再按段号顺序把每段重新 merge 一遍——段 M 旧版本的贡献在新一轮 replay 中被
    源文本天然取代。详见 docs/architecture/2026-10-17-kg-regenerate-replay.md §3。

    ## 清空范围

    只清 `source="generated"`：图谱（`_clear_graph_by_source`）+ 事实账本
    （`Fact`，含 `state`/`alert` 两类哨兵事实）。`novel:{id}` 参考语料图与
    user/bible 档事实一律不动。generated 侧全部事实均为**机器派生、确定性可重算**
    （别名归一只应用 auto 档纯函数，全仓无手动应用 review 档合并的端点），
    故清空 + replay 不丢任何用户输入。

    ## 顺序与并发

    运行全程持 **per-project RLock**（与 `merge_segment_into_kg` 同一把），
    读 generations → 清图谱 → 清账本 → 逐段 replay 都在临界区内，二者互相串行。
    清空先图谱后账本、同一事务内提交，避免中间态被其它请求读到。

    ## 失败语义

    单段 `merge_segment_into_kg` 回传 `merged is False` → 记入 `failed` 但**不中止**
    （沿用既有「簿记失败绝不阻断主流程」纪律，见 §6）。`merge_segment_into_kg`
    内部已吞异常，故生产环境不会因单段异常中止；只有当其**自身抛出**（例如被
    测试注入故障）时才会向上传播，由调用方（regenerate 的 try/except）兜住。

    Returns
    -------
    dict
        `{"rebuilt": bool, "segments": int, "replayed": int, "failed": list, "source": str}`。
    """
    with _project_kg_lock(project_id):
        gens = db.scalars(
            select(Generation)
            .where(Generation.project_id == project_id)
            .order_by(Generation.segment_no)
        ).all()
        # 清空 generated 侧全部派生数据：先图谱、再账本，同一事务提交。
        _clear_graph_by_source(db, project_id, source)
        db.query(Fact).filter(
            Fact.project_id == project_id, Fact.source == source
        ).delete()
        db.commit()

        replayed, failed = 0, []
        for g in gens:
            if not (g.text or "").strip():
                continue
            st = merge_segment_into_kg(
                db, project_id, g.text, g.segment_no, source=source
            )
            replayed += 1
            if st.get("merged") is False:
                failed.append(
                    {"segment_no": g.segment_no, "reason": st.get("reason")}
                )
        return {
            "rebuilt": True,
            "segments": len(gens),
            "replayed": replayed,
            "failed": failed,
            "source": source,
        }


def _hole_detection_key(props: dict) -> str:
    """取坑节点的「结算检测键」；取不到可用值返回空串（调用方跳过该坑）。

    为什么是 key / cue 而不是 question：
    - extractor 里 `key = cue or "疑问"`，即 key 的语义就是线索词；cue 在
      props 里**本来就已持久化**（build 一直写 cue/question/status 三件套），
      所以历史节点即使没有 key，用 cue 也能拿到同值的检测键；
    - question 是整句悬念原句，模型不会在后续段落里逐字复述它，拿 question
      做子串匹配几乎永远不命中，回落它等于「历史坑永远不能结算」，
      正是本次要修的缺陷。

    占位 cue "?"（直问句但无线索词）必须跳过：它匹配所有文本。
    """
    det = (props.get("key") or props.get("cue") or "").strip()
    if not det or det == _PLACEHOLDER_CUE:
        return ""
    return det


def merge_result_into_graph(
    db, project_id: int, result, source: str = "generated", segment_no: int = 1,
    text: str = "",
) -> dict:
    """把已抽取好的结果写进图谱（增量 upsert，不清空）。

    upsert 语义：
    - 角色：同名已存在 → 累加 mentions/speaking、扩展章节范围；不存在 → 新增
    - 坑：同一 question 已存在 → 不重复登记，但**要参与本段结算检测**（见下）；
      不存在 → 新增
    - 关系：同 (from,to,relation) 已存在 → 跳过；否则新增
    章节号统一取 segment_no（一段 = 一章），保证 last_chapter 单调递增不回退。

    本段结算（缺陷 1 修复）：增量路径里 extract_from_text 把所有 chunk 的
    chapter_no 设成同一个 segment_no，extractor.resolve_plotholes 的
    「末现章之后」条件永不成立，坑一旦建出来就永远是 open，已解决的悬念会
    持续注入每一代的提示词（get_reminders 的 unresolved_plotholes 只增不减），
    diagnosis 的 open_plothole_accumulation 也长期误报。这里补上增量路径的结算：
    本段 text 同时含该坑的检测键与任一强解决短语（_RESOLVE_CUES）→ resolved。
    检测键语义与 resolve_plotholes 完全一致（key + 强解决短语），不另立标准。
    """
    existing = db.scalars(
        select(KgNode).where(KgNode.project_id == project_id, KgNode.source == source)
    ).all()
    char_by_name: dict[str, KgNode] = {}
    hole_by_q: dict[str, KgNode] = {}
    for n in existing:
        props = n.props or {}
        if n.type == "character" and props.get("name"):
            char_by_name[props["name"]] = n
        elif n.type == "plothole" and props.get("question"):
            hole_by_q[props["question"]] = n

    n_new_char = n_upd_char = n_new_hole = n_new_rel = n_resolved_hole = 0
    name_to_node: dict[str, int] = {}

    for name, c in result.characters.items():
        node = char_by_name.get(name)
        if node is None:
            node = KgNode(
                project_id=project_id,
                type="character",
                props={"name": name, "mentions": c.mentions, "speaking": c.speaking},
                first_chapter=segment_no,
                last_chapter=segment_no,
                source=source,
            )
            db.add(node)
            db.flush()
            n_new_char += 1
        else:
            props = dict(node.props or {})
            props["mentions"] = (props.get("mentions") or 0) + c.mentions
            props["speaking"] = (props.get("speaking") or 0) + c.speaking
            node.props = props
            node.first_chapter = min(
                [x for x in (node.first_chapter, segment_no) if x is not None] or [segment_no]
            )
            node.last_chapter = max(
                [x for x in (node.last_chapter, segment_no) if x is not None] or [segment_no]
            )
            n_upd_char += 1
        name_to_node[name] = node.id

    for ph in result.plotholes.values():
        if ph.question in hole_by_q:
            continue
        db.add(
            KgNode(
                project_id=project_id,
                type="plothole",
                # key 与 cue/question/status 一起落库：后续段的结算检测要用 key
                # （见 _hole_detection_key；没有 key 时回落 cue）。
                props={
                    "key": ph.key,
                    "cue": ph.cue,
                    "question": ph.question,
                    "status": ph.status,
                },
                first_chapter=segment_no,
                last_chapter=segment_no,
                source=source,
            )
        )
        n_new_hole += 1

    # —— 缺陷 1：用本段文本结算已有的 open 坑 ——
    # 只处理「本 source 下、当前仍为 open」的坑；本段新开的坑不在此列
    # （同章自解析不算解决，与 resolve_plotholes 的「末现章之后」语义一致）。
    # resolved_holes 回传给调用方，让账本（Fact）侧的 status 在同一次 merge 里
    # 同步更新——只改 KgNode 不改 Fact 会让两边状态漂移（派单明确要求协同）。
    resolved_holes: list[dict] = []
    if text:
        for n in existing:
            if n.type != "plothole":
                continue
            props = n.props or {}
            if props.get("status") != "open":
                continue
            det = _hole_detection_key(props)
            if not det:
                continue
            if det in text and any(rc in text for rc in _RESOLVE_CUES):
                new_props = dict(props)
                new_props["status"] = "resolved"
                n.props = new_props
                n.last_chapter = max(
                    [x for x in (n.last_chapter, segment_no) if x is not None] or [segment_no]
                )
                resolved_holes.append(
                    {
                        "key": new_props.get("key") or new_props.get("cue") or "",
                        "question": new_props.get("question") or "",
                        "cue": new_props.get("cue") or "",
                        "first_chapter": n.first_chapter,
                        "last_chapter": n.last_chapter,
                    }
                )
                n_resolved_hole += 1

    # 关系去重：同 (from,to,relation) 已存在则跳过
    node_ids = [n.id for n in existing] + list(name_to_node.values())
    existing_edges = set()
    if node_ids:
        rows = db.query(KgEdge).filter(
            (KgEdge.from_id.in_(node_ids)) | (KgEdge.to_id.in_(node_ids))
        ).all()
        for e in rows:
            existing_edges.add((e.from_id, e.to_id, (e.props or {}).get("relation")))

    for (a, b), edge in result.relationships.items():
        aid, bid = name_to_node.get(a), name_to_node.get(b)
        if aid is None or bid is None:
            continue
        if (aid, bid, edge.relation) in existing_edges:
            continue
        db.add(
            KgEdge(
                from_id=aid,
                to_id=bid,
                type="relationship",
                props={"relation": edge.relation, "chapters": sorted(edge.chapters)},
            )
        )
        n_new_rel += 1

    db.commit()
    return {
        "merged": True,
        "project_id": project_id,
        "source": source,
        "segment_no": segment_no,
        "new_characters": n_new_char,
        "updated_characters": n_upd_char,
        "new_plotholes": n_new_hole,
        "new_relationships": n_new_rel,
        # 本段结算掉的旧坑：计数 + 明细。明细回传给调用方（engine）同步 Fact 状态；
        # 计数供前端/API 观测。纯新增键，既有消费方读顶层计数字段不受影响。
        "resolved_plotholes": n_resolved_hole,
        "resolved_holes": resolved_holes,
    }
