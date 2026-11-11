# -*- coding: utf-8 -*-
"""文风一致性校验 · **第一阶段（L1 影子记录）**。

设计依据：`.trae/documents/style-consistency-check-design.md`。本模块只做三件事：

1. 用确定性算法量出核心 8 项指标（加上 12 项展示指标，不判界）；
2. 用**本项目已落库正文**的最近 N 段生成版本化基线（`style_baselines`）；
3. 每次生成成功后写一条 `style_checks` 记录（`verdict='shadow'`）。

## 本阶段明确不做

- 不产出 pass / warn / fail（写了就会让以后的误报率统计口径变脏）；
- 不改 `generations.text`、不重写、不阻断生成或保存；
- 不做 UI 明细页、不做 L2（LLM 判定）。

## 口径说明（与设计文档 §2.1 的一处差异，必须知道）

指标计算复用了 `app.style.stylometry.features()`——与 `projects.style_profile`
的参考带**同一个函数、同一套定义**。因此：

- 「短句占比」是 **句长 ≤ 4 字**（stylometry 的定义），不是设计文档正文里写的
  ≤ 10 字。文档写「≤10 字（与档案同口径）」，但档案实际是 ≤4；为了保证
  `|z|` 能和已存的参考带比较，这里以**档案实际口径**为准。
- 句长把句末标点算进句子长度（`stylometry.sentences()` 保留终结符），
  与档案一致；设计文档 §2.1 的「去标点」写法与档案不同。
- 虚词表取 `stylometry._FUNC_WORDS`（12 个），不是文档列出的 8 个。

调用 `features(..., unit_chars=0)` 得到**未做长度缩放**的原始值：档案里的
非密度特征在拟合时乘过 `exp(-|ln(unit_chars/3000)|*0.3)`，比较参考带时用
`_reference_scale()` 还原同一个系数，否则 z 值不可比。

## 失败纪律

`record_segment_check()` **永不抛异常**：任何错误都吞掉、回滚并写日志。
校验器故障绝不能让生成失败（生成链路只加了一次调用，见 engine._commit_segment）。
"""
from __future__ import annotations

import hashlib
import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select

from app.models import Generation, StyleBaseline, StyleCheck
from app.style import stylometry

logger = logging.getLogger(__name__)

#: 校验器版本：口径或算法变更时必须同步 +1。
CHECK_VERSION = "l1-shadow-v1"

#: 核心 8 项（唯一"有资格"进阈值的指标；本阶段只入库，不判界）。
CORE_METRICS: tuple[str, ...] = (
    "句长均值",
    "句长CV",
    "短句占比",
    "段均句数",
    "对白字占比",
    "逗号密度",
    "虚词密度",
    "四字格密度",
)

#: 展示项 = 其余 12 项，只记录不判界（零膨胀项在参考带里 p50=0，判界会恒亮）。
DISPLAY_METRICS: tuple[str, ...] = tuple(
    k for k in stylometry.FEATURES if k not in CORE_METRICS
)

#: 低于此字数不做判定（设计 §2.4：≤1200 字的单段不可判）。值仍然记录。
MIN_JUDGE_CHARS = 1200

#: 自身基线认为"充分"的最小段数（设计 §1.4）。达不到就写在基线行上。
MIN_SELF_SEGMENTS = 20

#: 基线窗口：用最近多少段（设计 §3.3）。
WINDOW_SEGMENTS = 30

#: 冷启动参考带取 short 桶（1500 字单元，n=800）。
REFERENCE_BUCKET = "short"

#: 参考带偏离口径：|z| ≥ 3 才写一句说明（设计 §3.2）。
REFERENCE_Z = 3.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 1. 指标
# --------------------------------------------------------------------------- #

