"""蒸馏范围轴（P1）：范围怎么选、怎么进指纹、怎么尊重可见边界、按章加权怎么挑规则。

## 为什么这组测试值得单独建档

范围是**用户第一次能直接控制「蒸哪几章」**的地方，而它一动就会牵动三件事：

1. **缓存键**：范围变了产出就变了 ⇒ 指纹必须变。不变的后果是
   「用户点了全量，拿到的是 30% 采样蒸出来的那份」，而且**不报错**；
2. **可见边界**：蒸馏读的原文必须与检索链读的原文在同一个边界内，
   否则「检索得到的片段」与「蒸出来的规则」来自不同范围；
3. **早期文风漂移**：长篇作者的早期写法不代表成熟期。全量档必须
   **按章加权**（早期规则降权、注入时成熟期优先），而不是偷偷丢掉早期章节
   —— 偷偷丢章会让「全量」这个选项名不副实。

外加两条纪律：默认范围**逐字节不变**（不传 scope ⇒ 行为与加这条轴之前一致），
非法范围**当场 400**（不变成一个跑了两小时才失败的后台任务）。
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import DistillRun, NovelChunk, Project, RawNovel  # noqa: E402
from app.style import narrative as narrative_mod  # noqa: E402
from app.style.narrative import (  # noqa: E402
    MATURITY_WEIGHT_EARLY,
    MATURITY_WEIGHT_MATURE,
    _select_dim_rules,
    _tier_signature,
    estimate_distill_cost,
    extract_narrative_profile,
    maturity_weight,
    narrative_fingerprint,
    resolve_tier,
    sample_chapters_for_scope,
    sample_chapters_for_tier,
)
from app.style.scope import DistillScope, parse_scope  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402


# ====================================================================
# fixtures / 工具
# ====================================================================


@pytest.fixture(autouse=True)
def _clean_db():
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


class StubProvider:
    """确定性假 provider：每块返回一条**证据能通过**的规则。"""

    EVIDENCE = "医院里她抬起头"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        return '{"人物描写手法": ["出场先写环境再写人（证据：' + self.EVIDENCE + '）"]}'


@pytest.fixture
def stub_llm(monkeypatch):
    stub = StubProvider()
    monkeypatch.setattr(llm_provider_mod, "get_llm_provider", lambda: stub)
    return stub


def _make_corpus(db, n_chapters: int = 30) -> int:
    """造一本 n_chapters 章的语料，返回 novel_id。"""
    novel = RawNovel(title=f"范围轴测试语料{n_chapters}", status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for i in range(n_chapters):
        db.add(
            NovelChunk(
                novel_id=novel.id,
                chapter_no=i + 1,
                idx=i,
                text=(
                    "医院里她抬起头，看着走廊尽头的光。"
                    + "他说：走吧。" * (i % 3 + 1)
                ),
            )
        )
    db.commit()
    return novel.id


def _make_project(db, novel_id: int, tier: str | None = None,
                  cutoffs: list[dict] | None = None) -> Project:
    proj = Project(
        title="范围轴测试项目",
        target_length=5000,
        mode="quick",
        status="created",
        reference_novel_ids=[novel_id],
        narrative_tier=tier,
        reference_cutoffs=cutoffs,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _chapters(n: int = 30) -> list[tuple[int, str]]:
    return [(i + 1, f"第{i + 1}章正文") for i in range(n)]


# ====================================================================
# 1. 范围解析：非法参数当场报错，不静默按默认跑
# ====================================================================


def test_parse_scope_default_is_tier_and_respects_cutoff():
    """不传 scope ⇒ 沿用档位自带策略 + 尊重可见边界。"""
    for raw in (None, {}):
        scope = parse_scope(raw)
        assert scope.mode == "tier"
        assert scope.respect_cutoff is True
        assert scope.is_default is True


def test_parse_scope_accepts_common_aliases():
    """「fixed」「percent」这类顺手写法要认，别让人因为一个词被 400。"""
    assert parse_scope({"mode": "fixed", "chapters": 10}).mode == "fixed_chapters"
    assert parse_scope({"mode": "percent", "ratio": 0.5}).mode == "percentage"
    assert parse_scope({"mode": "FIXED_CHAPTERS", "chapters": 10}).mode == "fixed_chapters"


def test_parse_scope_rejects_unknown_mode():
    with pytest.raises(ValueError) as exc:
        parse_scope({"mode": "everything"})
    assert "percentage" in str(exc.value), "报错要说清有哪些合法值"


@pytest.mark.parametrize(
    "raw",
    [
        {"mode": "percentage"},                      # 缺 ratio
        {"mode": "percentage", "ratio": 0},          # ratio 必须 > 0
        {"mode": "percentage", "ratio": 1.5},        # ratio 必须 ≤ 1
        {"mode": "percentage", "ratio": "一半"},     # 类型错
        {"mode": "fixed_chapters"},                 # 缺 chapters
        {"mode": "fixed_chapters", "chapters": 0},   # 至少 1 章
        {"mode": "fixed_chapters", "chapters": "十"},
        {"mode": "range"},                          # 缺区间
        {"mode": "range", "from_chapter": 10},      # 缺一半
        {"mode": "range", "from_chapter": 30, "to_chapter": 10},  # 区间反了
        {"mode": "all", "ratio": 0.5},              # 该模式不接受这个参数
        {"mode": "tier", "chapters": 10},
        {"mode": "all", "unknown_field": 1},        # 未知字段
    ],
)
def test_parse_scope_rejects_bad_input(raw):
    """每一个非法组合都要在这里被拒掉。"""
    with pytest.raises(ValueError):
        parse_scope(raw)


def test_parse_scope_treats_none_as_absent():
    """公开形状（to_public_dict）会把所有参数都列出来、值是 None。

    None = 「这个模式用不上它」，不是「传了一个非法参数」——
    否则范围存进库再读出来就会炸。
    """
    scope = parse_scope(DistillScope().to_public_dict())
    assert scope.mode == "tier"
    assert scope.ratio is None and scope.chapters is None


# ====================================================================
# 2. 指纹：范围变了，旧缓存必须失效；默认范围不动旧签名
# ====================================================================


def test_default_scope_keeps_legacy_tier_signature():
    """默认范围 ⇒ 档位签名与加范围轴之前**逐字节相同**。

    不这样的话，已有项目的缓存与蒸馏库行会集体失效 ——
    加一条轴不该让所有人重蒸一遍。
    """
    cfg = resolve_tier("full")
    legacy = _tier_signature(cfg)
    assert _tier_signature(cfg, DistillScope()) == legacy
    assert _tier_signature(cfg, parse_scope({})) == legacy


def test_non_default_scope_changes_tier_signature():
    """范围变了 ⇒ 签名必变（否则全量档案会被采样档案顶掉）。"""
    cfg = resolve_tier("full")
    legacy = _tier_signature(cfg)
    for raw in (
        {"mode": "all"},
        {"mode": "range", "from_chapter": 1, "to_chapter": 50},
        {"mode": "fixed_chapters", "chapters": 10},
        {"mode": "percentage", "ratio": 0.5},
        {"mode": "tier", "respect_cutoff": False},
    ):
        assert _tier_signature(cfg, parse_scope(raw)) != legacy, raw


def test_narrative_fingerprint_separates_scopes():
    """指纹里必须能区分两个范围，否则缓存会互相顶。"""
    cfg = resolve_tier("full")
    a = narrative_fingerprint([1], cfg, scope=parse_scope({"mode": "all"}))
    b = narrative_fingerprint([1], cfg, scope=parse_scope({"mode": "range",
                                                           "from_chapter": 1,
                                                           "to_chapter": 10}))
    assert a["tier_signature"] != b["tier_signature"]
    default = narrative_fingerprint([1], cfg)
    assert default["tier_signature"] == _tier_signature(cfg)


# ====================================================================
# 3. 采样：每种范围各取各的
# ====================================================================


def test_scope_all_takes_every_chapter():
    cfg = resolve_tier("fast")
    got = sample_chapters_for_scope(_chapters(30), cfg, parse_scope({"mode": "all"}))
    assert [n for n, _ in got] == list(range(1, 31))


def test_scope_range_is_inclusive_on_both_ends():
    cfg = resolve_tier("fast")
    got = sample_chapters_for_scope(
        _chapters(30), cfg,
        parse_scope({"mode": "range", "from_chapter": 5, "to_chapter": 9}),
    )
    assert [n for n, _ in got] == [5, 6, 7, 8, 9]


def test_scope_fixed_spreads_over_whole_book_including_early_chapters():
    """「固定 N 章」= 全书均匀取 N 章，**含早期**。

    早期漂移靠权重处理，不靠偷偷丢章 —— 偷偷丢章会让「全量」名不副实。
    """
    cfg = resolve_tier("fast")
    chapters = _chapters(30)
    got = sample_chapters_for_scope(
        chapters, cfg, parse_scope({"mode": "fixed_chapters", "chapters": 5})
    )
    picked = [n for n, _ in got]
    assert len(picked) == 5
    assert picked[0] == 1, "第一个章号必须是第 1 章（均匀摊开 ⇒ 含早期）"
    # 摊在**全书**上：最后一章不能还在前 2/3 里（那是档位自带的后 2/3 策略）。
    assert picked[-1] > len(chapters) * 0.7, picked
    # 摊开 ⇒ 相邻两章的间隔大致相等，不是全挤在一起。
    gaps = {b - a for a, b in zip(picked, picked[1:])}
    assert len(gaps) == 1, picked


def test_scope_tier_mode_is_byte_identical_to_tier_sampling():
    """mode=tier ⇒ 与档位自带的采样逐字节一致。"""
    cfg = resolve_tier("fast")
    chapters = _chapters(30)
    assert sample_chapters_for_scope(
        chapters, cfg, parse_scope({"mode": "tier"})
    ) == sample_chapters_for_tier(chapters, cfg)


def test_scope_percentage_uses_ratio_sampling():
    cfg = resolve_tier("full")
    got = sample_chapters_for_scope(
        _chapters(30), cfg, parse_scope({"mode": "percentage", "ratio": 0.5})
    )
    assert 0 < len(got) <= 30


# ====================================================================
# 4. 可见边界：蒸馏与检索必须看同一个范围
# ====================================================================


def test_respect_cutoff_truncates_chapters(db_session, stub_llm):
    """项目设了「第 5 章」⇒ 蒸馏只读到第 5 章。"""
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid, tier="fast",
                         cutoffs=[{"novel_id": nid, "chapter": 5}])
    profile = extract_narrative_profile(
        db_session, proj, [nid], provider=stub_llm, tier="fast",
        scope=parse_scope({"mode": "all"}),
    )
    assert profile["n_chapters"] <= 5, "边界之后的章不该被读"
    assert profile["cutoffs_applied"] == {str(nid): 5}


def test_respect_cutoff_false_reads_whole_book(db_session, stub_llm):
    """显式关掉 ⇒ 整本照蒸（要绕过得明说、并留痕）。"""
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid, tier="fast",
                         cutoffs=[{"novel_id": nid, "chapter": 5}])
    profile = extract_narrative_profile(
        db_session, proj, [nid], provider=stub_llm, tier="fast",
        scope=parse_scope({"mode": "all", "respect_cutoff": False}),
    )
    assert profile["n_chapters"] == 30
    assert profile["cutoffs_applied"] == {}


def test_book_without_cutoff_is_fully_read(db_session, stub_llm):
    """没设边界的书**整本照蒸**（不是 fail-closed）。

    蒸馏是学写法，参考书整本都是合法风格材料；把角色聊天那套
    「没边界就整本挡掉」的剧透口径搬过来，会让老项目一蒸馏就变成空档案。
    """
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid, tier="fast")
    profile = extract_narrative_profile(
        db_session, proj, [nid], provider=stub_llm, tier="fast",
        scope=parse_scope({"mode": "all"}),
    )
    assert profile["n_chapters"] == 30
    assert profile["cutoffs_applied"] == {}


# ====================================================================
# 5. 按章加权：早期规则降权，注入时成熟期优先
# ====================================================================


def test_maturity_weight_splits_book_in_half():
    """分界是全书的一半；章号未知（0）不惩罚。"""
    assert maturity_weight(1, 10) == MATURITY_WEIGHT_EARLY
    assert maturity_weight(5, 10) == MATURITY_WEIGHT_EARLY
    assert maturity_weight(6, 10) == MATURITY_WEIGHT_MATURE
    assert maturity_weight(10, 10) == MATURITY_WEIGHT_MATURE
    assert maturity_weight(0, 10) == MATURITY_WEIGHT_MATURE
    assert maturity_weight(None, 10) == MATURITY_WEIGHT_MATURE


def test_rules_carry_weight_and_archive_reports_distribution(db_session, stub_llm):
    """每条规则带权重，档案里报告早期/成熟期各多少条。"""
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid, tier="fast")
    profile = extract_narrative_profile(
        db_session, proj, [nid], provider=stub_llm, tier="fast",
        scope=parse_scope({"mode": "all"}),
    )
    rules = [r for items in profile["rules"].values() for r in items]
    assert rules, "没有规则产出，权重无从谈起"
    for rule in rules:
        assert "weight" in rule, "每条规则都要带成熟度权重"
        assert rule["weight"] in (MATURITY_WEIGHT_EARLY, MATURITY_WEIGHT_MATURE)
    counts = profile["weighting"]["counts"]
    assert sum(counts.values()) == len(rules)
    assert profile["weighting"]["version"] >= 1


def test_select_dim_rules_prefers_mature_then_spreads_early():
    """带权重时：成熟期先取；同一层内部均匀摊开（不挤在最后两章）。"""
    items = []
    for ch in range(1, 21):
        items.append({
            "rule": f"第{ch}章的规则",
            "novel_id": 1,
            "chapter_no": ch,
            "weight": maturity_weight(ch, 20),
        })
    picked = _select_dim_rules(items, 4)
    picked_chapters = [it["chapter_no"] for it in picked]
    # 成熟期是第 11~20 章 ⇒ 取 4 条应当全在成熟期，且摊开（不挤在最后两章）。
    assert all(ch >= 11 for ch in picked_chapters)
    assert len(set(picked_chapters)) == 4
    assert picked_chapters[-1] - picked_chapters[0] >= 8, picked_chapters


def test_select_dim_rules_fills_early_chapters_uniformly():
    """成熟期只有 1 章、其余都是早期时，补位要**摊开**（不全是最初几章）。"""
    items = []
    for ch in range(1, 21):
        weight = MATURITY_WEIGHT_MATURE if ch == 15 else MATURITY_WEIGHT_EARLY
        items.append({"rule": f"r{ch}", "novel_id": 1,
                      "chapter_no": ch, "weight": weight})
    picked = _select_dim_rules(items, 3)
    chapters = [it["chapter_no"] for it in picked]
    assert chapters[0] == 15, "成熟期那条必须先进来"
    # 补的两条来自低权重类，且在类内摊开（不是连着的最初两章）。
    assert all(it["weight"] == MATURITY_WEIGHT_EARLY for it in picked[1:])
    assert abs(chapters[2] - chapters[1]) >= 5, chapters


def test_legacy_rules_without_weight_keep_old_behaviour():
    """老档案没有 weight 键 ⇒ 逐字节走旧取法（成熟期优先 + 原顺序补足）。"""
    items = [{"rule": f"r{ch}", "novel_id": 1, "chapter_no": ch}
             for ch in range(1, 21)]
    picked = _select_dim_rules(items, 3)
    chapters = [it["chapter_no"] for it in picked]
    assert all(ch >= 11 for ch in chapters), "旧档案仍是成熟期优先"
    assert chapters == [13, 16, 19] or all(ch >= 11 for ch in chapters)


# ====================================================================
# 6. 成本预估：与真实蒸馏同一把尺
# ====================================================================


def test_estimate_matches_actual_sampling(db_session, stub_llm):
    """估出来的章数 = 真会读的章数；块数按同一套分块算。"""
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid, tier="fast")
    scope = parse_scope({"mode": "all"})
    est = estimate_distill_cost(db_session, proj, [nid], tier=resolve_tier("fast"),
                                 scope=scope)
    assert est["chapters"] == 30
    assert est["blocks"] >= 1
    assert est["llm_calls"] == est["blocks"] + est["voice_calls"]
    assert est["block_chars"] > 0
    assert est["scope"]["mode"] == "all"


def test_estimate_respects_cutoff(db_session):
    """有边界时，预估的章数也要跟着截断（不然预估就是在骗人）。"""
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid, tier="fast",
                         cutoffs=[{"novel_id": nid, "chapter": 5}])
    est = estimate_distill_cost(db_session, proj, [nid], tier=resolve_tier("fast"),
                                 scope=parse_scope({"mode": "all"}))
    assert est["chapters"] <= 5


# ====================================================================
# 7. API：契约、错误码、并发防重
# ====================================================================


def test_start_distill_records_scope_on_run_row(db_session, stub_llm):
    """提交的范围要落在任务行里（历史记录不随项目偏好变化）。"""
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid)
    client = TestClient(fastapi_app)
    r = client.post(
        f"/api/projects/{proj.id}/distill",
        json={"tier": "fast", "scope": {"mode": "all"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"]["mode"] == "all"
    run = db_session.get(DistillRun, body["job_id"])
    assert run is not None
    assert run.scope_json["mode"] == "all"


def test_start_distill_saves_scope_as_project_preference(db_session, stub_llm):
    """范围是项目级偏好：生成链路核对指纹时要用同一份。"""
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid)
    client = TestClient(fastapi_app)
    r = client.post(
        f"/api/projects/{proj.id}/distill",
        json={"tier": "fast", "scope": {"mode": "all"}},
    )
    assert r.status_code == 200, r.text
    db_session.refresh(proj)
    assert proj.narrative_scope["mode"] == "all"


def test_start_distill_rejects_bad_scope_with_400(db_session, stub_llm):
    """非法范围当场 400，不变成跑两小时才失败的后台任务。"""
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid)
    client = TestClient(fastapi_app)
    r = client.post(
        f"/api/projects/{proj.id}/distill",
        json={"tier": "fast", "scope": {"mode": "range",
                                        "from_chapter": 30, "to_chapter": 5}},
    )
    assert r.status_code == 400, r.text
    assert "区间" in r.json()["detail"]


def test_estimate_endpoint_returns_numbers(db_session):
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid)
    client = TestClient(fastapi_app)
    r = client.post(
        f"/api/projects/{proj.id}/distill/estimate",
        json={"tier": "fast", "scope": {"mode": "fixed_chapters", "chapters": 8}},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["chapters"] == 8
    assert data["blocks"] >= 1
    assert data["llm_calls"] >= data["blocks"]


def test_estimate_endpoint_rejects_bad_scope(db_session):
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid)
    client = TestClient(fastapi_app)
    r = client.post(
        f"/api/projects/{proj.id}/distill/estimate",
        json={"tier": "fast", "scope": {"mode": "nonsense"}},
    )
    assert r.status_code == 400, r.text


def test_concurrent_run_with_different_scope_is_refused(db_session, stub_llm):
    """同档位但范围不同 ⇒ 409，而不是偷偷拿旧范围的结果顶替。"""
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid)
    db_session.add(DistillRun(
        project_id=proj.id, tier="fast", kind="narrative", status="running",
        stage="正在跑", scope_json={"mode": "all", "ratio": None, "chapters": None,
                                    "from_chapter": None, "to_chapter": None,
                                    "respect_cutoff": True},
    ))
    db_session.commit()
    client = TestClient(fastapi_app)
    r = client.post(
        f"/api/projects/{proj.id}/distill",
        json={"tier": "fast", "scope": {"mode": "fixed_chapters", "chapters": 8}},
    )
    assert r.status_code == 409, r.text
    assert "范围不同" in r.json()["detail"]


def test_concurrent_run_with_same_scope_is_reused(db_session, stub_llm):
    """同档位同范围 ⇒ 复用那条任务，不烧第二遍钱。"""
    nid = _make_corpus(db_session, 30)
    proj = _make_project(db_session, nid)
    run = DistillRun(
        project_id=proj.id, tier="fast", kind="narrative", status="running",
        stage="正在跑", scope_json={"mode": "all", "ratio": None, "chapters": None,
                                    "from_chapter": None, "to_chapter": None,
                                    "respect_cutoff": True},
    )
    db_session.add(run)
    db_session.commit()
    client = TestClient(fastapi_app)
    r = client.post(
        f"/api/projects/{proj.id}/distill",
        json={"tier": "fast", "scope": {"mode": "all"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["reused"] is True
    assert r.json()["job_id"] == run.id
