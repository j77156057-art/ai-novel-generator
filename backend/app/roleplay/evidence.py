"""角色对话：**证据**（他真正经历过的片段）与**来源标注**。

## 这份模块回答的问题

用户问：「角色聊天不参考原文的话，问他只有他们知道的事，应该怎么回答？」

真跑（MiniMax-M3.1-Flash + 《北荒刀歌》）给出的答案是「**他在编**」：
检索到的 6 段原文里没有任何一条是那个问题的答案，模型却答得像模像样
（「小时候他是胖墩儿一个」「骑马是我带的」）。

根因有两层：
1. **没人能指定扮演谁** → 角色定向检索根本没跑（`_roleplay_character_name` 取不到人）。
2. **检索的是「他出现过」而不是「他知道」** → 别人当着他面说破的秘密，他也会知道。

## 来源标注：两种

| 标注 | 含义 | 判据 |
|---|---|---|
| **他亲历** | 他自己开过口，或这件事被明确记在他名下 | ① 对白的**说话人是他**；② KG 角色已知情报里有这件事 |
| **他在场听到** | 他在旁边听着 / 被提到 | 其余 |

为什么要标注而不是直接过滤掉「在场听到」的那些：
用户在两条路里选了「标注来源，由模型自己判断能不能当作记忆说出口」——
这样角色能演出「这事我是听说的」这种层次感，而不是一律查无此事。

## 为什么判据是机械的

「他亲历」的判定不能靠模型 —— 那正是要防的事（模型会把一切说成自己亲历）。
所以这里用**可复算的机械判据**：说话人匹配 + KG 的知情记录，命中不了就是「在场听到」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 说话动词（后面必须跟「：」）：`老周道：“…”` / `老周挠挠头道：“…”`
#:
#: **不用正则去猜「名字是哪几个字」** —— 中文里名字与说话动词之间可以夹
#: 任意多的动作/神态（「老周挠挠头道」「老周想了想，一脸为难道」），
#: 正则贪婪匹配会把「草的老周」当成名字（实测踩过）。
#:
#: 改成**倒着看**：找到说话动词，往前看一小段窗口，
#: 角色名必须**紧贴**动词（中间最多 8 个字，如「挠挠头」「忍不住破戒」）。
#:
#: 代价：「老周想了想，一脸为难道：」这种隔了逗号的写法会漏判成「在场听到」。
#: 方向是**保守**的（不会把别人对他说的话当成他的记忆）—— 这个方向的错代价更小。
_SPEECH_TAIL = re.compile(
    r"(?:说道|问道|答道|笑道|喝道|怒道|低声道|沉声道|轻声道|开口道|"
    r"道|说|问|答|喊|骂|唤|叫)(?:道)?[：:]"
)

#: 名字与说话动词之间最多能夹几个字（「忍不住破戒」= 5）。
#: 名字与说话动词之间最多能夹几个字（「挠挠头」= 3）。
_SPEAKER_GAP = 4
#: 往前看的窗口大小。
_SPEAKER_WINDOW = 8


#: 台词里直接叫他的名字（说明他在场且被交涉）
_QUOTE_RE = re.compile(r"[“「『]([^”」』]{1,400})[”」』]")


@dataclass
class RoleplaySource:
    """一条带来源标注的证据。"""

    chunk_id: int
    chapter_no: int | None
    idx: int | None
    text: str
    #: "亲历" | "在场听到"
    origin: str
    #: 命中的具体判据（给人看的依据，不是给模型看的）
    basis: str
    score: float = 0.0
    #: 他明确知道的事实（来自 KG 角色已知情报）
    known_facts: list[str] = field(default_factory=list)
    #: 这条证据是**关于谁**的（渲染时用它裁出「他自己开过口的行」）
    character: str = ""

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "chapter_no": self.chapter_no,
            "idx": self.idx,
            "origin": self.origin,
            "basis": self.basis,
            "score": self.score,
            "text": self.text,
            "known_facts": self.known_facts,
        }


def _speaker_lines(text: str, character: str) -> list[str]:
    """这段里**他自己开口**的行。

    两条判据（任一命中即算他说的）：

    1. **强判据**：说话动词紧贴他的名字（中间最多 8 个字，如「老夫子忍不住破戒说道」）。
    2. **行首判据**：这一行**以他的名字开头**，且行里有引号对白。
       —— 中文的段落常以说话人起笔（「老周摇头凝视着……乐呵呵道：“有趣得很”」），
       这种写法名字与说话动词之间隔着好几个字（还有逗号），强判据抓不到，
       但它**确实是他在说**。

       实测（子 agent 复核指出）：片段 2/4/5 明明含老周本人的对白，
       却因为强判据够不着而被标成「他在场听到」——那会让他把自己亲历的事
       说成「听人提起过」。

    ## 两条排除规则（2026-10-28 加，都是实测踩出来的）

    - **名字与说话动词之间跨了标点** ⇒ 主语换人了，那不是他说的。
      （实测：「…横眉竖眼的老夫子，无奈道：」——「老夫子」在句中是**宾语**，
      说话的是苏酥；而「…马草的老周挠挠头道：」里的「的」是**定语**，老周**就是**说话人。
      两者的区别不是「的」，而是**跨不跨标点** ⇒ 只用标点规则，不用「的」规则。）
    - **行首判据要求他紧跟一个说话动词**（中间不许跨标点）——
      否则「徐凤年到底是身体疲乏，…徐骁远远站着，小心翼翼赔笑道：…」这种
      （行以徐凤年开头、但说话的是徐骁）会被算成徐凤年开的口。
    """
    out: list[str] = []
    for line in (text or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        # 判据 2：行首就是他 + 行里有引号对白
        if s.startswith(character) and ("“" in s or "「" in s):
            out.append(s)
            continue
        # 判据 1：说话动词紧贴他的名字
        for m in _SPEECH_TAIL.finditer(s):
            head = s[max(0, m.start() - _SPEAKER_WINDOW): m.start()]
            if character not in head:
                continue
            gap = len(head) - head.rindex(character) - len(character)
            if gap <= _SPEAKER_GAP:
                out.append(s)
                break
    return out


def _mentioned_in_dialogue(text: str, character: str) -> bool:
    """台词里有没有叫到他（说明他在场，且被交涉）。"""
    for m in _QUOTE_RE.finditer(text or ""):
        if character and character in m.group(1):
            return True
    return False


def classify_origin(text: str, character: str) -> tuple[str, str]:
    """判定一段原文对角色而言是「亲历」「在场听到」还是「只是被提到」。

    返回 (origin, basis)：
    - origin: "亲历" | "在场听到" | "只是被提到"
    - basis: 命中的判据（人可复核）

    ## 三档而不是两档（2026-10-28 实测加的）

    用户问「老夫子性格怎么样」，答案里出现了**另一个老头**（程白霜/薛宋官那段）。
    那一段里有「老夫子」三个字（其实说的是别人），旧判据判成「他在场听到」——
    模型据此以为「老夫子在场听见了这段」。

    ⇒ 现在把「文里提到他」与「他在场」分开：提到他**不代表他在场**，
    更不代表他知道那件事。标出来，模型自己判断能不能当记忆说。

    ## 判据是**保守**的（方向很重要）

    「他亲历」要求认出他**开口了**。中文里他说的写法五花八门
    （「老周挠挠头道」「老周憨憨道」「老周摇头凝视着……乐呵呵道」「大少爷，这是……」），
    判据只认得住两种（说话人紧贴名字 / 行首就是他的名字 + 有引号）。

    ⇒ **认不出就判「他在场听到」或「只是被提到」**。这个方向的错代价小得多：
    - 误判成「亲历」⇒ 他会**知道**本不该知道的秘密（别人当着他面说破的）；
    - 漏判成「在场听到」⇒ 他把自己亲历的事说成「听人提起过」（显得生分，不骗人）。
    """
    if _speaker_lines(text, character):
        return "亲历", "这段里他自己开过口"
    if _mentioned_in_dialogue(text, character):
        return "在场听到", "台词里叫到了他，但说话的不是他"
    if character and character in (text or ""):
        return "只是被提到", "文里提到了他，但这段里没认出他的对白"
    return "只是被提到", "仅提到其名，无对白可证"


def known_facts_for(db, project_id: int, character: str) -> list[str]:
    """KG 里明确记着**他知道**的事（角色已知情报）。

    这是「亲历」最可靠的一条依据 —— 它来自抽取后的结构化记录，
    不是靠猜。取不到就返回空列表（不阻断）。

    实体名要**认别名**：同一角色在 KG 里可能登记成「老周」/「黄老九」。
    """
    name = (character or "").strip()
    if not name or not project_id:
        return []
    try:
        from app.models import KgEntity, KgEntityKnowledge

        ents = db.query(KgEntity).filter(
            KgEntity.project_id == project_id,
            KgEntity.kind == "character",
        ).all()
        ent = None
        for e in ents:
            names = {e.canonical_name or ""}
            for a in (e.aliases or []):
                names.add(str(a))
            if name in names:
                ent = e
                break
        if ent is None:
            return []
        rows = db.query(KgEntityKnowledge).filter(
            KgEntityKnowledge.project_id == project_id,
            KgEntityKnowledge.entity_id == ent.id,
        ).all()
        out: list[str] = []
        for r in rows:
            t = (r.known_text or "").strip()
            if t and t not in out:
                out.append(t)
        return out[:12]
    except Exception:  # noqa: BLE001 —— 取不到就退化成「没有已知情报」
        return []


def gather_evidence(
    db,
    project,
    novel_ids: list[int],
    character: str,
    *,
    k: int = 6,
) -> list[RoleplaySource]:
    """取这个角色**自己出场/说过**的原文片段，每条带来源标注。

    检索用 BM25 按角色名 + 子串硬约束（`retrieve_for_character`）：
    中文人名当关键词比向量准 —— 实测向量检索会把 8 万字的最长章排到第一。
    """
    from app.retrieval.strategies import retrieve_for_character

    name = (character or "").strip()
    if not name:
        return []

    facts = known_facts_for(db, getattr(project, "id", None), name)

    out: list[RoleplaySource] = []
    seen_chunks: set[int] = set()
    for nid in novel_ids or []:
        try:
            hits = retrieve_for_character(db, nid, name, k=k)
        except Exception:  # noqa: BLE001 —— 单本失败不阻断其余
            continue
        for h in hits:
            cid = h.get("chunk_id")
            if cid in seen_chunks:
                continue
            seen_chunks.add(cid)
            text = h.get("text") or ""
            origin, basis = classify_origin(text, name)
            out.append(RoleplaySource(
                chunk_id=cid,
                chapter_no=h.get("chapter_no"),
                idx=h.get("idx"),
                text=text,
                origin=origin,
                basis=basis,
                score=float(h.get("score") or 0.0),
                known_facts=list(facts),
                character=name,
            ))
    return out


#: 引号里的内容（中文小说里的台词）。
_QUOTED = re.compile(r"[“「『]([^”」』]{1,400})[”」』]")


def _own_quotes(text: str, character: str) -> list[str]:
    """这一段里，**他名字所在那一行的引号内容**。

    ## 判据只有一层机械规则：名字与引号**同行**

    刻意**不判断谁在说** —— 那个判据调了七版，每版都被真实文本里的新反例推翻
    （「老夫子怒极，颤声道」是并列谓语、「老夫子一拍桌子，道」是插入语、
    「马草的老周」里的「的」是定语…）。规则越多越容易过拟合到例子里。

    这里要的不是「谁说的」，而是「**哪几句是他的台词**」——
    模型据此能引用自己的原话，比整段喂进去强得多
    （实测：#8 的 6 段共 6297 字 → 这条规则能挑出他名字所在行的引号）。

    代价：偶尔会混进同一行里别人的话（「老夫子怒极，颤声道：「好一个丧家犬！」」
    前面那句其实是苏酥说的）。量级远小于整段，且提示词里已说明「引号里属于你的那几句」。
    """
    out: list[str] = []
    for line in (text or "").split("\n"):
        s = line.strip()
        if not s or character not in s:
            continue
        idx = s.find(character)
        tail = s[idx + len(character):]
        for q in _QUOTED.findall(tail):
            q = q.strip()
            if q:
                out.append(q)
    return out


def render_evidence_block(sources: list[RoleplaySource]) -> str:
    """把证据渲染成给模型看的块 —— **带来源标注**。

    模型据此判断「这件事能不能当作我自己的记忆说出口」：
    「亲历」的可以当亲历说；「在场听到」的只能说「我听人提起过」。

    ## 「亲历」的段落额外列出**他亲口说过的话**

    见 `_own_quotes` 的说明：整段喂进去，模型会把「别人在谈论他」当成他的素材，
    然后用第三人称写人物小传（实测「老夫子么，读书人的脾气，未必好…」）。
    额外列出他名字所在行的引号，模型就知道**自己的原话**是什么。
    """
    if not sources:
        return ""
    lines = [
        "【你能回忆起的片段（每段都标了来源）】",
        "注意：这是**书里的片段**，不是你的传记。",
        "大多数里面是**别人在说话**、关于你的话；",
        "只有引号里**属于你的那几句**才是你亲口说的。",
    ]
    for i, s in enumerate(sources, 1):
        mark = {
            "亲历": "他亲历",
            "在场听到": "他在场听到",
        }.get(s.origin, "只是被提到")
        body = s.text.strip()
        if not body:
            continue
        lines.append(f"（{i}）【{mark}·{s.basis}】")
        if s.origin == "亲历":
            own = _own_quotes(s.text, s.character or "")
            if own:
                lines.append("你亲口说过的话：")
                lines += [f"　{q}" for q in own[:8]]
        lines.append(body)
        if s.known_facts:
            lines.append("（你明确知道的事：" + "；".join(s.known_facts[:5]) + "）")
    return "\n".join(lines)
