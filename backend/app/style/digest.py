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

#: 整本投喂的**门槛**（字）。超过这个量级就退回「切块抽」。
#:
#: 为什么是 30 万字：这是「一次调用能稳定产出完整摘要」的量级上限。
#: 更大的书即使渠道能吃下（opencode-go 实测能读 200 万字），
#: 一次输出的体量也会撑不住 —— 摘要的结构化条目数是**有上限**的
#: （见 `digest_caps`），书再大也只是同样的条目数，信息密度靠时间线取舍。
WHOLE_BOOK_MAX_CHARS = 300_000

#: 主线覆盖门槛（候选事件里被摘要覆盖的比例）。
MIN_MAINLINE_COVERAGE = 0.8
#: 角色覆盖门槛（高频角色里出现在摘要中的比例）。
MIN_CHARACTER_COVERAGE = 0.9

#: 证据缺失率超过此值 ⇒ 整次判失败（不是「丢几条没关系」）。
#:
#: 一两条缺证据很正常（模型把引文写错了）；**成片**缺证据说明模型在编，
#: 这时摘要的其它部分也不能信。
MAX_EVIDENCE_DROP_RATIO = 0.2

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
    - 自动 ⇒ **书的大小**与**渠道单次能读多少字**两条同时满足才走整本：
      渠道必须**声明**过能吃下这么多字（`provider.max_context_chars`）。
      没声明就当它吃不下 —— 假设错了的后果是「请求发出去了、回来被截断、
      我们以为压缩成功了」，这是本仓库反复踩的那类静默失效。
    """
    if requested and requested != MODE_AUTO:
        if requested not in (MODE_WHOLE, MODE_RETRIEVAL):
            raise ValueError(f"未知的摘要抽取档位：{requested!r}")
        return requested, "用户指定"

    limit = int(getattr(provider, "max_context_chars", 0) or 0)
    if chars_in > WHOLE_BOOK_MAX_CHARS:
        return MODE_RETRIEVAL, (
            f"全书 {chars_in:,} 字超过整本投喂门槛 {WHOLE_BOOK_MAX_CHARS:,} 字"
            f"{'，且渠道未声明单次可读上限' if not limit else f'，渠道单次可读约 {limit:,} 字'}"
            " ⇒ 切块抽取后合并"
        )
    if limit <= 0:
        return MODE_RETRIEVAL, (
            f"渠道未声明单次可读的字数上限 ⇒ 不敢整本投喂"
            f"（全书 {chars_in:,} 字）⇒ 切块抽取后合并"
        )
    if chars_in > limit:
        return MODE_RETRIEVAL, (
            f"全书 {chars_in:,} 字超过渠道单次可读的 {limit:,} 字 ⇒ 切块抽取后合并"
        )
    return MODE_WHOLE, (
        f"全书 {chars_in:,} 字 ≤ 门槛 {WHOLE_BOOK_MAX_CHARS:,} 字，且渠道单次可读 "
        f"{limit:,} 字 ⇒ 整本一次投喂（模型看得到全局，跨章的问题才答得出来）"
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
        "2. 每一项都要带 evidence，quote 必须是给定原文里**逐字存在**的片段"
        f"（≤ {MAX_QUOTE_CHARS} 字），evidence.chapter_no 必须是它真实出现的那一章。\n"
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
    """
    q = _norm(_clean_quote(quote))
    if len(q) < 4:
        return False
    body = _norm(chapter_text)
    if q in body:
        return True
    for cut in (12, 8):
        if len(q) > cut and q[:cut] in body:
            return True
    return False


def _entry_evidence_ok(entry: dict, chapters: dict[int, str]) -> bool:
    """这条摘要的证据是否站得住：章号存在 + 引文逐字在那一章里。"""
    evidence = entry.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        ch = ev.get("chapter_no")
        quote = ev.get("quote")
        if not isinstance(ch, int) or isinstance(ch, bool):
            continue
        if not isinstance(quote, str) or not quote.strip():
            continue
        text = chapters.get(ch)
        if text and _quote_in_chapter(quote, text):
            return True
    return False


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
    stats = {"kept": 0, "dropped": 0, "dropped_by_block": {}}
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
            if _entry_evidence_ok(entry, chapters):
                kept.append(entry)
            else:
                dropped += 1
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
    chapters: dict[int, str], limit: int = MAINLINE_CANDIDATES
) -> list[str]:
    """从原文里挑「像一件事」的句子，作为**主线的候选集合**。

    判据与 `roleplay/bio.py` 同源：有动作动词、长度适中、每章至多一条。
    按章号**均匀**取样，保证覆盖全书（不是只取开头那几章）。

    这是**启发式**的候选集，不是真值 —— 它的用途是当闸门
    （摘要把主线压丢了要能被挡住），不是当标准答案。
    """
    if not chapters:
        return []
    nos = sorted(chapters)
    picked: list[str] = []
    per_chapter: dict[int, str] = {}
    for ch in nos:
        text = chapters.get(ch) or ""
        for sent in _SENT_END.split(text):
            s = sent.strip()
            if not (_SENT_MIN_CHARS <= len(s) <= _SENT_MAX_CHARS):
                continue
            if not _ACTION_HINT.search(s):
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


