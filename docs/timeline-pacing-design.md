# 时间线规划器 + 剧情节点数据结构（细化设计）

> 分支：`feat/timeline-pacing`（worktree `worktrees/timeline-pacing`，基于 master 5bb5701）
> 目标：把「一次投喂整条故事线 → 第一章就完结」改成「按剧情点分段投喂，进度条驱动」
> 本文只覆盖 A（时间线规划器）+ C（节点数据结构）；B（段间状态传递）在 A 内预留接口，D（支线调度）下一轮。

## 1. 问题根因（已确认）

`generate_next_segment` 每次把 `project.outline`（整条故事线）+ `plot_bible`（含完整 synopsis/结局走向）
一次性塞进 `build_user_prompt`。模型拿到全貌 + 没有章节边界约束，必然把整条线压进第一段。

现有可复用资产（都已存在，不要重造）：
- **段续写环**：`generate_loop` → 每段一次 `generate_next_segment`
- **摘要树**：L1 段 / L2 卷 / L3 主线骨架（`summary_tree.py`）
- **生成闭环**：每段后 `_merge_segment_into_graph` 增量更新 generated 图谱
- **伏笔跟踪**：`get_reminders` 的 `unresolved_plotholes`（未回收钩子）
- **风格档案**：`style_narrative`（节拍序列/高频场景模板）+ `style_profile`（数字）
- **章节级约束**：prompts.py 已有「禁止金句化/段尾升华/情绪直陈/允许留白」

## 2. 核心概念

### 剧情节点（PlotNode）= 一个剧情点

一段故事从进入某个剧情点、推进到该点高潮、完成该点，再进入下一点。
节点是**树状**：主干节点串成主线，支线节点挂在主干节点上（讲日常/配角）。

### 两层进度

- **主线进度**：已完成主干节点 / 总主干节点 → 0~100%（分母固定，一次性规划）
- **支线进度**：已消耗支线额度段数 / 支线预留额度段数 → 0~100%
  （分母 = §4.2 预留的支线段数，固定；不按节点数算，避免滚动追加导致分母变化/除零）
- **节拍进度（高潮内部）**：当进入某节点的高潮区，高潮跨多段，每段在节拍上前进一步
  （节拍序列来自 `style_narrative` 的蒸馏）

> 进度条只用「段数」口径，不用「节点数」口径——节点级 done 只作内部状态。
> 原因：支线滚动追加会让节点分母动态变化，进度条会倒退/除零。

### 章节预算反推（不是先规划剧情再发现装不下）

先有章节预算：`总字数 / 每段字数 = 段数`。时间线从段数反推能装几个节点、
每个节点分几段。纯支线段占段不推进主线进度。

## 3. 剧情节点怎么规划：混合（主干一次性 + 支线滚动）

- **主干节点**：一次性全规划。outline/plot_bible 本就有全貌，一次 LLM 调用产出所有主干节点，
  进度条能预知总量，且省调用。
- **支线节点**：滚动追加。主干推进过程中，当 KG 出现新的配角/未回收钩子，或主线推进到
  一定节奏，LLM 动态生成一条支线节点挂到当前主干节点下。不预先规划支线——支线本就该
  随人物关系生长。

## 4. 节奏参数从语料蒸馏（branch_ratio 首轮；高潮密度/长度 v2）

新增 `app/style/rhythm.py`（蒸馏节奏参数，挂 style_narrative 同级）。

**首轮只蒸馏 branch_ratio**（可从「非高潮章节占比」较可靠地估计）：
- **支线占比 branch_ratio**：语料里非主线推进的章节（日常/支线/过渡）占多少 →
  决定每几段插一条支线

**高潮密度/高潮长度 v2 再做**：这两个需要章节级情绪标注（爽/虐/怒高点即高潮候选），
首轮语料没有该标注，且节拍序列是抽象规则（无章节号）数不出间隔。
首轮用默认值：密度每 4-6 段一主干节点，高潮 segment_budget 2-3 段。
承认首轮只蒸一个参数，不假装一次全出。

