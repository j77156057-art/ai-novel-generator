"""逐章推进台账的纯函数测试（不碰数据库、不跑模型）。"""
from __future__ import annotations

from types import SimpleNamespace

from app.generation.progress_ledger import (
    DEFAULTS,
    _open_questions,
    advance_requirements,
    build_progress_ledger,
    chapter_view,
    resolve_config,
)

BIBLE = {
    "characters": [
        {"name": "林川", "role": "主角", "首次出场章节": 1},
        {"name": "苏禾", "role": "同伴", "首次出场章节": 1},
        {"name": "老周", "role": "配角", "首次出场章节": 3},
    ],
    "realm_order": [["炼气", "筑基", "金丹"]],
    "derived_item_names": ["铜牌"],
}


def _chapters(pairs):
    return [{"chapter": i + 1, "text": t} for i, t in enumerate(pairs)]


def _rows_by(ledger, kind):
    return {r["character"]: r for r in ledger["attributes"]["rows"]
            if r["kind"] == kind}


# ─── 阵容 ────────────────────────────────────────────────────────────────


def test_cast_debut_overlap_and_protagonist():
    ledger = build_progress_ledger(
        _chapters([
            "林川和苏禾进了旧库房。\n\n两个人谁也没说话。",
            "林川把铜牌推给苏禾。\n\n苏禾没有接。",
            "林川、苏禾和老周在门口碰头。\n\n老周看了看铜牌。",
        ]),
        plot_bible=BIBLE,
        config={**DEFAULTS, "cast_overlap_high": 0.8},
    )
    cast = ledger["cast"]
    assert cast["protagonist"] == "林川"
    assert cast["debuts"] == {"林川": 1, "苏禾": 1, "老周": 3}
    rows = {r["chapter"]: r for r in cast["rows"]}
    assert rows[2]["overlap_prev"] == 1.0
    assert rows[3]["overlap_prev"] < 0.8
    # 第 1–2 章是唯一一对高度重合的相邻章
    assert cast["longest_overlap_run"]["start"] == 1
    assert cast["longest_overlap_run"]["end"] == 2
    assert cast["longest_overlap_run"]["member_count"] == 2
    # 角色卡声明的登场章与窗口观察不一致时要点出来
    checks = {c["character"]: c for c in cast["debut_checks"]}
    assert checks["老周"]["declared"] == 3
    assert checks["老周"]["status"] == "与角色卡一致"


def test_cast_run_requires_pairwise_core_size():
    """上一章超编不能把一个 2 人区间算成 5 人小圈子。"""
    ledger = build_progress_ledger(
        _chapters([
            "林川、苏禾、老周、甲、乙在屋里。",
            "林川和苏禾在屋里。",
            "林川和苏禾还在屋里。",
        ]),
        plot_bible=BIBLE,
        config={**DEFAULTS, "cast_core_max": 2},
    )
    # 第 2→3 章才是 2 人区间；第 1→2 章被第 1 章的 5 人否掉。
    run = ledger["cast"]["longest_overlap_run"]
    assert run["start"] == 2 and run["end"] == 3


# ─── 属性 ────────────────────────────────────────────────────────────────


def test_realm_baseline_is_not_a_change_and_later_rank_is():
    ledger = build_progress_ledger(
        _chapters([
            "林川的修为突破到炼气。",
            "林川的修为突破到筑基。",
            "林川的修为突破到金丹。",
            "林川去了旧库房。",
        ]),
        plot_bible=BIBLE,
        config=DEFAULTS,
    )
    realm = _rows_by(ledger, "realm")["林川"]
    assert realm["first_seen"] == 1
    assert [c["chapter"] for c in realm["changes"]] == [2, 3]
    assert realm["last_change"] == 3
    assert realm["chapters_since_last_change"] == 1


def test_realm_absent_uses_cast_debut_as_observation_start():
    ledger = build_progress_ledger(
        _chapters(["林川进了屋。", "苏禾也进了屋。", "老周最后进来。"]),
        plot_bible=BIBLE,
        config=DEFAULTS,
    )
    realm = _rows_by(ledger, "realm")["林川"]
    assert realm["status"] == "no_attribute_evidence"
    assert realm["first_seen_basis"] == "cast_debut"
    assert realm["longest_zero_run"] == 3


def test_skill_acquired_once_then_used_is_not_a_new_change():
    ledger = build_progress_ledger(
        _chapters([
            "林川学会了开锁。",
            "林川施展了开锁。",
            "林川掌握了开锁。",
            "林川学会了记账。",
        ]),
        plot_bible=BIBLE,
        config=DEFAULTS,
    )
    skill = _rows_by(ledger, "skill")["林川"]
    assert [c["chapter"] for c in skill["changes"]] == [1, 4]
    assert skill["change_count"] == 2
    # 使用事件单独记账，不算变化点
    assert ledger["attributes"]["skill_uses"]["林川"][0]["skill"] == "开锁"


