"""叙事档案的分层结构（method / characters / world / timeline）。

## 为什么分层

一份蒸馏产物要服务三种用途，各自需要的东西不同：

    用途        需要什么
    原创        作者的写法（手法/语序/句法）—— 对任何新书都有效
    续写        写法 + 世界观 + 人物 + 时间线 —— 只对这本书有效
    角色聊天    单个角色的性格/经历/口头禅/关系

旧结构是 14 个维度平铺在一个 `rules` 字典里，三种模式都从里面挑。这导致
**原创模式必须靠 `_prompt_form` 逐条剥离实体**——而剥离必然漏：

    实测：791 条「高频场景模板」剥掉「（证据：…）」后仍带完整剧情
         「贵客抵达→王府开中门并铺陈仪仗→以糖葫芦诱引小王爷」

分层后，原创模式**根本不读 characters/world/timeline**，不需要剥离。

## 四层各自回答什么

    method     怎么写（抽象、可迁移、零专名）
    characters 这个人是谁、会怎么做、怎么说话（含专名）
    world      这个世界的规则（力量体系/势力/地理/时代，含专名）
    timeline   已经发生了什么（有序事件，含专名）

## 兼容旧档案

旧档案只有 `rules`（14 维平铺）。`normalize_layers()` 把 `rules` 整体当
method，其余三层为空——**不猜测、不推断**。宁可少给，不可给错：
把旧 rules 里的「角色声音词典」猜成 characters 层，会让原创模式读到参考书角色。
"""

from __future__ import annotations

from typing import Any

#: 四层键名。顺序即渲染顺序。
LAYERS: tuple[str, ...] = ("method", "characters", "world", "timeline")

#: method 层里属于「手法」的维度（与 narrative.NARRATIVE_DIMENSIONS 的差集是
#: 角色声音/文笔量化审计/文风漂移分析——那三个不进 method）。
METHOD_KEYS: tuple[str, ...] = (
    "人物描写手法",
    "对白风格",
    "段落编排",
    "场景构建",
    "情绪表达",
    "节奏控制",
    "世界观呈现",
    "剧情编排模式",
    "伏笔与悬念设计",
    "节拍序列",
    "高频场景模板",
    "潜台词模式",
    "语序编排",
)

#: 只给续写用的 method 子集：原创要学「怎么写」，续写额外要「怎么接」。
#: 目前两层相同——留这个常量是为了将来出现「只对续写有意义」的维度时
#: 不用改调用方。
CONTINUATION_EXTRA_METHOD_KEYS: tuple[str, ...] = ()


def empty_layers() -> dict[str, Any]:
    """四层全空的结构。"""
    return {"method": {}, "characters": [], "world": {}, "timeline": []}


def normalize_layers(profile: dict | None) -> dict[str, Any]:
    """把任意形态的档案规范成四层结构。

    接受的输入：
      1. 新格式 `{method: {...}, characters: [...], world: {...}, timeline: [...]}`
      2. 旧格式 `{rules: {14维}, forbidden: {...}}` → rules 整体归 method
      3. 混合（新键存在但某层缺失）→ 缺失层补空

    **不做智能推断**：旧格式里的「角色声音词典」留在 method 里，不搬到
    characters。理由见模块 docstring —— 猜错会让原创模式读到参考书角色。
    """
    if not isinstance(profile, dict):
        return empty_layers()

    out = empty_layers()

    # 1) 新格式优先
    has_new = any(k in profile for k in LAYERS)
    if has_new:
        m = profile.get("method")
        if isinstance(m, dict):
            out["method"] = dict(m)
        c = profile.get("characters")
        if isinstance(c, list):
            out["characters"] = list(c)
        w = profile.get("world")
        if isinstance(w, dict):
            out["world"] = dict(w)
        t = profile.get("timeline")
        if isinstance(t, list):
            out["timeline"] = list(t)
        # 新格式里若还留着 rules（过渡期双写），把它并进 method 的空维度
        legacy = profile.get("rules")
        if isinstance(legacy, dict):
            for dim, items in legacy.items():
                out["method"].setdefault(dim, items)
    else:
        # 2) 旧格式：rules 整体归 method
        legacy = profile.get("rules")
        if isinstance(legacy, dict):
            out["method"] = dict(legacy)

    # forbidden 与层无关，单独保留在顶层（调用方按 profile 决定用不用）
    if isinstance(profile.get("forbidden"), dict):
        out["forbidden"] = dict(profile["forbidden"])
    return out


def method_rules(profile: dict | None) -> dict[str, list]:
    """取 method 层的维度 → 规则列表。"""
    return normalize_layers(profile).get("method") or {}


def character_entries(profile: dict | None) -> list[dict]:
    """取 characters 层（每个元素是一个角色的描述字典）。"""
    return normalize_layers(profile).get("characters") or []


def world_facts(profile: dict | None) -> dict:
    """取 world 层（力量体系/势力/地理/时代 等键）。"""
    return normalize_layers(profile).get("world") or {}


def timeline_events(profile: dict | None) -> list[dict]:
    """取 timeline 层（有序事件）。"""
    return normalize_layers(profile).get("timeline") or []


def layer_stats(profile: dict | None) -> dict[str, Any]:
    """各层规模——给 UI / 日志用，判断一次蒸馏到底产出了什么。"""
    layers = normalize_layers(profile)
    method = layers.get("method") or {}
    return {
        "method_dims": len(method),
        "method_rules": sum(len(v) for v in method.values() if isinstance(v, list)),
        "characters": len(layers.get("characters") or []),
        "world_keys": len(layers.get("world") or {}),
        "timeline_events": len(layers.get("timeline") or []),
    }
