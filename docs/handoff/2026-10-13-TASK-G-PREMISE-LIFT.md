# 任务 G · system 提示上提 premise（基调锚点最小改）

> 状态：可独立 worktree 开工。**基线 HEAD = `e5e17a3`**
> 工作目录：`/c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/`
> 验收目标：`verify_acceptance.py` C 段新增 C5 PASS → 16/0 + premise 进 system 静态层
> 派单对应：v2 文档 §任务 G

---

## 1. 背景与根因

`Project.premise`（`models.py:48`，`Text`，可空）是用户填的"小说大概内容"——是**基调锚点**。当前在 `build_user_prompt`（`prompts.py` line 216 附近）作为**动态层**出现，但只与 `plot_bible` 一起拼"剧情圣经·动态指引"。

**问题**：
- 续写时 `plot_bible` 的 `synopsis`/`direction` 只在 user 段（动态），前情膨胀会把它推远
- premise 本身是**用户基调锚点**，应该进 system 静态层（`build_system_prompt`）才不会被前情冲掉

### 1.1 改动约束

- premise 上提 system 静态层（"基调锚点"块）
- premise > 500 字截断到 500 字（防 system 层膨胀）
- premise=None 显示"未给定"（不报错）

### 1.2 不要做

- ❌ 不动 `extractor.py`（E 独占）
- ❌ 不动 `engine.py::build_context`（C 独占）—— G 只改 `build_system_prompt` 入参和 `generate_next_segment` 传参
- ❌ 不动 `queries.py`（D/F 独占）
- ❌ 不动 `build.py`（F 独占）
- ❌ 不动 `wizard.py`（B 独占）
- ❌ 不动 `build_user_prompt`（C 独占）
- ❌ 不动 `merged_constraint_items`（D 独占）
- ❌ 不动 `upcoming_constraints_block`（D 独占）

---

## 2. 改动清单

### 改动 1 · `backend/app/generation/prompts.py::build_system_prompt` 入参 + 块

line 113 附近 `def build_system_prompt(...)` 签名追加 `premise` 参数：

```python
def build_system_prompt(
    project_title: str,
    reminders: dict,
    style_dims: dict | None = None,
    plot_bible: dict | None = None,
    style_profile: dict | None = None,
    *,                            # ← keyword-only
    premise: str | None = None,   # ← E-任务 G
) -> str:
```

在 `merged_constraint_items` 之后、 `_style_block` 之前**插入**：

```python
# E-任务 G：基调锚点（premise 上提 system 静态层）
premise_block = (
    f"【基调锚点（用户给定，整个创作期保持）】\n"
    f"{(premise or '（未给定，凭类型/风格自主开局）')[:500]}"
)
```

并把 `premise_block` 拼到 `return` 字符串的**最前**（在【角色人设约束】之前）：

```python
return (
    f"你是一位长篇小说续写助手。当前创作项目：《{project_title}》。\n\n"
    f"{premise_block}\n\n"
    "【角色人设约束】..."
    # ... 其余不变
)
```

**关键**：
- `premise[:500]` 截断保护
- `premise or '（未给定...）'` 处理 None
- 块名"基调锚点"和派单 v2 §任务 G 一致

### 改动 2 · `backend/app/generation/engine.py` 透传 premise

`engine.py::generate_next_segment`（line 386 附近）`build_system_prompt` 调用改为：

```python
system = build_system_prompt(
    project.title, ctx["reminders"], _style_info(project), project.plot_bible,
    _style_profile(db, project),
    premise=project.premise,  # ← E-任务 G
)
```

`generate_next_segment_stream`（line 415 附近）同样改。

`build_context`（line 239）**不**改——premise 已经在 `project` 上能读到，build_context 透传的是 reminders/prior/constraints，无需 premise。

### 改动 3 · `backend/app/routers/generation.py::generate_plan` 同步

line 192 附近 `build_system_prompt` 调用改为：

```python
system = build_system_prompt(
    proj.title, ctx["reminders"], _style_info(proj), proj.plot_bible, profile,
    premise=proj.premise,  # ← E-任务 G
)
```

但**这是 D 任务范围**（D 改 generate_plan 简化为 build_plan）——若 D 已合并，G 任务在 D 改后的 `engine.py::build_plan` 内调 `build_system_prompt` 处加 premise 即可。

**保险做法**：G 任务**只改 `build_system_prompt` 函数体 + `engine.py::generate_next_segment` / `stream`**；`generate_plan` 的 `build_system_prompt` 调用由 D 任务**自己**改（**D 任务的 spec §改动 1 已包含把 `_style_profile_digest` 搬到 engine，可顺便加 premise 参数**）。

**为最小破坏**：G 任务**不动** `generate_plan` 里的 `build_system_prompt` 调用（用位置参数/默认参数兼容）。看 `generate_plan` 的 `build_system_prompt` 调用（line 199-201）：

```python
system = build_system_prompt(
    proj.title, ctx["reminders"], _style_info(proj), proj.plot_bible, profile
)
```