def test_item_gain_transfer_and_loss_are_changes():
    ledger = build_progress_ledger(
        _chapters([
            "林川拿到了铜牌。",
            "林川把铜牌交给苏禾。",
            "苏禾把铜牌弄丢了。",
        ]),
        plot_bible=BIBLE,
        config=DEFAULTS,
    )
    item = _rows_by(ledger, "item")
    assert [c["chapter"] for c in item["林川"]["changes"]] == [1]
    assert [c["chapter"] for c in item["苏禾"]["changes"]] == [2, 3]
    state = ledger["attributes"]["item_states"]["铜牌"]
    assert state["first_seen"] == 1
    assert [c["change"] for c in state["changes"]] == ["gain", "transfer", "loss"]


def test_item_noise_is_rejected():
    ledger = build_progress_ledger(
        _chapters([
            "林川拿到了什么。\n\n后来人把铜牌弄丢了。\n\n他收到了同型号药盒的提醒。",
        ]),
        plot_bible=BIBLE,
        config=DEFAULTS,
    )
    item = _rows_by(ledger, "item")["林川"]
    values = [v for c in item["changes"] for v in c["values"]]
    assert "什么" not in values
    assert "后来人" not in values
    assert "同型号药盒的提醒" not in values


def test_nonperson_subject_is_not_attributed_to_speaker():
    """`平台拿到名单` 是平台拿的，不能因为苏禾在说话就记到他头上。"""
    ledger = build_progress_ledger(
        _chapters([
            "苏禾说：“平台拿到名单以后，收件地址都没改。”",
        ]),
        plot_bible=BIBLE,
        config=DEFAULTS,
    )
    item = _rows_by(ledger, "item")["苏禾"]
    assert item["changes"] == []
    assert any(u["basis"] == "nonperson_subject"
               for u in ledger["attributes"]["unattributed"]["item"])


def test_repeated_gain_of_same_item_is_deduped():
    ledger = build_progress_ledger(
        _chapters([
            "林川拿到了铜牌。",
            "林川又拿到了铜牌。",
        ]),
        plot_bible=BIBLE,
        config=DEFAULTS,
    )
    changes = _rows_by(ledger, "item")["林川"]["changes"]
    assert [c["chapter"] for c in changes] == [1]
    gains = [c for c in ledger["attributes"]["item_states"]["铜牌"]["changes"]
             if c["change"] == "gain"]
    assert len(gains) == 1


# ─── 衔接 ────────────────────────────────────────────────────────────────


def test_time_jump_with_shared_anchor_is_explained():
    ledger = build_progress_ledger(
        _chapters([
            "林川把铜牌交给苏禾。\n\n苏禾说：三天后旧库房见。",
            "第2章 旧库房\n\n三天后，林川赶到旧库房，苏禾已经等在那里。",
        ]),
        plot_bible=BIBLE,
        config=DEFAULTS,
    )
    row = ledger["linkage"]["rows"][0]
    assert row["time_jump_days"] == 3
    assert row["time_jump"] is True
    assert row["shared_anchor"] is True
    assert row["time_jump_unexplained"] is False


def test_time_jump_without_anchor_is_a_break():
    ledger = build_progress_ledger(
        _chapters([
            "林川把铜牌交给苏禾。\n\n苏禾说完了话。",
            "第2章 城外\n\n半个月后，城外的风很冷。",
        ]),
        plot_bible=BIBLE,
        config=DEFAULTS,
    )
    row = ledger["linkage"]["rows"][0]
    assert row["time_jump_days"] == 15
    assert row["shared_anchor"] is False
    assert row["time_jump_unexplained"] is True
    assert ledger["linkage"]["breaks"][0]["chapter"] == 2


def test_previous_question_rementioned():
    ledger = build_progress_ledger(
        _chapters([
            "林川在屋里坐着。\n\n那封信到底是谁送来的？",
            "林川还在想那封信到底是谁送来的。",
        ]),
        plot_bible=BIBLE,
        config=DEFAULTS,
    )
    row = ledger["linkage"]["rows"][0]
    assert row["prev_questions"]
    assert row["questions"][0]["mentioned"] is True
    assert not row["questions_dropped"]


def test_rhetorical_renzhi_is_not_a_question():
    assert _open_questions(
        "三号机先断网，谁都别恢复。", tail_paragraphs=1, top=3) == []
    assert _open_questions(
        "那封信到底是谁送来的？", tail_paragraphs=1, top=3) == ["那封信到底是谁送来的"]


