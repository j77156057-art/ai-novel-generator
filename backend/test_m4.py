"""M4 分段生成环端到端验证（离线，服务层）。

流程：解析种子小说 → 切分写库 → build_graph → 注入 M3.3 约束 →
  generate_plan（预览）→ generate_next_segment（续写+总结+入库）→ generate_loop（链式续写）。

不依赖网络：embedding 走本地兜底，LLM 强制注入 LocalFallback 以保证确定性回显。
每次运行清空测试库，避免 stale 数据。
"""

import os
import sys
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "ai_novel_m4_test.db")
if os.path.exists(_DB):
    try:
        os.remove(_DB)
    except OSError:
        pass
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["LLM_PROVIDER"] = "local"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fixtures import FIXTURE  # noqa: E402
from app.db import Base, SessionLocal  # noqa: E402
from app.generation import build_context, generate_loop, generate_next_segment  # noqa: E402
from app.generation.prompts import build_system_prompt, build_user_prompt  # noqa: E402
from app.ingestion.chunk import chunk_text  # noqa: E402
from app.ingestion.parse_txt import parse_txt  # noqa: E402
from app.kg.build import build_graph  # noqa: E402
from app.kg.queries import get_reminders  # noqa: E402
from app.llm.provider import LocalFallbackLLMProvider  # noqa: E402
from app.models import Generation, NovelChunk, Project, RawNovel  # noqa: E402


def _seed(db) -> int:
    chapters = parse_txt(content=FIXTURE)
    novel = RawNovel(title="fixture", platform="seed", source_url="fixture://m4")
    db.add(novel)
    db.flush()
    for ch in chapters:
        for i, piece in enumerate(chunk_text(ch["body"], 1200, 200)):
            db.add(NovelChunk(novel_id=novel.id, chapter_no=ch["chapter_no"], idx=i, text=piece, embedding=None))
    db.commit()
    return novel.id


def main() -> int:
    Base.metadata.create_all(bind=SessionLocal().bind)
    db = SessionLocal()

    novel_id = _seed(db)
    project = Project(title="种子项目", mode="quick", target_length=5000,
                      outline=["林夜在旧宅发现一束来历不明的光", "苏晚携秘密登场", "真相在第三章揭开"])
    db.add(project)
    db.flush()
    project_id = project.id

    summary = build_graph(db, project_id, novel_id=novel_id)
    print("build_graph ->", summary)
    # 显式按来源查（P0-1 后 get_reminders 默认 source="generated"）
    reminders_ref = get_reminders(db, project_id, source=f"novel:{novel_id}")
    print("参考语料图 reminders ->", reminders_ref)

    provider = LocalFallbackLLMProvider()
    failures = []

    # —— A) generate_plan 预览（不调 LLM 之外的组装）——
    # 语义（M5 设计 + P0 修复）：图谱按来源隔离，参考语料是「风格样本」，
    # 其角色/坑**不得**注入本项目生成约束（前端原文：「各自成图，绝不混在一起」）。
    # 本项目自己的约束来自 **plot_bible（用户构思，最高权威）+ generated 图（闭环累积）** 的合并；
    # 本用例的项目既无 bible 又尚无生成内容，故两项都应为空。
    # （bible 那一路的合并口径与「provider 实收」验证见 test_p0_regression P0-2 / P0-4 段。）
    ctx = build_context(db, project, novel_ids=[novel_id])
    system = build_system_prompt(project.title, ctx["reminders"], project.style_dims)
    user = build_user_prompt(ctx["prior_summaries"], ctx["rag_context"], project.outline, 800)

    if not all(t in system for t in ("角色人设约束", "未填伏笔")):
        failures.append("system 提示缺少 角色/伏笔 约束区块")
    if "林夜" in system or "那光是什么" in system:
        failures.append("参考语料事实泄漏进 system 提示（P0-1 source 隔离失效）")
    if not all(t in user for t in ("前情提要", "检索上下文", "大纲")):
        failures.append("user 提示缺少 前情/检索/大纲 区块")
    chars = list(ctx["constraints"]["characters"])  # 角色名为字符串列表
    holes = list(ctx["constraints"]["plotholes"])  # 未填坑问题为字符串列表
    if chars or holes:
        failures.append(f"新项目（尚无生成内容）约束应为空，实际 chars={chars} holes={holes}")
    print("plan constraints ->", ctx["constraints"])

    # —— B) 续写单段（带 RAG）——
    r1 = generate_next_segment(db, project, novel_ids=[novel_id], provider=provider)
    print("segment 1 ->", {k: (v if k not in ("system_prompt", "user_prompt", "rag_context") else f"<{len(str(v))} chars>") for k, v in r1.items()})

    seg1 = db.scalars(__import__("sqlalchemy").select(Generation).where(Generation.project_id == project_id).order_by(Generation.segment_no)).all()
    if len(seg1) != 1:
        failures.append(f"应落库 1 段，实际 {len(seg1)}")
    if r1["segment_no"] != 1:
        failures.append(f"首段 segment_no 应为 1，实际 {r1['segment_no']}")
    # 闭环（P0-4）：本段生成后事实已增量并回 generated 图（不清空、upsert）
    if (r1.get("kg_update") or {}).get("merged") is not True:
        failures.append(f"段后图谱未进闭环，kg_update={r1.get('kg_update')}")
    # 注：旧版「正文回显 林夜/那光是什么」证明的是「参考语料约束注入生成环」语义，
    # 已被 M5 设计废弃（参考语料只做风格样本，图谱各自成图）——且该回显实际来自
    # RAG 片段而非约束。「约束进入生成环」的现行证明在 test_p0_regression（P0-3：
    # mock 真实 provider 断言 constraints 进入请求体）。
    # 段级总结写入 Project.summary 链
    if not isinstance(project.summary, list) or len(project.summary) != 1:
        failures.append(f"Project.summary 应为长度1的列表，实际 {project.summary}")
    # rag_context 应为列表（RAG 已参与）
    if not isinstance(r1["rag_context"], list):
        failures.append("rag_context 应为列表")

    # —— C) 多段链式续写 ——
    results = generate_loop(db, project, novel_ids=[novel_id], provider=provider, segments=2)
    print("loop segments ->", [r["segment_no"] for r in results])
    for r in results:
        if r["segment_no"] not in (2, 3):
            failures.append(f"loop 段号异常 {r['segment_no']}")
    # 第 3 段的前情应含前两段总结（链式注入）
    ctx3 = build_context(db, project, novel_ids=[novel_id])
    if len(ctx3["prior_summaries"]) != 3:
        failures.append(f"第3段前情链长度应为3，实际 {len(ctx3['prior_summaries'])}")

    total = db.scalars(__import__("sqlalchemy").select(Generation).where(Generation.project_id == project_id)).all()
    if len(total) != 3:
        failures.append(f"总落库段数应为3，实际 {len(total)}")

    db.close()

    if failures:
        print("\n❌ M4 验证失败：")
        for f in failures:
            print("  -", f)
        return 1
    print("\n✅ M4 分段生成环验证通过：约束隔离（参考语料不注入） / 闭环增量建图 / 续写落库 / 段级总结链 / 多段链式 全部符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
