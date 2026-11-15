"""skill 清单加载机制：18 个版本文件必须能被 `load_skill()` 按目录 + 版本加载并过校验。

单独搬 JSON 等于搬了一堆没人读的文件，所以这里钉住的是**加载机制**：
- 目录布局 `agents/skills/<skill_id>/v<version>.json`，按 id + version 定位；
- 每个清单都要过 `validate_manifest`（字段集、角色、能力依赖、步骤顺序、验收条款）；
- 版本不存在 / 技能不存在要报**可自愈**的话（列出可用项），不能一律 `invalid_manifest`；
- 全部清单当前是 `experimental`：不给 `allow_experimental=True` 一律拒绝。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents import diagnostic_skills as ds  # noqa: E402

SKILLS_ROOT = Path(_BACKEND) / "app" / "agents" / "skills"
PACKED = sorted(
    (p.parent.name, int(p.stem[1:]))
    for p in SKILLS_ROOT.glob("*/v*.json")
)


def test_all_eighteen_version_files_are_present():
    assert len(PACKED) == 18, PACKED
    # 用户点名的几个：closed_inventory 8 版、body_consistency 2 版、body_repair 2 版
    assert sum(1 for s, _ in PACKED if s == "closed_inventory") == 8
    assert sum(1 for s, _ in PACKED if s == "body_consistency") == 2
    assert sum(1 for s, _ in PACKED if s == "body_repair") == 2


@pytest.mark.parametrize("skill_id,version", PACKED)
def test_every_version_loads_and_validates(skill_id: str, version: int):
    """随便取一个 skill 名和版本号，都要能按目录加载出来并校验清单。"""
    manifest = ds.load_skill(skill_id, version, allow_experimental=True)
    assert manifest["id"] == skill_id
    assert manifest["version"] == version
    assert manifest["steps"] and manifest["acceptance"]
    assert manifest["allowed_actions"] == []
    # 清单必须声明且只声明一个已注册角色
    assert len(manifest["agent_roles"]) == 1
    assert manifest["agent_roles"][0] in ds.ROLES
    # 步骤里引用的能力必须已注册且版本够
    for step in manifest["steps"]:
        assert step["capability"] in ds.CAPABILITIES


@pytest.mark.parametrize("skill_id,version", PACKED)
def test_experimental_skills_need_the_explicit_switch(skill_id: str, version: int):
    with pytest.raises(ValueError, match="skill_status_not_allowed:experimental"):
        ds.load_skill(skill_id, version)


def test_discovery_catalog_agrees_with_the_directory():
    found = {(e["id"], e["version"]) for e in ds.discover_skills(allow_experimental=True)}
    assert found == set(PACKED)
    # 默认不带实验开关 → 一个都不给（全部是 experimental）
    assert ds.discover_skills() == []


# ----------------------------------------------------------------------
# 报错要能自愈：区分「没有这个版本」与「清单坏了」
# ----------------------------------------------------------------------


def test_a_missing_version_lists_available_versions():
    with pytest.raises(ValueError) as exc:
        ds.load_skill("body_consistency", 4, allow_experimental=True)
    message = str(exc.value)
    assert "skill_version_not_found" in message
    assert "v1" in message and "v2" in message


def test_an_unknown_skill_lists_available_skills():
    with pytest.raises(ValueError) as exc:
        ds.load_skill("no_such_skill", 1, allow_experimental=True)
    message = str(exc.value)
    assert "unknown_skill" in message
    assert "closed_inventory" in message


def test_skill_identifier_cannot_escape_the_skill_root():
    for bad in ("../agents", "Closed_Inventory", "closed-inventory", ""):
        with pytest.raises(ValueError, match="invalid_skill_identifier"):
            ds.load_skill(bad, 1, allow_experimental=True)


# ----------------------------------------------------------------------
# 清单校验的硬边界
# ----------------------------------------------------------------------


def _manifest() -> dict:
    return ds.load_skill("text_quality_audit", 1, allow_experimental=True)


def test_manifest_rejects_unexpected_field_set():
    broken = dict(_manifest())
    broken["extra"] = 1
    with pytest.raises(ValueError, match="invalid_manifest_fields"):
        ds.validate_manifest(broken)


def test_manifest_rejects_invented_role():
    broken = dict(_manifest(), agent_roles=["poet"])
    with pytest.raises(ValueError, match="invalid_manifest_contract"):
        ds.validate_manifest(broken)


def test_manifest_rejects_missing_capability():
    broken = dict(
        _manifest(),
        required_capabilities=[{"id": "no_such_capability", "min_version": 1}],
    )
    with pytest.raises(ValueError, match="missing_capability"):
        ds.validate_manifest(broken)


def test_manifest_rejects_actions():
    broken = dict(_manifest(), allowed_actions=["write_body"])
    with pytest.raises(ValueError, match="unsupported_action"):
        ds.validate_manifest(broken)


def test_validated_status_requires_source_evidence():
    broken = dict(_manifest(), status="validated")
    with pytest.raises(ValueError, match="validated_requires_evidence"):
        ds.validate_manifest(broken)


def test_step_may_not_depend_on_a_later_step():
    manifest = _manifest()
    steps = [dict(s) for s in manifest["steps"]]
    steps[0]["requires"] = [steps[-1]["id"]]
    broken = dict(manifest, steps=steps)
    with pytest.raises(ValueError, match="invalid_step"):
        ds.validate_manifest(broken)


def test_manifest_identity_must_match_the_file_location(tmp_path):
    """文件放在 a/v1.json 里却自称 b v1 → 身份不符，直接拒绝。"""
    manifest = _manifest()
    forged = dict(manifest, id="closed_inventory")
    target = tmp_path / "text_quality_audit" / "v1.json"
    target.parent.mkdir()
    target.write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")
    original = ds.ROOT
    ds.ROOT = tmp_path
    try:
        with pytest.raises(ValueError, match="manifest_identity_mismatch"):
            ds.load_skill("text_quality_audit", 1, allow_experimental=True)
    finally:
        ds.ROOT = original
