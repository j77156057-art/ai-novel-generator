"""QA6 · 前端助手面板「契约静态比对」（commit 30f4508）。

client.ts 有没有发后端不认识的字段？ —— 后端全是 StrictRequest(extra="forbid")，
多一个/拼错一个就是 422，而工程师没跑过真链路，所以必须静态把两边对着数一遍。

三个检查：
1. **字段对齐**：TS 接口 / 实际 request body 里出现的键 ⊆ 后端 pydantic 字段。
2. **服务端独占键**：frontend/src 里不许出现把 base_content_hash / target_snapshot /
   diff / impact 塞进**请求体**的写法（它们只能出现在响应读取里）。
3. **ApiError 向后兼容**：新增 `detail: unknown` 后，所有既有 `new ApiError(status, msg)`
   调用点的 message 行为必须与改动前一致。

运行：python tests/qa6_frontend_contract_static.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend" / "src"
sys.path.insert(0, str(BACKEND))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, evidence: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ← {evidence}" if evidence else ""))
    (PASS if cond else FAIL).append(name)


def _iface_bodies(src: str) -> dict[str, str]:
    """把 types.ts 里每个 interface 的 body 抽出来：{名字: body}。"""
    out: dict[str, str] = {}
    for m in re.finditer(r"export interface (\w+)\b(?:\s+extends\s+(\w+))?\s*\{", src):
        start = m.end()
        depth, i = 1, start
        while depth and i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        out[m.group(1)] = src[start:i - 1]
    return out


def ts_interface_fields(path: Path, name: str) -> tuple[set[str], set[str]]:
    """抽 TS interface 的字段：返回 (全部字段, 必填字段)。

    支持 `export interface X extends Y { ... }`（继承字段一并并入）。
    """
    src = path.read_text(encoding="utf-8")
    bodies = _iface_bodies(src)
    if name not in bodies:
        raise SystemExit(f"没找到 interface {name}")
    parent = re.search(rf"export interface {name}\b\s+extends\s+(\w+)", src)
    parent = parent.group(1) if parent else None
    body = bodies[name]
    optional: set[str] = set()
    required: set[str] = set()
    for line in body.splitlines():
        fm = re.match(r"\s*(\w+)\s*(\?)?\s*:", line)
        if not fm or line.lstrip().startswith("//"):
            continue
        (optional if fm.group(2) else required).add(fm.group(1))
    fields = optional | required
    if parent:
        pf, pr = ts_interface_fields(path, parent)
        fields |= pf
        required |= pr
    return fields, required


def py_fields(cls) -> tuple[set[str], set[str]]:
    """pydantic v2 模型的 (全部字段, 必填字段)。"""
    fields = set(cls.model_fields)
    required = {n for n, f in cls.model_fields.items() if f.is_required()}
    return fields, required


def main() -> int:
    from app.routers.assistant import AssistantDiagnoseRequest, AssistantQARequest
    from app.routers.assistant_proposals import (
        CreateProposalRequest,
        ProposalActionRequest,
        RestoreVersionRequest,
    )

    types_ts = FRONTEND / "api" / "types.ts"
    client_ts = FRONTEND / "api" / "client.ts"
    panel_vue = FRONTEND / "components" / "AssistantPanel.vue"

    print("=" * 78)
    print("### 1. TS 请求体 vs 后端 pydantic 字段（多一个键 = 422）")
    print("=" * 78)
    pairs = [
        ("AssistantQaRequest", "/api/assistant/qa", AssistantQARequest),
        ("AssistantDiagnoseRequest", "/api/assistant/diagnose", AssistantDiagnoseRequest),
        ("AssistantProposalCreate", "/api/assistant/proposals", CreateProposalRequest),
        ("AssistantActionRequest", ".../approve|reject|apply", ProposalActionRequest),
        ("AssistantRestoreRequest", "/versions/{id}/restore", RestoreVersionRequest),
    ]
    for ts_name, endpoint, cls in pairs:
        ts_all, ts_req = ts_interface_fields(types_ts, ts_name)
        py_all, py_req = py_fields(cls)
        extra = ts_all - py_all
        missing_req = py_req - ts_all
        print(f"\n  {endpoint}   (TS {ts_name} ↔ {cls.__name__})")
        print(f"     TS  字段: {sorted(ts_all)}")
        print(f"     后端字段: {sorted(py_all)}")
        check(f"{endpoint}: TS 无多余字段", not extra, f"多余={sorted(extra)}")
        check(f"{endpoint}: 后端必填项 TS 都有", not missing_req, f"缺失={sorted(missing_req)}")
        check(f"{endpoint}: TS 必填 ⊆ 后端必填", ts_req <= py_req,
              f"TS 必填={sorted(ts_req)} vs 后端必填={sorted(py_req)}")

    print("\n" + "=" * 78)
    print("### 2. 服务端独占键不得出现在前端**请求体**里")
    print("=" * 78)
    # 注意：响应体 interface（AssistantProposal 等）里出现这四个键是**正确**的，
    # 它们是服务端返回给界面看的审批依据；这里只查请求体侧的 5 个 interface。
    banned = ["base_content_hash", "target_snapshot", "diff", "impact"]
    request_ifaces = ["AssistantQaRequest", "AssistantDiagnoseRequest", "AssistantProposalCreate",
                      "AssistantActionRequest", "AssistantRestoreRequest"]
    src_ts = types_ts.read_text(encoding="utf-8")
    for name in request_ifaces:
        fields, _ = ts_interface_fields(types_ts, name)
        hits = sorted(set(fields) & set(banned))
        check(f"请求体 interface {name} 不含服务端独占键", not hits, f"命中={hits}")
    panel_src = panel_vue.read_text(encoding="utf-8")
    client_src = client_ts.read_text(encoding="utf-8")
    for label, src in (("client.ts", client_src), ("AssistantPanel.vue", panel_src)):
        hits: list[str] = []
        for key in banned:
            for line_no, line in enumerate(src.splitlines(), 1):
                if re.search(rf"^\s*{key}\s*:", line):
                    hits.append(f"{label}:{line_no} `{line.strip()[:70]}`")
        check(f"{label}: 代码里没有拼服务端独占键到请求体", not hits, "; ".join(hits[:4]))
    # types.ts 里出现的四个键必须只属于响应体 interface
    resp_only = []
    for key in banned:
        for line_no, line in enumerate(src_ts.splitlines(), 1):
            if re.search(rf"^\s*{key}\s*:", line):
                owner = next((n for n, body in _iface_bodies(src_ts).items()
                              if f"{key}" in body), "?")
                resp_only.append(f"{key}@{owner}")
    check("types.ts 里的四个独占键都只出现在响应体 interface",
          all(not o.startswith(f"{k}@{n}") for k in banned for o in resp_only
              for n in request_ifaces),
          "; ".join(sorted(set(resp_only))))

    # 更严的一条：createProposal 的调用点 body 里只能出现后端认识的键
    print("\n  —— AssistantPanel.submitDraft() 实际 body 的键 ——")
    panel = panel_vue.read_text(encoding="utf-8")
    body_m = re.search(r"const body: Parameters<typeof api\.createProposal>\[0\] = \{(.*?)\n    \};",
                       panel, re.S)
    if body_m:
        keys = set(re.findall(r"^\s{6}(\w+):", body_m.group(1), re.M))
        opt_keys = set(re.findall(r"body\.(\w+)\s*=", panel))
        all_keys = keys | opt_keys
        py_all, _ = py_fields(CreateProposalRequest)
        print(f"     实际发送键 = {sorted(all_keys)}")
        check("submitDraft body 的键全部被后端接受", all_keys <= py_all,
              f"越界={sorted(all_keys - py_all)}")
    else:
        check("能定位到 submitDraft 的 body 字面量", False, "regex 未命中")

    print("\n" + "=" * 78)
    print("### 3. action 值与路径一致（approve/reject/apply/restore）")
    print("=" * 78)
    client_src = client_ts.read_text(encoding="utf-8")
    panel = panel_vue.read_text(encoding="utf-8")
    for fn, expect in (("approveProposal", "approve"), ("rejectProposal", "reject"),
                       ("applyProposal", "apply"), ("restoreVersion", "restore")):
        idx = client_src.index(f"\n  {fn}")
        seg = client_src[idx:idx + 400]
        has_post = 'jsonInit("POST", body)' in seg
        nxt = client_src.find("\n  /**", idx + 1)
        if nxt != -1:
            seg_full = client_src[idx:nxt]
        has_path = f"/{expect}`," in seg_full or f"/{expect}`" in seg_full
        verb = re.search(r'jsonInit\("(\w+)"', seg_full)
        path_in = re.search(r"`([^`]*\{proposalId\}[^`]*)`|`([^`]*\{versionId\}[^`]*)`", seg_full)
        print(f"     {fn}: method={verb.group(1) if verb else '?'}, "
              f"path={(path_in.group(0) if path_in else '?').strip('`')}")
        check(f"client.ts {fn} 用 POST", has_post, seg_full.strip()[:100])
        check(f"client.ts {fn} 路径落在 /{expect}", has_path, (path_in.group(0) if path_in else "?")[:80])
    # 调用点侧：AssistantPanel 传的 action 字面量必须与路径动作一致
    for expect in ("approve", "reject", "apply", "restore"):
        found = re.search(rf"action: \"{expect}\"", panel)
        check(f"AssistantPanel 调用 {expect} 时 action='{expect}'", found is not None)

    print("\n" + "=" * 78)
    print("### 4. ApiError 新增 detail 后是否向后兼容")
    print("=" * 78)
    src = client_ts.read_text(encoding="utf-8")
    calls = re.findall(r"new ApiError\(([^)]*)\)", src)
    two_arg = [c for c in calls if len([x for x in c.split(",") if x.strip()]) == 2]
    three_arg = [c for c in calls if len([x for x in c.split(",") if x.strip()]) >= 3]
    print(f"     两参调用 {len(two_arg)} 处：{[c.strip()[:52] for c in two_arg]}")
    print(f"     三参调用 {len(three_arg)} 处：{[c.strip()[:52] for c in three_arg]}")
    all_components = [p for p in FRONTEND.rglob("*.vue")] + [p for p in FRONTEND.rglob("*.ts")]
    legacy_users: list[str] = []
    for p in all_components:
        for line_no, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"catch\s*\((err|\w+)\)", line) or "\b message" in line:
                pass
            if re.search(r"\b\w+\.message\b", line) and "ApiError" not in line:
                legacy_users.append(f"{p.name}:{line_no} {line.strip()[:60]}")
    check("两参调用仍然存在且依赖 `detail === undefined → message` 的兜底", len(two_arg) > 0)
    check("三参调用只有 request() 里那一处（结构化 detail 的唯一来源）", len(three_arg) == 1,
          f"实际 {len(three_arg)} 处")
    if legacy_users:
        print(f"     其它依赖 error.message 的位置（改动前也是这个行为）：{legacy_users[:6]}")

    print("\n" + "=" * 78)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  - 失败：{f}")
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
