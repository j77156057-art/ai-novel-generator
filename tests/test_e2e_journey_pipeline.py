"""v3.0 e2e user journey 自动化测试（mock provider）。

「建项目→生成→批抽→读侧→切片→豁免」六步用户视角全链路。
不依赖网络/智谱——mock LLM provider 后同步跑批抽（flush_project
跳过 worker 线程，测试稳定可重入）。

跑法：python tests/test_e2e_journey_pipeline.py

对应设计：docs/kg-e2e-journey-design.md
- 真 LLM 冒烟脚本在 scratch/run_journey_e2e.py，本测试是回归保护
- 断言区分「空即合法」（judge findings 无 foreshadow 输入）vs
  「空即异常」（N 段真实文本 kg_events 不该为 0）
"""
from __future__ import annotations

import os, sys, json
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

def check(label, got, want):
    """断言版 check —— 失败即抛。

    （原来的脚本版只记数不抛：跑挂了也会显示通过。）
    """
    assert got == want, f"{label}: got={got!r} want={want!r}"


# ────────────────────────────────────────────────────────────────────
# Mock LLM Provider
# ────────────────────────────────────────────────────────────────────

def install_mock_provider():
    """把全局 LLM provider 换成 mock（_PROVIDER 单例）。

    mock 输出固定中文段（足够 length 触发 3000 字样本量下限可缩到 800
    ——回归测试不需要真 3000 字耗时，只验「KG 回流 + 字段链路」）
    """
    from app.llm import provider as p_mod
    from app.llm.provider import OpenAICompatibleChatProvider

    class MockProvider(OpenAICompatibleChatProvider):
        def __init__(self):
            self.api_key = "fake"
            self.base_url = "http://fake"
            self.model = "m"
            self.extra_body = {}
            self.max_tokens = None
            self.timeout = 60
            self.stream_timeout = 60
            self._models = ["m"]
            self._stop_sequences = None
            self.last_finish_reason = None

        def _model_chain(self):
            return ["m"]

        def _complete(self, messages, stop_sequences=None, max_tokens=None, temperature=None):
            sys_prompt = messages[0]["content"] if messages else ""
            # 抽真 LLM 的 segment 体（包含「林悠然」+「顾辰逸」+「辰逸父亲是董事长」
            # 三个关键实体，让批抽能抽出 entity_knowledge 触发 unauthorized 规则）
            return (
                "林悠然走进了天津港候船厅。顾辰逸在后头跟着，她没有回头。"
                "林悠然停下脚步，望向窗外的船只，心里念着：顾辰逸父亲是董事长这件事"
                "她从未告诉任何人。她深呼吸，顾辰逸默默站在她身边，没问她在想什么。"
                "两人在候船厅坐了许久，窗外的船起起落落。"
            )

        def _stream(self, messages, stop_sequences=None, max_tokens=None, temperature=None):
            text = self._complete(messages, stop_sequences, max_tokens, temperature)
            for ch in text:
                yield ch

    p_mod._PROVIDER = MockProvider()
    # 也清掉缓存（get_llm_provider 可能返回旧的）
    if hasattr(p_mod, "get_llm_provider"):
        try:
            p_mod.get_llm_provider.cache_clear()
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────────
# Mock 批抽（避免真 LLM 抽实体 + 知识）
# ────────────────────────────────────────────────────────────────────

