"""B2/B3：bible 键的封闭契约 + 空列表语义（2026-11-11）。

## B2 被钉住的缺陷

`background / 身世 / 功法 / 等级 / 品阶 / power_level / level` 写进 `plot_bible`
后 system prompt 的 md5 一字节不变：用户以为改了设定，模型根本没看到。
根因不是「漏了某个键」，而是**没有一张键表**——没人读的键既不拒绝也不告警。

本文件守护：
  B2-1 渲染器支持的键是一张封闭集合；集合必须覆盖所有实际读取点
       （prompts / power_prompt / power_system_type / era_check / sentinel）。
  B2-2 已知没有承载概念的键（功法/等级/品阶/power_level/level/gender…）
       写入时必须给出明确 warning，不能静默。
  B2-3 未登记的键同样必须 warning（以后加新键不能再次静默失效）。
  B2-4 角色卡里的 `background/身世/来历` 真的进 system（md5 变化）；
       同时这些键不再出现在告警里。
  B2-5 原本无声的键：**告警**与「md5 不变」同时成立（证明告警不是说谎）。
  B2-6 回执出口：POST /bible/apply 返回 warnings；老 PATCH 返回
       X-Bible-Warnings 头，不破坏 ProjectOut 契约。

## B3 被钉住的缺陷

`characters=[]` 被当成「未宣告清册」→ KG 旧角色回流。现在空列表 =
「本项目现在没有任何角色」，必须剔除 KG 角色；缺键仍然不过滤（老项目兼容）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.generation import power_system_type  # noqa: E402
from app.generation.power_prompt import build_power_constraint  # noqa: E402
from app.generation.prompts import (  # noqa: E402
    build_system_prompt,
    build_user_prompt,
    merged_constraint_items,
)
from app.kg import era_check  # noqa: E402
from app.kg.bible import (  # noqa: E402
    KNOWN_UNSUPPORTED_BIBLE_KEYS,
    KNOWN_UNSUPPORTED_CHARACTER_KEYS,
    SUPPORTED_BIBLE_KEYS,
    SUPPORTED_CHARACTER_KEYS,
    validate_bible_keys,
)
from app.main import app as fastapi_app  # noqa: E402
from app.models import Project  # noqa: E402

REMINDERS = {
    "character_constraints": [{"name": "林夜", "mentions": 5}],
    "unresolved_plotholes": [{"question": "黑铁钥匙的来历"}],
}


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class _Project:
    def __init__(self, bible: dict | None) -> None:
        self.plot_bible = bible


def _sys(bible: dict) -> str:
    return build_system_prompt("测试项目", REMINDERS, {"类型": "仙侠"}, bible)


# ======================================================================
# B2-1 封闭集合必须覆盖所有实际读取点
# ======================================================================
def test_supported_set_covers_every_reader():
    # power_system_type 的体系类型键
    assert set(power_system_type._SYSTEM_TYPE_KEYS) <= SUPPORTED_BIBLE_KEYS
    # era_check 的基线键与世界观容器
    assert set(era_check._BASELINE_KEYS) <= SUPPORTED_BIBLE_KEYS
    assert set(era_check._WORLD_CONTAINERS) <= SUPPORTED_BIBLE_KEYS
    # 其余分散读取点
    for key in (
        "characters", "relationships", "foreshadowing",
        "synopsis", "direction", "conflicts",
        "power_ladder", "量级阶梯", "systems", "realm_order",
    ):
        assert key in SUPPORTED_BIBLE_KEYS, key


def test_character_supported_set_covers_renderer():
    for key in (
        "name", "名字", "role", "motivation",
        "background", "身世", "来历", "背景",
        "voice", "sample_lines",
        "gender", "性别", "first_chapter", "首次出场章节", "首次出场",
        "境界", "修炼", "量级", "量级细层", "量级序号", "出手规模",
    ):
        assert key in SUPPORTED_CHARACTER_KEYS, key


# ======================================================================
# B2-2 / B2-3 静默无效键必须告警
# ======================================================================
def test_known_unsupported_top_level_keys_warn():
    silent = ["background", "身世", "来历", "功法", "等级", "品阶",
              "power_level", "level", "gender"]
    assert set(silent) <= set(KNOWN_UNSUPPORTED_BIBLE_KEYS)
    for key in silent:
        warnings = validate_bible_keys({key: "任意值"})
        assert any(w["path"] == key and w["disposition"] == "unsupported"
                   for w in warnings), (key, warnings)


def test_known_unsupported_character_fields_warn():
    silent = ["功法", "等级", "品阶", "power_level", "level"]
    assert set(silent) <= set(KNOWN_UNSUPPORTED_CHARACTER_KEYS)
    for key in silent:
        bible = {"characters": [{"name": "林夜", "role": "主角", key: "X"}]}
        warnings = validate_bible_keys(bible)
        assert any(w["path"] == f"characters[0].{key}" for w in warnings), warnings


def test_unregistered_key_warns():
    warnings = validate_bible_keys({"future_key_2030": 1})
    assert [w["disposition"] for w in warnings] == ["unknown"]
    assert "future_key_2030" in warnings[0]["message"]


def test_supported_bible_has_no_warnings():
    bible = {
        "synopsis": "梗概",
        "direction": "走向",
        "conflicts": ["冲突"],
        "relationships": [{"a": "林夜", "b": "白光", "relation": "师徒"}],
        "foreshadowing": ["伏笔"],
        "power_ladder": "15",
        "system_type": "细分型",
        "systems": [{"类型": "序列型"}],
        "realm_order": ["炼气", "筑基"],
        "baseline": "王朝",
        "世界观": {"baseline": "古代架空"},
        "characters": [{
            "name": "林夜", "role": "主角", "motivation": "复仇",
            "background": "幼年灭门", "境界": "金丹",
            "修炼": {"当前境界": "金丹"},
            "量级": "B", "量级细层": "三品", "量级序号": 3,
            "出手规模": "一城",
            "gender": "男", "first_chapter": 1,
        }],
    }
    assert validate_bible_keys(bible) == []


def test_world_container_shape_warns():
    warnings = validate_bible_keys({"世界观": "古代架空"})
    assert [w["disposition"] for w in warnings] == ["shape"]
    assert warnings[0]["path"] == "世界观"


def test_characters_shape_warns():
    warnings = validate_bible_keys({"characters": {"name": "林夜"}})
    assert warnings == [{
        "path": "characters",
        "disposition": "shape",
        "message": "characters 必须是数组；非数组时渲染器读不到任何角色。",
    }]


# ======================================================================
# B2-4 角色背景真的进 system
# ======================================================================
def test_character_background_renders_into_system():
    base = {"characters": [{"name": "林夜", "role": "主角"}]}
    for key in ("background", "身世", "来历", "背景"):
        bible = {"characters": [{"name": "林夜", "role": "主角", key: "幼年灭门，誓要复仇"}]}
        prompt = _sys(bible)
        assert "背景：幼年灭门，誓要复仇" in prompt, key
        assert _md5(prompt) != _md5(_sys(base)), key
    # 没有背景时与旧格式逐字节一致
    assert "  · 林夜（主角）" in _sys(base)


def test_character_voice_and_samples_render_into_system():
    bible = {"characters": [{
        "name": "林夜", "role": "主角",
        "voice": {
            "self_ref": "我",
            "address": {"长辈": "您", "同伴": "老白"},
            "tics": ["行，我先看看", "这个不归我管"],
            "sentence": "短句，先给结论",
            "under_pressure": "越急越贫",
            "taboo": ["不说包在我身上"],
        },
        "sample_lines": ["行，我先看看。", "这个不归我管。"],
    }]}
    prompt = _sys(bible)
    for fragment in (
        "自称：我", "称呼：长辈→您", "口头禅：“行，我先看看”",
        "句长/节奏：短句，先给结论", "急时：越急越贫",
        "禁说：不说包在我身上", "台词参考：“行，我先看看。”",
    ):
        assert fragment in prompt, fragment
    assert validate_bible_keys(bible) == []


# ======================================================================
# B2-5 原本无声的键：md5 不变 + 必须告警（二者同时成立）
# ======================================================================
def test_previously_silent_keys_warn_and_do_not_render():
    base = {"characters": [{"name": "林夜", "role": "主角"}]}
    base_md5 = _md5(_sys(base))
    for key, value in (
        ("功法", "天地一刀斩"), ("等级", "甲等"), ("品阶", "一品"),
        ("power_level", 9), ("level", 9),
    ):
        bible = {"characters": [{"name": "林夜", "role": "主角", key: value}]}
        assert _md5(_sys(bible)) == base_md5, key          # 仍然不渲染
        assert validate_bible_keys(bible), key              # 但不再静默
    # gender 已升级为渲染字段（角色行输出「性别」与代词），不再是「静默无效键」。
    gendered = {"characters": [{"name": "林夜", "role": "主角", "gender": "男"}]}
    assert _md5(_sys(gendered)) != base_md5
    assert validate_bible_keys(gendered) == []
    # 顶层 gender 仍没有读取者（只支持角色卡内 gender）。
    for key in ("功法", "等级", "品阶", "power_level", "level", "gender"):
        assert _md5(_sys({key: "X"})) == _md5(_sys({})), key
        assert validate_bible_keys({key: "X"}), key


def test_scale_hint_without_scale_warns():
    # 出手规模单独写不会进量级块（power_prompt 只把它当量级的附加说明）
    bible = {"characters": [{"name": "林夜", "role": "主角", "出手规模": "一城"}]}
    warnings = validate_bible_keys(bible)
    assert [w["path"] for w in warnings] == ["characters[0].出手规模"]
    assert warnings[0]["disposition"] == "shape"
    assert validate_bible_keys({"characters": [
        {"name": "林夜", "量级": "B", "出手规模": "一城"},
    ]}) == []


# ======================================================================
# 支持键的有效性：md5 必须变
# ======================================================================
def test_supported_power_keys_change_power_block():
    base = [{"name": "林夜", "role": "主角"}]
    base_text = build_power_constraint(_Project({"characters": base}))
    for card_key, card_value in (("境界", "金丹"), ("量级", "B"),
                                 ("量级细层", "三品"), ("量级序号", 3),
                                 ("出手规模", "一城")):
        card = {"name": "林夜", "role": "主角", card_key: card_value}
        if card_key == "出手规模":
            card["量级"] = "B"   # 出手规模只在有量级时生效（见 shape 告警用例）
        text = build_power_constraint(_Project({"characters": [card]}))
        assert _md5(text) != _md5(base_text), card_key
    for top in ({"system_type": "粗放型"}, {"power_ladder": "15"},
                {"量级阶梯": "15"}):
        text = build_power_constraint(_Project({"characters": base, **top}))
        assert _md5(text) != _md5(base_text), top


def test_supported_user_prompt_keys_change_user_prompt():
    """bible 支持键必须真的进 prompt——但首段只注入 conflicts。

    2026-11-12 起，synopsis / direction / relationships / foreshadowing 是
    「整条弧光」，首段注入会让模型在第一节演完（p5 实测 750 字收口），
    统一延后到第 2 节；因此这里按段型分别断言。
    """
    base = {"characters": [{"name": "林夜", "role": "主角"}]}
    arc_keys = (
        ("synopsis", "主角复仇"),
        ("direction", "走向结局"),
        ("relationships", [{"a": "林夜", "b": "白光", "relation": "师徒"}]),
        ("foreshadowing", ["钥匙的来历"]),
    )

    # 首段：弧光键一律不注入，只有 conflicts（本节可切入的冲突）生效
    opening_base = build_user_prompt([], [], None, 1000, plot_bible=base)
    for key, value in arc_keys:
        text = build_user_prompt([], [], None, 1000,
                                 plot_bible={**base, key: value})
        assert _md5(text) == _md5(opening_base), f"首段不应注入 {key}"
    conflict_text = build_user_prompt(
        [], [], None, 1000, plot_bible={**base, "conflicts": ["灭门之仇"]}
    )
    assert _md5(conflict_text) != _md5(opening_base), "conflicts"

    # 第 2 节起：五个键全部恢复注入
    later_base = build_user_prompt(["上一段"], [], None, 1000, plot_bible=base)
    for key, value in arc_keys + (("conflicts", ["灭门之仇"]),):
        text = build_user_prompt(["上一段"], [], None, 1000,
                                 plot_bible={**base, key: value})
        assert _md5(text) != _md5(later_base), key


# ======================================================================
# B3 空列表 ≠ 缺键
# ======================================================================
def test_b3_empty_characters_removes_kg_roles():
    chars, _ = merged_constraint_items(REMINDERS, {"characters": []})
    assert chars == []
    prompt = _sys({"characters": []})
    assert "林夜" not in prompt
    assert "暂无锁定角色" in prompt


def test_b3_missing_key_still_merges_kg_roles():
    chars, _ = merged_constraint_items(REMINDERS, {"relationships": []})
    assert [c.get("name") for c in chars] == ["林夜"]


def test_b3_non_empty_roster_filters_like_before():
    reminders = {"character_constraints": [{"name": "林夜"}, {"name": "顾辰逸"}]}
    chars, _ = merged_constraint_items(
        reminders, {"characters": [{"name": "林夜", "role": "主角"}]}
    )
    assert [c.get("name") for c in chars] == ["林夜"]
    prompt = build_system_prompt("测试项目", reminders, {"类型": "仙侠"},
                                 {"characters": [{"name": "林夜", "role": "主角"}]})
    assert "林夜" in prompt
    assert "顾辰逸" not in prompt
    # 非空清单的渲染格式与改前一致（旧版只渲染 name/role/motivation）
    assert "  · 林夜（主角；动机：复仇）" in build_system_prompt(
        "测试项目", {}, {}, {"characters": [
            {"name": "林夜", "role": "主角", "motivation": "复仇"},
        ]}
    )


# ======================================================================
# B2-6 回执：apply 返回 warnings；PATCH 放响应头
# ======================================================================
def _client_and_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as db:
        db.add(Project(id=1, title="B2 回执项目"))
        db.commit()

    def _override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = _override
    return TestClient(fastapi_app), factory


def test_apply_endpoint_returns_warnings():
    client, factory = _client_and_factory()
    try:
        r = client.post("/api/projects/1/bible/apply", json={
            "bible": {
                "characters": [{"name": "林夜", "role": "主角", "功法": "天地一刀斩"}],
                "等级": "甲等",
                "future_key": True,
            },
            "note": "B2 回执实测",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        paths = {w["path"] for w in body["warnings"]}
        assert {"characters[0].功法", "等级", "future_key"} <= paths
        assert body["warnings_count"] == len(body["warnings"])
    finally:
        fastapi_app.dependency_overrides.clear()


def test_patch_endpoint_exposes_warning_header():
    client, factory = _client_and_factory()
    try:
        r = client.patch("/api/projects/1", json={
            "plot_bible": {"characters": [{"name": "林夜", "品阶": "一品"}]},
        })
        assert r.status_code == 200, r.text
        assert r.headers["X-Bible-Warnings-Count"] == "1"
        from urllib.parse import unquote
        warnings = json.loads(unquote(r.headers["X-Bible-Warnings"]))
        assert warnings[0]["path"] == "characters[0].品阶"
    finally:
        fastapi_app.dependency_overrides.clear()
