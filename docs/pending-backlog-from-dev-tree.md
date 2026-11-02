
> ⚠️ **本仓库不写本机绝对路径**：上面的 `<REPO_ROOT>` / `<WORKSPACE>` / `<TMP>`
> 都是**占位符**（本机上的真实路径每次可能不同，由助手在当次对话里给出）。

# 待办清单：dev 树（`ai-novel-generator`）独有的后端改动（2026-11-02 记录）

> **为什么有这份清单**：仓库分成了两棵 —— **发布版**（`<REPO_ROOT>`，
> 即 GitHub 上那份）与**工作区 dev 树**（`<WORKSPACE>/ai-novel-generator`）。
> 两棵树**双向分叉**（后端 59/75 文件不同、发布版独有 83 个文件；前端 22/23 不同、发布版独有 8 个）。
> 前端已决定**在发布版上重做**（见 `docs/frontend-handoff.md`）；
> **后端这一批修复按主人决定：先不移植，只记录清单。**

## 清单（dev 树独有、发布版没有的后端修复）

| # | 提交 | 修的是什么 | 发布版有没有等价物 |
|---|---|---|---|
| 1 | `75fea74` fix(kg): 说话归因把被切碎的 3 字真名归并回来（方案 R2 结构归并） | 说话人归因时把被切碎的 3 字真名合并回完整名 | ❌ 未见 |
| 2 | `b383288` fix(kg): 补 `_NAME_CHARS` 真名末字——解 3 字名末字硬拒，45 个真角色不再被三通路全堵 | 真名末字表缺失 ⇒ 3 字名被三条通路同时硬拒 | ❌ 未见（两边都有 `_NAME_CHARS` 但内容不同） |
| 3 | `527a26a` fix(kg): 清掉 S1 连带放行的 7 个伪角色（整词进 `_NAME_WORD_STOP`） | 停用词表漏了整词 ⇒ 7 个伪角色被当真人 | ❌ 未见 |
| 4 | `7d6229d` fix(kg): 说话归因加时间词停用护栏——修「许久」被 62609a5 误标为说话人 | 「许久」这类时间词被误判成说话人 | ❌ 未见 |
| 5 | `cdd932f` fix(assistant): 修段落问答必答错、角色被误报未出场、写意图漏词三项缺陷 | 助理的段落问答：必答错、角色误报未出场、写意图漏词 | ❌ 未见（涉及 `agents/audit.py`、`agents/pending_actions.py`、`routers/assistant.py`） |
| 6 | `a859140` chore(paths): 仓库里不再带本机绝对路径 —— 改相对路径 / 环境变量 / `NOVEL_DIR` 查找 | 去掉仓库里的本机绝对路径 | ⚠️ **部分已做**：发布版有 7 个含「路径」的提交，但需逐条核对是否覆盖 dev 树的这一版 |
| 7 | `d49ac9f` merge(content): 并入本地 master 线的文风修复（22 处冲突已解，内容级整合） | 一次跨 22 处冲突的内容级整合 | ⚠️ **不明**（发布版是更晚的整合，可能已包含等价内容） |

**另有两个 dev 树独有的后端源文件**（发布版没有）：
`backend/app/agents/ai_flavor_memory.py`、`backend/app/generation/formulaic.py`
⇒ 需要时单独评估要不要拿。

## 移植时的注意事项（真要动的时候再看）

1. **发布版的后端是更新的一层**（多了整个助理 agent 子系统、body_repair / closed_inventory /
   authority_body_repair、盲评 v2 支撑），⇒ dev 树的旧实现**不能整文件覆盖**发布版，
   必须逐文件看差异后取「发布版的结构 + dev 树的修复」。
2. **KG 那 4 条修复（#1~#4）耦合在一起**（`_NAME_CHARS` / `_NAME_WORD_STOP` / 说话归并 / 时间词护栏），
   拆开移植可能引入回归 ⇒ 建议**作为一个整体**移植，并跑
   `backend/tests/test_name_*.py`、`test_voice_char_name_filter.py` 等相关用例。
3. 移植后必须跑**全量** `python -m pytest tests/`（发布版根目录），不是定向脚本。

## 复现命令（要拿 dev 树的原始改动时）

```powershell
$W = "<WORKSPACE>/ai-novel-generator"
git -C $W show 75fea74 --stat
git -C $W format-patch -1 75fea74 --stdout > <TMP>/kg_fix_speech_attrib.patch
```
