"""Phase 0.5：生成前单轮定向取证——RAG 查询词对齐「本段目标」。

## 缺陷本体

`engine._rag_query(prior, project)` 把**上一段摘要**（`prior[-1]`）当检索词：它描述
「刚写完什么」，而不是「接下来要写什么」。首段更糟——`prior` 为空，查询词退化成
「书名 + 大纲第一条」（大纲是整篇的，不按段对齐）。

## 本次改动（`_rag_query_targeted`）

查询词改为 **目标侧 + 衔接侧**：

1. 目标侧：`reminders.unresolved_plotholes[*].question` + `reminders.character_constraints[*].name`
   （`build_context` 在 `engine.py:245` **已加载**，零额外 IO），每项压空白后截 `_C_ITEM`；
2. 衔接侧：`prior[-1]` 的尾部 `_C_TAIL` 字符；
3. 回退分两种，别混为一谈：
   (a) 目标侧与衔接侧**皆空** → 逐字节返回 `_rag_query(prior, project)`（自由生成 /
       首段无前情，行为与改动前完全一致）；
   (b) 目标侧为空**但有前情** → 查询词 = `prior[-1]` 的尾部窗口，这是设计内的
       **收窄**，与旧行为（原样返回整条 `prior[-1]`）不同；
4. 总长压到 `_C_QUERY`；`len(target)` 逼近 `_C_QUERY` 时衔接侧会被整段截掉
   （预算悬崖，既定后果，见 `test_t4c_query_budget_cliff_drops_tail`）。

`hybrid_search` 调用次数、DB 查询数、LLM 调用数一律不变（只换传进去的字符串）。

## 本文件的变异性质（每条 docstring 里写明）

- T1/T3/T4/T5/T6/T7 对「改动前的代码」是红（缺符号 / 缺返回键）。
- T2/T5 在**语义上**对旧实现恒真（旧代码根本不走新分支），判别力来自变异注入。
- T2b/T1b/T4b/T4c（QA round 2 补）钉的是**已实现的行为**：加入时即绿，作用是
  杀掉「窗口取头不取尾」「窗口整个不要」「拼接顺序反转」「单项不截断」
  「预算封顶 off-by-one」这五类此前无人钉住的变异。
"""

from __future__ import annotations

import hashlib
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.generation.engine as engine_mod  # noqa: E402
import app.routers.generation as gen_router_mod  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import build_context  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import Generation, KgNode, NovelChunk, Project, RawNovel  # noqa: E402

