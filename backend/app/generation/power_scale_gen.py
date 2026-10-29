"""AI 帮角色定量级（只建议，不落库）。

## 为什么单独一个模块

用户诉求（2026-10-29）：「这种高纬度生物，举手投足可以破坏一个城市甚至一个星球
那种角色的感觉，但生成出来的就和路边混混斗殴一样」。

⇒ 需要 AI 从角色的**身份/动机/世界观**推出「他出手是什么规模」。

## 量级是**相对标尺**

- 档位固定五档（凡人/伤人/毁物/毁城/灭世）——
  跨体系通用（不预设武侠还是修仙）；
- 每档配一句**尺度锚点**（「抬手能让半座雾港沉进海里」），
  让模型有物理参照，而不是只看到一个抽象词；
- ⚠️ 判据是「**能造成什么后果**」，不是 DPS 数值。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.generation.power_scale import SCALE_HINTS, SCALE_LEVELS, normalize_scale

_SCALE_SYSTEM = """你在为中文小说里的角色定「**量级**」。

## 量级是什么

量级 = **这个角色出手时能造成什么后果**。不是战斗力数值，是**规模**。

参考五档（跨体系通用，不要预设是武侠还是修仙）：

{levels}

## 硬要求

1. **量级要与身份一致**：身份是「神女/旧神/掌门」这类高位的，
   量级不能给「凡人级」；身份是「船工/乞丐」这类，不能给「灭世级」。
2. **出手规模要具体**：写「抬手能让半座雾港沉进海里」，
   不要写「威力极大」。这句会**原样进提示词**，模型照着它写场面。
3. **压制**写他能压住谁、压不住什么（一句话）。
4. 若这个角色是**凡人级**，也要写清他挡不住什么（这决定了场面张力）。

## 输出 JSON（只输出 JSON）

{{"量级": "毁城级", "出手规模": "抬手能让半座雾港沉进海里，海水倒卷着撞碎灯塔石阶",
  "压制": "能压住塔里的灯火，但伤不了人"}}
"""


def build_scale_prompt(char: dict, bible: dict | None = None, *,
                        note: str = "") -> str:
    """构造量级生成提示词。"""
    levels = "\n".join(
        f"- {name}：{SCALE_HINTS.get(name, '')}" for name in SCALE_LEVELS)
    card = (char or {}).get("card") if isinstance((char or {}).get("card"), dict) else {}
    ability = (card or {}).get("能力") if isinstance((card or {}).get("能力"), dict) else {}
    profile = {
        "名字": (char or {}).get("name") or "",
        "身份": (char or {}).get("role") or "",
        "动机": (char or {}).get("motivation") or "",
        "武器": [x.get("名称") for x in (ability.get("武器") or [])
                 if isinstance(x, dict)],
        "已有能力": [x.get("名称") for x in (ability.get("能力") or [])
                     if isinstance(x, dict)],
        "能力上限": ability.get("能力上限") or [],
        "修炼": (card or {}).get("修炼") or {},
    }
    world = ""
    if isinstance(bible, dict):
        parts = []
        for key, label in (("synopsis", "故事"), ("premise", "简介")):
            if bible.get(key):
                parts.append(f"{label}：{str(bible[key])[:400]}")
        pw = bible.get("power_system")
        if pw:
            parts.append(f"体系：{json.dumps(pw, ensure_ascii=False)[:400]}")
        world = "\n".join(parts)
    extra = f"\n\n## 作者的要求\n{note.strip()[:400]}" if note.strip() else ""
    return (
        _SCALE_SYSTEM.format(levels=levels)
        + "\n\n## 这个角色\n"
        + json.dumps(profile, ensure_ascii=False, indent=1)
        + (f"\n\n## 世界观\n{world}" if world else "")
        + extra
        + "\n\n按上述 JSON 输出这个角色的量级。"
    )


def parse_scale(raw: Any) -> dict:
    """解析量级结果。

    ⚠️ **量级档必须真的在那五档里** —— 模型自造一个「神级」就没法横向比较了
    （分层群像与「谁压谁」都靠同一把尺）。
    认不出来 ⇒ 丢掉这个档位（其它字段保留）。
    """
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        data = {}
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
            try:
                data = json.loads(cleaned)
            except Exception:  # noqa: BLE001
                m = re.search(r"\{.*\}", cleaned, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(0))
                    except Exception:  # noqa: BLE001
                        data = {}
    if not isinstance(data, dict):
        return {}
    # 也接受外层裹一层 {"量级": {...}}
    if isinstance(data.get("量级"), dict):
        data = data["量级"]
    out = normalize_scale(data)
    level = out.get("量级") or ""
    for name in SCALE_LEVELS:
        if name in level:
            out["量级"] = name
            break
    else:
        # 认不出档位 ⇒ 丢掉档位（其它字段仍有用）
        out.pop("量级", None)
    return out
