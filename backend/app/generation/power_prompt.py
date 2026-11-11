"""把「量级 + 体系类型」渲染成一段静态 system 提示。

设计边界（与实验口径一致）：

- **只写有证据的量级**：角色卡里显式写了 `量级 / 量级细层 / 量级序号` 才写具体档位；
  只写了境界名的，标 `量级 unknown`，**不按境界折算**（`resolve_scale` 的推定逻辑
  不用于这里——实验已证明按境界推定会把粗放型的强弱写反）。
- **体系类型决定比较边界**：粗放型只记录称谓，不做数值比较；细分型/序列型才给
  可比的序数说明。
- 15 层细层只在项目确实使用该阶梯时注入，避免把「大奉式九品」塞进都市异能项目。
"""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.generation.power_scale import (
    SCALE_HINTS,
    SCALE_HINTS_15,
    SCALE_LEVELS,
    SCALE_SEQ_BY_LABEL,
    detailed_scale,
    scale_seq,
)
from app.generation.power_system_type import (
    SYSTEM_TYPE_HINTS,
    normalize_system_type,
    system_type_from_bible,
)

#: 15 层阶梯的识别词：出现这些境界名才认为项目在用这张表。
_LADDER_15_MARKERS = (
    "九品", "八品", "七品", "六品", "五品", "四品", "三品", "二品", "一品", "超品",
)

_LABEL_BY_SEQ = {seq: label for label, seq in SCALE_SEQ_BY_LABEL.items()}

_TAIL_RULE = "写打斗时：低量级不要硬接高量级的一击；高量级交锋不要写成贴身短刃。"


def _bible(project) -> dict:
    bible = getattr(project, "plot_bible", None)
    return bible if isinstance(bible, dict) else {}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(str(v).strip() for v in value if str(v).strip())
    if isinstance(value, dict):
        return "；".join(f"{k}={v}" for k, v in value.items())
    return str(value).strip()


def _uses_15_ladder(bible: dict) -> bool:
    declared = _as_text(bible.get("power_ladder") or bible.get("量级阶梯"))
    if declared in ("15", "15层", "十五层", "power_scale_15"):
        return True
    blob = _as_text(bible)
    return any(marker in blob for marker in _LADDER_15_MARKERS)


def _explicit_scale(card: dict) -> tuple[str, int | None]:
    """返回 (标签/五档名, seq)；没有任何显式量级证据时返回 ("", None)。"""
    nested = card.get("量级")
    raw = {**card, **nested} if isinstance(nested, dict) else card

    seq: int | None = None
    if "量级序号" in raw:
        seq = scale_seq(raw.get("量级序号"))
    for field in ("量级细层", "量级"):
        value = raw.get(field)
        if value in (None, ""):
            continue
        label = detailed_scale(value)
        if label:
            return label, SCALE_SEQ_BY_LABEL.get(label, seq)
        text = _as_text(value)
        if text:
            return text, seq
    if seq is not None:
        return _LABEL_BY_SEQ.get(seq, f"seq {seq}/15"), seq
    return "", None


def _character_scale_rows(bible: dict, *, limit: int = 12) -> list[str]:
    """每个角色一行；只写显式证据，境界名单独出现时记为 unknown。

    境界与量级是**同一维度（战力）的两种刻度**，不是两个维度：
    境界是体系内名义阶位，量级是跨体系可比的物理尺度，realm_map 里
    `tiers[].stage_seq` 就是两者的默认映射。所以两者同时存在时合并成
    「境界「A」（量级 B，seq n/15）」，境界名在前（读者看到的是这个名字），
    量级放在括号里当可比刻度——既不丢境界名，也不让量级消失。
    """
    rows: list[str] = []
    characters = bible.get("characters") or []
    if not isinstance(characters, list):
        return rows
    for card in characters:
        if not isinstance(card, dict):
            continue
        name = _as_text(card.get("name") or card.get("名字"))
        if not name:
            continue

        detail, seq = _explicit_scale(card)
        realm = ""
        cultivation = card.get("修炼")
        if isinstance(cultivation, dict):
            realm = _as_text(cultivation.get("当前境界"))
        realm = realm or _as_text(card.get("境界"))

        if detail:
            hint = _as_text(card.get("出手规模"))
            hint = hint or SCALE_HINTS_15.get(detail) or SCALE_HINTS.get(detail, "")
            if realm:
                head = f"  · {name}＝境界「{realm}」（量级 {detail}"
                if seq is not None:
                    head += f"，seq {seq}/15"
                head += "）"
            else:
                head = f"  · {name}＝{detail}"
                if seq is not None:
                    head += f"（seq {seq}/15）"
            rows.append(head + (f"：{hint}" if hint else ""))
        elif realm:
            # 有境界名但没有量级证据：只登记称谓，明确标 unknown，不折算。
            rows.append(f"  · {name}＝境界「{realm}」（量级 unknown，不按境界折算）")
        if len(rows) >= limit:
            break
    return rows


def build_power_constraint(project, max_chars: int = 900) -> str:
    """渲染量级约束块；开关关闭时返回空串（拼接结果逐字节不变）。"""
    # 用 getattr 读开关：本模块与 config 的 power 字段物理分块，单侧批次里
    # 字段可能不存在，此时按默认「开启」处理，而不是抛 AttributeError。
    if not getattr(settings, "power_guard_enabled", True):
        return ""
    bible = _bible(project)
    system_type, source = system_type_from_bible(bible)
    system_type = normalize_system_type(system_type)
    hint = SYSTEM_TYPE_HINTS.get(system_type, "")

    head = [
        "【量级与体系（写强弱前对照）】",
        f"体系类型：{system_type}（来源：{source}）—— {hint}",
    ]
    if system_type == "粗放型":
        head.append(
            "粗放型只记录称谓，不做数值比较：不要因为境界名更靠后就默认更强，"
            "强弱只按「出手规模 / 具体表现」写。"
        )
    else:
        head.append("境界序数只在同一体系内比较；跨体系时只比五档量级或 15 层 seq。")
    head.append(
        "五档量级：" + "；".join(
            f"{level}={SCALE_HINTS.get(level, '')}" for level in SCALE_LEVELS
        )
    )

    ladder: list[str] = []
    if _uses_15_ladder(bible) and SCALE_SEQ_BY_LABEL:
        ladder.append(
            "十五层细层（有明确出处才按层写，不要自行补层）："
            + "；".join(
                f"{label}={SCALE_HINTS_15.get(label, '')}"
                for label in SCALE_SEQ_BY_LABEL
            )
        )

    rows = _character_scale_rows(bible)
    rows_block = ["本作已登记（无证据的一律 unknown）："] + rows if rows else []

    def render(with_ladder: bool, with_rows: bool) -> str:
        parts = list(head)
        if with_ladder:
            parts.extend(ladder)
        if with_rows:
            parts.extend(rows_block)
        parts.append(_TAIL_RULE)
        return "\n".join(parts)

    text = render(True, True)
    while len(text) > max_chars and rows:
        rows.pop()
        rows_block = ["本作已登记（无证据的一律 unknown）："] + rows if rows else []
        text = render(True, bool(rows))
    if len(text) > max_chars:
        text = render(False, bool(rows))
    return text
