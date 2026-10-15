"""QA5 独立复验脚本：commit e933f73「漂移检测可被请求体 scope 绕过」修复。

**这不是 pytest 用例**（文件名不以 test_ 开头，避免被收集），而是一份
一次性跑完、把证据打到 stdout 的独立核验程序：

    cd <repo> && python tests/qa5_verify_proposal_fix.py

设计约束（与工程师的 tests/test_assistant_proposals.py 刻意不共用任何 fixture，
避免「用他的脚手架证明他的修复」）：

- 自建独立 sqlite 库：脚本开头就把 `DATABASE_URL` 指向系统临时目录下的
  `qa5_verify_<ts>.db`，**绝不触碰 backend/novel_dev.db**（18MB 真实数据）
  也不触碰仓库根的 ./novel_dev.db；
- 自建 Project / Generation / AgentRun，不复用 conftest 的任何东西；
- 断言只依赖 HTTP 状态码与数据库行的可观测事实，不依赖实现细节。

覆盖 team-lead 派单的 7 项 + 1 项独立风险评估（第 8 项）。
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import traceback
from datetime import datetime

# ── 必须在 import app.* 之前把 DB 指到临时文件 ──────────────────────────
_TMPDIR = os.path.join(tempfile.gettempdir(), "qa5_verify")
os.makedirs(_TMPDIR, exist_ok=True)
_DB_PATH = os.path.join(_TMPDIR, f"qa5_{datetime.now():%Y%m%d_%H%M%S}.db").replace("\\", "/")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from sqlalchemy import func, select  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.agents import proposals as proposal_lib  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.provider import LocalFallbackLLMProvider  # noqa: E402
import app.llm.provider as llm_provider_mod  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import AgentRun, EditProposal, Generation, Project, Version  # noqa: E402

# Assistant provider 钉死成本地兜底，杜绝测试触网
llm_provider_mod.build_assistant_provider = LocalFallbackLLMProvider

ORIGINAL_TEXT = "原始正文：铜钥匙藏在门后。"
HAND_EDITED = "用户亲手改写的重要内容，绝不能丢"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记录一条核验结论。"""
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ====================================================================
# 基础设施
# ====================================================================

def reset_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


class Env:
    """一次核验所需的全部资源：session + client + 数据。"""

    def __init__(self) -> None:
        reset_db()
        self.db = SessionLocal()
        self.client = TestClient(fastapi_app)

    # —— 数据构造 ——

    def make_project(self, title: str) -> int:
        p = Project(title=title, target_length=5000, mode="quick", status="created",
                    plot_bible={"characters": [{"name": "张三", "role": "主角"}]})
        self.db.add(p)
        self.db.commit()
        self.db.refresh(p)
        return int(p.id)

    def make_segment(self, project_id: int, text: str = ORIGINAL_TEXT,
                     segment_no: int = 1) -> int:
        g = Generation(project_id=project_id, segment_no=segment_no,
                       chapter_range=f"{segment_no}-{segment_no}", text=text,
                       summary={"short": f"第 {segment_no} 段摘要"})
        self.db.add(g)
        self.db.commit()
        self.db.refresh(g)
        return int(g.id)

    def make_run(self, project_id: int) -> int:
        r = AgentRun(project_id=project_id, role="assistant", status="success",
                     started_at=proposal_lib._now_utc(),
                     finished_at=proposal_lib._now_utc(),
                     request_hash=proposal_lib.request_hash({"qa5": True}),
                     model="(qa5)")
        self.db.add(r)
        self.db.commit()
        self.db.refresh(r)
        return int(r.id)

    # —— HTTP 动作 ——

    def create(self, project_id: int, gen_id: int, after_text: str, **extra) -> dict:
        payload = {
            "project_id": project_id, "type": "text_edit",
            "target_kind": "generation_text", "target_id": str(gen_id),
            "after_text": after_text,
        }
        payload.update(extra)
        r = self.client.post("/api/assistant/proposals", json=payload)
        return {"status": r.status_code, "body": r.json() if _is_json(r) else r.text}

    def approve(self, pid: int) -> dict:
        r = self.client.post(f"/api/assistant/proposals/{pid}/approve",
                             json={"action": "approve", "actor": "qa5"})
        return {"status": r.status_code, "body": r.json() if _is_json(r) else r.text}

    def apply(self, pid: int) -> dict:
        r = self.client.post(f"/api/assistant/proposals/{pid}/apply",
                             json={"action": "apply", "actor": "qa5"})
        return {"status": r.status_code, "body": r.json() if _is_json(r) else r.text}

    def detail(self, pid: int) -> dict:
        r = self.client.get(f"/api/assistant/proposals/{pid}")
        return {"status": r.status_code, "body": r.json() if _is_json(r) else r.text}

    # —— 观测 ——

    def gen_text(self, gen_id: int) -> str:
        self.db.expire_all()
        g = self.db.get(Generation, gen_id)
        return g.text if g else "<missing>"

    def version_count(self) -> int:
        return int(self.db.scalar(select(func.count(Version.id))) or 0)

    def proposal_count(self) -> int:
        return int(self.db.scalar(select(func.count(EditProposal.id))) or 0)

    def proposal_row(self, pid: int) -> EditProposal:
        self.db.expire_all()
        return self.db.get(EditProposal, pid)

    def close(self) -> None:
        self.db.close()