回退：短篇/未蒸馏时 branch_ratio 用默认 0.2。

## 5. 进度条：双进度（主线 + 支线并行）

- **主线进度**：done 主干节点 / 总主干节点（分母固定）
- **支线进度**：已消耗支线额度段数 / 支线预留额度段数（分母固定 = §4.2 的支线预留段数）
- 纯支线段：不涨主线进度，涨支线进度；UI 显示两条，支线作为主线的「并行轨道」

两条进度分母都固定（一次性预算决定），单调递增，不会因滚动追加而倒退。

TimelineState 增加字段：

```python
    branch_budget: int          # 支线预留额度段数（固定，= round(total_segments*branch_ratio)）
    branch_used: int            # 已消耗的支线额度段数（推进时累加）
```

## 6. 节点数据结构（C）

新增 `app/generation/timeline.py`：

```python
@dataclass
class PlotNode:
    id: str                      # "n3" 或 "s3-支线表哥"
    kind: str                    # "main" | "branch"
    title: str                   # "逼宫对决"
    emotion: str                 # 爽|虐|甜|怒|悬疑  （接情绪曲线，决定句长/对白密度）
    # —— 准入/准出（模型据此判断本段算不算写完这个点）——
    entry_condition: str         # "主角已知身份被怀疑，尚未对峙"
    exit_condition: str          # "主角当众揭穿对手，对手退场"
    # —— 节拍（高潮内部展开，来自 style_narrative 节拍序列）——
    beats: list[str]             # ["铺垫","升温","爆发","余波"]  —— 高潮跨多段用
    # —— 伏笔登记（接 KG plotholes）——
    opens: list[str]             # 本节点开启的钩子（写进 unresolved）
    closes: list[str]            # 本节点回收的钩子（从未 unresolved 移除）
    # —— 进度预算（由规划器填，不是手填）——
    segment_budget: int          # 本节点分到几段（含高潮展开）
    parent: str | None           # 支线节点挂到哪个主干节点
    children: list[str]          # 支线节点 id
```

进度状态：

```python
@dataclass
class TimelineState:
    nodes: dict[str, PlotNode]   # id -> 节点，含支线
    order: list[str]             # 主线节点推进顺序（不含支线）
    done: set[str]               # 已完成的节点 id
    total_main: int              # 主干节点数（一次性规划，固定）
    # 当前段上下文
    current_node: str | None     # 本段正在写的节点
    current_beat: int            # 高潮内节拍位置
```

主线进度 = `len(done ∩ main) / total_main`。支线节点不计入主线分母，走独立的支线进度。

## 7. 时间线规划器（A）

新增函数（同文件 `timeline.py`）：

```python
def plan_timeline(project, style_narrative, total_segments) -> TimelineState
```

### 7.1 主干节点数（首轮用默认节奏，v2 接蒸馏）

首轮**高潮密度用默认**（每 4-6 段一个主干节点），因为语料无章节级情绪标注，
数不出高潮间隔（见 §4）。`plan_timeline` 接收 `rhythm` 参数（来自 rhythm.py 的
branch_ratio；高潮密度首轮传默认值），v2 补齐高潮密度后此处自动接上、无需改结构。

```python
avg_node_segments = 5   # 首轮默认；v2 从高潮密度蒸馏
```

### 7.2 章节预算反推

```
total_segments = 总字数 / 每段字数
branch_budget = round(total_segments * branch_ratio)   # 支线预留的「段数额度」，固定
主线可用段 = total_segments - branch_budget
主干节点数 = clamp(主线可用段 / 平均节点段数, 3, 12)
每个主干节点 segment_budget = 主线可用段 / 主干节点数
```

> 预留的是**段数额度**（给支线的篇幅），不是支线节点。支线节点滚动生成时从这个
> 额度里扣段数（branch_used 累加），扣满即不再追加支线。这样 §3「支线不预先规划」
> 与「预留」自洽——预留的是篇幅额度，不是具体节点。

