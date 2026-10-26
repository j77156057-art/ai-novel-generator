"""Assistant 各路由共用的内部小工具（第 7 步从 `routers/assistant.py` 抽出）。

## 为什么单独成模块

`/qa`、`/diagnose`、`/runs`、`/analyze-logs`、`/audit-*`、`/invoke-generation`
这些端点共享同一套「开 AgentRun → 落 Evidence → 调 Assistant provider →
收尾」的骨架。骨架放在这里，端点文件只写自己的规则与提示词：

- **只保留一份真相**：Evidence 行的构造口径（snippet 截断、score 转 float）、
  request_hash 的算法、provider 的降级契约，各写一份必然漂移；
- **不循环依赖**：本模块只 import `app.*` 下层（agents / db / llm / models），
  不 import 任何 router —— `assistant.py` / `assistant_audit.py` /
  `assistant_generation.py` 都单向依赖它。

## 命名说明

函数名保留 `routers/assistant.py` 里的下划线前缀：它们是**包内**共享工具，
不是公开 API。既有测试按 `assistant_router._ensure_assistant_provider()`
  引用（`test_assistant_readonly.py`），抽取后经 `assistant.py` 的 re-export
仍然解析到同一处。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.llm.registry import ASSISTANT_ROLE, get_provider_registry
from app.models import Evidence, Project
from app.schemas import StrictRequest

logger = logging.getLogger("router.assistant")


# ====================================================================
# 时间 / 哈希 / 项目校验
# ====================================================================


def _now_utc() -> datetime:
    """统一的 UTC 时间源。

    全部写入路径都用它：`started_at` / `finished_at` 必须同源，否则
    「耗时」会出现负数（一边带 tzinfo、一边 naive 做减法直接抛
    TypeError，见 `app.agents.log_audit._as_utc` 的说明）。
    """
    return datetime.now(timezone.utc)


def _request_hash(body: StrictRequest) -> str:
    """稳定哈希：用于 AgentRun.request_hash 字段（同一请求多次重放可关联）。

    用 `model_dump(mode="json")` 而不是 `str(body)`：后者含对象地址，
    同一个请求两次构造哈希不同，重放关联就失效了。
    """
    payload = json.dumps(body.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_project(db: Session, project_id: int) -> Project:
    """project_id 必须存在；不存在 → 404（不静默按「空项目」继续）。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return proj


# ====================================================================
# Assistant provider（role 隔离的唯一入口）
# ====================================================================


def _ensure_assistant_provider():
    """按 role 取 Assistant provider；构造抛错时不污染缓存——异常原样冒泡。

    调用方（router 端点）应捕获异常并把它降级成「只读检索关闭」响应，
    不让 Writer 单例被波及。
    """
    return get_provider_registry().get(ASSISTANT_ROLE)


def _chat_with_assistant(*, system: str, user: str, fallback: str) -> tuple[str, str]:
    """Assistant provider 的统一调用入口（诊断 / 日志分析 / 审核共用）。

    契约（与第 3 步完全一致）：
    - 走 `get_provider_registry().get('assistant')`，Writer 单例不被触碰；
    - 构造/调用抛错 → 返回 `(fallback, "")`，**不**冒泡——模型侧故障
      只让解释性文字降级，规则结论照常返回；
    - `model_label` 为空串 = 「没过模型」，调用方记 `(local_fallback)`。
    """
    try:
        provider = _ensure_assistant_provider()
        model_label = (
            getattr(provider, "model", "") or getattr(provider, "label", "") or ""
        )
        raw = provider.chat(system, user)
        answer = (raw or "").strip() if isinstance(raw, str) else str(raw or "")
        return answer, model_label
    except Exception as exc:  # noqa: BLE001 - Assistant 失败仅降级文案
        logger.warning("assistant provider 失败：%s", exc)
        return fallback, ""


# ====================================================================
# Evidence 行 / 置信度
# ====================================================================


def _build_evidence_row(run_id: int, kind: str, source: str, ref_id: int | str,
                        snippet: str, score: float) -> Evidence:
    """把工具调用结果打包成 Evidence 行（只准备对象，commit 由 caller 控制）。"""
    return Evidence(
        run_id=run_id,
        kind=kind,
        source=source,
        ref_id=str(ref_id),
        snippet=(snippet or "")[:1000],  # 防爆库
        score=float(score),
    )


def _confidence_from_evidence(rows: list[Evidence]) -> float:
    """从 evidence 行数与平均 score 推导 confidence（启发式；非概率）。

    0.0 - 1.0：行数 >= 3 且均分 >= 0.5 → 0.8；行数 1 → 0.3；无 evidence → 0.1。
    """
    if not rows:
        return 0.1
    avg = sum(r.score for r in rows) / len(rows)
    base = min(1.0, max(0.0, avg))
    # 行数加成：<= 3 行 → 加 0.0；>= 5 行 → 加 0.2
    bonus = min(0.2, max(0.0, (len(rows) - 3) * 0.05))
    return round(min(1.0, base + bonus), 3)


def evidence_rows_to_dicts(rows: list[Evidence]) -> list[dict]:
    """Evidence 行 → JSON 安全出参（score 统一转 float）。

    SQLite 上 `Evidence.score` 是 String 列（见 models.py 注释），读回是
    字符串；出参转 float，调用方不该知道存储引擎的怪癖。
    """
    return [
        {
            "id": r.id,
            "kind": r.kind,
            "source": r.source,
            "ref_id": r.ref_id,
            "snippet": r.snippet,
            "score": float(r.score),
        }
        for r in rows
    ]


def persist_evidence(
    db: Session, run_id: int, report: Any
) -> list[Evidence]:
    """把规则报告的 evidence 落成 Evidence 行（按 dedup_key 去重）。

    `report` 是 `app.agents.diagnosis.DiagnosticReport`（不直接 import 是
    为了避免本模块与规则模块的双向依赖；这里只用 `iter_evidence` 约定的
    迭代协议）。两条 issue 常引用同一条证据，去重后 Evidence 表只留一行，
    响应里两条 issue 仍各自指向它。
    """
    from app.agents.diagnosis import iter_evidence

    rows: list[Evidence] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    for ev in iter_evidence(report):
        if ev.dedup_key in seen_keys:
            continue
        seen_keys.add(ev.dedup_key)
        row = _build_evidence_row(
            run_id, kind=ev.kind, source=ev.source, ref_id=ev.ref_id,
            snippet=ev.snippet, score=ev.score,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows
