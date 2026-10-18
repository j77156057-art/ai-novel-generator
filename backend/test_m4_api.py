"""M4 API 层端到端验证：通过真实 HTTP 链路跑通 上传→建项目→建图谱→生成预览→续写→多段→查看。

端点覆盖：upload → create project → build-kg → generate-plan → generate → generate-loop → generations。
离线（embedding 本地兜底 + LLM 本地兜底）。

五段断言，分别锁五件不同的事：
  一、主干链路连通性（契约：请求体用 novel_ids 列表；缺省回退 project.reference_novel_ids）。
  二、P0-1 来源隔离——参考语料的角色/悬念**不得**成为本项目的生成约束（HTTP 层双向验证）。
  三、用户权威层——plot_bible（bible/user）经 HTTP 真实抵达提示词。
  四、请求体严格模式——拼错/旧字段必须 422，不得静默丢弃。
  五、层级前情——封卷后卷摘要与主线骨架**真的随 prompt 下发**，且覆盖证明无空洞。
"""

import os
import sys
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "ai_novel_m4_api.db")
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
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

# 参考语料里的事实（来自 fixtures.FIXTURE）——这些一旦出现在本项目约束里即为泄漏。
REF_CHAR_NAMES = ("林夜", "苏晚")
REF_HOLE_FRAGMENT = "那光是什么"


def _check(failures: list[str], ok: bool, msg: str, got=None) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {msg}" + ("" if got is None else f"  ← {got}"))
    if not ok:
        failures.append(msg + ("" if got is None else f"（实际：{got}）"))


