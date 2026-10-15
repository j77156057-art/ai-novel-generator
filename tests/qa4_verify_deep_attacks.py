"""QA4 第二轮深挖：绕过漂移检测 / 伪造 diff / 跨项目 restore。

这些是独立于第一轮的场景，专门针对「请求体可控字段能否污染服务端计算结果」。
每个函数自带__main__ 入口，可单独跑。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa4_verify_harness import (  # noqa: E402
    QA4_DB,
    Report,
    SessionLocal,
    approve,
    business_unchanged,
    count,
    diff_snapshots,
    get_text,
    init_db,
    make_client,
    new_proposal,
    proposal_status,
    reset_db,
    seed_generation,
    seed_project,
    snapshot_all,
    try_apply,
    try_restore,
)
from sqlalchemy import text  # noqa: E402

REPORT = Report()
init_db()


def _last_gen_id(db) -> int:
    return db.execute(text("SELECT id FROM generations ORDER BY id DESC LIMIT 1")).scalar()


# ====================================================================
# A. scope 注入：能否关掉漂移检测 / 伪造审批人看到的 diff
# ====================================================================
def attack_A_scope_injection() -> None:
    REPORT.section("A. scope 注入攻击：请求体 scope 能否覆盖 base_content_hash / diff / impact")
    reset_db()
    client = make_client()
    db = SessionLocal()
    proj = seed_project(db, "A-scope 注入")
    pid_ = proj.id
    seed_generation(db, pid_, "原始正文：铜钥匙藏在门后。")
    gid = _last_gen_id(db)

    # —— A1: scope.base_content_hash="" 是否能关掉漂移检测 ——
    c = new_proposal(
        client, pid_, gid,
        "提案想写的内容：铜钥匙被张三取出。",
        reason="A1 绕过漂移检测",
        scope={"base_content_hash": ""},
    )["proposal"]
    stored_hash = db.execute(
        text("SELECT json_extract(scope_json,'$.base_content_hash') FROM edit_proposals WHERE id=:i"),
        {"i": c["id"]},
    ).scalar()
    # 服务端自己算的真基线（对同一份原文两次哈希，结果必然相同）
    import hashlib

    true_hash = hashlib.sha256("原始正文：铜钥匙藏在门后。".encode("utf-8")).hexdigest()
    REPORT.check(
        "A1-a. 实测：请求体 scope 里的 base_content_hash **被写进了库**，覆盖了服务端算的真基线",
        stored_hash == "" and true_hash != "",
        f"真基线 sha256(原文)={true_hash[:16]}…（服务端本应存这个）；"
        f"实际入库 json_extract(...)= {stored_hash!r}  ← 请求体的空串把漂移判据清空了",
    )

    approve(client, c["id"])
    # 制造漂移：用户手动改了目标
    db.execute(text("UPDATE generations SET text=:t WHERE id=:i"),
               {"t": "用户在审批期间手写的正文：钥匙其实是银的。", "i": gid})
    db.commit()
    manual = get_text(db, gid)

    code, body = try_apply(client, c["id"])
    after_manual = get_text(db, gid)
    REPORT.check(
        "A1-b. 【P0】漂移检测被完全绕过：手改内容被静默覆盖，apply 返回 200",
        code == 200,
        f"apply code={code}（期望 409 stale）；detail={json.dumps(body, ensure_ascii=False)[:200]}",
    )
    REPORT.check(
        "A1-c. 【P0】用户手写的新正文被覆盖成提案内容",
        after_manual != manual,
        f"手写内容={manual!r} → apply 后={after_manual!r}（用户的手改被丢了）",
    )
    REPORT.check(
        "A1-d. 提案状态推进为 applied（而非 stale）",
        proposal_status(db, c["id"]) == "applied",
        f"status={proposal_status(db, c['id'])}",
    )

    # —— A1-e: 变体 ———
    # (i)base_content_hash=null
    reset_db()
    db = SessionLocal()
    client = make_client()
    p = seed_project(db, "A1e 变体")
    seed_generation(db, p.id, "变体测试原文。", segment_no=1)
    gv = _last_gen_id(db)
    db.close()
    v_null = new_proposal(client, p.id, gv, "变体写的内容。", reason="v-null",
                          scope={"base_content_hash": None})["proposal"]
    approve(client, v_null["id"])
    db = SessionLocal()
    db.execute(text("UPDATE generations SET text='用户手改的变体正文。' WHERE id=:i"), {"i": gv})
    db.commit()
    db.close()
    c_null, _ = try_apply(client, v_null["id"])

    # (ii) base_content_hash = 伪造的 64 位 hex（无需算哈希，纯伪造）
    reset_db()
    db = SessionLocal()
    client = make_client()
    p2 = seed_project(db, "A1e 变体2")
    seed_generation(db, p2.id, "变体测试原文2。", segment_no=1)
    gv2 = _last_gen_id(db)
    db.close()
    v_fake = new_proposal(client, p2.id, gv2, "变体2 写的内容。", reason="v-fake",
                          scope={"base_content_hash": "0" * 64})["proposal"]
    approve(client, v_fake["id"])
    db = SessionLocal()
    db.execute(text("UPDATE generations SET text='用户手改的变体2正文。' WHERE id=:i"), {"i": gv2})
    db.commit()
    db.close()
    c_fake, _ = try_apply(client, v_fake["id"])

    REPORT.check(
        "A1-e. 【P0】漂移绕过仅在base_content_hash 为**falsy** 时成立（精确根因）",
        c_null == 200 and c_fake == 409,
        f"scope.base_content_hash=null → apply={c_null}（绕过成功，期望 409）；"
        f"scope.base_content_hash='000…0'（非空 64hex）→ apply={c_fake}（正确判 stale）。"
        "根因：proposals.py:779 `if expected_hash and current.hash != expected_hash` "
        "—— expected_hash 为空/None 时整个漂移分支被短路跳过；"
        "填任意非空假值反而会被比对出来。",
    )

    # —— A2: scope.diff 能否伪造审批人看到的 diff ——
    reset_db()
    db = SessionLocal()
    client = make_client()
    proj = seed_project(db, "A2 伪造 diff")
    pid_ = proj.id
    seed_generation(db, pid_, "原始正文：铜钥匙藏在门后。\n第二行原文。\n第三行原文。")
    gid2 = _last_gen_id(db)
    c2 = new_proposal(
        client, pid_, gid2,
        "这里是一段全新的、与原文毫无关系的替换内容，会整段覆盖掉原来的正文。",
        reason="A2 伪造 diff",
        scope={"diff": "", "impact": "仅修正一个错别字", "target_snapshot": {"content_hash": "fake"}},
    )["proposal"]
    REPORT.check(
        "A2-a. 【P1】请求体 scope 里的 diff 覆盖了服务端算的真diff（审批人看到空白）",
        c2["diff"] == "",
        f"入库 diff={c2['diff']!r}（服务端本应给出真实 unified diff）；"
        f"impact={c2['impact']!r} ← 被改成 '仅修正一个错别字'",
    )
    REPORT.check(
        "A2-b. target_snapshot.content_hash 也可被伪造为任意值",
        db.execute(
            text("SELECT json_extract(scope_json,'$.target_snapshot.content_hash') FROM edit_proposals WHERE id=:i"),
            {"i": c2["id"]},
        ).scalar() == "fake",
        "scope_json.target_snapshot.content_hash='fake'（真实基线已被覆盖）",
    )
    detail = client.get(f"/api/assistant/proposals/{c2['id']}").json()["proposal"]
    REPORT.check(
        "A2-c. 详情端点同样返回被伪造的 diff/impact（GET 不做二次净化）",
        detail["diff"] == "" and detail["impact"] == "仅修正一个错别字",
        f"GET 返回 diff={detail['diff']!r} impact={detail['impact']!r}",
    )
    REPORT.note(
        "注：before_text / after_text 两列未被污染（仍是服务端算的真值），"
        "所以前端若只渲染 before/after 就不受影响；但任何按 diff/impact 展示审批理由的"
        "UI 都会被绕过。这是「审批依据可由提案作者伪造」的信任边界问题。"
    )
    db.close()


# ====================================================================
# B. 跨项目 restore / 跨项目 target
# ====================================================================
def attack_B_cross_project() -> None:
    REPORT.section("B. 跨项目攻击：restore 能否写到别的项目的段落下")
    reset_db()
    client = make_client()
    db = SessionLocal()

    p1 = seed_project(db, "B1 项目一")
    p2 = seed_project(db, "B2 项目二")
    seed_generation(db, p1.id, "项目一的正文。", segment_no=1)
    g1 = _last_gen_id(db)
    seed_generation(db, p2.id, "项目二的正文，绝不该被项目一的操作改动。", segment_no=1)
    g2 = _last_gen_id(db)
    db.close()

    db = SessionLocal()
    c = new_proposal(client, p1.id, g1, "项目一改写后的正文。", reason="B")["proposal"]
    approve(client, c["id"])
    db.close()
    code, body = try_apply(client, c["id"])
    vid = body["version_id"]
    db = SessionLocal()
    src_pid = body["proposal_id"]
    db.close()

    # 场景1：把 version.project_id 篡改成项目二，但target_id 仍指向项目一的段
    db = SessionLocal()
    g1_before = get_text(db, g1)
    g2_before = get_text(db, g2)
    db.execute(text("UPDATE versions SET project_id=:p WHERE id=:v"), {"p": p2.id, "v": vid})
    db.commit()
    db.close()
    code1, body1 = try_restore(client, vid)
    db = SessionLocal()
    t1 = get_text(db, g1)
    t2_after = get_text(db, g2)
    REPORT.check(
        "B1. project_id 与 target_id 不匹配（指向不同项目）→ 404，两个项目正文都没变",
        code1 == 404 and t1 == g1_before and t2_after == g2_before,
        f"code={code1}（detail={json.dumps(body1, ensure_ascii=False)[:150]}）；"
        f"g1 {g1_before!r} → {t1!r}；g2 {g2_before!r} → {t2_after!r}",
    )
    db.close()

    # 场景2：真正的越权面 —— version.project_id 与 target_id 都指向项目二
    db = SessionLocal()
    db.execute(
        text("UPDATE versions SET project_id=:p, snapshot_json=json_set(snapshot_json,'$.target_id',:t) WHERE id=:v"),
        {"p": p2.id, "t": str(g2), "v": vid},
    )
    db.commit()
    db.close()
    before_g2 = get_text(SessionLocal(), g2)
    code2, body2 = try_restore(client, vid)
    db = SessionLocal()
    after_g2 = get_text(db, g2)
    REPORT.check(
        "B2. 【P1】篡改 version 使 project_id 与 target_id 同属项目二 → restore 成功**写进了项目二**",
        code2 == 200 and after_g2 != before_g2,
        f"code={code2}；项目二 text: {before_g2!r} → {after_g2!r}；"
        f"body={json.dumps({k: v for k, v in body2.items() if k not in ('restored_from',)}, ensure_ascii=False)[:200]}",
    )
    REPORT.note(
        "结论：restore 端点不做「当前操作者是否有权操作该项目」的校验（单用户阶段无RBAC），"
        "其写入范围完全由 versions 行里的 project_id + snapshot.target_id 决定。"
        "攻击前提是攻击者能改 versions 表（DB 层信任）。若 DB 层可信，这只是「按数据写」的"
        "既定语义，不构成 API 层越权—— 但 ADR §7 的 RBAC 未决项落地前，它是明确的口子。"
    )
    db.close()

    # 场景3：提案创建时 run_id 指向别的项目的 AgentRun（审计污染）
    db = SessionLocal()
    db.execute(
        text("INSERT INTO agent_runs (project_id, role, status, started_at, finished_at, request_hash, model)"
             " VALUES (:p, 'assistant', 'success', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'h', 'x')"),
        {"p": p2.id},
    )
    db.commit()
    other_run = db.execute(text("SELECT MAX(id) FROM agent_runs")).scalar()
    db.close()

    db = SessionLocal()
    seed_generation(db, p1.id, "run_id 审计污染用正文。", segment_no=9)
    g3 = _last_gen_id(db)
    db.close()
    r = client.post("/api/assistant/proposals", json={
        "project_id": p1.id, "type": "text_edit", "target_kind": "generation_text",
        "target_id": str(g3), "after_text": "审计污染测试。", "run_id": other_run,
    })
    REPORT.check(
        "B3. 【P2】可用别的项目的 AgentRun.id 作为本项目提案的 run_id（审计归属被污染）",
        r.status_code == 200,
        f"code={r.status_code}；proposal.run_id={r.json()['proposal']['run_id'] if r.status_code==200 else '-'}"
        f"（该 run 属于项目 {p2.id}，提案却挂在项目 {p1.id} 下）",
    )


# ====================================================================
# C. 其它边角攻击
# ====================================================================
def attack_C_misc_edges() -> None:
    REPORT.section("C. 边角攻击：状态机绕过 / 超长文本 / fact_resolution 非 JSON / 空提案")
    reset_db()
    client = make_client()
    db = SessionLocal()
    proj = seed_project(db, "C-边角")
    pid_ = proj.id
    seed_generation(db, pid_, "正文：铜钥匙在门后。")
    gid = _last_gen_id(db)
    db.close()

    # C1: after_text 与原文完全一致 → 应拒（空提案）
    r = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "type": "text_edit", "target_kind": "generation_text",
        "target_id": str(gid), "after_text": "正文：铜钥匙在门后。",
    })
    REPORT.check("C1. after_text 与目标完全一致 → 422（不生成空提案）", r.status_code == 422,
                 f"code={r.status_code} body={r.text[:140]}")

    # C2: fact_resolution 的 after_text 非法 JSON
    r2 = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "type": "fact_resolution", "target_kind": "project_plot_bible",
        "target_id": "plot_bible", "after_text": "{不是合法 json",
    })
    REPORT.check("C2. fact_resolution 的 after_text 非法 JSON → 422", r2.status_code == 422,
                 f"code={r2.status_code} body={r2.text[:140]}")

    # C3: fact_resolution 的 after_text 是 JSON 数组（plot_bible 应为对象）
    r3 = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "type": "fact_resolution", "target_kind": "project_plot_bible",
        "target_id": "plot_bible", "after_text": "[1,2,3]",
    })
    REPORT.check("C3. fact_resolution 的 after_text 是数组 → 422（plot_bible 必须是对象）", r3.status_code == 422,
                 f"code={r3.status_code} body={r3.text[:140]}")

    # C4: text_edit 的 target_id 非整数
    r4 = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "type": "text_edit", "target_kind": "generation_text",
        "target_id": "abc", "after_text": "x",
    })
    REPORT.check("C4. text_edit 的 target_id 非整数 → 422", r4.status_code == 422, f"code={r4.status_code}")

    # C5: 目标 generation 属于别的项目
    db = SessionLocal()
    other = seed_project(db, "C-别的项目")
    seed_generation(db, other.id, "别的项目的段。", segment_no=1)
    other_gid = _last_gen_id(db)
    db.close()
    r5 = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "type": "text_edit", "target_kind": "generation_text",
        "target_id": str(other_gid), "after_text": "跨项目改写",
    })
    REPORT.check("C5. target generation 属于别的项目 → 404（TargetMissing）", r5.status_code == 404,
                 f"code={r5.status_code} body={r5.text[:140]}")

    # C6: 超长 after_text
    r6 = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "type": "text_edit", "target_kind": "generation_text",
        "target_id": str(gid), "after_text": "长" * 200_001,
    })
    REPORT.check("C6. after_text 超长（>200000）→ 422", r6.status_code == 422, f"code={r6.status_code}")

    # C7: 不存在的 proposal → 404；不存在的 project → 404
    c404 = client.post("/api/assistant/proposals/999999/approve", json={"action": "approve"})
    p404 = client.post("/api/assistant/proposals", json={
        "project_id": 999999, "type": "text_edit", "target_kind": "generation_text",
        "target_id": "1", "after_text": "x",
    })
    REPORT.check("C7. 不存在的 proposal / project → 404", c404.status_code == 404 and p404.status_code == 404,
                 f"proposal={c404.status_code} project={p404.status_code}")

    # C8: 缺 action 字段 → 422
    c8 = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "target_kind": "generation_text", "target_id": str(gid), "after_text": "x",
    })
    c9 = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "type": "text_edit", "target_kind": "generation_text",
        "target_id": str(gid), "after_text": "x", "type_unknown": 1,
    })
    REPORT.check("C8. 创建提案缺 type → 422；带未知字段 → 422", c8.status_code == 422 and c9.status_code == 422,
                 f"缺 type={c8.status_code}；未知字段={c9.status_code}")

    # C9: action 与路径动作不匹配
    c = new_proposal(client, pid_, gid, "动作不匹配测试正文。")["proposal"]
    x1 = client.post(f"/api/assistant/proposals/{c['id']}/approve", json={"action": "apply"})
    x2 = client.post(f"/api/assistant/proposals/{c['id']}/apply", json={"action": "approve"})
    x3 = client.post(f"/api/assistant/proposals/{c['id']}/reject", json={"action": "reject"})
    REPORT.check(
        "C9. action 与路径动作不匹配 → 422（apply 端点不能被当 approve 用）",
        x1.status_code == 422 and x2.status_code == 422 and x3.status_code == 200,
        f"approve端点传 apply={x1.status_code}；apply端点传 approve={x2.status_code}；reject 端点传 reject={x3.status_code}",
    )

    # C10: 非法 risk_level
    r10 = client.post("/api/assistant/proposals", json={
        "project_id": pid_, "type": "text_edit", "target_kind": "generation_text",
        "target_id": str(gid), "after_text": "风险等级测试。", "risk_level": "critical",
    })
    REPORT.check("C10. 非法 risk_level → 422", r10.status_code == 422, f"code={r10.status_code}")

    # C11: 默认风险等级映射
    db = SessionLocal()
    r11 = new_proposal(client, pid_, gid, "text_edit 默认风险等级。", risk_level=None)
    REPORT.note(
        f"默认 risk_level 映射：fact_resolution=high / text_edit=medium / prompt_edit=low"
        f"（proposals.py:645-649）；本次显式未传 → {r11['proposal']['risk_level']!r}"
    )
    db.close()

    # C12: 一个提案反复 approve（approved 后再 approve）
    c12 = new_proposal(client, pid_, gid, "重复 approve 测试。")["proposal"]
    a1 = client.post(f"/api/assistant/proposals/{c12['id']}/approve", json={"action": "approve"})
    a2 = client.post(f"/api/assistant/proposals/{c12['id']}/approve", json={"action": "approve"})
    REPORT.check("C12. approved 后重复 approve → 409", a1.status_code == 200 and a2.status_code == 409,
                 f"首次={a1.status_code} 二次={a2.status_code}")


if __name__ == "__main__":
    print(f"QA4 独立验证库：{QA4_DB}")
    attack_A_scope_injection()
    attack_B_cross_project()
    attack_C_misc_edges()
    print(REPORT.summary())
    sys.exit(1 if REPORT.failed else 0)