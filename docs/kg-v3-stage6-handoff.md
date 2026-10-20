# KG v3.0 + 文风蒸馏阶段 6 接手文档

> 生成时间：2026-10-18 13:00（**已合并 stage6 阶段 = 接受 percentile 100% = 工程常态 + UI 文案调整**）。
> 当前 HEAD：`d06b97a`（worktrees/master 分支）。
> 本文档面向下一窗口：先看第 0 节（并发状态）+ 第 3 节（D 项核心结论）+ 第 4 节（测试）+ 第 6 节（已留 1 件）+ 第 8 节（API key）。

---

## 0. 接手第一件事：工作树状态

**master HEAD = d06b97a，未提交改动 = 0**。**所有 worktree 由用户掌控**：
- `worktrees/master`（主工作树，本会话方案 C + D 全部产出，HEAD = d06b97a）
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

## 1. 环境（沿用 stage3/4/5）

- 项目路径：`C:\Users\h'h'h\WorkBuddy\2026-10-12-11-59-30\ai-novel-generator`
- 你的 worktree：`worktrees/master`（HEAD = `d06b97a`）
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
- **Python 路径用 raw string + h'h'h（不是 h\h\h）**——bash 转义陷阱

---

## 2. 本次会话完成的工作（4 个 commit）

```
d06b97a docs: 接受 percentile 100% = 工程常态 + UI 文案调整
b8f0289 fix(providers): writer.max_tokens 4096 → 8192（v2.20 验证不再截断）
bf78965 docs: v2.20 smoke B 项简化版（30 章 + 智谱 + max_tokens 8192）
9b076e5 docs: v2.18 smoke 方案 C 重跑（智谱生成文风已大幅逼近参考）
```

加上 stage2/3/4/5 的所有 commit，**方案 B（bucket 切分）+ 方案 C（features 长度归一化）+ D（接受工程常态）完整闭环**。

---

## 3. 接受 percentile 100% = 工程常态（阶段 6 主线）

### 3.1 根因（沿用 stage5）

15 轮真链路 + 2 轮 probe 一致确认 percentile 100% 触发：
- 智谱 glm-4-flash-250414 文风服从性上限：mean_abs_z 1.0-1.8 区间
- 各 bucket 留出 max 1.27-1.61 区间
- 智谱生成段 z 与 bucket 留出 max 几乎齐平（差 0.07-0.3）
- 智谱 + 任何 max_tokens + 任何 reference 长度组合都接近「z ≈ 留出 max」边界
- 50+ 章 reference 让 long bucket 留出 max 升 1.6-1.8 不够
- nemotron-3-super OR 限流（v2.19b 27 分钟后 429）

### 3.2 B 项失败（v2.19b/v2.20）

B 项 3 件事 0/3 完成：

| 任务 | 状态 | 失败原因 |
|---|---|---|
| B ① 参考 100 章 32.9 万字 | 完成切片但**未真链路** | 边际效应不显著（v2.16 60 章 max=1.613） |
| B ② nemotron-3-super | v2.19b 跑 27 分钟后 **OR 429** | Free 档速率限制 |
| B ③ max_tokens 8192 | v2.20 段 1 **不截断**（5779 字）但 z 反而涨 | 长文 features 仍系统性偏大 |

**v2.20 关键数据**：
- 段 1 wc 2491 → 5779（+132%，max_tokens 拉满生效）
- 段 1 mean_abs_z 1.27 → 1.506（+19%，长文 features 涨）
- 段 1 bucket medium 0.83x → long 1.93x（5779 字走 long）
- 段 1 percentile 100.0 → 100.0（仍 100%）

### 3.3 D 项设计（d06b97a）

**核心思路**：percentile = UI 标尺，bucket chip + scale + 字数 = 真正直观的反馈。

**为什么 percentile 不是量化指标**：
- v2.18 段 1 mean_abs_z 1.27 vs medium bucket 留出 max 1.27 → 边界 100%
- v2.18 段 2 mean_abs_z 1.195 vs medium bucket 留出 max 1.27 → 边界 100%
- v2.20 段 1 mean_abs_z 1.506 vs long bucket 留出 max 1.61 → 99.5% 以下
- v2.20 段 2 mean_abs_z 1.84 vs short bucket 留出 max 1.49 → 100%
- **percentile 是「z vs 留出 max」的比值，不是「风格好坏」的绝对值**

