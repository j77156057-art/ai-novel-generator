"""专名表的 LLM 判定与构建（通用版，不绑定任何具体小说）。

## 判据为什么必须是「通用的」

这是本模块最重要的一条约束：**代码里不能出现任何一本小说的名字、人物、
地名或词表**。用户上传任意小说都要能用。

所以 LLM 提示词里不写「《北荒刀歌》」，只写判据本身：

    这个词能否脱离本书语境独立成立？
      能    → 普通词 → 不脱敏（保留原样）
      不能  → 本书独有设定 → 脱敏（换占位符）

这条判据对任何虚构作品都成立：奇幻的「中土」、科幻的「星际联邦」、
武侠的「北荒」，都是「不能独立成立」的一类；而「江湖」「魔法」「飞船」
是通用词，哪个故事里都可能有。

## 两阶段（各自的定位）

1. `pmi_candidates` —— 纯统计，**高召回**，允许虚词混入
2. LLM 判定 —— **高精度**，把虚词踢掉

不要试图用规则替掉第 2 步：后缀规则和自足率判据都已实测失败（见
`app/style/entities.py` 模块 docstring），而且任何规则都要针对具体语言
和具体书的命名习惯调参，换一本就失效。

## 缓存

LLM 判定按**词**缓存（`{词: 类别 or None}`），与书无关：

- 换一本书复用同一份缓存 → 同一个词在不同书里判定通常一致（虚词就是虚词）
- 重建同一本书的表 → 零 LLM 调用

但**专名表本身按书缓存**：A 书的人名地名不该出现在 B 书的表里。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.style.entities import (
    PLACEHOLDER_PREFIX,
    drop_fragments,
    pmi_candidates,
)

#: 判定结果类别 → 表里的键
CATEGORY_KEYS = {
    "character": "characters",
    "place": "places",
    "faction": "factions",
    "item": "items",
}

#: 每批送 LLM 的候选数。150 实测批次间判定量波动很大（0~46 个），
#: 原因是候选质量参差；不影响最终并集，只是日志噪声。改 100 稍稳。
BATCH_SIZE = 100

#: 判定提示词——**通用模板，不含任何具体书名/人物/地名**。
_JUDGE_SYSTEM = """你是中文小说语料的专有名词标注员。给你一批按统计方法从某部长篇小说里召回的候选词，逐个判断它是不是**这部小说独有的专有名词**。

四种类别：
- character  人名（含绰号、固定的尊称式指代）
- place      地名 / 国名 / 区域名
- faction    门派 / 势力 / 军种 / 官署名 / 组织名
- item       专有物件 / 功法 / 称号 / 名号

**必须返回 null 的情况**：
1. 通用词：任何脱离本书也成立的普通名词。比如职业、身份、泛称、抽象概念
2. 形容词 / 动作 / 状态描写
3. 虚词 / 副词 / 连词 / 代词 / 数量词
4. 盗版站水印、章节标题、站点广告文本
5. 更长专名的片段：若某词只是另一个更长专名的一部分（比如完整地名的前缀、
   完整人名的姓），返回 null —— 只保留完整形态

**核心判据只有一条：这个词能否脱离本书语境独立成立？**
- 能独立成立 → 它在别的故事里也会出现 → null（不要脱敏，否则会破坏正常词汇）
- 不能（是本书独有的设定）→ 归类

举例（这些例子是通用的，不是任何具体小说的内容）：
- 泛称/通用：「公子」「师父」「皇帝」「高手」「江湖」→ null
- 状态/动作：「厉害」「平静」「仰头」「转身」→ null
- 虚词：「如何」「这个」「都要」「许多」→ null

只输出 JSON 数组，每个候选词必须出现一次：
[{"w":"候选词","t":"character"},{"w":"另一个","t":null}]"""


def _judge_batch(provider, words: list[str]) -> dict[str, str | None]:
    user = f"候选词（共 {len(words)} 个），逐个判断：\n{'、'.join(words)}"
    raw = ""
    for _ in range(2):
        try:
            raw = provider.chat(_JUDGE_SYSTEM, user)
            break
        except Exception:  # noqa: BLE001 - 单批失败不影响其余批次
            continue
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str | None] = {}
    for it in arr:
        if isinstance(it, dict) and it.get("w"):
            t = it.get("t")
            out[str(it["w"])] = str(t) if t else None
    return out


def build_entity_table(
    text: str,
    provider,
    *,
    cache_path: Path | None = None,
    batch_size: int = BATCH_SIZE,
    log=None,
) -> dict[str, Any]:
    """从语料正文构建专名表。**不含任何具体书名/人物/地名的假设。**

    返回：
        {
          "characters": [...], "places": [...], "factions": [...], "items": [...],
          "version": 2,
          "audited": <送审候选数>,
          "dropped_fragments": {...},
        }

    `cache_path` 给定时，LLM 判定按词缓存（跨书复用）；不给则不缓存。
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    cand = pmi_candidates(text)
    _log(f"PMI 召回 {len(cand)} 个候选")

    # 去重后按频次降序
    todo = sorted(cand.items(), key=lambda x: -x[1])

    verdicts: dict[str, str | None] = {}
    if cache_path and cache_path.exists():
        try:
            verdicts = json.loads(cache_path.read_text(encoding="utf-8"))
            _log(f"复用判定缓存 {len(verdicts)} 条")
        except Exception:  # noqa: BLE001
            verdicts = {}

    pending = [(w, c) for w, c in todo if w not in verdicts]
    _log(f"待审 {len(pending)} 个（已缓存 {len(todo) - len(pending)} 个）")

    nb = (len(pending) + batch_size - 1) // batch_size
    for i in range(0, len(pending), batch_size):
        batch = pending[i:i + batch_size]
        got = _judge_batch(provider, [w for w, _ in batch])
        for w, _c in batch:
            # 未返回的也记 None：避免下次重复问同一个词
            verdicts[w] = got.get(w)
        _log(f"批 {i // batch_size + 1}/{nb} 判出专名 {sum(1 for v in got.values() if v)} 个")
        if cache_path:
            try:
                cache_path.write_text(
                    json.dumps(verdicts, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:  # noqa: BLE001 - 缓存失败不影响主流程
                pass

    table: dict[str, list[str]] = {k: [] for k in CATEGORY_KEYS.values()}
    for w, _c in todo:
        key = CATEGORY_KEYS.get(verdicts.get(w) or "")
        if key and w not in table[key]:
            table[key].append(w)

    kept, dropped = drop_fragments(table, text)
    out: dict[str, Any] = dict(kept)
    # **判定之前**的 PMI 候选集：例句的兜底闸要用它（专名表保证精度，
    # 但保证不了覆盖 —— 实测表外仍有专名漏进提示词）。
    # 候选集有误报，但误伤只少了些例子，不会带进参考书的人名。
    out["candidates"] = sorted(cand.keys(), key=lambda c: -cand.get(c, 0))
    out["version"] = 2
    out["audited"] = len(todo)
    out["dropped_fragments"] = dropped
    return out


def save_entity_table(table: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")


def load_entity_table(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


__all__ = [
    "PLACEHOLDER_PREFIX",
    "CATEGORY_KEYS",
    "build_entity_table",
    "save_entity_table",
    "load_entity_table",
]
