"""Assistant 模型能力探测端点（只读，仅消耗模型额度）。

## 为什么需要这两个端点

用户问「这个模型能不能关思考」「并发该开多大」「为什么蒸馏这么慢」时，
助手需要**实测**而不是背文档。文档写的「并发 30」通常是并发数口径，
1500 token 的长 prompt 下差一个数量级——本项目实测：智谱短 prompt
8/8 全成功，长 prompt 并发 3 就 8/12 触发 429。

## 为什么进 Assistant 白名单是安全的

两个端点**不读写任何业务表**，唯一副作用是消耗模型额度：
- 不发 AgentRun / Evidence（没有业务数据要留痕）；
- 不改 settings / provider 配置（探测结果只返回给用户看，采纳与否由用户决定）；
- 不碰 Writer / Distiller 单例（直接用 settings 里的 key 发裸 HTTP）。

这是刻意设计：探测本身不该产生「系统状态」，否则「我探了一下」会变成
一次不可逆的配置变更。

## 与 registry 工具的关系

`app/agents/registry.py` 里也注册了同名工具（`probe_model_thinking` /
`probe_model_concurrency`），供未来 agent 自主调度时使用。两端点与工具
共用 `app.llm.capability` 的同一份实现——不复制判据。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents.registry import _resolve_probe_target
from app.db import get_db
from app.llm.capability import probe_concurrency, probe_thinking

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


# ====================================================================
# 请求体
# ====================================================================


class AssistantProbeThinkingRequest(BaseModel):
    """POST /api/assistant/probe-thinking 请求体。"""

    #: 项目 id 仅作审计锚点（探测不依赖项目数据），可省略
    project_id: int | None = None
    #: 显式指定渠道；不传则按 distiller → writer 顺序解析
    provider: str | None = None
    #: 显式指定模型；不传则用该渠道的默认模型
    model: str | None = None
    #: 探测 prompt 的输入长度。**不要调小**——短 prompt 测不出限流，
    #: 也测不出思考链对输出预算的消耗。
    in_tokens: int = Field(default=1500, ge=200, le=8000)
    #: 输出上限。太小会被思考链吃光导致正文为空（那正是要探测的现象）
    out_tokens: int = Field(default=500, ge=100, le=4000)


class AssistantProbeConcurrencyRequest(BaseModel):
    """POST /api/assistant/probe-concurrency 请求体。"""

    project_id: int | None = None
    provider: str | None = None
    model: str | None = None
    #: 要测的并发档位。默认 2/5/10——够判断量级，又不会烧掉几十次调用。
    levels: list[int] | None = None
    #: 每档请求数。少于 8 结果不可信（单次波动会有 ±1 的误差）
    requests_per_level: int = Field(default=8, ge=4, le=40)
    in_tokens: int = Field(default=1500, ge=200, le=8000)
    out_tokens: int = Field(default=500, ge=100, le=4000)
    #: 已探测出的关闭思考参数（JSON），例如 {"thinking":{"type":"disabled"}}。
    #: 不传则按模型默认行为测——若思考开着，吞吐会被推理拖低，结论偏保守。
    think_off: dict | None = None


# ====================================================================
# 端点
# ====================================================================


@router.post("/probe-thinking")
def assistant_probe_thinking(
    body: AssistantProbeThinkingRequest, db: Session = Depends(get_db)
) -> dict:
    """实测目标模型能否关闭思考链。

    逐个试 6 种参数（thinking=disabled / reasoning_effort=none /
    enable_thinking=false / chat_template_kwargs 等），返回：
    - `verdict`: supported / unsupported / baseline_failed / not_configured
    - `baseline`: 基线延迟、正文长度、推理长度、是否在思考
    - `strategies[]`: 每个策略的实测结果
    - `recommended`: 最优参数（含可直接抄进配置的 JSON）
    - `note`: 人话解释

    **不要按参数名推断**：实测里 `reasoning_effort="low"` 反而让推理变长
    （基线 96 字 → 104），`minimal` 更差（174 字）。
    """
    target = _resolve_probe_target(db, body.project_id or 0, body.provider, body.model)
    if not target["api_key"]:
        return {
            "verdict": "not_configured",
            "target": target,
            "note": (f"渠道 {target['provider']!r} 没配 api_key（来源 {target['source']}）。"
                     "先在「模型设置」里填好 key，或显式传 provider/model。"),
        }
    result = probe_thinking(
        target["base_url"], target["api_key"], target["model"],
        in_tokens=body.in_tokens, out_tokens=body.out_tokens)
    result["target"] = target
    return result


@router.post("/probe-concurrency")
def assistant_probe_concurrency(
    body: AssistantProbeConcurrencyRequest, db: Session = Depends(get_db)
) -> dict:
    """实测目标模型的并发上限。

    逐档加压（默认 2/5/10），每档发 `requests_per_level` 个请求，返回：
    - `verdict`: ok / no_full_success / not_configured
    - `rows[]`: 每档的成功数、耗时、吞吐、TPM、P50 延迟、错误码
    - `recommended`: 100% 成功档位里吞吐最高的那档
    - `note`: 人话解释 + 建议写入的 max_concurrent_hint

    **取「100% 成功里吞吐最高」而不是「吞吐最高」**：过载点的吞吐数字
    看着漂亮，但那只是「成功数 / 总耗时」的假象，实际一半请求在 429 重试。
    """
    target = _resolve_probe_target(db, body.project_id or 0, body.provider, body.model)
    if not target["api_key"]:
        return {
            "verdict": "not_configured",
            "target": target,
            "note": (f"渠道 {target['provider']!r} 没配 api_key（来源 {target['source']}）。"
                     "先在「模型设置」里填好 key，或显式传 provider/model。"),
        }
    result = probe_concurrency(
        target["base_url"], target["api_key"], target["model"],
        levels=body.levels, requests_per_level=body.requests_per_level,
        in_tokens=body.in_tokens, out_tokens=body.out_tokens,
        think_off=body.think_off)
    result["target"] = target
    return result