def measure(text: str) -> dict[str, Any]:
    """量一段正文。**算不出的项给 None，不给 0**。

    Returns:
        {
          "chars": 去空白后的字数,
          "judgeable": 字数是否达到可判门槛,
          "core": {8 项: float | None},
          "display": {12 项: float | None},
          "missing": {指标: 原因} ∪ {"_check": 原因},
          "metric_scope": "stylometry.raw.v1",
        }
    """
    raw_text = text or ""
    chars = len("".join(raw_text.split()))
    result: dict[str, Any] = {
        "chars": chars,
        "judgeable": chars >= MIN_JUDGE_CHARS,
        "core": {k: None for k in CORE_METRICS},
        "display": {k: None for k in DISPLAY_METRICS},
        "missing": {},
        "metric_scope": "stylometry.raw.v1",
    }
    if chars == 0:
        result["missing"]["_check"] = "正文为空，无指标可算"
        return result

    paras = stylometry.paragraphs(raw_text)
    sents = [s for p in paras for s in stylometry.sentences(p)]
    vec = stylometry.features(raw_text, unit_chars=0)

    # 逐项判断"能不能算"——不能算的写 None，不留 0。
    computable = {k: True for k in stylometry.FEATURES}
    if not sents:
        for k in ("句长均值", "句长CV", "句长p90", "短句占比", "长句占比"):
            computable[k] = False
        result["missing"]["句长均值"] = "未切出句子"
        result["missing"]["短句占比"] = "未切出句子"
    elif len(sents) < 2:
        computable["句长CV"] = False
        result["missing"]["句长CV"] = f"只有 {len(sents)} 句，变异系数无定义"
    if not paras:
        computable["段均句数"] = False
        computable["段长CV"] = False
        computable["对白段占比"] = False
        result["missing"]["段均句数"] = "无自然段"

    for k in CORE_METRICS:
        result["core"][k] = float(vec[k]) if computable.get(k, True) else None
    for k in DISPLAY_METRICS:
        result["display"][k] = float(vec[k]) if computable.get(k, True) else None

    if not result["judgeable"]:
        result["missing"]["_check"] = (
            f"正文字数 {chars} < {MIN_JUDGE_CHARS}，单段不可判定（指标仍记录）"
        )
    return result


# --------------------------------------------------------------------------- #
# 2. 基线
# --------------------------------------------------------------------------- #

