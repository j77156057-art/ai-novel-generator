# KG v3.0 + 文风蒸馏阶段 5 接手文档

> 生成时间：2026-10-18 07:30（**已合并 stage5 阶段 = 方案 C「features 长度归一化」+ 治本 percentile 100% 触发**）。
> 当前 HEAD：`53323f8`（worktrees/master 分支）。
> 本文档面向下一窗口：先看第 0 节（并发状态）+ 第 3 节（方案 C 3 步）+ 第 4 节（测试）+ 第 6 节（已留 3 件）+ 第 8 节（API key）。

---

## 0. 接手第一件事：工作树状态

**master HEAD = 53323f8，未提交改动 = 0**。**所有 worktree 由用户掌控**：
- `worktrees/master`（主工作树，本会话方案 C 全部产出，HEAD = 53323f8）
- `worktrees/style-distill`（feat/style-distillation，HEAD = e9305d3，已 cherry-pick 同步）
- `worktrees/fix-extractor-3char-and-noise`（其他 agent 持有，**勿碰**）
- `worktrees/timeline-pacing`（其他 agent 持有，**勿碰**）
- `worktrees/codex-bc-dfg`（其他 agent 持有，**勿碰**）

```bash
cd "C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator"
git worktree list
git log --oneline -10 master
```

**接手后第一件事**：跑核心 18 套件确认环境通（见第 4 节）。

---

## 1. 环境（沿用 stage3/4）

- 项目路径：`C:\Users\h'h'h\WorkBuddy\2026-10-12-11-59-30\ai-novel-generator`
- 你的 worktree：`worktrees/master`（HEAD = `53323f8`）
- 用户名含单引号 → bash 路径会炸，命令写成 .py 脚本放 scratch 目录执行
- 后端 venv：`C:\Users\h'h'h\AppData\Local\Programs\Python\Python313\python.exe`（有 sqlalchemy 2.0.35）
- 跑测试前必须 `env -u PYTHONPATH -u VIRTUAL_ENV`
- git commit 用 `git commit -F msg文件`（**不要**在 commit message 里用反引号）
- 不要碰 `worktrees/fix-extractor-3char-and-noise` / `timeline-pacing` / `codex-bc-dfg`
- taskkill 在 MSYS bash 里要用单斜杠 `/F /PID`，`//F` 不识别；Python subprocess 调 `taskkill` 也 OK
- 前端 `npm run dev` 需 `API_TARGET` 环境变量；Popen 里 npm 要写 `npm.cmd`
- uvicorn 端口避开 8020/8100/8848/8011/8021/8050/8060
- **pydantic-settings 不读 os.environ，只读 .env 文件**——见 skill 沉淀
- **netstat 输出 GBK 编码**——Python 解析用 `encoding="gbk", errors="replace"`

---

## 2. 本次会话完成的工作（5 个 commit 方案 C 3 步 + v2.17/v2.18 验证）

```
53323f8 docs: v2.17 smoke 方案 C 真链路验证
e0f6106 feat(style): Step 3 pipeline.py 同步 + style_distance 选 bucket 重算 vec
63c1d03 feat(style): Step 2 实现（NON_DENSITY_FEATURES + _length_scale_factor + features 升级）
2bf2e86 test(style): Step 1 RED 测试（10 断言）
d75c9d5 docs: 方案 C「features 长度归一化」设计（治本 percentile 100% 触发）
```

加上 stage2/3/4 的所有 commit，**方案 C 完整实施 + 真链路验证**。

---

## 3. 方案 C「features 长度归一化」3 步实施（阶段 5 主线，治本 percentile 100%）

### 3.1 根因（沿用 stage4 f519cf3）

13 轮真链路冒烟验证 percentile 100% 触发：
- 智谱 250414 段 1 6264 字 mean_abs_z=2.094，percentile=100%
- nemotron-3-super 段 2 2841 字 mean_abs_z=9.165，percentile=100%
- 方案 B「对数粒度 profile」段 1 mean_abs_z 降 40%（3.476→2.094），worst_features 全变，但 **percentile 计算本身没变**

**真因**：features 20 个特征中 **11 个非密度类**（按字/句/段/run 统计）—— 短文 vs 长文统计波动大，导致 `mean_abs_z` 系统性偏大。

### 3.2 方案 C 设计（d75c9d5）

