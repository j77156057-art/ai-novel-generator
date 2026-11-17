"""结构约束检测（v15）：把「提示里写了但没人执行」变成「能算出来、能留痕」。

## 为什么单独一个模块

三项检测都只读**已有档案**（`project.outline` / 角色卡），不新增人工清单，
也不依赖 LLM 二次调用：

1. **本章情节点有没有写进去**——把本章大纲条目按三级切分成并列事件
   （`；` → `。` → 「逗号 + 推进连词」），用汉字二元组覆盖率判断每个事件
   在正文里是否出现。覆盖率口径见 `_beat_coverage`：不是「关键词命中」而是
   「事件二元组有多少出现在正文」，对同义改写有一定容忍度。
   切完不足 `beat_min_units` 条时（典型是卷纲每章只有一句话），覆盖率只剩
   1/1、1/2 这种量级，**不具区分度**：此时标 `discrimination="insufficient"`，
   `coverage` 不参与任何判据，改用兜底分母（本章大纲独有词三元组，与越章
   检测同一词源、同一阈值），兜底值本轮也只输出、不拦截。
2. **有没有提前用掉后续章的材料**——取后续 `lookahead` 章的条目，扣掉本章与
   更早章节已经出现过的二元组，剩下的「本章上下文里没有、后续章独有」的二元组
   如果在正文里命中，就是越章材料。这是「词面命中」的可计算版本：
   判据不是某个词表，而是**本章与后续章的差集**，换书自动跟着换。
3. **人物属性与设定卡是否一致**——对每张写了性别的角色卡，在名字后一个短窗口
   内找「他/她」，与设定卡性别比对。窗口内的代词是模型指代该角色最直接的信号。

## 拦截策略

检测结果写进 `quality_json.structure_check`。是否阻断落库由
`settings.structure_guard_mode` 决定：

- `report`（默认）：告警落库，`structure_check.blocking=True` 只标记、不阻断；
- `block`：`_commit_segment` 在写库前抛 `StructureBlocked`，本章不落库。

不做「自动重写」：结构问题（漏拍/越章/性别）不是局部替换能稳定修好的，
重写轮预算已经用在文风与篇幅上；硬拦 + 人工/下一章修正比假装修好更可靠。
"""
from __future__ import annotations

import re
from typing import Any

#: 拆「并列事件」的第一、二级分隔符：先分号、再句号/换行。
#: 只切标点，不认剧情词——换书不受影响。
_SEMICOLON = re.compile(r"[；;]+")
_SENTENCE = re.compile(r"[。\n]+")
#: 第三级：「逗号 + 推进连词」。只在前面两级切出的条数仍不足 `min_units` 时启用，
#: 把「他走进屋，随后把箱子放下」这类一逗到底的长句再拆开。
#: 连词是**通用叙事连接词**（随后/接着/然后…），不含任何作品专有说法。
_PROGRESSION_WORDS = (
    "随后", "随即", "接着", "然后", "于是", "之后", "而后", "紧接着",
    "接下来", "跟着", "最后", "终于", "结果",
)
_COMMA_PROGRESSION = re.compile(
    r"[，,]\s*(?=(?:" + "|".join(_PROGRESSION_WORDS) + r"))"
)
_SPLIT_LEVELS = (_SEMICOLON, _SENTENCE, _COMMA_PROGRESSION)
#: 单条事件的最少汉字数。少于这个数多是「他走了」这类残句，切出来只会稀释分母。
#: 这是**切分规格**（本轮用户指定），不是需要校准的观察阈值。
_BEAT_MIN_HANZI = 6
#: 大纲条目前缀「第N章《标题》：」——只去前缀，不要求标题格式统一。
_CHAPTER_PREFIX = re.compile(r"^\s*第[^：:]{0,24}[：:]\s*")
#: 只取汉字做二元组，标点/空格不参与。
_HANZI_RUN = re.compile(r"[\u4e00-\u9fff]+")
#: 章标题（书名号内）。标题词最独特，作为越章检测的补充候选。
_TITLE = re.compile(r"《([^》]*)》")
#: 角色名后的指代窗口在这里截断：逗号/句末/引号/冒号/括号之前都算「另一个话题」，
#: 再往后出现的他/她不再指这个人（否则「A 看着窗外，她……」会被误判成 A 的性别错）。
_CLAUSE_END = re.compile(r"[，,。！!？?；;：:、\n“”「」『』（）()\s]")
#: 高频功能字：由两个功能字组成的二元组信息量太低，直接剔除。
_STOP_CHARS = set(
    "的了是在有和就都而及与着或一个上中也你他她我你们它们这那不没为要会能可到说走看把被从对里"
)