**UI 文案调整**（KgWorkspace.vue Style 审计 subtab）：
- 当前：percentile ≥ 97.5（留出集分位）触发告警
- 改为：percentile ≥ 99.5（留出集分位）触发告警（v2.15 从 97.5 提到 99.5——
  bucket 留出样本数少是工程边界，100% 触发常态化不构成真异常）。
  段 1 mean_abs_z ≈ 1.27 与 medium bucket 留出 max 几乎齐平——
  percentile = UI 标尺，bucket chip + scale + 字数更直观。

### 3.4 仍要保留的真信号

percentile 100% 常态化不等于告警无用。**以下场景** percentile > 99.5 仍是真信号：
- 智谱生成段 z 远超留出 max（1.5+ 倍）—— 风格突变
- bucket 跨档突变（短文走 long bucket 1.93x）—— length 归一化失效
- 模型换了（智谱 vs nemotron 风格差异大）—— 新模型无留出参考

---

## 4. 测试约定与当前绿数

- 独立脚本：`cd backend && env -u PYTHONPATH -u VIRTUAL_ENV "<venv python>" tests/test_xxx.py`
- 集中跑：`cd scratch && "C:/Users/h'h'h/AppData/Local/Programs/Python/Python313/python.exe" run_tests.py tests/test_xxx.py [更多 tests]`
- 冒烟脚本位置：`C:\Users\h'h'h\AppData\Local\hermes\cache\scratch\smoke_v2XX.py`（v21 起，20 个版本）

**当前绿数（stage6 末）**：
- 18 套件 548 断言全绿
  - 14 套件 stage3 末基线 491 断言
  - test_log_buckets_profile 17 断言（方案 B Step 1）
  - test_log_buckets_integration 20 断言（方案 B Step 3）
  - test_log_buckets_threshold 2 断言（阈值 99.5 last-mile）
  - test_length_normalize 18 断言（方案 C Step 1）

**前端 build**：
- vue-tsc 0 error
- vite build 4.27s（14 chunk：ProjectWorkspace 60KB / KgWorkspace 33KB / ...）

**接手后第一件事**：跑 18 套件确认环境通。

---

## 5. 关键决策与本会话沉淀

### 5.1 接受 percentile 100% = 工程常态（d06b97a）

15 轮真链路 + 2 轮 probe 一致确认 percentile 100% 触发是工程边界（智谱弱文风 + bucket 留出 max）。**不再追求 percentile < 80%**。**percentile = UI 标尺，bucket chip + scale + 字数 = 真反馈**。

### 5.2 max_tokens 8192 真改（b8f0289）

providers.yaml writer.max_tokens 4096 → 8192（v2.20 验证段 1 wc 2491→5779 不再截断）。z 略涨（1.27→1.506）是智谱长文 features 系统性偏大，与 max_tokens 无关。

### 5.3 治本完整链路

- **方案 B「对数粒度 profile」**（5 步）：段 1 mean_abs_z 3.476→2.094 降 40% + bucket 切分 + 前端 chip
- **方案 C「features 长度归一化」**（3 步）：段 1 mean_abs_z 3.708→1.27 降 66% + 治本 features 长度 bias
- **D 接受工程常态**（1 步）：percentile = UI 标尺 + bucket chip 真反馈

**完整 21 个 commit**（stage 2-6 累计）：

| Stage | commit | 内容 |
|---|---|---|
| 2 | 581e813 起 | era 量纲 + e2e journey + KG v3 |
| 3 | 103242f-0cca142 | 7 轮真链路 + nemotron probe + style-distill cherry-pick |
| 4 | 18bb4c0-07d1a2d | 方案 B 5 步 + 60 章边际 + 浏览器冒烟 |
| 5 | 2bf2e86-9b076e5 | 方案 C 3 步 + v2.17/v2.18 真链路 |
| 6 | b8f0289-d06b97a | max_tokens 8192 + v2.20 + 接受工程常态 |

### 5.4 patch 技能沉淀

- file 里 multi-line string concat 末尾常有字面 `\\n\\n`（4 char backslash + n），patch 时必须用字面 4 字符锚点
- netstat 输出 GBK 编码
- Python 路径 raw string 用 `h'h'h`（bash 转义陷阱：`h\h\h` 错）

### 5.5 增量设计哲学

- **审计先行**——13 轮真链路 + 2 轮 probe 验证 percentile 100% 真因
- **精巧优先**——不追求 percentile < 80%（已智谱弱文风 + 工程边界双重制约）
- **接受工程边界**——percentile = UI 标尺，bucket chip 才是真反馈
- **设计先行、过审再开工**——d06b97a 前 docs/accept_percentile_as_ui_meter.md 先评审