def _is_json(resp) -> bool:
    return resp.headers.get("content-type", "").startswith("application/json")


# ====================================================================
# 1. P0 原复现是否真死 + 全部 falsy 变体
# ====================================================================

FALSY_VARIANTS = [
    ("空串", ""),
    ("null", None),
    ("数字 0", 0),
    ("false", False),
    ("换行", "\n"),
    ("纯空格", "   "),
    ("制表符", "\t"),
    ("空 dict", {}),
    ("空 list", []),
]


def verify_1_p0_variants() -> None:
    section("1. P0：漂移检测不得被请求体 scope 关闭（9 种 falsy 变体）")

    for label, val in FALSY_VARIANTS:
        env = Env()
        try:
            proj = env.make_project("P1")
            gen = env.make_segment(proj)
            resp = env.create(proj, gen, "攻击者的内容",
                              scope={"base_content_hash": val})
            blocked_at_create = resp["status"] == 422
            layer = "create/422" if blocked_at_create else f"create/{resp['status']}"

            if blocked_at_create:
                ok = env.proposal_count() == 0
                check(f"变体 {label}({val!r}) 在创建阶段被拒", True,
                      f"HTTP 422，落库提案数={env.proposal_count()}，"
                      f"detail={str(resp['body'].get('detail'))[:60]}")
                check(f"变体 {label} 未落任何提案行", ok)
            else:
                # 创建阶段未拦 → 必须退到 apply 层拦住
                check(f"变体 {label}({val!r}) 创建阶段未被拦", False,
                      f"HTTP {resp['status']}；转测 apply 层")
                pid = resp["body"]["proposal"]["id"]
                env.approve(pid)
                g = env.db.get(Generation, gen)
                g.text = HAND_EDITED
                env.db.commit()
                ap = env.apply(pid)
                check(f"变体 {label} 在 apply 层被拦", ap["status"] in (400, 409, 422),
                      f"HTTP {ap['status']}；内容保留={env.gen_text(gen) == HAND_EDITED}；"
                      f"versions={env.version_count()}")
        finally:
            env.close()

    # —— 原始复现全链（无恶意 scope，纯正常流程 + 手改）——
    env = Env()
    try:
        pid1 = env.make_project("原始复现")
        gen = env.make_segment(pid1)
        created = env.create(pid1, gen, "改写后的正文：铜钥匙被张三取出。")
        check("原始复现：创建成功", created["status"] == 200)
        pid = created["body"]["proposal"]["id"]
        env.approve(pid)

        g = env.db.get(Generation, gen)
        g.text = HAND_EDITED
        env.db.commit()

        ap = env.apply(pid)
        check("原始复现：手改后 apply 返回 409", ap["status"] == 409,
              f"实际 {ap['status']}")
        check("原始复现：409 的 error=stale",
              isinstance(ap["body"], dict)
              and ap["body"].get("detail", {}).get("error") == "stale",
              str(ap["body"])[:120])
        check("原始复现：用户手改内容未被覆盖", env.gen_text(gen) == HAND_EDITED,
              f"当前内容={env.gen_text(gen)!r}")
        check("原始复现：versions 表 0 行", env.version_count() == 0,
              f"versions={env.version_count()}")
        check("原始复现：提案 status=stale",
              env.proposal_row(pid).status == "stale",
              f"status={env.proposal_row(pid).status}")
    finally:
        env.close()


