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

### 缓存里必须分清「不是专名」和「没判出来」

- ``None`` = 模型判了「不是专名」
- `UNJUDGED` = **没判出来**（批次失败 / 输出被 max_tokens 截断 / 模型漏了这个词）

这两者混为一谈会造成很难发现的错误：一次批次失败 = 一百个真名被记成
普通词，还被**永久缓存**下来。实测 2026-10-30 的 18 题盲评包就是这样
把真名留在了题面里 ⇒ 盲评当场作废。

所以：

- 批次失败/截断 ⇒ **拆小批重试**，拆到 5 个词一批还不行才标 `UNJUDGED`；
- `UNJUDGED` **不进缓存** ⇒ 下次还会重判；
- 缓存带版本号，版本对不上整份作废（判定逻辑升级时旧的漏名结果不能用）。
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
#:
#: ⚠️ **盲评脱敏那条链路要调小**（见 `acceptance_lib.mask_side` 传入的
#: `batch_size`）：候选多半是「长得像人名」的噪声，模型回包是 100 个 JSON
#: 对象 —— 输出越长越容易被 max_tokens 拦腰截断，而**截断会让整批判定
#: 直接消失**（实测见下）。
BATCH_SIZE = 100

#: 判定缓存的版本号。**版本对不上就整份作废**。
#:
#: 为什么要有版本：旧格式把「批次失败」也写成了「判定为 null」，
#: 而 null 的含义是「这个词不是专名」—— 于是一批失败 = 一百个真名
#: 被记成普通词，还被永久缓存下来。升级判定逻辑时必须让旧缓存失效，
#: 否则等于继续用一份**已经漏过名**的判定结果。
CACHE_VERSION = 2

#: 「没判出来」的判定值。
#:
#: ⚠️ **它和 null 是两回事**，不能混：
#:   - ``None`` = 模型明确判了「这不是专名」
#:   - ``UNJUDGED`` = 模型没给出答案（批次失败 / 输出被截断 / 漏了这个词）
#:
#: 脱敏时把 UNJUDGED 当成「不是专名」= 把真名留在题面里 = 盲评直接作废；
#: 所以脱敏那条链路要么重试到判出来，要么**整包判废**（fail closed）。
UNJUDGED = ""

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


def _judge_batch(provider, words: list[str]) -> tuple[dict[str, str | None], bool]:
    """判一批候选词。返回 ``(判定结果, 是否被截断)``。

    ## 为什么返回值里带「是否被截断」

    provider 在 ``finish_reason == "length"``（输出撞上 max_tokens）时
    **只告警、照常返回半截内容**。半截的 JSON 数组解析不了 ⇒ 本批返回空 ⇒
    上游如果把「空」当成「这批都没判出专名」，等于**一百个真名被判成普通词**
    （实测 2026-10-30：18 题盲评包的人类侧 1896 个候选里 1880 个被记成
    null，成片出现；真名「张山」「李香玲」原样留在题面里 —— 盲评当场作废）。

    所以这里把「截断」这件事**显式报上去**，让调用方重试（拆小批次）
    而不是默默接受一个空结果。
    """
    user = f"候选词（共 {len(words)} 个），逐个判断：\n{'、'.join(words)}"
    raw = ""
    truncated = False
    for _ in range(2):
        try:
            raw = provider.chat(_JUDGE_SYSTEM, user)
            truncated = getattr(provider, "last_finish_reason", None) == "length"
            break
        except Exception:  # noqa: BLE001 - 单批失败不影响其余批次
            continue
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return {}, truncated
    try:
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}, truncated
    out: dict[str, str | None] = {}
    for it in arr:
        if isinstance(it, dict) and it.get("w"):
            t = it.get("t")
            out[str(it["w"])] = str(t) if t else None
    return out, truncated


def _load_cache(cache_path: Path | None) -> dict[str, str | None]:
    """读判定缓存。**版本对不上 / 旧格式 ⇒ 整份作废**。

    旧格式（没有版本号的裸 dict）为什么必须作废：它把「批次失败」也记成了
    null，而 null 的含义是「不是专名」—— 继续用它 = 继续用一份漏过名的判定。
    """
    if not cache_path or not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    if "__v__" not in data:
        return {}          # 旧格式：作废
    if data.get("__v__") != CACHE_VERSION:
        return {}          # 版本不匹配：作废
    verdicts = data.get("verdicts")
    return dict(verdicts) if isinstance(verdicts, dict) else {}


