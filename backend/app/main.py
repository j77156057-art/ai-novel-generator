"""AI 小说生成器 · M1 数据层入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 启动时加载前端「模型设置」页持久化的 LLM 配置（覆盖 .env 默认值）
    with SessionLocal() as db:
        settings_router.load_persisted_llm_config(db)
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


@app.get("/health")
def health() -> dict:
    return {"ok": True}
