"""从世界观测评配置生成「时代约束」提示块。

只读取 world_profile.json，不在代码里写死古代/现代规则：
- baseline / allowed_tech_levels 决定哪些档位算合规；
- native_tech_families 取本世界日常器物示例；
- tech_lexicon 取不允许的档位作为「不要写」清单；
- exception_channels / period_mismatch.legit_cues 决定例外怎么说。

供生成链路（实验 s2 / 产品 engine）在 system prompt 里插入一小段，
默认上限 300 字，避免显著抬高 prompt 体积。
"""
from __future__ import annotations

from pathlib import Path

from app.kg.era_guard import DATA_FILE, EraGuard


def _pick(items: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def build_era_constraint(
    profile_path: str | Path | None = None,
    max_chars: int = 300,
    profile: dict | None = None,
) -> str:
    """渲染一段精炼时代约束；配置异常时返回空串（调用方按未启用处理）。

    `profile` 非空时优先于 `profile_path`：项目级基线（王朝/现代/未来）由
    `era_check` 解析后据此覆盖 `allowed_tech_levels`，不改仓库里的默认表。
    """
    guard = EraGuard(profile_path or DATA_FILE, profile=profile)
    profile = guard.profile

    native_groups: list[list[str]] = []
    for fam in profile["native_tech_families"]:
        level = fam.get("tech_level", profile.get("native_family_tech_level", "原生"))
        if level in guard.allowed_levels:
            native_groups.append(fam["terms"])
    # 跨家族轮询取样，避免清一色冷兵器。
    native: list[str] = []
    for round_no in range(3):
        for terms in native_groups:
            if len(native) >= 8:
                break
            if round_no < len(terms):
                native.append(terms[round_no])
        if len(native) >= 8:
            break
    native_sample = _pick(native, 8)

    banned_by_level: dict[str, list[str]] = {}
    for level, table in profile["tech_lexicon"].items():
        if level in guard.allowed_levels:
            continue
        banned_by_level.setdefault(level, []).extend(table.keys())
    banned_sample = _pick(
        [t for level in ("近现代", "未来") for t in banned_by_level.get(level, [])],
        10,
    )
    level_label = "/".join(k for k in ("近现代", "未来") if banned_by_level.get(k)) or "外来"

    channels = "、".join(profile.get("exception_channels", [])) or "设定内渠道"
    legit = _pick(
        list(profile.get("period_mismatch", {}).get("legit_cues", [])),
        4,
    )
    legit_text = "、".join(legit) if legit else "收藏、装饰"

    def render() -> str:
        return (
            "【时代约束（写正文前先对照）】\n"
            f"基线：{profile.get('baseline', '未设定')}。日常器物可写："
            f"{'、'.join(native_sample)}。\n"
            f"不要写：{'、'.join(banned_sample)}等{level_label}器物；"
            f"确需出现，只能由{channels}带入并交代来源，否则算设定漏洞。\n"
            "例外不算越界：比喻（像…）、否定（没有…）、假设（若有…）、"
            "回忆/影视、词义借用（打电报告知）；"
            f"古物作{legit_text}可保留，但不得当日常工具用。"
        )

    text = render()
    while len(text) > max_chars and banned_sample:
        banned_sample.pop()
        text = render()
    return text


if __name__ == "__main__":
    block = build_era_constraint()
    print(block)
    print(f"\nchars={len(block)}")