def _save_cache(cache_path: Path | None, verdicts: dict[str, str | None]) -> None:
    """写判定缓存。**只写判出来的**（UNJUDGED 不入缓存 ⇒ 下次还会重判）。"""
    if not cache_path:
        return
    try:
        cache_path.write_text(
            json.dumps({"__v__": CACHE_VERSION, "verdicts": verdicts},
                       ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 缓存失败不影响主流程
        pass


def _judge_resilient(provider, words: list[str], *, log=None,
                      min_batch: int = 5) -> dict[str, str | None]:
    """把一批词判完，尽量**不留「没判出来」的**。

    做法：整批判 → 有词没判到，就把没判到的**拆成更小的批**再判
    → 拆到 ``min_batch`` 还是缺，就标成 `UNJUDGED`。

    为什么拆小批就能救回来：回包是「每个词一个 JSON 对象」，
    词越多输出越长 ⇒ 越容易被 max_tokens 拦腰截断 ⇒ 越容易整批消失。
    拆到 5 个词一批时输出只有几十个 token practically 不会截断。

    ⚠️ **绝不把「没判到」写成 null**（null 的含义是「不是专名」）。
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    out: dict[str, str | None] = {}
    queue: list[list[str]] = [list(words)]
    rounds = 0
    while queue:
        rounds += 1
        if rounds > 64:      # 兜底：别无限拆
            for batch in queue:
                for w in batch:
                    out.setdefault(w, UNJUDGED)
            break
        batch = queue.pop(0)
        got, truncated = _judge_batch(provider, batch)
        out.update(got)
        missing = [w for w in batch if w not in got]
        if not missing:
            continue
        if len(batch) <= min_batch:
            for w in missing:
                out[w] = UNJUDGED
            _log(f"  有 {len(missing)} 个词没判出来（批次已拆到 {min_batch} 个）："
                 f"{missing[:6]}{' …' if len(missing) > 6 else ''}")
            continue
        # 拆成更小的批重试（截断多半是输出太长）
        half = max(min_batch, len(missing) // 2)
        chunks = [missing[i:i + half] for i in range(0, len(missing), half)]
        _log(f"  {len(missing)} 个词没判到"
             f"{'（回包被 max_tokens 截断）' if truncated else ''}"
             f" ⇒ 拆成 {len(chunks)} 小批重试")
        queue = chunks + queue      # 小的先判
    return out


def judge_proper_nouns(
    words: list[str],
    provider,
    *,
    cache_path: Path | None = None,
    batch_size: int = BATCH_SIZE,
    log=None,
) -> dict[str, str | None]:
    """判定一批候选词是不是**专有名词**（纯判定，不做召回）。

    与 `build_entity_table` 的第 2 步**同源**（同一提示词、同一套类别），
    但**不做 PMI 召回** —— 召回口径由调用方决定。

    为什么要单独暴露：盲评包的脱敏要按**自己的口径**召回
    （只判「落在样本里」的候选，省判定成本），不能被建表流程的召回参数绑住。

    返回 ``{词: 类别 or None or UNJUDGED}``：

    - 类别（``"character"`` 等）= 判出来了，是专名
    - ``None`` = 判出来了，**不是**专名
    - `UNJUDGED` = **没判出来**（批次失败 / 输出被截断）

    ⚠️ 这三者的区别是这次踩过的坑：把「没判出来」当成「不是专名」，
    等于把真名留在盲评题面里（实测一批失败 = 100 个真名被判成普通词）。
    调用方要按 `UNJUDGED` 保守处理（脱敏场景：重试或整包作废）。
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    verdicts: dict[str, str | None] = _load_cache(cache_path)
    if verdicts:
        _log(f"复用判定缓存 {len(verdicts)} 条")

    pending = [w for w in words if w not in verdicts]
    _log(f"待审 {len(pending)} 个（已缓存 {len(words) - len(pending)} 个）")

    nb = (len(pending) + batch_size - 1) // batch_size
    for i in range(0, len(pending), batch_size):
        batch = pending[i:i + batch_size]
        got = _judge_resilient(provider, batch, log=log)
        for w in batch:
            # **只缓存判出来的**：没判出来的（UNJUDGED）不进缓存 ⇒ 下次还会重判
            v = got.get(w)
            if v is not UNJUDGED:
                verdicts[w] = v
        _log(f"批 {i // batch_size + 1}/{nb} 判出专名 "
             f"{sum(1 for v in got.values() if v)} 个")
        _save_cache(cache_path, verdicts)

    out: dict[str, str | None] = {}
    for w in words:
        out[w] = verdicts[w] if w in verdicts else UNJUDGED
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

    verdicts: dict[str, str | None] = _load_cache(cache_path)
    if verdicts:
        _log(f"复用判定缓存 {len(verdicts)} 条")

    pending = [(w, c) for w, c in todo if w not in verdicts]
    _log(f"待审 {len(pending)} 个（已缓存 {len(todo) - len(pending)} 个）")

    nb = (len(pending) + batch_size - 1) // batch_size
    for i in range(0, len(pending), batch_size):
        batch = pending[i:i + batch_size]
        got = _judge_resilient(provider, [w for w, _ in batch], log=log)
        for w, _c in batch:
            v = got.get(w)
            # **只缓存判出来的**：没判出来的（UNJUDGED）不进缓存 ⇒ 下次还会重判
            if v is not UNJUDGED:
                verdicts[w] = v
        _log(f"批 {i // batch_size + 1}/{nb} 判出专名 {sum(1 for v in got.values() if v)} 个")
        _save_cache(cache_path, verdicts)

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
