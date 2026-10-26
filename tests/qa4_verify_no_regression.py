"""QA4 第三段：Writer 链路零回归 + Assistant 仍无写权限。

跑法：python tests/qa4_verify_no_regression.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa4_verify_harness import (  # noqa: E402
    QA4_DB,
    Report,
    SessionLocal,
    init_db,
    make_client,
    reset_db,
    seed_generation,
    seed_project,
)
from sqlalchemy import text  # noqa: E402

REPORT = Report()
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
init_db()

#: 本 commit 声称未改的模块 —— 必须零 diff。
UNTOUCHED = [
    "backend/app/generation",
    "backend/app/llm",
    "backend/app/routers/generation.py",
    "backend/app/routers/assistant.py",
    "backend/app/generation/prompts.py",
]


def attack_D_writer_zero_regression() -> None:
    REPORT.section("D. Writer 链路零回归：git diff 确认未改 + provider 超时实测")
    out = subprocess.run(
        ["git", "diff", "HEAD~1", "--name-only"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
    )
    changed = [x.strip() for x in out.stdout.splitlines() if x.strip()]
    REPORT.note(f"本 commit 实际改动 {len(changed)} 个文件：{changed}")
    touched = [f for f in UNTOUCHED if any(c.startswith(f) or c == f for c in changed)]
    REPORT.check(
        "D1. generation / llm / routers/generation.py / routers/assistant.py 零改动",
        not touched,
        f"被改动的 Writer/只读模块={touched or '无'}"
        f"；实际改动仅限 {changed}",
    )

    # —— Writer provider 超时 ——
    # 直接实例化真实渠道类（与 tests/test_provider_timeouts.py 同口径），
    # 绕开 env 造成 LocalFallback 降级的不确定性；再单独用真实工厂路径复验。
    import urllib.request

    import app.llm.provider as llm_provider_mod
    from app.config import settings
    from app.llm.provider import ZhipuChatProvider, build_provider

    w = ZhipuChatProvider("fake-key", "https://fake.api", "fake-model")
    REPORT.note(f"Writer 渠道类实例：{type(w).__name__}")
    REPORT.check(
        "D2. Writer provider（ZhipuChatProvider）默认 chat timeout=120",
        w.timeout == 120,
        f"实际 w.timeout={w.timeout}（不变量，禁止与 stream 收敛）",
    )
    REPORT.check(
        "D3. Writer provider 默认 stream_timeout=180（c1a7179 基线不回退）",
        w.stream_timeout == 180,
        f"实际 w.stream_timeout={w.stream_timeout}",
    )

    # —— 真打一次调用，确认 urlopen 收到的 timeout 就是 120 / 180 ——
    captured: list = []

    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._b = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def read(self):
            return self._b

        def __iter__(self):
            return iter(self._b.splitlines(keepends=True))

    def _fake_urlopen(req, timeout=None, **_kw):
        captured.append(timeout)
        body = getattr(req, "data", b"") or b""
        if b'"stream": true' in body:
            return _Resp(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n')
        return _Resp(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

    orig_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen
    try:
        captured.clear()
        try:
            w.chat("sys", "user")
        except Exception:
            pass
        chat_seen = list(captured)
        captured.clear()
        try:
            list(w.stream_chat("sys", "user"))
        except Exception:
            pass
        stream_seen = list(captured)
    finally:
        urllib.request.urlopen = orig_urlopen

    REPORT.check(
        "D4. 实测 urlopen timeout：chat=120 / stream_chat=180（不只是属性值）",
        chat_seen == [120] and stream_seen == [180],
        f"chat 实测={chat_seen}（期望 [120]）；stream_chat 实测={stream_seen}（期望 [180]）",
    )

    # —— 真实工厂路径复验（临时把 settings 指到 zhipu + 假 key）——
    snap = (settings.llm_provider, settings.zhipu_api_key,
            settings.assistant_provider, settings.assistant_api_key)
    try:
        settings.llm_provider = "zhipu"
        settings.zhipu_api_key = "fake-key"
        llm_provider_mod.reset_provider_cache()
        wp2 = llm_provider_mod.get_llm_provider()
        REPORT.check(
            "D4b. 经真实工厂 get_llm_provider()（llm_provider=zhipu）得到 120/180",
            isinstance(wp2, ZhipuChatProvider) and wp2.timeout == 120 and wp2.stream_timeout == 180,
            f"工厂返回 {type(wp2).__name__} timeout={getattr(wp2,'timeout',None)} "
            f"stream_timeout={getattr(wp2,'stream_timeout',None)}",
        )

        settings.assistant_provider = "zhipu"
        settings.assistant_api_key = "fake-key"
        settings.assistant_model = "glm-4-flash"
        settings.assistant_base_url = "https://fake.api"
        llm_provider_mod.reset_assistant_provider_cache()
        ap = llm_provider_mod.build_assistant_provider()
        REPORT.check(
            "D6. 经真实工厂 build_assistant_provider() 得到独立 60/60（双轨隔离）",
            isinstance(ap, ZhipuChatProvider) and ap.timeout == 60 and ap.stream_timeout == 60,
            f"Assistant 工厂返回 {type(ap).__name__} timeout={getattr(ap,'timeout',None)} "
            f"stream_timeout={getattr(ap,'stream_timeout',None)}（与 Writer 120/180 不同）",
        )
    finally:
        (settings.llm_provider, settings.zhipu_api_key,
         settings.assistant_provider, settings.assistant_api_key) = snap
        llm_provider_mod.reset_provider_cache()
        llm_provider_mod.reset_assistant_provider_cache()

    cfg = settings.get_provider_config("writer")
    acfg = settings.get_provider_config("assistant")
    REPORT.check(
        "D5. writer_timeout 配置层=120，与 assistant_timeout 相互独立",
        cfg["timeout"] == 120 and acfg["timeout"] != cfg["timeout"],
        f"writer={cfg['timeout']} assistant={acfg['timeout']}",
    )

    # —— build_provider 默认参数（未传时保持 120/180）——
    bp = build_provider("zhipu", "fake-key", "glm-4-flash", "https://fake.api")
    REPORT.check(
        "D7. build_provider 默认 timeout=120 / stream_timeout=180（b58c763 口径）",
        bp.timeout == 120 and bp.stream_timeout == 180,
        f"build_provider(默认) → timeout={bp.timeout} stream_timeout={bp.stream_timeout}",
    )
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()


def attack_E_assistant_readonly() -> None:
    REPORT.section("E. Assistant 零写权限：工具白名单 + diagnose 响应不泄漏写通道 token")
    from app.agents.registry import FORBIDDEN_PREFIXES, get_tool_registry, reset_tool_registry

    reset_tool_registry()
    reg = get_tool_registry()
    names = reg.names()
    REPORT.note(f"Assistant 工具白名单（{len(names)} 个）：{names}")

    bad_prefixes = ("write_", "commit_", "patch_", "rollback_", "apply_", "update_", "delete_")
    offenders = [n for n in names if n.startswith(bad_prefixes)]
    REPORT.check(
        "E1. 工具白名单无任何 write_/commit_/patch_/rollback_/apply_/update_/delete_ 前缀工具",
        not offenders,
        f"越权工具={offenders or '无'}；实际 {len(names)} 个工具全部只读",
    )
    non_readonly = [n for n in names if reg.resolve(n).read_only is not True]
    REPORT.check(
        "E2. 全部工具 read_only=True",
        not non_readonly,
        f"非只读工具={non_readonly or '无'}",
    )
    REPORT.note(f"FORBIDDEN_PREFIXES = {FORBIDDEN_PREFIXES}")

    # 尝试注册一个写工具 —— 必须 PermissionError
    from app.agents.registry import ToolSpec

    blocked = []
    for name in ("write_generation_text", "commit_proposal", "patch_target", "rollback_version",
                 "apply_proposal_x", "delete_project", "update_plot_bible"):
        try:
            reg.register(ToolSpec(name=name, description="攻击用", callable=lambda **k: {},
                                  read_only=True))
            blocked.append((name, "REGISTERED(!!)"))
        except PermissionError:
            blocked.append((name, "PermissionError"))
        except Exception as exc:
            blocked.append((name, f"{type(exc).__name__}"))
    REPORT.check(
        "E3. 7 个写工具名全部被registry 拒绝（PermissionError）",
        all(x[1] == "PermissionError" for x in blocked),
        f"{blocked}",
    )

    # invoke 也必须拦
    invoke_blocked = []
    for name in ("write_generation_text", "apply_proposal"):
        try:
            reg.invoke(name, db=None, project_id=1)
            invoke_blocked.append((name, "INVOKED(!!)"))
        except PermissionError:
            invoke_blocked.append((name, "PermissionError"))
        except KeyError:
            invoke_blocked.append((name, "KeyError(未注册)"))
        except Exception as exc:
            invoke_blocked.append((name, type(exc).__name__))
    REPORT.check(
        "E4. invoke() 也拦得住写工具名",
        all(x[1] in ("PermissionError", "KeyError(未注册)") for x in invoke_blocked),
        f"{invoke_blocked}",
    )

    # —— diagnose 响应不泄漏写通道 token ——
    reset_db()
    import app.llm.provider as llm_provider_mod
    from app.llm.provider import LocalFallbackLLMProvider

    orig = llm_provider_mod.build_assistant_provider
    llm_provider_mod.build_assistant_provider = LocalFallbackLLMProvider
    db = SessionLocal()
    proj = seed_project(db, "E-diagnose 泄漏检查")
    seed_generation(db, proj.id, "正文。", segment_no=1)
    db.close()
    client = make_client()

    r = client.post("/api/assistant/diagnose", json={"project_id": proj.id, "scope": "all"})
    body_text = json.dumps(r.json(), ensure_ascii=False)
    forbidden = ["patch_json", "proposal_id", "approve", "apply", "restore", "rolled_back",
                 "base_content_hash", "after_text", "write_generation", "commit_", "rollback_",
                 "EditProposal", "edit_proposals"]
    leaked = [t for t in forbidden if t in body_text]
    REPORT.check(
        "E5. /api/assistant/diagnose 响应不泄漏任何写通道 token",
        r.status_code == 200 and not leaked,
        f"code={r.status_code}；泄漏 token={leaked or '无'}；"
        f"响应顶层键={sorted(r.json().keys())}",
    )

    q = client.post("/api/assistant/qa", json={"project_id": proj.id, "query": "铜钥匙是谁的？"})
    q_text = json.dumps(q.json(), ensure_ascii=False)
    q_leaked = [t for t in forbidden if t in q_text]
    REPORT.check(
        "E6. /api/assistant/qa 响应同样不泄漏写通道 token",
        q.status_code == 200 and not q_leaked,
        f"code={q.status_code}；泄漏={q_leaked or '无'}",
    )
    llm_provider_mod.build_assistant_provider = orig

    # —— diagnose 只读：跑完之后业务表零变化 ——
    from qa4_verify_harness import BUSINESS_TABLES, business_unchanged, snapshot_all

    db = SessionLocal()
    before = snapshot_all(db)
    db.close()
    client.post("/api/assistant/diagnose", json={"project_id": proj.id, "scope": "all"})
    client.post("/api/assistant/qa", json={"project_id": proj.id, "query": "再说一次"})
    db = SessionLocal()
    after = snapshot_all(db)
    ok, ch = business_unchanged(before, after)
    REPORT.check(
        "E7. 调 diagnose / qa 之后业务表零变化（真只读）",
        ok,
        f"变化的业务表={ch or '无'}；edit_proposals {before['edit_proposals']['rows']}→{after['edit_proposals']['rows']}；"
        f"versions {before['versions']['rows']}→{after['versions']['rows']}",
    )
    db.close()


def attack_F_full_pytest() -> None:
    REPORT.section("F. 全量 pytest：确认 112 passed 且四个基线套件无回归")
    # pytest.ini 已带 `-ra -q`；命令行再传 -q 会变成 -qq，**抑制末尾汇总行**。
    # 所以这里只补 `-p no:cacheprovider`，并自行数点 + 解析 collect 计数。
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--no-header", "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=900,
    )
    # 汇总行形如 "112 passed, 1 warning in 199.65s"（pytest.ini 的 -q 会保留它；
    # 但命令行再传 -q 变 -qq 就会抑制——所以不传）。
    import re

    m = re.search(r"(\d+) passed", out.stdout)
    n_pass = int(m.group(1)) if m else 0
    m_fail = re.search(r"(\d+) (failed|error)", out.stdout)
    n_fail = int(m_fail.group(1)) if m_fail else 0
    tail = [l for l in out.stdout.strip().splitlines() if l.strip()][-3:]
    REPORT.note("pytest 输出（末 3 行）：")
    for line in tail:
        REPORT.note(f"    {line}")
    REPORT.check(
        "F1. pytest 全量 112 passed / 0 failed（exit=0）",
        out.returncode == 0 and n_fail == 0 and n_pass == 112,
        f"returncode={out.returncode}；汇总行 '{tail[-1] if tail else 'N/A'}'；"
        f"解析通过={n_pass}（期望 112）失败/错误={n_fail}（期望 0）",
    )

    # 分文件计数：`--collect-only`（不带额外 -q）输出 node id，按 `::` 归属文件
    per = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "--no-header", "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=600,
    )
    counts: dict[str, int] = {}
    for line in per.stdout.splitlines():
        if "::" in line:
            f = line.split("::", 1)[0].strip()
            counts[f] = counts.get(f, 0) + 1
    REPORT.note(f"各文件收集到的用例数：{counts}")
    REPORT.check(
        "F1b. 全量收集总数=112（55 基线 + 57 新增）",
        sum(counts.values()) == 112,
        f"合计={sum(counts.values())}；明细={counts}",
    )
    for fname, expect in (
        ("tests/test_assistant_diagnose.py", 31),
        ("tests/test_assistant_readonly.py", 11),
        ("tests/test_provider_registry.py", 12),
        ("tests/test_provider_timeouts.py", 1),
        ("tests/test_assistant_proposals.py", 57),
    ):
        got = counts.get(fname, 0)
        REPORT.check(
            f"F2. {os.path.basename(fname)} 收集到 {expect} 项（基线不回退）",
            got == expect,
            f"实际={got}",
        )


if __name__ == "__main__":
    print(f"QA4 独立验证库：{QA4_DB}")
    attack_D_writer_zero_regression()
    attack_E_assistant_readonly()
    attack_F_full_pytest()
    print(REPORT.summary())
    sys.exit(1 if REPORT.failed else 0)