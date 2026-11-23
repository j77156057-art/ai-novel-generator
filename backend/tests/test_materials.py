# -*- coding: utf-8 -*-
"""素材包：目标条数、去重/年份、目录、注入、工具与后台任务。"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.db import Base  # noqa: E402
from app.materials import research, store, worker  # noqa: E402
from app.materials.pack import build_block  # noqa: E402
from app.materials.settings import MaterialSettings, target_raw, target_usable  # noqa: E402
from app.models import MaterialResearchJob, Project, ProjectMaterial  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _project(db, **kwargs) -> Project:
    project = Project(
        title="素材包测试",
        genre="都市",
        premise="老城区改造与几个小人物的生活。",
        outline=[f"第{i}章：第 {i} 章的拆迁与谈判。" for i in range(1, 6)],
        target_length=kwargs.pop("target_length", 100000),
        **kwargs,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


# --------------------------------------------------------------------------- #
# 目标条数
# --------------------------------------------------------------------------- #

def test_target_counts():
    assert target_usable(20_000) == 20
    assert target_usable(50_000) == 20
    assert target_usable(75_000) == 30
    assert target_usable(100_000) == 40
    assert target_usable(150_000) == 60
    assert target_raw(20_000) == 40
    assert target_raw(100_000) == 40
    assert target_raw(200_000) == 80


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #

def test_store_dedupe_year_toc_and_usage(db):
    pid = _project(db).id
    rows = store.add_many(
        db,
        pid,
        [
            {"title": "2023 年旧城改造征收流程", "snippet": "含评估与补偿。"},
            {"title": "2023 年旧城改造征收流程", "snippet": "含评估与补偿。"},
            {"title": "明代官服补子", "snippet": "文官绣鸟、武官绣兽。"},
        ],
        status="usable",
        origin="manual",
    )
    assert len(rows) == 2
    assert rows[0].published_year == 2023

    toc = store.toc(db, pid)
    assert toc["stats"]["total"] == 2
    assert toc["stats"]["unknown_year"] == 1

    hit = store.search(db, pid, "官服")
    assert len(hit) == 1
    assert store.mark_used(db, [hit[0].id], project_id=pid) == 1
    db.refresh(hit[0])
    assert hit[0].use_count == 1


def test_year_parse():
    assert research.find_year("", "2023 年的征收办法", "") == (2023, "2023")
    assert research.find_year("旧城改造", "没有年份", "") == (None, "")


def test_resolve_year_prefers_evidence_and_rejects_founding():
    # 正文里的「2007 年成立」是机构历史，不是素材年份。
    assert research.resolve_year(
        "房屋交接查水电表",
        "石家庄科恒电子，2007年成立于高新区，专注 PCBA 服务。",
        "",
    ) == (None, "", "none")

    # 渠道发布时间优先级最高。
    assert research.resolve_year("任意标题", "没有日期", "", "2024-01-02T10:00:00")[:2] == (
        2024,
        "2024-01-02T10:00:00",
    )

    # URL 日期路径。
    assert research.resolve_year(
        "旧城改造", "正文没有年份", "https://news.example.com/2023/05/12/city"
    )[:2] == (2023, "2023")

    # 摘要里的完整日期。
    assert research.resolve_year("", "发布于 2022-11-03 的征收办法", "")[:2] == (
        2022,
        "2022-11-03",
    )

    # 「更新于 2021 年」这类发布措辞。
    assert research.resolve_year("", "最后更新于 2021 年", "")[:2] == (2021, "2021")


def test_query_sanitizer_drops_novel_and_worldbuilding_terms():
    cleaned = research._clean_queries(
        [
            "都市修真 世界观 设定",
            "灵石 设定集",
            "灵脉 地质 断裂带",
            "许照 租房 日常",
            "租房流程一",
            "租房流程二",
            "租房流程三",
        ],
        surface=["租房"],
        theme="都市修真",
        needs=["民间禁忌"],
        limit=20,
        blocked_terms=["许照"],
    )
    assert all(
        word not in q
        for q in cleaned
        for word in ("设定", "世界观", "小说", "许照")
    )
    assert sum(1 for q in cleaned if "租房" in q) <= 2
    assert "灵脉 地质 断裂带" in cleaned


def test_carrier_queries_are_capped():
    premise = "房东把空房低价出租，租客用灵石抵房租，水电抄表，押金交给二房东。"
    cleaned = research._clean_queries(
        [
            "二房东 转租 法律风险",
            "押一付三 租房合同",
            "水电 抄表 底数",
            "二手房 出租 挂网 平台",
            "道教隐士 修行",
            "风水 龙脉 勘探",
        ],
        surface=["租房"],
        theme="都市修真",
        needs=["道教文化", "风水龙脉"],
        premise=premise,
        limit=20,
        blocked_terms=[],
    )
    carrier = [
        q
        for q in cleaned
        if any(k in q for k in ("二房东", "租房", "水电", "二手房", "出租"))
    ]
    assert len(carrier) <= 2
    assert "道教隐士 修行" in cleaned
    assert "风水 龙脉 勘探" in cleaned


def test_is_fiction_result():
    assert research.is_fiction_result(
        "灵气大盗修仙传完整版在线免费阅读", "", "https://fanqienovel.com/x"
    )
    assert research.is_fiction_result("某某小说最新章节", "", "https://www.qidian.com/book/1")
    assert not research.is_fiction_result(
        "旧城改造征收流程", "某市政府发布征收补偿办法", "https://www.gov.cn/a"
    )


def test_parse_published_year_html():
    assert research.parse_published_year_html(
        '<meta property="article:published_time" content="2023-05-12T08:00:00+08:00">'
    )[:2] == (2023, "2023-05-12T08:00:00+08:00")
    assert research.parse_published_year_html(
        '<script>{"datePublished":"2024-01-02"}</script>'
    )[:2] == (2024, "2024-01-02")
    assert research.parse_published_year_html(
        "<p>正文日期 2019-03-04</p>"
    )[:2] == (2019, "2019-03-04")
    assert research.parse_published_year_html("<p>无日期</p>") == (None, "")
    # 正文里的历史年份、附近没有发布/日期字样时不再当发布年份。
    assert research.parse_published_year_html(
        "<p>2001 年烟台确立新定位，城市交通逐步打破龙脉神话。</p>"
    ) == (None, "")


def test_backfill_years_clears_unsupported_and_updates_url(db):
    pid = _project(db).id
    rows = store.add_many(
        db,
        pid,
        [
            {
                "title": "房屋交接查水电表",
                "snippet": "某公司 2007 年成立于高新区。",
            },
            {
                "title": "旧城改造流程",
                "snippet": "正文没有年份。",
                "url": "https://example.com/2023/05/12/city",
            },
            {
                "title": "手动素材",
                "snippet": "正文没有年份。",
                "published_year": 1999,
            },
        ],
        status="usable",
    )
    # 模拟旧规则留下的错误年份。
    rows[0].published_year = 2007
    rows[0].published_at = "2007"
    rows[1].published_year = 2019
    rows[1].published_at = "2019"
    rows[2].origin = "manual"
    db.commit()

    result = store.backfill_years(db, pid)
    db.refresh(rows[0])
    db.refresh(rows[1])
    db.refresh(rows[2])
    assert rows[0].published_year is None
    assert rows[1].published_year == 2023
    assert rows[2].published_year == 1999  # 手动素材不动
    assert result["cleared"] >= 1
    assert result["updated"] >= 1


def test_year_enrichment_fetches_pages_concurrently(db, monkeypatch):
    """年份补全改为并发抓页：同一预算内覆盖全部素材，且确实是并行发出。"""
    import threading
    import time

    project = _project(db, target_length=20000)
    job = MaterialResearchJob(
        project_id=project.id, chapter_no=1, trigger="manual", status="running",
        target_raw=3, target_usable=1, dedupe_key=f"p{project.id}:c1",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    for i in range(3):
        db.add(ProjectMaterial(
            project_id=project.id, job_id=job.id, title=f"条目{i}",
            url=f"https://example.com/{i}", snippet="摘要",
            fingerprint=f"fp-{i}", status="raw",
        ))
    db.commit()

    seen: list[str] = []
    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_fetch(url, *, timeout_s=5.0):
        nonlocal active, peak
        with lock:
            seen.append(url)
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return 2021, "2021-01-01"

    monkeypatch.setattr(research, "fetch_published_year", fake_fetch)
    worker._enrich_years_from_pages(db, job)

    rows = db.query(ProjectMaterial).filter(ProjectMaterial.job_id == job.id).all()
    assert len(seen) == 3
    assert peak >= 2, f"抓页没有并行（peak={peak}）"
    assert all(r.published_year == 2021 for r in rows)
    assert (job.meta or {}).get("year_enriched") == 3


def test_list_unknown_year_filter(db):
    pid = _project(db).id
    store.add_many(
        db,
        pid,
        [
            {"title": "有年份素材", "snippet": "内容", "published_year": 2024},
            {"title": "无年份素材", "snippet": "内容"},
        ],
        status="usable",
    )
    unknown = store.list_materials(db, pid, unknown_year=True)
    assert unknown["total"] == 1
    assert unknown["items"][0]["published_year"] is None


def test_assistant_material_tool_rendering_is_compact():
    from app.routers.assistant import _qa_evidence_from_tool, _qa_tool_message

    payload = {
        "ok": True,
        "text": "【素材】风水禁忌：泰山石敢当、上梁仪式。",
        "items": [{"id": 1, "summary": "很长的元数据" * 100}],
        "used_ids": [1],
    }
    message = _qa_tool_message("material_pack_search", payload)
    assert "泰山石敢当" in message
    assert "used_ids" not in message
    assert "很长的元数据" not in message
    assert len(message) < 300

    rows, lines, _ = _qa_evidence_from_tool("material_pack_search", payload, 1)
    assert rows and lines
    assert "泰山石敢当" in lines[0]
    assert "used_ids" not in lines[0]


def test_search_runner_cooldown_on_blocked(db):
    from app.retrieval.brief.backend import SearchError

    class _BlockedBackend:
        def __init__(self):
            self.calls = 0

        def search(self, query, count):
            self.calls += 1
            raise SearchError("captcha", blocked=True)

    runner = research.SearchRunner(
        db, MaterialSettings(block_cooldown_s=60, search_min_interval_s=1.0)
    )
    backend = _BlockedBackend()
    runner._web = False
    runner._free = backend
    research.reset_cooldowns()

    results, error = runner.search("q1")
    assert results == []
    assert error.startswith("blocked:")

    results, error = runner.search("q2")
    assert results == []
    assert error == "source_cooldown"
    assert backend.calls == 1

    research.reset_cooldowns()


def test_subgenre_mechanism_queries_are_added(db):
    cases = [
        ("签到系统逼我当大佬", "都市", "大学生绑定签到系统，每天打卡获得奖励。", "游戏任务 设计 奖励 机制"),
        ("开局就无敌", "玄幻", "主角开局满级，各路强者上门。", "战力 分级 体系 设计"),
        ("我在大胤当县令", "历史", "现代公务员穿越到大胤朝当县令。", "古代 县级 行政 制度"),
        ("规则怪谈：第七层", "悬疑", "老公寓每层贴一条规则，违反者消失。", "规则 设计 心理学 实验"),
    ]
    for title, genre, premise, expected in cases:
        project = Project(
            title=title,
            genre=genre,
            premise=premise,
            plot_bible={"synopsis": premise},
            outline=[f"第1章：{premise}"],
        )
        plan = research.infer_topics(db, project, 1, provider=_FakeProvider())
        assert expected in plan["queries"], (title, expected)


def test_reference_queries_allow_novel_terms_and_fallback(db):
    project = _project(db)
    cleaned = research._clean_reference_queries(
        [
            "都市修真 网文 代表作",
            "许照 小说 推荐",
            "民间怪谈 小说 推荐",
            "《道教内丹术》",
            "《山海经》小说",
        ],
        blocked_terms=["许照"],
        limit=6,
    )
    assert "都市修真 网文 代表作" in cleaned
    assert "民间怪谈 小说 推荐" in cleaned
    assert "《道教内丹术》" not in cleaned
    assert "《山海经》小说" not in cleaned
    assert all("许照" not in q for q in cleaned)

    fallback = research.fallback_reference_queries(
        project, theme="都市修真", limit=4
    )
    assert fallback
    assert any("小说" in q or "网文" in q for q in fallback)


def test_finalize_fiction_rows_into_directory(db):
    pid = _project(db).id
    store.add_many(
        db,
        pid,
        [{"title": "同类小说 A", "snippet": "另一本小说的设定。"}],
        status="raw",
    )
    # 手动改分类，模拟 worker 入库时的标记。
    rows = db.query(ProjectMaterial).filter(ProjectMaterial.project_id == pid).all()
    rows[0].category = research.FICTION_CATEGORY
    db.commit()

    assert research.finalize_fiction_rows(db, pid) == 1
    assert store.count_usable(db, pid) == 1
    toc = store.toc(db, pid)
    sections = {s["category"]: s for s in toc["directory"]}
    assert sections[research.FICTION_CATEGORY]["count"] == 1


class _RefAwareRunner:
    provider_name = "fake"

    def __init__(self, *args, **kwargs):
        pass

    def search(self, query: str, *, count: int | None = None):
        if "网文" in query or "小说" in query:
            return (
                [
                    {
                        "title": "都市修真 同类小说推荐",
                        "snippet": "同题材网文参考。",
                        "url": "https://www.qidian.com/book/9",
                    }
                ],
                "",
            )
        return (
            [
                {
                    "title": f"{query} 资料",
                    "snippet": "现实知识素材。",
                    "url": "https://example.org/knowledge",
                }
            ],
            "",
        )


def test_reference_queries_run_even_when_target_met(db, monkeypatch):
    project = _project(db, target_length=20000)
    monkeypatch.setattr(worker, "ensure_worker", lambda: None)
    job = worker.enqueue_research(db, project, 1, trigger="manual")
    assert job is not None
    # 不预设 queries：让主题推断生成知识查询 + reference 查询。
    job.target_raw = 2
    job.target_usable = 1
    db.commit()

    monkeypatch.setattr(research, "SearchRunner", _RefAwareRunner)
    monkeypatch.setattr(research, "_provider", lambda: _FakeProvider())
    monkeypatch.setattr(research, "fetch_published_year", lambda *a, **k: (None, ""))
    assert worker._run_job(db, job.id) is True

    db.refresh(job)
    assert (job.meta or {}).get("reference_queries")
    rows = (
        db.query(ProjectMaterial)
        .filter(ProjectMaterial.project_id == project.id)
        .all()
    )
    fiction = [r for r in rows if r.category == research.FICTION_CATEGORY]
    assert len(fiction) == 1


def test_toc_directory_groups_and_items(db):
    pid = _project(db).id
    store.add_many(
        db,
        pid,
        [
            {"title": "风水知识", "category": "风水"},
            {"title": "同类小说 A", "category": research.FICTION_CATEGORY},
            {"title": "无分类素材"},
        ],
        status="usable",
    )
    data = store.toc(db, pid)
    sections = {s["category"]: s for s in data["directory"]}
    assert sections[research.FICTION_CATEGORY]["count"] == 1
    assert sections[research.FICTION_CATEGORY]["items"][0]["title"] == "同类小说 A"
    assert "未分类" in sections
    assert sections["风水"]["count"] == 1


class _MixedRunner:
    provider_name = "fake"

    def __init__(self, *args, **kwargs):
        pass

    def search(self, query: str, *, count: int | None = None):
        return (
            [
                {
                    "title": "都市修真小说最新章节",
                    "snippet": "另一本小说的世界观与桥段。",
                    "url": "https://www.qidian.com/book/1",
                },
                {
                    "title": "道教采月华吐纳术",
                    "snippet": "道教采月精的修行方法。",
                    "url": "https://example.org/dao",
                },
            ],
            "",
        )


def test_worker_keeps_fiction_as_reference(db, monkeypatch):
    project = _project(db)
    monkeypatch.setattr(worker, "ensure_worker", lambda: None)
    job = worker.enqueue_research(db, project, 1, trigger="manual")
    assert job is not None
    job.queries = ["都市修行"]
    job.target_raw = 2
    job.target_usable = 1
    db.commit()

    monkeypatch.setattr(research, "SearchRunner", _MixedRunner)
    monkeypatch.setattr(research, "_provider", lambda: _FakeProvider())
    # 测试不联网补年份。
    monkeypatch.setattr(research, "fetch_published_year", lambda *a, **k: (None, ""))
    assert worker._run_job(db, job.id) is True

    rows = (
        db.query(ProjectMaterial)
        .filter(ProjectMaterial.project_id == project.id)
        .all()
    )
    assert len(rows) == 2
    fiction = [r for r in rows if r.category == research.FICTION_CATEGORY]
    assert len(fiction) == 1
    assert fiction[0].status == "usable"


# --------------------------------------------------------------------------- #
# 注入
# --------------------------------------------------------------------------- #

def test_add_many_dedupes_same_title_different_url(db):
    """同一篇文章换跳转 URL（百度 link?url= 每次不同）不能重复入库。"""
    project = _project(db)
    title = "我是长沙人,五一去了南昌后发现:南昌人跟其他地方的人不大一样_新浪新闻"
    added = store.add_many(
        db, project.id,
        [
            {"title": title, "url": "http://www.baidu.com/link?url=AAA"},
            {"title": title, "url": "http://www.baidu.com/link?url=BBB"},
        ],
        status="usable",
    )
    assert len(added) == 1

    # 太短的通用标题不按标题合并，避免误删不同来源的同名文章。
    short = "建筑风水禁忌 - 知乎专栏"
    added2 = store.add_many(
        db, project.id,
        [
            {"title": short, "url": "u1"},
            {"title": short, "url": "u2"},
        ],
        status="usable",
    )
    assert len(added2) == 2


def test_pack_limits_fiction_references(db):
    project = _project(db)
    items = []
    for i in range(10):
        items.append({"title": f"知识素材{i}", "summary": "现实知识素材。", "category": "风水"})
        items.append(
            {
                "title": f"同类小说{i}",
                "summary": "同题材小说参考。",
                "category": research.FICTION_CATEGORY,
            }
        )
    store.add_many(db, project.id, items, status="usable")
    block, ids = build_block(db, project, 1)
    assert block.count("[同类小说参考]") <= 2
    assert "知识素材" in block


def test_pack_injection_and_toc_mode(db):
    project = _project(db)
    store.add_many(
        db,
        project.id,
        [
            {"title": "拆迁谈判流程", "summary": "评估、公示、签约。", "use_case": "第 1 章 拆迁"},
            {"title": "明代官服", "summary": "补子区分品级。", "use_case": "人物服饰"},
        ],
        status="usable",
    )
    block, ids = build_block(db, project, 1)
    assert "拆迁谈判流程" in block
    assert ids

    # 阈值降到 1 后应改给目录 + 工具提示，而不是整块塞正文。
    tiny = MaterialSettings(toc_threshold=1)
    toc_block, toc_ids = build_block(db, project, 1, settings=tiny)
    assert "素材包目录" in toc_block
    assert toc_ids == []

    # 兜底通道不看阈值：目录模式下仍能拿回具体条目与 id。
    from app.materials.pack import build_rows_block

    rows_block, rows_ids = build_rows_block(db, project, 1, settings=tiny)
    assert "拆迁谈判流程" in rows_block
    assert rows_ids


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def test_material_tools_are_registered_readonly():
    from app.agents.registry import get_tool_registry

    registry = get_tool_registry()
    names = {
        item["function"]["name"] for item in registry.describe_tools()
    }
    for name in ("material_pack_toc", "material_pack_search",
                 "material_pack_get", "material_pack_by_use"):
        assert name in names
        spec = registry.resolve(name)
        assert spec.read_only is True


def test_material_search_tool_marks_ids(db):
    from app.materials import tool

    pid = _project(db).id
    store.add_many(
        db, pid,
        [{"title": "旧城改造征收", "summary": "流程与补偿。"}],
        status="usable",
    )
    result = tool.impl_search(db, pid, query="征收")
    assert result["count"] == 1
    # 搜索只是候选，不增加使用次数。
    assert result["used_ids"] == []
    got = tool.impl_get(db, pid, ids=[result["items"][0]["id"]])
    assert got["used_ids"]


# --------------------------------------------------------------------------- #
# 主题推断 / 整理
# --------------------------------------------------------------------------- #

class _FakeProvider:
    def __init__(self, mode: str = "ok"):
        self.mode = mode
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        if self.mode == "fail":
            raise RuntimeError("no model")
        if "资料研究员" in system:
            return json.dumps(
                {
                    "theme": "都市修仙",
                    "surface": ["租房", "交房", "水电"],
                    "needs": ["灵脉世界观", "丹炉器物", "精怪传说", "旧改与超凡冲突"],
                    "topics": ["灵脉", "丹炉", "精怪", "旧改"],
                    "queries": {
                        "theme": ["道教隐士修行方式", "玉石原石鉴定方法"],
                        "event": ["旧城改造居民谈判", "民间符咒禁忌"],
                        "character": ["宫观簿记与账本", "矿石交易以物抵债"],
                        "texture": ["老城区车库仓储", "市井老楼邻里生活"],
                        "surface": [
                            "押一付三租房合同",
                            "交房水电抄表",
                            "租房流程三",
                        ],
                        "reference": [
                            "都市修真 网文 代表作",
                            "民间修行 小说 推荐",
                        ],
                    },
                },
                ensure_ascii=False,
            )
        return (
            '[{"index":1,"summary":"征收分评估、公示、补偿三步。",'
            '"use_case":"第 1 章 拆迁谈判","category":"制度流程",'
            '"published_year":2023,"tags":["征收","流程"]}]'
        )


def test_infer_topics_and_consolidate(db):
    project = _project(db)
    plan = research.infer_topics(
        db, project, 1, provider=_FakeProvider()
    )
    assert plan["source"] == "model"
    assert plan["theme"] == "都市修仙"
    assert len(plan["queries"]) >= 4
    # 载体查询按分组截断：surface 只取前 2 条，第三条不能进来。
    assert "租房流程三" not in plan["queries"]
    assert sum(1 for q in plan["queries"] if "租房" in q) <= 2
    assert sum(1 for q in plan["queries"] if "交房" in q) <= 2
    # 现实知识类查询必须保留。
    assert any("道教" in q or "矿石" in q for q in plan["queries"])
    # 同题材网文 reference 查询允许出现「小说/网文」并单独记录。
    assert "都市修真 网文 代表作" in plan["reference_queries"]
    assert "民间修行 小说 推荐" in plan["queries"]
    assert any("网文" in q for q in plan["reference_queries"])

    rows = store.add_many(
        db, project.id,
        [{"title": "征收流程", "snippet": "2023 年评估公示补偿。"}],
        status="raw",
    )
    done = research.consolidate(db, rows, provider=_FakeProvider())
    assert done == 1
    db.refresh(rows[0])
    assert rows[0].status == "usable"
    assert rows[0].consolidated_by == "model"
    assert rows[0].published_year == 2023


def test_infer_topics_falls_back_and_consolidate_heuristic(db):
    project = _project(db)
    plan = research.infer_topics(db, project, 1, provider=_FakeProvider("fail"))
    assert plan["source"] == "fallback"
    assert plan["queries"]

    rows = store.add_many(
        db, project.id,
        [{"title": "职业流程", "snippet": "具体步骤。"}],
        status="raw",
    )
    research.consolidate(db, rows, provider=_FakeProvider("fail"))
    db.refresh(rows[0])
    assert rows[0].status == "usable"
    assert rows[0].consolidated_by == "heuristic"


def test_hook_collect_injects_pack_and_enqueues(db, monkeypatch):
    from app.materials import hook

    project = _project(db)
    store.add_many(
        db, project.id,
        [{"title": "拆迁谈判流程", "summary": "评估、公示、签约。", "use_case": "第 1 章 拆迁"}],
        status="usable",
    )
    enqueued: list[bool] = []
    monkeypatch.setattr(worker, "enqueue_research", lambda *a, **k: enqueued.append(True))
    monkeypatch.setattr(store, "mark_used", lambda *a, **k: 1)

    blocks = hook.collect(
        db,
        project,
        1,
        brief_blocks=["原有块"],
        allow_tool_round=False,
        settings=MaterialSettings(auto_research=True, toc_threshold=999),
    )
    assert blocks[0] == "原有块"
    assert any("拆迁谈判流程" in b for b in blocks[1:])
    assert enqueued

    off = hook.collect(
        db, project, 1, brief_blocks=["原有块"],
        settings=MaterialSettings(enabled=False),
    )
    assert off == ["原有块"]


def test_hook_falls_back_to_rows_when_tool_round_empty(db, monkeypatch):
    """目录模式下模型没取到素材（工具轮返回空）→ 直注 Top-N 并计使用次数。"""
    from app.materials import hook, tool

    project = _project(db)
    store.add_many(
        db, project.id,
        [{"title": "拆迁谈判流程", "summary": "评估、公示、签约。", "use_case": "第 1 章 拆迁"}],
        status="usable",
    )
    marked: list[list[int]] = []
    monkeypatch.setattr(store, "mark_used", lambda _db, ids, **k: marked.append(list(ids)))
    seen: dict = {}

    def _no_tools(provider, db_, project_, system, user, **kwargs):
        seen.update(system=system, user=user, **kwargs)
        return []

    monkeypatch.setattr(tool, "gather_pack_blocks", _no_tools)
    cfg = MaterialSettings(auto_research=False, toc_threshold=0)

    blocks = hook.collect(
        db, project, 1, provider=object(), allow_tool_round=True, settings=cfg
    )
    assert any("素材包目录" in b for b in blocks)
    direct = [b for b in blocks if "已标注年份与适用处" in b]
    assert direct and "拆迁谈判流程" in direct[0]
    assert marked and marked[0]
    # 工具轮拿到的必须是瘦上下文，而不是整份写作 prompt。
    assert len(seen["system"]) < 100
    assert "素材包目录" in seen["user"]
    assert "检索要求" in seen["user"]
    assert seen["budget_s"] == cfg.tool_round_budget_s

    # 工具轮真的取到素材时不做兜底，避免重复注入。
    marked.clear()
    monkeypatch.setattr(tool, "gather_pack_blocks", lambda *a, **k: ["【工具取回】素材"])
    blocks2 = hook.collect(
        db, project, 1, provider=object(), allow_tool_round=True, settings=cfg
    )
    assert blocks2[-1] == "【工具取回】素材"
    assert not any("已标注年份与适用处" in b for b in blocks2)
    assert not marked

    # 没开工具轮（fast_mode）时同样直注，而不是只剩目录。
    def _boom(*a, **k):
        raise AssertionError("fast_mode 不应调用素材工具轮")

    monkeypatch.setattr(tool, "gather_pack_blocks", _boom)
    marked.clear()
    blocks3 = hook.collect(
        db, project, 1, provider=object(), allow_tool_round=False, settings=cfg
    )
    assert any("已标注年份与适用处" in b for b in blocks3)
    assert marked


def test_dispatch_counts_search_hits_only_for_generation(db, monkeypatch):
    """生成链路里 search/by_use 的命中条目计入使用次数；助手路径不计数。"""
    from app.materials import tool

    pid = _project(db).id
    store.add_many(
        db, pid,
        [{"title": "旧城改造征收流程", "summary": "签约与补偿。"}],
        status="usable",
    )
    marked: list[list[int]] = []
    monkeypatch.setattr(store, "mark_used", lambda _db, ids, **k: marked.append(list(ids)))

    # 助手口径（默认）：候选不算使用。
    tool.dispatch(db, pid, "material_pack_search", {"query": "征收"})
    assert marked == []

    # 生成口径：拼进 prompt 的命中条目计数。
    tool.dispatch(db, pid, "material_pack_search", {"query": "征收"}, count_hits=True)
    assert marked and marked[0]

    # 同一轮工具调用里重复命中同一条，只计 1 次。
    marked.clear()
    seen: set[int] = set()
    tool.dispatch(
        db, pid, "material_pack_search", {"query": "征收"},
        count_hits=True, seen_ids=seen,
    )
    assert marked and marked[0]
    marked.clear()
    tool.dispatch(
        db, pid, "material_pack_search", {"query": "征收"},
        count_hits=True, seen_ids=seen,
    )
    assert marked == []


def test_filter_duplicate_entries_dedupes_by_id():
    from app.materials import tool

    text = "[#1][2024][a] A\n  摘要\n[#2][2025][b] B\n  摘要"
    seen = {1}
    out = tool.filter_duplicate_entries(text, seen)
    assert "[#1]" not in out
    assert "[#2]" in out
    assert seen == {1, 2}


def test_gather_pack_blocks_skips_repeated_entries(db, monkeypatch):
    """同一轮里重复 search 到同一条：第二个块被过滤掉，不重复注入。"""
    from app.materials import tool

    project = _project(db)
    store.add_many(
        db, project.id,
        [{"title": "旧城改造征收流程", "summary": "签约与补偿。"}],
        status="usable",
    )
    monkeypatch.setattr(store, "mark_used", lambda *a, **k: None)

    class _Provider:
        def __init__(self) -> None:
            self.n = 0

        def complete_message(self, messages, *, tools=None, tool_choice=None,
                             budget_seconds=None):
            self.n += 1
            if self.n <= 2:
                return {
                    "content": "",
                    "tool_calls": [{
                        "id": f"c{self.n}",
                        "function": {
                            "name": "material_pack_search",
                            "arguments": json.dumps({"query": "征收"}),
                        },
                    }],
                }
            return {"content": "无", "tool_calls": []}

    blocks = tool.gather_pack_blocks(
        _Provider(), db, project, "你是素材检索助手。", "【目录】…",
        budget_s=10.0, max_rounds=3,
    )
    assert len(blocks) == 1
    assert "[#" in blocks[0]


def test_gather_pack_blocks_passes_budget_as_hard_cap(db):
    """工具轮把剩余预算传给单次调用（硬顶），不是只在轮间检查。"""
    from app.materials import tool

    project = _project(db)
    seen: dict = {}

    class _Provider:
        def complete_message(self, messages, *, tools=None, tool_choice=None,
                             budget_seconds=None):
            seen["budget"] = budget_seconds
            seen["system"] = messages[0]["content"]
            seen["user"] = messages[1]["content"]
            return {"content": "无", "tool_calls": []}

    blocks = tool.gather_pack_blocks(
        _Provider(), db, project, "你是素材检索助手。", "【目录】…",
        budget_s=25.0, max_rounds=2,
    )
    assert blocks == []
    assert 0 < seen["budget"] <= 25.0
    assert "素材检索助手" in seen["system"]
    assert seen["user"] == "【目录】…"


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #

class _FakeRunner:
    provider_name = "fake"

    def __init__(self, *args, **kwargs):
        pass

    def search(self, query: str, *, count: int | None = None):
        return (
            [
                {"title": f"{query} 资料一", "snippet": "2023 年的具体流程说明。", "url": "u1"},
                {"title": f"{query} 资料二", "snippet": "另一条可核实的事实。", "url": "u2"},
                {"title": f"{query} 资料三", "snippet": "第三条可核实的事实。", "url": "u3"},
            ],
            "",
        )


def test_worker_enqueue_dedupes_and_runs(db, monkeypatch):
    project = _project(db, target_length=20000)
    monkeypatch.setattr(worker, "ensure_worker", lambda: None)
    monkeypatch.setattr(worker, "MAX_QUEUE", 100)

    first = worker.enqueue_research(db, project, 1, trigger="manual")
    second = worker.enqueue_research(db, project, 1, trigger="manual")
    assert first is not None and second is not None
    assert first.id == second.id

    job = db.get(MaterialResearchJob, first.id)
    job.queries = ["旧城改造 征收 流程"]
    job.target_raw = 3
    job.target_usable = 1
    db.commit()

    monkeypatch.setattr(research, "SearchRunner", _FakeRunner)
    monkeypatch.setattr(research, "_provider", lambda: _FakeProvider())
    assert worker._run_job(db, job.id) is True

    db.refresh(job)
    assert job.status == "done"
    assert job.usable_count >= 1
    assert store.count_usable(db, project.id) >= 1
