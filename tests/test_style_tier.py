"""文风蒸馏三档机制回归：tests/test_style_tier.py

## 要防的 bug（本项目今天已经踩过两次的同类形态）

**缓存键不区分变量 → 静默返回旧结果。** 具体到档位就是：

    用户在完整档（最贵的一档）跑完之后切到快速档 → narrative_fingerprint
    只按 source_ids + narrative_version 算，没变→ project_narrative 认为
    缓存有效 → 直接把完整档的旧产出返回给用户。

用户「选了快速档」，拿到的还是完整档结果，**全程零报错**。

所以本文件最重要的断言是 `test_fingerprint_differs_by_tier` 及其下游
`test_project_narrative_recomputes_when_tier_changes`：tier 必须在指纹里。

## 其余覆盖

- 三档的采样方式 / 维度数 / 清洗强度**真的不同**（不是只改了标签）
- 不传 tier 时行为与完整档逐项一致（默认档不能破坏已验证过的产出）
- 快速档的采样章数与 LLM 调用量显著低于完整档（mock 计数，不真调 LLM）
- 未知档位必须报错而不是静默回落到默认档
- 落库列存在 + 迁移清单里有它（护栏 tests/test_schema_migration_completeness.py）
- **写入路径的档位校验**（前端工程师 frontend-tier-selector 实测发现的缺口：
  PATCH /api/projects/{id} 不校验 narrative_tier，任意字符串 200 落库）
"""
from __future__ import annotations

import os
import re
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import KgNode, NovelChunk, Project, RawNovel  # noqa: E402
from app.style.narrative import (  # noqa: E402
    DEFAULT_TIER,
    NARRATIVE_DIMENSIONS,
    SAMPLE_RATIO,
    TIER_BALANCED,
    TIER_FAST,
    TIER_FULL,
    TIER_REGISTRY,
    NarrativeTier,
    _build_character_name_index,
    extract_narrative_profile,
    list_tiers,
    narrative_fingerprint,
    project_narrative,
    resolve_tier,
    sample_chapters_for_tier,
)


# ====================================================================
# fixtures / 工具
# ====================================================================


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


