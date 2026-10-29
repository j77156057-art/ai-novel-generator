"""AI 小说生成器 · M1 数据层入口。"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# —— 日志级别（2026-10-29）——
# 本应用**从未配置过日志** ⇒ 根 logger 停在默认的 WARNING，
# 所有 `info` 全被吞掉。后果是启动时的三个「留痕」动作**实际无声**：
#   蒸馏收割 / KG 批抽收割 / 伏笔脏回收章清理
# 实测：清理真的跑了（库里 108 条脏值清了），但启动日志里**一个字都没有**
# ⇒ 与「清理必须留痕」的纪律相悖。
# 默认开 INFO（全仓 info 只有 20 余处，量很小）；要静默就把 LOG_LEVEL 设成 WARNING。
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

from app.db import init_db, SessionLocal  # noqa: E402
from app.routers import novels  # noqa: E402
from app.routers import projects  # noqa: E402
from app.routers import generation  # noqa: E402
from app.routers import wizard  # noqa: E402
from app.routers import settings as settings_router  # noqa: E402
from app.routers import assistant as assistant_router  # noqa: E402
from app.routers import assistant_proposals as assistant_proposals_router  # noqa: E402
from app.routers import assistant_audit as assistant_audit_router  # noqa: E402
from app.routers import assistant_probe as assistant_probe_router  # noqa: E402
from app.routers import assistant_generation as assistant_generation_router  # noqa: E402
from app.routers import assistant_clean as assistant_clean_router  # noqa: E402
from app.routers import kg_v3 as kg_v3_router  # noqa: E402
from app.routers import roleplay as roleplay_router  # noqa: E402


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

    # —— 收割被中断的 **KG 批抽**后台任务 ——
    # 批抽同样跑在进程内线程里（队列在内存 deque），服务重启后线程没了、
    # 队列没了，但它登记的 AgentRun(role=kg_batch_extract) 行还停在 running
    # ⇒ Worker 页永远显示「在抽」（实测积压 30 条，最早一条是 8 天前）。
    # 蒸馏那边有 reap_stale_runs，批抽这边原先漏了 ⇒ 同样的模式补上。
    try:
        from app.kg.batch_worker import reap_stale_runs as reap_kg_runs

        with SessionLocal() as db:
            reaped = reap_kg_runs(db)
        if reaped:
            logging.getLogger(__name__).info(
                "启动收割：%s 条 KG 批抽任务因服务重启被标记为失败", reaped
            )
    except Exception as exc:  # noqa: BLE001 —— 收割是增强，不是启动的前提
        logging.getLogger(__name__).warning("KG 批抽启动收割失败（已忽略）：%s", exc)

    # —— 清理伏笔的**脏回收章**（2026-10-29）——
    # 老账本里有一批**编造的**「计划回收章」（实测 140 条里 108 条：`0`/`50`/`30`/`12`）：
    # - `pf=0` ⇒ judge 报「计划第 0 章回收，已超期 9 章」——**83 条假告警**；
    # - `pf=50/30/12` ⇒ 界面上显示「计划第 50 章回收」这种**假计划**。
    # 判据是**确定性**的（数值不变式 + 正文证据，零误伤），置空的方向是
    # 「不知道」而不是「猜一个数」⇒ 可逆、零损失 ⇒ 启动时自动清 + 记条数。
    # 放在自己的 try 里：清理失败不该拖垮启动（最坏是界面还显示旧值）。
    try:
        from app.kg.foreshadow_cleanup import clean_all_foreshadows

        with SessionLocal() as db:
            st = clean_all_foreshadows(db)
        if st.get("cleaned"):
            logging.getLogger(__name__).info(
                "启动清理：%d 条伏笔的回收章无正文证据，已置空（保留 %d 条合法值）",
                st["cleaned"], st["kept"],
            )
    except Exception as exc:  # noqa: BLE001 —— 清理是增强，不是启动的前提
        logging.getLogger(__name__).warning("伏笔回收章启动清理失败（已忽略）：%s", exc)
    yield


app = FastAPI(title="AI 小说生成器 · M1–M4", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.include_router(novels.router)
app.include_router(projects.router)
app.include_router(generation.router)
app.include_router(kg_v3_router.router)
app.include_router(roleplay_router.router)
app.include_router(wizard.router)
app.include_router(settings_router.router)
app.include_router(assistant_router.router)
# 第4 步：提案审批 / 应用 / 回滚。与 assistant_router 共用 /api/assistant 前缀，
# 两者路径无重叠（qa|diagnose vs proposals|versions|prompts）。
app.include_router(assistant_proposals_router.router)
# 动作提案（agent 提「做什么事」，人批了才执行）：与文本提案分开一张表、
# 一套端点，路径无重叠（action-proposals vs proposals）。
from app.routers import assistant_action_proposals as assistant_action_proposals_router
app.include_router(assistant_action_proposals_router.router)
# 第7 步：审核（audit-corpus|audit-kg|audit-generation）与调动生成
# （invoke-generation）。同样共用 /api/assistant 前缀，路径无重叠。
app.include_router(assistant_audit_router.router)
app.include_router(assistant_probe_router.router)
app.include_router(assistant_generation_router.router)
# 第10 步：语料正文清洗（clean-corpus —— 检测+模型复核，产出待批提案）。
app.include_router(assistant_clean_router.router)
from app.routers import assistant_attachments
app.include_router(assistant_attachments.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