### 4.3 节点准入/准出从哪来

**从 outline + plot_bible 提取**（LLM 规划一次，结果落库，之后只读）：
- 调一次 LLM，输入 outline + plot_bible + 语料高潮密度，输出 PlotNode 列表
- 准入/准出条件由 LLM 按 outline 的情节点填
- 落库到 `project`（新列 `plot_timeline` JSON），幂等：已规划且 source_ids 一致则复用

### 7.4 进度条与每段推进（首轮：段数预算判定）

`advance(state, segment_no)`：每段生成后调用，**纯规则、不判自然语言**：

- 当前节点累计写到 `segment_budget` 段 → 标记 done，进入下一主干节点
- 若本段是支线段 → `branch_used += 1`，不涨主线进度
- 高潮区：节点内按 `current_beat` 顺序推进，段数够则 `current_beat += 1`（不越界 beats 长度）

**exit_condition 不参与程序判定**——它是自然语言，规则判不了。它的作用是**注入提示词**
（告诉模型「这段要写到什么程度算完成这个点」），v2 再上 LLM 判定 exit。
首轮用段数预算兜底，保证可测、不依赖 LLM。

进度返回 `{main_progress, branch_progress, current_node, current_beat}`。

> 这样首轮 advance 是纯函数（输入 state + segment_no，输出新 state），测试无需真实文本。

## 8. 注入改造（最小改动）

改 `generate_next_segment`：
- `build_user_prompt` 不再传整条 `project.outline`
- 改传**当前剧情点的提示块**：节点标题 + 准入/准出 + 当前节拍 + 本段情绪标签 +
  本节点开/收的钩子 + 主线进度百分比
- 新增 `build_node_prompt(node, beat, progress) -> str`（prompts.py）

`build_system_prompt` 层不动（角色/伏笔 reminders 仍挂系统层，防漂移）。

## 9. B（段间状态传递）预留接口

每段结束 `_merge_segment_into_graph` 已更新图谱——下段的 reminders 天然读到新状态。
时间线只需在 `advance` 里同步：done 节点的 closes 钩子确认移除、opens 写入 unresolved。
**不额外造状态通道，复用 KG。** 接口：`advance` 返回的进度 + KG reminders 一起注入下段。

## 10. 测试计划（先 RED）

新增 `tests/test_timeline.py`（advance 是纯函数，测试无需真实文本）：
1. `plan_timeline` 从段数反推节点数，clamp 在 [3,12]
2. 支线额度正确扣除（branch_budget = round(total*ratio)），不计入主线分母
3. `advance` 节点写到 segment_budget 段 → done，主线进度上升
4. 支线段 → branch_used 累加，主线进度不变
5. 高潮区 current_beat 按段数推进，不越界 beats 长度
6. branch_ratio 蒸馏：有语料统计时用蒸馏值，无则回退默认 0.2
7. `build_node_prompt` 只含当前节点信息，不含整条 outline

## 11. 落地顺序

1. `timeline.py`：PlotNode / TimelineState / plan_timeline / advance（纯函数，先测）
2. `build_node_prompt` + 改 `generate_next_segment` 注入点
3. DB 迁移：`project.plot_timeline` 列（JSON）
4. 端到端：红楼梦语料，验证第一章不再完结、进度条推进

## 12. 已定决策

- **规划方式**：混合——主干一次性全规划，支线滚动追加（§3）
- **branch_ratio**：从语料蒸馏，不写死（§4）
- **进度显示**：双进度条，主线 + 支线并行（§5）

## 13. 仍待定（v2 再议）

- **exit_condition 的 LLM 判定**：首轮用段数预算兜底（§7.4），v2 上 LLM 判定自然语言 exit
- **高潮密度/高潮长度的语料蒸馏**：首轮用默认（§4），v2 补章节级情绪标注后蒸馏
- **支线滚动触发时机**：固定节奏 + 新配角触发双条件，实现时定