5 个位置参数 + style_profile=profile 之后是 5 个实参。我把 `premise` 设为 **keyword-only 默认 None**——上面这个调用**不传 premise** 也合法（默认 None 走"未给定"路径）。**G 任务完工时 `generate_plan` 走未给定路径**——D 任务后续会顺手加 `premise=proj.premise` 补全。

**这是 G 任务的妥协**——让 G 任务**完全不碰** `generate_plan`（属于 D 范围），维持 ownership matrix。

### 改动 4 · `verify_acceptance.py` 新增 C5 check

在 C 段尾部追加：

```python
# E-任务 G：premise 进 system 静态层
s = build_system_prompt(
    "测", {"character_constraints": [], "unresolved_plotholes": []},
    None, None, None,
    premise="用户基调锚点测试",
)
check("C5 premise 进 system 静态层（基调锚点块）",
      "基调锚点" in s and "用户基调锚点测试" in s, f"截 300: {s[:300]}")

# premise=None 路径不报错
s2 = build_system_prompt("测", {"character_constraints": [], "unresolved_plotholes": []})
check("C5b premise=None 显示「未给定」不报错",
      "未给定" in s2, f"截 300: {s2[:300]}")

# premise > 500 字截断
long_p = "测" * 1000
s3 = build_system_prompt("测", {"character_constraints": [], "unresolved_plotholes": []},
                         None, None, None, premise=long_p)
check("C5c premise > 500 字截断",
      s3.count("测") <= 600,  # 系统提示本身也含「测」字，留点 buffer
      f"截断后含 '测' 数 = {s3.count('测')}")
```

### 改动 5 · 前端（极小）

`frontend/src/api/client.ts` 已有 `generate` 方法——**不动**。`/api/generate-plan` 返回的 `system_prompt` 会自动含【基调锚点】块，前端 plan 预览页（如有）能直接看到。

---

## 3. 验收

### 3.1 跑全闸门

```bash
cd backend
python verify_acceptance.py 2>&1 | tail -3
# 期望：PASS 16  FAIL 0  (C5/C5b/C5c PASS)

python test_p0_regression.py 2>&1 | grep "结果"
# 期望：55/55

python test_ledger.py 2>&1 | tail -2
# 期望：40/40

python test_extractor_v2.py 2>&1 | tail -2
# 期望：8/8
cd ../frontend && npm run typecheck
# 期望：0 error
```

### 3.2 直接验

```bash
cd backend
python - <<'PY'
from app.generation.prompts import build_system_prompt
s = build_system_prompt(
    "测", {"character_constraints": [], "unresolved_plotholes": []},
    None, None, None,
    premise="用户基调锚点测试",
)
assert "基调锚点" in s
assert "用户基调锚点测试" in s
print("OK premise 已上提 system 静态层")
print(s[:300])
PY
```

---

## 4. Worktree 流程

```bash
cd /c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator
git worktree add worktrees/feat-prompt-premise-lift e5e17a3
cd worktrees/feat-prompt-premise-lift

# 改 backend/app/generation/prompts.py（build_system_prompt 入参 + premise_block）
# 改 backend/app/generation/engine.py（generate_next_segment + stream 透传 premise）
# 改 backend/verify_acceptance.py（C5 三条 check）
# 跑 §3.1

cat > /tmp/msg_g.txt <<'EOF'
feat(prompt): premise 上提 system 静态层（基调锚点）

G 任务：从基线 e5e17a3 拉 worktree feat-prompt-premise-lift。

1) build_system_prompt 入参新增 keyword-only premise: str|None
2) premise 渲染为【基调锚点】块，premise[:500] 截断，None 走"未给定"路径
3) premise_block 拼到 return 字符串最前（角色人设约束之前）
4) engine.py::generate_next_segment + stream 透传 project.premise
5) verify_acceptance C 段新增 C5/C5b/C5c 三条 check

不动 extractor.py / queries.py / build.py / build_user_prompt /
build_context / merged_constraint_items / upcoming_constraints_block /
wizard.py / generate_plan 内的 build_system_prompt 调用（D 范围）。
EOF
git -c commit.gpgsign=false add backend/app/generation/prompts.py \
    backend/app/generation/engine.py backend/verify_acceptance.py
git -c commit.gpgsign=false commit -F /tmp/msg_g.txt
```

---

## 5. 失败回退

- C5 fail → 看 `build_system_prompt` 实际输出前 300 字
- C5c fail → premise[:500] 截断逻辑错
- 任何总闸门退化 → `git reset --hard HEAD`

---

## 6. 不要做

- ❌ push
- ❌ 改 `extractor.py`（E 独占）
- ❌ 改 `queries.py`（D/F 独占）
- ❌ 改 `build.py`（F 独占）
- ❌ 改 `build_user_prompt`（C 独占）
- ❌ 改 `wizard.py`（B 独占）
- ❌ 改 `build_context`（C 独占）
- ❌ 改 `merged_constraint_items`（D 独占）
- ❌ 改 `upcoming_constraints_block`（D 独占）
- ❌ 改 `generate_plan` 里的 `build_system_prompt` 调用（D 任务范围，G 不碰）

---

**End of task G spec**