#: 伏笔特征词：造数据时放在 `question` 开头，断言只认它，避免与书名/摘要文字撞车。
HOLE_FEATURE = "玄铁令"
HOLE_QUESTION = "玄铁令到底落在谁手上？"
#: 角色名特征词（同样避免与其它字段撞车）。
CHAR_FEATURE = "沈砚舟"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_db():
    """隔离库：与本仓库既有测试同款（tests/conftest.py 已把 DATABASE_URL 指向临时库）。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _targeted():
    """取 `_rag_query_targeted`；未落地时给出一句能直接定位问题的失败信息。

    「先红」阶段这一步就是红的（ImportError/AttributeError 换成可读断言），
    避免整个模块 collection error 把其它用例的失败信息一起吞掉。
    """
    fn = getattr(engine_mod, "_rag_query_targeted", None)
    assert fn is not None, (
        "engine 缺少 _rag_query_targeted——Phase 0.5 的查询词构造未落地"
    )
    return fn


def _const(name: str) -> int:
    """取模块级常数；未定义时给出可读失败信息。"""
    val = getattr(engine_mod, name, None)
    assert isinstance(val, int) and val > 0, f"engine 缺少正整数常数 {name}（实际 {val!r}）"
    return val


def _make_project(
    db,
    title: str = "定向检索测试项目",
    outline: list | None = None,
    summary: list | None = None,
    novel_ids: list[int] | None = None,
) -> Project:
    proj = Project(
        title=title,
        target_length=5000,
        mode="quick",
        status="created",
        outline=outline,
        summary=summary,
        reference_novel_ids=novel_ids or [],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _make_corpus(db, tag: str, n_chunks: int = 6) -> int:
    """一本参考语料（`hybrid_search` 的检索对象）。"""
    novel = RawNovel(title=f"定向检索语料{tag}", status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for i in range(n_chunks):
        db.add(
            NovelChunk(
                novel_id=novel.id,
                chapter_no=i + 1,
                idx=i,
                text=f"第{i + 1}章：{tag}的雨落在铁皮屋顶上，他数着门缝里的光。",
            )
        )
    db.commit()
    return novel.id


def _seed_hole(db, project_id: int, question: str = HOLE_QUESTION) -> None:
    """未填伏笔（`get_reminders` 只收 status=open 的 plothole）。"""
    db.add(
        KgNode(
            project_id=project_id,
            type="plothole",
            props={"question": question, "status": "open"},
            first_chapter=1,
            source="generated",
        )
    )
    db.commit()


def _seed_character(db, project_id: int, name: str = CHAR_FEATURE) -> None:
    db.add(
        KgNode(
            project_id=project_id,
            type="character",
            props={"name": name, "mentions": 3},
            first_chapter=1,
            last_chapter=2,
            source="generated",
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# T1：定向生效——查询词必须含伏笔特征词
# ---------------------------------------------------------------------------


def test_t1_plothole_feature_word_reaches_query(db_session):
    """钉住：有伏笔 + 有前情时，查询词**含伏笔特征词**（目标侧真的进了检索词）。

    修复前：红——查询词直接 `return prior[-1]`，伏笔词完全不在查询词里。
    变异有效性：把目标侧整段删掉（只留 tail）→ 本用例变红。
    """
    targeted = _targeted()
    proj = _make_project(db_session)
    _seed_hole(db_session, proj.id)
    reminders = engine_mod.get_reminders(db_session, proj.id, source="generated")
    prior = ["他把门反锁，窗外的雨声盖住了脚步。"]

    query = targeted(reminders, prior, proj)

    assert HOLE_FEATURE in query, (
        f"查询词未包含未填伏笔特征词 {HOLE_FEATURE!r}——目标侧没有进入检索词：{query!r}"
    )
    # 衔接侧没有被目标侧挤掉（两段都要在）
    assert "雨声" in query, f"衔接侧（prior[-1] 尾部）丢失：{query!r}"


# ---------------------------------------------------------------------------
# T2：回退逐字节一致（含生僻字符 / 换行 / 全角空格）
# ---------------------------------------------------------------------------


def test_t2_fallback_is_byte_identical(db_session):
    """钉住：无目标信号且无前情时，返回值与 `_rag_query(prior, project)` **逐字节**相同。

    **本条对旧实现恒真**（旧代码根本没有新分支），判别力全部来自变异：
      - 把回退分支改成 `return ""` → 红（长度/哈希不等）；
      - 改走 `return (target + " " + tail).strip()`（即"总做后处理"）→ 红，因为
        拼接+strip 会吃掉 title 首尾空白并把 outline 项位置改写；
      - 在回退分支上补一句 `.strip()` 或 `" ".join(x.split())` 压空白 → 红
        （title 里的换行/全角空格被改写）。
    """
    targeted = _targeted()
    # 含生僻字符、换行、全角空格、制表符，且**首尾都带空白**——任何「顺手 strip /
    # 压空白」都会改字节。首尾空白必须有：否则 `.strip()` 是 no-op，变异会漏检
    # （本用例的第一版就是这样漏掉了 M3 变异）。
    weird_title = "  《\u2e80玄\u3000铁》\n第一卷\t主线  "
    proj = _make_project(
        db_session,
        title=weird_title,
        outline=["第一幕：\u2e80雨夜\n铁盒\t ", "第二幕：不算"],
        summary=[],
    )
    empty_reminders = {"unresolved_plotholes": [], "character_constraints": []}

    legacy = engine_mod._rag_query([], proj)
    assert legacy, "前置条件失效：_rag_query 的兜底不应为空（否则本用例无从比对）"

    got = targeted(empty_reminders, [], proj)

    assert got == legacy, f"回退不再逐字节一致：\n  got={got!r}\n  legacy={legacy!r}"
    assert len(got) == len(legacy), f"长度不等：{len(got)} != {len(legacy)}"
    assert hashlib.sha256(got.encode("utf-8")).hexdigest() == hashlib.sha256(
        legacy.encode("utf-8")
    ).hexdigest(), "sha256 不等——回退路径被加了后处理"


# ---------------------------------------------------------------------------
# T3：首段（prior 为空）不退化成「书名 + 大纲首项」
# ---------------------------------------------------------------------------


def test_t3_first_segment_uses_target_not_outline(db_session):
    """钉住：`prior=[]` 但伏笔非空时，查询词非空、含伏笔特征词、且**不等于**旧兜底。

    修复前：红——`prior` 为空时旧实现返回「书名 + outline[0]」，伏笔词根本不在里面。
    变异有效性：把目标侧删掉 → 退回旧兜底 → 本用例的 `!= legacy` 与 `in` 双双变红。
    """
    targeted = _targeted()
    proj = _make_project(
        db_session, outline=["第一幕：旧大纲不会出现在定向查询词里"], summary=[]
    )
    _seed_hole(db_session, proj.id)
    reminders = engine_mod.get_reminders(db_session, proj.id, source="generated")

    query = targeted(reminders, [], proj)
    legacy = engine_mod._rag_query([], proj)

    assert query, "首段查询词为空——定向侧未生效"
    assert HOLE_FEATURE in query, f"首段查询词未含伏笔特征词：{query!r}"
    assert query != legacy, f"首段仍退化成「书名+大纲首项」：{query!r} == {legacy!r}"


# ---------------------------------------------------------------------------
# T4：长度上界（常数由 backend/novel_dev.db 实测后定值）
# ---------------------------------------------------------------------------


def test_t4_query_length_bounded_on_pathological_input(db_session):
    """钉住：最坏输入（50 条伏笔 + 20 个角色名 + 巨长前情）下，查询词长度 ≤ `_C_QUERY`。

    实测（backend/novel_dev.db，只读）：伏笔 question 中位 8 / 最长 14 字符，
    角色名 2~3 字符，`prior[-1]` 中位 38 / 最长 46 字符（summary_tree 的 L1），
    旧查询词（`generations.summary`）最长 278 字符。故本用例刻意造**远超实测**的
    输入，验证封顶确实是「上界」而不是「刚好够用」。

    修复前：红（缺函数/常数）。
    变异有效性：把 `return query[:_C_QUERY]` 的截断去掉 → 本用例红（长度约 2400）。
    """
    targeted = _targeted()
    c_query = _const("_C_QUERY")
    proj = _make_project(db_session)

    # 上界情景（设计文档 §3.1）：50 条伏笔 × 每条 40 字符 + 20 个角色名 × 6 字符
    reminders = {
        "unresolved_plotholes": [{"question": "伏" * 40} for _ in range(50)],
        "character_constraints": [{"name": "角" * 6} for _ in range(20)],
    }
    prior = ["前" * 2000]

    query = targeted(reminders, prior, proj)

    # 先证「这个输入确实会超出上界」，否则断言可能恒真（常数大到离谱也过）
    untruncated = 50 * 40 + 20 * 6 + (50 + 20 - 1) + 1 + 2000
    assert untruncated > c_query, (
        f"用例失效：构造的未截断长度 {untruncated} 未超过 _C_QUERY={c_query}，"
        "断言变成恒真"
    )
    assert len(query) <= c_query, (
        f"查询词长度 {len(query)} 超过上界 _C_QUERY={c_query}——封顶失效"
    )


# ---------------------------------------------------------------------------
# T5：`hybrid_search` 调用次数不变（最坏输入：reminders 极多 + 语料 2 本）
# ---------------------------------------------------------------------------


def test_t5_hybrid_search_call_count_and_query_identity(db_session, monkeypatch):
    """钉住：每本语料仍**恰好 1 次** `hybrid_search`，两次收到的 query 相同且 == `ctx["rag_query"]`。

    **本条对旧实现恒真**（调用次数与查询词内容无关），判别力来自变异：
      - 在循环里把 `hybrid_search` 调两次 → 次数 3/4 → 红；
      - 循环内每本语料各自重算一次查询词（把构造挪进循环）→ 次数仍 2 但
        query 与 `ctx["rag_query"]` 可能不一致 → 「== ctx["rag_query"]」这条会红；
      - 把 `k=4` 改成按语料分化 → 不影响本用例（刻意的：本次不改 k）。
    修复前：红——`build_context` 返回值没有 `rag_query` 键。
    """
    proj = _make_project(db_session)
    _seed_hole(db_session, proj.id)
    _seed_character(db_session, proj.id)
    n1 = _make_corpus(db_session, "甲")
    n2 = _make_corpus(db_session, "乙")
    proj.reference_novel_ids = [n1, n2]
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)

    queries: list[str] = []

    def fake_search(db, novel_id, query, k=10):
        queries.append(query)
        return []

    monkeypatch.setattr(engine_mod, "hybrid_search", fake_search)

    ctx = build_context(db_session, proj)

    assert "rag_query" in ctx, "build_context 返回 dict 缺 rag_query 键"
    assert len(queries) == 2, (
        f"hybrid_search 调用次数变了：{len(queries)} 次（应为 2 = 每本语料 1 次），queries={queries!r}"
    )
    assert queries[0] == queries[1], f"不同语料收到的查询词不一致：{queries!r}"
    assert queries[0] == ctx["rag_query"], (
        f"检索实际用的查询词与 ctx['rag_query'] 不一致（可观测字段会骗人）："
        f"{queries[0]!r} != {ctx['rag_query']!r}"
    )
    # 定向真的生效（否则上面三条都可能「一致地空」而恒真）
    assert HOLE_FEATURE in ctx["rag_query"], (
        f"查询词未含伏笔特征词：{ctx['rag_query']!r}"
    )


# ---------------------------------------------------------------------------
# T6：兼容不回归——未选语料时 rag_context 仍为 []，rag_query 为 ""
# ---------------------------------------------------------------------------


def test_t6_no_reference_novel_keeps_empty_rag(db_session):
    """钉住：未选参考语料（自由生成）时 `rag_context == []` 且 `rag_query == ""`。

    这条对应设计文档 §1.5 的既有钉点（`backend/test_m4_api.py:239`
    「未选参考语料时 plan2["rag_context"] == []」）在 engine 层的等价保护。

    修复前：红（`build_context` 没有 `rag_query` 键）。
    变异有效性：在 `if novel_ids:` 之外无条件计算 `rag_query`
    （即把 `rag_query = _rag_query_targeted(...)` 提前到 `if` 之前）→ 本用例红
    （未选语料却报了非空查询词，可观测字段与真实检索不一致）。
    """
    proj = _make_project(db_session, summary=["上一段写完了。"])
    _seed_hole(db_session, proj.id)  # 有伏笔也不能在无语料时冒出查询词

    ctx = build_context(db_session, proj, novel_ids=[])

    assert ctx["rag_context"] == [], f"未选语料仍检索了：{ctx['rag_context']!r}"
    assert ctx["rag_query"] == "", (
        f"未选语料却有非空查询词（可观测字段说谎）：{ctx['rag_query']!r}"
    )


# ---------------------------------------------------------------------------
# T7：可观测——generate-plan 端点回传 rag_query，且与生成路径实际用的一致
# ---------------------------------------------------------------------------


def test_t7_generate_plan_exposes_rag_query(db_session, monkeypatch):
    """钉住：`POST /api/generate-plan` 返回含 `rag_query`，且等于 `build_context` 实际用的那个串。

    可观测是本次唯一的产品面新增（设计文档 §3.2）：让 A/B 不必跑生成就能看见
    「这次到底拿什么词去检索」。

    修复前：红（返回 dict 没有 rag_query 键）。
    变异有效性：把端点里的 `ctx.get("rag_query", "")` 换成写死的 `""`
    → 本用例的 `== expected` 与 `HOLE_FEATURE in body["rag_query"]` 双双变红。
    """
    proj = _make_project(db_session)
    _seed_hole(db_session, proj.id)
    nid = _make_corpus(db_session, "丙")
    proj.reference_novel_ids = [nid]
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)

    # 桩掉两条档案链路：本用例只关心查询词，不关心提示词内容/蒸馏耗时
    monkeypatch.setattr(engine_mod, "_style_narrative", lambda db, p: None)
    monkeypatch.setattr(engine_mod, "_style_profile", lambda db, p: None)
    monkeypatch.setattr(engine_mod, "hybrid_search", lambda db, nid, q, k=10: [])
    monkeypatch.setattr(gen_router_mod, "_style_narrative", lambda db, p: None)
    monkeypatch.setattr(gen_router_mod, "_style_profile", lambda db, p: None)

    with TestClient(fastapi_app) as client:
        resp = client.post("/api/generate-plan", json={"project_id": proj.id})
        assert resp.status_code == 200, f"端点失败：{resp.status_code} {resp.text[:500]}"
        body = resp.json()

    assert "rag_query" in body, f"generate-plan 未回传 rag_query：keys={sorted(body)}"
    assert isinstance(body["rag_query"], str), f"rag_query 类型异常：{body['rag_query']!r}"
    assert HOLE_FEATURE in body["rag_query"], (
        f"端点回的查询词不含伏笔特征词（可观测字段没接上真实查询词）：{body['rag_query']!r}"
    )

    # 与生成路径实际使用的那份逐字节一致
    ctx = build_context(db_session, proj)
    assert body["rag_query"] == ctx["rag_query"], (
        f"端点与生成路径使用的查询词不一致：{body['rag_query']!r} != {ctx['rag_query']!r}"
    )


# ---------------------------------------------------------------------------
# T2b：衔接侧窗口的**位置与长度**（杀掉「取头不取尾」「窗口整个不要」两类变异）
# ---------------------------------------------------------------------------


def test_t2b_tail_window_position_and_length(db_session):
    """钉住：衔接侧取的是 `prior[-1]` 的**最后** `_C_TAIL` 个字符，不多不少。

    独立 QA 的两个变异此前全绿（无用例钉住）：
      - γ：`prior[-1][-_C_TAIL:]` → `prior[-1][:_C_TAIL]`（取头不取尾）；
      - δ：`tail = prior[-1]`（把窗口去掉，整条摘要都塞进去）。

    **造数据必须用三段不同字符**（甲/乙/丙 各 100 字）：若全用同一个字，取头取尾
    结果一模一样，本用例对 γ 恒真——这正是第一版漏检的原因。

    修复前：红（缺函数）。注入 γ → `"甲"` 会出现在结果里且长度变了 → 红；
    注入 δ → 结果长度 300 而非 200 → 红。
    """
    targeted = _targeted()
    tail_len = _const("_C_TAIL")
    proj = _make_project(db_session)
    # 300 字，三段不同字符：尾部窗口 = 乙*100 + 丙*100（恰 200 字）
    prior = ["甲" * 100 + "乙" * 100 + "丙" * 100]
    empty_reminders = {"unresolved_plotholes": [], "character_constraints": []}

    query = targeted(empty_reminders, prior, proj)

    expected_tail = "乙" * 100 + "丙" * 100
    assert len(expected_tail) == tail_len, (
        f"用例前置条件失效：期望尾巴长度 {len(expected_tail)} != _C_TAIL={tail_len}"
    )
    assert query == expected_tail, (
        f"衔接侧窗口不是「prior[-1] 的最后 {tail_len} 字」：\n  got={query!r}\n  exp={expected_tail!r}"
    )
    assert query != prior[-1], "衔接侧仍是整条摘要（窗口未生效）"
    assert "甲" not in query, f"衔接侧取了头部而不是尾部（首段字符混入）：{query[:20]!r}…"


# ---------------------------------------------------------------------------
# T1b：目标侧在前、衔接侧在后（杀掉「拼接顺序反转」变异）
# ---------------------------------------------------------------------------


def test_t1b_target_before_tail_order(db_session):
    """钉住：查询词 = `target + " " + tail`，**目标侧在前**。

    独立 QA 的变异 η（`(tail + " " + target)`）此前全绿：原 T1 只断言 `in`，
    顺序反转也满足。本用例用 `startswith` / `endswith` 双向钉住顺序 + 窗口。

    修复前：红（缺函数）。注入 η → `startswith(伏笔词)` 失败 → 红。
    """
    targeted = _targeted()
    proj = _make_project(db_session)
    _seed_hole(db_session, proj.id)
    reminders = engine_mod.get_reminders(db_session, proj.id, source="generated")
    prior = ["甲" * 100 + "乙" * 100 + "丙" * 100]

    query = targeted(reminders, prior, proj)

    expected_target = " ".join(HOLE_QUESTION.split())  # 单项长度远小于 _C_ITEM，不被截断
    expected_tail = "乙" * 100 + "丙" * 100
    assert query.startswith(expected_target), (
        f"目标侧不在最前（顺序反了或目标侧丢失）：{query[:40]!r}"
    )
    assert query.endswith(expected_tail), (
        f"衔接侧不在最后（顺序反了或窗口取错）：{query[-40:]!r}"
    )
    assert query == expected_target + " " + expected_tail, (
        f"拼接结果不是「目标 + 单空格 + 衔接尾窗」：{query!r}"
    )


# ---------------------------------------------------------------------------
# T4b：单项截断 `_C_ITEM` 真的生效（杀掉「去掉 text[:_C_ITEM]」变异）
# ---------------------------------------------------------------------------


def test_t4b_single_item_truncated_to_c_item(db_session):
    """钉住：单条伏笔 question 超过 `_C_ITEM` 时被截到恰好 `_C_ITEM` 字符。

    独立 QA 的变异 ι（去掉 `text[:_C_ITEM]`）此前全绿：原有用例的伏笔都远短于
    40 字，截断分支根本没被执行。

    修复前：红（缺函数）。注入 ι → 查询词变成 100 个「伏」→ 红。
    """
    targeted = _targeted()
    c_item = _const("_C_ITEM")
    proj = _make_project(db_session)
    reminders = {
        "unresolved_plotholes": [{"question": "伏" * 100}],
        "character_constraints": [],
    }

    query = targeted(reminders, [], proj)

    assert query == "伏" * c_item, (
        f"单项未被截到 _C_ITEM={c_item}：实际长度 {len(query)}，内容 {query[:20]!r}…"
    )


def _reminders_exact_target(n: int) -> dict:
    """造一个 `target` 长度**恰为 n** 的 reminders（每项 ≤ `_C_ITEM`，不触发截断）。

    `target = " ".join(items)` ⇒ `len(target) = sum(项长) + (项数 - 1)`。
    """
    c_item = _const("_C_ITEM")
    k = 1
    while k * c_item + (k - 1) < n:  # 找到够用的最小项数
        k += 1
    need = n - (k - 1)  # 各单项长度之和
    base, rem = divmod(need, k)
    lengths = [base + (1 if i < rem else 0) for i in range(k)]
    assert sum(lengths) == need, (sum(lengths), need)
    assert all(1 <= length <= c_item for length in lengths), lengths
    return {
        "unresolved_plotholes": [{"question": "伏" * length} for length in lengths],
        "character_constraints": [],
    }


def _target_text(reminders: dict) -> str:
    """复刻「伏笔 question 压空白后按序拼成 target」的结果，用于在断言里定位边界。"""
    items = [
        " ".join(str(h.get("question") or "").split())
        for h in reminders["unresolved_plotholes"]
    ]
    return " ".join(i for i in items if i)


# ---------------------------------------------------------------------------
# T4c：预算悬崖——`len(target)` 逼近 `_C_QUERY` 时衔接侧被整段吃掉（既定后果）
# ---------------------------------------------------------------------------


def test_t4c_query_budget_cliff_drops_tail(db_session):
    """钉住预算悬崖：`query = (target + " " + tail).strip()[: _C_QUERY]`。

    下面两条恒等式是三个点位断言的**理论依据**，但**不要在断言里反算它们**：

    1. **尾巴保留的字符数** = `max(0, _C_QUERY - len(target) - 1)`；
    2. **总长** = `min(len(target) + 1 + len(tail), _C_QUERY)`。

    为什么明确禁止反算：用**实现公式**反算期望值，会把实现的错误同步复制进测试，
    从而永远绿。这条是**原理层面的理由，本仓库没有做过「反算版断言」的对照实验**
    （不要把它写成实测结论）。本仓库实测过的只有下面这份红/绿清单——针对的是当前
    这版**具体字面量**断言。

    本用例用**具体字面量**钉住三个锚点：

    - 100 —— 尾巴完整保留：`len == 100 + 1 + 200`、`endswith("甲" * 200)`；
    - 359 —— 只剩那个分隔空格：目标侧之后的部分 `.strip() == ""`、总长恰 360
      （`target + " "` 恰好占满；末尾空格无害，`app/tokenize.py` 把非字母数字
      一律当分隔符丢掉）；
    - 360 —— 恰为目标侧本身：`q == target`，分隔符与尾巴都不出现。

    实测覆盖面（**只钉机制与边界，不钉常量取值**——常量取值本身即规格，
    在这里再钉一遍是冗余）：
    - 变红：切片改成 `[:_C_QUERY + 1]` 或 `[:_C_QUERY - 1]`（off-by-one）；
      去掉总长截断（`return query`）；只截尾巴不截总长；把 `_C_QUERY` 压到 200
      （装不下一整条 200 字尾巴，锚点 ① 立刻破）。
    - **仍绿**（刻意不钉）：`_C_QUERY` 改成 310 / 410 / 500 —— 切片随常量同步变，
      三个锚点仍成立；也**不**区分「先截 target 再拼 tail 且末尾仍做总截断」这类
      **语义等价**的冗余写法（`target[:_C_QUERY]` 对不长于 `_C_QUERY` 的目标是 no-op）。

    这是「目标侧优先」拼接的既定后果（50 条实测 14 字伏笔 = 50*14+49 = 749 > 360
    必然触发），不是 bug；钉住它是为了防止将来改实现时**静默**退化（比如有人把
    截断写成「只截尾巴」，衔接侧就会在长目标下悄悄留长）。

    修复前：红（缺函数）。
    """
    targeted = _targeted()
    c_query = _const("_C_QUERY")
    proj = _make_project(db_session)
    # 尾巴用「甲」：300 字，窗口取尾部 200 字仍是「甲」，便于用 `"甲" in q` 判断去留
    prior = ["甲" * 300]

    # ① 目标侧 100 字：360-100-1=259 ≥ 200，尾巴**完整**保留（200 字全留）
    assert c_query - 100 - 1 > 200, (
        f"用例前置条件失效：_C_QUERY={c_query} 时目标侧 100 字不足以让尾巴完整保留"
    )
    q_short = targeted(_reminders_exact_target(100), prior, proj)
    assert len(q_short) == 100 + 1 + 200, f"目标侧 100 时尾巴应整段保留：len={len(q_short)}"
    assert q_short.endswith("甲" * 200), "目标侧 100 时衔接侧未完整保留"


    # ② 目标侧 359 字（= _C_QUERY-1）：尾巴保留 0 字，只剩那个分隔空格
    rem_edge = _reminders_exact_target(c_query - 1)
    target_edge = _target_text(rem_edge)
    assert len(target_edge) == c_query - 1, (
        f"用例前置条件失效：构造的 target 长度 {len(target_edge)} != {c_query - 1}"
    )
    q_edge = targeted(rem_edge, prior, proj)
    kept_edge = q_edge[len(target_edge):]  # 目标侧之后的部分
    assert kept_edge.strip() == "", (
        f"目标侧已占满预算，衔接侧本应一个字符不留，实际残留：{kept_edge[:20]!r}…"
    )
    assert len(q_edge) == c_query, (
        f"目标侧 {c_query - 1} 字时总长应恰为 {c_query}（含分隔空格），实际 {len(q_edge)}"
    )

    # ③ 目标侧 360 字（= _C_QUERY）：结果就恰是目标侧本身，空格与尾巴都不出现
    rem_full = _reminders_exact_target(c_query)
    target_full = _target_text(rem_full)
    assert len(target_full) == c_query, (
        f"用例前置条件失效：构造的 target 长度 {len(target_full)} != {c_query}"
    )
    q_full = targeted(rem_full, prior, proj)
    assert q_full == target_full, (
        f"目标侧占满预算时结果应恰为目标侧本身（既无分隔符也无尾巴）：尾 5 字 {q_full[-5:]!r}"
    )

