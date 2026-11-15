# 两分支冻结快照清单（2026-11-15）

> 说明：下文出现的 `D:\Temp` 路径是当时本机临时脚本 / 副本 / 日志的位置，**这些脚本未入库**，仅作取证记录保留，不可直接复跑。

> 本文件仅本地留存，不提交、不推送。

## 1. 冻结标签

| 标签名 | 类型 | 标签对象 SHA | 目标提交 SHA | 对应分支 |
| --- | --- | --- | --- | --- |
| `freeze/feat-20261115` | annotated | `41ee9331f396b9502b13be4187c7279c46e6e221` | `989abcefb9f1206460134c90fe714b838c93cd21` | `feat/resume-push-2026-10` |
| `freeze/main-cb05a45` | annotated | `79791ff4da0e548ae8046561ea7d051fefa1bb57` | `cb05a452644d6092f35b16592124c747d6fba375` | `origin/main` |

- 冻结时间：2026-11-15 20:42 (+08:00)
- 标签说明中已写入：冻结时间、目标 SHA、用途「供合流比对使用」
- 打标签前已用 `git rev-parse` 实测两个 tip，未凭记忆
- 标签仅存在本地，未 push

## 2. 分支规模

| 分支 | 提交总数 | 相对另一分支的独有提交 |
| --- | --- | --- |
| `origin/main` | 134 | 134 |
| `feat/resume-push-2026-10` | 264 | 264 |

- `git rev-list --left-right --count origin/main...feat/resume-push-2026-10` → `134  264`
- `git merge-base --all origin/main feat/resume-push-2026-10` 无输出（退出码 1）：**两分支没有共同祖先**，属于无关历史。后续任何「合流比对」都只能做内容级 diff，不能做祖先级 diff。

## 3. 冻结时刻工作区状态

| 类别 | 数量 |
| --- | --- |
| 已暂存（staged） | 0 |
| 未暂存（unstaged，含已跟踪文件修改/删除） | 0 |
| 未跟踪（untracked） | 1 |

未跟踪文件清单：

| 路径 | 大小 |
| --- | --- |
| `reports/2026-11-15-merge-conflict-analysis.md` | 34,944 字节 |

`git stash list` 为空，无既有 stash 条目。

## 4. `git stash create` 结果

- 执行命令：`git stash create`（未使用 `git stash push`，未改动工作区与暂存区）
- 返回：**空输出**，退出码 0 —— 因为没有已暂存/未暂存改动可打包，git 未生成任何 dangling 提交，故无 SHA 可记录
- 该操作对工作区、暂存区、工作树文件零改动；执行后 `git status --short` 与执行前完全一致

## 5. 状态与任务描述不符的如实记录

任务描述的前提是「工作区有大量已暂存与未暂存改动、以及未跟踪文件」，实测不符：

- 主工作区 `git status`：`nothing added to commit but untracked files present`，已跟踪文件无任何修改，暂存区为空
- 仅 1 个未跟踪文件（见上表）
- 推测描述所指的改动可能在打标签前已被其他会话提交或清理（例如 tip `989abce chore: 把待出版书稿与第三方原文对照挡在仓库外`）；无法回溯确认，此处不做推断性结论

因该差异不涉及写操作风险，本次仅完成「打标签」这一纯增量动作，并按实际状态记录；**未对工作区做任何清理、暂存、提交或恢复操作**。

## 6. 其他 worktree 观察（未触碰）

`git worktree list` 显示 11 个 linked worktree（部分为其他会话的只读分析用临时 worktree）。仅做状态观察，未进入、未切换、未清理：

| worktree | 分支/HEAD | 脏项 |
| --- | --- | --- |
| `worktrees/narrative-foundation` | `feat/narrative-foundation` | 未跟踪 1 项 |
| `worktrees/registry-r5` | `fix/registry-r5-whitelist` | 未跟踪 1 项（`nul`） |
| `worktrees/style-distill` | `feat/style-distillation` | 未跟踪 3 项 |
| `worktrees/timeline-pacing` | `feat/timeline-pacing` | 修改 1 项 + 未跟踪 2 项 |
| 其余 7 个（含 `D:/Temp/mwt-main-20261115`、`D:/Temp/wt-detail-backend`，均 detached 于 `cb05a45`） | — | 干净 |

## 7. 如何用这个快照复现某次比对

1. 取两分支内容差异：`git diff --stat freeze/main-cb05a45 freeze/feat-20261115`（无共同祖先，属内容级 diff）。
2. 取单分支某一时点：`git rev-parse freeze/feat-20261115^{commit}` 得到固定 SHA，后续所有比对都以该 SHA 为基准，不再引用分支名（分支会继续前进）。
3. 需要独立目录时：`git worktree add --detach ../cmp-<name> freeze/feat-20261115`，用完自行 `git worktree remove`，不影响主工作区与其他会话的 worktree。
