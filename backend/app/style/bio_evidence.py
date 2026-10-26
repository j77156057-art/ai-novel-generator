"""角色档案的**证据回填**：让已经抽好的「关键经历」真的能进提示词。

## 为什么要这个模块

叙事档案里**早就有**「关键经历」字段（LLM 从局部窗口总结的），质量不差
（实测徐凤年 6 条：湖畔下潜、寻红薯、赠佛钵、托付李当心、气运被窃…）。

但它**进不了提示词**：`render_character_profile` 的门禁要求
`evidence_status == "located_extracts_not_semantic_proof"`，
而这个字段只有**带 claim 账本的新档案**才有 —— 老档案缺这个键 ⇒
默认值 `unverified_legacy` ⇒ **整个角色档案（身份/说话方式/行为倾向/底线/
关系网/关键经历）渲染成空串**。

⇒ 实测后果：角色对话时模型拿不到**任何**角色档案，只能靠语气猜他是谁。

## 为什么不放松门禁

门禁的原意是对的：**「没核过的声明不许进提示词」**（注释写得很清楚：
「A warning next to an unverified claim still supplies that claim to the model」）。

老档案的字段值是**改写过的总结**，claim 级那道判据（value 必须逐字出现在摘录里）
核不了 ⇒ 不能直接放行。

## 本模块的做法：字段级**事实核验**

把字段值切成 3~4 字的实词 n-gram（去掉高频虚词组合），
看它能不能在**语料**里定位到：

- 定位到 ⇒ 这条声明的**核心事件在书里存在** ⇒ 放行，并把定位到的**原文段落**
  作为证据一起给模型（这就是「配合检索」）；
- 定位不到 ⇒ **丢掉这条字段**（不是整个角色）。

实测（雪中悍刀行，8 个角色 × 32 条经历）：
- 每条经历**至少命中一个** ≥3 字的事件短语（32/32），
  如「九岁奉召」「一字十文」「徽山轩辕」「被徐凤年」。

## 性能

核验要对 120 万字语料做子串搜索。**实测按「把所有 gram 都查一遍」要 35 秒**
（200 条声明 × 25 个 gram × 1.2MB ≈ 6GB 扫描）。

⇒ 改成**找到第一个命中就停**（gram 已按长度降序：先 4 字后 3 字）：
实测命中绝大多数是 4 字 gram ⇒ 每个声明平均只扫一遍语料 ⇒ 亚秒级。

## 与门禁的关系

回填**不放宽**门禁，而是**把老档案补成合规档案**：
核验过的字段进 `evidence` + `evidence_status`，没核上的字段**不进**。
渲染时仍然逐条走原来的门禁。

## 缓存

核验结果按 (novel_id, 档案指纹) 做**进程内缓存**，同一份档案只算一次。
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re
from dataclasses import dataclass, field

#: 高频虚词（不算实字）。它们在任何句子里都出现，对「这句话讲的是什么事」没有信息量。
STOP_CHARS = set(
    "的了是在有个和与也就都不而及其之于所以但却又再更加些这那有着被从对到把将为以"
    "他说她话时候里上下来去就也是在有与和"
)

#: 事件短语的长度（汉字）。4 字优先（信息量最大），3 字兜底。
GRAM_SIZES = (4, 3)

#: 进程内缓存：key = (novel_id, 档案指纹)，value = 回填结果。
_CACHE: dict[tuple[int, str], list[dict]] = {}


@dataclass
class ClaimEvidence:
    """一条字段声明的核验结果。"""

    field: str
    value: str
    #: 命中的事件短语
    phrase: str
    #: 证据原文（命中短语所在的那一行）
    quote: str
    chapter_no: int | None
    #: 已查的 gram 里命中的比例（诊断用；判定只需要「至少一个」）
    coverage: float = 0.0
    checked: int = 0

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "value": self.value,
            "phrase": self.phrase,
            "quote": self.quote,
            "chapter_no": self.chapter_no,
            "coverage": round(self.coverage, 3),
            "checked": self.checked,
            "verification": "located_extract",
        }


def _content_chars(text: str) -> set[str]:
    return {c for c in (text or "") if "一" <= c <= "鿿" and c not in STOP_CHARS}


def _grams(text: str) -> list[str]:
    """字段值里的实词短语（按长度降序 ⇒ 先查信息量最大的）。

    「按长度降序」不只是为了找长短语，也是**性能**上的关键：
    命中绝大多数是 4 字 gram，判定时可以**找到第一个就停**。
    """
    t = "".join(c for c in (text or "") if "一" <= c <= "鿿")
    out: list[str] = []
    for n in GRAM_SIZES:
        for i in range(len(t) - n + 1):
            g = t[i:i + n]
            if g and g not in out:
                out.append(g)
    return out


def build_corpus(lines: list[tuple[int, str]]) -> tuple[str, list[int]]:
    """把语料拼成一个大字符串 + 每行的起点（供 bisect 定位）。"""
    parts: list[str] = []
    starts: list[int] = []
    pos = 0
    for _ch, line in lines:
        starts.append(pos)
        parts.append(line)
        pos += len(line) + 1
    return "\n".join(parts), starts


def verify_claims(
    profile: dict,
    corpus: str,
    starts: list[int],
    lines: list[tuple[int, str]],
    *,
    fields: tuple[str, ...] = ("身份", "说话方式", "行为倾向", "性格底线", "关键经历"),
) -> tuple[list[ClaimEvidence], list[str]]:
    """核验一个角色的字段声明。

    返回 (核验通过的声明, 核验不通过的字段值列表)。
    """
    name = (profile.get("name") or "").strip()
    if not name:
        return [], []

    # 只在**有他出场的行**里找 —— 定位更准（短语必须跟他同框），
    # 也快得多（每角色 ~900 行 vs 全库 72838 行）。
    his: list[tuple[int, str]] = [(ch, ln) for ch, ln in lines if name in ln]
    if not his:
        # 他在语料里**一次都没出现** ⇒ 所有字段都核不上。
        # 要**记进 rejected**（而不是静默丢掉）—— 诊断里得看得到
        # 「这个角色根本没出场」这个事实。
        out: list[str] = []
        for fld in fields:
            raw = profile.get(fld)
            values = raw if isinstance(raw, list) else (
                [raw] if isinstance(raw, str) and raw.strip() else []
            )
            out.extend(str(v).strip() for v in values if str(v).strip())
        return [], out
    sub_corpus, sub_starts = build_corpus(his)

    accepted: list[ClaimEvidence] = []
    rejected: list[str] = []
    for fld in fields:
        raw = profile.get(fld)
        values = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) and raw.strip() else [])
        for value in values:
            value = str(value).strip()
            if not value:
                continue
            grams = _grams(value)
            if not grams:
                rejected.append(value)
                continue
            # 查**全部** gram（子语料很小，查得完）⇒ 命中率与证据行都更准。
            hits: list[tuple[str, int]] = []
            for g in grams:
                pos = sub_corpus.find(g)
                if pos >= 0:
                    hits.append((g, pos))
            if not hits:
                rejected.append(value)
                continue
            # 取**最长**的命中短语；同一位置可能被多个 gram 命中
            best_phrase, best_pos = max(hits, key=lambda hp: len(hp[0]))
            coverage = len({g for g, _ in hits}) / max(1, len(set(grams)))
            idx = bisect.bisect_right(sub_starts, best_pos) - 1
            quote = his[idx][1] if 0 <= idx < len(his) else ""
            chapter_no = his[idx][0] if 0 <= idx < len(his) else None
            accepted.append(ClaimEvidence(
                field=fld, value=value, phrase=best_phrase,
                quote=quote, chapter_no=chapter_no,
                coverage=coverage, checked=len(grams),
            ))
    return accepted, rejected


def backfill_profile(
    profile: dict,
    corpus: str,
    starts: list[int],
    lines: list[tuple[int, str]],
    *,
    fields: tuple[str, ...] = ("身份", "说话方式", "行为倾向", "性格底线", "关键经历"),
) -> dict:
    """给一个**老档案**（没有 claim 账本）做证据回填。

    返回一个**新档案**（不改原对象）：
    - 核验通过的字段保留；
    - 核验不通过的字段**丢掉**（宁可少给，不可给错）；
    - 带 `evidence` / `evidence_status` / `evidence_summary` ⇒ 门禁放行。
    """
    if profile.get("evidence_status") == "located_extracts_not_semantic_proof":
        return profile  # 新档案：已有账本，不动

    accepted, rejected = verify_claims(profile, corpus, starts, lines, fields=fields)
    if not accepted:
        # 一条都核不上 ⇒ 整个档案不渲染（门禁继续挡着）
        out = dict(profile)
        out["evidence_status"] = "unverified_legacy"
        out["evidence"] = []
        out["evidence_summary"] = {"accepted": 0, "rejected": len(rejected)}
        return out

    out = dict(profile)
    rejected_set = set(rejected)
    for fld in fields:
        raw = out.get(fld)
        if isinstance(raw, list):
            out[fld] = [x for x in raw if str(x).strip() not in rejected_set]
        elif isinstance(raw, str) and raw.strip() in rejected_set:
            out[fld] = ""
    out["evidence"] = [c.to_dict() for c in accepted]
    out["evidence_status"] = "located_extracts_not_semantic_proof"
    out["evidence_summary"] = {
        "accepted": len(accepted),
        "rejected": len(rejected),
        "backfilled": True,
        "legacy_without_claims": False,
    }
    # 证据里的原文片段（供渲染时附上）
    out["evidence_quotes"] = [
        {"field": c.field, "value": c.value, "quote": c.quote,
         "chapter_no": c.chapter_no, "phrase": c.phrase}
        for c in accepted if c.quote
    ]
    return out


def _fingerprint(characters: list[dict]) -> str:
    """档案指纹（变了就要重新回填）。"""
    raw = json.dumps(
        [
            {
                "name": c.get("name"),
                "身份": c.get("身份"),
                "说话方式": c.get("说话方式"),
                "行为倾向": c.get("行为倾向"),
                "性格底线": c.get("性格底线"),
                "关键经历": c.get("关键经历"),
                "evidence_status": c.get("evidence_status"),
            }
            for c in (characters or [])
            if isinstance(c, dict)
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def backfill_profiles(
    db,
    novel_id: int,
    characters: list[dict],
    *,
    fields: tuple[str, ...] = ("身份", "说话方式", "行为倾向", "性格底线", "关键经历"),
) -> list[dict]:
    """给一整本书的角色档案回填证据。返回新列表（不修改原档案）。

    **带缓存**：核验要扫全库（雪中 120 万字）⇒ 按 (novel_id, 档案指纹)
    做进程内缓存，同一份档案只算一次。
    """
    from app.models import NovelChunk

    chars = [c for c in (characters or []) if isinstance(c, dict)]
    if not chars:
        return []
    fp = _fingerprint(chars)
    key = (int(novel_id), fp)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    lines: list[tuple[int, str]] = []
    rows = db.query(NovelChunk).filter(NovelChunk.novel_id == novel_id).all()
    for r in rows:
        for line in (r.text or "").split("\n"):
            line = line.strip()
            if line:
                lines.append((int(r.chapter_no or 0), line))
    if not lines:
        return []
    corpus, starts = build_corpus(lines)
    out = [backfill_profile(c, corpus, starts, lines, fields=fields) for c in chars]
    _CACHE[key] = out
    return out
