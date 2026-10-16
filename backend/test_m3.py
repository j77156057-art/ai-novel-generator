"""M3 知识图谱端到端验证（离线）。

流程：解析种子小说 → 切分 → 写 raw_novels/novel_chunks → build_graph → 查询断言。
不依赖网络（embedding 走本地兜底）。每次运行清空测试库，避免 stale 数据。
"""

import os
import sys
import tempfile

# 必须在 import app 之前设定，确保走 sqlite 且离线
_DB = os.path.join(tempfile.gettempdir(), "ai_novel_m3_test.db")
if os.path.exists(_DB):
    try:
        os.remove(_DB)
    except OSError:
        pass
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["EMBEDDING_PROVIDER"] = "local"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fixtures import FIXTURE  # noqa: E402  测试 fixture 内嵌，不依赖语料目录

from sqlalchemy import create_engine, delete

from app.db import Base, SessionLocal
from app.ingestion.parse_txt import parse_txt
from app.ingestion.chunk import chunk_text
from app.kg.build import build_graph
from app.kg.queries import get_graph, get_character, get_reminders
from app.models import KgEdge, KgNode, NovelChunk, Project, RawNovel


def main() -> int:
    # 干净库
    eng = create_engine(f"sqlite:///{_DB}", connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(bind=eng)
    db = SessionLocal()

    # 灌种子（直接构造分片，跳过 M2 embedding）；fixture 内嵌，不依赖语料目录
    chapters = parse_txt(content=FIXTURE)
    assert chapters, "parse_txt 未产出章节"

    novel = RawNovel(title="fixture", platform="seed", source_url="fixture://m3")
    db.add(novel)
    db.flush()
    for ch in chapters:
        for i, piece in enumerate(
            chunk_text(ch["body"], 1200, 200)
        ):
            db.add(
                NovelChunk(
                    novel_id=novel.id,
                    chapter_no=ch["chapter_no"],
                    idx=i,
                    text=piece,
                    embedding=None,
                )
            )
    db.commit()

    project = Project(title="种子项目", mode="quick", target_length=5000)
    db.add(project)
    db.flush()
    project_id = project.id

    # 建图
    summary = build_graph(db, project_id, novel_id=novel.id)
    print("build_graph ->", summary)

    # 查询
    graph = get_graph(db, project_id)
    char_names = sorted(n["props"]["name"] for n in graph["nodes"] if n["type"] == "character")
    print("characters ->", char_names)

    lin = get_character(db, project_id, "林夜")
    print("林夜 ->", lin)

    wan = get_character(db, project_id, "苏晚")
    print("苏晚 ->", wan)

    # P0-1 修复后 get_reminders 必须显式指定 source：生成链只读 generated 图，
    # 参考语料图按 novel:{id} 查询（图谱按来源隔离）。本测试断言的是**参考语料图**
    # 的坑，故显式取该语料 source，而非默认的 generated。
    reminders = get_reminders(db, project_id, source=f"novel:{novel.id}")
    print("reminders ->", reminders)

    # —— 断言 ——
    failures = []

    # 1) 角色精确 {林夜, 苏晚}
    if set(char_names) != {"林夜", "苏晚"}:
        failures.append(f"角色集合应为 {{林夜,苏晚}}，实际 {set(char_names)}")

    # 2) 林夜出场 [1,2,3]
    if lin is None:
        failures.append("未查到角色 林夜")
    else:
        # 2026-10 KG 缺陷 3 修复：get_character 不再返回 appearances 字段。
        # 原字段是 list(range(first,last+1)) 伪造的连续出场列表（角色只在第 3、
        # 50 章出场也会返回 48 章），仓库内无任何消费方，已按「无消费方就删除
        # 伪造字段」移除。这里原先钉的是伪造值 [1,2,3]——本例角色恰好连续出场
        # 于 1..3，伪造值与真实首末章一致，所以旧断言看着像对的，实为钉死缺陷。
        # 改为钉真实字段 first/last_chapter（语义不变，数据来源诚实）。
        if (lin["first_chapter"], lin["last_chapter"]) != (1, 3):
            failures.append(f"林夜首末章应为 (1,3)，实际 {(lin['first_chapter'], lin['last_chapter'])}")
        if "appearances" in lin:
            failures.append(f"get_character 不应再返回伪造的 appearances，实际 {lin['appearances']!r}")

    # 3) 林夜-苏晚 关系边存在
    edge_ok = any(
        (e["from"], e["to"]) in _pair_ids(db, graph, "林夜", "苏晚")
        for e in graph["edges"]
    )
    if not edge_ok:
        failures.append("林夜-苏晚 关系边缺失")
    else:
        print("关系边 ->", [(e["from"], e["to"], e["props"]) for e in graph["edges"]])

    # 4) 未填坑含「那光是什么」悬念，且全部 open
    unresolved = reminders["unresolved_plotholes"]
    qs = [h.get("question", "") for h in unresolved]
    print("unresolved questions ->", qs)
    if not any("那光是什么" in (q or "") for q in qs):
        failures.append(f"未填坑应含「那光是什么」悬念，实际 {qs}")
    open_cnt = sum(1 for h in graph["nodes"] if h["type"] == "plothole" and h["props"].get("status") == "open")
    total_ph = sum(1 for h in graph["nodes"] if h["type"] == "plothole")
    if open_cnt != total_ph:
        failures.append(f"坑应全部 open，实际 open {open_cnt}/{total_ph}")

    db.close()

    if failures:
        print("\n❌ M3 验证失败：")
        for f in failures:
            print("  -", f)
        return 1
    print("\n✅ M3 规则抽取整链验证通过：角色/章节/关系/坑 全部符合预期。")
    return 0


def _pair_ids(db, graph, a, b):
    idmap = {n["props"]["name"]: n["id"] for n in graph["nodes"] if n["type"] == "character"}
    if a in idmap and b in idmap:
        return {(idmap[a], idmap[b]), (idmap[b], idmap[a])}
    return set()


if __name__ == "__main__":
    sys.exit(main())