def _candidate_names(
    db, novel_id: int, chapters: dict[int, str], limit: int = CHARACTER_CANDIDATES
) -> list[str]:
    """原文里的高频角色名（覆盖判定的分母）。

    优先用**知识图谱**里的 character 节点（图谱是仓库既有的事实层）；
    没有图谱时退回 `style/entities.py` 的 PMI 候选（纯统计，零依赖）。

    两条路都拿不到时返回空列表 ⇒ 角色覆盖这一项判「不适用」，
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
            from app.style.entities import drop_fragments, pmi_candidates

            text = "\n".join(chapters.values())
            table = pmi_candidates(text)
            kept, _ = drop_fragments(table, text)
            names = [n for n, _ in sorted(kept.items(), key=lambda kv: -kv[1])]
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
            "chars_in": int(chars_in),
            "chars_out": int(chars_out),
            "ratio": round(ratio, 4),
            "target_ratio": target_ratio,
            "max_chars_out": int(max_out),
        },
        "mainline_coverage": {
            "ok": mainline_coverage is None or mainline_coverage >= MIN_MAINLINE_COVERAGE,
            "covered": covered,
            "candidates": len(candidates),
            "value": None if mainline_coverage is None else round(mainline_coverage, 4),
            "threshold": MIN_MAINLINE_COVERAGE,
        },
        "character_coverage": {
            "ok": character_coverage is None or character_coverage >= MIN_CHARACTER_COVERAGE,
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
            "dropped": int(evidence_stats.get("dropped", 0)),
            "kept": int(evidence_stats.get("kept", 0)),
            "dropped_ratio": round(drop_ratio, 4),
            "threshold": MAX_EVIDENCE_DROP_RATIO,
        }
    return {
        "passed": all(c["ok"] for c in checks.values()),
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


def build_book_digest(
    db,
    novel_id: int,
    provider=None,
    *,
    mode: str = MODE_AUTO,
    progress: Callable[[str, int, int], None] | None = None,
    target_ratio: float = TARGET_COMPRESSION,
) -> dict:
    """给一本书做结构化摘要，返回可直接落库的产物。

    流程：选档 → 取正文 → 抽取（整本 / 切块）→ 证据强制 → 验收 → 落字段。

    `progress(stage, current, total)` 的单位是**块**（整本档是 1 块）。
    """
    from app.style.narrative import _load_novel_chapters

    chapters = _load_novel_chapters(db, int(novel_id))
    if not chapters:
        raise ValueError(f"语料 {novel_id} 没有正文，无法做全书摘要")
    chapter_map = {ch: text for ch, text in chapters}
    chars_in = sum(len(text or "") for _, text in chapters)

    chosen, reason = choose_digest_mode(chars_in, provider, mode)
    caps = digest_caps(chars_in)
    if progress is not None:
        progress(f"准备抽取（{chosen}）：{reason}", 0, 1)

    if chosen == MODE_WHOLE:
        system, user = build_digest_prompt(
            _render_book(chapters), caps=caps, whole=True
        )
        raw = provider.chat(system, user, max_tokens=32_000)
        parsed = parse_digest(raw)
        if progress is not None:
            progress("整本摘要已返回，正在校验证据", 1, 1)
    else:
        block_chars = int(getattr(provider, "distill_block_chars", 0) or 0) or 20_000
        chunks = _chunk_chapters(chapters, block_chars)
        merged: dict[str, list] = {"timeline": [], "characters": [], "reactions": []}
        for idx, chunk in enumerate(chunks, start=1):
            label = f"第 {chunk[0][0]}~{chunk[-1][0]} 章"
            system, user = build_digest_prompt(
                _render_book(chunk), caps=caps, whole=False, chunk_label=label
            )
            raw = provider.chat(system, user, max_tokens=16_000)
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
    candidates = _candidate_events(chapter_map)
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


def digest_summary(row: Any) -> dict:
    """`BookDigest` 行 → API 摘要对象（**不带全文**，列表页用）。"""
    stats = row.stats_json or {}
    verify = row.verify_json or {}
    checks = verify.get("checks") or {}
    return {
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
        "timeline": timeline[: caps.timeline],
        "characters": characters[: caps.characters],
        "reactions": reactions[: caps.reactions],
    }
