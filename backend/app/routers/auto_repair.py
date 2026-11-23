# -*- coding: utf-8 -*-
"""自动返修：确定性修复 → 诊断 → 模型定向重写 → 验收 → 回滚/落库。

这是「让平台自己修」的编排层，不重复实现任何既有能力：

- 确定性修复：``app.style.deterministic_repair``（标点 / 双人同段 / 引号）；
- 诊断与指令：``app.style.repair.build_repair_plan``（含硬伤 + 文风指标）；
- 模型重写：``/api/assistant/invoke-generation`` 的服务函数（快照 / 回滚 /
  AgentRun 审计 / KG 重建都在那边）；
- 验收：``app.style.repair_accept.evaluate_repair``，不达标就回滚到改前。

所有步骤都按「确定性优先、模型其次、验收兜底」执行；确定性修复失败不影响
正文，模型失败只回传错误、已完成的确定性修复仍然保留。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.generation.repetition import (
    analyze_length,
    analyze_repetition,
    analyze_summary_style,
    quality_payload,
)
from app.models import Generation, Project
from app.schemas import StrictRequest
from app.style.consistency import active_gate_limits, generation_gate
from app.style.deterministic_repair import deterministic_repair
from app.style.repair import build_repair_plan
from app.style.repair_accept import evaluate_repair, text_snapshot

logger = logging.getLogger("router.auto_repair")

router = APIRouter(prefix="/api/projects", tags=["auto-repair"])


class AutoRepairBody(StrictRequest):
    """自动返修请求。

    - ``apply_deterministic``：是否先落确定性修复（标点/拆段/引号）；
    - ``use_model``：确定性修复后仍有问题（口头禅/数字/文风偏离）时，
      是否调模型做定向重写。两项都关 = 只体检不动正文。
    """

    apply_deterministic: bool = True
    use_model: bool = True


def _max_chars(project: Project) -> int:
    value = getattr(project, "generation_length", None)
    try:
        return int(value) if value else 4000
    except (TypeError, ValueError):
        return 4000


def _stamp_quality(
    target: Generation,
    text: str,
    max_chars: int,
    extra: dict,
    gate_limits: dict[str, dict[str, float]] | None = None,
) -> None:
    """确定性路径没有经过引擎，这里补一份与引擎同口径的质量结论。"""
    quality = quality_payload(
        analyze_repetition(text),
        length=analyze_length(text, max_chars, is_rewrite=True),
        summary_style=analyze_summary_style(text),
    )
    quality["style_gate"] = generation_gate(
        text, max_chars, gate_limits=gate_limits
    )
    quality["auto_repair"] = extra
    target.quality_json = json.dumps(quality, ensure_ascii=False)


@router.post("/{project_id}/segments/{segment_no}/auto-repair")
def auto_repair_segment(
    project_id: int,
    segment_no: int,
    body: AutoRepairBody | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """一键自动返修一段正文。"""
    body = body or AutoRepairBody()
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")

    target = db.scalars(
        select(Generation).where(
            Generation.project_id == project_id,
            Generation.segment_no == segment_no,
        )
    ).first()
    if target is None:
        raise HTTPException(status_code=404, detail=f"段 {segment_no} 不存在")

    max_chars = _max_chars(project)
    # 与生成链路同一套判罚带：有参考语料就现推，没有退回常量带。
    try:
        from app.generation.engine import _apply_gate_limits

        gate_limits = (_apply_gate_limits(db, project, seg_no=segment_no) or {}).get(
            "chapter_limits"
        )
    except Exception as exc:  # noqa: BLE001 —— 推导失败不阻断返修
        logger.warning("自动返修推导闸门带失败，用当前生效值：%s", exc)
        gate_limits = None
    if not gate_limits:
        gate_limits = active_gate_limits()
    before_text = target.text or ""
    before = text_snapshot(before_text, max_chars, gate_limits)

    # ---- 1. 确定性修复（不调模型）----
    det_text, det_info = deterministic_repair(before_text)
    deterministic_applied = False
    if det_info.get("changed") and body.apply_deterministic:
        target.text = det_text
        # 旧质量结论是对旧正文算的，先清空；下面会按最终正文重写。
        target.quality_json = None
        db.commit()
        db.refresh(target)
        deterministic_applied = True
    base_text = det_text if deterministic_applied else before_text

    # ---- 2. 诊断（确定性修复之后再诊断，已修掉的不会重复下发）----
    plan = build_repair_plan(
        base_text,
        project.style_profile if isinstance(project.style_profile, dict) else None,
        project.style_narrative if isinstance(project.style_narrative, dict) else None,
    )

    # ---- 3. 模型定向重写（只改命中的问题）----
    model_info: dict | None = None
    acceptance: dict | None = None
    rolled_back = False
    after_text = base_text
    keep_engine_quality = False

    if body.use_model and plan.get("ok") and plan.get("instruction"):
        from app.routers.assistant_generation import (
            AssistantInvokeGenerationRequest,
            assistant_invoke_generation,
        )

        request = AssistantInvokeGenerationRequest(
            project_id=project_id,
            action="regenerate_segment",
            segment_no=segment_no,
            max_chars=max_chars,
            instruction=plan["instruction"],
        )
        try:
            result = assistant_invoke_generation(request, db)
            if not isinstance(result, dict):
                model_info = {
                    "used": False,
                    "error": "返修指令被判为局部改写，需要补充修改范围，未执行",
                }
            elif result.get("status") == "needs_clarification":
                model_info = {
                    "used": False,
                    "error": "返修指令被判为局部改写，需要补充修改范围，未执行",
                }
            else:
                db.refresh(target)
                after_text = target.text or ""
                acceptance = evaluate_repair(
                    base_text, after_text, max_chars, gate_limits
                )
                model_info = {
                    "used": True,
                    "snapshot_id": result.get("snapshot_id"),
                    "chars": result.get("chars"),
                    "answer": result.get("answer"),
                }
                if acceptance["ok"]:
                    keep_engine_quality = True
                elif result.get("snapshot_id"):
                    # 模型改坏了：退回确定性修复后的版本，保留机械修复成果。
                    from app.agents.dialog.rollback import rollback_snapshot

                    try:
                        rollback_snapshot(
                            db, project, int(result["snapshot_id"]), mode="text_only"
                        )
                        db.refresh(target)
                        after_text = target.text or ""
                        rolled_back = True
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("自动返修回滚失败：%s", exc)
                        model_info["rollback_error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 —— 模型失败不推翻已完成的确定性修复
            logger.warning("自动返修模型重写失败：%s", exc)
            model_info = {"used": False, "error": str(exc)}

    # ---- 4. 收尾：质量结论与最终正文同源 ----
    if not keep_engine_quality:
        db.refresh(target)
        _stamp_quality(
            target,
            target.text or base_text,
            max_chars,
            {
                "deterministic": det_info,
                "deterministic_applied": deterministic_applied,
                "model": model_info,
                "acceptance": acceptance,
                "rolled_back": rolled_back,
            },
            gate_limits,
        )
        db.commit()
        db.refresh(target)
        after_text = target.text or base_text

    after = text_snapshot(after_text, max_chars, gate_limits)
    return {
        "ok": True,
        "project_id": project_id,
        "segment_no": segment_no,
        "generation_id": target.id,
        "changed": after_text != before_text,
        "deterministic_applied": deterministic_applied,
        "before": before,
        "after": after,
        "deterministic": det_info,
        "plan": {
            "ok": bool(plan.get("ok")),
            "summary": plan.get("summary"),
            "reason": plan.get("reason"),
            "issues": plan.get("issues") or [],
        },
        "model": model_info,
        "acceptance": acceptance,
        "rolled_back": rolled_back,
        "text": after_text,
    }