# ─── structure 引用 / 配置 ────────────────────────────────────────────────


def test_structure_check_is_referenced_not_recomputed():
    stored = {
        "status": "checked", "mode": "report",
        "beat": {"coverage": 0.5, "total": 2, "covered": 1, "missing": ["情节点B"]},
        "premature": {"chapters_hit": 1, "hits": []},
        "gender": {"count": 1, "mismatches": []},
        "blocking": False, "enforced": False,
    }
    ledger = build_progress_ledger(
        _chapters(["林川进了屋。", "苏禾进了屋。"]),
        plot_bible=BIBLE,
        structure_checks={1: stored},
        config=DEFAULTS,
    )
    row = ledger["structure"]["rows"][0]
    assert row["source"] == "stored"
    assert row["beat_coverage"] == 0.5
    assert row["beat_missing"] == ["情节点B"]
    assert row["premature_chapters_hit"] == 1
    assert row["gender_count"] == 1
    # 只判不拦：台账不产出 passed / violations
    assert "passed" not in ledger
    assert "violations" not in ledger


def test_config_defaults_and_override():
    assert resolve_config(SimpleNamespace()).keys() == DEFAULTS.keys()
    cfg = resolve_config(SimpleNamespace(progress_ledger_cast_overlap_high=0.5))
    assert cfg["cast_overlap_high"] == 0.5
    assert cfg["cast_stagnant_chapters"] == DEFAULTS["cast_stagnant_chapters"]


def test_same_input_same_output():
    chapters = _chapters(["林川进了屋。", "苏禾进了屋。"])
    assert (build_progress_ledger(chapters, plot_bible=BIBLE, config=DEFAULTS)
            == build_progress_ledger(chapters, plot_bible=BIBLE, config=DEFAULTS))


# ─── 落库视图 / 提示层要求 ────────────────────────────────────────────────


def test_chapter_view_keeps_only_verifiable_fields():
    ledger = build_progress_ledger(
        _chapters([
            "林川和苏禾进了旧库房。",
            "第二天，林川把铜牌递给苏禾。",
        ]),
        plot_bible=BIBLE,
        config={**DEFAULTS, "cast_overlap_high": 0.8},
    )
    view = chapter_view(ledger, 2)
    assert view["mode"] == "report"
    assert view["status"] == "ok"
    assert view["cast"]["cast"] == ["林川", "苏禾"]
    assert view["cast"]["overlap_prev"] == 1.0
    # 首段时间锚点：台账能识别，提示里的第 3 条就有对应字段
    assert view["linkage"]["head_time_cues"] == ["第二天"]
    assert view["attributes"]["counts"]["item"] >= 1
    assert view["structure"]["source"] in ("stored", "computed", "missing")
    # 只判不拦：视图里不出现 passed / violations
    assert "passed" not in view
    assert "violations" not in view


def test_chapter_view_reports_skipped_window():
    view = chapter_view(build_progress_ledger([], plot_bible=BIBLE), 5)
    assert view["mode"] == "report"
    assert view["status"] == "skipped"
    assert "passed" not in view and "violations" not in view


def test_advance_requirements_only_names_checkable_fields():
    block = advance_requirements(
        chapter=4,
        plot_bible=BIBLE,
        prior_chapters=_chapters([
            "林川和苏禾进了旧库房。",
            "林川和苏禾还在旧库房。",
            "林川和苏禾走出旧库房。",
        ]),
        config=DEFAULTS,
    )
    assert "attributes[*].changes" in block
    assert "cast.overlap_prev" in block
    assert "head_time_cues" in block
    # 上一章名单来自正文匹配，不是写死的名字
    assert "第 3 章出场的是 林川、苏禾" in block
    assert "已连续 3 章" in block
    assert f"配置上限是 {DEFAULTS['cast_stagnant_chapters']} 章" in block


def test_advance_requirements_skips_cast_without_history():
    block = advance_requirements(
        chapter=1, plot_bible=BIBLE, prior_chapters=[], config=DEFAULTS)
    assert "attributes[*].changes" in block
    assert "head_time_cues" in block
    # 没有前序章节就无法比对集合，阵容那条宁可不写
    assert "角色阵容" not in block


def test_advance_requirements_skips_cast_without_roster():
    block = advance_requirements(
        chapter=3,
        plot_bible={"characters": []},
        prior_chapters=_chapters(["有人进了屋。", "有人还在屋里。"]),
        config=DEFAULTS,
    )
    assert "attributes[*].changes" in block
    assert "角色阵容" not in block