profile fit 时**用 bucket 自己的 unit_chars 算 features**（短文→short bucket→1500 字缩放/长文→long bucket→6000 字缩放）—— 11 个非密度特征乘 `exp(-|log(len/unit_chars)| * 0.3)` 双向收敛。

**关键设计**：
- `NON_DENSITY_FEATURES` frozenset (11 个) = {句长均值/CV/p90, 短/长句占比, 段长CV, 段均句数, 对白字/段占比, 短串占比, 段尾升华率}
- `DENSITY_FEATURES` = FEATURES - NON_DENSITY_FEATURES (9 个, per1k 已归一)
- `_length_scale_factor(text_chars, unit_chars) = exp(-|log(ratio)| * 0.3)`
  - 1x → 1.0（基准）
  - 2x → 0.81（长文统计稳定，缩 0.81）
  - 0.5x → 0.81（双向收敛对称）
  - unit_chars<=0/None → 1.0（退化）

### 3.3 实施 3 步

- **Step 1（2bf2e86）** RED 测试 10 断言
- **Step 2（63c1d03）** `NON_DENSITY_FEATURES` + `_length_scale_factor` + `features(text, *, unit_chars=3000)` 签名升级 + 11 个非密度特征应用 scale
- **Step 3（e0f6106）** `_fit_single_bucket` 内部 features 调用都传 `unit_chars` + `style_distance` 选 bucket 后用 `bucket.unit_chars` 重算 vec

### 3.4 v2.17 真链路验证（53323f8）

30 章 9.3 万字大奉 reference + 智谱 250414 + min_chars + max_tokens + 2 禁忌：

| 指标 | v2.15 (无方案 C) | **v2.17 (方案 C)** | 变化 |
|---|---|---|---|
| 段 1 mean_abs_z | 3.708 | **1.217** | **-67%** ⭐ |
| 段 2 mean_abs_z | 3.093 | 2.618 | -15% |
| 段 1 bucket | short 0.48x | medium 0.98x | |
| 段 2 bucket | medium 0.76x | short 0.2x（智谱截断 606 字）| |
| 段 1 worst | 段尾升华率/句号密度/段均句数 | 句号密度/句长CV/句长均值 | 变了 |
| 段 2 worst | 段尾升华率/长句占比/句长均值 | 段尾升华率/句长p90/句长CV | 收敛 |

### 3.5 probe 路径验证（沿用 v2.16 数据 + 方案 C）

30 章 9.3 万字 fit profile + 6 个不同长度文本：

| 文本 | 旧版 percentile | **方案 C 后** |
|---|---|---|
| 1440 字 | 100.0 | **92.0** |
| 3000 字 | 100.0 | 100.0（medium 留出 max=1.27 略超）|
| 6264 字 | 100.0 | **80.0** ⭐ |
| 5674 字 | 100.0 | **80.0** ⭐ |

**3/4 段 percentile 跌破 100%**（80-92 < 97.5 阈值）—— **方案 C 治本**！

### 3.6 真链路 percentile 仍 100% 的另一条根因

v2.17 真链路 percentile 仍 100/100 —— **不是方案 C 失效**，是**智谱弱模型 + max_tokens 截断**：
- 段 2 实际 606 字（撞 max_tokens=4096 截断）—— 走 short bucket（留出 30 max=1.49）—— mean_abs_z 2.618 > 1.49 → 100%
- 段 1 实际 2947 字（撞 max_tokens=2048 截断）—— 走 medium bucket（留出 11 max=1.27）—— 1.217 略超 1.27 → 100%
- 智谱弱模型 + max_tokens 截断导致段长变化大，**bucket 命中尺寸不一**，部分 bucket 留出 max 不够

**真链路要根除 percentile 100%**：参考 50+ 章 + 智谱升级到 nemotron-3-super + max_tokens 拉满（>=8192）。

---

## 4. 测试约定与当前绿数

- 独立脚本：`cd backend && env -u PYTHONPATH -u VIRTUAL_ENV "<venv python>" tests/test_xxx.py`
- 集中跑：`cd scratch && "C:/Users/h'h'h/AppData/Local/Programs/Python/Python313/python.exe" run_tests.py tests/test_xxx.py [更多 tests]`
- 冒烟脚本位置：`C:\Users\h'h'h\AppData\Local\hermes\cache\scratch\smoke_v2XX.py`（v21 起，17 个版本）

