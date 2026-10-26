"""正文的**文字质量**审核（工序链「⑥ 正文审查」的第三块）。

## 它审什么

1. **标点事故**（确定性、几乎不会误报 → **error** 级）：
   - 中文之间出现半角 `,` `.` `;` `:`；
   - 连续重复标点（`。。`、`，，`）。
2. **外来拉丁词**（确定性但**故意不阻塞** → **warn** 级）：
   中文叙事里混进英文单词。

## 为什么「外来词」只报不拦

小说里合法地会出现英文：品牌、型号、术语、对话里的引文、专名。
白名单**永远盖不全** —— 而误报会**堵死真活**（生成闸门把 error 变成 409，
一道误报就能让项目写不出字）。所以判据分两档：

- **能用规则判死的**（标点）才 error；
- **判不准的**（外来词）只报出来给人看，不阻塞。

这条分档不是保守，是从实测来的：真书正文里那个 `River` 确实该改，
但同一套规则放到别的书（写代码的、写品牌的）就会误报。

## 与既有审核的分工（别抢活）

| 审核 | 审什么 |
|------|--------|
| 封闭清单审计 | **越界**：正文里出现了契约之外的物品/角色/地点 |
| 整段验收 | 与**保存权威**一致（时间/地点/参与者/物品状态） |
| 本模块 | **文字本身**的质量（标点、外来词） |

「正文内部的事实自洽」（同一实体年龄打架）属���模型那层，见
`docs/text-review-and-draft-proposal-design.md` A2。
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from app.agents.diagnosis import (
    DiagnosticIssue,
    DiagnosticReport,
    IssueEvidence,
    make_issue_id,
)

#: 每条 issue 最多挂几条证据（防爆响应体；完整清单进 claim）。
MAX_EVIDENCE_PER_ISSUE = 5

#: 证据片段的最大长度。
_MAX_SNIPPET = 160

#: 中日韩统一表意文字（判「中文句子」用）。
_CJK = r'一-鿿㐀-䶿'

#: 中文之间夹着半角标点 —— 几乎一定是排版事故。
#: （两个分支都是定宽的 lookaround：变长 lookbehind 在 `re` 里会直接编译失败。）
_HALFWIDTH_BETWEEN_CJK = re.compile(
    r'[' + _CJK + r']([,.;:])'
    r'|([,.;:])[' + _CJK + r']'
)

#: 连续重复的标点。
_REPEATED_PUNCT = re.compile(r'([，。！？；：、,.;:])\1+')

#: 拉丁词（至少 3 个字母，允许词内撇号）。
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'’]{2,}")

#: 引号对（用于判断某个词是否落在引文里）。
_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (
    ('「', '」'), ('『', '』'), ('“', '”'), ('‘', '’'),
    ('"', '"'), ("'", "'"),
)

#: 书名号里的内容（书名/刊名可以是外文）。
_BOOK_TITLE = re.compile(r'《[^》]*》')


def _is_cjk(ch: str) -> bool:
    return bool(ch) and bool(re.match('[' + _CJK + ']', ch))


def _context(text: str, start: int, end: int, pad: int = 24) -> str:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    return text[lo:hi].replace('\n', ' ')


def _inside_quotes(text: str, start: int) -> bool:
    """这个位置是否落在引文/书名号里（引文里的外文是合法的）。"""
    for m in _BOOK_TITLE.finditer(text):
        if m.start() <= start < m.end():
            return True
    for open_ch, close_ch in _QUOTE_PAIRS:
        open_idx = text.rfind(open_ch, 0, start)
        close_idx = text.rfind(close_ch, 0, start)
        if open_idx > close_idx:
            return True
    return False


def _known_names(project: Any) -> set[str]:
    """项目里已登记的**专名**（角色/地名）—— 它们出现在正文里是合法的。

    来源：剧情圣经（characters / relationships）+ 叙事档案里的角色。
    """
    names: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            names.add(value.strip())

    bible = getattr(project, 'plot_bible', None)
    if isinstance(bible, dict):
        for entry in (bible.get('characters') or []):
            if isinstance(entry, dict):
                _add(entry.get('name'))
        for entry in (bible.get('relationships') or []):
            if isinstance(entry, dict):
                _add(entry.get('a'))
                _add(entry.get('b'))
    narrative = getattr(project, 'style_narrative', None)
    if isinstance(narrative, dict):
        for entry in (narrative.get('characters') or []):
            if isinstance(entry, dict):
                _add(entry.get('name'))
    return names


def _suspect_latin_tokens(text: str, known: set[str]) -> list[tuple[str, int, int, bool]]:
    """中文叙事里可疑的拉丁词（词、起始偏移、结束偏移、是否紧贴中文）。

    放过的：贴着数字或连字符（型号）、已登记的专名、≤2 字母、
    全大写缩写（DNA / NASA）、**引文里且与中文分开的**（“Hello” 他说 —— 合法）。

    ## 为什么「紧贴中文」要单独看

    紧贴（`Already惹了`）和分开（`River 里头`）是**两种症状**：
    - 分开的：可能是引文、术语、品牌、专名 —— 白名单盖不全，判不准；
    - 紧贴的：模型在中英之间漏了空格，是**明确的排版事故**
      （“Already惹了。” 这种混排读起来就是坏的）。

    所以引号的豁免只对**分开的**词有效：紧贴的词在引号里也一样是事故
    （实测真书正文：「Already惹了。」）。
    """
    out: list[tuple[str, int, int, bool]] = []
    for m in _LATIN_WORD.finditer(text):
        word = m.group(0)
        start, end = m.start(), m.end()
        bare = word.strip("'’")
        if len(bare) <= 2:
            continue
        if bare.isupper() and len(bare) <= 4:
            continue
        prev_ch = text[start - 1] if start > 0 else ''
        next_ch = text[end] if end < len(text) else ''
        if prev_ch.isdigit() or next_ch.isdigit() or prev_ch in '-_' or next_ch in '-_':
            continue  # 型号 / 编号
        if bare in known:
            continue
        glued = bool(prev_ch and _is_cjk(prev_ch)) or bool(next_ch and _is_cjk(next_ch))
        if not glued and _inside_quotes(text, start):
            continue  # 引文里的独立外文：合法
        # 附近（前后各 40 字）有中文才算「中文叙事里混进外文」。
        # **前后都看**：段首的词前面没有中文（“Already惹了。” 就在段首），
        # 只看前面会漏掉最常见的那种。
        lo = max(0, start - 40)
        hi = min(len(text), end + 40)
        if not re.search('[' + _CJK + ']', text[lo:hi]):
            continue
        out.append((word, start, end, glued))
    return out


def _evidence_rows(gens: Sequence[Any], rows: Sequence[tuple[Any, str, str]]
                   ) -> tuple:
    out = []
    for gen, snippet, ref in rows[:MAX_EVIDENCE_PER_ISSUE]:
        out.append(IssueEvidence(
            kind="generation",
            source="generation",
            ref_id=ref,
            snippet=snippet[:_MAX_SNIPPET],
            score=0.8,
        ))
    return tuple(out)


#: 半角 → 全角标点（只改规则命中的那几处）。
_FULLWIDTH_PUNCT: dict[str, str] = {
    ',': '，', '.': '。', ';': '；', ':': '：',
}


def fix_punctuation(text: str) -> tuple[str, int]:
    """把标点事故**机械地**改对：重复标点收成一个、半角换全角。

    返回 `(改后的文本, 改了几处)`。

    ## 为什么不让模型来改

    修法是**唯一确定**的（`。。` → `。`、中文旁的 `,` → `，`）。
    模型去改反而可能把好字改坏，还会多烧一次调用。
    这里的分工是：规则算出「改后应该长什么样」，模型只负责**提出**，
    人在卡上确认 —— 服务端不替用户写正文。
    """
    changed = 0
    # —— 第一遍：连续重复的标点收成一个 ——
    pieces: list[str] = []
    last = 0
    for m in _REPEATED_PUNCT.finditer(text):
        pieces.append(text[last:m.start()])
        pieces.append(m.group(1))
        last = m.end()
        changed += 1
    pieces.append(text[last:])
    text = ''.join(pieces)
    # —— 第二遍：命中规则的那些半角标点换全角 ——
    pieces = []
    last = 0
    for m in _HALFWIDTH_BETWEEN_CJK.finditer(text):
        pieces.append(text[last:m.start()])
        for ch in m.group(0):
            pieces.append(_FULLWIDTH_PUNCT.get(ch, ch))
        last = m.end()
        changed += 1
    pieces.append(text[last:])
    return ''.join(pieces), changed


def suggest_punctuation_fixes(db: Any, project: Any) -> list[dict[str, Any]]:
    """给出「改前 / 改后」的标点修正建议（只算确定能改的那些）。

    每一项：`{generation_id, segment_no, before, after, changes}`。
    `changes` 是改了几处；`after` 是改后的整段正文（**整段**，不是片段）——
    提案的 `after_text` 要的是整段替换。
    """
    from sqlalchemy import select

    from app.models import Generation

    gens = list(db.scalars(
        select(Generation).where(Generation.project_id == project.id)
        .order_by(Generation.segment_no, Generation.id)).all())
    out: list[dict[str, Any]] = []
    for gen in gens:
        text = gen.text or ''
        if not text.strip():
            continue
        after, changes = fix_punctuation(text)
        if changes and after != text:
            out.append({
                'generation_id': gen.id,
                'segment_no': gen.segment_no,
                'before': text,
                'after': after,
                'changes': changes,
            })
    return out


def run_text_quality_audit(db: Any, project: Any,
                           provider: Any = None) -> tuple[DiagnosticReport, dict[str, Any]]:
    """跑一遍文字质量审核。

    返回 `(DiagnosticReport, 概要统计)` —— 与其它审核同构（九字段 issue）。

    `provider` 给了才会跑**事实自洽**那一层（要调模型）；不给就只跑确定性规则。
    """
    from sqlalchemy import select

    from app.models import Generation

    gens = list(db.scalars(
        select(Generation).where(Generation.project_id == project.id)
        .order_by(Generation.segment_no, Generation.id)).all())
    taken: set[str] = set()
    issues: list[DiagnosticIssue] = []
    known = _known_names(project)

    punct_rows: list[tuple[Any, str, str]] = []
    latin_rows: list[tuple[Any, str, str]] = []
    punct_hits: list[str] = []
    latin_hits: list[str] = []
    glued_hits: list[str] = []

    for gen in gens:
        text = gen.text or ''
        if not text.strip():
            continue
        for m in _HALFWIDTH_BETWEEN_CJK.finditer(text):
            punct_rows.append((gen, _context(text, m.start(), m.end()), str(gen.id)))
            punct_hits.append(f'第{gen.segment_no}段「{m.group(0)}」')
        for m in _REPEATED_PUNCT.finditer(text):
            punct_rows.append((gen, _context(text, m.start(), m.end()), str(gen.id)))
            punct_hits.append(f'第{gen.segment_no}段「{m.group(0)}」')
        for word, start, end, glued in _suspect_latin_tokens(text, known):
            latin_rows.append((gen, _context(text, start, end), str(gen.id)))
            latin_hits.append(f'第{gen.segment_no}段「{word}」')
            if glued:
                glued_hits.append(f'第{gen.segment_no}段「{word}」')

    if punct_hits:
        issues.append(DiagnosticIssue(
            id=make_issue_id("text_punctuation", f"n{len(punct_hits)}", taken),
            type="text_quality",
            severity="error",
            title=f"{len(punct_hits)} 处标点事故",
            claim=("正文里出现半角标点夹在中文之间，或连续重复的标点："
                   + "、".join(punct_hits[:8])
                   + ("…" if len(punct_hits) > 8 else "")
                   + "。这类地方在中文正文里几乎都是排版事故。"),
            suggestion=("改成中文全角标点、去掉重复的那个；"
                        "要批量改就走提案（批准后才写进正文）。"),
            rule="text_punctuation_accident",
            evidence=_evidence_rows(gens, punct_rows),
        ))

    if latin_hits:
        extra = ''
        if glued_hits:
            extra = ('其中 ' + "、".join(glued_hits[:6])
                     + ('…' if len(glued_hits) > 6 else '')
                     + ' 是**紧贴中文**的（中英之间漏了空格），基本可以确定要改。')
        issues.append(DiagnosticIssue(
            id=make_issue_id("text_foreign_latin", f"n{len(latin_hits)}", taken),
            type="text_quality",
            # 故意是 warn 不是 error：白名单盖不全（品牌/术语/引文/专名都可能是外文），
            # 而误报会堵死真活。判不准的东西只报给人看。
            severity="warn",
            title=f"{len(latin_hits)} 处疑似外来英文",
            claim=("中文叙事里出现了这些英文词："
                   + "、".join(latin_hits[:8])
                   + ("…" if len(latin_hits) > 8 else "")
                   + "。" + extra
                   + "如果是引文、型号或专名就不用管；如果是从别处漏进来的，建议改掉。"),
            suggestion="逐处看一眼：该留的留着，该换的走提案改掉。",
            rule="text_foreign_latin",
            evidence=_evidence_rows(gens, latin_rows),
        ))

    stats = {
        'segments': len(gens),
        'punctuation_hits': len(punct_hits),
        'foreign_latin_hits': len(latin_hits),
        'foreign_latin_glued': len(glued_hits),
        'blocking': sum(1 for i in issues if i.severity == 'error'),
    }

    # —— 第三层：事实自洽（要模型；只报不挡）——
    if provider is not None:
        try:
            facts, fact_stats = check_fact_consistency(db, project, provider)
        except Exception as exc:  # noqa: BLE001 - 这一层失败不该拖垮整次审核
            facts, fact_stats = [], {'checked': False, 'error': type(exc).__name__}
            # **不静默**：这一层挂掉时，统计里要看得见 ——
            # 否则「没报=没问题」会被误读成「查过没问题」。
            stats['fact_error'] = f'{type(exc).__name__}: {str(exc)[:120]}'
        else:
            issues.extend(facts)
            stats['fact_contradictions'] = fact_stats.get('count', 0)
            stats['fact_checked'] = True
    else:
        stats['fact_checked'] = False

    report = DiagnosticReport(
        issues=issues,
        extra_evidence=[],
        tool_results=stats,
        rules_evaluated=[
            'text_punctuation_accident',
            'text_foreign_latin',
            'text_fact_contradiction',
        ],
    )
    return report, stats


# ----------------------------------------------------------------------
# 事实自洽：同一实体在正文里属性打架（模型判，只报不挡）
# ----------------------------------------------------------------------

#: 交给模型看的正文上限（字符）。截断会在 issue 里**说清**，不静默。
_FACT_CHECK_MAX_CHARS = 12000

_FACT_CHECK_SYSTEM = (
    '你是小说校对。只找**同一个实体**在正文里的**关键属性**前后矛盾，'
    '属性只限：年龄、身份/关系、数量、称呼。\n'
    '不算矛盾的情况：时间推进导致的合理变化（如「三年前」与「如今」）、'
    '同一人的不同侧面、角色撒谎、视角人物的猜测。\n'
    '不确定就不报。宁可漏报，不要误报。\n'
    '只输出 JSON：{"contradictions":[{"entity":"实体名","attribute":"属性",'
    '"values":["说法1","说法2"],"source_ids":["G1P001"],"quote":"原文片段",'
    '"reason":"为什么矛盾"}]}。没有矛盾就输出 {"contradictions":[]}。'
)


def check_fact_consistency(db: Any, project: Any, provider: Any
                           ) -> tuple[list[Any], dict[str, Any]]:
    """同一实体的关键属性在正文里前后矛盾 → 报出来（**warn，不阻塞**）。

    ## 为什么只报不挡

    模型的判断会错：时间推进、角色撒谎、视角人物的猜测，看上去都像矛盾。
    所以这一层**一律 warn**：作为开放项给人看，不挡工序链、不挡生成。
    封闭清单审计那边的教训是同一个：矛盾 = 证据不足，不是确认有问题。

    ## 为什么只问「同一实体」

    不同实体之间「对不上」往往是正常的（两个角色各说各话）。
    只有**同一个实体**的属性打架才是硬矛盾。
    """
    from sqlalchemy import select

    from app.agents.diagnosis import DiagnosticIssue, IssueEvidence, make_issue_id
    from app.models import Generation

    gens = list(db.scalars(
        select(Generation).where(Generation.project_id == project.id)
        .order_by(Generation.segment_no, Generation.id)).all())
    if not gens:
        return [], {'count': 0, 'checked': True, 'reason': 'no_body'}

    body_parts: list[str] = []
    source_index: dict[str, str] = {}
    truncated = False
    total = 0
    for gen in gens:
        text = gen.text or ''
        if not text.strip():
            continue
        for i, para in enumerate(text.split('\n')):
            para = para.strip()
            if not para:
                continue
            total += len(para)
            if total > _FACT_CHECK_MAX_CHARS:
                truncated = True
                break
            sid = f'G{gen.segment_no}P{i:03d}'
            source_index[sid] = para[:120]
            body_parts.append(f'[{sid}] {para}')
        if truncated:
            break

    if not body_parts:
        return [], {'count': 0, 'checked': True, 'reason': 'empty_body'}

    bible = getattr(project, 'plot_bible', None) or {}
    payload = {
        'body': '\n'.join(body_parts),
        'known_characters': [
            c.get('name') for c in (bible.get('characters') or [])
            if isinstance(c, dict) and c.get('name')
        ],
    }
    raw = provider.chat(_FACT_CHECK_SYSTEM,
                        json.dumps(payload, ensure_ascii=False))
    try:
        answer = json.loads(raw.strip().removeprefix('```json')
                           .removesuffix('```').strip())
    except Exception:  # noqa: BLE001 - 模型答得不合格就不报（不猜）
        return [], {'count': 0, 'checked': True, 'reason': 'unparsable'}
    items = answer.get('contradictions') if isinstance(answer, dict) else None
    if not isinstance(items, list):
        return [], {'count': 0, 'checked': True, 'reason': 'invalid_shape'}

    taken: set[str] = set()
    issues: list[Any] = []
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        entity = str(item.get('entity') or '').strip()
        attribute = str(item.get('attribute') or '').strip()
        values = [str(v).strip() for v in (item.get('values') or []) if str(v).strip()]
        quote = str(item.get('quote') or '').strip()
        reason = str(item.get('reason') or '').strip()
        if not entity or not attribute or len(values) < 2:
            continue
        source_ids = [str(s) for s in (item.get('source_ids') or []) if str(s)]
        evidence = tuple(
            IssueEvidence(kind='generation', source='generation',
                          ref_id=sid,
                          snippet=source_index.get(sid, quote or '')[:200],
                          score=0.6)
            for sid in source_ids[:3]
        ) or (IssueEvidence(kind='generation', source='generation',
                            ref_id=str(entity), snippet=quote[:200], score=0.6),)
        issues.append(DiagnosticIssue(
            id=make_issue_id('text_fact_contradiction', entity, taken),
            type='text_quality',
            # 一律 warn：模型的判断会错，矛盾只说明「值得人看一眼」。
            severity='warn',
            title=f'「{entity}」的{attribute}前后不一致',
            claim=(f'同一实体「{entity}」的{attribute}在正文里出现了两个说法：'
                   + '、'.join(values[:4]) + '。'
                   + (f'（{reason}）' if reason else '')
                   + ('注意：正文过长，本次只核对了前面一部分。' if truncated else '')),
            suggestion=('人看一眼：如果这是时间推进/角色撒谎/视角猜测，就不用管；'
                        '否则走提案改掉其中一处。'),
            rule='text_fact_contradiction',
            evidence=evidence,
        ))
    return issues, {'count': len(issues), 'checked': True, 'truncated': truncated}
