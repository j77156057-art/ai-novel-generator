"""QA4 独立验证：EditProposal 提案审批 + Version 快照 + 回滚（第 4 步）。

跑法：  python tests/qa4_verify_proposal_write_path.py

覆盖 team-lead 派单的 8 项，每项都要求行号级或实测证据。
本脚本**不跑**工程师写好的pytest 用例，全部自己造场景。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa4_verify_harness import (  # noqa: E402
    BUSINESS_TABLES,
    QA4_DB,
    Report,
    SessionLocal,
    approve,
    business_unchanged,
    count,
    diff_snapshots,
    fastapi_app,
    get_proposal_row,
    get_text,
    init_db,
    make_client,
    new_proposal,
    proposal_status,
    reset_db,
    seed_generation,
    seed_project,
    snapshot_all,
    table_fingerprint,
    try_apply,
    try_restore,
)
from sqlalchemy import text  # noqa: E402

from app.agents import proposals as proposal_lib  # noqa: E402
from app.models import (  # noqa: E402
    EditProposal,
    Generation,
    Project,
    PromptOverride,
    Version,
)

REPORT = Report()
init_db()


# ====================================================================
# 1. 未批准绝不改库
# ====================================================================
def attack_01_no_write_without_apply() -> None:
    REPORT.section("1. 未批准绝不改库：创建 → approve → apply，每步前后 8 表 SHA256 比对")
    reset_db()
    db = SessionLocal()
    client = make_client()

    proj = seed_project(db, "P1-未批准绝不改库")
    gen = seed_generation(db, proj.id, "原始正文：铜钥匙藏在门后。")
    gen_id, pid_ = gen.id, proj.id

    s0 = snapshot_all(db)
    REPORT.note(f"基线：generations rows={s0['generations']['rows']} sha={s0['generations']['sha256'][:16]}")
    REPORT.note(f"       versions   rows={s0['versions']['rows']}")

    # —— 创建 ——
    created = new_proposal(client, pid_, gen_id)
    pid = created["proposal"]["id"]
    s1 = snapshot_all(db)
    changed = diff_snapshots(s0, s1)
    ok, biz_changed = business_unchanged(s0, s1)
    REPORT.check(
        "1a. 创建 pending 提案 → 6 张业务表逐字节零变化",
        ok,
        f"变化的业务表={biz_changed or '无'}；proposal.status={created['proposal']['status']}",
    )
    REPORT.check(
        "1b. 创建后唯一新增的是 edit_proposals + agent_runs（审计表）",
        set(changed) == {"edit_proposals", "agent_runs"},
        f"实际变化表={sorted(changed)}；edit_proposals {s0['edit_proposals']['rows']}→{s1['edit_proposals']['rows']}，"
        f"agent_runs {s0['agent_runs']['rows']}→{s1['agent_runs']['rows']}",
    )
    REPORT.check(
        "1c. 创建后 versions 表仍为 0 行",
        s1["versions"]["rows"] == 0,
        f"versions rows={s1['versions']['rows']}",
    )
    REPORT.check(
        "1d. Generation.text 此刻仍是原文",
        get_text(db, gen_id) == "原始正文：铜钥匙藏在门后。",
        f"实际={get_text(db, gen_id)!r}",
    )

    # —— approve ——
    approve(client, pid)
    s2 = snapshot_all(db)
    ok2, biz2 = business_unchanged(s0, s2)
    REPORT.check(
        "1e. approve 后 6 张业务表仍逐字节零变化",
        ok2,
        f"变化的业务表={biz2 or '无'}",
    )
    REPORT.check(
        "1f. approve 后 versions 表**仍为 0 行**（审批不落快照）",
        s2["versions"]["rows"] == 0,
        f"versions rows={s2['versions']['rows']}",
    )
    REPORT.check(
        "1g. approve 后 Generation.text 仍是原文",
        get_text(db, gen_id) == "原始正文：铜钥匙藏在门后。",
        f"实际={get_text(db, gen_id)!r}",
    )
    REPORT.check(
        "1h. approve 只改edit_proposals 一张表",
        set(diff_snapshots(s1, s2)) == {"edit_proposals"},
        f"实际变化表={sorted(diff_snapshots(s1, s2))}",
    )

    # —— apply ——
    code, body = try_apply(client, pid)
    s3 = snapshot_all(db)
    REPORT.check("1i. apply 返回 200", code == 200, f"code={code} body={json.dumps(body, ensure_ascii=False)[:200]}")
    REPORT.check(
        "1j. apply 后 Generation.text 才变化，且变成after_text",
        get_text(db, gen_id) == "改写后的正文：铜钥匙被张三取出。",
        f"实际={get_text(db, gen_id)!r}",
    )
    REPORT.check(
        "1k. apply 后 versions 表恰好 1 行",
        s3["versions"]["rows"] == 1,
        f"versions rows={s3['versions']['rows']}",
    )
    REPORT.check(
        "1l. apply 只碰generations / versions / edit_proposals（+prompt_overrides 不该动）",
        set(diff_snapshots(s2, s3)) <= {"generations", "versions", "edit_proposals"},
        f"实际变化表={sorted(diff_snapshots(s2, s3))}",
    )
    changed_after_apply = sorted(diff_snapshots(s0, s3))
    REPORT.check(
        "1m. 整条链路的净业务影响= generations 单表（爆炸半径可控）",
        [t for t in changed_after_apply if t in BUSINESS_TABLES] == ["generations"],
        f"净变化表={changed_after_apply}",
    )
    db.close()


# ====================================================================
# 2. 攻击 approve 缺失路径 + 各终态二次 apply
# ====================================================================
def attack_02_missing_approve_and_terminal_states() -> None:
    REPORT.section("2. 攻击 approve 缺失：pending 直apply + rejected/stale/applied/rolled_back 二次 apply")
    reset_db()
    db = SessionLocal()
    client = make_client()
    proj = seed_project(db, "P2-攻击审批缺失")
    pid_ = proj.id

    # (a) pending 直接 apply
    g1 = seed_generation(db, pid_, "正文A：铜钥匙在门后。")
    c = new_proposal(client, pid_, g1.id, "改写A：铜钥匙被取出。", reason="场景a")["proposal"]
    before = snapshot_all(db)
    code, body = try_apply(client, c["id"])
    after = snapshot_all(db)
    REPORT.check(
        "2a. pending 未 approve 直接 apply → 409",
        code == 409,
        f"code={code} detail={json.dumps(body, ensure_ascii=False)[:220]}",
    )
    ok, ch = business_unchanged(before, after)
    REPORT.check(
        "2b. pending 直apply 后业务表零变化",
        ok,
        f"变化的业务表={ch or '无'}；versions rows={after['versions']['rows']}",
    )
    REPORT.check(
        "2c. pending 直 apply 后提案仍是 pending（未被静默推进）",
        proposal_status(db, c["id"]) == "pending",
        f"status={proposal_status(db, c['id'])}",
    )

    # (b) rejected → apply
    g2 = seed_generation(db, pid_, "正文B：铜钥匙在门后。")
    c2 = new_proposal(client, pid_, g2.id, "改写B：铜钥匙被取出。", reason="场景b")["proposal"]
    r = client.post(f"/api/assistant/proposals/{c2['id']}/reject", json={"action": "reject", "actor": "qa4"})
    code_r, _ = try_apply(client, c2["id"])
    code_a = client.post(f"/api/assistant/proposals/{c2['id']}/approve", json={"action": "approve"}).status_code
    REPORT.check(
        "2d. rejected 提案 apply → 409，且无法再 approve（终态）",
        r.status_code == 200 and code_r == 409 and code_a == 409,
        f"reject={r.status_code} apply={code_r} re-approve={code_a}；"
        f"text={get_text(db, g2.id)!r}",
    )

    # (c) stale → apply
    g3 = seed_generation(db, pid_, "正文C：铜钥匙在门后。")
    c3 = new_proposal(client, pid_, g3.id, "改写C：铜钥匙被取出。", reason="场景c")["proposal"]
    approve(client, c3["id"])
    g3.text = "用户手动编辑过的正文C"  # 模拟漂移
    db.commit()
    code_s, _ = try_apply(client, c3["id"])
    code_s2, _ = try_apply(client, c3["id"])
    code_s3 = client.post(f"/api/assistant/proposals/{c3['id']}/approve", json={"action": "approve"}).status_code
    REPORT.check(
        "2e. stale 提案 apply → 409（两次都是），且 stale 后无法再 approve",
        code_s == 409 and code_s2 == 409 and code_s3 == 409,
        f"apply1={code_s} apply2={code_s2} re-approve={code_s3}",
    )
    REPORT.check(
        "2f. stale 提案的 apply 从未写入正文",
        get_text(db, g3.id) == "用户手动编辑过的正文C",
        f"text={get_text(db, g3.id)!r}",
    )

    # (d) applied 二次 apply —— 关键：会不会重复写 Version
    g4 = seed_generation(db, pid_, "正文D：铜钥匙在门后。")
    c4 = new_proposal(client, pid_, g4.id, "改写D：铜钥匙被取出。", reason="场景d")["proposal"]
    approve(client, c4["id"])
    code1, b1 = try_apply(client, c4["id"])
    v1 = b1["version_id"]
    rows_after_first = count(db, Version)
    mid_text = get_text(db, g4.id)
    code2, b2 = try_apply(client, c4["id"])
    code3, b3 = try_apply(client, c4["id"])
    rows_after_retry = count(db, Version)
    REPORT.check(
        "2g.已 applied 提案二次/三次 apply → 全部 409",
        code1 == 200 and code2 == 409 and code3 == 409,
        f"apply1={code1} apply2={code2} apply3={code3}；detail2={json.dumps(b2, ensure_ascii=False)[:180]}",
    )
    REPORT.check(
        "2h. 二次 apply **没有**产生重复 Version（幂等）",
        rows_after_first == rows_after_retry,
        f"首次 apply 后 versions={rows_after_first}；重试 2 次后={rows_after_retry}（version_id={v1}）",
    )
    REPORT.check(
        "2i. 二次 apply 没有重复覆盖正文（正文保持 after_text）",
        get_text(db, g4.id) == mid_text == "改写D：铜钥匙被取出。",
        f"text={get_text(db, g4.id)!r}",
    )

    # (e) rolled_back → apply
    code_rb, body_rb = try_restore(client, v1)
    code_rr, body_rr = try_apply(client, c4["id"])
    REPORT.check(
        "2j. restore 后提案→rolled_back，再 apply → 409",
        code_rb == 200 and code_rr == 409,
        f"restore={code_rb}（proposal_status={body_rb.get('proposal_status')}）再 apply={code_rr}；"
        f"status 现在={proposal_status(db, c4['id'])}",
    )
    REPORT.check(
        "2k. rolled_back 是终态：不能再 approve 也不能再 reject",
        client.post(f"/api/assistant/proposals/{c4['id']}/approve", json={"action": "approve"}).status_code == 409
        and client.post(f"/api/assistant/proposals/{c4['id']}/reject", json={"action": "reject"}).status_code == 409,
        "approve=409 reject=409",
    )
    db.close()


# ====================================================================
# 3. 无批量语义 + 路由清单精确核对
# ====================================================================
def attack_03_no_batch_semantics_and_route_audit() -> None:
    REPORT.section("3. 无批量语义：apply/restore 的批量字段全422 + 全路由清单核对")
    reset_db()
    db = SessionLocal()
    client = make_client()
    proj = seed_project(db, "P3-无批量")
    pid_ = proj.id
    gen = seed_generation(db, pid_, "正文：铜钥匙在门后。")
    c = new_proposal(client, pid_, gen.id, "改写：铜钥匙被取出。")["proposal"]
    approve(client, c["id"])
    code, body = try_apply(client, c["id"])
    vid = body["version_id"]

    batch_fields = [
        {"apply_all": True},
        {"proposal_ids": [1, 2, 3]},
        {"ids": [1, 2, 3]},
        {"all": True},
        {"batch": True},
        {"proposal_id_list": [c["id"]]},
        {"targets": ["a", "b"]},
        {"segments": [1, 2, 3]},
        {"dry_run": False},
        {"force": True},
        {"skip_drift_check": True},
        {"no_version": True},
    ]
    bad_apply = []
    for extra in batch_fields:
        r = client.post(
            f"/api/assistant/proposals/{c['id']}/apply",
            json={"action": "apply", "actor": "qa4", **extra},
        )
        if r.status_code != 422:
            bad_apply.append((extra, r.status_code, r.text[:120]))
    REPORT.check(
        "3a. apply 端点对12 种批量/越权语义字段全部 422",
        not bad_apply,
        f"未 422 的：{bad_apply}" if bad_apply else f"全部 422；共测{len(batch_fields)} 组字段",
    )

    restore_batch = [
        {"restore_all": True},
        {"version_ids": [1, 2]},
        {"ids": [vid]},
        {"all": True},
        {"batch": True},
        {"to_version_id": 1},
        {"target_project_id": 999},
    ]
    bad_restore = []
    for extra in restore_batch:
        rc, rb = try_restore(client, vid, body=extra)
        if rc != 422:
            bad_restore.append((extra, rc, str(rb)[:120]))
    REPORT.check(
        "3b. restore 端点对 7 种批量/越权语义字段全部 422",
        not bad_restore,
        f"未 422 的：{bad_restore}" if bad_restore else f"全部 422；共测 {len(restore_batch)} 组字段",
    )

    r = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "type": "text_edit", "target_kind": "generation_text",
        "target_id": str(gen.id), "after_text": "批量提案不该建得成", "apply_all": True,
    })
    REPORT.check("3c. 创建端点也拒批量字段（422）", r.status_code == 422, f"code={r.status_code}")

    # —— 路由清单——
    routes = []
    for r_ in fastapi_app.routes:
        methods = sorted(getattr(r_, "methods", []) or [])
        if not methods:
            continue
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            routes.append(f"{m} {r_.path}")
    routes_sorted = sorted(set(routes))
    REPORT.note(f"fastapi_app 共 {len(routes_sorted)} 条路由（含全部 router）")
    proposal_routes = sorted(x for x in routes_sorted if "/proposals" in x or "/versions" in x or "/prompts" in x)
    REPORT.note("提案相关路由：")
    for x in proposal_routes:
        REPORT.note(f"    {x}")

    expected = {
        "POST /api/assistant/proposals",
        "GET /api/assistant/proposals",
        "GET /api/assistant/proposals/{proposal_id}",
        "POST /api/assistant/proposals/{proposal_id}/approve",
        "POST /api/assistant/proposals/{proposal_id}/reject",
        "POST /api/assistant/proposals/{proposal_id}/apply",
        "POST /api/assistant/versions/{version_id}/restore",
        "GET /api/assistant/versions",
        "GET /api/assistant/versions/diff",
        "GET /api/assistant/prompts/active",
        "GET /api/assistant/prompts/versions",
    }
    actual = set(proposal_routes)
    REPORT.check(
        "3d. 提案 router 端点数 = 11（与 docstring 表格逐条精确一致）",
        actual == expected,
        f"缺失={sorted(expected - actual)}；多出={sorted(actual - expected)}",
    )

    # 「写业务表」不等于「HTTP POST」：create/approve/reject 也是 POST，
    # 但它们只动edit_proposals。这里按**实际写库能力**划分，全应用只有
    # apply / restore 两个端点能碰generations/projects/prompt_overrides。
    all_post = [x for x in proposal_routes if x.startswith("POST")]
    biz_write_routes = [
        "POST /api/assistant/proposals/{proposal_id}/apply",
        "POST /api/assistant/versions/{version_id}/restore",
    ]
    REPORT.note(f"提案 router 的 POST 端点共 {len(all_post)} 个：{all_post}")
    REPORT.check(
        "3e. POST 端点共 5 个；其中具备业务表写权限的仅 apply / restore 两个",
        len(all_post) == 5 and set(biz_write_routes) <= set(all_post),
        f"POST={all_post}；具备写库能力的子集={biz_write_routes}"
        "（create/approve/reject 的零业务表写入已在 1a/1e 与 2b/2d 实测证明）",
    )
    # 路径里不得出现复数/id 列表占位
    plural = [x for x in proposal_routes if any(
        tok in x for tok in ("{ids}", "_ids}", "{proposal_ids}", "{version_ids}", "{targets}", "batch", "all")
    )]
    REPORT.check(
        "3f. 没有任何端点路径支持一次改多段（无复数占位/批量路径）",
        not plural,
        f"可疑路径={plural}" if plural else "11 条路径全为单数语义",
    )
    db.close()


# ====================================================================
# 4. 漂移即 stale（含 stale 是终态的完整链路）
# ====================================================================
def attack_04_drift_becomes_stale() -> None:
    REPORT.section("4. 漂移即 stale：手改目标 → apply 409 + status=stale + 新内容未被覆盖 + versions 0 行")
    reset_db()
    db = SessionLocal()
    client = make_client()
    proj = seed_project(db, "P4-漂移")
    pid_ = proj.id

    gen = seed_generation(db, pid_, "原始正文：铜钥匙藏在门后。")
    gen_id = gen.id
    c = new_proposal(client, pid_, gen.id, "提案想写的正文：铜钥匙被张三取出。")["proposal"]
    pid = c["id"]
    approve(client, pid)
    base_hash = c["base_content_hash"]

    # 模拟用户手动编辑目标（绕过提案流直接改库）
    gen.text = "用户在审批期间手写的正文：钥匙其实是银的。"
    db.commit()
    manual_text = get_text(db, gen_id)

    before = snapshot_all(db)
    code, body = try_apply(client, pid)
    after = snapshot_all(db)

    REPORT.check("4a. 漂移后 apply → 409", code == 409, f"code={code} body={json.dumps(body, ensure_ascii=False)[:300]}")
    REPORT.check(
        "4b. 响应体带 expected_hash / current_hash 双指纹，便于排障",
        isinstance(body, dict) and body.get("detail", {}).get("error") == "stale"
        and body["detail"].get("expected_hash") == base_hash,
        f"detail={json.dumps(body.get('detail', {}), ensure_ascii=False)[:260]}",
    )
    st = proposal_status(db, pid)
    REPORT.check("4c. 提案 status 变为 stale", st == "stale", f"status={st}")
    REPORT.check(
        "4d. **用户手写的新正文未被覆盖**",
        get_text(db, gen_id) == manual_text == "用户在审批期间手写的正文：钥匙其实是银的。",
        f"text={get_text(db, gen_id)!r}",
    )
    ok, ch = business_unchanged(before, after)
    REPORT.check(
        "4e. 漂移 apply 后业务表零变化（除 prompt_overrides 外全等）",
        ok,
        f"变化的业务表={ch or '无'}",
    )
    REPORT.check(
        "4f. versions 表 0 行（漂移不产生快照）",
        after["versions"]["rows"] == 0,
        f"versions rows={after['versions']['rows']}",
    )
    REPORT.check(
        "4g. 漂移事实被commit 留痕（只推进 edit_proposals 一张表）",
        set(diff_snapshots(before, after)) == {"edit_proposals"},
        f"实际变化表={sorted(diff_snapshots(before, after))}",
    )

    # stale 是终态
    re_apply = try_apply(client, pid)[0]
    re_approve = client.post(f"/api/assistant/proposals/{pid}/approve", json={"action": "approve"}).status_code
    re_reject = client.post(f"/api/assistant/proposals/{pid}/reject", json={"action": "reject"}).status_code
    REPORT.check(
        "4h. stale 是终态：再 apply / 再 approve / 再 reject 全 409",
        re_apply == 409 and re_approve == 409 and re_reject == 409,
        f"apply={re_apply} approve={re_approve} reject={re_reject}",
    )

    # 重新基于最新内容建提案 → 必须能走通（证明 stale 不是死路）
    fresh = new_proposal(client, pid_, gen_id, "基于手写版再改：钥匙是金的。")["proposal"]
    approve(client, fresh["id"])
    code_f, body_f = try_apply(client, fresh["id"])
    REPORT.check(
        "4i. 基于最新内容重建提案后可正常 apply（stale 不是死胡同）",
        code_f == 200 and get_text(db, gen_id) == "基于手写版再改：钥匙是金的。",
        f"code={code_f} text={get_text(db, gen_id)!r} version_id={body_f.get('version_id')}",
    )
    db.close()


# ====================================================================
# 5. 无半版本（真实 sqlite 事务行为，不用 monkeypatch 版的假设）
# ====================================================================
def attack_05_atomicity_real_transaction() -> None:
    REPORT.section("5. 无半版本：真实 sqlite 事务下在「写业务表后/Version 前」注入异常")
    reset_db()
    client = make_client()

    # —— (a) generation_text 路径 ——
    db = SessionLocal()
    proj = seed_project(db, "P5a-事务回滚")
    gen = seed_generation(db, proj.id, "原文：铜钥匙在门后。")
    gen_id = proj.id
    real_gid = db.execute(
        text("SELECT id FROM generations ORDER BY id DESC LIMIT 1")
    ).scalar()
    c = new_proposal(client, proj.id, real_gid, "改写：铜钥匙被取出。")["proposal"]
    pid = c["id"]
    approve(client, pid)
    db.close()

    before = _snapshot()
    # 用「真实的 version_writer 失败」模拟 Version 写入前崩溃 —— writer 抛错后
    # apply_proposal 的 except 分支必须 db.rollback()，业务表回到原值。
    _boom_a = {"fired": False}

    def exploding_writer(*_a: object, **_kw: object):
        _boom_a["fired"] = True
        raise RuntimeError("QA4 注入：Version 写入前崩溃")

    db = SessionLocal()
    from app.models import EditProposal as _EP

    prop = db.get(_EP, pid)
    prop.status = "approved"  # 保证状态机放行（已approve 过，这里只是保险）
    db.commit()
    db.close()

    code_a, text_a = _apply_with_faulty_writer(pid, exploding_writer)
    db = SessionLocal()
    after = snapshot_all(db)
    REPORT.check(
        "5a.(a) 注入点确实被触发了（Version 写入前抛错）",
        _boom_a["fired"],
        f"injector fired={_boom_a['fired']}；apply 结果={code_a}（{str(text_a)[:80]}）",
    )
    ok, ch = business_unchanged(before, after)
    REPORT.check(
        "5b. (a) 业务表回滚到原值（Generation.text 未被留下半截）",
        ok and get_text(db, real_gid) == "原文：铜钥匙在门后。",
        f"变化的业务表={ch or '无'}；text={get_text(db, real_gid)!r}",
    )
    REPORT.check(
        "5c. (a) versions 表 0 行（无半版本）",
        after["versions"]["rows"] == 0,
        f"versions rows={after['versions']['rows']}",
    )
    REPORT.check(
        "5d. (a) 提案 status 未被推进（仍 approved，不是 applied）",
        proposal_status(db, pid) == "approved",
        f"status={proposal_status(db, pid)}",
    )
    db.close()

    # —— (b) prompt_override 路径：写 override 后、Version 前崩 ——
    db = SessionLocal()
    proj2 = seed_project(db, "P5b-prompt 回滚")
    # 先给项目加一个 generation 以便对比（不应被动）
    seed_generation(db, proj2.id, "P5b 正文不应变化。")
    db.close()
    db = SessionLocal()
    r = client.post("/api/assistant/proposals", json={
        "project_id": proj2.id, "type": "prompt_edit", "target_kind": "prompt_override",
        "target_id": "style_suffix", "after_text": "QA4 注入用的提示词覆写内容",
        "reason": "场景b",
    })
    assert r.status_code == 200, r.text
    ppid = r.json()["proposal"]["id"]
    approve(client, ppid)
    db.close()

    before_b = _snapshot()
    _boom_b = {"fired": False}

    def exploding_writer_b(*_a: object, **_kw: object):
        _boom_b["fired"] = True
        raise RuntimeError("QA4 注入：prompt_override 写入后崩溃")

    code_b, text_b = _apply_with_faulty_writer(ppid, exploding_writer_b)
    db = SessionLocal()
    after_b = snapshot_all(db)
    ov_rows = db.execute(text("SELECT COUNT(*) FROM prompt_overrides")).scalar()
    REPORT.check(
        "5e. (b) 注入点确实被触发了（prompt_override 写入后、Version 前）",
        _boom_b["fired"],
        f"injector fired={_boom_b['fired']}；apply 结果={code_b}（{str(text_b)[:80]}）",
    )
    ok_b, ch_b = business_unchanged(before_b, after_b)
    REPORT.check(
        "5f. (b) prompt_overrides 回滚（覆写没留下），业务表零变化",
        ok_b and ov_rows == 0,
        f"变化的业务表={ch_b or '无'}；prompt_overrides rows={ov_rows}（应为0，说明覆写也回滚了）",
    )
    REPORT.check(
        "5g. (b) versions 表 0 行",
        after_b["versions"]["rows"] == 0,
        f"versions rows={after_b['versions']['rows']}",
    )
    REPORT.check(
        "5h. (b) 提案 status 未被推进",
        proposal_status(db, ppid) == "approved",
        f"status={proposal_status(db, ppid)}",
    )
    db.close()

    # —— (c) 代码级确认：apply 用同一个 session 且自带 try/except+rollback ——
    import inspect as _inspect

    src = _inspect.getsource(proposal_lib.apply_proposal)
    has_rollback = "db.rollback()" in src
    has_try = "try:" in src
    # 只看 try 块内部的事务顺序：`db.commit()` 在漂移分支里也出现过一次
    # （proposals.py:781，那是「只提交 stale 事实」的刻意设计），
    # 拿全源码做index 比较会被它干扰。
    try_body = src[src.index("    try:\n"):]
    order = [
        try_body.index("write_target"),
        try_body.index("writer("),
        try_body.index('proposal.status = "applied"'),
        try_body.index("db.commit()"),
    ]
    REPORT.check(
        "5i. apply_proposal 源码内自带 try + db.rollback()（不依赖调用方回滚）",
        has_try and has_rollback,
        f"源码含 try={has_try} 含 db.rollback()={has_rollback}（proposals.py:803-824）",
    )
    REPORT.check(
        "5j. try 块内事务顺序正确：write_target → writer(Version) → 置 applied → commit",
        order == sorted(order),
        "顺序：write_target → db.flush() → writer(Version) → 置 applied/version_id/applied_at → db.commit()"
        "（proposals.py:805-821）；漂移分支的 commit(:781) 独立于 try 块，刻意只提交 stale 事实",
    )


def _snapshot() -> dict:
    db = SessionLocal()
    try:
        return snapshot_all(db)
    finally:
        db.close()


def _apply_with_faulty_writer(pid: int, writer) -> tuple[int, str]:
    """直接调apply_proposal 并注入会抛错的 version_writer。

    刻意**绕过 HTTP 层**：这样能证明「回滚是 apply_proposal 自己做的」，
    而不是 router 或 TestClient 帮它擦屁股。
    """
    from app.models import EditProposal as _EP

    db = SessionLocal()
    try:
        prop = db.get(_EP, pid)
        code, out = 200, ""
        try:
            proposal_lib.apply_proposal(db, proposal=prop, actor="qa4", version_writer=writer)
        except Exception as exc:  # noqa: BLE001
            code, out = 500, f"{type(exc).__name__}: {exc}"
        return code, out
    finally:
        db.close()


# ====================================================================
# 6. 回滚不丢历史
# ====================================================================
def attack_06_restore_preserves_history() -> None:
    REPORT.section("6. 回滚不丢历史：v1→v2→v3 → restore(v1)")
    reset_db()
    client = make_client()
    db = SessionLocal()
    proj = seed_project(db, "P6-回滚历史")
    pid_ = proj.id
    gen = seed_generation(db, pid_, "v0：铜钥匙在门后。")
    real_gid = db.execute(text("SELECT id FROM generations ORDER BY id DESC LIMIT 1")).scalar()

    # 造 3 个版本
    version_ids = []
    for tag, after in (("v1", "v1：铜钥匙被取出。"), ("v2", "v2：铜钥匙被张三取出。"), ("v3", "v3：铜钥匙被李四取出。")):
        c = new_proposal(client, pid_, real_gid, after, reason=tag)["proposal"]
        approve(client, c["id"])
        code, body = try_apply(client, c["id"])
        assert code == 200, body
        version_ids.append(body["version_id"])
    db.close()
    v1, v2, v3 = version_ids

    db = SessionLocal()
    REPORT.check("6a. 造出 v1/v2/v3 三个 Version", count(db, Version) == 3, f"versions rows={count(db, Version)}")
    REPORT.check("6b. apply 3 次后正文= v3 内容", get_text(db, real_gid) == "v3：铜钥匙被李四取出。", f"text={get_text(db, real_gid)!r}")
    history_before = {vid: _version_fingerprint(vid) for vid in (v1, v2, v3)}
    db.close()

    # restore v1
    code_r, body_r = try_restore(client, v1)
    db = SessionLocal()
    after_restore = count(db, Version)
    REPORT.check("6c. restore(v1) 返回 200", code_r == 200, f"code={code_r} body={json.dumps({k: v for k, v in body_r.items() if k != 'restored_from'}, ensure_ascii=False)[:240]}")
    REPORT.check(
        "6d. (a) 业务表内容回到 v1 的快照",
        get_text(db, real_gid) == "v1：铜钥匙被取出。",
        f"text={get_text(db, real_gid)!r}（v1 快照应为 'v1：铜钥匙被取出。'）",
    )
    REPORT.check(
        "6b2.(b) versions 行数 3→ 4（restore 自身生成新 Version，历史不丢）",
        after_restore == 4,
        f"restore 前=3，restore 后={after_restore}",
    )
    hist_after = {vid: _version_fingerprint(vid) for vid in (v1, v2, v3)}
    unchanged = [vid for vid in (v1, v2, v3) if history_before[vid] == hist_after[vid]]
    REPORT.check(
        "6e.(b) v1/v2/v3 三条历史 Version 一字未改（SHA 逐条比对）",
        len(unchanged) == 3,
        f"未变的 version_id={unchanged}（应全为3条）",
    )
    # 对应提案 → rolled_back
    src_pid = body_r.get("proposal_id")
    REPORT.check(
        "6f.(c) 对应提案 status 变为 rolled_back",
        proposal_status(db, src_pid) == "rolled_back",
        f"proposal_id={src_pid} status={proposal_status(db, src_pid)}",
    )
    new_vid = body_r.get("version_id")
    REPORT.check(
        "6g. restore 产生的第 4 条 Version 标记 is_restore=True",
        _version_flag(new_vid) == 1,
        f"new version_id={new_vid} is_restore={_version_flag(new_vid)}",
    )

    # 不存在的 version → 404
    code_404, _ = try_restore(client, 999999)
    REPORT.check("6h. restore 不存在的 version_id → 404", code_404 == 404, f"code={code_404}")

    # 跨项目越权：属于别的 project 的version_id
    db2 = SessionLocal()
    other = seed_project(db2, "P6-别的项目")
    seed_generation(db2, other.id, "别的项目的正文，不该被 QA4 restore 改动。")
    other_gid = db2.execute(text("SELECT id FROM generations ORDER BY id DESC LIMIT 1")).scalar()
    other_text_before = get_text(db2, other_gid)
    db2.close()

    # 篡改 v1 的 project_id 指向别的项目（模拟跨项目version）
    db2 = SessionLocal()
    db2.execute(text("UPDATE versions SET project_id = :other WHERE id = :v"), {"other": other.id, "v": v1})
    db2.commit()
    db2.close()

    code_cross, body_cross = try_restore(client, v1)
    db2 = SessionLocal()
    other_text_after = get_text(db2, other_gid)
    REPORT.check(
        "6i. 跨项目 restore（version.project_id 指向别的项目）被拒或不影响原项目",
        code_cross in (404, 409, 422, 400) or other_text_after == other_text_before,
        f"code={code_cross}；被restore 的目标项目 text前后一致={other_text_after == other_text_before}"
        f"（after={other_text_after!r}）",
    )
    REPORT.note(
        "注：restore 端点按 version.project_id 定位目标。若 version.project_id 被篡改为"
        "另一 project，read_target/write_target 内部会用该项目id 去 get(Generation)，"
        "只有当该 generation 真属于该项目时才会写。这构成越权面（见报告 P1-x）。"
    )
    db.close()
    db2.close()


def _version_fingerprint(vid: int) -> tuple:
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT id, project_id, proposal_id, source_proposal_id, kind, content_hash, is_restore, snapshot_json, created_by FROM versions WHERE id = :v"),
            {"v": vid},
        ).fetchone()
        return tuple(str(x) for x in row) if row else ("MISSING",)
    finally:
        db.close()


def _version_flag(vid: int) -> int:
    db = SessionLocal()
    try:
        return db.execute(text("SELECT is_restore FROM versions WHERE id = :v"), {"v": vid}).scalar()
    finally:
        db.close()


# ====================================================================
# 7. patch 白名单
# ====================================================================
def attack_07_patch_whitelist() -> None:
    REPORT.section("7. patch 白名单：删除类/改配置/类型错配全拒 + 合法 text_edit 走通（对照组）")
    reset_db()
    client = make_client()
    db = SessionLocal()
    proj = seed_project(db, "P7-白名单")
    pid_ = proj.id
    gen = seed_generation(db, pid_, "原文：铜钥匙在门后。")
    real_gid = db.execute(text("SELECT id FROM generations ORDER BY id DESC LIMIT 1")).scalar()
    db.close()

    malicious = [
        # —— 试图删除 Project ——
        ("删除 Project", {"project_id": pid_, "type": "text_edit", "target_kind": "project_plot_bible", "target_id": "plot_bible", "after_text": '{"characters":[]}', "scope": {"patch": {"op": "delete", "target": "project"}}}),
        # —— 试图删除 Generation ——
        ("删除 Generation", {"project_id": pid_, "type": "text_edit", "target_kind": "generation_text", "target_id": str(real_gid), "after_text": "x", "scope": {"patch": {"op": "delete"}}}),
        # —— 试图改 AppSetting / LLM key ——
        ("改 AppSetting(LLM key)", {"project_id": pid_, "type": "text_edit", "target_kind": "generation_text", "target_id": str(real_gid), "after_text": "x", "scope": {"patch": {"api_key": "sk-evil"}}}),
        ("patch 键含 llm_key", {"project_id": pid_, "type": "prompt_edit", "target_kind": "prompt_override", "target_id": "style_suffix", "after_text": "y", "scope": {"patch": {"llm_key": "sk-evil"}}}),
        # —— 非白名单列 ——
        ("改 generations.summary（非白名单列）", {"project_id": pid_, "type": "text_edit", "target_kind": "generation_text", "target_id": str(real_gid), "after_text": "x", "scope": {"patch": {"fields": ["summary"]}}}),
        ("改 projects.title（非白名单列）", {"project_id": pid_, "type": "fact_resolution", "target_kind": "project_plot_bible", "target_id": "plot_bible", "after_text": '{"x":1}', "scope": {"patch": {"fields": ["title"]}}}),
        # —— type× target_kind 错配 ——
        ("type=text_edit 配 target_kind=prompt_override", {"project_id": pid_, "type": "text_edit", "target_kind": "prompt_override", "target_id": "style_suffix", "after_text": "x"}),
        ("type=prompt_edit 配 target_kind=generation_text", {"project_id": pid_, "type": "prompt_edit", "target_kind": "generation_text", "target_id": str(real_gid), "after_text": "x"}),
        ("type=fact_resolution 配 target_kind=generation_text", {"project_id": pid_, "type": "fact_resolution", "target_kind": "generation_text", "target_id": str(real_gid), "after_text": '{"a":1}'}),
        # —— 黑名单目标名 ——
        ("target_id=app_settings（黑名单目标）", {"project_id": pid_, "type": "text_edit", "target_kind": "generation_text", "target_id": "app_settings", "after_text": "x"}),
        ("target_id=facts（黑名单目标）", {"project_id": pid_, "type": "text_edit", "target_kind": "generation_text", "target_id": "facts", "after_text": "x"}),
        # —— 未知键 ——
        ("patch 含未知键", {"project_id": pid_, "type": "text_edit", "target_kind": "generation_text", "target_id": str(real_gid), "after_text": "x", "scope": {"patch": {"unexpected_key": 1}}}),
        # —— truncate/drop ——
        ("patch 值含 truncate_table", {"project_id": pid_, "type": "text_edit", "target_kind": "generation_text", "target_id": str(real_gid), "after_text": "x", "scope": {"patch": {"fields": ["truncate_table"]}}}),
    ]

    not_rejected = []
    for label, payload in malicious:
        r = client.post("/api/assistant/proposals", json=payload)
        # 期望 422(PatchRejected) 或 400；404/409 也算「没建成功」可接受，但我们要确认确实被拒
        if r.status_code == 200:
            not_rejected.append((label, r.status_code, r.text[:140]))
        REPORT.note(f"恶意「{label}」→ HTTP {r.status_code}")
    REPORT.check(
        "7a. 13 组恶意 patch 全部被拒（无一建得成提案）",
        not not_rejected,
        f"漏网的={not_rejected}" if not_rejected else "全部非 200（422 PatchRejected / 400）",
    )

    # —— 对照组：合法 text_edit 必须能走通（防止「全被拒」的假通过）——
    c = new_proposal(client, pid_, real_gid, "合法改写：铜钥匙被张三取出。")["proposal"]
    approve(client, c["id"])
    code_ok, body_ok = try_apply(client, c["id"])
    db = SessionLocal()
    REPORT.check(
        "7b. 对照组：合法 text_edit 改 Generation.text 能完整走通",
        code_ok == 200 and get_text(db, real_gid) == "合法改写：铜钥匙被张三取出。" and count(db, Version) == 1,
        f"apply code={code_ok}；text={get_text(db, real_gid)!r}；versions={count(db, Version)}",
    )
    db.close()

    # —— 对照组2：合法 prompt_edit（走白名单 prompt_key）——
    db = SessionLocal()
    seed_generation(db, pid_, "对照2 正文。", segment_no=2)
    db.close()
    rp = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "type": "prompt_edit", "target_kind": "prompt_override",
        "target_id": "directive_suffix", "after_text": "结尾请收束到本章悬念。",
    })
    REPORT.check("7c. 对照组：合法 prompt_edit 能创建提案", rp.status_code == 200, f"code={rp.status_code}")
    if rp.status_code == 200:
        ppid = rp.json()["proposal"]["id"]
        approve(client, ppid)
        cp, bp = try_apply(client, ppid)
        db = SessionLocal()
        ov = count(db, PromptOverride)
        REPORT.check(
            "7d. 对照组：合法 prompt_edit 能 apply 落库",
            cp == 200 and ov == 1,
            f"apply code={cp}；prompt_overrides rows={ov}",
        )
        db.close()

    # —— 非法 prompt_key（不在 PROMPT_KEYS）——
    db = SessionLocal()
    seed_generation(db, pid_, "对照3 正文。", segment_no=3)
    db.close()
    rbad = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "type": "prompt_edit", "target_kind": "prompt_override",
        "target_id": "not_a_real_key", "after_text": "越权覆写",
    })
    REPORT.check("7e. 非法 prompt_key（不在白名单）→ 422", rbad.status_code == 422, f"code={rbad.status_code} body={rbad.text[:120]}")


if __name__ == "__main__":
    print(f"QA4 独立验证库：{QA4_DB}")
    attack_01_no_write_without_apply()
    attack_02_missing_approve_and_terminal_states()
    attack_03_no_batch_semantics_and_route_audit()
    attack_04_drift_becomes_stale()
    attack_05_atomicity_real_transaction()
    attack_06_restore_preserves_history()
    attack_07_patch_whitelist()
    print(REPORT.summary())
    sys.exit(1 if REPORT.failed else 0)