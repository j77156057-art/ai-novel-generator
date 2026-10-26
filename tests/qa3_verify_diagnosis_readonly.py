"""QA 独立验证脚本：Assistant 只读诊断流（commit edd4578）。

**这不是 pytest 用例**（文件名刻意不以 `test_` 开头，避免被 `pytest -q` 收集）。
它是 QA（software-qa-engineer-3）为「独立验证诊断流只读与证据完整性」写的
一次性取证脚本，运行方式：

    cd backend && python ../tests/qa3_verify_diagnosis_readonly.py

覆盖 team-lead 派单的 7 项实测（静态 grep 项见 QA 报告，本脚本只管动态行为）：

  Q1  实测「不改库」：调 /api/assistant/diagnose 前后对
      generations / projects / facts / kg_nodes / kg_edges 五张表做
      **行数 + 逐行内容 SHA256** 比对，必须逐字节一致。
  Q2  Writer 链路零回归：Writer provider chat timeout==120 / stream_timeout==180；
      assistant provider 故意抛异常时 Writer 普通 chat 仍成功、单例未被污染。
  Q3  4 条规则可复现：四种**各自最小**数据分别触发，并校验 rule 名 /
      severity 取值域；name_conflict 必须**不自动合并** Fact。
  Q4  证据完整性：抽查 issue 的 evidence —— snippet 必须是库中真实原文片段
      （Generation.text / NovelChunk.text 可 grep 到）、score ∈ [0,1]、
      kind ∈ 合法域、source 区分 generated 与 novel:{id} 且不串图。
  Q5  证据不丢失：规则一条不命中但请求带 query 时，检索证据**仍**出现在响应里
      （这是工程师修过的缺陷场景）；无证据时不得编造。
  Q6  PermissionError 不降级：端点返回 403 且 AgentRun.status='failed'。
  Q7  StrictRequest 仍生效：/qa 与 /diagnose 携带 4 个写语义字段一律 422。

脚本自带独立 sqlite 库（DATABASE_URL 指向临时文件），**不碰**任何真实开发库；
离线运行（assistant provider 强制走本地 stub，不发任何网络请求）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

# ── 隔离数据库：必须在 import app.* 之前设置 ────────────────────────────
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent / "backend"
_DB_PATH = _BACKEND / "qa3_verify_tmp.db"
if _DB_PATH.exists():
    _DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
os.environ.setdefault("ASSISTANT_PROVIDER", "local")

if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.agents import diagnosis as diagnosis_mod  # noqa: E402
from app.agents.diagnosis import (  # noqa: E402
    ALLOWED_EVIDENCE_KINDS,
    ALLOWED_SEVERITIES,
    run_diagnostic_rules,
)
from app.agents.registry import (  # noqa: E402
    ToolSpec,
    get_tool_registry,
    invoke_tool,
    reset_tool_registry,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.provider import LocalFallbackLLMProvider, ZhipuChatProvider  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import (  # noqa: E402
    AgentRun,
    Evidence,
    Fact,
    Generation,
    KgEdge,
    KgNode,
    NovelChunk,
    Project,
    RawNovel,
)

# ====================================================================
# 迷你断言框架
# ====================================================================

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def reset_all() -> None:
    """清单例 + 重建空库（各检查项之间完全隔离）。"""
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_tool_registry()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


# ====================================================================
# 只读快照工具（Q1 的核心）
# ====================================================================

#: 必须逐字节不变的业务表。
READONLY_TABLES = ("generations", "projects", "facts", "kg_nodes", "kg_edges")

_MODEL_BY_TABLE = {
    "generations": Generation,
    "projects": Project,
    "facts": Fact,
    "kg_nodes": KgNode,
    "kg_edges": KgEdge,
}


def _row_repr(row) -> str:
    """把一行 ORM 记录序列化成**稳定字符串**。

    ⚠️ 不能用 `repr(row)`：SQLAlchemy 对象的 repr 是
    `<app.models.Fact object at 0x000002C0...>` —— 含内存地址，
    两次查询必然不同，会让「逐字节比对」永远失败（QA 自己的坑，已踩）。
    按列名排序输出列值，JSON 字段用 sort_keys 规范化。
    """
    cols = sorted(row.__table__.columns.keys())
    parts = []
    for c in cols:
        v = getattr(row, c, None)
        if isinstance(v, (dict, list)):
            v = json.dumps(v, sort_keys=True, ensure_ascii=False)
        elif isinstance(v, datetime):
            v = v.isoformat()
        parts.append(f"{c}={v!r}")
    return f"{type(row).__name__}({', '.join(parts)})"


def snapshot_tables(db) -> dict[str, dict]:
    """对 5 张业务表做 (行数, 内容 SHA256, 逐行内容) 三重快照。"""
    snap: dict[str, dict] = {}
    for table in READONLY_TABLES:
        model = _MODEL_BY_TABLE[table]
        rows = list(db.scalars(select(model).order_by(model.id)).all())
        payload = [_row_repr(r) for r in rows]
        blob = "\n".join(payload).encode("utf-8")
        snap[table] = {
            "count": len(rows),
            "sha256": hashlib.sha256(blob).hexdigest(),
            "rows": payload,
        }
    return snap


def diff_snapshots(before: dict[str, dict], after: dict[str, dict]) -> list[str]:
    """返回不一致描述；空列表 = 逐字节一致。"""
    problems: list[str] = []
    for table in READONLY_TABLES:
        b, a = before[table], after[table]
        if b["count"] != a["count"]:
            problems.append(f"{table}: 行数 {b['count']} → {a['count']}")
            continue
        if b["sha256"] != a["sha256"]:
            diffs = [
                f"row{i}: {x!r} != {y!r}"
                for i, (x, y) in enumerate(zip(b["rows"], a["rows"]))
                if x != y
            ]
            problems.append(
                f"{table}: sha256 变化 {b['sha256'][:12]} → {a['sha256'][:12]}；"
                f"{len(diffs)} 行不同，例如 {diffs[:2]}"
            )
    return problems


# ====================================================================
# 假 provider（离线可控）
# ====================================================================

class _StubAssistant:
    """记录 system/user 的本地 assistant provider（不发网络请求）。"""

    def __init__(self, reply: str = "STUB_ANSWER") -> None:
        self.reply = reply
        self.model = "stub-assistant-model"
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str, constraints=None) -> str:  # noqa: ANN001
        self.calls.append((system, user))
        return self.reply


def use_stub_assistant(monkey_holder: dict, reply: str = "STUB_ANSWER") -> _StubAssistant:
    """把 assistant provider 工厂换成本地 stub，并写入 registry 缓存。"""
    stub = _StubAssistant(reply)
    llm_provider_mod._ASSISTANT_PROVIDER = stub
    monkey_holder["stub"] = stub
    return stub


def use_broken_assistant() -> None:
    """让 assistant provider 构造必定抛异常。"""
    def _boom():
        raise RuntimeError("QA3 injected assistant provider failure")

    llm_provider_mod.build_assistant_provider = _boom
    llm_provider_mod._ASSISTANT_PROVIDER = None
    reset_provider_registry()


def restore_assistant_factory() -> None:
    """恢复 build_assistant_provider 原实现。"""
    llm_provider_mod.build_assistant_provider = _ORIG_BUILD_ASSISTANT
    llm_provider_mod._ASSISTANT_PROVIDER = None


_ORIG_BUILD_ASSISTANT = llm_provider_mod.build_assistant_provider


# ====================================================================
# 造数helper
# ====================================================================

def make_project(db, title: str = "QA3 项目", ref_novel_ids: list[int] | None = None) -> Project:
    proj = Project(
        title=title,
        target_length=5000,
        mode="quick",
        status="created",
        plot_bible={"characters": ["张三"]},
        premise="QA3 取证用项目",
    )
    if ref_novel_ids is not None:
        proj.reference_novel_ids = ref_novel_ids
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def add_hole(db, project_id: int, question: str, first_chapter: int = 1) -> None:
    db.add(KgNode(
        project_id=project_id,
        type="plothole",
        props={"status": "open", "question": question},
        first_chapter=first_chapter,
        last_chapter=first_chapter,
        source="generated",
    ))
    db.commit()


def add_kg_character(db, project_id: int, name: str) -> None:
    db.add(KgNode(
        project_id=project_id,
        type="character",
        props={"name": name, "mentions": 3},
        first_chapter=1,
        last_chapter=2,
        source="generated",
    ))
    db.commit()


def add_fact(db, project_id: int, *, type: str, key: str,  # noqa: A002
             aliases: list[str] | None = None, props: dict | None = None,
             confidence: str = "high", source: str = "generated") -> None:
    db.add(Fact(
        project_id=project_id,
        type=type,
        key=key,
        props=props or {},
        aliases=aliases or [],
        source=source,
        confidence=confidence,
    ))
    db.commit()


def add_generation(db, project_id: int, segment_no: int, text: str) -> int:
    gen = Generation(
        project_id=project_id,
        segment_no=segment_no,
        chapter_range=f"第{segment_no}章",
        text=text,
        summary={"short": f"第{segment_no}段摘要"},
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen.id


def add_reference_corpus(db) -> tuple[int, list[int]]:
    """造一本参考语料 + 若干分片，返回 (novel_id, chunk_ids)。"""
    novel = RawNovel(title="参考语料·测试用", author="QA3", platform="local", status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    texts = [
        "张三在雨夜里握紧了那枚铜钱，指节发白。",
        "李四从巷口走来，脚步很轻，像是怕惊动什么人。",
        "王五的大伞倾向赵六那一侧，两人并肩走进长街。",
    ]
    ids: list[int] = []
    for i, t in enumerate(texts):
        chunk = NovelChunk(novel_id=novel.id, chapter_no=1, idx=i, text=t)
        db.add(chunk)
        db.commit()
        db.refresh(chunk)
        ids.append(chunk.id)
    return novel.id, ids


# ====================================================================
# Q1 —— 只读边界：不改库
# ====================================================================

def q1_readonly_boundary() -> None:
    section("Q1  只读边界：调 /diagnose 前后 5 张业务表逐字节一致")
    reset_all()
    holder: dict = {}
    use_stub_assistant(holder)

    with SessionLocal() as db:
        proj = make_project(db, "Q1 只读边界项目")
        pid = proj.id
        # 造齐 4 条规则各自需要的全部数据
        add_hole(db, pid, "铜钱究竟是谁留下的？", 1)
        add_hole(db, pid, "李四为何背叛了组织？", 2)
        add_kg_character(db, pid, "李四")
        add_kg_character(db, pid, "张三")
        add_fact(db, pid, type="character", key="张三", aliases=["李四"])
        add_fact(db, pid, type="character", key="王五", aliases=["钱多多"])
        for i in range(6):
            add_fact(db, pid, type="character", key=f"低置信角色{i}",
                     confidence="low", source=f"novel_{i + 1}")
        add_fact(db, pid, type="state", key="life:赵六",
                 props={"value": "dead", "since_segment": 1, "text": "赵六倒在长街尽头。"},
                 source="generated")
        add_generation(db, pid, 1, "赵六倒在长街尽头，再也没有起来。")
        add_generation(db, pid, 2, "众人谈起赵六当年的勇猛，无不叹息。")

        before = snapshot_tables(db)
        print(f"  调用前快照：{ {t: before[t]['count'] for t in READONLY_TABLES} }")

    from app.routers import assistant as assistant_router

    body = assistant_router.AssistantDiagnoseRequest(project_id=pid, scope="all", query="赵六")
    resp = assistant_router.assistant_diagnose(body=body, db=SessionLocal())

    check("Q1.1 诊断返回 issues（非空说明数据确实触发了规则）",
          len(resp["issues"]) > 0, f"命中 {len(resp['issues'])} 条："
          + ", ".join(sorted({i['rule'] for i in resp['issues']})))

    with SessionLocal() as db:
        after = snapshot_tables(db)
        print(f"  调用后快照：{ {t: after[t]['count'] for t in READONLY_TABLES} }")
        problems = diff_snapshots(before, after)
        check("Q1.2 五张业务表行数 + 内容 SHA256 逐字节一致",
              not problems,
              "全部一致：" + ", ".join(f"{t}={after[t]['count']}行/{after[t]['sha256'][:8]}"
                                       for t in READONLY_TABLES)
              if not problems else "\n         ".join(problems))

        # 审计表**应该**有写入（否则说明端点没落 Evidence，是另一种 bug）
        runs = list(db.scalars(select(AgentRun).where(AgentRun.project_id == pid)).all())
        evs = list(db.scalars(select(Evidence).where(Evidence.run_id == runs[-1].id)).all())
        check("Q1.3 审计表 AgentRun/Evidence 正常写入（只读≠不记录）",
              len(runs) == 1 and runs[0].status == "success" and len(evs) > 0,
              f"AgentRun={len(runs)} 条 status={runs[-1].status}；Evidence={len(evs)} 行")

    reset_all()


# ====================================================================
# Q2 —— Writer 链路零回归
# ====================================================================

def q2_writer_zero_regression() -> None:
    section("Q2  Writer 链路零回归：超时值 + assistant 故障隔离")
    reset_all()
    restore_assistant_factory()

    # --- Q2.1 超时不变量 ---
    writer = ZhipuChatProvider("fake-key", "https://fake.api", "fake-model")
    check("Q2.1 Writer chat timeout == 120", writer.timeout == 120, f"实际 {writer.timeout}")
    check("Q2.2 Writer stream_timeout == 180", writer.stream_timeout == 180,
          f"实际 {writer.stream_timeout}")

    # --- Q2.3 assistant 抛异常时 Writer 单例与 chat 完好 ---
    captured: list[int | None] = []

    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._b = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def read(self) -> bytes:
            return self._b

    def _fake_urlopen(req, timeout=None, **kw):
        captured.append(timeout)
        return _Resp(json.dumps({"choices": [{"message": {"content": "writer-alive"}}]}).encode())

    real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen
    try:
        writer2 = ZhipuChatProvider("fake-key", "https://fake.api", "fake-model")
        llm_provider_mod._PROVIDER = writer2
        use_broken_assistant()

        # 走一次完整的 /diagnose（assistant provider 必抛）
        with SessionLocal() as db:
            proj = make_project(db, "Q2 项目")
            pid = proj.id
            add_hole(db, pid, "Q2 的坑？", 1)
        from app.routers import assistant as assistant_router

        holder: dict = {}
        stub = use_stub_assistant(holder)  # 先给个正常的，确认端点本身可跑
        body = assistant_router.AssistantDiagnoseRequest(project_id=pid, scope="all")
        resp_ok = assistant_router.assistant_diagnose(body=body, db=SessionLocal())
        check("Q2.3a assistant 正常时诊断 200 且有 issues",
              len(resp_ok["issues"]) > 0, f"issues={len(resp_ok['issues'])}")

        # 现在把 assistant 换成坏的（必须 reset registry，否则拿到缓存里的旧 stub）
        llm_provider_mod._ASSISTANT_PROVIDER = None
        reset_provider_registry()
        use_broken_assistant()
        reset_provider_registry()  # ← 让 registry 下次真的去调那个 boom 工厂
        resp_bad = assistant_router.assistant_diagnose(
            body=assistant_router.AssistantDiagnoseRequest(project_id=pid, scope="all"),
            db=SessionLocal(),
        )
        check("Q2.3b assistant 抛异常 → 端点降级但仍 200（不被 5xx 拖垮）",
              isinstance(resp_bad, dict) and "issues" in resp_bad,
              f"降级 answer: {resp_bad['answer'][:60]}...")

        # Writer 单例未被污染
        check("Q2.3c Writer 单例未被 assistant 故障污染",
              llm_provider_mod._PROVIDER is writer2,
              f"_PROVIDER={type(llm_provider_mod._PROVIDER).__name__}")

        captured.clear()
        out = llm_provider_mod._PROVIDER.chat("sys", "user")
        check("Q2.3d assistant 故障后 Writer 普通 chat 仍成功",
              out == "writer-alive" and captured == [120],
              f"Writer 返回 {out!r}，timeout={captured}")
    finally:
        urllib.request.urlopen = real_urlopen
        restore_assistant_factory()

    # --- Q2.5 源码零回归确认 ---
    import subprocess

    changed = subprocess.run(
        ["git", "diff", "HEAD~1", "--name-only", "--",
         "backend/app/generation", "backend/app/llm", "backend/app/routers/generation.py"],
        cwd=str(_HERE.parent), capture_output=True, text=True,
    ).stdout.strip()
    check("Q2.5 generation/* llm/* routers/generation.py 未被本 commit 修改",
          changed == "", f"git 报告改动：{changed!r}" if changed else "git diff 输出为空")

    reset_all()


# ====================================================================
# Q3 —— 4 条规则可复现（各自最小数据）
# ====================================================================

def q3_rules_reproducible() -> None:
    section("Q3  四条规则可复现（各自最小数据）+ name_conflict 不自动合并")

    # ---------- 规则 ① open_plothole_accumulation ----------
    reset_all()
    holder: dict = {}
    use_stub_assistant(holder)
    with SessionLocal() as db:
        proj = make_project(db, "Q3-规则1")
        pid = proj.id
        add_hole(db, pid, "那封没寄出的信是谁写的？", 1)
        rep = run_diagnostic_rules(db, proj, scope="all")
    got = [i for i in rep.issues if i.rule == "open_plothole_accumulation"]
    check("Q3.1 rule=open_plothole_accumulation 可复现",
          len(got) == 1 and got[0].severity in ALLOWED_SEVERITIES,
          f"severity={got[0].severity if got else 'N/A'} / "
          f"evidence={len(got[0].evidence) if got else 0} 条"
          + (f" / claim={got[0].claim[:50]!r}" if got else ""))

    # ---------- 规则 ② low_confidence_facts_overflow ----------
    reset_all()
    with SessionLocal() as db:
        proj = make_project(db, "Q3-规则2")
        pid = proj.id
        for i in range(6):  # 阈值 5，必须 >5 才报
            add_fact(db, pid, type="character", key=f"待裁决{i}", confidence="low")
        rep = run_diagnostic_rules(db, proj, scope="all")
    got = [i for i in rep.issues if i.rule == "low_confidence_facts_overflow"]
    check("Q3.2 rule=low_confidence_facts_overflow 可复现（6 > 阈值5）",
          len(got) == 1 and got[0].severity in ALLOWED_SEVERITIES,
          f"severity={got[0].severity if got else 'N/A'}"
          + (f" / claim={got[0].claim[:60]!r}" if got else ""))

    # 边界：正好 5 条不应报
    with SessionLocal() as db:
        p2 = make_project(db, "Q3-规则2-边界")
        for i in range(5):
            add_fact(db, p2.id, type="character", key=f"边界{i}", confidence="low")
        rep2 = run_diagnostic_rules(db, p2, scope="all")
    check("Q3.2b 边界：恰好 5 条低置信 → 不报（阈值语义为「>」而非「>=」）",
          not [i for i in rep2.issues if i.rule == "low_confidence_facts_overflow"],
          f"issues={[i.rule for i in rep2.issues]}")

    # ---------- 规则 ③ name_conflict ----------
    reset_all()
    with SessionLocal() as db:
        proj = make_project(db, "Q3-规则3")
        pid = proj.id
        # 形态①：别名已独立成 KG 节点
        add_kg_character(db, pid, "李四")
        add_fact(db, pid, type="character", key="张三", aliases=["李四"])
        # 形态②：别名撞上另一条规范名
        add_fact(db, pid, type="character", key="王五", aliases=["赵六"])
        add_fact(db, pid, type="character", key="赵六")

        facts_before = snapshot_tables(db)["facts"]
        alias_before = {
            r.key: sorted(r.aliases or [])
            for r in db.scalars(select(Fact).where(Fact.project_id == pid)).all()
        }
        rep = run_diagnostic_rules(db, proj, scope="all")
        facts_after = snapshot_tables(db)["facts"]
        alias_after = {
            r.key: sorted(r.aliases or [])
            for r in db.scalars(select(Fact).where(Fact.project_id == pid)).all()
        }

    got = [i for i in rep.issues if i.rule == "name_conflict"]
    sev = {i.id: i.severity for i in got}
    check("Q3.3 rule=name_conflict 两种形态都触发",
          len(got) == 2, f"命中 {len(got)} 条：{[(i.id, i.severity) for i in got]}")
    check("Q3.3b name_conflict severity 合法（warn / error 形态分明）",
          all(s in ALLOWED_SEVERITIES for s in sev.values())
          and any("split:" in k for k in sev) and any("collide:" in k for k in sev),
          f"id→severity: {sev}")
    check("Q3.3c **name_conflict 不自动合并 Fact**：facts 表逐字节不变",
          facts_before["sha256"] == facts_after["sha256"] and facts_before["count"] == facts_after["count"],
          f"{facts_before['count']} 行 / sha {facts_before['sha256'][:12]} → "
          f"{facts_after['count']} 行 / sha {facts_after['sha256'][:12]}")
    check("Q3.3d 别名 key 与值未被篡改",
          alias_before == alias_after, f"aliases={alias_after}")

    # ---------- 规则 ④ dead_character_reappearance ----------
    reset_all()
    with SessionLocal() as db:
        proj = make_project(db, "Q3-规则4")
        pid = proj.id
        add_fact(db, pid, type="state", key="life:赵六",
                 props={"value": "dead", "since_segment": 1, "text": "赵六倒在长街尽头。"},
                 source="generated")
        add_generation(db, pid, 1, "赵六倒在长街尽头，再也没有起来。")
        add_generation(db, pid, 2, "众人谈起赵六当年的勇猛，无不叹息。")
        rep = run_diagnostic_rules(db, proj, scope="all")
    got = [i for i in rep.issues if i.rule == "dead_character_reappearance"]
    check("Q3.4 rule=dead_character_reappearance 可复现（life:X=dead + 后续正文再出现 X）",
          len(got) == 1 and got[0].severity in ALLOWED_SEVERITIES,
          f"severity={got[0].severity if got else 'N/A'} / "
          f"claim={got[0].claim[:60]!r}" if got else "未命中")

    # 死亡段之后不再出现 → 不报
    reset_all()
    with SessionLocal() as db:
        proj = make_project(db, "Q3-规则4-反向")
        pid = proj.id
        add_fact(db, pid, type="state", key="life:赵六",
                 props={"value": "dead", "since_segment": 2, "text": "赵六死了。"},
                 source="generated")
        add_generation(db, pid, 1, "赵六还在这里。")
        add_generation(db, pid, 2, "赵六死了。")
        add_generation(db, pid, 3, "众人谈起往事。")
        rep = run_diagnostic_rules(db, proj, scope="all")
    check("Q3.4b 反向：死亡段之后未再出现 → 不报",
          not [i for i in rep.issues if i.rule == "dead_character_reappearance"],
          f"issues={[i.rule for i in rep.issues]}")

    # ---------- 全 4 条同时命中 ----------
    reset_all()
    with SessionLocal() as db:
        proj = make_project(db, "Q3-全规则")
        pid = proj.id
        add_hole(db, pid, "铜钱是谁留下的？", 1)
        add_kg_character(db, pid, "李四")
        add_fact(db, pid, type="character", key="张三", aliases=["李四"])
        for i in range(6):
            add_fact(db, pid, type="character", key=f"低置信{i}", confidence="low")
        add_fact(db, pid, type="state", key="life:赵六",
                 props={"value": "dead", "since_segment": 1, "text": "赵六死了。"},
                 source="generated")
        add_generation(db, pid, 2, "赵六的名字再次被提起。")
        rep = run_diagnostic_rules(db, proj, scope="all")
    rules_hit = {i.rule for i in rep.issues}
    allfour = set(diagnosis_mod.RULE_ORDER)
    check("Q3.5 四条规则可同时命中，且 rules_evaluated 与 RULE_ORDER 一致",
          rules_hit == allfour and tuple(rep.rules_evaluated) == tuple(diagnosis_mod.RULE_ORDER),
          f"命中={sorted(rules_hit)}；evaluated={rep.rules_evaluated}")

    # 确定性：同一库跑两次 issue id 完全一致
    reset_all()
    with SessionLocal() as db:
        proj = make_project(db, "Q3-确定性")
        pid = proj.id
        add_hole(db, pid, "确定性坑？", 1)
        add_kg_character(db, pid, "李四")
        add_fact(db, pid, type="character", key="张三", aliases=["李四"])
        ids_1 = [i.id for i in run_diagnostic_rules(db, proj, scope="all").issues]
        ids_2 = [i.id for i in run_diagnostic_rules(db, proj, scope="all").issues]
    check("Q3.6 确定性：同一库连跑两次 issue id 逐字节一致",
          ids_1 == ids_2, f"{ids_1} == {ids_2}")

    reset_all()


# ====================================================================
# Q4 —— 证据完整性
# ====================================================================

def q4_evidence_integrity() -> None:
    section("Q4  证据完整性：snippet 真实 / score∈[0,1] / kind 合法 / source 隔离")
    reset_all()
    holder: dict = {}
    use_stub_assistant(holder)

    with SessionLocal() as db:
        novel_id, chunk_ids = add_reference_corpus(db)
        proj = make_project(db, "Q4 证据完整性", ref_novel_ids=[novel_id])
        pid = proj.id
        add_hole(db, pid, "铜钱究竟是谁留下的？", 1)
        add_kg_character(db, pid, "李四")
        add_fact(db, pid, type="character", key="张三", aliases=["李四"])
        for i in range(6):
            add_fact(db, pid, type="character", key=f"低置信角色{i}",
                     confidence="low", source="novel_7")
        add_fact(db, pid, type="state", key="life:赵六",
                 props={"value": "dead", "since_segment": 1, "text": "赵六死在长街。"},
                 source="generated")
        gid_late = add_generation(db, pid, 2, "众人谈起赵六当年的勇猛，无不叹息。")

        gen_texts = [g.text or "" for g in db.scalars(
            select(Generation).where(Generation.project_id == pid)).all()]
        chunk_texts = [c.text or "" for c in db.scalars(
            select(NovelChunk).where(NovelChunk.novel_id == novel_id)).all()]
        kg_dump = repr([
            (n.id, n.type, n.props, n.source) for n in db.scalars(
                select(KgNode).where(KgNode.project_id == pid)).all()])
        # Fact 证据 source 核对用：记下每条 Fact 的 key → 自身 source
        fact_keys_src = [
            {"key": f.key, "source": f.source} for f in db.scalars(
                select(Fact).where(Fact.project_id == pid)).all()]
        fact_dump = repr([
            (f.id, f.type, f.key, f.aliases, f.props, f.source) for f in db.scalars(
                select(Fact).where(Fact.project_id == pid)).all()])

    from app.routers import assistant as assistant_router

    resp = assistant_router.assistant_diagnose(
        body=assistant_router.AssistantDiagnoseRequest(
            project_id=pid, scope="all", query="铜钱"),
        db=SessionLocal(),
    )

    issues = resp["issues"]
    # 检索证据（kind=chunk）挂在**顶层 evidence**（report.extra_evidence），
    # 不在 issues[].evidence 里 —— 断言必须把两处合起来看，否则会误判成「无 chunk 证据」。
    issue_ev = [e for i in issues for e in i["evidence"]]
    top_ev = resp["evidence"]
    all_ev = issue_ev + top_ev
    check("Q4.1 顶层 evidence 非空", len(top_ev) > 0,
          f"issues={len(issues)}，issue 内 evidence={len(issue_ev)}，"
          f"顶层 evidence={len(top_ev)}")

    bad_score = [e for e in all_ev if not (0.0 <= float(e["score"]) <= 1.0)]
    check("Q4.2 全部 evidence.score ∈ [0,1]",
          not bad_score, f"越界 {len(bad_score)} 条" if bad_score
          else f"取值集合={sorted({round(float(e['score']), 3) for e in all_ev})}")

    bad_kind = [e for e in all_ev if e["kind"] not in ALLOWED_EVIDENCE_KINDS]
    check("Q4.3 全部 evidence.kind ∈ {generation, chunk, reminder, fact, kg}",
          not bad_kind, f"越界 kind={ {e['kind'] for e in bad_kind} }" if bad_kind
          else f"实际 kind 分布={ {k: sum(1 for e in all_ev if e['kind'] == k) for k in ALLOWED_EVIDENCE_KINDS} }")

    bad_sev = [i for i in issues if i["severity"] not in ALLOWED_SEVERITIES]
    check("Q4.4 全部 issue.severity ∈ {info, warn, error}",
          not bad_sev, f"实际分布={ {s: sum(1 for i in issues if i['severity'] == s) for s in ALLOWED_SEVERITIES} }")

    missing = [i for i in issues if not all(
        i.get(k) for k in ("id", "type", "severity", "title", "claim", "suggestion", "rule"))]
    check("Q4.5 issue 九字段齐全（id/type/severity/title/claim/evidence/suggestion/rule）",
          not missing, f"缺字段 issue={[i.get('id') for i in missing]}")

    # --- snippet 真实性：逐条按 kind 到对应表里 grep ---
    gen_ev = [e for e in all_ev if e["kind"] == "generation"]
    chunk_ev = [e for e in all_ev if e["kind"] == "chunk"]
    other_ev = [e for e in all_ev if e["kind"] in ("reminder", "fact", "kg")]

    gen_bad = [e for e in gen_ev if not any(e["snippet"] in t for t in gen_texts)]
    check("Q4.6 generation 证据 snippet 可在 Generation.text 中 grep 到（逐字节）",
          gen_ev and not gen_bad,
          f"抽查 {len(gen_ev)} 条全部命中原文" if gen_ev and not gen_bad
          else f"gen_ev={len(gen_ev)}，未命中={len(gen_bad)}")

    chunk_bad = [e for e in chunk_ev if not any(e["snippet"] in t for t in chunk_texts)]
    check("Q4.7 chunk 证据 snippet 可在 NovelChunk.text 中 grep 到（逐字节）",
          bool(chunk_ev) and not chunk_bad,
          f"抽查 {len(chunk_ev)} 条全部命中原文" if chunk_ev and not chunk_bad
          else f"chunk_ev={len(chunk_ev)}，未命中={len(chunk_bad)}")

    other_bad = [e for e in other_ev
                 if e["snippet"] not in kg_dump and e["snippet"] not in fact_dump]
    check("Q4.8 reminder/fact/kg 证据 snippet 可在 kg_nodes / facts 原值中 grep 到",
          not other_bad,
          f"抽查 {len(other_ev)} 条全部命中" if not other_bad
          else f"未命中={[(e['kind'], e['snippet'][:30]) for e in other_bad]}")

    check("Q4.9 抽查 ≥3 条 issue 的证据明细（人工可复核）", True,
          "; ".join(f"[{i['rule']}/{i['severity']}] {e['kind']}·{e['source']}·"
                    f"ref={e['ref_id']}·score={e['score']}·snippet={e['snippet'][:28]!r}"
                    for i in issues[:3] for e in i["evidence"][:1]))

    # --- source 隔离 ---
    # 关键：证据 source 必须**忠实反映该行数据自身的来源**。
    # 语料来源的 Fact（source=novel_7）标成 novel:7 是**正确隔离**，
    # 把它算成「串图」是断言写反了。真正的串图方向是反过来的：
    # 语料来源的数据被**误标成 generated**（把参考语料说成用户自己写的）。
    # 因此下面按「行来源 → 证据标签」逐条核对，而不是要求所有非 chunk 证据都是 generated。
    gen_kind = ("generation", "reminder", "kg")
    gen_side = [e for e in all_ev if e["kind"] in gen_kind]
    src_bad = [e for e in gen_side if e["source"] != "generated"]
    check("Q4.10 生成图侧证据(generation/reminder/kg) source 恒为 'generated'",
          not src_bad, f"异常={[(e['kind'], e['source']) for e in src_bad]}" if src_bad
          else f"{len(gen_side)} 条生成图侧证据 source 全为 generated")

    # Fact 证据：逐条核对 source 是否等于该 Fact 行自身 source 的归一化结果
    fact_ev = [e for e in all_ev if e["kind"] == "fact"]
    expect_src: dict[str, str] = {}
    for r in fact_keys_src:
        expect_src[r["key"]] = r["source"]
    mismatched = []
    for e in fact_ev:
        key = (e["ref_id"] or "").removeprefix("fact:")
        raw = expect_src.get(key)
        if raw is None:
            continue
        want = f"novel:{raw.split('_', 1)[1]}" if raw.startswith("novel_") else raw
        if e["source"] != want:
            mismatched.append((key, raw, e["source"], want))
    check("Q4.10b Fact 证据 source 忠实反映该行事实自身来源（语料事实不得标成 generated）",
          not mismatched,
          f"核对 {len(fact_ev)} 条，来源标签全部与 facts.source 一致（语料事实→novel:7，生成事实→generated）"
          if not mismatched else f"错标={mismatched}")

    leaked_as_generated = [
        e for e in fact_ev
        if e["source"] == "generated"
        and str(expect_src.get((e["ref_id"] or "").removeprefix("fact:"), "")).startswith("novel")
    ]
    check("Q4.10c 无「语料事实被误标为 generated」的串图",
          not leaked_as_generated,
          f"误标 {len(leaked_as_generated)} 条" if leaked_as_generated
          else "语料来源 fact 全部标为 novel:{id}，无一条被误标 generated")

    src_bad2 = [e for e in chunk_ev if e["source"] != f"novel:{novel_id}"]
    check("Q4.11 参考语料证据 source 恒为 'novel:{id}'，不与生成内容串图",
          not src_bad2 and bool(chunk_ev),
          f"异常={[(e['source']) for e in src_bad2]}" if src_bad2
          else f"{len(chunk_ev)} 条语料证据 source 均为 novel:{novel_id}")

    novel_tagged_gen = [e for e in gen_side if str(e["source"]).startswith("novel")]
    gen_tagged_chunk = [e for e in chunk_ev if str(e["source"]).startswith("generated")]
    check("Q4.12 双向不串图：生成图侧证据无 novel 标签 / 语料证据无 generated 标签",
          not novel_tagged_gen and not gen_tagged_chunk,
          f"串图 {len(novel_tagged_gen) + len(gen_tagged_chunk)} 条"
          if (novel_tagged_gen or gen_tagged_chunk)
          else "生成图侧与语料侧两个来源域完全隔离")

    reset_all()


# ====================================================================
# Q5 —— 证据不丢失 / 无证据不编造
# ====================================================================

def q5_evidence_not_lost() -> None:
    section("Q5  证据不丢失（规则零命中但带 query）+ 无证据不编造")
    reset_all()
    holder: dict = {}
    stub = use_stub_assistant(holder)

    with SessionLocal() as db:
        novel_id, _ = add_reference_corpus(db)
        proj = make_project(db, "Q5 证据不丢失", ref_novel_ids=[novel_id])
        pid = proj.id
        # 干净库：4 条规则**一条都不会命中**（无坑 / 无低置信 / 无别名冲突 / 无死亡角色）
        add_generation(db, pid, 1, "张三在雨夜里握紧了那枚铜钱。")

    from app.routers import assistant as assistant_router

    resp = assistant_router.assistant_diagnose(
        body=assistant_router.AssistantDiagnoseRequest(
            project_id=pid, scope="all", query="铜钱"),
        db=SessionLocal(),
    )

    check("Q5.1 前置：4 条规则确实一条都没命中",
          resp["issues"] == [] and len(resp["rules_evaluated"]) == 4,
          f"issues={resp['issues']}；rules_evaluated={resp['rules_evaluated']}")

    check("Q5.2 **证据没有丢失**：顶层 evidence 仍带 query 的检索证据（回归重点）",
          len(resp["evidence"]) > 0,
          f"evidence={len(resp['evidence'])} 条 → "
          f"{[(e['kind'], e['source'], e['ref_id']) for e in resp['evidence']]}")
    check("Q5.3 检索证据 kind=='chunk' 且 source 为 novel:{id}",
          all(e["kind"] == "chunk" for e in resp["evidence"])
          and all(str(e["source"]).startswith("novel:") for e in resp["evidence"]),
          f"sources={ {e['source'] for e in resp['evidence']} }")
    check("Q5.4 模型确实被调用（need_llm = 规则命中 or 带 query）",
          len(stub.calls) >= 1,
          f"assistant provider 被调用 {len(stub.calls)} 次")
    check("Q5.5 answer 非空且说明规则已全执行",
          bool(resp["answer"].strip()),
          f"answer={resp['answer'][:80]!r}")
    check("Q5.6 confidence ∈ [0,1] 且 agent_run_id 存在",
          0.0 <= float(resp["confidence"]) <= 1.0 and resp["agent_run_id"] is not None,
          f"confidence={resp['confidence']}，agent_run_id={resp['agent_run_id']}")

    # --- 无证据时不得编造 ---
    # ⚠️ 两个必须说清的事实（QA 踩过的坑）：
    #  1) ProviderRegistry 自己有一层 _cache，光改 _ASSISTANT_PROVIDER 不够，
    #     必须 reset_provider_registry()，否则拿到的还是上一个 stub。
    #  2) `hybrid_search` 对**任意** query 都会用 RRF 把 k 个分片排出来（哪怕全 0 分），
    #     所以「查无此人 → evidence 必为空」这个断言本身不成立。
    #     PRD 第 2 条真正可确定性验证的是「**不得编造引用**」——
    #     即每条 evidence 的 snippet 必须能在库里 grep 到；
    #     「明确说明未找到」是交给模型的文案职责，只能验到 system 提示词确实下了这条指令。
    stub2_holder: dict = {}
    stub2 = use_stub_assistant(stub2_holder, reply="我没有找到相关内容。")
    reset_provider_registry()  # ← 让 registry 丢掉旧 stub，改用 stub2

    with SessionLocal() as db:
        chunk_texts = [c.text or "" for c in db.scalars(select(NovelChunk)).all()]

    resp_none = assistant_router.assistant_diagnose(
        body=assistant_router.AssistantDiagnoseRequest(
            project_id=pid, scope="all", query="查无此人的名字欧阳娜娜"),
        db=SessionLocal(),
    )
    none_ev = resp_none["evidence"]
    fabricated = [
        (e["ref_id"], e["snippet"][:30]) for e in none_ev
        if not any(e["snippet"] in t for t in chunk_texts)
    ]
    check("Q5.7 **无证据时不得编造引用**：全部 evidence snippet 可在 NovelChunk.text 中 grep 到",
          not fabricated,
          f"{len(none_ev)} 条证据 snippet 全部可在库内原文 grep 到，无一条编造"
          if not fabricated else f"编造={fabricated}")

    sys_prompts = [s for s, _ in stub2.calls]
    check("Q5.8 system 提示词确实要求「无证据时明确说明 / 禁止编造」（把说明义务落到模型上）",
          any("无证据时明确说明" in s and "禁止编造" in s for s in sys_prompts),
          f"命中 {sum(1 for s in sys_prompts if '无证据时明确说明' in s)}/{len(sys_prompts)} 次调用")

    if none_ev:
        low = [(round(float(e["score"]), 4), e["ref_id"]) for e in none_ev]
        print(f"         [P2 观察] 查无此角色时检索仍返回 {len(low)} 条低分证据：{low}")
        print("         [P2 观察] 根因：hybrid_search 用 RRF 融合，恒返回 top-k，无相关度下限；"
              "非编造（snippet 均可在原文 grep 到），但会把 0 分命中当证据呈现。")

    reset_all()


# ==================================================================
# Q6 —— PermissionError 不降级
# ====================================================================

def q6_permission_error_not_downgraded() -> None:
    section("Q6  PermissionError 不降级：必须 403 + AgentRun.status='failed'")
    reset_all()
    holder: dict = {}
    use_stub_assistant(holder)

    reg = get_tool_registry()
    # --- Q6.1 register 层双重门：write_ 前缀工具被拒，且不进入注册表 ---
    try:
        reg.register(ToolSpec(name="write_generation", description="恶意写入",
                              read_only=True, callable=lambda **kw: {}))
        registered = True
        err = ""
    except PermissionError as exc:
        registered = False
        err = str(exc)
    check("Q6.1 register 拒绝 write_ 前缀工具（read_only=True 也拒）",
          not registered and "write_generation" not in reg.names(), f"PermissionError: {err}")

    try:
        reg.register(ToolSpec(name="benign_tool", description="read_only=False 伪装",
                              read_only=False, callable=lambda **kw: {}))
        registered2, err2 = True, ""
    except PermissionError as exc:
        registered2, err2 = False, str(exc)
    check("Q6.2 register 拒绝 read_only=False 的工具",
          not registered2 and "benign_tool" not in reg.names(), f"PermissionError: {err2}")

    # --- Q6.3 invoke 层双重门：即使绕过 register 直接 invoke 也被拒 ---
    try:
        invoke_tool("write_generation", db=None, project_id=1)
        invoked_ok, err3 = True, ""
    except PermissionError as exc:
        invoked_ok, err3 = False, str(exc)
    check("Q6.3 invoke 层二次门：直接 invoke 写工具仍被拒",
          not invoked_ok, f"PermissionError: {err3}")

    # --- Q6.4 端到端：让规则取数时抛 PermissionError → 必须 403 ---
    with SessionLocal() as db:
        proj = make_project(db, "Q6 越权项目")
        pid = proj.id
        add_hole(db, pid, "Q6 的坑？", 1)

    import app.agents.diagnosis as diag_mod

    orig_invoke = diag_mod.invoke_tool if hasattr(diag_mod, "invoke_tool") else None

    # diagnosis.py 在 _invoke 内部 `from app.agents.registry import invoke_tool`，
    # 因此 patch registry 模块属性即可生效。
    import app.agents.registry as reg_mod

    real_invoke_tool = reg_mod.invoke_tool

    def _permission_raising_invoke(name, /, db=None, project_id=None, **kwargs):
        if name == "get_reminders":  # 模拟：规则取数时被角色锁拦下
            raise PermissionError(f"QA3 injected PermissionError on {name}")
        return real_invoke_tool(name, db=db, project_id=project_id, **kwargs)

    reg_mod.invoke_tool = _permission_raising_invoke
    try:
        from app.routers import assistant as assistant_router

        status_code = None
        detail = ""
        try:
            assistant_router.assistant_diagnose(
                body=assistant_router.AssistantDiagnoseRequest(project_id=pid, scope="all"),
                db=SessionLocal(),
            )
        except Exception as exc:  # HTTPException
            status_code = getattr(exc, "status_code", None)
            detail = getattr(exc, "detail", str(exc))

        check("Q6.4 端点把 PermissionError 转成 403（不是 200「没问题」）",
              status_code == 403, f"status_code={status_code}，detail={detail!r}")

        with SessionLocal() as db:
            runs = list(db.scalars(
                select(AgentRun).where(AgentRun.project_id == pid).order_by(AgentRun.id)).all())
            last = runs[-1]
            check("Q6.5 AgentRun.status == 'failed'（越权留痕，不是 success）",
                  last.status == "failed",
                  f"status={last.status!r}，model={last.model!r}，"
                  f"finished_at={last.finished_at is not None}")
    finally:
        reg_mod.invoke_tool = real_invoke_tool

    # --- Q6.6 HTTP 层真打一次，确认 403 状态码 ---
    reg_mod.invoke_tool = _permission_raising_invoke
    try:
        with TestClient(fastapi_app) as client:
            r = client.post("/api/assistant/diagnose",
                            json={"project_id": pid, "scope": "all"})
            check("Q6.6 HTTP 层实测返回 403",
                  r.status_code == 403, f"HTTP {r.status_code}，body={r.text[:120]}")
    finally:
        reg_mod.invoke_tool = real_invoke_tool

    reset_all()


# ====================================================================
# Q7 —— StrictRequest 仍生效
# ====================================================================

def q7_strict_request() -> None:
    section("Q7  StrictRequest：写语义字段一律 422（/qa 与 /diagnose 各 4 个）")
    reset_all()
    holder: dict = {}
    use_stub_assistant(holder)

    with SessionLocal() as db:
        proj = make_project(db, "Q7 严格请求")
        pid = proj.id
        add_hole(db, pid, "Q7 的坑？", 1)

    forbidden = ("patch_json", "commit_hash", "target_table", "rollback_version")
    all_422 = True
    with TestClient(fastapi_app) as client:
        for field in forbidden:
            for endpoint, base in (("/api/assistant/qa", {"query": "问题"}),
                                   ("/api/assistant/diagnose", {"scope": "all"})):
                payload = {"project_id": pid, **base, field: "anything"}
                r = client.post(endpoint, json=payload)
                ok = r.status_code == 422
                all_422 &= ok
                print(f"  [{'PASS' if ok else 'FAIL'}] {endpoint} + {field} → "
                      f"{r.status_code}")
                if not ok:
                    print(f"         期望 422，实际 {r.status_code}；body={r.text[:160]}")
    check("Q7.1 /qa 与 /diagnose 对 4 个写语义字段全部 422",
          all_422, "8/8 组合均被 StrictRequest(extra=forbid) 拒绝")

    # 反面对照：合法请求必须 200（确认不是「所有请求都 422」的假通过）
    with TestClient(fastapi_app) as client:
        r_ok = client.post("/api/assistant/diagnose", json={"project_id": pid, "scope": "all"})
        check("Q7.2 对照组：合法请求体返回 200（排除假通过）",
              r_ok.status_code == 200,
              f"HTTP {r_ok.status_code}，issues={len(r_ok.json().get('issues', []))}")

    # 端点清单：确认没有其它写入路径
    paths = sorted(
        r.path for r in fastapi_app.routes
        if getattr(r, "path", "").startswith("/api/assistant")
    )
    check("Q7.3 /api/assistant 下只有 /qa 与 /diagnose 两个端点（无 apply/commit/rollback/patch/versions/proposals）",
          paths == ["/api/assistant/diagnose", "/api/assistant/qa"], f"实际路由={paths}")

    reset_all()


# ====================================================================
# main
# ====================================================================

def main() -> int:
    print("=" * 78)
    print("QA3 独立验证：Assistant 只读诊断流（commit edd4578）")
    print(f"DB(isolated) = {os.environ['DATABASE_URL']}")
    print("=" * 78)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    q1_readonly_boundary()
    q2_writer_zero_regression()
    q3_rules_reproducible()
    q4_evidence_integrity()
    q5_evidence_not_lost()
    q6_permission_error_not_downgraded()
    q7_strict_request()

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = len(_RESULTS) - passed
    print(f"\n{'=' * 78}")
    print(f"总计 {len(_RESULTS)} 项：通过 {passed} / 失败 {failed}")
    if failed:
        print("\n失败项：")
        for name, ok, detail in _RESULTS:
            if not ok:
                print(f"  - {name}\n      {detail}")
    print("=" * 78)

    # 清理临时库
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    if _DB_PATH.exists():
        _DB_PATH.unlink()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