class CountingProvider:
    """数调用次数的假 provider——绝不在测试里真调 LLM。

    返回的 JSON 带**合法维度键**但证据刻意不通过（原文里根本没有那句话），
    让所有规则都被证据验证拦掉（rejected 路径）。我们要量的是**调用次数**，
    不是规则内容。维度键必须合法：失败块护栏（2026-10-16 P1-2 修复）会把
    「不含任何预期维度键」的返回当成不可用输出计失败——用无关键 JSON 会让
    整组测试被那道护栏误伤。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[tuple[str, str]] = []

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        self.prompts.append((system, user))
        return '{"人物描写手法": ["占位规则（证据：原文里根本没有这句话）"]}'


def _make_corpus(db, n_chapters: int = 120) -> int:
    """造一本 n_chapters 章的语料，返回 novel_id。"""
    novel = RawNovel(title=f"档位测试语料{n_chapters}", status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for i in range(n_chapters):
        db.add(NovelChunk(
            novel_id=novel.id, chapter_no=i + 1, idx=i,
            text=("医院里她抬起头，看着走廊尽头的光。".format(i=i)
                  + "他说：走吧。" * (i % 3 + 1)
                  + "安小落沉默了很久，才低声开口。"),
        ))
    db.commit()
    return novel.id


def _make_project(db, novel_id: int, tier: str | None = None) -> Project:
    proj = Project(
        title="档位测试项目", target_length=5000, mode="quick", status="created",
        reference_novel_ids=[novel_id], narrative_tier=tier,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


# ====================================================================
# 1. 档位注册表：三档真的不同（不是只改了标签）
# ====================================================================


def test_three_tiers_registered_with_readable_names():
    assert set(TIER_REGISTRY) == {TIER_FULL, TIER_BALANCED, TIER_FAST}
    for key, tier in TIER_REGISTRY.items():
        assert tier.key == key
        assert tier.name, f"{key} 缺可读名字（前端要显示）"
        assert tier.description, f"{key} 缺一句话说明（前端要显示）"

    names = [t.name for t in TIER_REGISTRY.values()]
    assert len(set(names)) == 3, f"三档名字必须互不相同：{names}"


def test_sampling_axis_really_differs():
    """采样轴：第一档百分比 0.15，后两档固定 30 章。"""
    full = TIER_REGISTRY[TIER_FULL]
    balanced = TIER_REGISTRY[TIER_BALANCED]
    fast = TIER_REGISTRY[TIER_FAST]

    assert full.sampling_mode == "percentage"
    assert full.sample_ratio == SAMPLE_RATIO == 0.15
    assert full.fixed_chapters is None

    for t in (balanced, fast):
        assert t.sampling_mode == "fixed_chapters"
        assert t.fixed_chapters == 30
        assert t.sample_ratio is None

    # 采样方式真的产出不同的章数（用同一份 200 章语料）
    chapters = [(i + 1, f"第{i + 1}章内容") for i in range(200)]
    n_full = len(sample_chapters_for_tier(chapters, full))
    n_balanced = len(sample_chapters_for_tier(chapters, balanced))
    n_fast = len(sample_chapters_for_tier(chapters, fast))

    assert n_full == 30# 200 × 0.15
    assert n_balanced == 30
    assert n_fast == 30
    # 章数在这里恰好相同（同为 30），但**方式**不同：把语料放大到 600 章才拉开
    chapters_big = [(i + 1, f"第{i + 1}章内容") for i in range(600)]
    assert len(sample_chapters_for_tier(chapters_big, full)) == 90   # 600 × 15%
    assert len(sample_chapters_for_tier(chapters_big, balanced)) == 30  # 固定 30
    assert len(sample_chapters_for_tier(chapters_big, fast)) == 30


def test_dimension_axis_really_differs():
    """维度轴：完整/平衡全维度（14），快速档是 11 的真子集。"""
    full = TIER_REGISTRY[TIER_FULL]
    balanced = TIER_REGISTRY[TIER_BALANCED]
    fast = TIER_REGISTRY[TIER_FAST]

    assert full.dimension_count == 14
    assert balanced.dimension_count == 14
    assert fast.dimension_count == 11, (
        f"快速档维度数应为 11（14 − 3 个 v5 增量维度），实际 {fast.dimension_count}"
    )

    # 快速档必须是**真子集**，不是重排
    assert set(fast.dimensions) < set(full.dimensions)
    dropped = set(full.dimensions) - set(fast.dimensions)
    assert dropped == {"节拍序列", "高频场景模板", "潜台词模式"}, dropped


def test_cleaning_and_normalization_axes_differ():
    """归一化 / 清洗强度轴：三档三档不同，且第一档最重。"""
    full = TIER_REGISTRY[TIER_FULL]
    balanced = TIER_REGISTRY[TIER_BALANCED]
    fast = TIER_REGISTRY[TIER_FAST]

    assert (full.normalize_character_names, balanced.normalize_character_names,
            fast.normalize_character_names) == (True, False, False)
    assert (full.fake_character_cleaning, balanced.fake_character_cleaning,
            fast.fake_character_cleaning) == ("full", "partial", "none")
    assert full.voice_mode == "targeted"
    assert balanced.voice_mode == "targeted"
    assert fast.voice_mode == "single_batch"


# ====================================================================
# 2.【重点】指纹必须含 tier
# ====================================================================


def test_fingerprint_differs_by_tier():
    """同一组 source_ids，tier 不同时指纹必须不同。

    这是整个档位机制的命门：指纹不含 tier → project_narrative 会把上一档的
    缓存当成命中返回，用户「选了快速档」却拿到完整档产出，且零报错。
    """
    ids = [1, 2]
    fps = {k: narrative_fingerprint(ids, k) for k in (TIER_FULL, TIER_BALANCED, TIER_FAST)}

    # 指纹里必须**显式带** tier key（不是靠别的字段间接区分）
    for key, fp in fps.items():
        assert fp.get("tier") == key, f"{key} 的指纹里没有 tier 字段：{fp}"

    # 三档两两不同
    assert fps[TIER_FULL] != fps[TIER_BALANCED]
    assert fps[TIER_FULL] != fps[TIER_FAST]
    assert fps[TIER_BALANCED] != fps[TIER_FAST]

    # tier_signature 也要跟着变（改了档位配置也要能失效缓存）
    sigs = {fp["tier_signature"] for fp in fps.values()}
    assert len(sigs) == 3, f"三档的 tier_signature 应互不相同：{sigs}"


def test_fingerprint_default_equals_full_tier():
    """不传 tier 的指纹 == 显式传 full 的指纹（默认档不能改老行为）。"""
    assert narrative_fingerprint([1]) == narrative_fingerprint([1], TIER_FULL)
    assert narrative_fingerprint([1]) == narrative_fingerprint([1], None)
    assert resolve_tier(None).key == DEFAULT_TIER == TIER_FULL


def test_fingerprint_still_normalizes_source_id_order():
    """旧语义不许丢：source_ids 顺序不同仍归一。"""
    assert narrative_fingerprint([1, 2]) == narrative_fingerprint([2, 1])


def test_project_narrative_recomputes_when_tier_changes():
    """端到端：切档必须触发重算，不能返回上一档缓存。

    变异检查的目标测试。把 narrative_fingerprint 里的 tier 字段去掉，
    本测试会转红（第二步会拿到完整档的缓存，tier 断言失败）。
    """
    db = SessionLocal()
    try:
        nid = _make_corpus(db, 40)
        proj = _make_project(db, nid)

        provider = CountingProvider()
        full_profile = extract_narrative_profile(
            db, proj, [nid], provider=provider, tier=TIER_FULL
        )
        assert full_profile["tier"] == TIER_FULL

        # 落库为完整档的产出（模拟「用户先前跑过完整档」）
        proj.style_narrative = {
            **full_profile,
            "fingerprint": narrative_fingerprint([nid], TIER_FULL),
        }
        proj.narrative_tier = TIER_FULL
        db.commit()

        # —— 关键：切到快速档，指纹变了，必须重算 ——
        import app.style.narrative as narr

        real_extract = narr.extract_narrative_profile
        provider2 = CountingProvider()

        def _counting_extract(*args, **kwargs):
            kwargs["provider"] = provider2
            return real_extract(*args, **kwargs)

        narr.extract_narrative_profile = _counting_extract
        try:
            result = project_narrative(db, proj, tier=TIER_FAST)
        finally:
            narr.extract_narrative_profile = real_extract

        assert provider2.calls > 0, "切档后没有重新提炼——直接返回了上一档缓存"
        assert result["tier"] == TIER_FAST, (
            f"切到快速档却拿到tier={result.get('tier')} 的产出（静默返回旧结果）"
        )
        assert result["fingerprint"]["tier"] == TIER_FAST
        assert result["fingerprint"] != narrative_fingerprint([nid], TIER_FULL)
    finally:
        db.close()


def test_project_narrative_same_tier_hits_cache():
    """同档位仍然走缓存（档位不能把缓存机制整个废掉）。"""
    db = SessionLocal()
    try:
        nid = _make_corpus(db, 30)
        proj = _make_project(db, nid, tier=TIER_FAST)
        cached = {
            "tier": TIER_FAST,
            "rules": {},
            "fingerprint": narrative_fingerprint([nid], TIER_FAST),
        }
        proj.style_narrative = cached
        db.commit()
        assert project_narrative(db, proj) is not None
        assert project_narrative(db, proj) == cached, "同档位应命中缓存"
    finally:
        db.close()


# ====================================================================
# 3. 不传 tier 时行为与完整档一致
# ====================================================================


def test_default_tier_config_matches_full_tier_exactly():
    """默认档的**配置**与完整档逐项相同（不只是名字叫「完整」）。"""
    default = resolve_tier(None)
    full = TIER_REGISTRY[TIER_FULL]
    assert default.key == full.key
    assert default.sampling_mode == full.sampling_mode
    assert default.sample_ratio == full.sample_ratio
    assert default.fixed_chapters == full.fixed_chapters
    assert default.dimensions == full.dimensions
    assert default.normalize_character_names == full.normalize_character_names
    assert default.fake_character_cleaning == full.fake_character_cleaning
    assert default.voice_mode == full.voice_mode


def test_default_tier_extract_matches_explicit_full_tier():
    """不传 tier 与显式传 full：产出结构与调用次数完全一致。"""
    db = SessionLocal()
    try:
        nid = _make_corpus(db, 60)
        proj = _make_project(db, nid)

        p_default = CountingProvider()
        default_profile = extract_narrative_profile(db, proj, [nid], provider=p_default)
        p_full = CountingProvider()
        full_profile = extract_narrative_profile(
            db, proj, [nid], provider=p_full, tier=TIER_FULL
        )

        assert p_default.calls == p_full.calls
        assert default_profile["n_chapters"] == full_profile["n_chapters"]
        assert set(default_profile["rules"]) == set(full_profile["rules"])
        assert default_profile["tier"] == full_profile["tier"] == TIER_FULL
    finally:
        db.close()


# ====================================================================
# 4. 快速档成本显著低于完整档（mock 计数，不真调 LLM）
# ====================================================================


def test_reduced_tiers_sample_fewer_chapters():
    """采样轴端到端：后两档的采样章数必须少于完整档。

    用 600 章语料（600 × 15% = 90vs 固定 30）——200 章时恰好都是 30，
    看不出差别，那不是「真的不同」。
    """
    db = SessionLocal()
    try:
        nid = _make_corpus(db, 600)
        proj = _make_project(db, nid)

        counts = {}
        for key in (TIER_FULL, TIER_BALANCED, TIER_FAST):
            profile = extract_narrative_profile(
                db, proj, [nid], provider=CountingProvider(), tier=key
            )
            counts[key] = profile["n_chapters"]

        assert counts[TIER_FULL] == 90, counts
        assert counts[TIER_BALANCED] == 30, counts
        assert counts[TIER_FAST] == 30, counts
    finally:
        db.close()


@pytest.mark.parametrize(
    "tier_key", [TIER_BALANCED, TIER_FAST],
    ids=["balanced", "fast"],
)
def test_reduced_tiers_make_fewer_llm_calls(tier_key):
    """采样更少 + 维度更少 → LLM 调用量必须少于完整档。"""
    db = SessionLocal()
    try:
        nid = _make_corpus(db, 600)
        proj = _make_project(db, nid)

        p_full = CountingProvider()
        full_profile = extract_narrative_profile(db, proj, [nid], provider=p_full)

        p_tier = CountingProvider()
        tier_profile = extract_narrative_profile(
            db, proj, [nid], provider=p_tier, tier=tier_key
        )

        assert tier_profile["n_chapters"] < full_profile["n_chapters"], (
            f"{tier_key} 采样章数应少于完整档："
            f"{tier_profile['n_chapters']} vs {full_profile['n_chapters']}"
        )
        assert p_tier.calls < p_full.calls, (
            f"{tier_key} 的 LLM 调用量应少于完整档：{p_tier.calls} vs {p_full.calls}"
        )
        assert tier_profile["tier"] == tier_key
    finally:
        db.close()


def test_fast_tier_calls_are_less_than_half_of_full():
    """快速档的成本必须**显著**低于完整档——这正是它存在的理由。

    完整档是最贵的一档；如果快速档只省一点点，用户就没有理由选它。
    差异来自两处：采样 90→30 章（分块数减少）+ 砍掉 3 个每块都要调的维度。
    """
    db = SessionLocal()
    try:
        nid = _make_corpus(db, 600)
        proj = _make_project(db, nid)

        p_full = CountingProvider()
        extract_narrative_profile(db, proj, [nid], provider=p_full)

        p_fast = CountingProvider()
        extract_narrative_profile(db, proj, [nid], provider=p_fast, tier=TIER_FAST)

        assert p_fast.calls > 0
        assert p_fast.calls < p_full.calls / 2, (
            f"快速档调用量应显著低于完整档（< 一半）：{p_fast.calls} vs {p_full.calls}"
        )
    finally:
        db.close()


def test_fast_tier_skips_the_three_v5_dimensions_in_output():
    """快速档的产出里不许出现被砍掉的三个维度（键都不该有）。"""
    db = SessionLocal()
    try:
        nid = _make_corpus(db, 120)
        proj = _make_project(db, nid)
        profile = extract_narrative_profile(
            db, proj, [nid], provider=CountingProvider(), tier=TIER_FAST
        )
        dropped = {"节拍序列", "高频场景模板", "潜台词模式"}
        assert dropped.isdisjoint(set(profile["rules"])), (
            f"快速档产出里仍有被砍掉的维度：{dropped & set(profile['rules'])}"
        )
        assert set(profile["rules"]) == set(TIER_REGISTRY[TIER_FAST].dimensions) | {"文风漂移分析"}
    finally:
        db.close()


# ====================================================================
# 5. 伪角色清洗强度真的生效
# ====================================================================


def test_fake_character_cleaning_strength_differs_by_tier():
    """清洗强度轴：同一份脏图谱，三档给出的规范名池真的不同。"""
    db = SessionLocal()
    try:
        proj = Project(title="清洗测试", target_length=5000, mode="quick", status="created")
        db.add(proj)
        db.commit()
        db.refresh(proj)

        # 脏图谱：真角色 + 高频虚词 + 分词截断
        dirty = [
            ("安小落", 12000, True), ("南宫爵", 9000, True),
            ("小落", 8000, True),          # 截断名
            ("时间", 5000, True),          # 虚词
            ("相信", 4000, True),          # 虚词
            ("宫爵", 3000, True),          # 截断名
        ]
        for name, mentions, speaking in dirty:
            db.add(KgNode(
                project_id=proj.id, type="character",
                source="novel:1",
                props={"name": name, "mentions": mentions, "speaking": speaking},
            ))
        db.commit()

        corpus = "安小落出现了很多次。安小落沉默。安小落走了。南宫爵也来了，南宫爵点头。"

        full_names, full_alias = _build_character_name_index(
            db, proj, 1, corpus, cleaning="full"
        )
        partial_names, partial_alias = _build_character_name_index(
            db, proj, 1, corpus, cleaning="partial"
        )
        raw_names, raw_alias = _build_character_name_index(
            db, proj, 1, corpus, cleaning="none"
        )

        full_set = {n for n, _ in full_names}
        partial_set = {n for n, _ in partial_names}
        raw_set = {n for n, _ in raw_names}

        # 完整档：虚词被黑名单清掉 + 截断名归一到全名
        assert "时间" not in full_set and "相信" not in full_set
        assert "小落" not in full_set, "完整档应把「小落」归一到「安小落」"
        assert "安小落" in full_set
        assert full_alias.get("小落") == "安小落"

        # 平衡档：虚词仍被清掉，但不做频率自证（截断名原样保留）
        assert "时间" not in partial_set and "相信" not in partial_set
        assert "小落" in partial_set, "平衡档不做频率自证，截断名应原样保留"
        assert partial_alias == {}

        # 快速档：完全不清洗
        assert "时间" in raw_set and "相信" in raw_set
        assert raw_alias == {}
    finally:
        db.close()


# ====================================================================
# 6. 未知档位必须报错（不静默回落）
# ====================================================================


def test_unknown_tier_raises_instead_of_silently_defaulting():
    """选了不存在的档位必须报错，不能悄悄跑成默认档。

    「静默回落默认档」和「静默返回上一档缓存」是同一族 bug：用户以为自己
    选到了 X，实际得到的是 Y，且没有任何提示。
    """
    with pytest.raises(ValueError, match="未知蒸馏档位"):
        resolve_tier("turbo")
    with pytest.raises(ValueError):
        narrative_fingerprint([1], "turbo")


def test_list_tiers_is_ordered_and_complete():
    """前端拿这份列表渲染选档：顺序固定、三档齐全、字段够用。"""
    tiers = list_tiers()
    assert [t["key"] for t in tiers] == [TIER_FULL, TIER_BALANCED, TIER_FAST]
    for t in tiers:
        assert t["name"] and t["description"]
        assert t["sampling_mode"] in {"percentage", "fixed_chapters"}
        assert isinstance(t["dimensions"], list) and t["dimensions"]
        assert t["dimension_count"] == len(t["dimensions"])


def test_list_tiers_exposes_relative_cost_hint():
    """耗时提示必须是**相对档位**，三档都不许出现具体分钟数。

    ## 为什么连完整档也禁

    docs/style-distillation-tiers.md 全文只有**一处**计时（第 81 行）：
    语料 2《 大奉打更人 》833 章 → 采样 764 章 → 42 分钟。语料 1（603 章）
    那段**没有任何计时**，「64 分钟」在 docs 里零命中。

    曾把完整档写成「实测 603 章语料约 64 分钟」——那是把 brief 里的二手分钟数
    配上语料 1 的章数拼出来的组合，仓库里查不到出处，而且 603 章 64 分钟
    比 833 章 42 分钟还慢，与 docs 唯一的计时**直接矛盾**。

    界面上的耗时提示是对用户的时间承诺，不能挂在一个自相矛盾、且无出处的
    数字上。要加分钟数，必须先在 docs 里补一行带语料名+ 章数的计时记录。
    """
    hints = {t["key"]: t["cost_hint"] for t in list_tiers()}
    assert set(hints) == {TIER_FULL, TIER_BALANCED, TIER_FAST}
    assert all(hints.values()), "每档都要有耗时提示"
    # 三档一律不许出现「N 分钟」——包括完整档。
    for key, hint in hints.items():
        assert not re.search(r"\d+\s*分钟", hint), (
            f"{key} 的耗时提示里有具体分钟数，但仓库里没有带出处的实测记录：{hint}。"
            "要加分钟数，先在 docs 里补一行「语料 X / N 章 / 实测 M 分钟」。"
        )
    # 相对档位词要留着——这是从配置事实推导的，不会撒谎
    assert "最高" in hints[TIER_FULL]
    assert "最低" in hints[TIER_FAST]


def test_no_unverifiable_timing_numbers_in_tier_text():
    """三档的对外文案里不得出现 docs 无出处的计时数字。

    比上一条更宽：连「64 分钟」这种**没有数字紧跟「分钟」**的写法也一并挡住
    （比如「一小时量级」这种含糊表述同样没有实测支撑）。
    具体做法：docs 里出现过的计时只有一个 42 分钟（语料 2 / 833 章），
    因此三档文案里不该出现任何其它分钟数。
    """
    docs_text = open(
        os.path.join(os.path.dirname(__file__), "..", "docs", "style-distillation-tiers.md"),
        encoding="utf-8",
    ).read()
    # docs 里真实存在的分钟数（只有这一个）
    doc_minutes = set(re.findall(r"(\d+)\s*分钟", docs_text))

    for t in list_tiers():
        for field in ("cost_hint", "description", "name"):
            text = t[field]
            for minutes in re.findall(r"(\d+)\s*分钟", text):
                assert minutes in doc_minutes, (
                    f"档位 {t['key']} 的 {field} 出现「{minutes} 分钟」，"
                    f"但 docs 里查不到这个计时（docs 实测仅有 {sorted(doc_minutes)}）"
                )
            # 章数 + 分钟数的组合最危险：看起来最像实测，最容易是拼出来的
            assert not re.search(r"\d+\s*章[^。；)）]*\d+\s*分钟", text), (
                f"档位 {t['key']} 的 {field} 同时出现章数和分钟数，"
                f"疑似把不同来源的数字拼在一起：{text}"
            )


def test_cost_hint_key_must_exist_for_every_tier():
    """新增档位时忘了写 _COST_HINTS，必须响亮地失败而不是静默给错提示。"""
    from app.style import narrative as narr

    assert set(narr._COST_HINTS) == set(narr.TIER_REGISTRY), (
        "新增档位后 _COST_HINTS 必须同步补一项，否则 to_public_dict 会 KeyError"
    )


# ====================================================================
# 7. 落库：列存在 + 迁移清单里有
# ====================================================================


def test_narrative_tier_column_exists_on_project():
    """Project.narrative_tier 必须真实存在（不是只写在 models 的注释里）。"""
    cols = {c.name for c in Project.__table__.columns}
    assert "narrative_tier" in cols


def test_narrative_tier_in_migration_list():
    """迁移清单护栏：漏加 = 老库 SELECT projects.* 直接炸。"""
    import app.db as db_mod

    src = open(db_mod.__file__, encoding="utf-8").read()
    assert '("projects", "narrative_tier"' in src, (
        "db.py 的 migrations 清单里没有 projects.narrative_tier —— "
        "已存在的库升级后 SELECT 会 no such column"
    )


def test_narrative_tier_round_trips_through_db():
    """档位能存进去也能读回来（用户选档是项目级偏好）。"""
    db = SessionLocal()
    try:
        proj = Project(title="落库测试", target_length=5000, mode="quick",
                       status="created", narrative_tier=TIER_FAST)
        db.add(proj)
        db.commit()
        db.refresh(proj)
        assert proj.narrative_tier == TIER_FAST

        db.expire_all()
        reread = db.get(Project, proj.id)
        assert reread.narrative_tier == TIER_FAST
    finally:
        db.close()


def test_project_narrative_reads_tier_from_project_column():
    """项目上存的档位是项目级偏好：不显式传 tier 时也要生效。"""
    db = SessionLocal()
    try:
        nid = _make_corpus(db, 30)
        proj = _make_project(db, nid, tier=TIER_FAST)

        import app.style.narrative as narr

        real_extract = narr.extract_narrative_profile
        seen: dict = {}

        def _spy(*args, **kwargs):
            seen["tier"] = kwargs.get("tier")
            kwargs["provider"] = CountingProvider()
            return real_extract(*args, **kwargs)

        narr.extract_narrative_profile = _spy
        try:
            project_narrative(db, proj)
        finally:
            narr.extract_narrative_profile = real_extract

        assert seen.get("tier") is not None
        assert seen["tier"].key == TIER_FAST, (
            f"项目上存的是 {TIER_FAST}，实际用的却是 {seen['tier']}"
        )
    finally:
        db.close()


# ====================================================================
# 8. 向后兼容：老档案（无 tier 字段）不该被当成命中
# ====================================================================


def test_legacy_profile_without_tier_field_is_not_a_cache_hit():
    """v7 时代落的老档案指纹里没有 tier → 换档后不该被复用。

    反面：老指纹恰好等于新默认档指纹的话，切到快速档也会被误判成命中。
    """
    db = SessionLocal()
    try:
        nid = _make_corpus(db, 30)
        proj = _make_project(db, nid)
        proj.style_narrative = {
            "rules": {},
            # 模拟老档案：只有 source_ids + narrative_version
            "fingerprint": {
                "source_ids": [nid],
                "narrative_version": 7,
            },
        }
        db.commit()

        import app.style.narrative as narr

        real_extract = narr.extract_narrative_profile
        provider = CountingProvider()

        def _spy(*args, **kwargs):
            kwargs["provider"] = provider
            return real_extract(*args, **kwargs)

        narr.extract_narrative_profile = _spy
        try:
            result = project_narrative(db, proj, tier=TIER_FAST)
        finally:
            narr.extract_narrative_profile = real_extract

        assert provider.calls > 0, "老档案被当成命中缓存了（指纹不该兼容无 tier 字段）"
        assert result["fingerprint"]["tier"] == TIER_FAST
    finally:
        db.close()


def test_narrative_dimensions_unchanged_by_tier_work():
    """档位机制不许改动全局维度清单（全维度档依赖它仍是 14）。"""
    assert len(NARRATIVE_DIMENSIONS) == 14
    assert TIER_REGISTRY[TIER_FULL].dimensions == tuple(NARRATIVE_DIMENSIONS)
    assert TIER_REGISTRY[TIER_BALANCED].dimensions == tuple(NARRATIVE_DIMENSIONS)
    assert len(NARRATIVE_DIMENSIONS) == len(TIER_REGISTRY[TIER_FULL].dimensions)


# ====================================================================
# 9. 写入路径校验（PATCH /api/projects/{id}）
# ====================================================================
#
# 缺口来源：前端工程师 frontend-tier-selector 在 fa410d0 实测发现——
# `PATCH /api/projects/{id}` 逐字段 setattr，不校验 narrative_tier，
# 任意字符串都 200 并落库；非法值要等到之后 build-narrative 才 400，
# 那时已经无从追溯是谁写进去的。
#
# 漏的地方恰好是**唯一会写入非法值的入口**：build-narrative 那条路已经守住了。


@pytest.fixture
def api_client():
    """TestClient（走真实路由 + 真实校验层，不 mock 掉校验）。"""
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as client:
        yield client


@pytest.mark.parametrize("tier", [TIER_FULL, TIER_BALANCED, TIER_FAST])
def test_patch_accepts_valid_tiers(api_client, tier):
    """合法档位照常写入并回显。"""
    resp = api_client.post("/api/projects", json={
        "title": "档位校验测试", "target_length": 5000, "mode": "quick",
    })
    assert resp.status_code == 200, resp.text
    pid = resp.json()["id"]

    resp = api_client.patch(f"/api/projects/{pid}", json={"narrative_tier": tier})
    assert resp.status_code == 200, resp.text
    assert resp.json()["narrative_tier"] == tier


def test_patch_rejects_invalid_tier_with_400(api_client):
    """核心回归：非法档位必须 400，且**不能落库**。

    变异检查目标：把 patch_project 里的 resolve_tier 校验删掉 → 本测试转红。
    """
    resp = api_client.post("/api/projects", json={
        "title": "档位校验测试", "target_length": 5000, "mode": "quick",
    })
    pid = resp.json()["id"]

    resp = api_client.patch(f"/api/projects/{pid}", json={"narrative_tier": "nope"})
    assert resp.status_code == 400, (
        f"非法档位应回400，实际 {resp.status_code}；body={resp.text[:200]}"
    )
    assert "档位" in resp.json().get("detail", ""), resp.text

    # 更要紧的：不能只回400 而把脏值写进库
    reread = api_client.get(f"/api/projects/{pid}").json()
    assert reread["narrative_tier"] is None, (
        f"非法档位被落库了：{reread['narrative_tier']!r}"
    )


def test_patch_allows_clearing_tier_with_null(api_client):
    """null 表示「回到未选= 默认完整档」，必须允许（否则用户无法清空偏好）。"""
    resp = api_client.post("/api/projects", json={
        "title": "档位校验测试", "target_length": 5000, "mode": "quick",
    })
    pid = resp.json()["id"]

    api_client.patch(f"/api/projects/{pid}", json={"narrative_tier": TIER_FAST})
    resp = api_client.patch(f"/api/projects/{pid}", json={"narrative_tier": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["narrative_tier"] is None


def test_patch_invalid_tier_does_not_leak_other_fields(api_client):
    """档位非法时整条 patch 都不该落库（不部分写入）。"""
    resp = api_client.post("/api/projects", json={
        "title": "原始标题", "target_length": 5000, "mode": "quick",
    })
    pid = resp.json()["id"]

    api_client.patch(
        f"/api/projects/{pid}",
        json={"narrative_tier": "turbo", "genre": "悬疑"},
    )
    reread = api_client.get(f"/api/projects/{pid}").json()
    assert reread["genre"] is None, (
        f"档位非法时不该顺带写入其它字段：genre={reread['genre']!r}"
    )


def test_tiers_endpoint_lists_all_three_over_http(api_client):
    """前端选档依赖这个端点（frontend-tier-selector 的 UI 直接渲染它）。"""
    resp = api_client.get("/api/projects/narrative-tiers")
    assert resp.status_code == 200, (
        f"档位清单端点 404/422？（静态路由被 /{project_id} 抢走了）：{resp.text[:200]}"
    )
    body = resp.json()
    assert body["default"] == TIER_FULL
    assert [t["key"] for t in body["tiers"]] == [TIER_FULL, TIER_BALANCED, TIER_FAST]