### 5.6 端到端 vs probe 路径

真链路（uvicorn + generate）慢（智谱 1 段 5-10 min / nemotron 1 段 3-10 min）。probe 路径（直接调 fit_profile_v2 + style_distance）1s 跑完。**真链路 vs probe 数据存档各有用**。

### 5.7 cherry-pick 优先于 merge（沿用 stage3）

`git merge feat/style-distillation` 会拖 40573 行删除——绝对不能 merge。**cherry-pick 单独挑 2 commit 干净**。

---

## 6. 下一步候选（用户已确认留 1 件）

| # | 项 | 说明 | 估时 |
|---|---|---|---|
| A | **写 stage6 handoff ✅** | 本会话完成（本文档） | — |
| B | **5 章短链路真链路 ✅** | v2.21 实测短链路没优势（短 bucket 留出 max 仍小 + 智谱弱文风主因）—— commit 44cae7d | — |
| C | **彻底收尾** | 把全部产出 commit 推完 | 1h |

**真正下一步推荐**：C（彻底收尾）。

---

## 7. 用户偏好（沿用 stage2-5）

- 中文交流；设计文档先评审再实现；里程碑完成就 commit
- 要求彻底完整实现，会质疑"就这点吗"；百万字~千万字规模考虑
- 真实长篇语料端到端验证
- **"精巧"优先于"大而全"**——不要一味堆表/堆功能，要动态生长的设计
- 多 agent 并行时每个任务开独立 worktree
- 增量交付 + 端到端真验证，但拒绝粗糙
- "失败用 probe 不急改实现"
- "先问用户再动手"的红线
- **接受工程边界**——percentile 100% 触发是工程常态

---

## 8. 已知坑（多窗口踩过，stage2 8 + stage3 5 + stage4 2 + stage5 1 + 本轮 1 条新）

沿用 stage2/3/4/5 全部坑。

**本轮新增 1 条**：

**OR nemotron-3-super free 档限流**——v2.19b 跑 27 分钟后 429 Too Many Requests。**修法**：单段跑（不 loop 2 段）+ 等配额重置（1 分钟/1 小时/1 天 视配额而定）+ OR 付费档。

---

## 9. API key

- 智谱 glm-4-flash-250414 有效 key：`«redacted-secret»`（2026-10 验证）
  - 启动必须真调一次 LLM 验通
- **实测主工作树 backend/.env 里 ZHIPU_API_KEY=*** 是占位符**——真 key 在用户 shell 的 os.environ 里
- OpenRouter key（nemotron-3-super-120b-a12b:free）：`«redacted:sk-…»`
  - **v2.13 v3 + v2.19b 实测 OR key 限流**——nemotron-3-super 跑 27 分钟后 429
- MiniMax key：缺

---

## 10. 设计文档清单

- `docs/kg-v3-design-final.md`：v3.0 最终设计
- `docs/kg-v3-handoff.md`：stage1 接手文档（历史）
- `docs/kg-v3-stage2-handoff.md`：stage2 接手文档
- `docs/kg-v3-stage3-handoff.md`：stage3 接手文档
- `docs/kg-v3-stage4-handoff.md`：stage4 接手文档（方案 B 5 步）
- `docs/kg-v3-stage5-handoff.md`：stage5 接手文档（方案 C 3 步）
- `docs/kg-v3-stage6-handoff.md`：**本接手文档**（D 接受工程常态 + UI 文案）
- `docs/percentile_root_cause_analysis.md`：percentile 100% 触发架构级根因
- `docs/style_log_buckets_design.md`：方案 B「对数粒度 profile」设计
- `docs/style_length_normalize_design.md`：方案 C「features 长度归一化」设计
- `docs/accept_percentile_as_ui_meter.md`：**D 接受工程常态设计**
- `docs/v21-v220_*_smoke_*.json`：20 轮真链路冒烟 raw 数据归档

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

# 3. 选下一步（推荐 C 彻底收尾 或 B 5 章短链路，参见第 6 节）
```

**注意**：
- worktree backend/.env 的 `ZHIPU_API_KEY=***` 是占位符，跑真链路时必须 export 或在脚本里注入
- nemotron-3-super 限流（v2.19b + v2.13 v3 实测），单段跑或用 OR 付费档
- percentile 100% 触发是工程常态（智谱弱文风 + bucket 留出 max 工程边界）
