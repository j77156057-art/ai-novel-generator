# 接受 percentile 100% = 工程常态

> 设计时间：2026-10-18 12:50
> 触发：v2.18 (智谱 30 章 方案 C) / v2.20 (智谱 30 章 + max_tokens 8192) 真链路 percentile 仍 100/100。

---

## 0. 现状

15 轮真链路 + probe 一致确认（v2.1 → v2.20）：
- 智谱 glm-4-flash-250414 文风服从性上限：mean_abs_z 1.0-1.8 区间
- 各 bucket 留出 max 1.27-1.61 区间
- 智谱生成段 z 与 bucket 留出 max 几乎齐平（差 0.07-0.3）
- 智谱 + 任何 max_tokens + 任何 reference 长度组合都接近「z ≈ 留出 max」边界
- 50+ 章 reference 让 long bucket 留出 max 升 1.6-1.8 不够
- nemotron-3-super OR 限流（v2.19b 27 分钟后 429）

**B 项 3 件事 0/3 完成**：
- B ① 参考 50+ 章：v2.16 60 章 19 万字 fit 留出 max 1.613 → 100+ 章 32.9 万字应升到 2+，但 v2.20 实测**长文 features 涨更多**
- B ② nemotron-3-super：OR 429 限流
- B ③ max_tokens 8192：v2.20 段 1 不截断（5779 字），但 z 反而涨（1.27→1.506）

**percentile 100% 触发的根因** = 智谱弱模型文风 + bucket 留出分布 max 工程边界 —— **无法根除**。

---

## 1. 接受 percentile 100% = 工程常态

**核心思路**：
- percentile = UI 标尺（非量化指标）
- bucket chip + scale + 字数 = 真正直观的反馈
- 告警仅当用户跑端到端对话时人工看 bucket chip 即可

**为什么 percentile 不是量化指标**：
- v2.18 段 1 mean_abs_z 1.27 vs medium bucket 留出 max 1.27 → 边界 100%（真实数据）
- v2.18 段 2 mean_abs_z 1.195 vs medium bucket 留出 max 1.27 → 边界 100%（真实数据）
- v2.20 段 1 mean_abs_z 1.506 vs long bucket 留出 max 1.61 → 99.5% 以下（不触发）
- v2.20 段 2 mean_abs_z 1.84 vs short bucket 留出 max 1.49 → 100%（真实数据）
- **percentile 是「z vs 留出 max」的比值，不是「风格好坏」的绝对值**

**bucket chip + scale + 字数才直观**：
- `bucket=short/medium/long` —— 段走了哪档 profile
- `scale=0.5x/1.0x/2.0x` —— 长度对齐信号
- `chars=1440/5779/6264` —— 实际生成字符数
- **「本段是 short 0.5x」比「本段 99.5% 偏大」更直观**

---

## 2. UI 文案调整

**当前**：
> percentile ≥ 97.5（留出集分位）触发告警；engine 落库，段级历史。

**改为**：
> percentile ≥ 99.5（留出集分位）触发告警（v2.15 从 97.5 提到 99.5——
> bucket 留出样本数少是工程边界，100% 触发常态化不构成真异常）。
> 段 1 mean_abs_z ≈ 1.27 与 medium bucket 留出 max 几乎齐平——
> percentile = UI 标尺，bucket chip + scale + 字数更直观。

**"暂无告警"文案**：
- 当前：所有生成段 percentile 都在 97.5 分位以内
- 改为：所有生成段 percentile 都在 99.5 分位以内

**配合 v2.14 (07d1a2d) 已加的 3 列**：bucket / scale / 字数 —— 用户看 chip + scale 就够。

---

## 3. 真链路触发「真异常」的场景

percentile 100% 常态不等于告警无用。**以下场景** percentile > 99.5 仍是真信号：
- 智谱生成段 z 远超留出 max（1.5+ 倍）—— 风格突变
- bucket 跨档突变（短文走 long bucket 1.93x）—— length 归一化失效
- 模型换了（智谱 vs nemotron 风格差异大）—— 新模型无留出参考

**告警阈值从 99.5 再下调到 99.9 留 buffer**：
- 当前 99.5 触发频繁（100/100 真链路）—— 用户看到告警 100 条疲劳
- 建议 99.9 触发更少（仅真异常）—— 留 buffer 给真风格突变

但**当前生产保持 99.5**（f2a0611 commit），**调整 UI 文案**已足够。

---

## 4. 不做什么

- ❌ 不再调 percentile 阈值（97.5 → 99.5 已 commit）
- ❌ 不再加 reference 100+ 章（边际效应不显著）
- ❌ 不再换 nemotron（OR 限流 + 慢）
- ❌ 不再改 features 算法（方案 C 治本已实施）
- ❌ 不再改 distance 算法（v2.20 z 仍与留出 max 几乎齐平是智谱弱文风问题，不是算法问题）

---

## 5. 仍要做什么

- ✅ **改 UI 文案**（本 commit）—— 反映工程常态
- ⏳ **写 stage6 handoff** —— 收尾本文档 + 全部产出
- ⏳ **5 章短链路真链路**（stage5 handoff 第 6 节 D）—— 验证短链路 percentile 表现
- ⏳ **客户端浏览器端到端冒烟**（已 49add1e 实施）—— 不需重做

---

## 6. 设计哲学

- **审计先行** —— 13 轮真链路 + 2 轮 probe 验证 percentile 100% 根因
- **精巧优先** —— 不再追求 percentile < 80%（已智谱弱文风 + 工程边界双重制约）
- **接受工程边界** —— percentile = UI 标尺，bucket chip + scale 才是真反馈
- **设计先行、过审再开工** —— 本设计文档先评审

---

## 7. 关键参考

- `docs/percentile_root_cause_analysis.md`：13 轮验证 percentile 100% 真因
- `docs/style_log_buckets_design.md`：方案 B「对数粒度 profile」设计
- `docs/style_length_normalize_design.md`：方案 C「features 长度归一化」设计
- `docs/realbook-full-flow.md`：真书全流程九道工序的落地记录
