"""BookDigest：把一整本书压成结构化摘要（时间线 / 角色生平 / 情境反应）。

## 为什么要有它

参考语料动辄百万字。生成时要的是「这本书发生了什么、角色是谁、他们怎么说话」，
而不是几万字的原文摘要。现有三条链路各自拿原文：

- 检索链：BM25/向量检索出**零散**片段；
- 角色聊天：按角色名检索他出场的段落；
- 叙事蒸馏：切块提炼「**怎么写**」的手法（不看「写什么」）。

缺的是一份**全书级的结构化摘要**：时间线（因果顺序）、角色生平（谁经历了什么）、
情境反应（他面对这种事会说什么/做什么）。它有三条用途：

1. **替代整本进上下文**：省 token，也给模型全局；
2. **喂给检索**：时间线与反应可以做成可检索条目；
3. **角色对话**：reactions 直接就是「他面对这种事会怎么说」的答案。

## 为什么「整本一次投喂」是默认做法

同日的上下文预算实测（见 `docs/context-budget-design.md`）：

- **规则**（蒸馏：怎么写）要**精** —— 每条都要指向原文的具体位置
  ⇒ 2~5 万字/块就够；块再大只会让每维的**候选池**变小
  （实测 20 万字/块 ⇒ 每维候选 8 条，而下游要选 5 条 ⇒ 必然大量重复）。
- **摘要**（本书：写了什么）要**全** —— 覆盖整本
  ⇒ **整本一次投喂**才是对的：模型自己看全局，才答得出
  「主角在第 3 章和第 500 章的关系变化」这种跨章问题（切块做不到）。

⇒ 默认走整本投喂；书太大、或渠道**没声明**能吃下这么多字时，退回「切块抽 + 合并」。

## 证据强制（本模块最硬的纪律）

每条摘要都必须挂证据（章号 + 原文片段）。**没证据的条目直接丢弃**，不是标注「存疑」。

理由与仓库既有纪律一致（`narrative.py`：强制附证据是唯一有效的防幻觉手段）：
模型压缩一本书时最擅长的事就是编，而编出来的东西**看起来**很像摘要 ——
不校验证据，摘要就成了「一本编出来的书的目录」。

## 三项验收（都能自动判）

1. **压缩比**：输出字数 ≤ 输入字数 / 目标比（默认 10:1）；
2. **主线覆盖**：从原文启发式抽出的候选事件里，被摘要覆盖的比例 ≥ 0.8；
3. **角色覆盖**：原文里出现频次前 N 的角色，在摘要里出现的比例 ≥ 0.9。

第 2、3 项是**词级代理**（名字 + 动作动词的共现），不是语义判定 ——
它们的用途是**当闸门**（摘要把主线压丢了就要被挡住），不是当真理。
口径写在 `verify_digest` 的 docstring 里。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select

logger = logging.getLogger(__name__)

#: 产物版本。产物结构变了就 +1（旧产物不会被当成同一种东西消费）。
DIGEST_VERSION = 1

#: 压缩比目标：输出字数 ≤ 输入字数 / 该值。
TARGET_COMPRESSION = 10.0

#: 整本投喂的**绝对天花板**（字）：与渠道无关，用来兜住「渠道声明错了」。
#:
#: 门槛本身**跟随渠道声明**（`provider.max_context_chars`）——
#: 渠道能吃下多少字才是「能不能整本投喂」的真实约束。
#:
#: ⚠️ 但渠道可能**误配**（把一个其实读不了 1000 万字的渠道声明成那样）。
#: 假设错了的后果是「请求发出去了、回来被截断、我们以为压缩成功了」——
#: 这正是本模块反复防的那类静默失效 ⇒ 留一道与渠道无关的天花板。
#:
#: 取 200 万字：高于 opencode-go 声明的 150 万（留余量后的值），
#: 只在渠道声明得更离谱时才真的起作用。
WHOLE_BOOK_ABSOLUTE_MAX_CHARS = 2_000_000

#: 主线覆盖门槛（候选事件里被摘要覆盖的比例）。
#:
#: ## 校准依据（2026-11-02 真书实测，不是拍脑袋）
#:
#: 口径改对之后（候选事件 = 「涉及主要角色的事」），在一本 66 万字真书上跑了 3 次：
#:
#: | 摘要 | 主线覆盖 |
#: |---|---|
#: | 跑 3 | 0.80 |
#: | 跑 4 | 0.70 |
#: | 跑 5 | 0.70 |
#: | **阳性对照**（只留前 5 条时间线的故意做坏的摘要） | **0.325** |
#:
#: ⇒ 好摘要落在 0.70~0.80，坏摘要 0.325。
#: ⇒ 阈值取 **0.6**：好摘要稳过（不是卡在上沿），坏摘要仍然差一大截。
#:
#: ⚠️ 原来写的是 0.8 —— 那正好是好摘要的上沿 ⇒ 一半的正常摘要会被误杀。
#: 这个数当初是在**几百字的合成语料**上定的，搬到真书上必然要重新校准。
MIN_MAINLINE_COVERAGE = 0.6
#: 角色覆盖门槛（高频角色里出现在摘要中的比例）。
#:
#: 实测（同一批真跑）：好摘要 0.95~1.00；阳性对照（没有角色块）约 0.1。
#: ⇒ 阈值取 **0.8**：漏掉两成主要角色要挡住，但别把「漏了一个次要角色」也判死。
MIN_CHARACTER_COVERAGE = 0.8

#: 证据缺失率超过此值 ⇒ 整次判失败（不是「丢几条没关系」）。
#:
#: 一两条缺证据很正常（模型把引文写错了）；**成片**缺证据说明模型在编，
#: 这时摘要的其它部分也不能信。
#:
#: ## 校准依据（2026-11-02 真书实测）
#:
#: 口径改对之后（引文长度门槛按「能不能定位」而不是「多长」判，
#: 章号挂错改成**重新归属**而不是丢弃），这本真书上的丢弃率：
#:
#: | 版本 | 丢弃率 | 说明 |
#: |---|---|---|
#: | 跑 4 | 21.1% | 25/31 条是「引文太短」误杀 —— 门槛改对之后没这些了 |
#: | 跑 5 | **13.9%** | 提示词要求「原样复制 8~25 字」之后 |
#: | 阳性对照 | 100% | 整份编造的摘要 ⇒ 必须被清空 |
#:
#: ⇒ 阈值取 **0.25**：正常摘要稳过，整份编造的（100%）远远越线。
MAX_EVIDENCE_DROP_RATIO = 0.25

#: 参与覆盖判定的候选事件条数（从原文启发式抽取，按章均匀取样）。
MAINLINE_CANDIDATES = 40
#: 参与覆盖判定的角色名条数（按提及次数降序）。
CHARACTER_CANDIDATES = 20

#: 摘要的**最小体量**（字）：结构化摘要再精简也有这个量级（三块标题 + 几条内容）。
#:
#: 为什么压缩比要有这条下限：10:1 是给「几十万字的书」定的目标；
#: 一本一千字的书压成 100 字，那不是压缩，那是**把内容删了**。
#: ⇒ 压缩比判据是「输出 ≤ max(输入/目标比, 本下限)」。
MIN_DIGEST_CHARS = 200

#: 覆盖判定里，一个 2-gram 词组算「有区分度」的上限：它在原文句子里的
#: 出现比例超过这个值 ⇒ 到处都有的套话，不算证据。
DISTINCTIVE_DF_RATIO = 0.1
#: 覆盖判定要求命中**几个**有区分度的词组。
#:
#: 为什么是 2 个而不是 1 个：摘要是**改写**，逐字比对必然大量漏判；
#: 但只命中 1 个词组也可能是巧合（「矿道」「转身」这种到处都有）。
COVER_MIN_TERMS = 2

#: 每条摘要条目的**输出预算**（token）。
#:
#: 为什么要有这个数：摘要的输出体量由**条目数**决定，与上下文上限是两回事 ——
#: 输入可以是一整本 66 万字的书，输出可以是三万 token。
#: ⇒ 输出预算必须**按工作量推导**，不能写死一个数
#: （写死的后果：条目变多时响应被拦腰截断，JSON 解析直接失败）。
#:
#: 取值依据：一条时间线条目 ≈ 170 字（故事内时间 + 事件 + 角色 + 引文 + JSON 字段名），
#: 中文约 0.7 token/字 ⇒ 约 120 token；角色条目更重（身份/经历/关系/说话特征/引文），
#: 反应条目居中 ⇒ 取平均 **160 token/条**。
TOKENS_PER_DIGEST_ENTRY = 160
#: 输出预算的下限（太小的任务给太少会让模型截断）。
MIN_DIGEST_OUTPUT_TOKENS = 8_000
#: 输出预算的上限（渠道侧也有上限，别把一次请求的输出预算开成天文数字）。
MAX_DIGEST_OUTPUT_TOKENS = 64_000

#: 引文在**邻近**的章里找到 ⇒ 重新归属（改章号、留条目），而不是丢弃。
#:
#: 窗口取 10 章：邻近 ⇒ 大概率是章节编号口径不同
#: （2026-11-02 真跑实测：第一次跑偏移多为 ±1，第二次跑出现 ±27/±33 的远距离挂错）。
#:
#: 为什么不是「全书任意章都能改挂」：那等于放弃定位 ——
#: 引文在书里的**某个**角落出现，说明它不是编的；但若它离声称的章太远，
#: 那这条摘要说的到底是不是那一处的事，就说不清了 ⇒ 丢掉但**留下样本**。
EVIDENCE_REATTRIBUTE_WINDOW = 10
#: 引文短于这个字数 ⇒ **不能单独作为证据**（只防空引文与单字）。
#:
#: ## 为什么不是「越长越算数」（2026-11-02 第四次真跑实测）
#:
#: 第四轮跑丢了 31 条，逐条回查：**25 条是「引文太短」**，
#: 而这些短引文**确实逐字出现在它声称的那一章里**
#: （第五章那句三字断言在第 17 章、另一句在第 30 章）。
#:
#: ⇒ 之前把门槛定成 6 字，等于把「能定位到具体一章的证据」误杀了。
#:
#: 证据的强度取决于**定位**（这段话确实出现在这一章），不取决于长度 ——
#: 一句 4 个字的话如果**只**出现在那一章，它就是有效证据；
#: 反过来，一句 40 字的话如果满书都是，它也定位不了任何东西。
#:
#: ⇒ 门槛只保留 3 字（防空引文与单字）。
EVIDENCE_MIN_QUOTE_CHARS = 3

MODE_AUTO = "auto"
MODE_WHOLE = "whole"
MODE_RETRIEVAL = "retrieval"

#: 引文最长多少字（引文是用来**定位**的，不是用来复述的）。
MAX_QUOTE_CHARS = 60

#: 句末标点（切句用）。
_SENT_END = re.compile(r"(?<=[。！？!?…])")
#: 动作动词（粗判）：句子里有这些字才算「做了一件事」。
#:
#: 与 `roleplay/bio.py` 的判据同源（那里是「他做了一件事」，这里少了主语限制）。
_ACTION_HINT = re.compile(
    r"(走|跑|牵|骑|拔|挥|砍|刺|抬|低|转|看|望|站|坐|跪|倒|拾|捡|拿|递|送|还|退|进|"
    r"冲|扑|抓|握|推|拉|按|拍|摸|抚|叹|笑|喊|叫|答|问|开口|起身|伸手|摇头|点头|"
    r"杀|死|救|追|逃|闯|闯进|离开|回来|发现|决定|答应|拒绝|逃出)"
)
#: 一句话的长度区间（字）。太短没信息，太长是叙述而不是「一件事」。
_SENT_MIN_CHARS = 12
_SENT_MAX_CHARS = 50
#: 句子的**中文实体量**下限（汉字数，不含空白与标点）。
#:
#: 为什么不用 `len(s)`：靠空白与标点撑起来的对话碎片
#: （一段只有引号和空白的台词）长度够但**没有内容** ⇒ 会混进主线候选集。
_SENT_MIN_CJK = 8
#: 汉字占整句的比例下限（低于此值 ⇒ 是碎片，不是叙述）。
_SENT_CJK_RATIO = 0.6
#: 汉字串（用来数「中文实体量」）。
_CJK_RUN = re.compile(r"[一-鿿]")

#: 章号标记（整本投喂时给模型的分段锚点）。
_CHAPTER_MARK = re.compile(r"^\[第\s*(\d+)\s*章\]")


@dataclass(frozen=True)
class DigestCaps:
    """一次摘要的**条目上限**（随书的大小缩放）。

    为什么上限要随书缩放：压缩比是「输出 / 输入」，
    书越大，同样的条目数压缩得越狠（信息更稀）；
    书太小，条目数不缩放就压不到 10:1。
    ⇒ 条目数按「输入字数 / 每条覆盖多少字」推导，再夹在合理区间里。
    """

    timeline: int
    characters: int
    reactions: int

    def to_public_dict(self) -> dict[str, int]:
        return {
            "timeline": self.timeline,
            "characters": self.characters,
            "reactions": self.reactions,
        }


def digest_caps(chars_in: int) -> DigestCaps:
    """按输入字数推导条目上限。"""
    chars = max(int(chars_in), 1)
    return DigestCaps(
        timeline=max(20, min(200, chars // 3000)),
        characters=max(8, min(60, chars // 12000)),
        reactions=max(12, min(120, chars // 4000)),
    )


def digest_output_budget(caps: DigestCaps) -> int:
    """按**条目数**推导这一次的输出预算（token）。

    为什么不能写死：摘要的输出体量由条目数决定，而条目数随书变大
    （66 万字 ⇒ 200 + 55 + 120 = 375 条）。
    写死 32,000 token 的话，这一档几乎必然撞上限 ⇒ 响应被拦腰截断 ⇒
    JSON 解析失败 ⇒ 整次摘要作废（而且**看不出**是被截断的）。

    ⇒ 预算跟着工作量走，和 P1 的「调用预算按工作量推导」是同一条纪律。
    """
    entries = max(
        int(caps.timeline) + int(caps.characters) + int(caps.reactions), 1
    )
    raw = entries * TOKENS_PER_DIGEST_ENTRY
    return max(MIN_DIGEST_OUTPUT_TOKENS, min(MAX_DIGEST_OUTPUT_TOKENS, raw))


def _even_trim(items: list, cap: int) -> list:
    """把列表**均匀**取样到 cap 条（不是「保留前 N 条」）。

    为什么不能直接切前 N 条：切块档合并后条目数会超过上限，
    而「保留前 N 条」等于**只留前半本书** —— 末尾几十章在摘要里整段消失，
    而界面上看不出任何异常（条目数达标、验收也过了）。
    ⇒ 均匀取样：覆盖全书，且**确定性**（同一份输入永远取到同一批）。
    """
    if cap <= 0 or len(items) <= cap:
        return list(items)
    step = len(items) / cap
    return [items[int(i * step)] for i in range(cap)]


def _apply_caps(parsed: dict, caps: DigestCaps) -> dict:
    """把条目数压到上限（整本档用 —— 提示词里的条数上限只是**建议**，
    模型可能给超；不给它兜底的话，条目数与验收口径就对不上了）。"""
    out = dict(parsed)
    out["timeline"] = _even_trim(list(parsed.get("timeline") or []), caps.timeline)
    out["characters"] = _even_trim(list(parsed.get("characters") or []), caps.characters)
    out["reactions"] = _even_trim(list(parsed.get("reactions") or []), caps.reactions)
    return out


def choose_digest_mode(
    chars_in: int,
    provider: Any = None,
    requested: str = MODE_AUTO,
) -> tuple[str, str]:
    """选抽取档位，返回 `(mode, 理由)`。

    理由会一路带到界面上：用户必须知道「这一轮是整本投喂还是切块抽」，
    以及**为什么**这么选 —— 「自动」两个字没有任何信息量。

    ## 判据

    - 显式指定（`whole` / `retrieval`）⇒ 照办，理由写「用户指定」。
    - 自动 ⇒ 门槛**跟随渠道声明**（`provider.max_context_chars`）：
      渠道必须**声明**过能吃下这么多字，且这本书确实在它能吃下的量级内。
      没声明就当它吃不下 —— 假设错了的后果是「请求发出去了、回来被截断、
      我们以为压缩成功了」，这是本仓库反复踩的那类静默失效。
      再加一道与渠道无关的绝对天花板（`WHOLE_BOOK_ABSOLUTE_MAX_CHARS`）兜住渠道误配。

    ## 为什么门槛跟随渠道，而不是写死一个数（2026-11-02 真跑前改）

    原来写死「书 ≤ 30 万字」。真跑前先把本机的真书量了一遍才发现：
    **最小的真书也有 43.6 万字**（十八本里最小的一本）⇒ 写死的门槛让
    「整本一次投喂」这条路径在真书上**永远不可达**。

    而「整本」正是摘要相对「切块抽」的全部意义所在 ——
    切块抽回答不了「第 3 章和第 500 章的关系变化」这种跨章问题。

    ⇒ 门槛必须由**真实约束**（渠道能吃下多少字）推导，而不是由一个拍脑袋的数推导。
    """
    if requested and requested != MODE_AUTO:
        if requested not in (MODE_WHOLE, MODE_RETRIEVAL):
            raise ValueError(f"未知的摘要抽取档位：{requested!r}")
        return requested, "用户指定"

    limit = int(getattr(provider, "max_context_chars", 0) or 0)
    if limit <= 0:
        return MODE_RETRIEVAL, (
            f"渠道未声明单次可读的字数上限 ⇒ 不敢整本投喂"
            f"（全书 {chars_in:,} 字）⇒ 切块抽取后合并"
        )
    threshold = min(limit, WHOLE_BOOK_ABSOLUTE_MAX_CHARS)
    if chars_in > threshold:
        if threshold == limit:
            why = f"全书 {chars_in:,} 字超过渠道单次可读的 {limit:,} 字"
        else:
            why = (
                f"全书 {chars_in:,} 字超过整本投喂的绝对天花板 "
                f"{WHOLE_BOOK_ABSOLUTE_MAX_CHARS:,} 字"
                f"（渠道虽声明 {limit:,} 字，但那是渠道自己报的数）"
            )
        return MODE_RETRIEVAL, f"{why} ⇒ 切块抽取后合并"
    return MODE_WHOLE, (
        f"全书 {chars_in:,} 字 ≤ 渠道单次可读的 {limit:,} 字 ⇒ 整本一次投喂"
        "（模型看得到全局，跨章的问题才答得出来）"
    )


# ====================================================================
# 提示词与解析
# ====================================================================


def build_digest_prompt(
    book_text: str,
    *,
    caps: DigestCaps,
    whole: bool,
    chunk_label: str | None = None,
) -> tuple[str, str]:
    """构造摘要提示词，返回 `(system, user)`。

    `whole=True` 时 `book_text` 是整本书（章号用 `[第N章]` 标记）；
    `whole=False` 时是一块语料（同样带章号标记），此时只抽**这一块里**的事。

    提示词里最要紧的三条：

    1. **只输出 JSON**（不要解释、不要 markdown 围栏）；
    2. **每条都要证据**，且引文必须是原文**逐字**片段（≤ 60 字）；
    3. **不许编**：原文没有的名字、事件、关系一律不写 ——
       没把握就不写这一条，比写一条编的好。
    """
    system = (
        "你是小说编辑，负责把一本书压成结构化摘要。只输出一个 JSON 对象，"
        "不要解释、不要 markdown 围栏、不要任何多余文字。\n"
        "JSON 结构（三个数组，字段名不要改）：\n"
        '{"timeline": [{"seq": 1, "chapter_no": 12, "in_story_time": "故事内时间", '
        '"event": "发生了什么", "actors": ["角色名"], "weight": 0.8, '
        '"evidence": [{"chapter_no": 12, "quote": "原文逐字片段"}]}], '
        '"characters": [{"name": "角色名", "role": "身份", "arc": "经历主线", '
        '"key_events": [12, 88], "relations": [{"name": "另一个角色", "kind": "关系", '
        '"changed_at": 88}], "voice": {"avg_len": 6, "habits": ["说话习惯"]}, '
        '"evidence": [{"chapter_no": 88, "quote": "原文逐字片段"}]}], '
        '"reactions": [{"character": "角色名", "trigger": "他面对什么事", '
        '"says": "他会怎么说", "does": "他会怎么做", '
        '"evidence": [{"chapter_no": 207, "quote": "原文逐字片段"}]}]}\n'
        "硬要求：\n"
        "1. timeline 按**因果顺序**给 seq（不是叙述顺序）；in_story_time 写故事内时间"
        "（如「开篇前三年」），拿不准就写空字符串。\n"
        "2. 每一项都要带 evidence，quote 必须是给定原文里**逐字存在**的片段。\n"
        f"   - **长度 8~25 字，越短越好**：引文是用来**定位**的，"
        "不是用来复述的。\n"
        "   - 必须**原样复制**：一个字、一个标点都不要改、不要补、不要加省略号。\n"
        "   - 不要写「大概意思是……」这种改写句 —— 改写过的句子在原文里找不到，"
        "这一条会被整条丢掉。\n"
        "   - 证据.chapter_no 写这段引文**真正出现**的那一章；拿不准就写事件所在的那一章，"
        "我们会自己核对并改正。\n"
        "3. 原文里没有的名字、事件、关系一律不写。宁可少写一条，也不要编。\n"
        "4. 角色名用原文里的称呼，不要改写、不要加称号。\n"
        f"5. 条数上限：timeline ≤ {caps.timeline}，characters ≤ {caps.characters}，"
        f"reactions ≤ {caps.reactions}。"
    )
    scope = "整本书" if whole else f"这一块语料（{chunk_label}）"
    user = (
        f"下面是{scope}的原文（每章以 [第N章] 开头）。请按系统消息里的 JSON 结构输出摘要。\n\n"
        f"{book_text}"
    )
    return system, user


def _extract_json(raw: str) -> Any:
    """从模型输出里取出 JSON 对象。

    模型常犯的错：包一层 ```json 围栏、前后加一句「好的，以下是摘要」。
    这两种都不是「格式错误」而是「可解析的噪声」⇒ 先剥围栏再解析。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("模型没有返回任何内容")
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出里找不到 JSON 对象")
    return json.loads(text[start : end + 1])


def parse_digest(raw: str) -> dict:
    """把模型输出解析成摘要结构（**只做结构校验，不做证据校验**）。

    证据校验在 `enforce_evidence`：结构合法但引文是编的，结构解析是拦不住的。
    """
    data = _extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("摘要必须是 JSON 对象")
    out: dict[str, Any] = {
        "digest_version": DIGEST_VERSION,
        "timeline": [],
        "characters": [],
        "reactions": [],
    }
    for key in ("timeline", "characters", "reactions"):
        items = data.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            raise ValueError(f"摘要的 {key} 必须是数组")
        out[key] = [it for it in items if isinstance(it, dict)]
    return out


# ====================================================================
# 证据强制
# ====================================================================


def _norm(text: str) -> str:
    """归一化：去掉空白与常见装饰符号，便于逐字比对。"""
    return re.sub(r"[\s\u3000·．。，、！？!?：:；;\"'“”‘’（）()\[\]【】…—\-]+", "", text or "")


def _clean_quote(quote: str) -> str:
    """把引文清理成可比对的片段（去掉章号标记与省略号）。"""
    q = (quote or "").strip()
    q = _CHAPTER_MARK.sub("", q).strip()
    q = q.replace("……", "").replace("...", "").strip()
    return q[:MAX_QUOTE_CHARS]


def _quote_in_chapter(quote: str, chapter_text: str) -> bool:
    """引文是否**逐字**出现在这一章里。

    允许两种情况：
    - 完整出现；
    - 引文被模型截断（尾部少了几个字）⇒ 前缀在章里就算数。
      否则模型每改一个标点我们就会丢掉一条真条目。

    ⚠️ 长度门槛只有 3 字（`_quote_in_chapter` 内部）：
    第四轮真跑实测，被丢的 31 条里 **25 条是「引文太短」**，
    而那些短引文**确实逐字出现在它声称的那一章**（一句三字断言落在第 17 章）。
    ⇒ 证据的强度取决于**能不能定位到那一章**，不取决于引文多长。
    """
    q = _norm(_clean_quote(quote))
    if len(q) < 3:
        return False
    body = _norm(chapter_text)
    if q in body:
        return True
    for cut in (12, 8):
        if len(q) > cut and q[:cut] in body:
            return True
    return False


def _find_quote_chapter(
    quote: str, chapters: dict[int, str], near: int, window: int
) -> int | None:
    """在 `near` 附近 ±window 章里找引文，返回**真正**所在的章号（找不到 None）。

    找不到最近的那章优先（离得越近越可能是编号口径差）。
    """
    best: tuple[int, int] | None = None
    for ch in sorted(chapters):
        if abs(ch - near) > window or ch == near:
            continue
        if _quote_in_chapter(quote, chapters[ch]):
            dist = abs(ch - near)
            if best is None or dist < best[0]:
                best = (dist, ch)
    return best[1] if best else None


def _quote_usable(quote: str) -> bool:
    """这段引文**能不能当证据**（长度够不够定位）。"""
    return len(_norm(_clean_quote(quote))) >= EVIDENCE_MIN_QUOTE_CHARS


def _quote_exists_anywhere(quote: str, chapters: dict[int, str]) -> bool:
    """这段引文在**书里的任何一章**是否存在（用于区分「编的」与「挂太远」）。"""
    if not _quote_usable(quote):
        return False
    for text in chapters.values():
        if _quote_in_chapter(quote, text):
            return True
    return False


def _entry_evidence_reason(entry: dict, chapters: dict[int, str]) -> str:
    """这条摘要的证据为什么站不住（返回原因码；`"ok"` = 站得住）。

    ## 为什么要有「原因」而不只是「丢弃」

    2026-11-02 第一次真跑（66 万字真书）丢了 101/147 条（68.7%）。
    只报一个数字，用户没法判断这是「模型在编」还是「引文写错了一个标点」——
    而这两种情况的处置完全相反：
    前者要收紧提示词或换模型，后者要放宽引文匹配。

    ⇒ 丢弃时**记下原因**与样本，让界面能回答「为什么丢了这么多」。

    ## 「章号挂错」要**重新归属**，不是丢弃（2026-11-02 真跑实测）

    第二次真跑丢了 34 条，逐条回查原文后发现：**全部**是
    「引文逐字就在书里，只是挂错了章」（实测偏移多为 ±1）。

    引文是**证据**，章号只是**指针**。指针错了应该修指针，
    把证据一起扔掉等于「因为目录页码错了，就说这本书没写过这段」。

    ⇒ 邻近章里找得到就**改章号、留条目**；离得太远（超出窗口）
    仍然丢掉（那种情况条目说的事件与引文出处很可能不是同一处），
    但会记成 `quote_far`（区别于 `quote_not_found` = 压根不是书里的话）。
    """
    evidence = entry.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return "no_evidence"
    saw_chapter = False
    saw_quote = False
    saw_short = False
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        ch = ev.get("chapter_no")
        quote = ev.get("quote")
        if not isinstance(ch, int) or isinstance(ch, bool):
            continue
        saw_chapter = True
        if not isinstance(quote, str) or not quote.strip():
            continue
        saw_quote = True
        if not _quote_usable(quote):
            # 空引文/单字 ⇒ 当不了证据，但也许还有别的引文能救这条。
            saw_short = True
            continue
        text = chapters.get(ch)
        if text and _quote_in_chapter(quote, text):
            # 本章逐字存在 ⇒ 证据成立（**不论长短**：能定位到具体一章就是证据）。
            return "ok"
        found = _find_quote_chapter(quote, chapters, near=ch, window=EVIDENCE_REATTRIBUTE_WINDOW)
        if found is not None:
            # 就地改章号 ⇒ 摘要里的位置信息也变准了
            ev["chapter_no"] = found
            return "reattributed"
        if _quote_exists_anywhere(quote, chapters):
            # 引文**确实在书里**，只是离得太远 ⇒ 条目说的事与引文出处可能不是同一处。
            return "quote_far"
    if not saw_chapter:
        return "no_chapter_no"
    if not saw_quote:
        return "no_quote"
    if saw_short:
        return "quote_too_short"
    return "quote_not_found"


def _entry_evidence_ok(entry: dict, chapters: dict[int, str]) -> bool:
    """这条摘要的证据是否站得住：章号存在 + 引文逐字在那一章里。"""
    return _entry_evidence_reason(entry, chapters) == "ok"


def _entry_sample(entry: dict) -> dict:
    """取一条被丢弃条目的**样本**（引文 + 章号），供界面展示。"""
    evidence = entry.get("evidence")
    quotes: list[str] = []
    chapters: list[Any] = []
    if isinstance(evidence, list):
        for ev in evidence:
            if not isinstance(ev, dict):
                continue
            chapters.append(ev.get("chapter_no"))
            if isinstance(ev.get("quote"), str) and ev.get("quote").strip():
                quotes.append(ev["quote"].strip()[:60])
    return {
        "text": str(
            entry.get("event") or entry.get("name") or entry.get("character") or ""
        )[:80],
        "quotes": quotes[:2],
        "chapters": chapters[:3],
    }


def enforce_evidence(
    digest: dict, chapters: dict[int, str]
) -> tuple[dict, dict]:
    """丢掉没有真实证据的条目，返回 `(新摘要, 统计)`。

    ## 为什么是「丢弃」而不是「标注存疑」

    摘要的价值在于「它说的事真的发生过」。
    标着「存疑」的条目照样会被模型读进上下文、照样会被当成事实 ——
    标注只是给人看的，模型不看。
    ⇒ 没证据的条目直接不进摘要。

    统计里给出 `dropped_ratio`：成片缺失说明模型在编（见
    `MAX_EVIDENCE_DROP_RATIO`），此时整次摘要判失败。
    """
    stats = {
        "kept": 0,
        "dropped": 0,
        "dropped_by_block": {},
        # 「章号挂错、已重新归属」的条数（证据是真的，只是指针错了）。
        "reattributed": 0,
        # 丢弃**原因**分布 + 样本：让界面能回答「为什么丢了这么多」
        # （模型在编 vs 引文写错一个标点 —— 处置完全相反）。
        "drop_reasons": {},
        "drop_samples": {},
    }
    new: dict[str, Any] = {
        "digest_version": digest.get("digest_version", DIGEST_VERSION),
        "timeline": [],
        "characters": [],
        "reactions": [],
    }
    for key in ("timeline", "characters", "reactions"):
        kept = []
        dropped = 0
        for entry in digest.get(key) or []:
            reason = _entry_evidence_reason(entry, chapters)
            if reason == "ok" or reason == "reattributed":
                if reason == "reattributed":
                    stats["reattributed"] += 1
                kept.append(entry)
                continue
            dropped += 1
            stats["drop_reasons"][reason] = stats["drop_reasons"].get(reason, 0) + 1
            samples = stats["drop_samples"].setdefault(reason, [])
            if len(samples) < 5:
                samples.append(_entry_sample(entry))
        new[key] = kept
        stats["kept"] += len(kept)
        stats["dropped"] += dropped
        if dropped:
            stats["dropped_by_block"][key] = dropped
    stats["dropped_ratio"] = (
        stats["dropped"] / max(stats["kept"] + stats["dropped"], 1)
    )
    return new, stats


# ====================================================================
# 渲染与验收
# ====================================================================


def render_digest_text(digest: dict) -> str:
    """把摘要渲染成**可读文本**（注入提示词 / 算字数都用它）。

    结构化 JSON 是**存储**形态；进上下文的是渲染后的文本 ——
    模型读散文比读嵌套 JSON 更容易用上这些事实。
    """
    lines: list[str] = []
    timeline = digest.get("timeline") or []
    if timeline:
        lines.append("【故事时间线】")
        for item in timeline:
            who = "、".join(item.get("actors") or [])
            when = item.get("in_story_time") or ""
            head = f"{when}｜" if when else ""
            lines.append(f"{head}{item.get('event', '')}" + (f"（{who}）" if who else ""))
    characters = digest.get("characters") or []
    if characters:
        lines.append("【主要角色】")
        for c in characters:
            bits = [c.get("name", ""), c.get("role") or "", c.get("arc") or ""]
            line = "：".join(b for b in bits if b)
            relations = c.get("relations") or []
            if relations:
                rel = "、".join(
                    f"{r.get('name', '')}（{r.get('kind', '')}）"
                    for r in relations if isinstance(r, dict)
                )
                if rel:
                    line += f"；关系：{rel}"
            voice = c.get("voice") or {}
            habits = voice.get("habits") if isinstance(voice, dict) else None
            if habits:
                line += f"；说话习惯：{'、'.join(str(h) for h in habits)}"
            lines.append(line)
    reactions = digest.get("reactions") or []
    if reactions:
        lines.append("【情境反应】")
        for r in reactions:
            parts = [r.get("character", ""), r.get("trigger", "")]
            says = r.get("says")
            does = r.get("does")
            if says:
                parts.append(f"会说「{says}」")
            if does:
                parts.append(f"会{does}")
            lines.append("｜".join(p for p in parts if p))
    return "\n".join(lines)


def _candidate_events(
    chapters: dict[int, str], limit: int = MAINLINE_CANDIDATES,
    main_characters: list[str] | None = None,
) -> list[str]:
    """从原文里挑「像一件事」的句子，作为**主线的候选集合**。

    判据与 `roleplay/bio.py` 同源：有动作动词、长度适中、每章至多一条。
    按章号**均匀**取样，保证覆盖全书（不是只取开头那几章）。

    ## 「主线」的定义（2026-11-02 真跑后改的）

    原来只有两条判据（动作动词 + 长度），实测取出来的东西**不是主线**：
    这本真书 66 万字取 40 条，里面有
    「一整句没人出现的风景描写」（纯风景）
    「一句只有引号的对话碎片」（对话碎片）。

    ⇒ 加一条：**句子里必须出现至少一个主要角色**。
    「涉及主要角色的事」才是主线；不涉及主要角色的句子（风景、路人）
    本来就不该被算成「摘要必须覆盖的东西」。

    ⇒ 另加一条：**中文实体量**下限（按汉字数算，不按字符数）。
    原来按 `len(s)` 算，于是「一段只有引号和空白的对话碎片」这种
    靠空白撑长度的对话碎片也能过关。

    这是**启发式**的候选集，不是真值 —— 它的用途是当闸门
    （摘要把主线压丢了要能被挡住），不是当标准答案。
    """
    if not chapters:
        return []
    nos = sorted(chapters)
    picked: list[str] = []
    per_chapter: dict[int, str] = {}
    names = [n for n in (main_characters or []) if n]
    for ch in nos:
        text = chapters.get(ch) or ""
        for sent in _SENT_END.split(text):
            s = sent.strip()
            cjk = len(_CJK_RUN.findall(s))
            if not (_SENT_MIN_CJK <= cjk <= _SENT_MAX_CHARS):
                continue
            # 中文实体量占位（空白与标点撑起来的碎片要排除掉）
            if cjk < len(s) * 0.6:
                continue
            if not _ACTION_HINT.search(s):
                continue
            if names and not any(n in s for n in names):
                continue
            per_chapter[ch] = s
            break
    if not per_chapter:
        return []
    keys = sorted(per_chapter)
    step = max(len(keys) / limit, 1)
    idx = sorted({min(int(i * step), len(keys) - 1) for i in range(min(limit, len(keys)))})
    for i in idx:
        picked.append(per_chapter[keys[i]])
    return picked


#: 候选角色的**长度范围**（中文字符）：2-4 字是中文姓名常态。
_NAME_MIN_LEN = 2
_NAME_MAX_LEN = 4


def _text_character_names(
    chapters: dict[int, str], limit: int = CHARACTER_CANDIDATES
) -> list[str]:
    """从**纯文本**里取主要角色名（没有图谱时的兜底口径）。

    ## 为什么不能直接用 PMI 候选（2026-11-02 真跑时实测）

    直接取 PMI 高频词，这本真书（66 万字）取出来的是：
    「主角的姓、主角的名、什么、自己、没有、怎么、知道、起来、加上带人名的整句」。

    前两组是**碎片**（同一个人的姓与名被切开统计），
    后几组是**虚词**。拿这 20 个当「角色覆盖」的分母，
    一个写得很好的摘要也会被判不达标（实测 0.65）⇒ 闸门没有判别力。

    ## 现在的口径（**复用仓库既有的判据**，不另写一套）

    1. **召回**：KG 抽取器的两条人名信号 ——
       `_SPEAK_RE`（名字 + 说话动词，带「前面不能是汉字」的负向断言）与
       `_leading_name`（句首主语位），再用 `_valid_name` 过**正向形态**
       （黑名单 + 词形规则）。虚词过不了这一关。
    2. **碎片归一**：`_resolve_name_artifacts`（语料自证的包含关系）——
       「主角姓」只由「主角全名」支撑 ⇒ 归一/丢弃。
    3. **伪角色清洗**：`_is_fake_character`（虚词黑名单 + 集合性实体词形规则）。

    三步全是**纯统计**，零模型调用、零外部依赖。
    """
    if not chapters:
        return []
    text = "\n".join(chapters.values())
    if not text:
        return []

    try:
        from app.kg.extractor import _SPEAK_RE, _SEG_RE, _leading_name, _valid_name
        from app.style.narrative import (
            _enhance_names_with_corpus,
            _is_fake_character,
            _resolve_name_artifacts,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("人名判据不可用：%s", exc)
        return []

    counts: dict[str, int] = {}

    def _bump(name: str) -> None:
        if name and _valid_name(name):
            counts[name] = counts.get(name, 0) + 1

    for m in _SPEAK_RE.finditer(text):
        _bump(m.group(1))
    for sent in _SEG_RE.findall(text):
        try:
            _bump(_leading_name(sent))
        except Exception:  # noqa: BLE001
            continue

    if not counts:
        return []
    pairs = sorted(counts.items(), key=lambda kv: -kv[1])
    alias = _resolve_name_artifacts(pairs, text)

    # 碎片补全（语料自证）：「主角姓」→「主角全名」、「同门姓」→「同门全名」。
    #
    # 为什么必须做：`_SPEAK_RE` / `_leading_name` 切出来的**最多 3 字**
    # （见 extractor 的正则），所以 4 字真名进不了候选池 ⇒
    # 「主角姓」和「主角全名」会同时存在，而「主角姓」在摘要文本里
    # 只是「主角全名」的子串 ⇒ 覆盖判定会被这些碎片搅浑
    # （摘要写「同门全名」，分母里只剩「同门姓」就永远对不上）。
    #
    # ⇒ 用仓库既有的 `_enhance_names_with_corpus`（向前补尾字，失败再向后补首字）
    # 把碎片补成规范全名。补不上的保持原样。
    try:
        enhanced = _enhance_names_with_corpus(list(counts), text)
    except Exception:  # noqa: BLE001
        enhanced = {}

    merged: dict[str, int] = {}
    for name, c in pairs:
        target = alias.get(name, name)
        if target is None:
            continue
        target = enhanced.get(target, target)
        if _is_fake_character(target):
            continue
        merged[target] = max(merged.get(target, 0), c)

    out = [n for n, _ in sorted(merged.items(), key=lambda kv: -kv[1]) if n]
    return out[:limit]


def _candidate_names(
    db, novel_id: int, chapters: dict[int, str], limit: int = CHARACTER_CANDIDATES
) -> list[str]:
    """原文里的高频角色名（覆盖判定的分母）。

    优先用**知识图谱**里的 character 节点（图谱是仓库既有的事实层）；
    没有图谱时退回**纯文本口径**（`_text_character_names`，纯统计、零依赖）。

    两条路都拿不到时返回空列表 ⇒ 角色覆盖这一项判「**不适用**」，
    而不是判 0 分（那会把一个其实合格的摘要误杀）。
    """
    names: list[str] = []
    try:
        from sqlalchemy import select

        from app.models import KgNode

        rows = db.scalars(
            select(KgNode).where(
                KgNode.source == f"novel:{int(novel_id)}",
                KgNode.type == "character",
            )
        ).all()
        for node in rows:
            props = node.props or {}
            name = props.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    except Exception as exc:  # noqa: BLE001 —— 图谱不可用不该让验收崩
        logger.warning("读参考语料图谱失败（角色候选退回统计口径）：%s", exc)

    if not names and chapters:
        try:
            names = _text_character_names(chapters, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("统计口径的角色候选也没取到：%s", exc)

    # 伪角色清洗：图谱里的 character 节点混着大量虚词（「时间/相信/任何」）。
    try:
        from app.style.narrative import _FAKE_CHARS

        names = [n for n in names if n not in _FAKE_CHARS]
    except Exception:  # noqa: BLE001
        pass

    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
        if len(out) >= limit:
            break
    return out


def _bigrams(text: str) -> set[str]:
    """文本的 2-gram 词组集合（只取中文连续段）。

    为什么用 2-gram 而不是分词：中文没有空格，分词器会把「矿脉塌方」切成
    「矿脉/塌方」，而**事件最有区分度的恰恰是这种复合词**。
    2-gram 是最省事又够用的折中。
    """
    out: set[str] = set()
    for run in re.findall(r"[一-鿿]+", text or ""):
        for i in range(len(run) - 1):
            out.add(run[i : i + 2])
    return out


def _distinctive_terms(text: str, df: dict[str, int], n_sentences: int) -> set[str]:
    """文本里「有区分度」的 2-gram（原文里到处都有的套话不算）。"""
    threshold = max(1, int(n_sentences * DISTINCTIVE_DF_RATIO))
    return {g for g in _bigrams(text) if df.get(g, 0) <= threshold}


def _corpus_df(chapters: dict[int, str]) -> tuple[dict[str, int], int]:
    """全文的 2-gram 文档频率（按句计）与句子数。"""
    df: dict[str, int] = {}
    n_sentences = 0
    for text in chapters.values():
        for sent in _SENT_END.split(text or ""):
            s = sent.strip()
            if not s:
                continue
            n_sentences += 1
            for gram in _bigrams(s):
                df[gram] = df.get(gram, 0) + 1
    return df, n_sentences


def _entry_texts(digest: dict) -> list[str]:
    """摘要里所有「可被检索到的」文本（时间线 / 角色 / 反应）。"""
    texts: list[str] = []
    for item in digest.get("timeline") or []:
        texts.append(" ".join(str(v) for v in item.values() if isinstance(v, (str, int))))
    for item in digest.get("characters") or []:
        texts.append(" ".join(str(v) for v in item.values() if isinstance(v, (str, int))))
    for item in digest.get("reactions") or []:
        texts.append(" ".join(str(v) for v in item.values() if isinstance(v, (str, int))))
    return texts


def verify_digest(
    digest: dict,
    chapters: dict[int, str],
    *,
    chars_in: int,
    target_ratio: float = TARGET_COMPRESSION,
    mainline_candidates: list[str] | None = None,
    character_names: list[str] | None = None,
    evidence_stats: dict | None = None,
) -> dict:
    """三项验收：压缩比 / 主线覆盖 / 角色覆盖。

    ## 口径（都是能自动判的，不需要人看）

    1. **压缩比** = `chars_out / chars_in`，要求 ≤ `1 / target_ratio`，
       但**有下限**：输出不得小于 `MIN_DIGEST_CHARS`
       （一千字的书压成 100 字不是压缩，是删内容）。
    2. **主线覆盖** = 候选事件里被摘要覆盖的比例。判据是**词组重合**：
       候选句与摘要文本共享 **≥ 2 个「有区分度的 2-gram」**（在原文里不是到处
       都有的套话）⇒ 算覆盖。

       为什么不是逐字比对：摘要是**改写**，逐字比对必然大量漏判；
       为什么不是语义判定：那要再调一次模型，闸门就不该有模型在里面。
       ⇒ 词组重合是折中，它能抓住「事件本身被压掉了」，也会漏判
       「换了同义词的重述」——这个局限写在文档里，不假装它是真相。

    3. **角色覆盖** = 高频角色名里出现在**渲染文本**中的比例（逐字包含）。
       用渲染文本而不是结构化条目，是为了把「关系里的人」（relations）
       也算进去 —— 老陶只出现在「沈砚与老陶是师徒」这条关系里，
       他同样是这本书的重要角色。

    任一项不达标 ⇒ `passed=False`，由调用方决定是拒绝还是告警。
    """
    text = render_digest_text(digest)
    chars_out = len(text)
    ratio = (chars_out / chars_in) if chars_in else 0.0
    max_out = max(chars_in / max(target_ratio, 1.0), MIN_DIGEST_CHARS)

    df, n_sentences = _corpus_df(chapters)
    candidates = (
        mainline_candidates if mainline_candidates is not None
        else _candidate_events(chapters)
    )
    digest_terms = _distinctive_terms(text, df, n_sentences)
    covered = 0
    for sent in candidates:
        terms = _distinctive_terms(sent, df, n_sentences)
        if len(terms & digest_terms) >= COVER_MIN_TERMS:
            covered += 1
    mainline_coverage = (covered / len(candidates)) if candidates else None

    names = character_names if character_names is not None else []
    char_covered = sum(1 for n in names if n and n in text)
    character_coverage = (char_covered / len(names)) if names else None

    checks = {
        "compression": {
            "ok": ratio > 0 and chars_out <= max_out,
            "applicable": True,
            "chars_in": int(chars_in),
            "chars_out": int(chars_out),
            "ratio": round(ratio, 4),
            "target_ratio": target_ratio,
            "max_chars_out": int(max_out),
        },
        "mainline_coverage": {
            # 「候选集为空」= 这一项**不适用**，不是「通过」。
            # ⚠️ 2026-11-02 真跑踩到：这里原来写 `ok = value is None or ...`
            # ⇒ 没有候选时 `ok=True`、`value=None` ⇒ 界面显示「通过」，
            # 而这一项其实**根本没跑**（角色候选那个 bug 就是这么空过的）。
            # ⇒ 三态：True / False / None（不适用），None 不算通过，
            #    但也不阻塞整体结论（否则「没有候选」会让每次摘要都判失败）。
            "ok": None if mainline_coverage is None else mainline_coverage >= MIN_MAINLINE_COVERAGE,
            "applicable": mainline_coverage is not None,
            "covered": covered,
            "candidates": len(candidates),
            "value": None if mainline_coverage is None else round(mainline_coverage, 4),
            "threshold": MIN_MAINLINE_COVERAGE,
        },
        "character_coverage": {
            "ok": None if character_coverage is None else character_coverage >= MIN_CHARACTER_COVERAGE,
            "applicable": character_coverage is not None,
            "covered": char_covered,
            "candidates": len(names),
            "value": None if character_coverage is None else round(character_coverage, 4),
            "threshold": MIN_CHARACTER_COVERAGE,
        },
    }
    if evidence_stats is not None:
        drop_ratio = float(evidence_stats.get("dropped_ratio", 0.0))
        checks["evidence"] = {
            "ok": drop_ratio <= MAX_EVIDENCE_DROP_RATIO,
            "applicable": True,
            "dropped": int(evidence_stats.get("dropped", 0)),
            "kept": int(evidence_stats.get("kept", 0)),
            # 「章号挂错、已重新归属」的条数：证据是真的，只是指针错了。
            "reattributed": int(evidence_stats.get("reattributed", 0)),
            "dropped_ratio": round(drop_ratio, 4),
            "threshold": MAX_EVIDENCE_DROP_RATIO,
            # 为什么丢了这么多（见 enforce_evidence）：模型在编 vs 引文写错一个标点，
            # 处置完全相反 ⇒ 必须能分开看。
            "drop_reasons": evidence_stats.get("drop_reasons") or {},
            "drop_samples": evidence_stats.get("drop_samples") or {},
            "dropped_by_block": evidence_stats.get("dropped_by_block") or {},
        }
    # 整体结论：**不适用**的项不算失败，但必须单独列出来 ——
    # 「三项里有两项跑了、一项没跑」和「三项全跑过」是两回事，
    # 界面上要能分开说（否则又是一次「看起来通过」）。
    not_applicable = sorted(k for k, c in checks.items() if c.get("ok") is None)
    return {
        "passed": all(c["ok"] is not False for c in checks.values()),
        "not_applicable": not_applicable,
        "checks": checks,
    }


# ====================================================================
# 编排
# ====================================================================


def _chunk_chapters(
    chapters: list[tuple[int, str]], block_chars: int
) -> list[list[tuple[int, str]]]:
    """按字数把章节装箱（与蒸馏同一套思路：按字不按章）。"""
    chunks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    size = 0
    for ch, text in chapters:
        length = len(text or "")
        if current and size + length > block_chars:
            chunks.append(current)
            current = []
            size = 0
        current.append((ch, text))
        size += length
    if current:
        chunks.append(current)
    return chunks


def _render_book(chapters: list[tuple[int, str]]) -> str:
    return "\n".join(f"[第{ch}章]\n{text}" for ch, text in chapters)


def digest_plan(
    db,
    novel_id: int,
    provider=None,
    *,
    mode: str = MODE_AUTO,
    target_ratio: float = TARGET_COMPRESSION,
) -> dict:
    """把这次摘要的**计划**算出来（**不调模型、不花钱**）。

    ## 为什么要有「计划」这一步

    复用判定必须发生在**花钱之前**。

    2026-11-02 浏览器核对时读出来的旧顺序是
    「先 `build_book_digest()`（整本一次模型调用）→ 再看要不要复用」，
    于是任务状态里那句「命中已有摘要，未重新抽取」是**假的**：
    模型已经跑完了，额度已经花了，只是产物没保存。
    真跑 6 次这本真书时每次都付了一次全本调用的钱，其中有一次纯属白花。

    ⇒ 把「这次要按什么参数抽」单独算出来（纯本地计算：读正文、选档位、算条目上限），
    先拿它当**复用键**去比对，命中就不调模型。

    ## 复用键包含什么

    `(正文哈希, 产物版本, 有效档位, 压缩比目标)`：

    - 正文变了（重新导入 / 清洗规则升级）⇒ 必须重算；
    - 产物结构变了（`DIGEST_VERSION` +1）⇒ 旧产物不是同一种东西，不能复用；
    - **档位变了**（整本 → 切块）⇒ 结果不同，必须重算；
    - **压缩比目标变了** ⇒ 条目上限不同，必须重算。

    后两条是旧实现漏掉的：旧代码只看正文哈希，于是「改了压缩比目标再跑」
    会被静默复用成旧产物 —— 参数形同虚设。

    Returns:
        dict: `chapters` / `chapter_map` / `chars_in` / `mode` / `mode_reason` /
        `caps` / `plan_key`。
    """
    from app.style.narrative import _load_novel_chapters

    chapters = _load_novel_chapters(db, int(novel_id))
    if not chapters:
        raise ValueError(f"语料 {novel_id} 没有正文，无法做全书摘要")
    chapter_map = {ch: text for ch, text in chapters}
    chars_in = sum(len(text or "") for _, text in chapters)
    chosen, reason = choose_digest_mode(chars_in, provider, mode)
    caps = digest_caps(chars_in)
    return {
        "chapters": chapters,
        "chapter_map": chapter_map,
        "chars_in": chars_in,
        "mode": chosen,
        "mode_reason": reason,
        "caps": caps,
        "plan_key": {
            "mode": chosen,
            "target_ratio": float(target_ratio),
        },
    }


def build_book_digest(
    db,
    novel_id: int,
    provider=None,
    *,
    mode: str = MODE_AUTO,
    progress: Callable[[str, int, int], None] | None = None,
    target_ratio: float = TARGET_COMPRESSION,
    plan: dict | None = None,
) -> dict:
    """给一本书做结构化摘要，返回可直接落库的产物。

    流程：选档 → 取正文 → 抽取（整本 / 切块）→ 证据强制 → 验收 → 落字段。

    `progress(stage, current, total)` 的单位是**块**（整本档是 1 块）。

    `plan` 可以由调用方先算好传进来（`digest_plan()`）——
    复用判定要在调模型之前完成，而 `digest_plan()` 正是那个「不花钱的先手」。
    """
    if plan is None:
        plan = digest_plan(
            db, novel_id, provider=provider, mode=mode, target_ratio=target_ratio
        )
    chapters = plan["chapters"]
    chapter_map = plan["chapter_map"]
    chars_in = plan["chars_in"]
    chosen = plan["mode"]
    reason = plan["mode_reason"]
    caps = plan["caps"]
    if progress is not None:
        progress(f"准备抽取（{chosen}）：{reason}", 0, 1)

    if chosen == MODE_WHOLE:
        system, user = build_digest_prompt(
            _render_book(chapters), caps=caps, whole=True
        )
        raw = provider.chat(system, user, max_tokens=digest_output_budget(caps))
        parsed = _apply_caps(parse_digest(raw), caps)
        if progress is not None:
            progress("整本摘要已返回，正在校验证据", 1, 1)
    else:
        block_chars = int(getattr(provider, "distill_block_chars", 0) or 0) or 20_000
        chunks = _chunk_chapters(chapters, block_chars)
        merged: dict[str, list] = {"timeline": [], "characters": [], "reactions": []}
        for idx, chunk in enumerate(chunks, start=1):
            label = f"第 {chunk[0][0]}~{chunk[-1][0]} 章"
            chunk_text = _render_book(chunk)
            # caps 按**这一块**的字数算，不是按整本书。
            # 为什么：按整本书算 ⇒ 每个 5 万字的块都被要求给 200 条时间线，
            # 而块里根本没有那么多内容 ⇒ 模型硬凑（编）或撞输出预算（截断）。
            chunk_caps = digest_caps(len(chunk_text))
            system, user = build_digest_prompt(
                chunk_text, caps=chunk_caps, whole=False, chunk_label=label
            )
            raw = provider.chat(
                system, user, max_tokens=digest_output_budget(chunk_caps)
            )
            try:
                part = parse_digest(raw)
            except ValueError as exc:
                logger.warning("摘要块解析失败（%s）：%s", label, exc)
                continue
            for key in ("timeline", "characters", "reactions"):
                merged[key].extend(part.get(key) or [])
            if progress is not None:
                progress(f"摘要抽取 {idx}/{len(chunks)}（{label}）", idx, len(chunks))
        parsed = _merge_partials(merged, caps)

    enforced, evidence_stats = enforce_evidence(parsed, chapter_map)
    names = _candidate_names(db, int(novel_id), chapter_map)
    # 主线候选 = 「涉及主要角色的事」⇒ 候选集依赖角色名单（先取角色，再取候选）。
    candidates = _candidate_events(chapter_map, main_characters=names)
    report = verify_digest(
        enforced,
        chapter_map,
        chars_in=chars_in,
        target_ratio=target_ratio,
        mainline_candidates=candidates,
        character_names=names,
        evidence_stats=evidence_stats,
    )
    text = render_digest_text(enforced)
    return {
        "digest_version": DIGEST_VERSION,
        "source": {
            "novel_id": int(novel_id),
            "n_chapters": len(chapters),
            "n_chars": chars_in,
        },
        "mode": chosen,
        "mode_reason": reason,
        "caps": caps.to_public_dict(),
        "timeline": enforced.get("timeline") or [],
        "characters": enforced.get("characters") or [],
        "reactions": enforced.get("reactions") or [],
        "stats": {
            "chars_in": chars_in,
            "chars_out": len(text),
            "ratio": round(len(text) / chars_in, 4) if chars_in else 0.0,
            "kept": evidence_stats.get("kept", 0),
            "dropped": evidence_stats.get("dropped", 0),
            # 复用键的另一半（另一半是 content_hash / digest_version，存在行上）。
            # 有了它，「换了档位或压缩比目标」就不会被静默复用成旧产物。
            "plan_key": plan["plan_key"],
        },
        "verify": report,
        "text": text,
    }


# ====================================================================
# 落库
# ====================================================================


def digest_content_hash(db, novel_id: int) -> str:
    """这本书正文的哈希（复用蒸馏那套口径）。

    正文变了（重新导入 / 清洗规则升级）⇒ 摘要必须重算。
    与 `style_distills.content_hash` 同一把尺，两边的「失效」语义一致。
    """
    from app.style.narrative import corpus_content_hash

    return corpus_content_hash([int(novel_id)], db)


def save_digest(db, digest: dict, content_hash: str) -> Any:
    """把摘要落库（同输入幂等 upsert），返回行。"""
    from datetime import datetime, timezone

    from app.models import BookDigest

    novel_id = int((digest.get("source") or {}).get("novel_id"))
    row = db.scalar(
        select(BookDigest).where(
            BookDigest.novel_id == novel_id,
            BookDigest.content_hash == content_hash,
            BookDigest.digest_version == DIGEST_VERSION,
            BookDigest.status == "active",
        )
    )
    payload = {
        "timeline": digest.get("timeline") or [],
        "characters": digest.get("characters") or [],
        "reactions": digest.get("reactions") or [],
    }
    if row is None:
        row = BookDigest(
            novel_id=novel_id,
            digest_version=DIGEST_VERSION,
            mode=digest.get("mode") or MODE_AUTO,
            mode_reason=digest.get("mode_reason"),
            content_hash=content_hash,
            payload=payload,
            text=digest.get("text") or "",
            stats_json=digest.get("stats"),
            verify_json=digest.get("verify"),
            status="active",
        )
        db.add(row)
    else:
        row.mode = digest.get("mode") or MODE_AUTO
        row.mode_reason = digest.get("mode_reason")
        row.payload = payload
        row.text = digest.get("text") or ""
        row.stats_json = digest.get("stats")
        row.verify_json = digest.get("verify")
        row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


def latest_digest(db, novel_id: int) -> Any | None:
    """这本书最新的一条 active 摘要（没有则 None）。"""
    from app.models import BookDigest

    return db.scalar(
        select(BookDigest)
        .where(
            BookDigest.novel_id == int(novel_id),
            BookDigest.status == "active",
        )
        .order_by(BookDigest.id.desc())
        .limit(1)
    )


def find_reusable_digest(db, novel_id: int, content_hash: str, plan_key: dict) -> Any | None:
    """找一条**可以复用**的摘要产物（正文版本 + 产物版本 + 参数全对得上）。

    ## 为什么不用 `latest_digest()`

    `latest_digest()` 只按 id 取最新的一条 active 行。
    若这本书先后被导入过两个版本，库里就有两条 active 行 ——
    「最新那条」未必是「正文版本对得上那条」⇒ 明明有能用的产物，却会白重算一次
    （再一次全本模型调用）。

    ⇒ 这里按 `(novel_id, content_hash, digest_version, active)` 捞出候选行，
    再逐条比对 `plan_key`（档位 + 压缩比目标）。

    旧版本代码落的行没有 `plan_key` ⇒ 一定不算可复用 ⇒ 会重算一次并补上，
    这是对的（结构变了就该重算）。
    """
    from app.models import BookDigest

    rows = db.scalars(
        select(BookDigest)
        .where(
            BookDigest.novel_id == int(novel_id),
            BookDigest.content_hash == content_hash,
            BookDigest.digest_version == DIGEST_VERSION,
            BookDigest.status == "active",
        )
        .order_by(BookDigest.id.desc())
    ).all()
    for row in rows:
        if (row.stats_json or {}).get("plan_key") == plan_key:
            return row
    return None


def digest_summary(row: Any) -> dict:
    """`BookDigest` 行 → API 摘要对象（**不带全文**，列表页用）。"""
    stats = row.stats_json or {}
    verify = row.verify_json or {}
    checks = verify.get("checks") or {}
    out = {
        "id": row.id,
        "novel_id": row.novel_id,
        "digest_version": row.digest_version,
        "mode": row.mode,
        "mode_reason": row.mode_reason,
        "status": row.status,
        "stats": stats,
        "verify_passed": verify.get("passed"),
        "checks": {
            k: {"ok": v.get("ok"), "value": v.get("value")}
            for k, v in checks.items()
        },
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    # ⚠️ 诊断字段**要透传**（2026-11-02 真跑时踩到）：
    # 之前这里只回 ok/value，于是「证据丢弃原因」「哪几项不适用」这些
    # **界面上要给人看的**字段全被吃掉了 ⇒ 用户只看到一个比例，
    # 没法判断是模型在编还是引文写错一个标点。
    ev = checks.get("evidence") or {}
    if ev:
        out["evidence_detail"] = {
            "dropped": ev.get("dropped", 0),
            "kept": ev.get("kept", 0),
            "reattributed": ev.get("reattributed", 0),
            "dropped_ratio": ev.get("dropped_ratio"),
            "threshold": ev.get("threshold"),
            "drop_reasons": ev.get("drop_reasons") or {},
            "dropped_by_block": ev.get("dropped_by_block") or {},
            "drop_samples": ev.get("drop_samples") or {},
        }
    not_applicable = verify.get("not_applicable") or []
    if not_applicable:
        out["not_applicable"] = not_applicable
    return out


def _merge_partials(parts: dict[str, list], caps: DigestCaps) -> dict:
    """合并多块的摘要（切块档）。

    合并规则**全部确定性**（不额外调模型）：

    - 时间线：按 `chapter_no` 排序后去重（事件文本归一化后相同视为同一条）；
    - 角色：按名字合并（同名条目合并 `arc` / `evidence`）；
    - 反应：按 (角色, 情境) 去重。

    为什么不调模型做合并：合并本身是一次「无中生有」的机会 ——
    模型改写条目时会丢掉证据（证据强制会把它丢掉），或者改出新条目（没出处）。
    确定性合并只做拼接与去重，**不新增任何内容**。
    """
    timeline: list[dict] = []
    seen_events: set[str] = set()
    for item in parts.get("timeline") or []:
        key = _norm(str(item.get("event", "")))
        if not key or key in seen_events:
            continue
        seen_events.add(key)
        timeline.append(item)
    timeline.sort(key=lambda it: (it.get("chapter_no") or 0, it.get("seq") or 0))

    characters: dict[str, dict] = {}
    for item in parts.get("characters") or []:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        cur = characters.setdefault(name, {"name": name})
        for key, value in item.items():
            if key in ("name",):
                continue
            if key in ("evidence", "key_events", "relations"):
                merged = list(cur.get(key) or [])
                for v in value or []:
                    if v not in merged:
                        merged.append(v)
                cur[key] = merged
            elif not cur.get(key) and value:
                cur[key] = value
    characters = list(characters.values())

    reactions: list[dict] = []
    seen_reactions: set[str] = set()
    for item in parts.get("reactions") or []:
        key = _norm(f"{item.get('character', '')}{item.get('trigger', '')}")
        if not key or key in seen_reactions:
            continue
        seen_reactions.add(key)
        reactions.append(item)

    return {
        "digest_version": DIGEST_VERSION,
        "timeline": _even_trim(timeline, caps.timeline),
        "characters": _even_trim(characters, caps.characters),
        "reactions": _even_trim(reactions, caps.reactions),
    }