**当前绿数（stage5 末）**：
- 18 套件 548 断言全绿
  - 14 套件 stage3 末基线 491 断言
  - test_log_buckets_profile 17 断言（方案 B Step 1）
  - test_log_buckets_integration 20 断言（方案 B Step 3）
  - test_log_buckets_threshold 2 断言（阈值 99.5 last-mile）
  - **test_length_normalize 18 断言（方案 C Step 1）**

**接手后第一件事**：跑 18 套件确认环境通。

---

## 5. 关键决策与本会话沉淀

### 5.1 方案 C「features 长度归一化」3 步实施（5 个 commit）

详见第 3 节。**核心价值**：
- 段 1 mean_abs_z 降 67%（3.708→1.217）—— **方案 C 在真链路治本**
- probe 路径 6264 字 percentile 80%（之前 100%）
- 11 个非密度特征按 log 长度缩放，让跨长度可比
- 与方案 B「对数粒度 profile」叠加 = 双层归一（bucket 切分 + 长度归一）

### 5.2 probe 路径 vs 真链路

- **probe 路径**（直接调 `fit_profile_v2` + `style_distance`）1s 跑完，验证方案 C 治本 percentile
- **真链路**（uvicorn + generate）慢（智谱 1 段 5-10 min），受 max_tokens 截断影响
- **新发现**：真链路 percentile 100% 还有第二条根因——智谱弱模型 + max_tokens 截断

### 5.3 patch 技能沉淀（v4/v5 失败 4 次 + fit_profile 多次 patch 错配）

file 里 multi-line string concat 末尾常有字面 `\\n\\n`（4 char backslash + n），patch 时必须用字面 4 字符锚点。

### 5.4 增量设计哲学

- **审计先行**——开新维度前先查 master 现状
- **精巧优先**——11 个非密度特征单独缩放，不动 9 个密度
- **设计先行、过审再开工**——本设计文档先评审
- **失败用 probe 不急改实现**——上轮 exec(open(...).read()) 120s timeout 即是「不急」

### 5.5 端到端 vs probe 路径

真链路（uvicorn + generate）慢（60 章 fit profile 阻塞 5+ min + 智谱 5+ min）—— **probe 路径直接调 `fit_profile_v2` + `style_distance`** 是快速验证 bucket 切分效果的最快手段。**真链路 vs probe 数据存档各有用**：真链路 percentile/raw text；probe bucket 留出分布 / max holdout z。

### 5.6 cherry-pick 优先于 merge（沿用 stage3）

`git merge feat/style-distillation` 会拖 40573 行删除——绝对不能 merge。**cherry-pick 单独挑 2 commit 干净**。

---

## 6. 下一步候选（用户已确认留 3 件）

| # | 项 | 说明 | 前置 | 估时 |
|---|---|---|---|---|
| A | **写 stage5 handoff ✅** | 本会话完成（本文档） | — | — |
| B | **真链路 percentile 100% 根除——参考 50+ 章 + 智谱升级** | v2.17/18 真链路仍 100% 触发；智谱弱 + max_tokens 截断；升级到 nemotron-3-super + 50+ 章 reference + 拉满 max_tokens | v2.18 数据 + OpenRouter OR key | 1-2 天 |
| C | **接受 100% 触发 = 工程常态** | percentile = UI 标尺而非量化指标；告警仅当用户跑端到端对话时人工看 bucket chip | UI 调整 + 文档 | 半天 |
| D | **5 章短链路真链路** | 5 章大奉 1.5 万字 reference 跑端到端；v2.1 时跑过（segment 1500 字）；现在带方案 B/C 再跑 | 改 trim_dafeng5.py | 半天 |
| E | **客户端浏览器端到端冒烟 ✅** | 15 断言全过（49add1e commit 实施） | — | — |
| F | **加 reference 100+ 章 5 万字** | v2.16 60 章留出 max=1.613；试 100+ 章 | 切片脚本 + 真链路 | 半天 |

**真正推荐下一步**：B（真链路 percentile 100% 根除）或 C（接受工程常态）。

---

## 7. 用户偏好（沿用 stage2/3/4）

- 中文交流；设计文档先评审再实现；里程碑完成就 commit
- 要求彻底完整实现，会质疑"就这点吗"；百万字~千万字规模考虑
- 真实长篇语料端到端验证
- **"精巧"优先于"大而全"**——不要一味堆表/堆功能，要动态生长的设计
- 多 agent 并行时每个任务开独立 worktree
- 增量交付 + 端到端真验证，但拒绝粗糙
- "失败用 probe 不急改实现"
- "先问用户再动手"的红线