def _quantile(sorted_vals: list[float], q: float) -> float | None:
    """线性插值分位（与 numpy 默认口径一致）。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _summarize(values: Iterable[float]) -> dict[str, Any] | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    mean = statistics.fmean(vals)
    std = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return {
        "n": len(vals),
        "p1": _quantile(vals, 0.01),
        "p10": _quantile(vals, 0.10),
        "p50": _quantile(vals, 0.50),
        "p90": _quantile(vals, 0.90),
        "p99": _quantile(vals, 0.99),
        "mean": mean,
        "std": std,
    }


def _sample_rows(db, project_id: int, exclude_generation_id: int | None) -> list[Generation]:
    """本项目最近的已落库正文（按 id 倒序；排除正在校验的这一段，防自证）。"""
    stmt = (
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(Generation.id.desc())
        .limit(WINDOW_SEGMENTS)
    )
    if exclude_generation_id is not None:
        stmt = stmt.where(Generation.id != exclude_generation_id)
    rows = list(db.scalars(stmt).all())
    return [r for r in rows if (r.text or "").strip()]


def ensure_baseline(db, project_id: int,
                    exclude_generation_id: int | None = None) -> StyleBaseline | None:
    """取/建本项目自身基线。样本集没变就复用最新版本，变了才升版本号。

    Returns:
        StyleBaseline；本项目没有可用的已写正文时返回 None（不写检查记录）。
    """
    rows = _sample_rows(db, project_id, exclude_generation_id)
    if not rows:
        return None

    sample_ids = [r.id for r in rows]
    latest = db.scalars(
        select(StyleBaseline)
        .where(StyleBaseline.project_id == project_id)
        .order_by(StyleBaseline.baseline_version.desc())
        .limit(1)
    ).first()
    if latest is not None:
        latest_ids = (latest.metrics_json or {}).get("sample_generation_ids") or []
        # 精确相同（正常滚动），或本次样本只是最新基线的子集（回填校验一段
        # 已落库正文时，那一段本来就在基线里）——都复用最新版本，不升版本号。
        if sample_ids == latest_ids or set(sample_ids) <= set(latest_ids):
            return latest

    measured = [measure(r.text or "") for r in rows]
    core = {
        k: _summarize((m["core"].get(k) for m in measured)) for k in CORE_METRICS
    }
    segments = sorted(int(r.segment_no) for r in rows)
    source_range = (
        f"p{project_id} 段{segments[0]}–段{segments[-1]}"
        f"（{len(rows)} 段，最近 {WINDOW_SEGMENTS} 段窗口）"
    )
    insufficient = None
    if len(rows) < MIN_SELF_SEGMENTS:
        insufficient = (
            f"样本量不足：{len(rows)} 段 < {MIN_SELF_SEGMENTS} 段；"
            "分位只是冷启动初值，不得据此产出判定"
        )

    baseline = StyleBaseline(
        project_id=project_id,
        baseline_version=(latest.baseline_version + 1) if latest else 1,
        window_segments=WINDOW_SEGMENTS,
        sample_count=len(rows),
        source="self",
        source_range=source_range,
        insufficient_reason=insufficient,
        metrics_json={
            "core": core,
            "sample_generation_ids": sample_ids,
            "metric_scope": "stylometry.raw.v1",
        },
        created_at=_utcnow(),
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)
    return baseline


# --------------------------------------------------------------------------- #
# 3. 参考带偏离说明（只写说明，不产出判定）
# --------------------------------------------------------------------------- #

def _reference_scale(unit_chars: int | None) -> float:
    """还原档案拟合时对非密度特征乘的长度系数（与 stylometry 同一公式）。"""
    if not unit_chars or unit_chars <= 0:
        return 1.0
    return math.exp(-abs(math.log(unit_chars / 3000.0)) * 0.3)


def reference_deviation(project, metric: dict[str, Any]) -> dict[str, Any] | None:
    """相对参考带 `buckets.short` 的 |z| ≥ 3 偏离清单。

    没有 `style_profile` 的项目返回 None（不臆造参考带）。
    """
    profile = getattr(project, "style_profile", None) or {}
    bucket = (profile.get("buckets") or {}).get(REFERENCE_BUCKET) or {}
    feat = bucket.get("feat") or {}
    if not feat:
        return None

    unit_chars = bucket.get("unit_chars")
    scale = _reference_scale(unit_chars)
    deviations = []
    for k in CORE_METRICS:
        value = metric["core"].get(k)
        stats_ = feat.get(k) or {}
        std = float(stats_.get("std") or 0.0)
        mean = float(stats_.get("mean") or 0.0)
        if value is None or std <= 0:
            continue
        scaled = value * scale if k in stylometry.NON_DENSITY_FEATURES else value
        z = (scaled - mean) / std
        if abs(z) >= REFERENCE_Z:
            deviations.append({
                "metric": k,
                "z": round(z, 2),
                "value": round(value, 4),
                "band_mean": round(mean, 4),
                "band_std": round(std, 4),
            })
    return {
        "bucket": REFERENCE_BUCKET,
        "unit_chars": unit_chars,
        "scale": round(scale, 4),
        "z_threshold": REFERENCE_Z,
        "deviations": deviations,
        "disclaimer": "仅供参照，不作为判据",
    }


# --------------------------------------------------------------------------- #
# 4. 写记录
# --------------------------------------------------------------------------- #

def _note(metric: dict[str, Any]) -> str:
    if metric["chars"] == 0:
        return "这一段没有正文，未记录指标。"
    if not metric["judgeable"]:
        return (
            f"这一段太短（不足 {MIN_JUDGE_CHARS} 字），还不够判断风格是否一致；"
            "已经记录，等写满几段后再一起看。"
        )
    return "影子记录：已按本项目已写正文的分布记录本段指标，本阶段不作判定。"


def record_segment_check(db, project, generation: Generation) -> int | None:
    """生成成功后写一条影子记录。**永不抛异常**。

    调用点：`engine._commit_segment()` 的 `db.commit()` 之后（正文已落库）。
    校验失败只写日志，不影响生成结果——这是第一阶段的硬约束。
    """
    try:
        text = generation.text or ""
        metric = measure(text)
        baseline = ensure_baseline(
            db, project.id, exclude_generation_id=generation.id
        )
        if baseline is None:
            logger.info(
                "style shadow skipped: project=%s generation=%s 无已写正文可作基线",
                project.id, generation.id,
            )
            return None

        row = StyleCheck(
            project_id=project.id,
            scope="segment",
            generation_id=generation.id,
            agent_run_id=None,  # Writer 路径不写 agent_runs
            segment_no=generation.segment_no,
            chapter_range=generation.chapter_range,
            text_md5=hashlib.md5(text.encode("utf-8")).hexdigest(),
            check_version=CHECK_VERSION,
            baseline_id=baseline.id,
            baseline_version=baseline.baseline_version,
            verdict="shadow",  # 第一阶段：绝不写 pass/warn/fail
            metric_json={
                "chars": metric["chars"],
                "judgeable": metric["judgeable"],
                "core": metric["core"],
                "display": metric["display"],
                "metric_scope": metric["metric_scope"],
            },
            missing_json=metric["missing"] or None,
            evidence_json={
                "reference": reference_deviation(project, metric),
            },
            note=_note(metric),
            created_at=_utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    except Exception:  # noqa: BLE001 —— 校验器绝不能拖垮生成
        logger.warning("style shadow check failed (ignored)", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
