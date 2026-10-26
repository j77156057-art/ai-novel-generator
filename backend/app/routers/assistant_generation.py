"""Assistant 调动生成路由（第 7 步交付 4）：POST /api/assistant/invoke-generation。

## 这是全项目里唯一一个「Assistant 触发写业务表」的端点

助手默认**只能读**（qa / diagnose / runs / analyze-logs / audit-* 全部只读），
改正文/改图谱/改设定一律走 EditProposal 审批（ADR 第 4 步）。本端点是与
审批流并行的唯一例外：**用户显式指令助手去生成**，生成动作本身复用 Writer
的生成引擎（`app.generation.engine`），不另起一套写入逻辑。

三条硬约束（缺一不可，全部有测试守着）：

1. **显式指令**：请求体必须带非空 `instruction`（用户原话/意图，如
   「继续写下一段，写主角夜探祠堂」）。没有它 → 422。这样「生成」在
   API 层就是一次可追溯的用户动作，而不是任何内部逻辑顺带触发的副作用；
2. **复用引擎**：`generate_next` 走 `generate_next_segment`，
   `regenerate_segment` 走 `regenerate_segment_stream`（内部消费完流式
   事件再统一出参）。本文件**不**直接构造/写入 `Generation`——绕过引擎
   就意味着绕过角色锁定、未填坑注入、摘要树与图谱闭环，那是基线
   b58c763 的红线；
3. **留痕**：每次调用落一条 `role='assistant'` 的 AgentRun，`note` 写明
   「这次生成是助手调用的」，`model` 回显 **Writer 实际使用的模型**——
   事后翻审计表能回答「这段正文是谁、用什么模型写出来的」。

## 与 Writer 单例的关系

本端点**不**创建也不修改 Writer provider：引擎内部自己调
`get_llm_provider()`（与 POST /api/generate 完全同一条路径），这里只在
生成结束后读一次单例回显模型名。Writer 超时（chat 120s / stream 180s，
556c057 / c1a7179）同样原样继承——本文件不传自定义 provider。

## 工具注册表

`AssistantToolRegistry` **不注册**任何直写工具（write_*/commit_* 前缀
黑名单 + 只读白名单，见 `app.agents.registry`）。助手「调动生成」靠的是
这个受审计的 HTTP 端点，不是工具表里的后门。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.generation.engine import generate_next_segment, regenerate_segment_stream
from app.models import AgentRun, Evidence, Generation, Project
from app.routers.assistant_shared import (
    _build_evidence_row,
    _now_utc,
    _request_hash,
    _require_project,
)
from app.schemas import StrictRequest

logger = logging.getLogger("router.assistant_generation")

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

#: 助手可调用的两种生成动作。`Literal` + StrictRequest：传别的值 → 422，
#: 不静默按默认动作执行（「用户说要重写第 3 段、结果续写了一段」比报错糟）。
AssistantGenerationAction = Literal["generate_next", "regenerate_segment"]


# ====================================================================
# 请求体
# ====================================================================


class AssistantInvokeGenerationRequest(StrictRequest):
    """POST /api/assistant/invoke-generation 请求体。

    字段分三组：

    - **定位**：`project_id`；
    - **动作**：`action`（generate_next | regenerate_segment）、
      `segment_no`（仅 regenerate 必填）、`max_chars`（缺省用
      `project.generation_length`，与 /api/generate 同口径）；
    - **指令**：`instruction`（用户显式指令原文，非空）——这是「这次
      生成是用户要助手干的」的结构化证据，进 AgentRun 审计链。

    刻意**不**暴露 `novel_ids`：参考语料范围取项目自身配置
    （`project.reference_novel_ids`）。助手调动生成时改检索范围属于
    「顺带改生成配置」，不在本次授权里。
    """

    project_id: int
    action: AssistantGenerationAction
    #: 仅 regenerate_segment 必填；generate_next 必须不传（见 _validate）。
    segment_no: int | None = None
    #: 缺省 None → 引擎用 project.generation_length（不在这里复制默认值，
    #: 复制出来的第二套默认一定会和第一套漂移）。
    max_chars: int | None = None
    #: 用户显式指令（非空）。空串/纯空白 → 422。
    instruction: str

    @model_validator(mode="after")
    def _validate_action_fields(self) -> "AssistantInvokeGenerationRequest":
        """动作与字段的配套校验（fail loud，不静默忽略不匹配的字段）。"""
        if self.action == "regenerate_segment":
            if self.segment_no is None:
                raise ValueError("regenerate_segment 必须带 segment_no")
            if self.segment_no <= 0:
                raise ValueError("segment_no 必须为正整数")
        elif self.segment_no is not None:
            # generate_next 带 segment_no = 调用方对语义有误解。按默认
            # 动作执行会让他以为「指定段被续写了」，不如直接拒。
            raise ValueError("generate_next 不接受 segment_no（段号由引擎分配）")
        if not (self.instruction or "").strip():
            raise ValueError("instruction 不能为空：助手生成必须是显式用户指令")
        return self


# ====================================================================
# 内部小工具
# ====================================================================


def _writer_model_label() -> str:
    """Writer 单例实际使用的模型名（响应与 AgentRun.model 回显用）。

    生成结束后才读：引擎内部自己 `get_llm_provider()`，此时单例一定
    已构造且就是刚才那位。读不到（未配置 key 等）→ `(unknown)`；
    本地兜底 provider 没有 model/label 属性 → `(local_fallback)`——
    两种都不是正常生产路径，必须让看响应的人一眼看出不对劲。
    """
    from app.llm.provider import get_llm_provider

    try:
        provider = get_llm_provider()
    except Exception as exc:  # noqa: BLE001 - 回显失败不该把已完成的生成判失败
        logger.warning("invoke-generation 读取 Writer 模型名失败：%s", exc)
        return "(unknown)"
    label = getattr(provider, "model", "") or getattr(provider, "label", "") or ""
    return label or "(local_fallback)"


def _require_segment(db: Session, project_id: int, segment_no: int) -> Generation:
    """重生成目标段必须存在；不存在 → 404（不留半开的 AgentRun）。"""
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project_id, Generation.segment_no == segment_no
        )
    ).first()
    if gen is None:
        raise HTTPException(
            status_code=404, detail=f"段 {segment_no} 不存在，无法重新生成"
        )
    return gen


def _record_generation_evidence(
    db: Session, run_id: int, gen: Generation, text: str
) -> Evidence:
    """把「这段正文是这次助手调用产出的」落成 Evidence 行。

    kind="generation" 与诊断/问答的证据同族——助手日志详情页因此能
    顺着 Evidence 直接跳到生成段，不用再猜是哪一段。
    """
    row = _build_evidence_row(
        run_id, kind="generation", source="generated",
        ref_id=gen.id, snippet=(text or "")[:200], score=1.0,
    )
    db.add(row)
    db.flush()
    return row


# ====================================================================
# POST /api/assistant/invoke-generation
# ====================================================================


@router.post("/invoke-generation")
def assistant_invoke_generation(
    body: AssistantInvokeGenerationRequest, db: Session = Depends(get_db)
) -> dict:
    """助手调动生成（**唯一**允许助手触发写业务表的端点；显式用户指令）。

    执行顺序：

    1. 校验项目（404）与目标段（regenerate 时 404）；
    2. 开 AgentRun（role=assistant，note=「这次生成是助手调用的」）；
    3. 调 `app.generation.engine`——角色锁定 / 未填坑注入 / 摘要树 /
       图谱闭环全部走既有链路，本端点不碰 Generation 写入；
    4. 落一条 generation Evidence 指向产出的段；
    5. 收尾 AgentRun：`model` 回显 Writer 实际模型，`note` 保留调用
       性质说明；失败 → run 记 failed 并返回 5xx（生成是用户明确要的
       结果，不降级成「空响应但 200」）。

    `instruction` 原样进 AgentRun.request_hash 与响应 `answer`，让
    「助手为什么写了这一段」在审计链里可回答。
    """
    project = _require_project(db, body.project_id)
    if body.action == "regenerate_segment":
        # 先校验再开 run：目标段不存在时不留一条空跑的审计记录
        _require_segment(db, project.id, body.segment_no)

    run = AgentRun(
        project_id=project.id,
        role="assistant",
        status="running",
        started_at=_now_utc(),
        finished_at=None,
        request_hash=_request_hash(body),
        model="",
        note=(
            f"这次生成是助手调用的（action={body.action}"
            + (f", segment_no={body.segment_no}" if body.segment_no else "")
            + f"）：{body.instruction.strip()[:120]}"
        ),
    )
    db.add(run)
    db.flush()
    run_id = run.id

    writer_model = ""
    try:
        if body.action == "generate_next":
            result = generate_next_segment(
                db, project, max_chars=body.max_chars
            )
            gen = db.scalars(
                select(Generation)
                .where(
                    Generation.project_id == project.id,
                    Generation.segment_no == result["segment_no"],
                )
            ).first()
            if gen is None:  # 引擎承诺落库；真没有就是引擎坏了，不伪装成功
                raise RuntimeError("引擎未返回已落库的生成段")
            _record_generation_evidence(db, run_id, gen, result.get("text") or "")
            writer_model = _writer_model_label()
            run.status = "success"
            run.finished_at = _now_utc()
            run.model = writer_model
            db.commit()
            return {
                "answer": (
                    f"已按你的指令续写第 {result['segment_no']} 段"
                    f"（{len(result.get('text') or '')} 字，"
                    f"Writer 模型 {writer_model}）。"
                ),
                "agent_run_id": run_id,
                "action": body.action,
                "writer_model": writer_model,
                "segment": _segment_out(gen),
                "chars": len(result.get("text") or ""),
                "summary": result.get("summary") or "",
                "kg_update": result.get("kg_update") or {},
                "summary_tree": result.get("summary_tree") or {},
                "debug": {
                    "system_prompt": result.get("system_prompt") or "",
                    "user_prompt": result.get("user_prompt") or "",
                    "constraints": result.get("constraints") or {},
                    "rag_context": result.get("rag_context") or [],
                },
            }

        # ---- regenerate_segment：消费引擎的流式生成器（不另起写入）----
        pieces: list[str] = []
        done: dict[str, Any] = {}
        for event in regenerate_segment_stream(
            db, project, body.segment_no, max_chars=body.max_chars
        ):
            if event.get("type") == "chunk":
                pieces.append(event.get("text") or "")
            elif event.get("type") == "done":
                done = event
        if not done:
            raise RuntimeError("重生成流式结束但没有产出 done 事件")
        text = "".join(pieces)
        gen_id = done.get("id")
        gen = db.get(Generation, gen_id) if gen_id else None
        if gen is None:
            # 引擎的 done 事件带回了落库段 id；取不到只能是引擎/库出了问题，
            # 判失败而不伪装成功（「响应说重写了、库里还是旧文」最坏）。
            raise RuntimeError("重生成结束但未能取回目标段")
        _record_generation_evidence(db, run_id, gen, text)
        writer_model = _writer_model_label()
        run.status = "success"
        run.finished_at = _now_utc()
        run.model = writer_model
        db.commit()
        return {
            "answer": (
                f"已按你的指令重写第 {body.segment_no} 段"
                f"（{len(text)} 字，Writer 模型 {writer_model}）。"
            ),
            "agent_run_id": run_id,
            "action": body.action,
            "writer_model": writer_model,
            "segment": _segment_out(gen),
            "chars": len(text),
            "summary": done.get("summary") or "",
            "summary_tree": done.get("summary_tree") or {},
        }
    except Exception as exc:  # noqa: BLE001 - 生成失败必须留痕，不静默 200
        logger.warning("invoke-generation 失败（project=%s）：%s", project.id, exc)
        run.status = "failed"
        run.finished_at = _now_utc()
        run.model = writer_model or "(unknown)"
        run.note = f"这次生成是助手调用的（失败）：{str(exc)[:120]}"
        db.commit()
        raise HTTPException(
            status_code=500, detail=f"助手调动生成失败：{str(exc)[:200]}"
        )


def _segment_out(gen: Generation) -> dict:
    """生成段出参（与 GET /api/generations/{pid} 的形状一致，前端复用）。"""
    return {
        "id": gen.id,
        "project_id": gen.project_id,
        "segment_no": gen.segment_no,
        "chapter_range": gen.chapter_range,
        "text": gen.text,
        "summary": gen.summary,
    }
