"""产品链路的时代越界护栏：提示块 + 生成后自检。

默认 **关闭**（`era_guard_enabled=False`），按项目显式打开。打开后默认 **report**：
只扫描、把结果落进 `Generation.summary["era"]`，不改正文。只有
`ERA_GUARD_MODE=rewrite` 时才走句级改写——实验已证明句级独立重写会把
语义拼散（铜镜被改成导航、电脑被改去当砚台），所以这是显式开关，不是默认能力。

基线必须由项目**显式声明**（`plot_bible` 的世界设定或 `project.genre`）。
读不到或认不出一律**不注入约束**，meta 记 `baseline_unknown` 并告警——
实验量过：同一批 30 条原生器物，baseline 从王朝换现代 30/30 全部翻转。
基线判错会让整章被误判，而不注入只是少一层约束，两者代价不对等。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings
from app.kg.era_guard import DATA_FILE, EraGuard
from app.kg.era_prompt import build_era_constraint as _render_era_constraint

logger = logging.getLogger(__name__)

#: meta 里的三态：没开 / 开了但判不出基线 / 真正生效。
STATUS_DISABLED = "disabled"
STATUS_BASELINE_UNKNOWN = "baseline_unknown"
STATUS_ACTIVE = "active"

#: 这些值等于「没有声明」，不能拿去做基线匹配。
_UNDECLARED = {"", "未指定", "未知", "其他", "无", "none", "null", "unknown"}


#: 基线 → 允许的科技档位。档位名以 `world_profile.json` 的 `tech_lexicon` 为准。
_ALLOWED_BY_BASELINE: dict[str, tuple[str, ...]] = {
    "王朝": ("原生",),
    "近现代": ("原生", "近现代"),
    "现代": ("原生", "近现代"),
    "未来": ("原生", "近现代", "未来"),
}

#: 项目里可能的写法 → 规范基线。判定顺序按声明顺序：
#: 先认「未来/近现代」，再认「现代」，最后才落「王朝」，避免「未来都市」
#: 被「都市」抢先判成现代、也避免「近现代」被「现代」截胡。
_BASELINE_WORDS: dict[str, tuple[str, ...]] = {
    "未来": ("未来", "科幻", "星际", "太空", "赛博"),
    "近现代": ("近现代", "民国", "近代", "工业", "蒸汽"),
    "现代": ("现代", "当代", "都市"),
    "王朝": ("王朝", "古代", "古装", "架空", "修仙", "玄幻", "武侠", "封建", "前现代", "历史"),
}

_BASELINE_KEYS = (
    "baseline", "world_baseline", "世界基线", "时代基线", "时代背景", "era",
)
_WORLD_CONTAINERS = (
    "world", "world_setting", "worldview", "世界观", "设定", "setting", "settings", "背景",
)

_VIOLATIONS = ("hard_ban", "needs_authorization")


def _bible(project) -> dict:
    bible = getattr(project, "plot_bible", None)
    return bible if isinstance(bible, dict) else {}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return ""
    return str(value).strip()


def _declared_baseline(bible: dict) -> tuple[str, str]:
    """返回 `(原始基线文本, 来源键)`；没有声明时返回 `("", "")`。"""
    for key in _BASELINE_KEYS:
        text = _as_text(bible.get(key))
        if text:
            return text, f"plot_bible.{key}"
    for container in _WORLD_CONTAINERS:
        box = bible.get(container)
        if not isinstance(box, dict):
            continue
        for key in _BASELINE_KEYS:
            text = _as_text(box.get(key))
            if text:
                return text, f"plot_bible.{container}.{key}"
    return "", ""


def normalize_baseline(raw: str) -> str:
    """把「王朝架空」「近现代都市」这类描述收敛到已知基线；认不出返回空串。"""
    text = _as_text(raw)
    if not text:
        return ""
    for canonical, words in _BASELINE_WORDS.items():
        if any(word in text for word in sorted(words, key=len, reverse=True)):
            return canonical
    return ""


def _is_declared(raw: str) -> bool:
    return _as_text(raw).strip().lower() not in _UNDECLARED


def world_baseline(project) -> tuple[str, str]:
    """返回 `(基线, 来源)`；没有显式声明或认不出时基线为 `""`。

    读取顺序：
    1. `plot_bible` 里的显式基线（作者最权威）；
    2. `project.genre`（都市/历史/玄幻…是项目设定的一部分，不是从正文猜）；
    3. 都没有 ⇒ `("", "default")`，**不回落到王朝、也不猜现代**。

    认不出时返回 `("", "unrecognized:<来源>")`，让调用方跳过注入并告警。
    """
    raw, source = _declared_baseline(_bible(project))
    if not _is_declared(raw):
        genre = _as_text(getattr(project, "genre", ""))
        raw = genre if _is_declared(genre) else ""
        source = "project.genre" if raw else ""
    if not _is_declared(raw):
        return "", "default"
    baseline = normalize_baseline(raw)
    if baseline:
        return baseline, source
    return "", f"unrecognized:{source}"


def _resolve(project) -> tuple[str, str, str]:
    """返回 `(status, baseline, source)`；baseline 为空表示判不出。"""
    baseline, source = world_baseline(project)
    if baseline:
        return STATUS_ACTIVE, baseline, source
    logger.warning(
        "时代基线未知（来源=%s），本次不注入时代约束、不做时代扫描",
        source or "none",
    )
    return STATUS_BASELINE_UNKNOWN, "", source


def project_profile(
    project, *, baseline: str | None = None, source: str | None = None
) -> dict:
    """默认 world_profile + 项目基线覆盖（不写盘、不改仓库默认表）。"""
    profile = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    if baseline is None:
        baseline, source = world_baseline(project)
    profile["baseline"] = baseline or ""
    profile["allowed_tech_levels"] = list(_ALLOWED_BY_BASELINE.get(baseline or "", ()))
    profile["_baseline_source"] = source or ""
    return profile


def character_names(project, reminders: dict | None = None) -> list[str]:
    names: set[str] = set()
    for card in _bible(project).get("characters") or []:
        if isinstance(card, dict):
            name = _as_text(card.get("name") or card.get("名字"))
            if name:
                names.add(name)
    for card in (reminders or {}).get("character_constraints") or []:
        if isinstance(card, dict):
            name = _as_text(card.get("name"))
            if name:
                names.add(name)
    return sorted(names, key=len, reverse=True)


def constraint_for(project, max_chars: int = 300) -> tuple[str, dict]:
    """渲染提示块 + 元信息。

    `meta.status` 三态：`disabled`（开关没开）/ `baseline_unknown`（项目没显式
    声明或判不出，**不注入**）/ `active`（真正注入）。前两态都返回空块。
    """
    # 用 getattr 读开关：本模块与 config 的 era 字段物理分块，单侧批次里
    # 字段可能不存在，此时按「未开启」处理，而不是抛 AttributeError。
    if not getattr(settings, "era_guard_enabled", False):
        return "", {"enabled": False, "status": STATUS_DISABLED, "chars": 0}
    status, baseline, source = _resolve(project)
    if status != STATUS_ACTIVE:
        return "", {
            "enabled": True,
            "status": STATUS_BASELINE_UNKNOWN,
            "baseline_source": source,
            "chars": 0,
        }
    profile = project_profile(project, baseline=baseline, source=source)
    block = _render_era_constraint(profile=profile, max_chars=max_chars)
    return block, {
        "enabled": True,
        "status": STATUS_ACTIVE,
        "baseline": baseline,
        "baseline_source": source,
        "chars": len(block),
    }


def _violation_item(row: dict) -> dict:
    return {
        "term": row.get("term"),
        "violation": row.get("violation"),
        "note": row.get("violation_note"),
        "sentence": row.get("sentence"),
        "line": row.get("line"),
        "tech_level": row.get("tech_level"),
        "gate": row.get("gate"),
    }


def scan_text(
    text: str,
    project,
    *,
    segment_no: int = 0,
    reminders: dict | None = None,
    guard: EraGuard | None = None,
) -> dict:
    """只读扫描；返回可直接落 meta 的摘要（明细最多留 20 条）。

    基线判不出时**不扫描**（拿错误基线扫只会制造假告警），meta 记
    `baseline_unknown`。
    """
    if not getattr(settings, "era_guard_enabled", False):
        return {"enabled": False, "status": STATUS_DISABLED}
    status, baseline, source = _resolve(project)
    if status != STATUS_ACTIVE:
        return {
            "enabled": True,
            "status": STATUS_BASELINE_UNKNOWN,
            "baseline_source": source,
            "mentions": 0,
            "violations": [],
            "counts": {},
        }
    profile = project_profile(project, baseline=baseline, source=source)
    guard = guard or EraGuard(
        profile=profile, character_names=character_names(project, reminders)
    )
    rows = guard.scan_text(
        text,
        chapter=segment_no,
        corpus=str(getattr(project, "title", "") or ""),
        source="generated",
    )
    violations = [r for r in rows if r.get("violation") in _VIOLATIONS]
    counts: dict[str, int] = {}
    for row in violations:
        key = str(row.get("violation"))
        counts[key] = counts.get(key, 0) + 1
    return {
        "enabled": True,
        "status": STATUS_ACTIVE,
        "mode": "report",
        "baseline": baseline,
        "baseline_source": source,
        "mentions": len(rows),
        "violations": [_violation_item(r) for r in violations[:20]],
        "counts": counts,
    }


_REWRITE_SYSTEM = (
    "你是中文古装/架空王朝小说的润色助手。只改用户给出的这一句，"
    "把不符合该时代基线的现代器物、现代概念换成本时代能出现的东西，"
    "必须保持原句的人物、动作、因果和语气不变。只输出改写后的句子，不要解释。"
)


def _rewrite_sentences(
    text: str,
    guard: EraGuard,
    provider,
    *,
    rounds: int,
) -> tuple[str, list[dict]]:
    """句级改写越界句；改不动就保留原句，不制造二次语义损伤。"""
    changed: list[dict] = []
    out = text
    for _ in range(max(1, rounds)):
        current_rows = guard.scan_text(
            out, chapter=0, corpus="", source="generated"
        )
        targets = [r for r in current_rows if r.get("violation") in _VIOLATIONS]
        if not targets:
            break
        # 按句去重：同句多个越界词只改一次
        by_sentence: dict[str, list[str]] = {}
        for row in targets:
            sent = str(row.get("sentence") or "").strip()
            if sent:
                by_sentence.setdefault(sent, []).append(str(row.get("term")))
        if not by_sentence:
            break
        for sentence, terms in by_sentence.items():
            if sentence not in out:
                continue
            try:
                rewritten = provider.chat(
                    _REWRITE_SYSTEM,
                    f"【原文句子】{sentence}\n【必须替换的词】{'、'.join(dict.fromkeys(terms))}\n"
                    "只输出改写后的这一句。",
                )
            except Exception as exc:  # 改写失败不影响正文落库
                logger.warning("era rewrite failed: %s", exc)
                continue
            rewritten = _first_line(rewritten)
            if not _rewrite_acceptable(sentence, rewritten, terms):
                changed.append(
                    {"before": sentence, "after": rewritten, "terms": terms, "kept": True}
                )
                continue
            out = out.replace(sentence, rewritten, 1)
            changed.append(
                {"before": sentence, "after": rewritten, "terms": terms, "kept": False}
            )
    return out, changed


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        line = line.strip().strip("`").strip()
        if line:
            return line
    return ""


def _rewrite_acceptable(before: str, after: str, terms: list[str]) -> bool:
    if not after or after == before:
        return False
    if any(term and term in after for term in terms):
        return False
    # 过度删改（短于原句 40%）一律拒收，宁可保留原句也不把语义改没。
    return len(after) >= max(6, int(len(before) * 0.4))


def apply_to_text(
    text: str,
    project,
    provider=None,
    *,
    segment_no: int = 0,
    reminders: dict | None = None,
) -> tuple[str, dict]:
    """生成后自检入口。默认只报告；只有显式 rewrite 模式才改正文。"""
    meta = scan_text(text, project, segment_no=segment_no, reminders=reminders)
    if meta.get("status") != STATUS_ACTIVE:
        # 没开 / 判不出基线：正文原样返回，meta 已带 status 供区分。
        return text, meta

    mode = str(getattr(settings, "era_guard_mode", "report") or "report").strip().lower()
    meta["mode"] = mode
    if mode != "rewrite" or not meta.get("counts"):
        return text, meta

    provider_name = type(provider).__name__ if provider is not None else ""
    if provider is None or provider_name == "LocalFallbackLLMProvider":
        # 离线兜底 provider 只会回显模板，重写必伤正文；明确记录跳过原因。
        meta["rewrite_skipped"] = "provider-unavailable"
        return text, meta

    guard = EraGuard(
        profile=project_profile(project),
        character_names=character_names(project, reminders),
    )
    rewritten, changes = _rewrite_sentences(
        text,
        guard,
        provider,
        rounds=int(getattr(settings, "era_guard_max_rounds", 2) or 2),
    )
    meta["rewrites"] = changes
    meta["rewrite_changed"] = rewritten != text
    if rewritten != text:
        # 改完复扫一遍，meta 里保留的是**最终正文**的结论。
        final = scan_text(rewritten, project, segment_no=segment_no, reminders=reminders)
        for key in ("counts", "violations", "mentions"):
            meta[key] = final.get(key)
    return rewritten, meta


__all__ = [
    "apply_to_text",
    "character_names",
    "constraint_for",
    "normalize_baseline",
    "project_profile",
    "scan_text",
    "world_baseline",
]