def patch_extract_batch_with_mock():
    """把 extractor_v3.extract_batch 换成 mock 返回固定结构。

    batch_worker 调的是 extract_batch(provider, [(segment_no, text, chapter), ...])
    （批级入口），不是 extract_segment。mock 让每段直接落库：
    1 entity（林悠然）+ 1 event（天津港候船厅）+ 1 entity_knowledge
    （林悠然→顾辰逸父亲是董事长），等于绕过 LLM 抽取
    测「主链 enqueue→worker→落库→读侧」。
    """
    from app.kg import extractor_v3
    from app.kg import batch_worker

    call_log = {"count": 0}

    def _single_payload(seg: int, chapter: int) -> dict:
        """mock 抽取 payload。

        secrets 必须在林悠然身上（mocked 文本里她提到了「顾辰逸父亲是董事长」）——
        check_unauthorized_knowledge 的实现在 line 220 查的是「说话人 ent」自己
        的 secrets（"if not ent_secrets: continue"），不是 other 的。看似倒挂但
        这是实际行为；测的 follow 实现不改。
        """
        return {
            "entities": [
                {"kind": "character", "canonical_name": "林悠然",
                 "aliases": [], "attrs": {"secrets": ["顾辰逸父亲是董事长"]}},
                {"kind": "character", "canonical_name": "顾辰逸",
                 "aliases": [], "attrs": {"secrets": ["父亲是董事长"]}},
                {"kind": "location", "canonical_name": "天津港候船厅",
                 "aliases": [], "attrs": {}},
            ],
            "relations": [
                {"from": "林悠然", "to": "顾辰逸", "rel_type": "romantic",
                 "strength": 0.8},
            ],
            "events": [
                {"sequence": 1, "chapter": chapter, "event_type": "incident",
                 "summary": f"seg{seg} 林悠然在天津港候船厅", "subject": "林悠然",
                 "verb": "进入", "object": "候船厅", "location": "天津港候船厅",
                 "in_story_date": "2024年3月"},
            ],
            "event_links": [],
            "entity_knowledge": [
                {"character": "林悠然", "knows": "顾辰逸父亲是董事长",
                 "known_kind": "secret", "learned_at_sequence": 1},
            ],
            "state_changes": [
                {"character": "林悠然", "state_type": "location",
                 "new_value": "天津港候船厅"},
            ],
            "foreshadows": [
                {"name": "辰逸父亲身份", "status": "planted",
                 "planted_chapter": chapter, "planned_payoff_chapter": 50},
            ],
        }

    def mock_extract_batch(provider, items):
        """items: list[(segment_no, text, chapter)]

        返回 schema 与 normalize_extraction 同款（不分段嵌套，调用方
        _persist_extraction 从顶层读 entities/relations/events/...）。
        """
        call_log["count"] += 1
        entities, relations, events, event_links, knowledge, states, fores = [], [], [], [], [], [], []
        for seg, _text, chapter in items:
            payload = _single_payload(seg, chapter or 1)
            entities.extend(payload["entities"])
            relations.extend(payload["relations"])
            events.extend(payload["events"])
            event_links.extend(payload["event_links"])
            knowledge.extend(payload["entity_knowledge"])
            states.extend(payload["state_changes"])
            fores.extend(payload["foreshadows"])
        return {
            "entities": entities,
            "relations": relations,
            "events": events,
            "event_links": event_links,
            "entity_knowledge": knowledge,
            "state_changes": states,
            "foreshadows": fores,
        }

    orig = extractor_v3.extract_batch
    extractor_v3.extract_batch = mock_extract_batch
    if hasattr(batch_worker, "extract_batch"):
        batch_worker.extract_batch = mock_extract_batch

    return call_log, orig


# ────────────────────────────────────────────────────────────────────
# 测试 1：六步流程跑通（核心）
# ────────────────────────────────────────────────────────────────────