class StructureBlocked(RuntimeError):
    """`structure_guard_mode="block"` 且检测到阻断级结构问题时抛出。"""


def _bigrams(text: str) -> set[str]:
    out: set[str] = set()
    for run in _HANZI_RUN.findall(text or ""):
        for a, b in zip(run, run[1:]):
            if a in _STOP_CHARS and b in _STOP_CHARS:
                continue
            out.add(a + b)
    return out


def _trigrams(text: str) -> set[str]:
    out: set[str] = set()
    for run in _HANZI_RUN.findall(text or ""):
        for i in range(len(run) - 2):
            tri = run[i:i + 3]
            if all(c in _STOP_CHARS for c in tri):
                continue
            out.add(tri)
    return out


def _future_terms(entry: str) -> set[str]:
    """后续章「独有」候选词：正文三元组 + 标题二元组。

    为什么用三元组：二元的「人是 / 认出 / 写的」在任何一章都会命中，做越章检测
    只剩噪声；三元组在单章尺度上已相当独特。标题（书名号内）本身短、
    区分度高，故标题取二元组补回「断电 / 背锅」这类两字事件。
    """
    body = _chapter_body(entry)
    terms: set[str] = set()
    for run in _HANZI_RUN.findall(body):
        for i in range(len(run) - 2):
            tri = run[i:i + 3]
            # 至少两个实字，否则「写的是 / 一次」这类虚词组合会稳定误报。
            if sum(1 for c in tri if c not in _STOP_CHARS) < 2:
                continue
            terms.add(tri)
    m = _TITLE.search(entry)
    if m:
        for run in _HANZI_RUN.findall(m.group(1)):
            for a, b in zip(run, run[1:]):
                # 标题二元组同样要求「两个都是实字」，「一次」这类不算事件词。
                if a in _STOP_CHARS or b in _STOP_CHARS:
                    continue
                terms.add(a + b)
    return terms


def _chapter_body(entry: str) -> str:
    """去掉「第N章《标题》：」前缀，只留事件正文。"""
    return _CHAPTER_PREFIX.sub("", str(entry or ""), count=1).strip()