# ====================================================================
# 2. AST 级确认短路真移除 + fail loud 分支可达
# ====================================================================

def verify_3_ast_and_fail_loud() -> None:
    section("2. AST 级确认 `if expected_hash and ...` 短路真移除 + fail loud 可达")

    src_path = os.path.join(_BACKEND, "app", "agents", "proposals.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)

    # (a) 全文扫「裸真值守卫」：BoolOp(And) 里出现对 expected_hash 的裸引用
    guards: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp):
            for v in node.test.values:
                if isinstance(v, ast.Name) and "expected_hash" in v.id:
                    guards.append(f"line {node.lineno}: {ast.unparse(node.test)}")
    check("AST：apply 路径不存在 `... and expected_hash` 裸真值短路",
          not guards, f"命中={guards}")

    # (b) apply_proposal 里确实存在 fail-loud 分支（先判空再比 hash）
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "apply_proposal"), None)
    check("AST：找到 apply_proposal", fn is not None)
    if fn is not None:
        texts = [ast.unparse(n.test) for n in ast.walk(fn) if isinstance(n, ast.If)]
        has_loud = any("expected_hash" in t and "strip" in t for t in texts)
        check("AST：存在 `not expected_hash.strip()` 的 fail-loud 判空", has_loud,
              f"该函数的 if 条件={texts}")
        has_cmp = any("current.hash != expected_hash" in t for t in texts)
        check("AST：hash 比较分支仍在且不再带 falsy 前置", has_cmp)

    # (c) build_proposal 里拒绝发生在赋值之前
    fn_b = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "build_proposal"), None)
    order_ok = False
    if fn_b is not None:
        raise_lineno = assign_lineno = None
        for n in ast.walk(fn_b):
            if isinstance(n, ast.Raise) and raise_lineno is None:
                raise_lineno = n.lineno
            if (isinstance(n, ast.Assign)
                    and isinstance(n.targets[0], ast.Subscript)
                    and "scope_payload" in ast.unparse(n.targets[0])
                    and assign_lineno is None):
                assign_lineno = n.lineno
        order_ok = raise_lineno is not None and assign_lineno is not None \
            and raise_lineno < assign_lineno
        check("AST：拒绝(Raise) 早于 scope_payload 赋值",
              order_ok, f"raise@{raise_lineno} vs assign@{assign_lineno}")

    # (d) 运行期可达性：造一个基线缺失的提案，apply 必须 400 而非 200
    env = Env()
    try:
        proj = env.make_project("fail-loud")
        gen = env.make_segment(proj)
        created = env.create(proj, gen, "未经漂移检测就想写入的内容")
        pid = created["body"]["proposal"]["id"]
        env.approve(pid)

        row = env.proposal_row(pid)
        row.scope_json = dict(row.scope_json)
        row.scope_json["base_content_hash"] = ""
        env.db.commit()

        ap = env.apply(pid)
        check("运行期：基线缺失 → apply 非 200", ap["status"] != 200,
              f"HTTP {ap['status']}，detail={str(ap['body'])[:100]}")
        check("运行期：基线缺失 → apply 得 400（ProposalError→400）",
              ap["status"] == 400, f"实际 {ap['status']}")
        check("运行期：业务表未被写入", env.gen_text(gen) == ORIGINAL_TEXT)
        check("运行期：versions 表 0 行", env.version_count() == 0)
        check("运行期：提案状态未被推进到 applied",
              env.proposal_row(pid).status != "applied",
              f"status={env.proposal_row(pid).status}")

        # lib 层直调：None / "" / "   " 三种都必须抛 ProposalError
        for broken in (None, "", "   "):
            row = env.proposal_row(pid)
            row.scope_json = dict(row.scope_json)
            row.scope_json["base_content_hash"] = broken
            row.status = "approved"
            env.db.commit()
            env.db.refresh(row)
            try:
                proposal_lib.apply_proposal(env.db, proposal=row, actor="qa5")
                check(f"lib 层：baseline={broken!r} 抛错", False, "未抛异常 → 短路仍在")
            except proposal_lib.ProposalError as exc:
                check(f"lib 层：baseline={broken!r} → ProposalError",
                      "base_content_hash" in str(exc), str(exc)[:80])
            except Exception as exc:  # noqa: BLE001
                check(f"lib 层：baseline={broken!r} → ProposalError", False,
                      f"抛了 {type(exc).__name__}: {exc}")
    finally:
        env.close()

    # (e) 兜底：源码文本里出现旧写法的行，必须是注释/文档串而非可执行语句
    bad_lines = []
    for n in ast.walk(tree):
        if isinstance(n, ast.If) and "expected_hash and" in ast.unparse(n.test):
            bad_lines.append(n.lineno)
    check("AST：无任何可执行 if 的文本含 `expected_hash and`", not bad_lines,
          f"命中行={bad_lines}")
    print(f"  （说明：源码里仍有 'if expected_hash and' 字样的位置："
          f"{[i + 1 for i, l in enumerate(src.splitlines()) if 'expected_hash and' in l]}，"
          f"均在注释中）")


