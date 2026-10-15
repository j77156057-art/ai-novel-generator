"""AI 小说生成器 · M1 数据层入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import logging

from app.db import init_db, SessionLocal
from app.routers import novels
from app.routers import projects
from app.routers import generation
from app.routers import wizard
from app.routers import settings as settings_router
from app.routers import assistant as assistant_router
from app.routers import assistant_proposals as assistant_proposals_router
from app.routers import assistant_audit as assistant_audit_router
from app.routers import assistant_generation as assistant_generation_router
from app.routers import assistant_clean as assistant_clean_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 启动时加载前端「模型设置」页持久化的 LLM 配置（覆盖 .env 默认值）
    with SessionLocal() as db:
        settings_router.load_persisted_llm_config(db)

    # —— 收割被中断的蒸馏后台任务 ——
    # 蒸馏跑在进程内的后台线程里（BackgroundTasks），服务重启/重载后线程没了、
    # 但它登记的 DistillRun 行还停在 running → 前端进度条永远转，且**零报错**。
    # 必须在 init_db() 之后（表要存在）、用独立 session（生命周期与请求无关）。
    # 放在自己的 try 里：收割失败不该拖垮整个启动（最坏结果是进度条又转起来）。
    try:
        from app.style.jobs import reap_stale_runs

        with SessionLocal() as db:
            reaped = reap_stale_runs(db)
        if reaped:
            logging.getLogger(__name__).info(
                "启动收割：%s 条蒸馏任务因服务重启被标记为失败", reaped
            )
    except Exception as exc:  # noqa: BLE001 —— 收割是增强，不是启动的前提
        logging.getLogger(__name__).warning("蒸馏任务启动收割失败（已忽略）：%s", exc)
    yield


app = FastAPI(title="AI 小说生成器 · M1–M4", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.include_router(novels.router)
app.include_router(projects.router)
app.include_router(generation.router)
app.include_router(wizard.router)
app.include_router(settings_router.router)
app.include_router(assistant_router.router)
# 第4 步：提案审批 / 应用 / 回滚。与 assistant_router 共用 /api/assistant 前缀，
# 两者路径无重叠（qa|diagnose vs proposals|versions|prompts）。
app.include_router(assistant_proposals_router.router)
# 第7 步：审核（audit-corpus|audit-kg|audit-generation）与调动生成
# （invoke-generation）。同样共用 /api/assistant 前缀，路径无重叠。
app.include_router(assistant_audit_router.router)
app.include_router(assistant_generation_router.router)
# 第10 步：语料正文清洗（clean-corpus —— 检测+模型复核，产出待批提案）。
app.include_router(assistant_clean_router.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