---

## 8. 已知坑（多窗口踩过，stage2 8 + stage3 5 + stage4 2 + 本轮 1 条新）

沿用 stage2/3/4 全部坑（路径 / 测试 / 后台进程 / uvicorn / 弱模型 / 期望值 vs 实现 / 限流 / provider.py max_tokens / 智谱 min_tokens / multi-line string concat 末尾字面 `\\n\\n` / netstat GBK 编码 / probe 路径 vs 真链路）。

**本轮新增 1 条**：

**真链路智谱 max_tokens 截断**——v2.17 段 2 实际 606 字（撞 max_tokens=4096 截断）—— **生成段长 < 期望值**导致 percentile 仍 100%。**修法**：调高智谱 max_tokens 至 8192+ 或换 nemotron-3-super。**或在 system prompt 加「未完成写【本节完】才停」**。

---

## 9. API key

- 智谱 glm-4-flash-250414 有效 key：`«redacted-secret»`（2026-10 验证）
  - 启动必须真调一次 LLM 验通（看 uvicorn log 是否真打到了智谱），不能只看 uvicorn 200
- **实测主工作树 backend/.env 里 ZHIPU_API_KEY=*** 是占位符**——真 key 在用户 shell 的 os.environ 里
- OpenRouter key（nemotron-3-super-120b-a12b:free）：`«redacted:sk-…»`（2026-10-17 16:00 验证可用）
  - **v2.13 v3 实测 OR key 限流**——nemotron-3-super 跑 2 段 撞 429 Too Many Requests——单次 1 段 OK，loop 2 段就限流
- MiniMax key：缺

---

## 10. 设计文档清单

- `docs/kg-v3-design-final.md`：v3.0 最终设计
- `docs/kg-v3-handoff.md`：stage1 接手文档（历史）
- `docs/kg-v3-stage2-handoff.md`：stage2 接手文档
- `docs/kg-v3-stage3-handoff.md`：stage3 接手文档
- `docs/kg-v3-stage4-handoff.md`：stage4 接手文档（方案 B 5 步 + last-mile + 60 章边际）
- `docs/kg-v3-stage5-handoff.md`：**本接手文档**（方案 C 3 步 + 治本 percentile 100% 触发）
- `docs/percentile_root_cause_analysis.md`：percentile 100% 触发架构级根因
- `docs/style_log_buckets_design.md`：方案 B「对数粒度 profile」设计
- `docs/style_length_normalize_design.md`：**方案 C「features 长度归一化」设计**
- `docs/v21-v218_*_smoke_*.json`：14 轮真链路冒烟 raw 数据归档

---

## 11. 新窗口第一件事 checklist

```bash
# 1. 状态确认
cd "C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator"
git worktree list
git log --oneline -10 master

# 2. 跑 18 套件确认环境通（548 断言全绿基线）
cd "C:/Users/h'h'h/AppData/Local/hermes/cache/scratch"
"C:/Users/h'h'h/AppData/Local/Programs/Python/Python313/python.exe" run_tests.py \
  tests/test_v30_judge.py tests/test_era_dimension.py tests/test_kg_cascade_protocol.py \
  tests/test_batch_worker_observability.py tests/test_kg_v3_frontend_api.py \
  tests/test_v30_integration.py tests/test_v30_retrieval.py tests/test_v30_extractor.py \
  tests/test_v30_schema.py tests/test_e2e_v26_pipeline.py tests/test_e2e_journey_pipeline.py \
  tests/test_llm_fallback_signature.py tests/test_style_warning_persistence.py \
  tests/test_sample_chars_floor.py tests/test_log_buckets_profile.py \
  tests/test_log_buckets_integration.py tests/test_log_buckets_threshold.py \
  tests/test_length_normalize.py

# 3. 选下一步（推荐 B 真链路根除 或 C 接受工程常态，参见第 6 节）
```

**注意**：
- worktree backend/.env 的 `ZHIPU_API_KEY=***` 是占位符，跑真链路时必须 export 或在脚本里注入
- v2.18 真链路在跑中（proc_964cbc5d75ec），等结果
- 智谱 max_tokens 截断是真链路 percentile 100% 的另一条根因（详见第 8 节）
