"""逐章推进台账（v1）：角色阵容 / 属性推进 / 章间衔接。

## 它补的是哪一类问题

现有的文风、篇幅、密度判据回答「写得像不像小说」；`structure_check` 回答
「本章大纲的情节点有没有写进去」。两者都测不出**故事有没有在往前走**：

- 连续几十章都是同一小圈人，没有新角色进来；
- 主角之外的角色的境界 / 技能 / 道具从第 23 章到第 32 章一次没变；
- 章与章之间时间跳了、地点换了，却没有一句交代，上一章抛出的问题也没人再提。

本模块只做**离线回放**：输入已落库的若干章正文，输出逐章台账。它是纯函数，
不读写数据库、不改正文、不改 `passed`、不进 `violations`。接线留给后续会话。

## 三类指标的口径与出处

1. **角色阵容**：名单来自 `plot_bible.characters`（`coherence.build_roster` 负责
   姓名 + 别名 + 称谓归一），正文里按名单匹配，不写死任何名字。新登场章 =
   该角色第一次在正文出现的章；相邻章重合度 = 两章出场集合的 Jaccard；
   「同一小圈人」= 重合度 ≥ 阈值且集合大小 ≤ 阈值的连续区间。
2. **属性推进**：三类。境界阶梯来自 `plot_bible.realm_order`（与
   `kg.sentinel` 同一份数据，按体系分组、跨体系不互比）；技能按「习得类动词 +
   宾语」抽；道具/资源按「获得 / 失去 / 交接」三类动词抽，外加
   `plot_bible.derived_item_names` 这类显式声明的关键道具。变化点 = 该章抽到的
   值与上一已知值不同（境界按体系比，技能/道具按集合增删比）。
3. **章间衔接**：上一章末段与本章首段的共享角色、地点、时间线索；时间线索
   由**通用中文时间词**解析（第二天 / 三天后 / 一周后…），不认任何作品专有词。
   「跳跃未交代」= 跳跃天数 ≥ 阈值，且首段既没有过渡词、也没有与前章末段
   共享角色或地点。上一章末段的问题按疑问句抽，在本章的二元组覆盖率 ≥ 阈值
   才算「被再次提及」。

情节点覆盖 / 越章材料 / 性别一致**不在这里重算**：`structure_checks` 传进来
（落库的 `quality_json.structure_check`），缺的章才用 `structure_check.check_structure`
补算，来源在 `structure.source` 逐章标注。

## 阈值

全部来自 `config.py` 的 `progress_ledger_*` 字段；字段为 `None` 时用本模块
`DEFAULTS`。所有数值都标了「待验证」——它们是没有人工标注样本校准的观察口径，
不是判罚线；本模块也不判罚。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from app.coherence import build_roster, parse_relative, split_paragraphs
from app.generation.structure_check import _bigrams as _content_bigrams
from app.text_numbers import cn_number as _cn_number

LEDGER_VERSION = "progress-ledger-v1"

#: 阈值的兜底值（`config.py` 的 `progress_ledger_*` 为 None 时生效）。
#:
#: 逐条写明「待验证」：这些数字是观察口径的起点，不是从样本校准出来的
#: 推导值。校准方式见报告里的「口径与出处」一节。
DEFAULTS: dict[str, Any] = {
    # 相邻两章出场集合重合度 ≥ 它才算「高度重合」。0.8 = 五个里换一个。
    # **待验证**：没有真实章节对的重合度分布可查。
    "cast_overlap_high": 0.8,
    # 「同一小圈人」的集合大小上限。6 人以内且持续重合，才像「小圈子打转」。
    # **待验证**：一章有名有姓的角色通常 4~6 个，取 6 作上限；未按样本校准。
    "cast_core_max": 6,
    # 连续重合区间达到多少章才判「阵容停滞」。
    # **待验证**：20 章是「一卷左右」的粗粒度，未被样本校准。
    "cast_stagnant_chapters": 20,
    # 某个属性连续多少章没有变化才判「原地不动」。
    # **待验证**：10 章是「一个推进单元」的粗粒度，未被样本校准。
    "attr_stale_chapters": 10,
    # 章间衔接看首段 / 末段各几个自然段。
    # **待验证**：1 段是「开场第一个镜头」的最小口径，未按误报率校准。
    "head_paragraphs": 1,
    "tail_paragraphs": 1,
    # 从上一章末几段里找「留给下一章的问题」。
    # **待验证**：3 段覆盖常见的章末悬念段，未按召回率校准。
    "question_tail_paragraphs": 3,
    # 每次最多记几个未闭合问题。
    "question_top": 3,
    # 问题「被再次提及」的二元组覆盖率阈值。与 `structure_beat_min_coverage`
    # 同口径（同为 0.34），便于横向比较；**待验证**：未单独校准。
    "question_overlap": 0.34,
    # 时间跳跃达到多少天才需要交代。1 天内的「当晚 / 第二天」按正常过场处理。
    # **待验证**：2 天是「跨过一个完整日夜」的直觉口径，未校准。
    "time_jump_min_days": 2,
    # 章末落库时回看的章数（含当前章）。窗口太大则单章成本上升，太小则
    # 「连续零变化」看不出长度；8 章 ≈ 两卷内的一个推进单元。
    # **待验证**：未按「能发现多长的停滞」反推，取 8 只为控制单章读取量。
    "window_chapters": 8,
}


# ─── 通用工具 ────────────────────────────────────────────────────────────

#: 句子边界：中文句读 + 换行。属性归属只在**同一句**内成立，跨句不猜。
_SENT_END = re.compile(r"[。！？!?；;\n]+")

#: 地点后缀。这是**通用汉语构词线索**（院/站/区/楼/巷…），不是某本书的场景词。
_LOC_SUFFIX = (
    "院|站|巷|街|区|市|场|店|屋|房|间|室|厅|局|所|处|路|桥|河|山|寺|庙|宫|殿"
    "|城|村|镇|馆|堂|校|厂|库|楼|层|口"
)
_LOC_RE = re.compile(rf"(?P<loc>[\u4e00-\u9fff]{{1,3}}(?:{_LOC_SUFFIX}))")

#: 地点候选的前导功能字：`到跑腿站` → `跑腿站`。
_LOC_LEAD = set("到去在回进出从的了和与把被这那个个些又再就都也还正")

#: 时间词 → 向前推进的天数。**通用词**，不含任何作品专有说法。
_REL_DAY_WORDS: tuple[tuple[str, int], ...] = (
    ("大后天", 3), ("大前天", -3), ("后天", 2), ("前天", -2),
    ("第二天", 1), ("次日", 1), ("翌日", 1), ("隔天", 1), ("转天", 1),
    ("第三天", 2), ("当天", 0), ("当晚", 0), ("同一天", 0), ("那天", 0),
    ("今天", 0), ("明天", 1), ("昨天", -1), ("早上", 0), ("清晨", 0),
    ("上午", 0), ("中午", 0), ("下午", 0), ("傍晚", 0), ("晚上", 0),
    ("夜里", 0), ("半夜", 0), ("过了一会儿", 0), ("一会儿", 0), ("稍后", 0),
    ("不久", 0),
)
_REL_SPAN_RE = re.compile(
    r"(?P<n>[0-9一二三四五六七八九十两]+)\s*(?P<unit>天|日|周|星期|个月|月|年|小时|分钟)(?:以)?后"
    r"|(?P<half>半)\s*(?P<half_unit>天|个月|年)(?:以)?后"
)

#: 时间跳跃的「交代词」：出现即视为作者写了过渡，而不是硬切。
_TIME_BRIDGE = (
    "过去", "之后", "这期间", "那几天", "这几天", "这段时间", "期间", "一直",
    "忙着", "直到", "等到", "后来", "中间", "一晃", "转眼", "忙完", "处理完",
    "隔了", "过了", "没几天", "接下来的",
)

#: 疑问句线索：问号，或没有问号但带追问词。裸「谁/什么」不够：`谁都别恢复`
#: 不是问题，所以再叠一层否定式短语过滤（见 `_open_questions`）。
_INTERROGATIVE = re.compile(
    r"[？?]|是不是|难道|到底|怎么办|为什么|怎么会|怎么回事|是谁|谁在|谁能|谁知道"
    r"|谁会|谁要|谁的|什么|哪儿|哪里|怎么"
)
#: 「谁都别 / 什么都 / 怎么都」这类是任指，不是提问。
_NON_QUESTION = re.compile(r"谁都别|谁也别|什么都|什么也|怎么都|谁都不|谁也不")

#: 习得类动词。只认「学会 / 练成 / 掌握」这类**获得**语义；`施展 / 使出`
#: 是使用，不是变化，单独记账。
_SKILL_ACQUIRE_RE = re.compile(
    r"(?:学会了|学会|练成了|练成|练会|练会了|掌握了|掌握|习得了|习得|悟出了|悟出"
    r"|领悟了|领悟|精通了|精通|摸透了|摸透|入门了|入门)(?:了)?"
    r"(?P<skill>[^\s，。！？；：、,.!?;:“”‘’「」『』（）()]{2,8})"
)
_SKILL_USE_RE = re.compile(
    r"(?:施展出|施展|使出|动用|祭出|催动|用起|打出|捏起|念动)"
    r"(?P<skill>[^\s，。！？；：、,.!?;:“”‘’「」『』（）()]{2,8})"
)

#: 技能宾语的拒绝前缀：这些开头说明抽到的是动词短语或指示语，不是技能名。
_SKILL_BAD_HEAD = (
    "怎么", "如何", "怎样", "什么", "这种", "那种", "一门", "一套", "几招",
    "这个", "那个", "这些", "那些", "了", "的", "着", "是", "不", "没", "都",
    "也", "就", "又", "还", "把", "被", "将", "给", "让", "从", "向", "对",
    "跟", "和", "与", "在", "有", "要", "会", "能", "可以", "先", "再", "别",
    "该", "得", "只", "便", "才", "正", "刚", "一", "两", "几",
)

#: 道具候选的拒绝字符：出现这些说明抽到的是短语/指代，不是物件名。
_ITEM_BAD_CHARS = set("的了着是与就都也还很太你我他她它其什么怎么这那")
#: 道具候选的拒绝前缀：副词/连词开头的一律不是物件。
_ITEM_BAD_HEAD = (
    "后来", "然后", "接着", "于是", "等到", "只要", "只有", "只是", "便", "才",
    "又", "再", "正", "刚", "还", "也", "都", "先", "所以", "但是", "因为",
)
#: 道具候选的尾部动词/方位词：`封条递` → `封条`。
_ITEM_TRAIL = "递交给还送借塞放了过去回来出起下上走"
#: 数量词：`一条后台通知` → `后台通知`。
_ITEM_NUM = "一二三四五六七八九十两几半整这那"
_ITEM_MEASURE = "条张块枚份只把支根片颗本套个封件袋瓶盒"

#: 道具「获得」动词。只认改变归属的，`掏出/摸出` 只是拿出来，不算变化。
_ITEM_GAIN_RE = re.compile(
    r"(?:拿到了|得到了|收下了|捡到了|买下了|换到了|接过了|继承了|拿到|拿了|收下|收到"
    r"|捡到|捡起|买下|换到|接过|继承)"
    r"(?P<item>[^\s，。！？；：、,.!?;:“”‘’「」『』（）()]{2,8})"
)
#: 道具「交出 / 失去」动词。两种语序都要覆盖。
_ITEM_LOSS_RE = re.compile(
    r"(?:把|将)(?P<item_a>[^\s，。！？；：、,.!?;:“”‘’「」『』（）()把将]{2,8}?)"
    r"(?:丢了|弄丢|交出|交还|还了|卖了|当掉|押了|烧了|毁了|扔了|扔掉了|弄坏了)"
    r"|(?P<item_b>[^\s，。！？；：、,.!?;:“”‘’「」『』（）()把将]{2,8}?)"
    r"(?:丢了|弄丢了|不见了|被收走了|被拿走了)"
)
#: 道具「转手」：把 X 递给 / 交给 Y。
_ITEM_TRANSFER_RE = re.compile(
    r"(?:把|将)(?P<item>[^\s，。！？；：、,.!?;:“”‘’「」『』（）()]{2,8}?)"
    r"(?:递给|交给|送给|借给|还给)(?P<to>[\u4e00-\u9fff]{2,4})"
)
#: 非人主语：`平台拿到名单` 不能归给正在说话的角色。这是通用机构词，
#: 不是某本书的组织名。
_NON_PERSON_SUBJECT = (
    "平台", "公司", "系统", "对方", "单位", "上面", "后台", "总部", "官方",
    "组织", "机构", "部门", "街道", "社区",
)


def _nonperson_subject(text: str, start: int) -> bool:
    """动词前 4 字内是否有显式非人主语。"""
    head = (text or "")[max(0, start - 4):start]
    return head.endswith(_NON_PERSON_SUBJECT)


#: 境界「变化动词」。只用于给证据句加标签，不参与归属。
_REALM_CHANGE_VERB = re.compile(r"突破|晋级|晋升|进阶|踏入|迈入|达到|修到|练到|跌落|跌回|退到|降到")


def resolve_config(settings: Any = None) -> dict[str, Any]:
    """合并兜底值与 `Settings` 覆盖值。`None` 表示「没覆盖」，不是「关闭」。"""
    cfg = dict(DEFAULTS)
    s = settings
    if s is None:
        try:  # 延迟导入：本模块要在无 config 依赖的单测里也能跑
            from app.config import settings as s  # type: ignore
        except Exception:  # noqa: BLE001
            s = None
    if s is not None:
        for key in list(cfg):
            value = getattr(s, f"progress_ledger_{key}", None)
            if value is not None:
                cfg[key] = value
    return cfg


# ─── 基础抽取 ────────────────────────────────────────────────────────────


def _sentences(text: str) -> list[tuple[int, int, str]]:
    """返回 [(start, end, sentence)]，保留位置用于「最近角色」归属。"""
    out: list[tuple[int, int, str]] = []
    start = 0
    for m in _SENT_END.finditer(text or ""):
        seg = text[start:m.start()]
        if seg.strip():
            # 保留原文（含首部缩进），否则别名位置会相对句子起点偏移。
            out.append((start, m.start(), seg))
        start = m.end()
    tail = (text or "")[start:]
    if tail.strip():
        out.append((start, len(text), tail))
    return out


def _sentence_at(text: str, start: int, end: int) -> tuple[int, int, str]:
    """取覆盖 [start, end) 的句子；取不到就给一个短窗口。"""
    for s, e, seg in _sentences(text):
        if s <= start < e or s < end <= e:
            return s, e, seg
    lo, hi = max(0, start - 30), min(len(text), end + 30)
    return lo, hi, text[lo:hi]


def _alias_index(roster: dict[str, set[str]]) -> dict[str, str]:
    """别名 → 规范名。长的在前由调用方排序；这里只做表。"""
    idx: dict[str, str] = {}
    for canon, aliases in roster.items():
        for a in aliases:
            if len(a) >= 2:
                idx.setdefault(a, canon)
    return idx


def _name_re(roster: dict[str, set[str]]) -> re.Pattern | None:
    names = sorted(_alias_index(roster), key=len, reverse=True)
    if not names:
        return None
    return re.compile("|".join(re.escape(n) for n in names))


def _nearest_character(
    text: str,
    *,
    start: int,
    end: int,
    roster: dict[str, set[str]],
    alias_re: re.Pattern | None,
) -> tuple[str | None, str]:
    """把 [start, end) 的短语归给最近的角色。返回 (角色, 归属依据)。

    三级口径，逐级放宽，每级都留 `basis` 便于复核：

    1. `same_sentence`：同一句里只有一个角色 → 归他；多个角色 → 取离短语最近的，
       同距视为歧义、不猜；
    2. `unique_in_paragraph`：同句没名字、但整段只有一个角色 → 归他；
    3. `unattributed`：其余一律不归（宁可漏，不猜）。
    """
    if alias_re is None:
        return None, "unattributed"
    idx = _alias_index(roster)
    s, e, sentence = _sentence_at(text, start, end)
    found = [(m.start() + s, m.end() + s, idx.get(m.group(0)))
             for m in alias_re.finditer(sentence)]
    found = [f for f in found if f[2]]
    if found:
        ranked = sorted({c for _, _, c in found})
        if len(ranked) == 1:
            return ranked[0], "same_sentence"
        # 多个角色：按与短语的字符距离取最近；同距不猜。
        scored = []
        for f_start, f_end, canon in found:
            dist = 0 if (f_end > start and f_start < end) else min(
                abs(start - f_end), abs(f_start - end))
            scored.append((dist, canon))
        scored.sort()
        if len(scored) >= 2 and scored[0][0] == scored[1][0]:
            return None, "ambiguous"
        return scored[0][1], "same_sentence"

    # 段落回退：段内唯一角色才归。
    para_start = text.rfind("\n\n", 0, start)
    para_end = text.find("\n\n", end)
    para = text[para_start + 2 if para_start >= 0 else 0:
               para_end if para_end >= 0 else len(text)]
    chars = {idx[m.group(0)] for m in alias_re.finditer(para) if idx.get(m.group(0))}
    if len(chars) == 1:
        return next(iter(chars)), "unique_in_paragraph"
    return None, "unattributed"


def _cast_of_chapter(
    text: str, roster: dict[str, set[str]], alias_re: re.Pattern | None
) -> tuple[list[str], dict[str, int]]:
    """本章出场集合与提及次数。只认名单内的角色（不猜无名路人）。"""
    if alias_re is None:
        return [], {}
    idx = _alias_index(roster)
    counts: dict[str, int] = {}
    for m in alias_re.finditer(text or ""):
        canon = idx.get(m.group(0))
        if canon:
            counts[canon] = counts.get(canon, 0) + 1
    return sorted(counts), counts


def _locations(text: str) -> set[str]:
    """抽地点：通用后缀构词 + 去功能字前缀。人名不当地点。"""
    out: set[str] = set()
    for m in _LOC_RE.finditer(text or ""):
        loc = m.group("loc")
        while len(loc) > 2 and loc[0] in _LOC_LEAD:
            loc = loc[1:]
        if len(loc) >= 2:
            out.add(loc)
    return out


def _time_cues(text: str) -> list[dict]:
    """返回 [{text, days, span}]。days 正数=向前推进，负数=回溯。"""
    out: list[dict] = []
    used: list[tuple[int, int]] = []

    def taken(a: int, b: int) -> bool:
        return any(a < y and x < b for x, y in used)

    for word, days in sorted(_REL_DAY_WORDS, key=lambda kv: len(kv[0]), reverse=True):
        for m in re.finditer(re.escape(word), text or ""):
            if taken(m.start(), m.end()):
                continue
            used.append((m.start(), m.end()))
            out.append({"text": word, "days": days, "span": [m.start(), m.end()]})

    for m in _REL_SPAN_RE.finditer(text or ""):
        if taken(m.start(), m.end()):
            continue
        if m.group("half"):
            n, unit = 0.5, m.group("half_unit")
        else:
            n, unit = _cn_number(m.group("n")), m.group("unit")
        if n is None:
            continue
        per = {"天": 1, "日": 1, "周": 7, "星期": 7, "个月": 30, "月": 30,
               "年": 365, "小时": 0, "分钟": 0}[unit]
        used.append((m.start(), m.end()))
        out.append({"text": m.group(0), "days": float(n) * per,
                    "span": [m.start(), m.end()]})

    # 已有年份/年代表述（`八年前`）不是章间推进，交给 coherence 的时间线检查，
    # 这里只标成「回溯」不影响跳跃判定。
    for span, s, e in parse_relative(text or ""):
        if taken(s, e):
            continue
        used.append((s, e))
        out.append({"text": (text or "")[s:e], "days": -float(span[0]),
                    "span": [s, e]})
    out.sort(key=lambda r: r["span"][0])
    return out


def _open_questions(text: str, *, tail_paragraphs: int, top: int) -> list[str]:
    """上一章末几段里「留给下一章的问题」：疑问句，按出现顺序取最后几条。"""
    paras = split_paragraphs(text or "")
    if not paras:
        return []
    tail = "\n\n".join(paras[-max(1, tail_paragraphs):])
    picked: list[str] = []
    for _, _, sentence in _sentences(tail):
        s = sentence.strip(" “”「」\"'")
        if len(s) < 6:
            continue
        if not _INTERROGATIVE.search(s):
            continue
        if "？" not in s and "?" not in s and _NON_QUESTION.search(s):
            continue
        picked.append(s)
    return picked[-max(1, top):]


def _question_recall(question: str, text: str, threshold: float) -> dict:
    q_grams = _content_bigrams(question)
    if not q_grams:
        return {"question": question, "coverage": 0.0, "mentioned": False,
                "matched": []}
    t_grams = _content_bigrams(text)
    hit = sorted(q_grams & t_grams)
    coverage = len(hit) / len(q_grams)
    return {
        "question": question,
        "coverage": round(coverage, 3),
        "mentioned": coverage >= threshold and len(hit) >= 2,
        "matched": hit[:10],
    }


# ─── 1. 角色阵容 ──────────────────────────────────────────────────────────


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb) if (sa | sb) else 0.0


def _protagonist(roster: dict[str, set[str]], cards: list[dict],
                 cast_counts: dict[str, int]) -> tuple[str | None, str]:
    """主角判定：角色卡 role 写明「主角」优先；否则取全窗口提及最多的角色。"""
    for card in cards:
        if not isinstance(card, dict):
            continue
        role = str(card.get("role") or "")
        name = str(card.get("name") or "").strip()
        if name in roster and ("主角" in role or "protagonist" in role.lower()):
            return name, "角色卡 role 含「主角」"
    if cast_counts:
        name = max(cast_counts.items(), key=lambda kv: kv[1])[0]
        return name, "全窗口提及次数最多（角色卡未标主角）"
    return None, "无法判定"


def _build_cast(
    chapters: list[dict],
    roster: dict[str, set[str]],
    cards: list[dict],
    cfg: dict,
    alias_re: re.Pattern | None,
) -> dict:
    rows: list[dict] = []
    debuts: dict[str, int] = {}
    total_counts: dict[str, int] = {}
    prev_cast: list[str] | None = None

    cards_by_name = {str(c.get("name") or "").strip(): c
                     for c in cards if isinstance(c, dict)}

    def declared_debut(name: str) -> int | None:
        card = cards_by_name.get(name) or {}
        raw = card.get("首次出场章节", card.get("first_chapter"))
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    for ch in chapters:
        cast, counts = _cast_of_chapter(ch["text"], roster, alias_re)
        for name, n in counts.items():
            total_counts[name] = total_counts.get(name, 0) + n
        new = [n for n in cast if n not in debuts]
        for n in cast:
            debuts.setdefault(n, ch["chapter"])
        overlap = None if prev_cast is None else round(_jaccard(prev_cast, cast), 3)
        rows.append({
            "chapter": ch["chapter"],
            "cast": cast,
            "mentions": counts,
            "new_debuts": new,
            "overlap_prev": overlap,
            "cast_size": len(cast),
        })
        prev_cast = cast

    protagonist, protagonist_rule = _protagonist(roster, cards, total_counts)
    minor = [n for n in roster if n != protagonist]

    # 主角之外角色的覆盖：出现章数 / 窗口章数；merges 合并别名后的口径。
    window_len = len(chapters) or 1
    minor_coverage: dict[str, dict] = {}
    for name in minor:
        present = [r["chapter"] for r in rows if name in r["cast"]]
        minor_coverage[name] = {
            "chapters": len(present),
            "coverage": round(len(present) / window_len, 3),
            "first": present[0] if present else None,
            "last": present[-1] if present else None,
        }

    # 连续「高度重合且核心人数少」的区间：直接回答「是不是同一小圈人在转」。
    runs: list[dict] = []
    current: list[int] = []
    for i, row in enumerate(rows):
        high = (
            row["overlap_prev"] is not None
            and row["overlap_prev"] >= cfg["cast_overlap_high"]
            # 小圈子按**这一对**的较大人数判，避免把上一章的超编集合带进区间。
            and max(row["cast_size"], rows[i - 1]["cast_size"]) <= cfg["cast_core_max"]
        )
        if high:
            if not current:
                current = [i - 1, i]
            else:
                current.append(i)
        else:
            if len(current) >= 2:
                runs.append(_run_row(rows, current))
            current = []
    if len(current) >= 2:
        runs.append(_run_row(rows, current))

    longest = max(runs, key=lambda r: r["chapters"], default=None)
    stagnant = bool(longest and longest["chapters"] >= cfg["cast_stagnant_chapters"])

    # 角色卡声明的首次出场章 vs 本窗口观察到的「窗口内首次出现」。
    # 窗口从第 23 章起，声明在第 1~4 章的角色本来就不该在本窗口「新登场」。
    debut_checks: list[dict] = []
    for name in roster:
        declared = declared_debut(name)
        observed = debuts.get(name)
        if declared is None:
            status = "未在角色卡声明"
        elif observed is None:
            status = "窗口内未出场"
        elif declared < chapters[0]["chapter"]:
            status = "早于窗口（窗口内出现不代表新登场）"
        elif declared == observed:
            status = "与角色卡一致"
        else:
            status = "与角色卡不一致"
        debut_checks.append({
            "character": name,
            "declared": declared,
            "observed_in_window": observed,
            "status": status,
        })

    for row in rows:
        row["protagonist_present"] = bool(protagonist and protagonist in row["cast"])
        row["minor_present"] = [n for n in row["cast"] if n != protagonist]

    return {
        "protagonist": protagonist,
        "protagonist_rule": protagonist_rule,
        "debuts": debuts,
        "debut_scope": "window",
        "declared_debuts": {n: declared_debut(n) for n in roster},
        "debut_checks": debut_checks,
        "new_debuts_by_chapter": {r["chapter"]: r["new_debuts"] for r in rows
                                  if r["new_debuts"]},
        "rows": rows,
        "roster_size": len(roster),
        "minor_coverage": minor_coverage,
        "minor_never_appeared": [n for n, v in minor_coverage.items()
                                 if v["chapters"] == 0],
        "overlap_runs": runs,
        "longest_overlap_run": longest,
        "stagnant": stagnant,
        "thresholds": {
            "overlap_high": cfg["cast_overlap_high"],
            "core_max": cfg["cast_core_max"],
            "stagnant_chapters": cfg["cast_stagnant_chapters"],
        },
    }


def _run_row(rows: list[dict], indices: list[int]) -> dict:
    members = sorted({n for i in indices for n in rows[i]["cast"]})
    overlaps = [rows[i]["overlap_prev"] for i in indices[1:] if rows[i]["overlap_prev"] is not None]
    new_debuts = [n for i in indices for n in rows[i]["new_debuts"]]
    return {
        "start": rows[indices[0]]["chapter"],
        "end": rows[indices[-1]]["chapter"],
        "chapters": len(indices),
        "members": members,
        "member_count": len(members),
        "max_cast_size": max(rows[i]["cast_size"] for i in indices),
        "new_debuts": sorted(new_debuts),
        "avg_overlap": round(sum(overlaps) / len(overlaps), 3) if overlaps else None,
    }


# ─── 2. 属性推进 ──────────────────────────────────────────────────────────


def _realm_systems(plot_bible: dict | None) -> list[tuple[str, ...]]:
    """`realm_order` → 体系列表。一维=单体系；二维=多体系，**跨体系不互比**。"""
    raw = (plot_bible or {}).get("realm_order")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)) and raw and isinstance(raw[0], (list, tuple)):
        return [tuple(str(x) for x in g) for g in raw if g]
    if isinstance(raw, (list, tuple)):
        return [tuple(str(x) for x in raw)]
    return []


def _realm_mentions(
    chapters: list[dict],
    roster: dict[str, set[str]],
    alias_re: re.Pattern | None,
    systems: list[tuple[str, ...]],
) -> tuple[dict, list[dict]]:
    """逐章抽「角色 + 阶梯词」。返回 (per_character_events, unattributed)。

    归属口径见 `_nearest_character`。同一体系内按**本章最后一次提及**作为
    章末值（章内先高后低，说明在写回落，取最后一句更接近章末状态）。
    """
    per_char: dict[str, list[dict]] = {}
    unattributed: list[dict] = []
    if not systems:
        return per_char, unattributed

    word_to = []
    for si, group in enumerate(systems):
        for rank, word in enumerate(group, 1):
            word_to.append((word, si, rank))
    word_to.sort(key=lambda x: len(x[0]), reverse=True)
    realm_re = re.compile("|".join(re.escape(w) for w, _, _ in word_to))

    for ch in chapters:
        text = ch["text"]
        for m in realm_re.finditer(text):
            word = m.group(0)
            si = rank = None
            for w, s_i, r_i in word_to:
                if w == word:
                    si, rank = s_i, r_i
                    break
            if si is None:
                continue
            name, basis = _nearest_character(
                text, start=m.start(), end=m.end(), roster=roster, alias_re=alias_re)
            _, _, sentence = _sentence_at(text, m.start(), m.end())
            if not name:
                unattributed.append({
                    "chapter": ch["chapter"], "value": word,
                    "basis": basis, "sentence": sentence[:80],
                })
                continue
            per_char.setdefault(name, []).append({
                "chapter": ch["chapter"],
                "system": si,
                "rank": rank,
                "value": word,
                "basis": basis,
                "explicit": bool(_REALM_CHANGE_VERB.search(sentence)),
                "sentence": sentence[:100],
            })
    return per_char, unattributed


def _skill_events(
    chapters: list[dict],
    roster: dict[str, set[str]],
    alias_re: re.Pattern | None,
) -> tuple[dict, list[dict], dict]:
    """习得事件按角色归集；使用事件只记账、不算变化。"""
    acquire: dict[str, list[dict]] = {}
    unattributed: list[dict] = []
    uses: dict[str, list[dict]] = {}

    def clean(raw: str) -> str:
        s = raw.strip("的了着")
        if not (2 <= len(s) <= 8):
            return ""
        if any(s.startswith(h) for h in _SKILL_BAD_HEAD):
            return ""
        if any(ch in s for ch in "0123456789"):
            return ""
        if s in {a for al in roster.values() for a in al}:
            return ""
        return s

    for ch in chapters:
        text = ch["text"]
        for m in _SKILL_ACQUIRE_RE.finditer(text):
            skill = clean(m.group("skill"))
            if not skill:
                continue
            name, basis = _nearest_character(
                text, start=m.start(), end=m.end(), roster=roster, alias_re=alias_re)
            _, _, sentence = _sentence_at(text, m.start(), m.end())
            row = {"chapter": ch["chapter"], "skill": skill, "basis": basis,
                   "sentence": sentence[:100]}
            if name:
                acquire.setdefault(name, []).append(row)
            else:
                unattributed.append(row)
        for m in _SKILL_USE_RE.finditer(text):
            skill = clean(m.group("skill"))
            if not skill:
                continue
            name, _ = _nearest_character(
                text, start=m.start(), end=m.end(), roster=roster, alias_re=alias_re)
            if name:
                uses.setdefault(name, []).append({
                    "chapter": ch["chapter"], "skill": skill})
    return acquire, unattributed, uses


def _known_items(plot_bible: dict | None) -> list[str]:
    """项目显式声明的关键道具名。只读数据，不写死任何作品名。"""
    names: list[str] = []
    for key in ("derived_item_names", "items", "道具", "物品"):
        raw = (plot_bible or {}).get(key)
        if isinstance(raw, (list, tuple)):
            names.extend(str(x).strip() for x in raw if str(x).strip())
        elif isinstance(raw, dict):
            names.extend(str(x).strip() for x in raw.keys() if str(x).strip())
    return sorted({n for n in names if len(n) >= 2})


def _clean_item(raw: str) -> str:
    """把抽到的宾语清洗成「像物件名」的短语；不像就返回空串。

    这是词面抽取最容易出噪声的一环，规则全部是**通用汉语形态**：
    含「的/了/是/你我他/什么」的短语不是物件名；`后来人` 这类副词开头不是；
    `封条递` 这类尾部动词去掉；`一条后台通知` 去掉数量词。
    """
    s = (raw or "").strip("“”，。！？；：、,.!?;: \t\r\n")
    if not s or len(s) < 2:
        return ""
    if any(ch in _ITEM_BAD_CHARS for ch in s):
        return ""
    # 数量词前缀：一条后台通知 → 后台通知
    i = 0
    while i < len(s) and s[i] in _ITEM_NUM:
        i += 1
    if i and i < len(s) and s[i] in _ITEM_MEASURE:
        s = s[i + 1:]
    # 时间后缀：名单以后 → 名单
    for suf in ("以后", "之后", "以前", "之前"):
        if s.endswith(suf):
            s = s[:-len(suf)]
            break
    # 尾部动词/方位词：封条递 → 封条
    while s and s[-1] in _ITEM_TRAIL:
        s = s[:-1]
    if not (2 <= len(s) <= 8):
        return ""
    if any(s.startswith(h) for h in _ITEM_BAD_HEAD):
        return ""
    if s in _ITEM_NUM or s in _ITEM_MEASURE:
        return ""
    return s


def _item_events(
    chapters: list[dict],
    roster: dict[str, set[str]],
    alias_re: re.Pattern | None,
    known_items: list[str],
) -> tuple[dict, dict, list[dict]]:
    """道具变化：获得 / 失去 / 转手，外加显式道具的首次出现。

    返回 (per_character_events, per_item_state, unattributed)。
    """
    per_char: dict[str, list[dict]] = {}
    items: dict[str, dict] = {}
    item_seen: set[tuple] = set()
    unattributed: list[dict] = []

    def note_item(item: str, chapter: int, *, owner: str | None, change: str,
                  sentence: str) -> None:
        # 同一物件的同一种「获得 / 失去」只记第一次；否则每章「他掏出钥匙」
        # 都会被算成新变化。转手按次记（换手是真实变化）。
        key = (item, change, owner)
        if change in ("gain", "loss"):
            if key in item_seen:
                return
            item_seen.add(key)
        row = items.setdefault(item, {
            "first_seen": chapter, "owner": None, "changes": []})
        row["changes"].append({"chapter": chapter, "change": change,
                               "owner": owner, "sentence": sentence[:100]})
        if owner:
            row["owner"] = owner

    for ch in chapters:
        text = ch["text"]
        for m in _ITEM_GAIN_RE.finditer(text):
            item = _clean_item(m.group("item"))
            if not item:
                continue
            if _nonperson_subject(text, m.start()):
                _, _, sentence = _sentence_at(text, m.start(), m.end())
                unattributed.append({"chapter": ch["chapter"], "item": item,
                                     "change": "gain",
                                     "basis": "nonperson_subject",
                                     "sentence": sentence[:100]})
                continue
            name, basis = _nearest_character(
                text, start=m.start(), end=m.end(), roster=roster, alias_re=alias_re)
            _, _, sentence = _sentence_at(text, m.start(), m.end())
            if not name:
                unattributed.append({"chapter": ch["chapter"], "item": item,
                                     "change": "gain", "basis": basis,
                                     "sentence": sentence[:100]})
                continue
            per_char.setdefault(name, []).append({
                "chapter": ch["chapter"], "item": item, "change": "gain",
                "basis": basis, "sentence": sentence[:100]})
            note_item(item, ch["chapter"], owner=name, change="gain",
                      sentence=sentence)

        for m in _ITEM_LOSS_RE.finditer(text):
            item = _clean_item(m.group("item_a") or m.group("item_b") or "")
            if not item:
                continue
            name, basis = _nearest_character(
                text, start=m.start(), end=m.end(), roster=roster, alias_re=alias_re)
            _, _, sentence = _sentence_at(text, m.start(), m.end())
            if not name:
                unattributed.append({"chapter": ch["chapter"], "item": item,
                                     "change": "loss", "basis": basis,
                                     "sentence": sentence[:100]})
                continue
            per_char.setdefault(name, []).append({
                "chapter": ch["chapter"], "item": item, "change": "loss",
                "basis": basis, "sentence": sentence[:100]})
            note_item(item, ch["chapter"], owner=None, change="loss",
                      sentence=sentence)

        for m in _ITEM_TRANSFER_RE.finditer(text):
            item = _clean_item(m.group("item"))
            to = m.group("to")
            if not item:
                continue
            idx = _alias_index(roster)
            owner = idx.get(to)
            if not owner:
                continue
            _, _, sentence = _sentence_at(text, m.start(), m.end())
            per_char.setdefault(owner, []).append({
                "chapter": ch["chapter"], "item": item, "change": "transfer_in",
                "basis": "explicit_recipient", "sentence": sentence[:100]})
            note_item(item, ch["chapter"], owner=owner, change="transfer",
                      sentence=sentence)

        # 显式声明的关键道具：第一次在正文出现就记一笔（不归人）。
        for item in known_items:
            if item in text and item not in items:
                items[item] = {
                    "first_seen": ch["chapter"], "owner": None,
                    "changes": [{"chapter": ch["chapter"], "change": "first_seen",
                                 "owner": None, "sentence": ""}],
                }

    return per_char, items, unattributed


def _change_chapters(events: list[dict], kind: str) -> list[dict]:
    """把原始事件收敛成「变化点」。

    去重口径：技能按 `(技能名)` 只记第一次习得；道具按 `(物件, 变化类型)`
    只记第一次（获得 / 失去 / 转手各记一次）。同一章多次只留一条并计数。
    这样「每章都写他掏出同一把钥匙」不会变成十个假变化点。
    已知局限：同一物件的「得到 → 失去 → 再得到」只会记到第二次得到，
    重复轮回会被少算；本台账只判断「有没有推进」，不做归属审计。
    """
    seen: set[tuple] = set()
    by_chapter: dict[int, dict] = {}
    for ev in events:
        ch = ev["chapter"]
        val = ev.get("value") or ev.get("skill") or ev.get("item")
        key = (val, ev.get("change")) if kind == "item" else (val,)
        if val and key in seen:
            continue
        if val:
            seen.add(key)
        if ch not in by_chapter:
            by_chapter[ch] = {"chapter": ch, "values": [], "count": 0,
                              "evidence": ev.get("sentence") or ev.get("value") or ""}
        by_chapter[ch]["count"] += 1
        if val and val not in by_chapter[ch]["values"]:
            by_chapter[ch]["values"].append(val)
    return [by_chapter[k] for k in sorted(by_chapter)]


def _realm_change_points(events: list[dict]) -> list[dict]:
    """境界：同体系章末值与上一已知值不同才算变化；跨体系各记各的。"""
    last: dict[int, str] = {}
    out: list[dict] = []
    by_chapter: dict[int, list[dict]] = {}
    for ev in events:
        by_chapter.setdefault(ev["chapter"], []).append(ev)

    for ch in sorted(by_chapter):
        chapter_events = by_chapter[ch]
        changed = []
        touched_systems = {}
        for ev in chapter_events:
            touched_systems[ev["system"]] = ev  # 章内最后一次
        for si, ev in touched_systems.items():
            prev = last.get(si)
            if prev is None:
                # 第一次看到该体系的值只作为基线，不算变化。
                last[si] = ev["value"]
                continue
            if prev != ev["value"]:
                changed.append(ev)
                last[si] = ev["value"]
        if changed:
            out.append({
                "chapter": ch,
                "values": [f"体系{ev['system']}:{ev['value']}" for ev in changed],
                "count": len(changed),
                "evidence": changed[-1].get("sentence", ""),
                "explicit": any(ev["explicit"] for ev in changed),
            })
    return out


def _zero_run(chapters: list[int], change_chapters: list[int],
              first_chapter: int | None) -> dict:
    """最长连续零变化区间（按窗口章序，含首尾）。"""
    if not chapters or first_chapter is None:
        return {"chapters": 0, "start": None, "end": None}
    change_set = set(change_chapters)
    run, best = 0, {"chapters": 0, "start": None, "end": None}
    start = None
    for ch in chapters:
        if ch < first_chapter:
            continue
        if ch in change_set:
            run, start = 0, None
            continue
        run += 1
        if run == 1:
            start = ch
        if run > best["chapters"]:
            best = {"chapters": run, "start": start, "end": ch}
    return best


def _build_attributes(
    chapters: list[dict],
    roster: dict[str, set[str]],
    plot_bible: dict | None,
    cfg: dict,
    alias_re: re.Pattern | None,
    cast_rows: list[dict],
) -> dict:
    systems = _realm_systems(plot_bible)
    realm_events, realm_unattributed = _realm_mentions(
        chapters, roster, alias_re, systems)
    skill_events, skill_unattributed, skill_uses = _skill_events(
        chapters, roster, alias_re)
    item_events, item_states, item_unattributed = _item_events(
        chapters, roster, alias_re, _known_items(plot_bible))

    chapter_nos = [ch["chapter"] for ch in chapters]
    appear = {r["chapter"]: set(r["cast"]) for r in cast_rows}
    window_end = chapter_nos[-1] if chapter_nos else 0

    rows: list[dict] = []
    kinds = (
        ("realm", realm_events, _realm_change_points),
        ("skill", skill_events, lambda evs: _change_chapters(evs, "skill")),
        ("item", item_events, lambda evs: _change_chapters(evs, "item")),
    )
    for kind, per_char, converter in kinds:
        for name in roster:
            events = per_char.get(name, [])
            changes = converter(events)
            change_chs = [c["chapter"] for c in changes]
            first_seen = None
            first_basis = None
            if events:
                first_seen = min(e["chapter"] for e in events)
                first_basis = "first_attribute_evidence"
            else:
                # 没有任何该类证据时，以首次出场章作为观察起点：这样「零变化
                # 区间」回答的是「他出场了多久、这件事没动」，而不是 0。
                seen = [ch for ch, cast in appear.items() if name in cast]
                first_seen = seen[0] if seen else None
                first_basis = "cast_debut" if first_seen else None
            present = [ch for ch in chapter_nos if name in appear.get(ch, set())]
            run = _zero_run(chapter_nos, change_chs,
                            first_seen if first_seen is not None else None)
            last_change = change_chs[-1] if change_chs else None
            since = (window_end - last_change) if last_change else (
                window_end - first_seen if first_seen else None)
            has_evidence = bool(events)
            if not present:
                status = "not_in_window"
            elif last_change:
                status = ("stale" if since is not None
                          and since >= cfg["attr_stale_chapters"] else "changed")
            elif has_evidence:
                # 有词面证据但值一直没变（例如每章都写「炼气三层」）。
                status = "no_evidence_of_change"
            else:
                # 本章窗口内完全没有该类属性的词面证据。
                status = "no_attribute_evidence"
            rows.append({
                "character": name,
                "kind": kind,
                "first_seen": first_seen,
                "first_seen_basis": first_basis,
                "appearances": len(present),
                "changes": changes,
                "change_count": len(changes),
                "last_change": last_change,
                "chapters_since_last_change": since,
                "longest_zero_run": run["chapters"],
                "longest_zero_span": [run["start"], run["end"]]
                                      if run["start"] is not None else None,
                "status": status,
            })

    return {
        "rows": rows,
        "realm_systems": [list(g) for g in systems],
        "item_states": item_states,
        "items_known": _known_items(plot_bible),
        "item_first_seen": {n: v["first_seen"] for n, v in item_states.items()},
        "skill_uses": skill_uses,
        "unattributed": {
            "realm": realm_unattributed,
            "skill": skill_unattributed,
            "item": item_unattributed,
        },
        "thresholds": {"stale_chapters": cfg["attr_stale_chapters"]},
    }


# ─── 3. 章间衔接 ──────────────────────────────────────────────────────────


#: 章节标题行：`第24章《药盒上有名字》` / `第二十七章 名单`。
#: 它是排版产物，不是叙事首段；不剔掉会让每一对章都误判成「场景断裂」。
_CHAPTER_TITLE = re.compile(r"^\s*第[零一二三四五六七八九十百千万\d]+章")


def _head_paragraphs(text: str, count: int) -> list[str]:
    """正文首段（跳过章节标题行）。"""
    paras = split_paragraphs(text or "")
    while paras and _CHAPTER_TITLE.match(paras[0]):
        paras.pop(0)
    return paras[:max(1, count)]


def _build_linkage(
    chapters: list[dict],
    roster: dict[str, set[str]],
    cfg: dict,
    alias_re: re.Pattern | None,
) -> dict:
    rows: list[dict] = []

    for prev, cur in zip(chapters, chapters[1:]):
        head_paras = _head_paragraphs(cur["text"], cfg["head_paragraphs"])
        tail_paras = split_paragraphs(prev["text"])[-max(1, cfg["tail_paragraphs"]):]
        head = "\n\n".join(head_paras)
        tail = "\n\n".join(tail_paras)

        head_cast, _ = _cast_of_chapter(head, roster, alias_re)
        tail_cast, _ = _cast_of_chapter(tail, roster, alias_re)
        shared_entities = sorted(set(head_cast) & set(tail_cast))

        head_locs, tail_locs = _locations(head), _locations(tail)
        shared_locs = sorted(head_locs & tail_locs)

        head_cues = _time_cues(head)
        tail_cues = _time_cues(tail)
        jump_days = max([c["days"] for c in head_cues if c["days"] > 0], default=0)
        bridge_hit = [w for w in _TIME_BRIDGE if w in head]
        # 共享锚点：角色 / 地点。都没有只说明「换了镜头」，不等于写错；
        # 单独作为观察项报告，不直接判「接不上」。
        shared_anchor = bool(shared_entities or shared_locs)
        explained = bool(bridge_hit) or shared_anchor
        time_jump = jump_days >= cfg["time_jump_min_days"]
        unexplained = bool(time_jump and not explained)

        prev_questions = _open_questions(
            prev["text"], tail_paragraphs=cfg["question_tail_paragraphs"],
            top=cfg["question_top"])
        question_rows = [
            _question_recall(q, cur["text"], cfg["question_overlap"])
            for q in prev_questions
        ]
        rementioned = [q for q in question_rows if q["mentioned"]]
        dropped = [q for q in question_rows if not q["mentioned"]]

        rows.append({
            "prev_chapter": prev["chapter"],
            "chapter": cur["chapter"],
            "head": head[:200],
            "shared_entities": shared_entities,
            "shared_locations": shared_locs,
            "shared_time_cues": sorted({c["text"] for c in head_cues}
                                      & {c["text"] for c in tail_cues}),
            "head_time_cues": head_cues,
            "tail_time_cues": tail_cues,
            "time_jump_days": jump_days,
            "time_jump": time_jump,
            "bridge_words": bridge_hit,
            "shared_anchor": shared_anchor,
            "time_explained": explained if time_jump else None,
            "time_jump_unexplained": unexplained,
            "no_shared_anchor": not shared_anchor,
            "prev_questions": prev_questions,
            "questions": question_rows,
            "questions_rementioned": rementioned,
            "questions_dropped": dropped,
        })

    # 「接不上」只认时间跳跃未交代；「无共享锚点」单列，不当作错误。
    breaks = [r for r in rows if r["time_jump_unexplained"]]
    anchorless = [r for r in rows if r["no_shared_anchor"]]
    no_time_cue = [r for r in rows if not r["head_time_cues"]]
    dropped_any = [r for r in rows if r["questions_dropped"]]
    return {
        "rows": rows,
        "breaks": [{"prev_chapter": r["prev_chapter"], "chapter": r["chapter"],
                    "time_jump_days": r["time_jump_days"],
                    "head_time_cues": [c["text"] for c in r["head_time_cues"]],
                    "shared_entities": r["shared_entities"],
                    "shared_locations": r["shared_locations"]}
                   for r in breaks],
        "anchorless": [{"prev_chapter": r["prev_chapter"], "chapter": r["chapter"]}
                       for r in anchorless],
        "no_time_cue": [{"prev_chapter": r["prev_chapter"], "chapter": r["chapter"]}
                        for r in no_time_cue],
        "dropped_questions": [{"prev_chapter": r["prev_chapter"],
                               "chapter": r["chapter"],
                               "questions": [q["question"] for q in r["questions_dropped"]]}
                              for r in dropped_any],
        "thresholds": {
            "question_overlap": cfg["question_overlap"],
            "time_jump_min_days": cfg["time_jump_min_days"],
        },
    }


# ─── structure_check 的引用 ───────────────────────────────────────────────


def _structure_row(stored: dict | None, computed: dict | None) -> dict:
    """只搬运 `structure_check` 的字段，不重算三项判据。"""
    raw = stored or computed or {}
    source = "stored" if stored else ("computed" if computed else "missing")
    beat = raw.get("beat") or {}
    premature = raw.get("premature") or {}
    gender = raw.get("gender") or {}
    return {
        "source": source,
        "status": raw.get("status"),
        "mode": raw.get("mode"),
        "beat_coverage": beat.get("coverage"),
        "beat_total": beat.get("total"),
        "beat_covered": beat.get("covered"),
        "beat_units": beat.get("units"),
        "beat_min_units": beat.get("min_units"),
        "beat_discrimination": beat.get("discrimination"),
        "beat_counted": beat.get("counted"),
        "beat_fallback_coverage": (beat.get("fallback") or {}).get("coverage"),
        "beat_missing": beat.get("missing") or [],
        "premature_chapters_hit": premature.get("chapters_hit"),
        "premature_hits": premature.get("hits") or [],
        "gender_count": gender.get("count"),
        "gender_mismatches": gender.get("mismatches") or [],
        "blocking": bool(raw.get("blocking")),
        "enforced": bool(raw.get("enforced")),
    }


def _compute_structure(
    text: str, *, outline: Any, chapter: int, cards: list[dict], cfg: dict,
) -> dict | None:
    """落库没有本章 `structure_check` 时，用同一函数、同一默认值补算。

    阈值**一律读配置**，与 `engine._structure` 调用 `check_structure` 时用同一把尺；
    写死会在闸门阈值调整后让台账与闸门各判各的（第 35 章复盘发现 0.34 / 3 / 3
    曾在此硬编码）。这里只读值，不改判据语义。
    """
    if not isinstance(outline, (list, tuple)) or not (1 <= chapter <= len(outline)):
        return None
    try:
        from app.config import settings as _settings
    except Exception:  # noqa: BLE001 —— 无配置时退回 check_structure 默认值
        _settings = None
    try:
        from app.generation.structure_check import check_structure
        return check_structure(
            text,
            outline=outline,
            chapter=chapter,
            character_cards=cards,
            beat_min_coverage=float(getattr(
                _settings, "structure_beat_min_coverage", 0.34)),
            beat_min_units=int(getattr(
                _settings, "structure_beat_min_units", 3) or 3),
            premature_lookahead=int(getattr(
                _settings, "structure_premature_lookahead", 3)),
            premature_max_hits=int(getattr(
                _settings, "structure_premature_max_hits", 3)),
            gender_max_mismatches=int(getattr(
                _settings, "structure_gender_max_mismatches", 0)),
            gender_window=int(getattr(
                _settings, "structure_gender_window", 12)),
            mode="report",
        )
    except Exception:  # noqa: BLE001 —— 补算失败不能拖垮台账
        return None


# ─── 入口 ────────────────────────────────────────────────────────────────


def build_progress_ledger(
    chapters: list[dict],
    *,
    plot_bible: dict | None = None,
    outline: Any = None,
    structure_checks: dict[int, dict] | None = None,
    config: dict | None = None,
    settings: Any = None,
) -> dict:
    """离线回放：逐章台账。

    Args:
        chapters: `[{"chapter": int, "text": str}]`，按章号升序即可（内部会排序）。
            同名章号只保留最后一条（重写后的版本）。
        plot_bible: 项目设定；角色名单、别名、境界阶梯、关键道具都从这里来。
        outline: 每章一条的大纲（1-based），仅用于补算 `structure_check`。
        structure_checks: `{章号: quality_json["structure_check"]}`，优先使用。
        config: 直接覆盖阈值（测试用）；缺省走 `Settings` + `DEFAULTS`。
        settings: 可注入的 Settings（便于测试）。

    Returns:
        可直接 JSON 序列化的 dict。不含时间戳，保证同输入同输出。
    """
    cfg = dict(DEFAULTS)
    cfg.update(config or {})
    if not config:
        cfg = resolve_config(settings)

    # 同章多版本：保留最后一条（按输入顺序），与「重写后以最新落库为准」一致。
    by_chapter: dict[int, str] = {}
    for row in chapters or []:
        if not isinstance(row, dict):
            continue
        try:
            ch = int(row.get("chapter"))
        except (TypeError, ValueError):
            continue
        text = row.get("text") or ""
        if ch > 0 and text.strip():
            by_chapter[ch] = text
    ordered = [{"chapter": ch, "text": by_chapter[ch]} for ch in sorted(by_chapter)]
    if not ordered:
        return {
            "version": LEDGER_VERSION,
            "window": [],
            "status": "skipped",
            "reason": "没有可用章节正文",
            "config": cfg,
        }

    bible = plot_bible if isinstance(plot_bible, dict) else {}
    roster = build_roster(bible)
    cards = [c for c in (bible.get("characters") or []) if isinstance(c, dict)]
    alias_re = _name_re(roster)

    cast = _build_cast(ordered, roster, cards, cfg, alias_re)
    attributes = _build_attributes(
        ordered, roster, bible, cfg, alias_re, cast["rows"])
    linkage = _build_linkage(ordered, roster, cfg, alias_re)

    stored = structure_checks or {}
    structure_rows: list[dict] = []
    for ch in ordered:
        computed = None
        if ch["chapter"] not in stored:
            computed = _compute_structure(
                ch["text"], outline=outline, chapter=ch["chapter"],
                cards=cards, cfg=cfg)
        structure_rows.append({
            "chapter": ch["chapter"],
            **_structure_row(stored.get(ch["chapter"]), computed),
        })

    motionless = [r for r in attributes["rows"]
                  if r["status"] in ("stale", "no_evidence_of_change",
                                     "no_attribute_evidence")]
    structure_issues = [
        r for r in structure_rows
        if r["blocking"] or (r["beat_missing"] and r["source"] != "missing")
        or r["gender_count"] or (r["premature_chapters_hit"] or 0) > 0
    ]

    flags: list[dict] = []
    if cast["stagnant"]:
        flags.append({
            "code": "cast_frozen",
            "message": f"连续 {cast['longest_overlap_run']['chapters']} 章出场集合高度重合"
                       f"（{cast['longest_overlap_run']['start']}–{cast['longest_overlap_run']['end']}）",
        })
    if cast["minor_never_appeared"]:
        flags.append({
            "code": "minor_never_appeared",
            "message": "角色卡里始终没在正文出现的角色："
                       + "、".join(cast["minor_never_appeared"]),
        })
    if motionless:
        flags.append({
            "code": "attr_stagnant",
            "message": f"{len(motionless)} 条「角色×属性」在本窗口内没有推进"
                       f"（其中 {sum(1 for r in motionless if r['status'] == 'stale')} 条"
                       f"超过 {cfg['attr_stale_chapters']} 章未变）",
        })
    if linkage["breaks"]:
        flags.append({
            "code": "chapter_breaks",
            "message": f"{len(linkage['breaks'])} 处时间跳跃未交代",
        })
    if linkage["dropped_questions"]:
        flags.append({
            "code": "question_dropped",
            "message": f"{len(linkage['dropped_questions'])} 章没有再次提及上一章留下的问题",
        })
    if structure_issues:
        flags.append({
            "code": "structure_issue",
            "message": f"{len(structure_issues)} 章 structure_check 有告警（引用，不重算）",
        })

    return {
        "version": LEDGER_VERSION,
        "status": "ok",
        "window": [ordered[0]["chapter"], ordered[-1]["chapter"]],
        "chapter_count": len(ordered),
        "config": cfg,
        "cast": cast,
        "attributes": attributes,
        "linkage": linkage,
        "structure": {
            "rows": structure_rows,
            "issues": [{"chapter": r["chapter"], "source": r["source"],
                        "beat_missing": r["beat_missing"],
                        "premature_chapters_hit": r["premature_chapters_hit"],
                        "gender_count": r["gender_count"]}
                       for r in structure_issues],
            "sources": {str(r["chapter"]): r["source"] for r in structure_rows},
        },
        "flags": flags,
        "pending_verification": [
            "cast_overlap_high", "cast_core_max", "cast_stagnant_chapters",
            "attr_stale_chapters", "head_paragraphs", "tail_paragraphs",
            "question_tail_paragraphs", "question_overlap", "time_jump_min_days",
            "window_chapters", "structure_beat_min_units",
        ],
    }


# ─── 落库视图 / 提示层要求 ────────────────────────────────────────────────


def chapter_view(ledger: dict, chapter: int) -> dict:
    """把整窗台账裁成「本章一行」，写进 `quality_json.progress_ledger`。

    只搬运 `build_progress_ledger` 已经算好的字段，不重算、不判罚：
    `mode` 恒为 `report`，不参与 `passed`、不进 `violations`。整窗全量台账
    体积大（每章 × 每角色 × 三类属性），落库只需要本章的可核对项。
    """
    view: dict = {
        "version": LEDGER_VERSION,
        "mode": "report",
        "chapter": chapter,
        "status": ledger.get("status") or "unknown",
    }
    if view["status"] != "ok":
        view["reason"] = ledger.get("reason")
        return view

    view["window"] = ledger.get("window")
    view["chapter_count"] = ledger.get("chapter_count")

    cast_rows = (ledger.get("cast") or {}).get("rows") or []
    cast_row = next((r for r in cast_rows if r["chapter"] == chapter), None)
    if cast_row is not None:
        view["cast"] = {
            "protagonist": (ledger.get("cast") or {}).get("protagonist"),
            "cast": cast_row["cast"],
            "cast_size": cast_row["cast_size"],
            "new_debuts": cast_row["new_debuts"],
            "overlap_prev": cast_row["overlap_prev"],
            "protagonist_present": cast_row["protagonist_present"],
            "minor_present": cast_row["minor_present"],
        }
    else:
        view["cast"] = None

    attr_rows = (ledger.get("attributes") or {}).get("rows") or []
    changes: list[dict] = []
    for row in attr_rows:
        at_chapter = [c for c in (row.get("changes") or [])
                      if c.get("chapter") == chapter]
        if at_chapter:
            changes.append({
                "character": row["character"],
                "kind": row["kind"],
                "change_count": len(at_chapter),
                "changes": at_chapter,
            })
    current_cast = set((cast_row or {}).get("cast") or [])
    motionless = [
        {
            "character": row["character"],
            "kind": row["kind"],
            "status": row["status"],
            "chapters_since_last_change": row["chapters_since_last_change"],
            "longest_zero_run": row["longest_zero_run"],
        }
        for row in attr_rows
        if row["character"] in current_cast
        and row["status"] in ("stale", "no_evidence_of_change",
                              "no_attribute_evidence")
    ]
    view["attributes"] = {
        "changes": changes,
        "motionless": motionless,
        "counts": {
            "changes": len(changes),
            "realm": sum(1 for c in changes if c["kind"] == "realm"),
            "skill": sum(1 for c in changes if c["kind"] == "skill"),
            "item": sum(1 for c in changes if c["kind"] == "item"),
            "motionless": len(motionless),
        },
    }

    link_rows = (ledger.get("linkage") or {}).get("rows") or []
    link_row = next((r for r in link_rows if r["chapter"] == chapter), None)
    if link_row is not None:
        view["linkage"] = {
            "prev_chapter": link_row["prev_chapter"],
            "shared_entities": link_row["shared_entities"],
            "shared_locations": link_row["shared_locations"],
            "shared_time_cues": link_row["shared_time_cues"],
            "head_time_cues": [c.get("text") for c in link_row["head_time_cues"]],
            "time_jump_days": link_row["time_jump_days"],
            "time_jump": link_row["time_jump"],
            "time_explained": link_row["time_explained"],
            "time_jump_unexplained": link_row["time_jump_unexplained"],
            "no_shared_anchor": link_row["no_shared_anchor"],
            "prev_questions": link_row["prev_questions"],
            "questions": link_row["questions"],
        }
    else:
        view["linkage"] = None

    structure_row = next(
        (r for r in (ledger.get("structure") or {}).get("rows") or []
         if r["chapter"] == chapter), None)
    view["structure"] = None if structure_row is None else {
        "source": structure_row["source"],
        "beat_coverage": structure_row["beat_coverage"],
        "beat_total": structure_row["beat_total"],
        "beat_covered": structure_row["beat_covered"],
        "beat_units": structure_row["beat_units"],
        "beat_min_units": structure_row["beat_min_units"],
        "beat_discrimination": structure_row["beat_discrimination"],
        "beat_counted": structure_row["beat_counted"],
        "beat_fallback_coverage": structure_row["beat_fallback_coverage"],
        "beat_missing": structure_row["beat_missing"],
        "premature_chapters_hit": structure_row["premature_chapters_hit"],
        "gender_count": structure_row["gender_count"],
    }
    view["flags"] = [f.get("code") for f in (ledger.get("flags") or [])]
    view["thresholds"] = {
        "cast_overlap_high": (ledger.get("config") or {}).get("cast_overlap_high"),
        "attr_stale_chapters": (ledger.get("config") or {}).get("attr_stale_chapters"),
        "time_jump_min_days": (ledger.get("config") or {}).get("time_jump_min_days"),
    }
    return view


#: 技能「习得」动词（与 `_SKILL_ACQUIRE_RE` 同源，写进提示时保持一致）。
_SKILL_HINT_VERBS = "学会／练成／掌握／习得／悟出／领悟／精通／入门"
#: 道具「归属变化」动词（与 `_ITEM_*_RE` 同源）。
_ITEM_HINT_VERBS = "拿到／得到／收下／捡到／买下／接过；把X交给/递给/还给Y；把X丢了/卖了"


def advance_requirements(
    *,
    chapter: int | None = None,
    plot_bible: dict | None = None,
    prior_chapters: list[dict] | None = None,
    config: dict | None = None,
    settings: Any = None,
) -> str:
    """生成「本章推进要求」提示块：每条都对应台账里的一个可核字段。

    只写**能被台账验证**的要求，做不到的宁可不写：

    - 属性：`progress_ledger.attributes[*].changes` 本章非空；
    - 阵容：`progress_ledger.cast.overlap_prev` 低于阈值，或 `new_debuts` 非空；
    - 衔接：`progress_ledger.linkage.head_time_cues` 非空。

    角色名单、阈值、上一章出场集合全部来自入参与配置，不写死任何名字或章号。
    没有前序章节时（无法比对集合）不写阵容那一条。
    """
    try:
        ch = int(chapter) if chapter is not None else 0
    except (TypeError, ValueError):
        ch = 0
    if ch <= 0:
        return ""

    cfg = dict(DEFAULTS)
    cfg.update(config or {})
    if not config:
        cfg = resolve_config(settings)

    bible = plot_bible if isinstance(plot_bible, dict) else {}
    roster = build_roster(bible)
    alias_re = _name_re(roster)

    # 前序章节按章号去重（同章取最后一条），只保留 < 当前章 的。
    by_chapter: dict[int, str] = {}
    for row in prior_chapters or []:
        if not isinstance(row, dict):
            continue
        try:
            row_ch = int(row.get("chapter"))
        except (TypeError, ValueError):
            continue
        text = row.get("text") or ""
        if 0 < row_ch < ch and str(text).strip():
            by_chapter[row_ch] = text
    ordered = sorted(by_chapter)

    lines = [
        "【本章推进要求（逐章台账会按下列字段核对；只记报告，不拦稿）】",
    ]
    lines.append(
        "1. **属性推进**（核字段 `progress_ledger.attributes[*].changes`）："
        "本章至少写出 1 处台账能识别的属性变化，境界／技能／道具三类任选其一即可——"
        "境界用设定表里的阶梯原词写出变化（如「突破到X」），跨体系不要混用；"
        f"技能用「{_SKILL_HINT_VERBS}」+ 技能名（≥2 字），只写「施展／使出」不算变化；"
        f"道具用「{_ITEM_HINT_VERBS}」，「掏出／摸出」不算。"
    )

    if roster and ordered:
        casts = [(c, _cast_of_chapter(by_chapter[c], roster, alias_re)[0])
                 for c in ordered]
        prev_ch, prev_cast = casts[-1]
        pairs = 0
        for i in range(len(casts) - 1, 0, -1):
            cur_cast, before_cast = casts[i][1], casts[i - 1][1]
            high = (_jaccard(before_cast, cur_cast) >= cfg["cast_overlap_high"]
                    and max(len(before_cast), len(cur_cast))
                    <= cfg["cast_core_max"])
            if not high:
                break
            pairs += 1
        run_chapters = pairs + 1 if pairs else 1
        names = "、".join(prev_cast) if prev_cast else "（未匹配到角色卡名单内的角色）"
        text = (
            "2. **角色阵容**（核字段 `progress_ledger.cast.overlap_prev` / "
            f"`new_debuts`）：第 {prev_ch} 章出场的是 {names}。"
            f"本章与上一章的出场集合重合度必须 < {cfg['cast_overlap_high']:g}"
            "（Jaccard），也就是至少换掉一名在场角色：有人不在场，或有角色新到场。"
        )
        if run_chapters >= 2:
            text += (f"目前已连续 {run_chapters} 章重合度不低于该阈值，"
                     f"配置上限是 {cfg['cast_stagnant_chapters']} 章。")
            if run_chapters > cfg["cast_stagnant_chapters"]:
                text += "已经超过上限，本章必须换人。"
        text += ("只能在角色卡名单与本章大纲点名的角色里换，**不要凭空造人名**；"
                 "角色卡之外的名字不会被台账计进「新登场」。")
        lines.append(text)

    lines.append(
        "3. **章间衔接**（核字段 `progress_ledger.linkage.head_time_cues`）："
        "本章第一个自然段必须写出时间锚点，让读者知道距上一章过了多久——"
        "用「第二天／当晚／三天后／一周后／半小时后」这类通用时间词；"
        "只写「后来／不久」不算锚点。"
        f"若跳跃达到 {cfg['time_jump_min_days']} 天，必须用过渡句交代"
        "（如「这三天里……」），否则台账会记为「时间跳跃未交代」。"
    )
    return "\n".join(lines)