def _hanzi_count(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def _clean_beats(parts) -> list[str]:
    """去首尾标点、丢弃不足 6 汉字的残句、按原文顺序去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        clause = str(part).strip(" ，,、；;。\n")
        if _hanzi_count(clause) < _BEAT_MIN_HANZI or clause in seen:
            continue
        seen.add(clause)
        out.append(clause)
    return out


def _beats(entry: str, min_units: int = 3) -> tuple[list[str], int]:
    """本章大纲条目 → (并列事件列表, 实际用到的切分层级)。

    逐级切分，**够数就停**：先按 `；`，仍不足 `min_units` 再按 `。`/换行，
    仍不足再按「逗号 + 推进连词」。先停的好处是尽量保留粗粒度事件，
    避免把一条完整情节拆成若干碎片、把分母做虚。

    Returns:
        `(beats, level)`：`level` 为 0 表示没切成（整条算一条事件），
        1/2/3 对应实际生效的切分层级。
    """
    pieces = _clean_beats([_chapter_body(entry)])
    if not pieces:
        return [], 0
    level = 0
    for depth, pattern in enumerate(_SPLIT_LEVELS, start=1):
        if len(pieces) >= min_units:
            break
        parts: list[str] = []
        for piece in pieces:
            parts.extend(pattern.split(piece))
        parts = _clean_beats(parts)
        # 只接受「确实切出了新结果」的切分：整段没变化就保持上一级；
        # 全被 6 汉字门槛滤空也保持上一级，避免把这一条丢成空。
        if parts and parts != pieces:
            pieces = parts
            level = depth
    return pieces, level


def _beat_fallback(entries: list[str], idx: int, text: str,
                   min_coverage: float) -> dict:
    """方案 B 兜底分母：本章大纲**独有**词三元组。

    切出的条数不足 `min_units` 时，覆盖率没有区分度，改用「本章大纲独有词
    有多少出现在正文」作分母。词源与越章检测同源（`_future_terms`：正文三元组
    + 标题二元组），阈值也相同；「独有」= 扣掉其余各章的同类词项。

    自本次起升为**正式判据**：`counted=True`，不达标由调用方写进
    `blocking_reasons`（`beat_fallback_low`）；仍受 `mode` 控制，`report` 只判不拦。
    词项为空时无法判，保持 `skipped` 且不产生阻断理由。
    """
    own = _future_terms(entries[idx])
    other: set[str] = set()
    for j, entry in enumerate(entries):
        if j != idx:
            other |= _future_terms(entry)
    terms = own - other
    if not terms:
        return {
            "status": "skipped",
            "reason": "本章大纲没有独有词项",
            "source": "outline_unique_trigrams",
            "min_coverage": min_coverage,
            "counted": True,
            "basis": "fallback_term_coverage",
        }
    hit_terms = terms & (_bigrams(text) | _trigrams(text))
    coverage = round(len(hit_terms) / len(terms), 3)
    return {
        "status": "checked",
        "source": "outline_unique_trigrams",
        "total": len(terms),
        "hit": len(hit_terms),
        "coverage": coverage,
        "min_coverage": min_coverage,
        "passed": coverage >= min_coverage,
        "counted": True,
        "basis": "fallback_term_coverage",
        "terms": sorted(hit_terms)[:8],
    }


def _beat_coverage(beats: list[str], text_bigrams: set[str]) -> list[dict]:
    rows: list[dict] = []
    for beat in beats:
        grams = _bigrams(beat)
        if not grams:
            continue
        hit = len(grams & text_bigrams)
        rows.append({
            "beat": beat,
            "grams": len(grams),
            "hit": hit,
            "coverage": round(hit / len(grams), 3),
        })
    return rows


def _gender_pronoun(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if "女" in raw:
        return "她"
    if "男" in raw:
        return "他"
    return None


def _gender_words() -> dict[str, str]:
    """性别词 → 该词暗示的指代。只列**明确断言性别**的词，不列代词本身。"""
    return {
        "男人": "他", "男的": "他", "男性": "他", "男孩": "他", "男生": "他",
        "男子": "他", "小伙": "他", "汉子": "他", "先生": "他",
        "女人": "她", "女的": "她", "女性": "她", "女孩": "她", "女生": "她",
        "女子": "她", "姑娘": "她", "妇人": "她", "小姐": "她",
    }


def _gender_mismatches(text: str, cards: list[dict], window: int) -> list[dict]:
    """名字同一小句内出现**明确性别断言**且与设定卡相反 → 记一条不一致。

    为什么不看「他/她」代词：第 29 章实测「陈小满看向她」「顾青梧看着他」都是
    正确的宾语指代（指对面那个人），按代词判会稳定误报。只有当正文在名字同一
    小句里写出「男/女/男人/姑娘…」这种**断言性**词，才可能是设定冲突。
    """
    out: list[dict] = []
    words = _gender_words()
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or "").strip()
        expected = _gender_pronoun(card.get("gender") or card.get("性别"))
        if not name or not expected:
            continue
        for m in re.finditer(re.escape(name), text):
            head = text[max(0, m.start() - window): m.start()]
            tail = text[m.end(): m.end() + window]
            # 断言只在本小句内成立：遇到逗号/句末/引号即止。
            cut = _CLAUSE_END.search(tail)
            if cut:
                tail = tail[:cut.start()]
            cut = _CLAUSE_END.search(head[::-1])
            if cut:
                head = head[len(head) - cut.start():]
            clause = head + tail
            found = [w for w in words if w in clause]
            wrong = [w for w in found if words[w] != expected]
            if not wrong:
                continue
            out.append({
                "name": name,
                "expected": expected,
                "asserted": sorted(set(wrong)),
                "snippet": (text[max(0, m.start() - 6): m.end() + window]).replace("\n", " "),
            })
    return out


def check_structure(
    text: str,
    *,
    outline: Any,
    chapter: int | None,
    character_cards: list[dict] | None = None,
    beat_min_coverage: float = 0.34,
    beat_min_units: int = 3,
    premature_lookahead: int = 3,
    premature_max_hits: int = 3,
    gender_max_mismatches: int = 0,
    gender_window: int = 12,
    mode: str = "report",
) -> dict:
    """三项检测一次算完，返回可直接写 `quality_json` 的 dict。

    任何一项的输入缺失（无大纲/无章节号/无角色卡）都只把该项标 `skipped`，
    不让检测本身变成新的失败点；但 `skipped` 必须带 `reason`（code + message），
    说清是哪一类缺失、该去补什么。所有阈值都来自调用方传入的配置。
    """
    result: dict[str, Any] = {
        "mode": mode,
        "chapter": chapter,
        "beat": {"status": "skipped"},
        "premature": {"status": "skipped"},
        "gender": {"status": "skipped"},
        "blocking": False,
        "blocking_reasons": [],
    }

    def _skip(check: dict, code: str, message: str) -> None:
        """跳过的原因必须能照着补：code 供程序判别，message 给人看。"""
        check["status"] = "skipped"
        check["reason"] = {"code": code, "message": message}

    if not text:
        message = "正文为空，结构检查没有可检查的文本。"
        for key in ("beat", "premature", "gender"):
            _skip(result[key], "no_text", message)
        return result

    entries = [str(x) for x in outline] if isinstance(outline, (list, tuple)) else []
    chapter_in_outline = (chapter is not None and bool(entries)
                          and 1 <= chapter <= len(entries))
    if not entries:
        for key in ("beat", "premature"):
            _skip(result[key], "outline_entry_empty",
                  "项目章纲为空，情节点与越章检查没有比对物；请先补章纲。")
    elif chapter is None:
        for key in ("beat", "premature"):
            _skip(result[key], "no_chapter_label",
                  "本段没有章号（标签是「段N」），无法定位章纲条目；"
                  "请先给本段分配章号。")
    elif chapter < 1 or chapter > len(entries):
        for key in ("beat", "premature"):
            _skip(result[key], "chapter_beyond_outline",
                  f"第{chapter}章不在章纲范围内（共{len(entries)}条），"
                  f"请先补第{chapter}章章纲。")
    elif not entries[chapter - 1].strip():
        _skip(result["beat"], "outline_entry_empty",
              f"第{chapter}章章纲条目为空，请先填写本章章纲。")
    text_bigrams = _bigrams(text)

    # —— 1. 本章情节点覆盖 ——
    if chapter_in_outline and entries[chapter - 1].strip():
        beats, split_level = _beats(entries[chapter - 1], beat_min_units)
        if beats:
            rows = _beat_coverage(beats, text_bigrams)
            sufficient = len(beats) >= beat_min_units
            covered = [r for r in rows if r["coverage"] >= beat_min_coverage]
            result["beat"] = {
                "status": "checked",
                "total": len(rows),
                "units": len(beats),
                "min_units": beat_min_units,
                "split_level": split_level,
                "min_coverage": beat_min_coverage,
                # 条数不足时单元覆盖率不具区分度（1/1 恒 1.00、1/2 只有 0/0.5/1
                # 三档）：原始值照常给出，但改由 `fallback_term_coverage` 判。
                "discrimination": "sufficient" if sufficient else "insufficient",
                # 判据自本次起固定计入：条数够时用单元覆盖率，不够时切到
                # `fallback_term_coverage`；`basis` 明确告诉读者这一章按哪把尺判。
                "basis": "beat_units" if sufficient else "fallback_term_coverage",
                "counted": True,
                "covered": len(covered) if sufficient else None,
                "coverage": (round(len(covered) / len(rows), 3)
                             if sufficient and rows else None),
                "missing": [r["beat"] for r in rows
                            if r["coverage"] < beat_min_coverage] if sufficient else [],
            }
            if not sufficient:
                # 方案 B 兜底：本章大纲独有词三元组作分母（与越章检测同源）。
                fallback = _beat_fallback(
                    entries, chapter - 1, text, beat_min_coverage)
                result["beat"]["fallback"] = fallback
                # 升为正式判据后，与 beat_missing 同权进入阻断理由；
                # `report` 模式仍只标记、不拦落库。词项为空（skipped）不判。
                if fallback.get("status") == "checked" and not fallback.get("passed"):
                    result["blocking_reasons"].append("beat_fallback_low")
            elif len(covered) < len(rows):
                result["blocking_reasons"].append("beat_missing")
        else:
            _skip(result["beat"], "outline_entry_empty",
                  f"第{chapter}章章纲条目切不出至少 {beat_min_units} 个情节点，"
                  "请把本章章纲写成可执行的短句（用「；」或句号分隔）。")

    # —— 2. 越章材料（后续章独有词项命中）——
    if chapter_in_outline and premature_lookahead > 0:
        allowed: set[str] = set()
        for entry in entries[:chapter]:
            allowed |= _future_terms(entry)
        text_terms = _bigrams(text) | _trigrams(text)
        # 角色名本身在两章都会出现，不构成「越章材料」：把名字片段从候选里剔掉。
        name_grams: set[str] = set()
        for card in character_cards or []:
            if isinstance(card, dict):
                nm = str(card.get("name") or "").strip()
                name_grams |= {a + b for a, b in zip(nm, nm[1:])}
        hits: list[dict] = []
        future_entries = entries[chapter: chapter + premature_lookahead]
        for offset, entry in enumerate(future_entries, start=1):
            future_only = {
                t for t in (_future_terms(entry) - allowed)
                if not any(ng in t for ng in name_grams)
            }
            matched = sorted(g for g in future_only if g in text_terms)
            if matched:
                hits.append({
                    "chapter": chapter + offset,
                    "terms": matched[:8],
                })
        result["premature"] = {
            "status": "checked",
            "lookahead": premature_lookahead,
            "hits": hits,
            "chapters_hit": len(hits),
            "max_hits": premature_max_hits,
        }
        if len(hits) > premature_max_hits:
            result["blocking_reasons"].append("premature_material")
    elif chapter_in_outline:
        _skip(result["premature"], "disabled",
              "越章检查已关闭（premature_lookahead=0），本次不比对后续章材料。")

    # —— 3. 人物性别一致性 ——
    if character_cards:
        mismatches = _gender_mismatches(text, character_cards, gender_window)
        result["gender"] = {
            "status": "checked",
            "cards": sum(1 for c in character_cards
                         if isinstance(c, dict) and _gender_pronoun(
                             c.get("gender") or c.get("性别"))),
            "mismatches": mismatches,
            "count": len(mismatches),
            "max_mismatches": gender_max_mismatches,
        }
        if len(mismatches) > gender_max_mismatches:
            result["blocking_reasons"].append("gender_mismatch")
    else:
        _skip(result["gender"], "no_character_cards",
              "没有可用的人物卡，性别一致性没有检查对象。")

    result["blocking"] = bool(result["blocking_reasons"])
    # `block` 模式才真拦；`report` 只标记，落库照旧。
    result["enforced"] = bool(result["blocking"] and str(mode).lower() == "block")
    return result
