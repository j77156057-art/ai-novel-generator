"""QA6 · 前端助手面板「真连后端」运行时验证（commit 30f4508）。

目的：工程师只做了 typecheck + build（静态验证），本脚本补齐**运行时**证据。

做法：
1. 起一个真实 uvicorn（backend/app.main:app），DATABASE_URL 指向临时 sqlite，
   绝不碰仓库里 backend/novel_dev.db（18MB 真实数据）；
2. 用**前端 client.ts / AssistantPanel.vue 实际会发出的同样 payload**打后端，
   走完 qa → diagnose → 创建提案 → approve → apply → 漂移 409 → restore 全链路；
3. 把 TS 侧 `request()` 的错误解析逻辑原样移植到 Python，验证漂移 409 的结构化
   detail 一路到 ApiError.detail 都**没有退化成 "[object Object]"**；
4. 顺带做一批「契约负向用例」：拼错字段 / 多传服务端独占键 / action 与路径不一致
   —— 全部期望 422（后端 StrictRequest extra="forbid"）。

运行：python tests/qa6_assistant_e2e_runtime.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

# 必须在 import app.* 之前设置：app.db 在 import 期就读取 settings.database_url
TMP_DB = Path(tempfile.gettempdir()) / f"qa6_assistant_{int(time.time())}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TMP_DB.as_posix()}"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
STEP_LOG: list[str] = []


def step(title: str) -> None:
    print(f"\n{'=' * 78}\n### {title}\n{'=' * 78}")
    STEP_LOG.append(title)


def check(name: str, cond: bool, evidence: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ← {evidence}" if evidence else ""))
    (PASS if cond else FAIL).append(name)


# ==================================================================
# TS 侧 request() 的错误解析逻辑（frontend/src/api/client.ts 第 66-84 行）
# ==================================================================
def ts_request_parse(status: int, text: str) -> tuple[str, object]:
    """client.ts `request()` 的错误分支移植。返回 (message, ApiError.detail)。

    detail 必须保持结构化：漂移 409 的 dict 不能被 String() 压成 "[object Object]"，
    否则 AssistantPanel.runAction 里的 `detail?.error === "stale"` 永远判不出来。
    """
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        data = text

    raw = data.get("detail") if isinstance(data, dict) and "detail" in data else None
    if raw is not None and isinstance(raw, dict) and "message" in raw:
        message = str(raw["message"])
    elif raw is not None:
        message = str(raw)
    else:
        message = text or f"HTTP {status}"
    # new ApiError(res.status, detail, raw === undefined ? text : raw)
    detail = text if raw is None else raw
    return message, detail


# ==================================================================
# 播种（不走 API：generation 没有直连创建端点，直接落库即可）
# ==================================================================
def seed() -> int:
    from app.db import SessionLocal, init_db
    from app.models import Fact, Generation, Project

    init_db()
    db = SessionLocal()
    try:
        proj = Project(
            title="QA6 助手面板运行时验证项目",
            genre="悬疑",
            style="知乎盐选",
            target_length=5000,
            status="created",
            created_at=datetime.now(timezone.utc),
            plot_bible={"characters": [{"name": "陈默", "role": "主角"}]},
        )
        db.add(proj)
        db.flush()
        for i, text in enumerate(
            [
                "第一章。陈默推开旧宅的门，灰尘在光柱里浮动。他记得十年前这里也是这样。",
                "第二章。雨下了一夜。陈默发现抽屉里的照片被人动过，边缘多了一道折痕。",
                "第三章。他还是-alias 出现在第三段；赵雪没有出现在本段。陈默决定回去查证。",
            ],
            start=1,
        ):
            db.add(
                Generation(
                    project_id=proj.id,
                    segment_no=i,
                    chapter_range=f"第{i}章",
                    text=text,
                    summary={"summary": f"第{i}段概要"},
                )
            )
        # 8 条 confidence=low 的事实 → 触发 low_confidence_facts_overflow
        # （阈值 LOW_CONFIDENCE_FACT_LIMIT=5）：用来验证 issues[] 真的有内容，
        # 以及「从 issue 起草提案」这条 UI 路径（from_issue）在后端确实被接受。
        for i in range(8):
            db.add(
                Fact(
                    project_id=proj.id,
                    type="character",
                    key=f"待裁决名字{i}",
                    props={"mentions": 1},
                    aliases=[f"别名{i}"],
                    source="generated",
                    confidence="low",
                    first_segment=1,
                )
            )
        db.commit()
        pid = int(proj.id)
        print(f"  seeded project_id={pid} + 3 generations + 8 low-confidence facts  (DB={TMP_DB})")
        return pid
    finally:
        db.close()


def start_server() -> subprocess.Popen:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{TMP_DB.as_posix()}"
    port = 8123
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("uvicorn 启动即退出：\n" + (proc.stdout.read() or ""))
        try:
            if httpx.get(f"{base}/health", timeout=2.0).status_code == 200:
                print(f"  uvicorn up on {base}")
                return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("uvicorn 90s 内未 ready")


def req(client: httpx.Client, method: str, path: str, **kw):
    """发请求并把 (status, json, raw_text) 打出来。"""
    r = client.request(method, path, **kw)
    body = r.text
    try:
        js = r.json()
    except Exception:
        js = None
    print(f"    {method} {path} -> {r.status_code}")
    if r.status_code >= 400 or js is None:
        print(f"      body: {body[:600]}")
    return r, js, body


def main() -> int:
    project_id = seed()
    proc = start_server()
    base = "http://127.0.0.1:8123"
    timeline: list[str] = []

    try:
        target_id = "1"  # 贯穿全程的那个 generations.id（text_edit 要求整数字符串）
        with httpx.Client(base_url=base, timeout=120.0) as c:
            # ---------------------------------------------------------
            step("a) 播种结果确认")
            _, pj, _ = req(c, "GET", f"/api/projects/{project_id}")
            check("project 可读", pj is not None and pj.get("id") == project_id)

            # ---------------------------------------------------------
            step("b) POST /api/assistant/qa —— 前端 payload {project_id, query, scope}")
            payload = {"project_id": project_id, "query": "陈默在第二段发现了什么？", "scope": None}
            print(f"    body={json.dumps(payload, ensure_ascii=False)}")
            r, js, body = req(c, "POST", "/api/assistant/qa", json=payload)
            check("HTTP 200", r.status_code == 200, f"got {r.status_code}")
            if js:
                for k in ("answer", "evidence", "confidence", "agent_run_id"):
                    check(f"响应含 {k}", k in js, f"{k}={str(js.get(k))[:80]}")
                check("evidence 是 list", isinstance(js.get("evidence"), list),
                      f"{len(js.get('evidence') or [])} 条")
                if js.get("evidence"):
                    ev = js["evidence"][0]
                    check("evidence 项含 ref_id/snippet/score/kind/source",
                          all(k in ev for k in ("ref_id", "snippet", "score", "kind", "source")),
                          json.dumps(ev, ensure_ascii=False)[:160])
                timeline.append(f"qa -> run #{js.get('agent_run_id')}, confidence={js.get('confidence')}")
                qa_run_id = js.get("agent_run_id")
            else:
                qa_run_id = None

            # ---------------------------------------------------------
            step("c) POST /api/assistant/diagnose —— 前端 payload {project_id, scope, query}")
            payload = {"project_id": project_id, "scope": "all", "query": None}
            print(f"    body={json.dumps(payload, ensure_ascii=False)}")
            r, js, body = req(c, "POST", "/api/assistant/diagnose", json=payload)
            check("HTTP 200", r.status_code == 200, f"got {r.status_code}")
            diag_run_id = None
            issue_ids: list[str] = []
            if js:
                check("响应含 issues[]", isinstance(js.get("issues"), list),
                      f"{len(js.get('issues') or [])} 条")
                check("响应含 evidence[]/confidence/agent_run_id",
                      all(k in js for k in ("evidence", "confidence", "agent_run_id")))
                diag_run_id = js.get("agent_run_id")
                for it in js.get("issues") or []:
                    issue_ids.append(it.get("id"))
                    need = ("id", "type", "severity", "title", "claim", "evidence", "suggestion", "rule")
                    check(f"issue {it.get('id')} 九字段齐全",
                          all(k in it for k in need),
                          f"缺少 {[k for k in need if k not in it]}")
                    check(f"issue {it.get('id')} severity 合法",
                          it.get("severity") in ("info", "warn", "error"), str(it.get("severity")))
                timeline.append(f"diagnose -> run #{diag_run_id}, issues={len(issue_ids)}")

            # ---------------------------------------------------------
            step("c-2) 从诊断 issue 起草提案 —— 前端 draftFrom() 的形状（from_issue + reason）")
            if not issue_ids:
                check("诊断至少命中 1 条 issue（用于走 from_issue 路径）", False, "issues=[]")
                p_issue = None
            else:
                print(f"    issue ids = {issue_ids}")
                # AssistantPanel.draftFrom()：type/text_edit + target_id(用户填) +
                # after_text(用户手敲，界面一律留空) + reason←issue.suggestion +
                # from_issue←issue.id + run_id←本次诊断的 agent_run_id
                issue_map = {it.get("id"): it for it in (js or {}).get("issues") or []}
                pick = issue_ids[0]
                payload = {
                    "project_id": project_id,
                    "type": "text_edit",
                    "target_kind": "generation_text",
                    "target_id": target_id,
                    "after_text": "【QA6 从 issue 起草】第三章。陈默翻出旧照片，背面写着一行褪色的字。",
                    "reason": issue_map[pick].get("suggestion"),
                    "scope": None,
                    "from_issue": pick,
                    "run_id": diag_run_id,
                }
                print(f"    from_issue={pick}  run_id={diag_run_id}")
                r, js_i, body = req(c, "POST", "/api/assistant/proposals", json=payload)
                p_issue = (js_i or {}).get("proposal")
                check("带 from_issue 创建提案 HTTP 200", r.status_code == 200,
                      f"got {r.status_code}; {body[:300]}")
                if p_issue:
                    check("reason 被 issue.suggestion 填充",
                          bool(p_issue.get("reason")),
                          str(p_issue.get("reason"))[:60])
                    check("issue 证据并入 evidence",
                          len(p_issue.get("evidence") or []) > 0,
                          f"{len(p_issue.get('evidence') or [])} 条")
                    check("run_id 关联到本次诊断",
                          p_issue.get("run_id") == diag_run_id, str(p_issue.get("run_id")))
                # issue id 不存在时必须 404（后端刻意 fail loud，不静默生成无根据提案）
                r404, _, b404 = req(c, "POST", "/api/assistant/proposals", json={
                    "project_id": project_id, "type": "text_edit",
                    "target_kind": "generation_text", "target_id": target_id,
                    "after_text": "随便一份内容", "scope": None, "from_issue": "不存在的issue",
                })
                check("from_issue 指向不存在的 issue → 404", r404.status_code == 404,
                      f"got {r404.status_code}; {b404[:160]}")

            # ---------------------------------------------------------
            step("d-1) POST /api/assistant/proposals —— 前端四必填 + reason + scope:null + run_id")
            # target_id 已在上文定义（= "1"）
            after_text = "【QA6 改写版】第一章。陈默推开旧宅的门，灰尘在光柱里浮动——十年前这里也一样。"
            payload = {
                "project_id": project_id,
                "type": "text_edit",
                "target_kind": "generation_text",
                "target_id": target_id,
                "after_text": after_text,
                "reason": "统一第一章的开场镜头",
                "scope": None,
                "run_id": diag_run_id or qa_run_id,
            }
            print(f"    body={json.dumps(payload, ensure_ascii=False)}")
            r, js, body = req(c, "POST", "/api/assistant/proposals", json=payload)
            check("HTTP 200", r.status_code == 200, f"got {r.status_code}；body={body[:300]}")
            p1 = (js or {}).get("proposal") or {}
            p1_id = p1.get("id")
            check("提案停在第 ③ 步 pending", p1.get("status") == "pending", str(p1.get("status")))
            check("next_action=approve", (js or {}).get("next_action") == "approve")
            check("创建时带回了 diff / impact / base_content_hash",
                  bool(p1.get("diff")) and bool(p1.get("impact")) and bool(p1.get("base_content_hash")),
                  f"hash={str(p1.get('base_content_hash'))[:16]}…")
            base_before = p1.get("before_text")
            check("before_text 是正文原内容",
                  isinstance(base_before, str) and "第一章" in base_before,
                  repr(str(base_before)[:40]))
            timeline.append(f"proposal #{p1_id} created ({p1.get('status')})")

            step("d-2) GET /api/assistant/proposals?project_id=&limit=100（列表）")
            r, js, _ = req(c, "GET", f"/api/assistant/proposals?project_id={project_id}&limit=100")
            check("HTTP 200", r.status_code == 200)
            check("列表含刚才那条", (js or {}).get("count", 0) >= 1, f"count={js.get('count')}")

            step("d-3) GET /api/assistant/proposals/{id}（详情）")
            r, js, _ = req(c, "GET", f"/api/assistant/proposals/{p1_id}")
            check("HTTP 200", r.status_code == 200)
            check("详情 proposal.id 一致", ((js or {}).get("proposal") or {}).get("id") == p1_id)

            step("d-4) POST /api/assistant/proposals/{id}/approve —— body {action:'approve', actor}")
            r, js, body = req(c, "POST", f"/api/assistant/proposals/{p1_id}/approve",
                              json={"action": "approve", "actor": "QA6"})
            check("HTTP 200", r.status_code == 200, f"got {r.status_code}；body={body[:300]}")
            check("状态 → approved", ((js or {}).get("proposal") or {}).get("status") == "approved")
            check("approve 之后业务表仍未写入",
                  _gen_text(project_id, target_id).startswith("第一章。陈默"),
                  f"db.text={_gen_text(project_id, target_id)[:40]!r}")

            step("d-5) GET /api/assistant/versions（应用前基线：应为 0 条）")
            r, js, _ = req(c, "GET", f"/api/assistant/versions?project_id={project_id}&limit=100")
            v_before = (js or {}).get("count", 0)
            check("HTTP 200", r.status_code == 200, f"count={v_before}")

            step("d-6) POST /api/assistant/proposals/{id}/apply —— body {action:'apply', actor}")
            r, js, body = req(c, "POST", f"/api/assistant/proposals/{p1_id}/apply",
                              json={"action": "apply", "actor": "QA6"})
            check("HTTP 200", r.status_code == 200, f"got {r.status_code}；body={body[:300]}")
            apply_version_id = (js or {}).get("version_id")
            if js:
                check("返回 proposal_id/version_id/content_hash",
                      all(k in js for k in ("proposal_id", "version_id", "content_hash")),
                      json.dumps({k: js.get(k) for k in ("proposal_id", "version_id", "content_hash")},
                                 ensure_ascii=False)[:200])
            new_text = _gen_text(project_id, target_id)
            check("正文真的改成 after_text", new_text == after_text, f"db.text={new_text[:50]!r}")
            check("提案 status → applied",
                  ((js or {}).get("proposal") or {}).get("status") == "applied")
            timeline.append(f"proposal #{p1_id} applied -> Version #{apply_version_id}")

            step("d-7) 版本表真的多了一条")
            r, js, _ = req(c, "GET", f"/api/assistant/versions?project_id={project_id}&limit=100")
            v_after = (js or {}).get("count", 0)
            check("versions 行数 +1", v_after == v_before + 1, f"{v_before} -> {v_after}")

            # ---------------------------------------------------------
            step("e) 漂移：approve 后手动改目标正文，再 apply → 必须 409 stale")
            p2_after = "【QA6 第二次改写】第一章。陈默推开门，铜锁发出干涩的一声。"
            _, js2, _ = req(c, "POST", "/api/assistant/proposals", json={
                "project_id": project_id,
                "type": "text_edit",
                "target_kind": "generation_text",
                "target_id": target_id,
                "after_text": p2_after,
                "reason": "第二次改写（用于触发漂移）",
                "scope": None,
            })
            p2 = (js2 or {}).get("proposal") or {}
            p2_id = p2.get("id")
            check("第二条提案创建成功（pending）", p2.get("status") == "pending", str(p2.get("id")))
            req(c, "POST", f"/api/assistant/proposals/{p2_id}/approve",
                json={"action": "approve", "actor": "QA6"})
            # 手动改目标正文（模拟用户在另一个入口改了同一段）
            _set_gen_text(project_id, target_id, "【人手改过】这段被用户在别处改掉了，不是提案改的。")
            print(f"    手动篡改后 db.text={_gen_text(project_id, target_id)[:40]!r}")

            r, js, body = req(c, "POST", f"/api/assistant/proposals/{p2_id}/apply",
                              json={"action": "apply", "actor": "QA6"})
            check("HTTP 409", r.status_code == 409, f"got {r.status_code}")
            check("detail 是结构化 dict",
                  isinstance((js or {}).get("detail"), dict),
                  f"detail={json.dumps((js or {}).get('detail'), ensure_ascii=False)[:300]}")
            detail = (js or {}).get("detail") or {}
            check("detail.error == 'stale'", detail.get("error") == "stale", str(detail.get("error")))
            for k in ("message", "expected_hash", "current_hash", "hint"):
                check(f"detail 含 {k}", bool(detail.get(k)), str(detail.get(k))[:90])

            # —— 移植的 TS 侧解析：证明 ApiError.detail 仍是对象，不是 "[object Object]" ——
            msg, err_detail = ts_request_parse(r.status_code, body)
            print(f"    [TS request() 移植] ApiError.message = {msg[:120]!r}")
            print(f"    [TS request() 移植] ApiError.detail  = {json.dumps(err_detail, ensure_ascii=False)[:200]}")
            check("ApiError.detail 保持 dict（未退化成 [object Object]）",
                  isinstance(err_detail, dict))
            check("detail.error 可被 AssistantPanel 判出 stale",
                  isinstance(err_detail, dict) and err_detail.get("error") == "stale")
            check("ApiError.message 取自 detail.message（可读）",
                  isinstance(msg, str) and "目标内容已变化" in msg, msg[:80])
            check("漂移后提案被置 stale", _proposal_status(p2_id) == "stale", str(_proposal_status(p2_id)))
            check("漂移时目标内容未被覆盖",
                  _gen_text(project_id, target_id).startswith("【人手改过】"),
                  _gen_text(project_id, target_id)[:40])
            timeline.append(f"proposal #{p2_id} -> 409 stale")

            # ---------------------------------------------------------
            step("f) POST /api/assistant/versions/{id}/restore —— body {action:'restore', actor}")
            v_before = _version_count(project_id)
            r, js, body = req(c, "POST", f"/api/assistant/versions/{apply_version_id}/restore",
                              json={"action": "restore", "actor": "QA6"})
            check("HTTP 200", r.status_code == 200, f"got {r.status_code}；body={body[:300]}")
            if js:
                check("返回 restored_from_version_id 与新 version_id",
                      js.get("restored_from_version_id") == apply_version_id and bool(js.get("version_id")),
                      json.dumps({k: js.get(k) for k in ("restored_from_version_id", "version_id",
                                                         "is_restore", "proposal_status")},
                                 ensure_ascii=False))
            v_after2 = _version_count(project_id)
            check("versions 表行数再 +1（历史不丢）", v_after2 == v_before + 1, f"{v_before} -> {v_after2}")
            check("目标内容恢复成 Version 的 after",
                  _gen_text(project_id, target_id) == after_text,
                  f"db.text={_gen_text(project_id, target_id)[:50]!r}")
            timeline.append(f"restore Version #{apply_version_id} -> new Version +1 (total {v_after2})")

            step("f-2) GET /api/assistant/versions/diff?from_id=&to_id=")
            r, js, _ = req(c, "GET",
                           f"/api/assistant/versions/diff?from_id={apply_version_id}&to_id={v_after2}")
            check("HTTP 200", r.status_code == 200)
            check("返回 diff 字符串", isinstance((js or {}).get("diff"), str),
                  repr((js or {}).get("diff", ""))[:80])

            # ---------------------------------------------------------
            step("g) 端点 7 reject / 12 prompts/active / 13 prompts/versions 补齐")
            _, js3, _ = req(c, "POST", "/api/assistant/proposals", json={
                "project_id": project_id,
                "type": "text_edit", "target_kind": "generation_text", "target_id": "2",
                "after_text": "【QA6 拒绝用例】第二章。雨停了。",
                "reason": "用于 reject", "scope": None,
            })
            p3_id = ((js3 or {}).get("proposal") or {}).get("id")
            r, js, _ = req(c, "POST", f"/api/assistant/proposals/{p3_id}/reject",
                           json={"action": "reject", "actor": "QA6", "note": "QA6 自动验证"})
            check("reject → HTTP 200 且状态 rejected",
                  r.status_code == 200 and ((js or {}).get("proposal") or {}).get("status") == "rejected",
                  f"{r.status_code} / {((js or {}).get('proposal') or {}).get('status')}")

            r, js, _ = req(c, "GET", f"/api/assistant/prompts/active?project_id={project_id}")
            check("prompts/active HTTP 200 + blocks", r.status_code == 200 and "blocks" in (js or {}),
                  json.dumps(js, ensure_ascii=False)[:200])
            r, js, _ = req(c, "GET", f"/api/assistant/prompts/versions?project_id={project_id}")
            check("prompts/versions HTTP 200 + count", r.status_code == 200 and "count" in (js or {}),
                  json.dumps(js, ensure_ascii=False)[:200])

            # ---------------------------------------------------------
            step("h) 契约负向用例（后端 extra='forbid'；这些都必须 422，不能静默通过）")
            bad = {
                "字段拼错 after_texts": ("/api/assistant/proposals", {
                    "project_id": project_id, "type": "text_edit",
                    "target_kind": "generation_text", "target_id": "1", "after_texts": "x"}),
                "多传服务端独占键 scope.base_content_hash": ("/api/assistant/proposals", {
                    "project_id": project_id, "type": "text_edit", "target_kind": "generation_text",
                    "target_id": "1", "after_text": "y",
                    "scope": {"base_content_hash": ""}}),
                "缺 target_kind": ("/api/assistant/proposals", {
                    "project_id": project_id, "type": "text_edit", "target_id": "1", "after_text": "z"}),
                "qa 少传必填 query": ("/api/assistant/qa", {"project_id": project_id}),
                "diagnose 少传必填 scope": ("/api/assistant/diagnose", {"project_id": project_id}),
            }
            for name, (path, body_d) in bad.items():
                r, _, bodytxt = req(c, "POST", path, json=body_d)
                check(f"{name} → 422", r.status_code == 422, f"got {r.status_code}; {bodytxt[:160]}")

            r, _, bodytxt = req(c, "POST", f"/api/assistant/proposals/{p1_id}/apply",
                                json={"action": "approve", "actor": "QA6"})
            check("apply 端点传 action='approve' → 422（action 与路径不一致）",
                  r.status_code == 422, f"got {r.status_code}; {bodytxt[:160]}")

            r, _, bodytxt = req(c, "POST", f"/api/assistant/versions/{apply_version_id}/restore",
                                json={"action": "restore_all", "actor": "QA6"})
            check("restore 传批量语义 action → 422", r.status_code == 422,
                  f"got {r.status_code}; {bodytxt[:160]}")

            # ---- 非 stale 的 409：状态机拒绝（已在 applied 的提案再 apply）----
            r, _, bodytxt = req(c, "POST", f"/api/assistant/proposals/{p1_id}/apply",
                                json={"action": "apply", "actor": "QA6"})
            check("重复 apply → 409（状态机拒绝，detail 是字符串非 dict）",
                  r.status_code == 409, f"got {r.status_code}")
            msg409, d409 = ts_request_parse(r.status_code, bodytxt)
            check("非 stale 409 可被 distinguish：detail 不是 dict", not isinstance(d409, dict),
                  f"detail={json.dumps(d409, ensure_ascii=False)[:120] if not isinstance(d409, str) else d409[:120]}")
            print(f"    状态机 409 文案：{msg409[:120]}")

            # ---------------------------------------------------------
            step("i) UI 可达的 422 用例：after_text 与目标当前内容完全一致")
            # 这是「用户把现有正文原样粘贴进去」就能触发的真实路径，
            # 用的 payload 与 AssistantPanel.submitDraft() 完全一致。
            cur = _gen_text(project_id, target_id)
            r, js_same, body_same = req(c, "POST", "/api/assistant/proposals", json={
                "project_id": project_id,
                "type": "text_edit", "target_kind": "generation_text", "target_id": target_id,
                "after_text": cur, "reason": "", "scope": None,
            })
            same_detail = (js_same or {}).get("detail")
            same_msg, same_err_detail = ts_request_parse(r.status_code, body_same)
            print(f"    detail 类型 = {'array' if isinstance(same_detail, list) else type(same_detail).__name__}")
            print(f"    ApiError.message = {same_msg[:100]!r}")
            check("内容不变 → 422（不生成空提案）", r.status_code == 422, f"got {r.status_code}")
            check("该 422 的 detail 是**字符串**（前端能读出人话，不是 [object Object]）",
                  isinstance(same_detail, str), f"{type(same_detail).__name__}: {str(same_detail)[:90]}")

            # ---------------------------------------------------------
            step("j) 面板里「处理不到」的 422 形态（键名拼错）会退化成 [object Object]")
            r, _, body_bad = req(c, "POST", "/api/assistant/proposals", json={
                "project_id": project_id, "type": "text_edit", "target_kind": "generation_text",
                "target_id": target_id, "after_texts": "x",
            })
            bad_msg, bad_detail = ts_request_parse(r.status_code, body_bad)
            print(f"    detail 类型 = {'array' if isinstance(bad_detail, list) else type(bad_detail).__name__}")
            print(f"    ApiError.message = {bad_msg[:120]!r}")
            check("pydantic 型 422 的 detail 是数组（不是字符串）",
                  isinstance(bad_detail, list), type(bad_detail).__name__)
            # 注意：Python 的 str(list) 与 JS 的 String(array) 不是一回事 ——
            # 「[object Object]」是 **JS** 的行为，必须在 node 里实测，不能用 Python 模拟。
            js_argv = ["node", "-e",
                       "const a = JSON.parse(process.argv[1]); process.stdout.write(String(a));",
                       json.dumps(bad_detail, ensure_ascii=False)]
            js_out = subprocess.run(js_argv, capture_output=True, text=True)
            js_msg = (js_out.stdout or "").strip()
            print(f"    [node 实测] JS String(detail) = {js_msg[:80]!r}")
            print("    注：旧代码 String(data.detail) 行为相同 → 非 30f4508 引入；"
                  "且面板固定构造合法请求体，当前 UI 路径触达不到该形态。")
            check("JS 侧 String(array detail) 退化成 '[object Object]'（P2，非本次引入）",
                  "[object Object]" in js_msg, js_msg[:80])

            print("\n" + "-" * 78)
            print("链路时间线：")
            for i, t in enumerate(timeline, 1):
                print(f"  {i}. {t}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        try:
            TMP_DB.unlink(missing_ok=True)
        except Exception:
            pass

    print("\n" + "=" * 78)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 78)
    return 1 if FAIL else 0


# ==================================================================
# 直连 DB 的只读探针（用于验证「业务表真的改了」）
# ==================================================================
def _session():
    from app.db import SessionLocal
    return SessionLocal()


def _gen_text(project_id: int, gen_id: str) -> str:
    from app.models import Generation

    db = _session()
    try:
        g = db.get(Generation, int(gen_id))
        return (g.text or "") if g and int(g.project_id) == int(project_id) else "<missing>"
    finally:
        db.close()


def _set_gen_text(project_id: int, gen_id: str, text: str) -> None:
    from app.models import Generation

    db = _session()
    try:
        g = db.get(Generation, int(gen_id))
        g.text = text
        db.commit()
    finally:
        db.close()


def _proposal_status(pid: int) -> str:
    from app.models import EditProposal

    db = _session()
    try:
        p = db.get(EditProposal, pid)
        return p.status if p else "<missing>"
    finally:
        db.close()


def _version_count(project_id: int) -> int:
    from sqlalchemy import select

    from app.models import Version

    db = _session()
    try:
        return len(list(db.scalars(select(Version).where(Version.project_id == project_id)).all()))
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
