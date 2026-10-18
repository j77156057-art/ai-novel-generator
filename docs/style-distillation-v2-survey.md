# 文风蒸馏 v2 背景调研（2026-10-16）

> 性质：**问题陈述 + 工业参考**——**不是实施计划**。要看 v2.1 增量设计（只补
> master 没做的部分）请见 v2.1-design.md（下一份）。
> 写本调研的原因：v2 设想最早是「3 件事全做」，但 audit master 之后发现 90% 已落地。
> 不审计就开新工作 = 重复造轮（最违背「精巧优先」偏好）。

## 一、问题陈述

本项目 v7 蒸馏（`feat/style-distillation` 分支领先 master 25 个 commit 的工作记录里
描述）在「叙事档案 14 维度」+「角色声音词典」+「语料自证归一」已达 v7 成熟态。
但**「文笔量化 + 风格漂移告警」**这一层用户最关心（用户原文：
「精巧优先于大而全」「验收不靠人肉」），**是否真的接到了生成主链 + 落库 + 漂移告警**？

参考**4 个工业 / 学术方法学**（按与本项目相关度排序）：

### 1.1 Lorn.NovelWriteSkills（GitHub 235⭐, 2026-09）

中文长篇网文专属仓库，**核心能力就叫「蒸馏与模板」**。三档划分：

- **作者文风 DNA**：句长基线、段落节奏、感官偏好、比喻指纹、禁忌清单
- **作品蓝本 DNA**：章首模式指纹、钩子轮换规律、回报间隔、节奏分布
- **写作研究 DNA**：题材×平台默认约束基线

配套「**十层画像进化闭环**」+「**四指标像不像审计**」+「**写审改采集消费全链路**」
（参考 `feat/style-distillation` worktree 的 4 个 npm 脚本）。

### 1.2 StoryScope（COLM 2026, UMD+DeepMind）

`arXiv:2604.03136v6`。10 维叙事结构抽取（agent/social/event/plot/structure/setting/
time/revelation/perspective/style），单 prompt + GPT-5.1，**分维度抽 95.4% 覆盖 vs
单 call 68.4%**。最大贡献：**人类 vs AI 的本质差异在「叙事选择」不在「词汇风格」**——
character agency / chronological discontinuity 是更稳的判别特征（93.2% macro-F1）。

### 1.3 Ex3（arXiv 2408.08506, 中科院+寒武纪）

Extract → Excelsior (SFT) → Expand (树状)。**抽结构 + fine-tune 8B + 树状展开生成
无限长**。本项目不训练 → 整体不适用，但「**先抽结构再生成**」思路可借鉴。

### 1.4 Kovatchev 2026（ACL 2026）

style-similarity judge + GRPO 训练 8B。风格分 0.893。本项目不训练 → 不适用。

## 二、Master 现状盘点

master 的 `worktrees/master` 已实装 6 个 style 模块 + 5 个相关测试套件（grep 验证）。
**对比 Lorn 三档**：

| Lorn 档 | master 实现 | 状态 | 位置 |
|---|---|---|---|
| **作者文风 DNA** | ✅ `stylometry.py` 20 个特征 + z-scores + mean_abs_z | 已落地 | `backend/app/style/stylometry.py` (556 行) |
| **作者文风 DNA** | ✅ engine 主链接入 `style_distance(text, profile)` | 已落地 | `engine.py:569-570` |
| **作品蓝本 DNA** | ✅ 叙事档案 14 维度（含节拍序列 / 章首钩子 / 场景模板） | 已落地 | `backend/app/style/narrative.py` (2075 行) |
| **像不像审计** | ✅ `agents/audit.py` 复用 stylometry z-scores 给 KG 审计 | 已落地 | `agents/audit.py:949-958` |
| **进化闭环 + 多平台** | ❌ 未实现 | 候选 | — |

**关键发现**：v2 文档设想的「3 件事」**90% 已落地**，落地点比 v2 文档想的更精细（20 特征 vs
5 指标；z-score 留出集分位 vs 4 维硬阈值；engine 端接 vs 后置审计）。

## 三、真正可能缺的（v2.1 增量待查）

本调研完成后，v2.1 增量设计需确认 4 件事：

1. **style_distance 的结果是否落库到 `projects.style_narrative` 字段？**
   若只是 engine 算一次返回 dict 内存里丢——**无法回看**（缺历史漂移曲线）。
2. **告警阈值在哪？** `mean_abs_z` 给的是数值，**是 warn 还是 error？** 用户阈值。
3. **前端面板是否有 4 维 / 20 维展示？**（视觉验收「像不像」关键）
4. **`audit_kg` 工具（agent）是否在主链调用？** 还是只手动？

## 四、不做的事（你「精巧优先」原则）

- ❌ **重复造轮**：v2 文档原本要做的「句长直方图 / 比喻密度 / 章首模式 4 类 / 钩子轮换 3 类」——stylometry
  已实现 20 特征覆盖度更广。
- ❌ **不学 Ex3 的 fine-tune / Kovatchev 的 GRPO**——无 GPU，且本项目架构是「智谱 + RAG + prompt」非训练。
- ❌ **不学 StoryScope 的人类 vs AI 分类**——是评测方向不是蒸馏。
- ❌ **不新增表**——落库到现有 `projects.style_narrative` JSON 字段即可。

## 五、Skill 沉淀价值

本调研的 4 个工业 / 学术方法学 + master 现状盘点 → **应沉淀进 `ai-novel-generator` skill** 的
新章节「文风蒸馏方法学参考」。未来窗口遇到类似蒸馏需求**直接看 skill 知道 master 已经做到
什么程度**，避免每次都重新做行业调研。
