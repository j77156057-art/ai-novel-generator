"""通用一致性检查层（coherence-v1）。

与文风闸门（`app.style.consistency`）并排、互不干涉：闸门量的是「像不像」，
本层查的是「说不说得通」。四类检查：

1. `speaker`   说话人归属的形式信号（同引号内两组问答 / 自问已知 / 归属与持物冲突）
2. `timeline`  日期与相对时间跨度是否互斥
3. `reference` 本章出现、前文却无来源的名词指代
4. `foreshadow` 大纲安排在后面才揭示的线索，本章是否提前点破

**判定所需的一切都来自入参**：`plot_bible`（角色名、称谓、年代锚）、
`outline`（每章一条的文本）、`prior_chapters`（已写正文）。本文件不含任何
书名、人名、年份、章号常量——换书时只换数据，不换代码。

判定分三档：`definite`（形式证据已闭合）、`suspected`（有信号但需人工确认）、
`info`（无法判定，例如缺年份锚点）。设计原则是宁可多报 `suspected`，也不把
`definite` 的门槛放松——重写开关默认只看 `definite`。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from app.text_numbers import cn_number as _cn_number

CHECK_VERSION = "coherence-v1"

#: 段落切分：空行分段（与落库正文的段落口径一致）
_PARA_SPLIT = re.compile(r"\n\s*\n")

#: 中文引号与直角引号（部分作者只用直角引号）
_QUOTE = re.compile(r"“([^”]*)”|「([^」]*)」|\"([^\"]*)\"")

#: 带说话功能的动词。只收通用动词，不含任何作品专有词。
_SPEECH_VERBS = "说|问|答|道|喊|叫|接|开口|应|念|解释|嘟囔|嘀咕|回|补|提醒"

#: 归属提示语：引号后「XX说」/ 引号前「XX说：」
_CUE_AFTER = re.compile(r"[”」\"][，,]?\s*(?P<name>{N})(?P<verb>" + _SPEECH_VERBS + ")")
_CUE_BEFORE = re.compile(
    r"(?P<name>{N})(?P<verb>" + _SPEECH_VERBS + r")[^。！？\n]{{0,10}}[：:]\s*[“「\"]")

#: 仅出现人名、没有引号的段落（如「电话响了两声，X 接了。」）可指定下一句的说话人
_SILENT_CUE = re.compile(r"(?P<name>{N})[^。！？\n]{{0,6}}(?:" + _SPEECH_VERBS + ")")

#: 回答语气标记：出现在引号内的问句之后，提示「另一个人接话」
_ANSWER_MARKERS = re.compile(
    r"不能|只能|无法|不得|必须|可以|不行|没有|不用|不必|不是|不会|没|是|对|行|好")

#: 请示/征询句式：「行不行」「能不能」「可不可以」。只有这类问句后紧跟规则式回答，
#: 才足以判定「一问一答被并进同一对引号」；普通的自问自答是正常行文。
_PERMISSION_ASK = re.compile(r"行不行|可以吗|能不能|可不可以|要不要|行吗|好吗|成吗|对不对")

#: 名词性语素：用来把「这个/那个 + X」里的 X 限定为名词短语。
#: 是通用汉语构词线索（器物、单据、处所、人物），与任何具体作品无关。
_NOMINAL_CHARS = set(
    "箱单据票据机盒纸布条刀袋包本册信照图卡证表钱账房门楼灯人事话字笔车衣药书板"
    "角层屋院店站群号名费水米油盐茶烟酒菜肉饭瓶罐碗盘碟桶柜屉桌椅凳床被帘窗墙梯"
    "阶廊厅室顶底缝洞井河桥路街巷村镇城界域山林湖海岛峰谷关锁钥匙镜珠印纹痕图谱"
    "录志纪簿卷轴琴鼓钟铃脉魂气丹符阵剑枪丸晶石令牌筐篓壶盆"
)

#: 时间量词：`这个月`/`那天` 这类是时间状语，不是「缺来源的定指名词」。
_TIME_HEADS = set("月年天周次日早晚会季")

def _nominal_tail(s: str) -> str:
    """从右往左裁到名词性语素为止（「说明书展开一」→「说明书」）。

    裁不出至少两个字的中心名词就返回空串——宁可漏报，也不把动词短语当名词。
    """
    s = (s or "").strip()
    while s and s[-1] not in _NOMINAL_CHARS:
        s = s[:-1]
    return s if len(s) >= 2 else ""


def _central_noun(raw: str, *, require_nominal: bool = True) -> str:
    """`跟那张缴费单` → `缴费单`：取最后一个连接/指示/量词之后的部分。

    两个口径：

    - `require_nominal=True`（用于「这个 X」这类定指短语）：X 必须以名词性语素
      结尾，否则视为动词短语，直接放弃——宁可漏报也不误报；
    - `require_nominal=False`（用于「X 有什么关系」）：问句本身已经证明 X 是实体，
      只剥连接词与量词，不再要求词尾形态（「契约」「水费」都能留下）。
    """
    s = (raw or "").strip()
    s = re.split(r"[跟和与把对向这那哪张个份只盒包件本部间层条片块些种]", s)[-1]
    s = s.strip("的地得了着")
    if require_nominal:
        return _nominal_tail(s)
    return s if len(s) >= 2 else ""

#: 句首称呼：「小陈啊，…」「周叔，…」。称呼指向的是**听话人**，不是说话人。
_VOCATIVE = re.compile(r"^\s*(?P<name>[\u4e00-\u9fa5]{2,4})(?:啊|呀|呢|哥|叔|姐|姨|伯)?\s*[，,]")

#: 身份断言：「X 是「人名」寄的 / 发的 / 写的」
_IDENTITY_VERBS = "寄|发|送|写|留|派|安排|叫|找|打"
_IDENTITY_ASSERT = re.compile(
    r"(?P<obj>[\u4e00-\u9fa5]{{1,10}})(?:是|就是)(?P<ent>{N})(?P<verb>" + _IDENTITY_VERBS + ")的")

#: 通用器物名词（跨作品共用的词表，不是某本书的设定）
_ARTIFACT_NOUNS = (
    "报纸", "杂志", "说明书", "药盒", "药瓶", "药", "照片", "相片", "信件", "票据",
    "收据", "账单", "单据", "传单", "海报", "工牌", "制服", "校服", "录音", "录像",
    "短信", "日记", "卡片", "名片", "广告", "合同", "证明", "户口本", "存折", "车票",
)

#: 持物动词 → 用于跟踪「谁在拿这个东西」
_POSSESS_VERBS = "抱起|抱紧|抱着|拿起|拿着|拎起|拎着|提起|提着|搬进|搬到|装进|收进|塞进"

#: 泛指物品的词：场景里只有一个被跟踪物时，它们都指向它
_GENERIC_OBJECTS = ("东西", "货", "它", "这个", "那个", "这玩意儿")

#: 指代追问句型：「X 是什么 / 有什么关系 / 指什么」
_REF_QUERY = re.compile(r"(?P<noun>[\u4e00-\u9fa5]{2,6})(?:是什么|有什么关系|指什么|怎么回事|干什么用的)")

#: 定指短语：「那张 X / 这个 X」
_DEFINITE_NP = re.compile(r"(?:那|这)(?:张|个|份|只|盒|包|件|本|部)(?P<noun>[\u4e00-\u9fa5]{2,6})")


@dataclass
class Finding:
    """一条可定位的问题记录。`paragraph` 从 1 开始，按空行分段计数。"""

    category: str
    check: str
    level: str  # definite | suspected | info
    paragraph: int
    message: str
    quote: str = ""
    evidence: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ─── 基础工具 ────────────────────────────────────────────────────────────


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in _PARA_SPLIT.split(text or "") if p and p.strip()]


def extract_quotes(paragraph: str) -> list[tuple[int, int, str]]:
    """返回 [(start, end, content)]，content 不含引号本身。"""
    out: list[tuple[int, int, str]] = []
    for m in _QUOTE.finditer(paragraph):
        body = m.group(1) or m.group(2) or m.group(3) or ""
        out.append((m.start(), m.end(), body.strip()))
    return out


def person_markers(s: str) -> set[str]:
    """粗粒度人称：first（我/我们/咱）、second（你/您）。"""
    marks: set[str] = set()
    if re.search(r"(?<![你您])我|我们|咱", s):
        marks.add("first")
    if re.search(r"[你您]", s):
        marks.add("second")
    return marks


def chapter_index(label: str | None) -> int | None:
    """从「第2章《…》」这类标签里取章号；解析不了返回 None（不猜）。"""
    if not label:
        return None
    m = re.search(r"第\s*([0-9]+|[一二三四五六七八九十百零〇]+)\s*[章回节卷]", str(label))
    return _cn_number(m.group(1)) if m else None


#: 泛称，不能当别名用（否则「师傅」会被当成某个人）
_GENERIC_ADDRESS = {
    "你", "您", "哥", "姐", "师傅", "同志", "领导", "兄弟", "老板", "熟客", "外人",
    "下属", "上级", "老人", "大家", "各位", "客户", "业主", "客人", "对方",
}


def _addr_tokens(value: Any) -> set[str]:
    """把「周叔/老周」这类称谓串拆成别名集合，去掉括号注解与泛称。"""
    out: set[str] = set()
    for raw in re.split(r"[/／、，,；;]", str(value or "")):
        tok = re.sub(r"[（(].*$", "", raw).strip(" 　\"'“”")
        if 2 <= len(tok) <= 4 and tok not in _GENERIC_ADDRESS:
            out.add(tok)
    return out


def build_roster(plot_bible: dict | None) -> dict[str, set[str]]:
    """人名 → 别名集合，全部来自 `plot_bible.characters`。

    两个来源：

    1. `characters[].aliases`（作者显式声明的别名）；
    2. `characters[].voice.address` 的**值**。角色卡里写的是
       `"<被称呼者>": "<称谓>/<称谓>"`，含义是「本角色怎么称呼对方」——所以这些
       称谓属于**被称呼者**，要挂到被称呼者名下，而不是挂在卡片主人名下。

    泛称（你/您/师傅/哥…）不入册，否则每一句「师傅」都会被认成同一个人。
    """
    roster: dict[str, set[str]] = {}
    cards: dict[str, dict] = {}
    chars = (plot_bible or {}).get("characters") or []
    for item in chars:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        cards[name] = item
        aliases = {name}
        for a in item.get("aliases") or []:
            a = str(a).strip()
            if a and len(a) >= 2:
                aliases.add(a)
        roster[name] = aliases

    for name, item in cards.items():
        voice = item.get("voice") if isinstance(item.get("voice"), dict) else {}
        for target, value in (voice.get("address") or {}).items():
            target = str(target).strip()
            if target in roster:
                roster[target] |= _addr_tokens(value)

    # 去掉同时被两个角色认领的别名，避免归属歧义
    seen: dict[str, str] = {}
    for name, aliases in roster.items():
        for a in list(aliases):
            if a == name:
                continue
            if a in seen and seen[a] != name:
                aliases.discard(a)
            else:
                seen[a] = name
    return roster


def _name_pattern(roster: dict[str, set[str]]) -> str | None:
    names = sorted({a for v in roster.values() for a in v if len(a) >= 2}, key=len, reverse=True)
    return "|".join(re.escape(n) for n in names) if names else None


def _canonical(name: str, roster: dict[str, set[str]]) -> str:
    for canon, aliases in roster.items():
        if name in aliases:
            return canon
    return name


# ─── 1. 说话人归属 ───────────────────────────────────────────────────────


def _speaker_of(paragraph: str, prev_paragraphs: list[str],
                roster: dict[str, set[str]], names_re: str | None,
                quote: str = "") -> str | None:
    """推断引号归属；句首称呼指向听话人，与提示语冲突时判为「归属未知」。

    电话戏最容易出错：正文写「X 接了」，下一句开口的往往是打电话的另一方。
    因此当引号以 X 的称呼开头（「X，…」）而提示语又指向 X 时，不猜归属。
    """
    if not names_re:
        return None
    speaker: str | None = None
    after = re.search(_CUE_AFTER.pattern.format(N=names_re), paragraph)
    if after:
        speaker = _canonical(after.group("name"), roster)
    else:
        before = re.search(_CUE_BEFORE.pattern.format(N=names_re), paragraph)
        if before:
            speaker = _canonical(before.group("name"), roster)
        elif prev_paragraphs:
            last = prev_paragraphs[-1]
            if not extract_quotes(last):
                silent = re.search(_SILENT_CUE.pattern.format(N=names_re), last)
                if silent:
                    speaker = _canonical(silent.group("name"), roster)
    if not speaker:
        return None
    voc = _VOCATIVE.match(quote or "")
    if voc and voc.group("name") in roster.get(speaker, set()):
        return None
    return speaker


def _possession_facts(paragraphs: list[str], roster: dict[str, set[str]],
                      names_re: str | None) -> list[dict]:
    """跟踪「谁拿着什么」：返回 [{paragraph, holder, obj}]，按段号升序。"""
    facts: list[dict] = []
    last_name: str | None = None
    for idx, para in enumerate(paragraphs, 1):
        if names_re:
            for m in re.finditer(names_re, para):
                last_name = _canonical(m.group(0), roster)
        if not names_re:
            continue
        for m in re.finditer(
                r"(?P<holder>" + names_re + r"|他|她)[^。！？\n]{0,8}(?P<verb>"
                + _POSSESS_VERBS + r")(?:了|着|起|到|进|出|下|上|在)?"
                r"(?P<obj>[^，。！？\n]{2,6})", para):
            holder = m.group("holder")
            if holder in ("他", "她"):
                if not last_name:
                    continue
                holder = last_name
            obj = m.group("obj").strip("的地得了着")
            obj = _nominal_tail(obj) or obj
            if obj:
                facts.append({"paragraph": idx, "holder": holder, "obj": obj})
    return facts


def _same_object(a: str, b: str) -> bool:
    """器物同一性：去修饰后比中心字（「纸箱」≈「箱子」≈「箱」）。"""
    def head(s: str) -> str:
        s = re.sub(r"^(纸|木|铁|塑料|大|小|旧|新|那只|这只|一个|一只)", "", s)
        s = re.sub(r"[子儿]$", "", s)
        return s
    ha, hb = head(a), head(b)
    return bool(ha and hb) and (ha == hb or ha in hb or hb in ha)


def _check_speaker(paragraphs: list[str], roster: dict[str, set[str]],
                   names_re: str | None) -> list[Finding]:
    findings: list[Finding] = []
    facts = _possession_facts(paragraphs, roster, names_re)

    for idx, para in enumerate(paragraphs, 1):
        quotes = extract_quotes(para)

        # R1 同一对引号里出现两组话（问 + 答）
        for _s, _e, body in quotes:
            qm = body.find("？")
            if qm < 0:
                qm = body.find("?")
            if qm < 0:
                continue
            head, tail = body[:qm], body[qm + 1:]
            if len(tail.strip()) < 6 or not _ANSWER_MARKERS.search(tail):
                continue
            # 回答之后又出现问号 = 同一个人连着追问，不是两个人的问答
            if "？" in tail or "?" in tail:
                continue
            hp, tp = person_markers(head), person_markers(tail)
            perm = _PERMISSION_ASK.search(head)
            # 只有「向对方请示」或「问句里直接出现第二人称」才算一问一答；
            # 自问自答（「我总得知道什么时候回吧？总不能半夜也替您回消息。」）
            # 形式相同，但说话人是同一个，不算冲突。
            if not perm and "second" not in hp:
                continue
            if perm:
                level = "definite"
            elif "second" in tp or not tp:
                level = "suspected"
            else:
                continue
            findings.append(Finding(
                category="speaker", check="quote_two_turns", level=level,
                paragraph=idx,
                message="同一对引号内疑似两组对话（问句后紧跟回答），可能把两个人的话并进了一句",
                quote=body,
            ))

        # R2 相邻问答逐字复述：疑为说话人错位
        if idx < len(paragraphs):
            q = "".join(body for _s, _e, body in quotes)
            nq = "".join(body for _s, _e, body in extract_quotes(paragraphs[idx]))
            if q and nq:
                common = _longest_common(q, nq)
                if len(common) >= 6:
                    findings.append(Finding(
                        category="speaker", check="echo_answer", level="suspected",
                        paragraph=idx,
                        message=f"问答两句重复了「{common}」，疑为同一信息被分给了两个人",
                        quote=q,
                        evidence=[{"paragraph": idx + 1, "text": paragraphs[idx][:60]}],
                    ))

        # R4 同一段内两组引号、中间没有提示语，且人称发生对调（说话人确实换了）
        if len(quotes) >= 2:
            gap = para[quotes[0][1]:quotes[1][0]]
            m1, m2 = person_markers(quotes[0][2]), person_markers(quotes[1][2])
            flip = bool({"first"} & m1 and {"second"} & m2) or bool(
                {"second"} & m1 and {"first"} & m2)
            # 中间点了人名 = 有归属线索（「…。」她把门带上，「…」），不算无提示
            named = bool(names_re and re.search(names_re, gap))
            if flip and not named and not re.search(_SPEECH_VERBS, gap):
                findings.append(Finding(
                    category="speaker", check="two_quotes_no_cue", level="suspected",
                    paragraph=idx,
                    message="同一段内出现两组引号但没有提示语，说话人无法区分",
                    quote=" / ".join(b for _s, _e, b in quotes[:2]),
                ))

        # R5 持物冲突：说话人自称拿到了别人手上正拿着的东西
        for _s, _e, body in quotes:
            if not re.search(r"我(?:已经|刚刚|刚|先)?(?:拿到|拿了|收着|抱着|拿着|拎着|带着)", body):
                continue
            speaker = _speaker_of(para, paragraphs[:idx - 1], roster, names_re, body)
            if not speaker:
                continue
            holder_fact = None
            for f in facts:
                if f["paragraph"] < idx:
                    holder_fact = f
            if not holder_fact or holder_fact["holder"] == speaker:
                continue
            obj = re.search(
                r"(?:拿到|拿了|收着|抱着|拿着|拎着|带着)(?:了)?(?P<obj>[^，。！？\n]{0,4})", body)
            token = (obj.group("obj") if obj else "").strip()
            generic = (not token) or token in _GENERIC_OBJECTS or token.endswith("东西")
            if generic or _same_object(token, holder_fact["obj"]):
                findings.append(Finding(
                    category="speaker", check="possession_conflict", level="definite",
                    paragraph=idx,
                    message=(
                        f"「{speaker}」自称拿到了东西，但正文此前写的是"
                        f"「{holder_fact['holder']}」在拿「{holder_fact['obj']}」——疑为台词归属颠倒"
                    ),
                    quote=body,
                    evidence=[{"paragraph": holder_fact["paragraph"],
                               "text": paragraphs[holder_fact["paragraph"] - 1][:60]}],
                ))
    return findings


def _longest_common(a: str, b: str) -> str:
    """最长公共子串（中文按字），只用于重复检测。"""
    if not a or not b:
        return ""
    best, best_len = "", 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best_len:
                    best_len, best = cur[j], a[i - best_len:i]
        prev = cur
    return best


# ─── 2. 时间线 ───────────────────────────────────────────────────────────


@dataclass
class TimeMention:
    paragraph: int
    subject: str
    kind: str  # absolute | relative
    year: int | None = None
    span: tuple[int, int] | None = None  # relative: (low, high)
    text: str = ""

    def midpoint(self) -> float | None:
        if self.year is not None:
            return float(self.year)
        if self.span:
            return (self.span[0] + self.span[1]) / 2
        return None


_ABS_YEAR_4 = re.compile(r"(?P<y>1[89]\d{2}|20\d{2})\s*年")
_ABS_YEAR_CN = re.compile(r"(?P<y>[一九二〇零〇○]{1}[〇零一二三四五六七八九]{3})\s*年")
_ABS_YEAR_SHORT = re.compile(r"(?P<y>[〇零一二三四五六七八九]{2})\s*年")
_REL_YEAR = re.compile(r"(?P<n>[0-9]+|[一二三四五六七八九十百两]+)(?P<more>多)?\s*年(?:前|以前)")
_REL_WORDS = {"去年": (1, 1), "前年": (2, 2), "大前年": (3, 3), "今年": (0, 0)}


def parse_years(text: str) -> list[tuple[int, int, int]]:
    """返回 [(year, start, end)]；两位/中文年份只在像年份的上下文里认。"""
    out: list[tuple[int, int, int]] = []
    for m in _ABS_YEAR_4.finditer(text):
        out.append((int(m.group("y")), m.start(), m.end()))
    for m in _ABS_YEAR_CN.finditer(text):
        year = _cn_number(m.group("y"))
        if year and 1800 <= year <= 2099:
            out.append((year, m.start(), m.end()))
    for m in _ABS_YEAR_SHORT.finditer(text):
        raw = m.group("y")
        # 只认「印着/写着/是」后的两位年份，避免把「三年」「五年」当日期
        prefix = text[max(0, m.start() - 6):m.start()]
        if not re.search(r"印着|写着|标着|是|于|的|：|:", prefix):
            continue
        val = _cn_number(raw)
        if val is None:
            continue
        year = 1900 + val if val >= 50 else 2000 + val
        out.append((year, m.start(), m.end()))
    return out


def parse_relative(text: str) -> list[tuple[tuple[int, int], int, int]]:
    """返回 [((low, high), start, end)]，单位：距「现在」的年数。"""
    out: list[tuple[tuple[int, int], int, int]] = []
    for m in _REL_YEAR.finditer(text):
        n = _cn_number(m.group("n"))
        if n is None or n > 200:
            continue
        low, high = (n, n + 9) if m.group("more") else (n, n)
        out.append(((low, high), m.start(), m.end()))
    for word, span in _REL_WORDS.items():
        for m in re.finditer(word, text):
            out.append((span, m.start(), m.end()))
    return out


def _subject_near(text: str, start: int, roster: dict[str, set[str]],
                  entity_names: Iterable[str] = (), end: int | None = None) -> str:
    """取时间表达附近最近的名词：先认器物词表，再认项目实体名。

    窗口**双向**：中文的定语既可能在时间词前（「报纸上的八年前」），也可能在后
    （「二十多年前的老药」）。只回看左侧会把后者挂在「的老」这类残字上。
    距离按字符间隔算，重叠记 0；同距离取更长的词（「报纸」优先于「纸」）。
    """
    end = start if end is None else end
    lo, hi = max(0, start - 20), min(len(text), end + 14)
    nouns = [n for n in _ARTIFACT_NOUNS]
    nouns += [n for n in list(entity_names) + list(roster) if n and len(n) >= 2]
    best: tuple[int, str] | None = None
    for noun in nouns:
        for m in re.finditer(re.escape(noun), text[lo:hi]):
            s, e = lo + m.start(), lo + m.end()
            dist = 0 if (e > start and s < end) else min(abs(start - e), abs(s - end))
            if best is None or dist < best[0] or (dist == best[0] and len(noun) > len(best[1])):
                best = (dist, noun)
    if best:
        return best[1]
    left = text[max(0, start - 18):start]
    tail = re.sub(r"^[，。！？、：:；;“”「」\s]+", "", left[-6:])
    return tail or "（未指明）"


def _mentions(text: str, roster, entity_names) -> list[TimeMention]:
    paras = split_paragraphs(text)
    out: list[TimeMention] = []

    def snip(para: str, s: int, e: int) -> str:
        return para[max(0, s - 12):min(len(para), e + 10)].strip("，。！？、：:；; “”「」")

    for idx, para in enumerate(paras, 1):
        for year, s, e in parse_years(para):
            out.append(TimeMention(idx, _subject_near(para, s, roster, entity_names, e),
                                   "absolute", year=year, text=snip(para, s, e)))
        for span, s, e in parse_relative(para):
            out.append(TimeMention(idx, _subject_near(para, s, roster, entity_names, e),
                                   "relative", span=span, text=snip(para, s, e)))
    return out


def _iter_strings(obj, depth: int = 0):
    """递归遍历 JSON 里的所有字符串（限深，避免异常结构拖死）。"""
    if depth > 3:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for val in obj.values():
            yield from _iter_strings(val, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for val in obj:
            yield from _iter_strings(val, depth + 1)


def _plot_anchor(plot_bible: dict | None) -> tuple[int | None, str]:
    """从设定里抽故事年份锚点。**绝不写死年份**：没有就不给，宁可不判。

    先认显式字段（story_year / year / era / timeline / setting），再退化为
    「整份 plot_bible 里唯一出现的 4 位年份」。出现多个年份且无法分出主次时
    返回 None——这种情况多半是回忆年表，硬选一个会把整章时间线判错。
    """
    if not isinstance(plot_bible, dict):
        return None, "无 plot_bible"
    for key in ("story_year", "year", "era", "timeline", "setting"):
        val = plot_bible.get(key)
        if val is None:
            continue
        m = re.search(r"(1[89]\d{2}|20\d{2})", str(val))
        if m:
            return int(m.group(1)), f"plot_bible.{key}"

    counts: dict[int, int] = {}
    for s in _iter_strings(plot_bible):
        for m in re.finditer(r"(1[89]\d{2}|20\d{2})", s):
            year = int(m.group(1))
            counts[year] = counts.get(year, 0) + 1
    if len(counts) == 1:
        return next(iter(counts)), "plot_bible（唯一 4 位年份）"
    if counts:
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            return ranked[0][0], "plot_bible（出现最多的 4 位年份）"
        return None, "plot_bible 出现多个年份且分不出主年代"
    return None, "plot_bible 未声明年代锚点"


def _span_label(m: TimeMention) -> str:
    """时间表述的可读标签：绝对年份直接给，相对年代给区间（「二十多年前」→ 20~29 年前）。"""
    if m.kind == "absolute" and m.year is not None:
        return f"{m.year} 年"
    if m.span:
        lo, hi = m.span
        return f"{lo} 年前" if lo == hi else f"{lo}~{hi} 年前"
    return "未标明"


def _check_timeline(text: str, prior_chapters: list[dict], roster,
                    entity_names, anchor: int | None) -> tuple[list[Finding], list[dict]]:
    findings: list[Finding] = []
    notes: list[dict] = []
    current = _mentions(text, roster, entity_names)

    # T1 同章：不同器物的相对年代跨度相差过大（同一处的旧物通常属于同一时期）
    rel = [m for m in current if m.kind == "relative" and m.midpoint() is not None]
    for i, a in enumerate(rel):
        for b in rel[i + 1:]:
            if a.subject == b.subject:
                continue
            gap = abs((a.midpoint() or 0) - (b.midpoint() or 0))
            if gap >= 8:
                findings.append(Finding(
                    category="timeline", check="chapter_age_gap", level="suspected",
                    paragraph=a.paragraph,
                    message=(
                        f"同一章的旧物年代跨度约 {gap:.0f} 年："
                        f"「{a.subject}」记作 {_span_label(a)}，"
                        f"「{b.subject}」记作 {_span_label(b)}；若是同一处的物品需确认"
                    ),
                    evidence=[
                        {"paragraph": a.paragraph, "text": a.text},
                        {"paragraph": b.paragraph, "text": b.text},
                    ],
                ))

    # T2 相对年代之间没有锚点时，无法换算成绝对年份
    if any(m.kind == "relative" for m in current) and anchor is None:
        notes.append({"check": "timeline_anchor",
                      "reason": "本章有相对时间表述，但项目未声明年代锚点，绝对年份不可换算"})

    # T3 跨章：同一器物一处给绝对年份、一处给相对年代
    for m in current:
        for chap in prior_chapters:
            if chap.get("chapter") == 0:
                continue
            prior_mentions = _mentions(chap.get("text") or "", roster, entity_names)
            for pm in prior_mentions:
                if pm.subject != m.subject:
                    continue
                # 互斥判定必须能指出**两处原文**，所以证据永远是成对出现
                pair = [
                    {"chapter": chap.get("chapter"), "paragraph": pm.paragraph,
                     "text": pm.text},
                    {"chapter": "current", "paragraph": m.paragraph, "text": m.text},
                ]
                if pm.kind == m.kind:
                    if pm.kind == "absolute" and pm.year is not None and m.year is not None:
                        if abs(pm.year - m.year) > 1:
                            findings.append(Finding(
                                category="timeline", check="cross_chapter_year_clash",
                                level="definite", paragraph=m.paragraph,
                                message=(f"同一实体「{m.subject}」在两章的年份互斥："
                                         f"第{chap.get('chapter')}章 {pm.year} 年 / 本章 {m.year} 年"),
                                evidence=pair,
                            ))
                    continue
                # 一绝对一相对：有锚点才算互斥，没有锚点只能报疑似
                if anchor is not None:
                    rel_m = m if m.kind == "relative" else pm
                    abs_m = pm if m.kind == "relative" else m
                    mid = rel_m.midpoint() or 0
                    implied = anchor - mid
                    if abs_m.year is not None and abs(abs_m.year - implied) > 8:
                        findings.append(Finding(
                            category="timeline", check="cross_chapter_year_clash",
                            level="definite", paragraph=m.paragraph,
                            message=(f"同一实体「{m.subject}」的年代互斥："
                                     f"一处 {abs_m.year} 年，另一处约 {implied:.0f} 年（按锚点 {anchor} 折算）"),
                            evidence=pair,
                        ))
                else:
                    findings.append(Finding(
                        category="timeline", check="cross_chapter_year_clash",
                        level="suspected", paragraph=m.paragraph,
                        message=(f"同一实体「{m.subject}」一处写绝对年份、一处写相对年代，"
                                 "项目没有年代锚点，无法判定是否互斥"),
                        evidence=pair,
                    ))
    return findings, notes


# ─── 3. 指代不明 ─────────────────────────────────────────────────────────


def _check_reference(text: str, prior_text: str, roster,
                     entity_names) -> list[Finding]:
    findings: list[Finding] = []
    paras = split_paragraphs(text)
    known = prior_text + " " + " ".join(list(roster) + list(entity_names))
    for idx, para in enumerate(paras, 1):
        for m in _REF_QUERY.finditer(para):
            noun = _central_noun(m.group("noun"), require_nominal=False)
            if not noun:
                continue
            if noun in known or noun in text.replace(para, ""):
                continue
            findings.append(Finding(
                category="reference", check="no_antecedent", level="definite",
                paragraph=idx,
                message=f"「{noun}」在本章被当作已知对象追问，但前文与设定里都没有出现过",
                quote=m.group(0),
            ))
        for m in _DEFINITE_NP.finditer(para):
            noun = _central_noun(m.group("noun"))
            # 只认名词短语：「这个签了」「这个也给您」「这个干什么」是人称/动词开头，
            # 不构成「定指名词缺来源」，否则正常行文会被大面积误报。
            if not noun or noun[0] in _TIME_HEADS:
                continue
            if noun in known:
                continue
            findings.append(Finding(
                category="reference", check="definite_np_unknown", level="suspected",
                paragraph=idx,
                message=f"定指短语「{m.group(0)}」所指的「{noun}」在前文没有来源",
                quote=m.group(0),
            ))
    return findings


# ─── 4. 伏笔与提前点破 ───────────────────────────────────────────────────


def _clue_tokens(entry: str) -> set[str]:
    """从大纲条目里抽「可识别线索」：引号内的词、四位年份、带书名号的词。"""
    tokens: set[str] = set()
    for m in re.finditer(r"[「“]([^」”]{2,20})[」”]", entry):
        tokens.add(m.group(1))
    for m in re.finditer(r"(?:1[89]\d{2}|20\d{2})\s*年", entry):
        tokens.add(m.group(0).replace(" ", ""))
    for m in re.finditer(r"《([^》]{2,20})》", entry):
        tokens.add(m.group(1))
    return tokens


def _question_nouns(entries: Iterable[str]) -> set[str]:
    """大纲里仍在追问的对象名。剥掉「不写 / 本章」这类限定前缀，只留名词。"""
    out: set[str] = set()
    for entry in entries:
        for m in re.finditer(r"([\u4e00-\u9fa5]{2,6})(?:是谁|是什么|怎么回事|为什么|去哪)", entry):
            noun = re.sub(
                r"^(?:不写|不说|不解释|不提前|不得出现|不引入|本章|这一?章"
                r"|(?:不|别|没)(?:写|说|讲|问|解释|提前|出现|引入))", "", m.group(1))
            if len(noun) >= 2:
                out.add(noun)
    return out


def _prohibitions(entry: str) -> list[str]:
    """取「不写 X、不解释 Y」里的宾语短语，供「违反本章约束」检查。"""
    out: list[str] = []
    for m in re.finditer(r"(?:不写|不说|不解释|不提前|不得出现|不引入)([^。；\n]{2,40})", entry):
        seg = m.group(1)
        for part in re.split(r"[、，,]|和|与", seg):
            part = part.strip(" 。；：:）)")
            if len(part) >= 2:
                out.append(part)
    return out


def _check_foreshadow(text: str, outline: list, chapter: int | None,
                      roster) -> list[Finding]:
    findings: list[Finding] = []
    entries = [str(e) for e in (outline or []) if str(e).strip()]
    if not entries:
        return findings

    # F1 未来章节的专属线索提前出现
    if chapter:
        future = entries[chapter:]  # entries 是 1-based 第 1 章起
        # 「线索」= 大纲引号里的具体物件/说法。人名不算线索（角色每章都可能出现），
        # 本章大纲自己写到的条目也不算提前（那是本章安排的内容）。
        roster_names = {a for v in roster.values() for a in v}
        seen_prior = " ".join(entries[:chapter - 1])
        own = entries[chapter - 1] if chapter - 1 < len(entries) else ""
        for offset, entry in enumerate(future, chapter + 1):
            for token in _clue_tokens(entry):
                if token in roster_names or token in seen_prior or token in own:
                    continue
                if len(token) < 3 or token not in text:
                    continue
                findings.append(Finding(
                    category="foreshadow", check="future_clue_leak", level="suspected",
                    paragraph=_paragraph_of(text, token),
                    message=f"大纲第{offset}章才出现的线索「{token}」在本章已经出现",
                    quote=token,
                ))

    # F2 大纲仍在问「X 是谁」，正文却给出了确定身份
    questions = _question_nouns(entries)
    if questions:
        names_re = _name_pattern(roster)
        if names_re:
            for idx, para in enumerate(split_paragraphs(text), 1):
                for m in re.finditer(_IDENTITY_ASSERT.pattern.format(N=names_re), para):
                    findings.append(Finding(
                        category="foreshadow", check="premature_identity", level="suspected",
                        paragraph=idx,
                        message=(f"大纲里「{'、'.join(sorted(questions))}」等身份仍未揭开，"
                                 f"本章却断言「{m.group(0)}」"),
                        quote=m.group(0),
                    ))

    # F3 违反本章大纲明确的「不写」清单
    if chapter and 1 <= chapter <= len(entries):
        for phrase in _prohibitions(entries[chapter - 1]):
            # 疑问式宾语（「某某是谁」「某年发生了什么」）本身不是可检索的名词，
            # 只剥前缀会剩「是谁」，必然误命中，直接跳过。
            if re.search(r"谁|什么|怎么|为什么|哪|如何", phrase):
                continue
            core = re.sub(r"^(这一?章|本章|他|她|它)", "", phrase)
            if len(core) < 2:
                continue
            if core in text:
                findings.append(Finding(
                    category="foreshadow", check="chapter_prohibition", level="definite",
                    paragraph=_paragraph_of(text, core),
                    message=f"本章大纲明确要求不写「{phrase}」，正文里出现了「{core}」",
                    quote=core,
                ))
    return findings


def _paragraph_of(text: str, needle: str) -> int:
    for idx, para in enumerate(split_paragraphs(text), 1):
        if needle in para:
            return idx
    return 0


# ─── 入口 ────────────────────────────────────────────────────────────────


def run_coherence(
    text: str,
    *,
    plot_bible: dict | None = None,
    outline: list | None = None,
    chapter: int | None = None,
    prior_chapters: list[dict] | None = None,
    entity_names: Iterable[str] = (),
) -> dict:
    """跑四类检查，返回可直接写进 `quality_json` 的纯数据。

    Args:
        text: 待检正文。
        plot_bible: 项目设定（角色名、称谓、可选年代锚点）。
        outline: 每章一条的大纲文本（1-based）。
        chapter: 本章章号；None 时跳过「未来线索」与「不写清单」两项。
        prior_chapters: `[{"chapter": int, "text": str}]`，只含本章之前的正文。
        entity_names: 已有实体名（来自 KG 等），用于指代与说话人识别。

    Returns:
        dict：`version` / `chapter` / `counts` / `findings` / `notes` / `anchors`。
    """
    roster = build_roster(plot_bible)
    names_re = _name_pattern(roster)
    paras = split_paragraphs(text or "")
    prior = prior_chapters or []
    prior_text = " ".join(str(c.get("text") or "") for c in prior)
    anchor, anchor_src = _plot_anchor(plot_bible)

    findings: list[Finding] = []
    notes: list[dict] = []
    if not paras:
        return {"version": CHECK_VERSION, "chapter": chapter,
                "counts": {"definite": 0, "suspected": 0},
                "findings": [], "notes": [{"check": "empty", "reason": "正文为空"}],
                "anchors": {"year": anchor, "source": anchor_src}}

    findings += _check_speaker(paras, roster, names_re)

    tl, tl_notes = _check_timeline(text, prior, roster, entity_names, anchor)
    findings += tl
    notes += tl_notes

    findings += _check_reference(text, prior_text, roster, entity_names)
    findings += _check_foreshadow(text, list(outline or []), chapter, roster)

    findings.sort(key=lambda f: (f.paragraph, f.category, f.check))
    counts = {"definite": 0, "suspected": 0}
    for f in findings:
        counts[f.level] = counts.get(f.level, 0) + 1
    return {
        "version": CHECK_VERSION,
        "chapter": chapter,
        "counts": counts,
        "findings": [f.to_dict() for f in findings],
        "notes": notes,
        "anchors": {"year": anchor, "source": anchor_src},
    }