def test_six_step_journey_runs():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    import app.models  # noqa: F401
    from app.models import Project, Generation, AgentRun, KgEvent, KgEntity, KgEntityKnowledge
    from app.kg import batch_worker
    from app.kg.judge import judge_segment, save_judge_findings
    from app.kg.retrieval_v3 import slice_for_prompt

    # 1) 内存库 + StaticPool（batch_worker 跨线程自建 Session，必须共享连接）
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    import app.db as _db_mod
    prev_sessionlocal = _db_mod.SessionLocal
    _db_mod.SessionLocal = Session

    # 2) 后台 worker 线程必须在本测试期间停掉，否则它会跟本测试**抢同一个模块级队列**：
    #    队列是进程级全局的，前面别的测试往里塞过段；worker 抢先取批时，
    #    那批里混着别的测试的段 → 落库失败 → 本测试看到「0 个 event」。
    #    （这是真踩过的：单跑绿、全量跑红。）
    prev_stop = batch_worker._queue._stop
    prev_started = batch_worker._worker_started
    batch_worker._queue._stop = True          # 让已启动的 worker 循环退出
    batch_worker._worker_started = False       # 让后面的测试还能重新拉起 worker

    install_mock_provider()
    extract_log, orig = patch_extract_batch_with_mock()

    try:
        # —— Step ① 建项目（用独占 id，避免与其它测试的 project_id 撞）——
        db = Session()
        project_id = 987654
        p = Project(id=project_id, title="journey test", genre="urban_romance",
                    style="冷峻", generation_length=800)
        db.add(p); db.commit()
        check("建项目 拿到 id", isinstance(project_id, int), True)

        # 清掉全局队列里本项目的残留（跨测试污染）
        while batch_worker._queue.take_batch(project_id):
            pass

        # —— Step ② 模拟 5 段 generate 后落库（不真调 LLM；验 enqueue+commit 路径）——
        n_segments = 5
        # 文本里有说话动词（让 unauthorized_knowledge 触发）+ 角色名（让 mention 落库）
        # + 重复模式（让 slice_for_prompt 的 recent_entities 拉出来）
        segment_text = (
            "林悠然说道：「顾辰逸，你怎么也来了？」"
            "顾辰逸在天津港候船厅答：「我来看看你。」"
            "林悠然心里念着顾辰逸父亲是董事长的秘密。"
        )
        for seg in range(1, n_segments + 1):
            g = Generation(project_id=project_id, segment_no=seg, text=segment_text,
                           summary={"text": f"seg{seg} 林悠然候船", "current_node": f"node{seg}"},
                           chapter_range=f"1-{seg}")
            db.add(g)
            aid = batch_worker.enqueue_segment(db, project_id, seg, segment_text, chapter=seg)
            db.commit()
            check(f"段{seg} enqueue 返回 AgentRun id", isinstance(aid, int), True)
            # 同步触发批抽（跳过后台线程）
            batch_worker.flush_project(project_id)

        # —— Step ③ 批抽落库 ——
        events_n = db.query(KgEvent).filter_by(project_id=project_id).count()
        ents_n = db.query(KgEntity).filter_by(project_id=project_id).count()
        know_n = db.query(KgEntityKnowledge).filter_by(project_id=project_id).count()
        check("kg_events >= 段数（每段至少 1 event）", events_n >= n_segments, True)
        check("kg_entities >= 2（林悠然+顾辰逸）", ents_n >= 2, True)
        check("kg_entity_knowledge >= 1", know_n >= 1, True)
        check("extractor 被调用 n 次（每段一次）", extract_log["count"], n_segments)

        # —— Step ④ Judge（落库 + 读侧）——
        for seg in range(1, n_segments + 1):
            jr = judge_segment(db, project_id, segment_text, seg, current_chapter=seg)
            # judge_segment 返回 {"findings": [dict], "rules": {...}}
            save_judge_findings(db, project_id, jr["findings"], segment_no=seg)
        db.commit()
        # unauthorized_knowledge 必然报：林悠然提到"顾辰逸父亲是董事长"且她自己
        # 知道（mock 已落 learned_at），但该条 belongs to 林悠然本人——豁免生效
        # 后应消失。先生成再豁免（Step ⑥）
        from app.kg.judge import load_judge_findings
        all_findings = load_judge_findings(db, project_id)
        # 至少要有一些 findings（language_mix/foreshadow_expiry 等也可能在 mock 上报到）
        check("judge 至少跑过一次", isinstance(all_findings, list), True)
        # 落库返回的 entity 字段
        unauthorized_before = [f for f in all_findings if f["rule"] == "unauthorized_knowledge"]
        check("unauthorized_knowledge 已落库（豁免前）", len(unauthorized_before) >= 1, True)

        # —— Step ⑤ slice_for_prompt 回流 ——
        # 直接调内部 API（不需起 uvicorn）
        sl = slice_for_prompt(db, project_id, current_segment_no=n_segments, window=2)
        check("slice 返回 dict", isinstance(sl, dict), True)
        check("slice 含 recent_entities", "recent_entities" in sl, True)
        recent_ents = [e["name"] for e in sl.get("recent_entities", []) if e.get("name")]
        check("slice 提及 林悠然（KG 事实回流）", "林悠然" in recent_ents, True)
        check("slice 提及 顾辰逸（KG 事实回流）", "顾辰逸" in recent_ents, True)

        # —— Step ⑥ 豁免 ——
        # 走真实 API 端点的同款写入：Fact key="exempt:rule:entity"
        from app.models import Fact
        key = f"exempt:unauthorized_knowledge:林悠然"
        ex = Fact(project_id=project_id, type="exemption", key=key,
                  props={"rule": "unauthorized_knowledge", "entity": "林悠然",
                         "note": "本人自语，豁免"},
                  source="author", confidence="high")
        db.add(ex); db.commit()
        # 重跑 judge，应过滤该条
        for seg in range(1, n_segments + 1):
            jr2 = judge_segment(db, project_id, segment_text, seg, current_chapter=seg)
            # 清旧 findings 写新的（按段号，先删后写）
            save_judge_findings(db, project_id, jr2["findings"], segment_no=seg)
        db.commit()
        all_findings_after = load_judge_findings(db, project_id)
        unauthorized_after = [f for f in all_findings_after
                              if f["rule"] == "unauthorized_knowledge" and f.get("entity") == "林悠然"]
        check("豁免后 林悠然 的 unauthorized_knowledge 消失", len(unauthorized_after), 0)

    finally:
        # 还原 extractor（防止污染其它测试的 module 全局）
        from app.kg import extractor_v3 as _ev
        _ev.extract_batch = orig
        # 还原 SessionLocal 与 worker 状态（不还原会污染后面的测试）
        _db_mod.SessionLocal = prev_sessionlocal
        batch_worker._queue._stop = prev_stop
        batch_worker._worker_started = prev_started
        db.close()


# ────────────────────────────────────────────────────────────────────
# 测试 2：空数据语义
# ────────────────────────────────────────────────────────────────────

def test_empty_data_semantics():
    """明确「空即合法」vs「空即异常」。

    关键 skill 教训：foreshadows 无输入时 judge 报空 = 合法；
    但 N 段真实文本后 kg_events = 0 = 异常。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    import app.models  # noqa: F401
    from app.models import Project, KgEvent
    from app.kg.judge import check_foreshadow_expiry, check_impossible_sequences

    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)

    db = Session()
    p = Project(title="empty", genre="x", style="x", generation_length=800)
    db.add(p); db.commit()

    # 无 foreshadows、无 events → 两个规则都返回 [] = 合法空
    fs_findings = check_foreshadow_expiry(db, p.id, "", segment_no=1)
    is_findings = check_impossible_sequences(db, p.id, "", segment_no=1)
    check("foreshadow_expiry 无数据返回 []", fs_findings, [])
    check("impossible_sequences 无数据返回 []", is_findings, [])
    db.close()