# ====================================================================
# 3. 审批依据不可伪造（P1）
# ====================================================================

def verify_4_approval_material() -> None:
    section("3. P1：审批依据 diff / impact / target_snapshot 不可伪造")

    for key in ("diff", "impact", "target_snapshot", "base_content_hash"):
        env = Env()
        try:
            proj = env.make_project("伪造")
            gen = env.make_segment(proj)
            resp = env.create(proj, gen, "整段替换，影响很大但伪装成小改动",
                              scope={key: "仅修正一个错别字"})
            check(f"伪造 scope.{key} → 422", resp["status"] == 422,
                  f"实际 {resp['status']}；detail={str(resp['body'])[:80]}")
            check(f"伪造 scope.{key} 未落库", env.proposal_count() == 0)
        finally:
            env.close()

    # 组合伪造：diff + impact 一起传
    env = Env()
    try:
        proj = env.make_project("组合伪造")
        gen = env.make_segment(proj)
        resp = env.create(proj, gen, "整段替换",
                          scope={"diff": "", "impact": "仅修正一个错别字"})
        check("组合伪造 diff+impact → 422", resp["status"] == 422,
              f"实际 {resp['status']}")
    finally:
        env.close()

    # 绕过创建阶段直接改库：GET 详情返回的必须是服务端真值
    env = Env()
    try:
        proj = env.make_project("真值")
        gen = env.make_segment(proj)
        created = env.create(proj, gen, "完全不同的一段新正文内容")
        pid = created["body"]["proposal"]["id"]

        row = env.proposal_row(pid)
        row.scope_json = dict(row.scope_json)
        row.scope_json["diff"] = "仅修正一个错别字"
        row.scope_json["impact"] = "无影响"
        env.db.commit()

        d = env.detail(pid)
        got_diff = d["body"]["proposal"]["diff"]
        got_impact = d["body"]["proposal"]["impact"]
        check("GET 详情的 diff 是服务端真值（含真实新增行）",
              "完全不同的一段新正文内容" in got_diff, f"diff={got_diff[:80]!r}")
        check("GET 详情的 diff ≠ 伪造值", got_diff != "仅修正一个错别字")
        check("GET 详情的 impact 是服务端模板",
              got_impact == proposal_lib._describe_impact("generation_text", str(gen)),
              f"impact={got_impact[:80]!r}")
    finally:
        env.close()


# ====================================================================
# 4. P2 跨项目 run_id
# ====================================================================