def main() -> int:
    content = FIXTURE
    failures: list[str] = []

    with TestClient(app) as client:
        # ============ 一、主干链路 ============
        print("\n=== 一、主干链路 ===")

        # 1) 上传种子语料
        r = client.post(
            "/api/novels/upload",
            files={"file": ("fixture.txt", content.encode("utf-8"), "text/plain")},
        )
        if r.status_code != 200:
            failures.append(f"upload 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        novel_id = r.json()["novel_id"]
        print("upload ->", r.json())

        # 2) 建项目（带大纲，无 plot_bible）
        r = client.post(
            "/api/projects",
            json={
                "title": "种子项目",
                "mode": "quick",
                "target_length": 5000,
                "outline": ["林夜在旧宅发现一束来历不明的光", "苏晚携秘密登场"],
            },
        )
        if r.status_code != 200:
            failures.append(f"create_project 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        project_id = r.json()["id"]
        print("create_project ->", r.json())

        # 3) 建参考语料图谱（source=novel:{id}）
        r = client.post(f"/api/projects/{project_id}/build-kg?novel_id={novel_id}")
        if r.status_code != 200:
            failures.append(f"build-kg 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        print("build-kg ->", r.json())

        # 4) 生成前预览（plan）——请求体契约：novel_ids 列表
        r = client.post("/api/generate-plan", json={"project_id": project_id, "novel_ids": [novel_id]})
        if r.status_code != 200:
            failures.append(f"generate-plan 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        plan = r.json()
        print("generate-plan constraints ->", plan["constraints"])
        print("generate-plan rag_context 条数 ->", len(plan["rag_context"]))

        _check(failures, all(t in plan["system_prompt"] for t in ("角色人设约束", "未填伏笔")),
               "system_prompt 含 角色约束/伏笔 两个区块")
        # 旧断言要求 user_prompt 同时含「前情提要 / 检索上下文 / 大纲」三个扁平区块标题。
        # 该形态已被两次刻意变更取代：
        #   ① 前情改走分层 `prior_blocks`（[主线] / [卷 k｜段 a-b] / [近情·段 n]），
        #      不再有「前情提要」这个标题；
        #   ② v2.3 时间线分级投喂生效时**不注入整条【大纲】**（否则结局一次给全）。
        # 所以改为守真正稳定的不变量：user prompt 必须仍然是「续写指令」结构。
        # 分层前情本身由本文件后面的 prior_blocks 专用断言覆盖。
        _check(failures, "【续写指令】" in plan["user_prompt"],
               "user_prompt 含【续写指令】结构（前情/大纲形态已改为分层 prior_blocks + 节点提示）")
        _check(failures, isinstance(plan["rag_context"], list), "rag_context 为列表")

        # 5) 续写单段
        r = client.post("/api/generate", json={"project_id": project_id, "novel_ids": [novel_id]})
        if r.status_code != 200:
            failures.append(f"generate 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        gen = r.json()
        print("generate ->", {k: (v if k != "debug" else "<debug>") for k, v in gen.items()})
        _check(failures, gen.get("segment_no") == 1, "首段 segment_no 为 1", gen.get("segment_no"))

        # 6) 多段续写
        r = client.post(
            "/api/generate-loop",
            json={"project_id": project_id, "novel_ids": [novel_id], "segments": 2},
        )
        if r.status_code != 200:
            failures.append(f"generate-loop 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        loop = r.json()
        print("generate-loop segments ->", [s["segment_no"] for s in loop["segments"]])
        _check(failures, len(loop["segments"]) == 2, "loop 返回 2 段", len(loop["segments"]))

        # 7) 查看已生成段
        r = client.get(f"/api/generations/{project_id}")
        if r.status_code != 200:
            failures.append(f"generations 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        gens = r.json()
        print("generations count ->", len(gens))
        _check(failures, len(gens) == 3, "累计 3 段", len(gens))
        _check(failures, [g["segment_no"] for g in gens] == [1, 2, 3],
               "段号顺序为 [1,2,3]", [g["segment_no"] for g in gens])

        # 7.5) 层级前情真的下发了 —— 这是步骤 2 的**契约层**证据
        r = client.post("/api/generate-plan",
                        json={"project_id": project_id, "novel_ids": [novel_id]})
        _check(failures, r.status_code == 200, "第二段之后 generate-plan 返回 200", r.status_code)
        plan2 = r.json()
        blocks = plan2.get("prior_blocks") or []
        rep2 = plan2.get("prior_report") or {}
        print("  prior_blocks ->", blocks)
        print("  prior_report ->", {k: rep2.get(k) for k in
                                    ("n_segments", "sealed_volumes", "window", "uncovered",
                                     "degraded", "chars", "budget_chars")})
        _check(failures, len(blocks) >= 1, "plan 下发分层前情块 prior_blocks", len(blocks))
        _check(failures, any(b.startswith("[近情·段 ") for b in blocks),
               "前情块用分层标记（不再是 1. 2. 3. 的扁平编号）", blocks[:1])
        # 关键：user_prompt 是**真的**从 prior_blocks 渲染的（不是只挂在响应里好看）
        _check(failures, all(b in plan2["user_prompt"] for b in blocks),
               "user_prompt 确实由 prior_blocks 渲染而成")
        _check(failures, rep2.get("uncovered") == [],
               "覆盖证明 uncovered 为空（无信息空洞）", rep2.get("uncovered"))
        _check(failures, rep2.get("n_segments") == 3, "report.n_segments 与已写段数一致",
               rep2.get("n_segments"))

        # ============ 二、P0-1 来源隔离（HTTP 层）============
        print("\n=== 二、P0-1 来源隔离 ===")

        # 正向：参考语料图按 source=novel:{id} 查得到
        r = client.get(f"/api/projects/{project_id}/reminders?source=novel:{novel_id}")
        _check(failures, r.status_code == 200, "reminders?source=novel 返回 200", r.status_code)
        ref = r.json()
        ref_names = [c["name"] for c in ref["character_constraints"]]
        print("  novel 侧角色 ->", ref_names)
        _check(failures, all(n in ref_names for n in REF_CHAR_NAMES),
               "novel 侧图谱含参考语料角色", ref_names)
        print("  novel 侧未填坑 ->", [h["question"] for h in ref["unresolved_plotholes"]])
        _check(failures, any(REF_HOLE_FRAGMENT in (h["question"] or "")
                             for h in ref["unresolved_plotholes"]),
               "novel 侧图谱含参考语料悬念")

        # 反向：默认（generated）不得回吐参考语料事实
        r = client.get(f"/api/projects/{project_id}/reminders")
        _check(failures, r.status_code == 200, "reminders 默认返回 200", r.status_code)
        gen_side = r.json()
        gen_names = [c["name"] for c in gen_side["character_constraints"]]
        gen_holes = [h["question"] for h in gen_side["unresolved_plotholes"]]
        print("  generated 侧角色 ->", gen_names)
        print("  generated 侧未填坑 ->", gen_holes)
        leaked = [n for n in REF_CHAR_NAMES if n in gen_names]
        _check(failures, not leaked, "generated 侧不含参考语料角色", leaked or "无")

        # plan 的提示词层面：参考语料事实不得进 system_prompt / constraints
        leaked_sys = [n for n in REF_CHAR_NAMES if n in plan["system_prompt"]]
        _check(failures, not leaked_sys, "system_prompt 无参考语料角色泄漏", leaked_sys or "无")
        _check(failures, REF_HOLE_FRAGMENT not in plan["system_prompt"],
               "system_prompt 无参考语料悬念泄漏")
        # 注意：RAG 片段（风格样本）**应当**出现在 user_prompt 的【检索上下文】里——
        # 这是「参考语料只做风格样本」的设计，与上面的约束泄漏是两件事。
        _check(failures, REF_HOLE_FRAGMENT not in (gen.get("text") or ""),
               "本段正文未被参考语料悬念约束回显")

        # ============ 三、用户权威层（plot_bible）经 HTTP 生效 ============
        print("\n=== 三、用户权威层 plot_bible ===")
        r = client.post(
            "/api/projects",
            json={"title": "权威层项目", "mode": "quick", "target_length": 5000,
                  "outline": ["沈砚接手一桩旧案"]},
        )
        if r.status_code != 200:
            failures.append(f"create_project#2 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        p2 = r.json()["id"]
        bible = {
            "characters": [{"name": "沈砚", "role": "主角", "motivation": "查明父亲死因"}],
            "foreshadowing": ["铜镜背面的裂纹"],
            "synopsis": "一桩尘封十年的旧案",
            "direction": "线索逐步指向警队内部",
            "conflicts": ["信任与怀疑"],
            "relationships": [{"a": "沈砚", "b": "赵启", "relation": "师徒"}],
        }
        r = client.patch(f"/api/projects/{p2}", json={"plot_bible": bible})
        _check(failures, r.status_code == 200, "PATCH plot_bible 返回 200", r.status_code)

        r = client.post("/api/generate-plan", json={"project_id": p2})
        if r.status_code != 200:
            failures.append(f"generate-plan#2 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        plan2 = r.json()
        sys2 = plan2["system_prompt"]
        print("  项目2 constraints ->", plan2["constraints"])
        print("  项目2 rag_context 条数 ->", len(plan2["rag_context"]))
        _check(failures, "沈砚" in sys2, "bible 角色进入 system_prompt")
        _check(failures, "铜镜背面的裂纹" in sys2, "bible 伏笔进入 system_prompt")
        # v2.3 起：bible 的 synopsis/direction **只进 system 提示，不再进 user prompt**
        # ——时间线分级投喂把它拦在 user 侧（一次给全结局走向会让第一章就讲完）。
        # 上面已断言沈砚与铜镜进入 system_prompt；这里反过来守住「user 侧不得再出现」，
        # 防的是有人把整段 bible 又塞回 user 把分级投喂废掉。
        _check(failures,
               "一桩尘封十年的旧案" not in plan2["user_prompt"]
               and "铜镜背面的裂纹" not in plan2["user_prompt"],
               "bible 梗概不进 user_prompt（v2.3 分级投喂：只由 system 承载）")
        # 未选参考语料 → 不得凭空检索
        _check(failures, plan2["rag_context"] == [], "未选参考语料时 RAG 为空", plan2["rag_context"])
        # bible 层必须同时进 system_prompt **和** constraints（后者会被 provider 追加到
        # user 末尾做近因提醒）。两处口径必须一致，否则尾部那份自称完整的锁定名单会漏掉
        # 最高权威层——这正是本项修复要防的。
        _check(failures, "沈砚" in (plan2["constraints"]["characters"] or []),
               "bible 角色进入 constraints（与 system 同口径）",
               plan2["constraints"]["characters"])
        _check(failures, any("铜镜背面的裂纹" in (h or "")
                             for h in plan2["constraints"]["plotholes"]),
               "bible 伏笔进入 constraints（与 system 同口径）",
               plan2["constraints"]["plotholes"])

        # ============ 四、请求体严格模式（拼错字段 fail loud）============
        print("\n=== 四、请求体严格模式 ===")
        # 未知/拼错字段必须 422，而不是 200 + 静默丢弃。根治的是这一类「静默无效」故障：
        # novel_id 改名成 novel_ids 那次，旧字段被 pydantic 默认的 extra="ignore" 悄悄丢掉，
        # 接口照样 200，只是参考语料没生效——更难的是它连日志都没有，只有结果不对。
        r = client.post("/api/generate-plan", json={"project_id": project_id, "novel_id": novel_id})
        _check(failures, r.status_code == 422,
               "旧字段 novel_id 被拒（422 而非静默 200）", r.status_code)
        r = client.post("/api/generate-plan", json={"project_id": project_id, "novel_ids": [novel_id]})
        _check(failures, r.status_code == 200, "正确字段 novel_ids 仍正常（200）", r.status_code)
        r = client.post("/api/projects", json={"title": "拼错字段", "titel": "typo"})
        _check(failures, r.status_code == 422, "项目创建体拼错字段被拒（422）", r.status_code)
        r = client.patch(f"/api/projects/{project_id}", json={"plot_biblee": {}})
        _check(failures, r.status_code == 422, "PATCH 体拼错字段被拒（422）", r.status_code)
        r = client.post(f"/api/projects/{project_id}/build-bible", json={"answer": {"a": "b"}})
        _check(failures, r.status_code == 422, "向导 answers 拼错字段被拒（422）", r.status_code)

        # ============ 五、层级前情：封卷后卷摘要/主线骨架随 prompt 下发 ============
        print("\n=== 五、层级前情（封卷后才有卷摘要与主线骨架）===")
        r = client.post("/api/projects", json={"title": "层级前情项目", "mode": "quick",
                                              "target_length": 300000})
        hier_id = r.json()["id"]
        # 段数要过**卷层门槛** vsize + recent_min = 8 + 3 = 11，卷摘要才会参与前情
        # （不足 11 段时近情窗口必然覆盖全部，这是刻意行为）。
        # 取 19 段：19 = 2×8 + 3，余数恰好等于 recent_min → 近情窗口只占 3 段，
        # 预算留得下卷摘要。（余数越大窗口越长：N=17 时窗口 9 段，会把预算吃光。）
        for _ in range(19):
            rr = client.post("/api/generate", json={"project_id": hier_id})
            if rr.status_code != 200:
                failures.append(f"层级项目 generate 失败 {rr.status_code}: {rr.text}")
                break
        r = client.post("/api/generate-plan", json={"project_id": hier_id})
        hp = r.json()
        blocks = hp.get("prior_blocks") or []
        rep = hp.get("prior_report") or {}
        print("  prior_blocks ->", [b[:34] + "…" for b in blocks])
        print("  prior_report ->", {k: rep.get(k) for k in
                                    ("n_segments", "sealed_volumes", "window",
                                     "l2_rendered", "l2_dropped", "uncovered",
                                     "degraded", "over_budget", "chars", "budget_chars")})
        _check(failures, rep.get("n_segments") == 19, "19 段已入树", rep.get("n_segments"))
        _check(failures, rep.get("sealed_volumes") == 2, "已封 2 卷（段 1-16）",
               rep.get("sealed_volumes"))
        _check(failures, rep.get("window") == [17, 19],
               "近情窗口落在卷边界（最后 3 段）", rep.get("window"))
        _check(failures, any(b.startswith("[卷 1｜段 1-8]") for b in blocks),
               "卷 1 摘要随 prompt 下发（[卷 1｜段 1-8]）", [b[:16] for b in blocks])
        _check(failures, any(b.startswith("[卷 2｜段 9-16]") for b in blocks),
               "卷 2 摘要随 prompt 下发（[卷 2｜段 9-16]）", [b[:16] for b in blocks])
        _check(failures, any(b.startswith("[主线·覆盖至卷 2（段 1-16）]") for b in blocks),
               "主线骨架随 prompt 下发且带覆盖声明", [b[:24] for b in blocks])
        _check(failures, any(b.startswith("[近情·段 19]") for b in blocks),
               "当前未封卷以逐段全文下发", blocks[-1:])
        _check(failures, rep.get("l2_dropped") == [], "预算够用，卷摘要未丢",
               rep.get("l2_dropped"))
        _check(failures, rep.get("uncovered") == [],
               "覆盖证明 uncovered 为空（1..19 无空洞）", rep.get("uncovered"))
        _check(failures, all(b in hp["user_prompt"] for b in blocks),
               "user_prompt 由这些分层块渲染而成")
        _check(failures, rep.get("chars", 10 ** 9) <= rep.get("budget_chars", 0),
               "前情块在预算内", f"{rep.get('chars')} / {rep.get('budget_chars')}")
        _check(failures, not rep.get("degraded"), "粗层齐备，未走退化档", rep.get("degraded"))
        print(f"  分层构成：主线 1 + 卷摘要 {len(rep.get('l2_rendered') or [])} + "
              f"近情 {rep['window'][1] - rep['window'][0] + 1} 段 = {rep.get('chars')} 字")

    if failures:
        print("\n❌ M4 API 验证失败：")
        for f in failures:
            print("  -", f)
        return 1
    print("\n✅ M4 API 层端到端验证通过：主干链路 / P0-1 来源隔离 / plot_bible 权威层 / "
          "请求体严格模式 / 层级前情下发 全部符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
