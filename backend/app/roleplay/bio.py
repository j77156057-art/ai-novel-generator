"""角色的**生平时间线**（机械层，零模型调用）。

## 为什么需要它

档案的 characters 层**只抽前 N 个主要角色**（《北荒刀歌》实测 8 个：陆长风 / 陆崇 /
赵磐石 / 楚惊蛰 / 陆北枝 / 小宁 / 裴长卿 / 赵青锋）—— **老周不在里面**。

⇒ 用户在角色对话里选了「老周」，他有**声音**（角色声音词典里有），却**没有档案**
（身份/行为倾向/底线/关键经历全没有）。档案回填（`style/bio_evidence.py`）也救不了
—— 他压根没有条目可回填。

⇒ 这一层补的是**「他经历过什么」的机械版**：按章从原文里挑出**他在场**的事件句。
不需要模型、不需要重蒸馏，而且**直接服务于用户的目标**（角色知道自己知道的事）。

## 与「检索片段」的区别

- 检索片段（`roleplay/evidence.py`）：**散**的若干段原文，用来回答「他记得什么」。
- 生平时间线（本模块）：**按时间排**的事件序列，用来回答「他这一生经历了什么」。

两者互补：时间线给骨架，检索片段给细节。

## 挑事件的判据

每章里含他名字的行中，挑**最像一件事**的：
- 有动作动词（不是纯对白、不是心理描写）；
- 长度适中（12~40 字，太短没信息、太长是叙述）；
- 同一章只取一条（时间线要的是「关键节点」，不是流水账）。
"""
from __future__ import annotations

import re

#: 动作动词（粗判）：句子里有这些字才算「做了一件事」。
_ACTION_HINT = re.compile(
    r"(走|跑|牵|骑|拔|挥|砍|刺|抬|低|转|看|望|站|坐|跪|倒|拾|捡|拿|递|送|还|退|进|"
    r"冲|扑|抓|握|推|拉|按|拍|摸|抚|抓|叹|笑|喊|叫|答|问|开口|起身|伸手|摇头|点头|"
    r"握紧|松开|拔出|落下|起身)"
)

#: 心理描写为主 ⇒ 不是「做的事」。
_MIND_ONLY = re.compile(r"^(心想|心中|暗想|觉得|知道|明白|想起|记得)")

#: 事件短句的长度区间（字）。
EVENT_MIN_CHARS = 10
EVENT_MAX_CHARS = 44

#: 时间线最多列几章。
DEFAULT_MAX_CHAPTERS = 8


#: 一句话的长度区间（字）。太短没信息，太长是叙述而不是「一件事」。
SENT_MIN_CHARS = 12
SENT_MAX_CHARS = 50

#: 句末标点（切句用）。
_SENT_END = re.compile(r"(?<=[。！？!?…])")


def _event_candidate(line: str, character: str) -> str | None:
    """这一行里有没有「他做了一件事」的**完整句**？

    ## 为什么取完整句而不是裁半句

    第一版从长行裁到标点边界，裁出来的是**句子开头**（「恶奴就更没有了，」）——
    事件是残的，模型读到会以为他刚说了半句话就没了。
    """
    s = (line or "").strip()
    if not s or character not in s:
        return None
    best = None
    best_pos = 10 ** 9
    for sent in _SENT_END.split(s):
        sent = sent.strip()
        if not sent or character not in sent:
            continue
        n = len(sent)
        if n < SENT_MIN_CHARS or n > SENT_MAX_CHARS:
            continue
        if _MIND_ONLY.match(sent):
            continue
        if not _ACTION_HINT.search(sent):
            continue
        # 必须是**完整的话**：引号配对（对白被切在引号里就不算完整）
        if sent.count("“") != sent.count("”"):
            continue
        if sent.count("「") != sent.count("」"):
            continue
        # 且以句末标点结尾（「…嬉笑道：」不是完整句）
        if sent[-1] not in "。！？!?…":
            continue
        pos = sent.find(character)
        # 名字出现得越早，越像「他做的事」而不是「别人提到他」
        if pos < best_pos:
            best, best_pos = sent, pos
    return best


def character_timeline(
    db,
    novel_id: int,
    character: str,
    *,
    max_chapters: int = DEFAULT_MAX_CHAPTERS,
) -> list[dict]:
    """按章挑出这个角色**在场**的事件句。

    返回 [{"chapter_no": int, "event": str}, ...]（按章号升序）。
    """
    from app.models import NovelChunk

    name = (character or "").strip()
    if not name:
        return []
    rows = (
        db.query(NovelChunk)
        .filter(NovelChunk.novel_id == novel_id)
        .order_by(NovelChunk.chapter_no, NovelChunk.idx)
        .all()
    )
    by_chapter: dict[int, list[str]] = {}
    for r in rows:
        ch = int(r.chapter_no or 0)
        for line in (r.text or "").split("\n"):
            line = line.strip()
            if not line or name not in line:
                continue
            by_chapter.setdefault(ch, []).append(line)

    out: list[dict] = []
    for ch in sorted(by_chapter):
        best = None
        for line in by_chapter[ch]:
            cand = _event_candidate(line, name)
            if cand and (best is None or len(cand) > len(best)):
                best = cand
        if best:
            out.append({"chapter_no": ch, "event": best})
    return out[:max_chapters]


def render_timeline(timeline: list[dict], *, max_items: int = 6) -> str:
    """渲染成提示词里的「他经历过的事（按时间）」块。"""
    if not timeline:
        return ""
    lines = ["【他经历过的事（按时间先后）】"]
    for item in timeline[:max_items]:
        lines.append(f"第{item['chapter_no']}章：{item['event']}")
    return "\n".join(lines)