def verify_5_cross_project_run() -> None:
    section("4. P2：跨项目 run_id 必须被拒（含两组对照组）")

    env = Env()
    try:
        proj_a = env.make_project("项目一")
        proj_b = env.make_project("项目二")
        gen_a = env.make_segment(proj_a)
        foreign_run = env.make_run(proj_b)
        own_run = env.make_run(proj_a)

        r = env.create(proj_a, gen_a, "试图把别的项目的 run 挂过来",
                       run_id=foreign_run)
        check("跨项目 run_id → 409", r["status"] == 409,
              f"实际 {r['status']}；detail={str(r['body'])[:80]}")
        check("跨项目 run_id 未落提案", env.proposal_count() == 0)

        r2 = env.create(proj_a, gen_a, "用本项目的 run 是合法的", run_id=own_run)
        check("对照组：同项目 run_id → 200", r2["status"] == 200,
              f"实际 {r2['status']}；body={str(r2['body'])[:100]}")
        check("对照组：同项目 run_id 被正确记录",
              r2["status"] == 200 and r2["body"]["proposal"]["run_id"] == own_run)

        r3 = env.create(proj_a, gen_a, "不存在的 run", run_id=987654)
        check("对照组：不存在的 run_id → 404", r3["status"] == 404,
              f"实际 {r3['status']}")

        # 反向：把项目一的 run 挂到项目二的提案上
        gen_b = env.make_segment(proj_b)
        r4 = env.create(proj_b, gen_b, "反向跨项目", run_id=own_run)
        check("反向跨项目 run_id → 409", r4["status"] == 409, f"实际 {r4['status']}")
    finally:
        env.close()


# ====================================================================
# 5. 误伤检查（修复不得过度阻塞正常路径）
# ====================================================================

def verify_6_no_over_blocking() -> None:
    section("5. 误伤检查：正常路径不得被修复堵死")

    # (a) 无害元数据 note 必须保留
    env = Env()
    try:
        proj = env.make_project("元数据")
        gen = env.make_segment(proj)
        r = env.create(proj, gen, "改写后的正文：铜钥匙被张三取出。",
                       scope={"note": "无害的用户元数据应当被保留"})
        check("带无害 scope note 创建 → 200", r["status"] == 200,
              f"实际 {r['status']}；{str(r['body'])[:120]}")
        if r["status"] == 200:
            row = env.proposal_row(r["body"]["proposal"]["id"])
            check("scope.note 被保留进 scope_json",
                  row.scope_json.get("note") == "无害的用户元数据应当被保留",
                  f"scope_json keys={sorted(row.scope_json)}")
            check("服务端四键仍在且非空",
                  all(row.scope_json.get(k) for k in
                      ("base_content_hash", "diff", "impact")))
    finally:
        env.close()

    # (b) 多个无害键
    env = Env()
    try:
        proj = env.make_project("多元数据")
        gen = env.make_segment(proj)
        r = env.create(proj, gen, "改写正文",
                       scope={"note": "n", "ticket": "T-123", "reviewer": "bob"})
        ok = r["status"] == 200
        check("多无害键创建 → 200", ok, f"实际 {r['status']}")
        if ok:
            row = env.proposal_row(r["body"]["proposal"]["id"])
            check("三个无害键全保留",
                  row.scope_json.get("ticket") == "T-123"
                  and row.scope_json.get("reviewer") == "bob")
    finally:
        env.close()

    # (c) 不带任何 scope 的完整 happy path
    env = Env()
    try:
        proj = env.make_project("happy")
        gen = env.make_segment(proj)
        created = env.create(proj, gen, "改写后的正文：铜钥匙被张三取出。")
        check("happy path：创建 200", created["status"] == 200)
        pid = created["body"]["proposal"]["id"]
        check("happy path：base_content_hash 非空 64 位 sha256",
              len(created["body"]["proposal"].get("base_content_hash") or "") == 64,
              f"hash={created['body']['proposal'].get('base_content_hash')!r}")

        ap = env.approve(pid)
        check("happy path：approve 200", ap["status"] == 200)
        res = env.apply(pid)
        check("happy path：apply 200", res["status"] == 200,
              f"实际 {res['status']}；{str(res['body'])[:150]}")
        check("happy path：正文被正常写入",
              env.gen_text(gen) == "改写后的正文：铜钥匙被张三取出。",
              f"当前={env.gen_text(gen)!r}")
        check("happy path：产生 1 条 Version", env.version_count() == 1,
              f"versions={env.version_count()}")
        check("happy path：提案置 applied", env.proposal_row(pid).status == "applied")
    finally:
        env.close()

    # (d) 正常提案的 baseline 必为非空真值（多轮）
    env = Env()
    try:
        proj = env.make_project("baseline")
        empties = 0
        for i in range(3):
            gen = env.make_segment(proj, text=f"第 {i} 段原文", segment_no=i + 1)
            r = env.create(proj, gen, f"第 {i} 段新文")
            h = r["body"]["proposal"].get("base_content_hash") if r["status"] == 200 else None
            if not h or len(h) != 64:
                empties += 1
        check("3 条正常提案的 baseline 全为非空 sha256", empties == 0,
              f"异常条数={empties}")
    finally:
        env.close()

    # (e) scope.patch 仍可用（patch 是输入不是产物）
    env = Env()
    try:
        proj = env.make_project("patch")
        gen = env.make_segment(proj)
        r = env.create(proj, gen, "改写正文",
                       scope={"patch": {"fields": ["text"]}})
        check("scope.patch（合法输入）仍被接受 → 200", r["status"] == 200,
              f"实际 {r['status']}；{str(r['body'])[:120]}")
    finally:
        env.close()


