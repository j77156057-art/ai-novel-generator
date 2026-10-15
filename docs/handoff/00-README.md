# AI 网文生成器 · 5 件事修复派单交付包（2026-10-13 v3）

> **接收人**：接 B/C/D/F/G 任务的协作者（接单人已确认由其他 AI 接管）
> **总指挥**：WorkBuddy（用户侧的智能设计助手）— 改 `.env` 跑验收、做收口与合并
> **当前基线 HEAD**：`e5e17a3`（E 已完工；B/C/D/F/G 待办）
> **A 任务**：WorkBuddy 亲自做（真实 LLM key 只能本机 `.env` 落，不入 commit）

---

## 1. 怎么读这个包

**6 个文件，按需取用**：

| 文件 | 何时读 | 给谁 |
|---|---|---|
| **`00-README.md`** ← 你正在看 | 第一份 | 所有人 |
| `2026-10-13-AI-NOVEL-DISPATCH-PLAN.md` | 想看完整派单概览 | 所有人（**v2 派单 + ownership matrix + 收口顺序**） |
| `2026-10-13-TASK-B-WIZARD-CHARACTERS.md` | 接 B | 接手 B 任务者（wizard 主角/关键配角） |
| `2026-10-13-TASK-C-TARGET-TOTAL-PACING.md` | 接 C | 接手 C 任务者（总字数+节拍） |
| `2026-10-13-TASK-D-UPCOMING-CONFIRM.md` | 接 D | 接手 D 任务者（即将出场确认表） |
| `2026-10-13-TASK-F-KG-ENRICH.md` | 接 F | 接手 F 任务者（KG 字段升级） |
| `2026-10-13-TASK-G-PREMISE-LIFT.md` | 接 G | 接手 G 任务者（premise 上提） |

**E 已完工**（spec 留作历史参考）：`2026-10-13-TASK-E-EXTRACTOR-FIX.md`

---

## 2. 现状

- **基线 HEAD = `e5e17a3`**，含 E 全部修复（fixture 8/8, verify_sentinel 97/0, G1b 转绿, P0 54/55, ledger 40/40）
- **5 个 task 待做**（B/C/D/F/G），每个都有独立 worktree 路径
- **A 待做**：WorkBuddy 改 `.env`（不入 commit）
- **派单 v2 顶部的"基线 55/55"是误记**——实际基线 53/55，2 个 latent P0 fail 不是 E 修复引入
- **A 任务进度**：user 说之前给过真 key 但 `.env` 的 `ZHIPU_API_KEY` 仍空——`A 任务等 user 重发 key`

---

## 3. 派工安排（接单人确认）

| Task | 状态 | Worktree | 负责人 |
|---|---|---|---|
| A | 待做 | 无（直接改 `.env`） | **WorkBuddy 亲做**（真 key 不外传） |
| E | **已完成** | 已经在 `e5e17a3` 包含 | 已由 user (j77156057-art) + WorkBuddy 协同 |
| B | 待做 | `feat-wizard-characters` | **接单人 = 接 B/C/D/F/G 的人**（已确认外派） |
| C | 待做 | `feat-target-total-pacing` | 同 B 接单人 |
| D | 待做 | `feat-upcoming-characters` | 同 B 接单人 |
| F | 待做 | `feat-kg-schema-enrich` | 同 B 接单人 |
| G | 待做 | `feat-prompt-premise-lift` | 同 B 接单人 |

**5 个 task 都从 `e5e17a3` 拉 worktree**——不依赖其他 task 的 commit。ownership matrix 在派单 v2 顶部已写明，**严格不跨 task 改文件**。

---

## 4. 收口顺序

```
A (WorkBuddy 改 .env，1 分钟)
   ↓
B / C / G — 并行（不同文件，参考 ownership matrix）
   ↓
D (独占 prompts.py / engine.py / generation.py，与 C 串行合 prompts.py)
   ↓
F (独占 queries.py / build.py，与 D 无文件重叠)
   ↓
所有 task 完工后 WorkBuddy 跑总闸门 → git merge --no-ff 进 master
```

---

## 5. 总闸门期望

| 套件 | 当前 (e5e17a3) | 收口目标 |
|---|---|---|
| `verify_acceptance.py` | 14/0 | 16/0（C3+C4+C5 PASS） |
| `verify_sentinel.py` | 97/0 | ≥ 97/0 |
| `test_p0_regression.py` | 53/55* | 55/55（F 任务透出新字段**不**应改 P0） |
| `test_ledger.py` | 40/40 | 40/40 |
| `test_extractor_v2.py` (新) | 8/8 | 8/8 |
| `frontend typecheck` | 0 error | 0 error |

*「53/55」是**真实基线**——基线就 fail 2 条 latent（朱木兰 fixture 设计层问题；王守道已被 E 修复路径修好，**现在已 54/55**）。派单顶部若写 55/55 是误记。

---

## 6. 接收人 / 协作者开工步骤（统一）

```bash
# 1) 看自己那个 task 的 spec（你只读 1 个文件）
# 例：B 接单人只看 2026-10-13-TASK-B-WIZARD-CHARACTERS.md

# 2) 从基线拉 worktree
cd /c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator
git worktree add worktrees/<spec 里写的 worktree 名> e5e17a3
cd worktrees/<worktree 名>

# 3) 按 spec §2 改文件
# 4) 跑 spec §3 的验收
# 5) 按 spec §4 commit

cat > /tmp/msg.txt <<'EOF'
<copy from spec §4 commit message>
EOF
git -c commit.gpgsign=false add <changed files>
git -c commit.gpgsign=false commit -F /tmp/msg.txt

# 6) 通知 WorkBuddy 收口（不需要 push——本仓无 remote）
```

---

## 7. 注意事项

- **本仓库无 GitHub remote**——所有 worktree 收口走 `git merge --no-ff`（WorkBuddy 做）
- **schema 变更只能改 `models.py` + `db.py` 的 `migrations` 列表两处**
- **真 API key 永远不入 commit**（`.env` 始终 gitignored）
- **不要跨 task 改文件**——违反 ownership matrix 会让其他 task 合并冲突
- **commit 用 `-F <msg.txt> -- <paths>`** 避开沙箱 git shim 的 `-m` 吞消息 bug
- **不要碰 `extractor.py`**（E 已完，独占；若发现 E 任务漏的 bug，开新 worktree `fix-extractor-...` 不在本派单）

---

## 8. 给 WorkBuddy（总指挥）的收口提示

收到任何 task 完工的 worktree 之后：
1. **复跑该 task 自己的验收命令**（spec §3）
2. **跑总闸门 4 套**（verify_acceptance + verify_sentinel + test_p0 + test_ledger + test_extractor_v2 + frontend typecheck）
3. **`git merge --no-ff <worktree branch>`** 进 master（按 ownership matrix 串行：先 B/C/G → 再 D → 再 F）
4. **通知 user 回归**

---

**End of dispatch README**