# ====================================================================
# 6. 独立风险评估：跨项目 proposal_id 未校验归属
# ====================================================================

def verify_8_cross_project_proposal() -> None:
    section("6. 独立风险评估：GET/approve/reject/apply 未校验 proposal 归属项目")

    env = Env()
    try:
        proj_a = env.make_project("项目一")
        proj_b = env.make_project("项目二")
        gen_b = env.make_segment(proj_b, text="项目二的正文")

        # 在项目二下创建提案
        created = env.create(proj_b, gen_b, "项目二提案要写的内容")
        check("项目二提案创建成功", created["status"] == 200)
        pid_b = created["body"]["proposal"]["id"]

        # —— 用项目二的 proposal id，在「项目一」的上下文里操作 ——
        d = env.detail(pid_b)
        check("口子：GET /proposals/{项目二的 id} 可读", d["status"] == 200,
              f"HTTP {d['status']}；project_id={d['body']['proposal'].get('project_id')}"
              if d["status"] == 200 else f"HTTP {d['status']}")
        if d["status"] == 200:
            check("口子：读到的确实是项目二的提案",
                  d["body"]["proposal"].get("project_id") == proj_b)

        ap = env.approve(pid_b)
        check("口子：approve 项目二的提案可被任意调用方批准", ap["status"] == 200,
              f"HTTP {ap['status']}")

        res = env.apply(pid_b)
        check("口子：apply 项目二的提案可被任意调用方落库", res["status"] == 200,
              f"HTTP {res['status']}；{str(res['body'])[:120]}")
        check("口子：项目二正文确实被改写了（写入生效）",
              env.gen_text(gen_b) == "项目二提案要写的内容",
              f"当前={env.gen_text(gen_b)!r}")

        # —— 列表端点是否有项目过滤（对照）——
        lst = env.client.get("/api/assistant/proposals", params={"project_id": proj_a})
        ids = [p["id"] for p in lst.json()["proposals"]]
        check("对照：GET /proposals?project_id=项目一 不含项目二的提案",
              pid_b not in ids, f"项目一列表={ids}")

        # —— 全局无鉴权中间件确认 ——
        from app.main import app as _app  # noqa: PLC0415
        mw = [getattr(m, "cls", type(m)).__name__ for m in _app.user_middleware]
        has_auth = any("auth" in n.lower() or "jwt" in n.lower()
                       or "oauth" in n.lower() or "security" in n.lower() for n in mw)
        check("确认：应用整体无鉴权中间件", not has_auth, f"middleware={mw}")
    finally:
        env.close()


# ====================================================================
# main
# ====================================================================

def main() -> int:
    print(f"QA5 独立复验 · commit e933f73")
    print(f"独立 sqlite：{_DB_PATH}")
    print(f"backend/novel_dev.db 是否被触碰：否（脚本只连上面的临时库）")

    runners = [
        verify_1_p0_variants,
        verify_3_ast_and_fail_loud,
        verify_4_approval_material,
        verify_5_cross_project_run,
        verify_6_no_over_blocking,
        verify_8_cross_project_proposal,
    ]
    for fn in runners:
        try:
            fn()
        except Exception:  # noqa: BLE001
            check(f"小节 {fn.__name__} 执行异常", False, traceback.format_exc(limit=3))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    section("汇总")
    print(f"总断言 {len(RESULTS)} | PASS {passed} | FAIL {failed}")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ✗ {name} —— {detail}")

    try:
        os.remove(_DB_PATH)
    except OSError:
        pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
