# KG v3.0 + 文风蒸馏阶段 3 接手文档

> 生成时间：2026-10-17 16:20（**已合并 stage3 阶段 = 本会话全部产出**）。
> 当前 HEAD：`0cca142`（worktrees/master 分支）。
> 本文档面向下一窗口：先看第 0 节（并发状态）+ 第 4-7 节（核心收获）+ 第 8 节（API key）+ 第 9 节（已知坑）。

---

## 0. 接手第一件事：工作树状态

**master HEAD = 0cca142，未提交改动 = 0**。**所有 worktree 由用户掌控**：
- `worktrees/master`（主工作树，本会话全在此操作，HEAD = 0cca142）
- `worktrees/style-distill`（feat/style-distillation，HEAD = e9305d3，**已通过 cherry-pick 同步 2 commit 到 master**，但 worktree 本身仍保留作为未来蒸馏 v8+ 的沙箱）
- `worktrees/fix-extractor-3char-and-noise`（其他 agent 持有，**勿碰**）
- `worktrees/timeline-pacing`（其他 agent 持有，**勿碰**）
- `worktrees/codex-bc-dfg`（其他 agent 持有，**勿碰**）

```bash
cd "C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator"
git worktree list
git log --oneline -10 master
```

**接手后第一件事**：跑核心 14 套件确认环境通（见第 4 节）。

---

## 1. 环境（沿用 stage2）

- 项目路径：`C:\Users\h'h'h\WorkBuddy\2026-10-12-11-59-30\ai-novel-generator`
- 你的 worktree：`worktrees/master`（HEAD = `9305501`）
- 用户名含单引号 → bash 路径会炸，命令写成 .py 脚本放 scratch 目录执行
- 后端 venv：`C:\Users\h'h'h\AppData\Local\Programs\Python\Python313\python.exe`
  （有 sqlalchemy 2.0.35）
- 跑测试前必须 `env.pop("PYTHONPATH") + env.pop("VIRTUAL_ENV")`
- git commit 用 `git commit -F msg文件`（**不要**在 commit message 里用反引号）
- 不要碰 `worktrees/timeline-pacing` 和 `worktrees/style-distill`（其他 agent 的）
- taskkill 在 MSYS bash 里要用单斜杠 `/F /PID`，`//F` 不识别
- 前端 `npm run dev` 需 `API_TARGET` 环境变量；Popen 里 npm 要写 `npm.cmd`
- uvicorn 端口避开 8020/8100/8848/8011/8021/8050/8060
- **pydantic-settings 不读 os.environ，只读 .env 文件**——见 skill 沉淀

## 2. 本次会话完成的工作（11 个 commit）

```
0cca142 docs: v2.11 nemotron-3-super 写小说 probe 实测数据
5edc8b6 feat(style): 采样加字数下限保护（短篇全取，长篇保底30万字）
4d1fe45 docs: 文风蒸馏工作记录（v3→v7 演进 + 踩坑）
e51e042 fix(prompts): build_system_prompt 加 2 条 AI 腔禁忌（省略号节制 + 避免四字格）
2a97cf0 docs: v2.9 smoke 闭环（30 章 9.3 万字 reference + min_chars + max_tokens）
cf93ccb fix(prompts): build_user_prompt directive 强化「写不少于 0.8N 字」
75d481a fix(llm): 6 个 provider 实例化点传 max_tokens=settings.writer_max_tokens
4bd7468 docs: v2.6 smoke 闭环（15 章 + generation_length=3000）
ddeae20 feat: ZHIPU_MODEL 默认升级到 glm-4-flash-250414（#5 实测最优）
db730db docs: v2.1 真链路冒烟首次跑通数据归档
98c6461 v2.1 文风告警落库 + Style 审计 subtab
```

加上 stage2 的 6 个 commit，KG v3.0 + 文风蒸馏 阶段 2/3 **全部候选完成**。

---

## 3. 三个关键发现（本会话主线）

### 3.1 percentile 100% 触发根因（5 轮真链路 + 1 轮 prompt 强化一致确认）

智谱 glm-4-flash-250414 的文风服从性上限已到——即使加 30 章 reference + max_tokens=4096 + min_chars=0.8N + 2 条 AI 腔禁忌，percentile 仍 100% 触发。**唯一根除路径 = 换模型**。

| 轮 | reference | min_chars | max_tokens | 段 1 wc | 段 2 wc | percentile |
|---|---|---|---|---|---|---|
| v2.1 | 1.5万/5章 | 否 | 2048 | 1727 | 1548 | 1/3 |
| v2.5 | 1.5万/5章 | 否 | 2048 | 1963 | 5444 | 2/2 |
| v2.6 | 4.7万/15章 | 否 | 2048 | 3840 | 825（截断）| 1/2 |
| v2.7 | 4.7万/15章 | 否 | **4096** | 1359 | 637 | 2/2 |
| v2.8 | 4.7万/15章 | **0.8N** | 4096 | 1652 | 3882 | 2/2 |
| **v2.9** | **9.3万/30章** | 0.8N | 4096 | **6336** | 618 | 2/2 |
| **v2.10** | 9.3万/30章 + 2 禁忌 | 0.8N | 4096 | 6264 | 4747 | 2/2 |

**v2.10 段 1 worst_features**：「四字格密度/问号密度/句号密度/句长p90/比喻密度」——「省略号密度」**退出**（2 条禁忌有效），但**其他特征仍 100%**——智谱文风服从性差到换 1-2 特征不够。

### 3.2 max_tokens 真 bug（75d481a）

v2.6 smoke 暴露智谱撞 max_tokens=2048 截断。**根因**：provider.py line 600-620 get_llm_provider() 实例化 6 个 provider（OpenRouter / Zhipu / SiliconFlow / MiniMax / GenericOpenAI / DashScope）**只传 api_key/base_url/model/models，没传 max_tokens**——走 OpenAICompatibleChatProvider.__init__ 的默认 None——智谱 API 兜底 2048 token 上限——长文（>1500 字）被截断。

**修法**：所有 6 个 provider 实例化点加 `max_tokens=settings.writer_max_tokens,`。providers.yaml writer.max_tokens=4096 早已设但未注入 provider，本次让 settings 真正流到 provider。

**附带坑**：第 1 次 patch 漏改 Zhipu 第 4 个 positional 参数为 `models=` 关键字（误把 `settings.zhipu_model_list` list 传到了 `extra_body` dict 位置——`self.extra_body = list` 引发 `**self.extra_body` 的 `**list is not a mapping` TypeError，导致 HTTP 500）。第 2 次 patch 修正：显式 `models=`.

### 3.3 换模型 = nemotron-3-super 真能写小说（0cca142）

**probe 路径**（绕开完整 engine）实测 OpenRouter nemotron-3-super-120b-a12b:free：

- 145.8s 写 4096 tokens（reasoning 588 + content 3508）
- msg 3691 字（vs 智谱 250414 基线 1300-1500 字，**2.5x**）
- 文风对比：
  * 自然对话 + 短句为主（vs 智谱爱四字格/省略号）
  * 现场感（破庙/霉味/青砖/铜牌）vs 智谱模板化描述
  * 少 AI 腔：极少省略号 / 无段尾升华
  * 现代刑侦观察（监控/指纹/DNA）作为穿越设定——创造性强

**3 款新免费模型 probe**（用户提供的 OR 图 1）：
- ❌ `nvidia/nemotron-3.5-super-120b-a12b:free` 404 invalid
- ❌ `nvidia/nemotron-3.5-super-vl:free` 120s upstream idle timeout
- ❌ `TeleAI/Xing4.0-29B-A4B:free` 404 invalid
- ✅ `Xing4.0-29B-A4B:free`（alt slug）200 但内容是结构化分析（reasoning 暴露在 content 里，不适合小说生成）
- ❌ `inclusionai/mercury-decide:free` 404
- ❌ `Respan/span-01-lite:free` 决策模型不能用 chat/completions

**确认**：`nvidia/nemotron-3-super-120b-a12b:free` 是当前 OpenRouter **唯一**可用 + 真能写小说的免费模型。

**v2.11 v3 端到端**（带 OR key + 30 章 reference）卡 8 分钟未到 B 段——`upload` HTTP 200 但 body 不含 `id` 字段（KeyError），smoke 持续等响应才看「卡住」。**这条路长度超出单次 background 540s 限制**，需更细化分段跑。

## 4. 测试约定与当前绿数

- 独立脚本：`cd backend && env -u PYTHONPATH -u VIRTUAL_ENV "<venv python>" tests/test_xxx.py`
- 集中跑：`cd scratch && "C:/Users/h'h'h/AppData/Local/Programs/Python/Python313/python.exe" run_tests.py tests/test_xxx.py [更多 tests]`
- 冒烟脚本位置：`C:\Users\h'h'h\AppData\Local\hermes\cache\scratch\smoke_v2XX.py`（v21 起，13 个版本）
  - **关键**：`smoke_v21.py` 仍是基础版（GEN_LENGTH=3000 已 sed 改过、LOG_PATH hardcode `journey_e2e.log`）
  - wrapper 改 `S._log` 路径 + `S.DAFENG_TXT`（5/15/30 章）
  - 启动要 export `ZHIPU_API_KEY`（或写 .env），跑完自动清 worktree .env

**当前绿数（stage3 末）**：
- 14 套件 491 断言全绿
  - v30_judge 31 / era_dimension 57 / kg_cascade_protocol 49 / batch_worker_observability 36
  - kg_v3_frontend_api 41 / v30_integration 33 / v30_retrieval 39 / v30_extractor 102
  - v30_schema 39 / e2e_v26_pipeline 9 / e2e_journey_pipeline 19 / llm_fallback_signature 5
  - style_warning_persistence 19 / **sample_chars_floor 12**（新加，cherry-pick 自 style-distill）

**接手后第一件事**：跑 14 套件确认环境通。

---

## 5. 关键决策与本会话沉淀

### 5.1 ZHIPU_MODEL 默认升级（ddeae20）

从 `glm-4-flash` 升级到 `glm-4-flash-250414`（5e3f0da N=4 实测：knowledge 0→8, state 0→6, foreshadows 0→4, event_type 1→4, in_story_date 6.7%→100%, 速度 -37%）。

worktree backend/.env 里 `ZHIPU_MODEL=` 注释化（默认从 config.py 读）。

### 5.2 AUTO_AUDIT_INTERVAL=5 / 500 条截断 / Style 审计独立 tab

按用户默认偏好（v2.1 设计文档默认值），实现于 98c6461 commit。

### 5.3 增量设计哲学（沿用 stage2）

- **审计先行**——开新维度前先查 master 现状（避免 90% 重复造轮）
- **精巧优先**——不要一味堆表/堆功能，要动态生长
- **设计先行、过审再开工**——用户原话
- **失败用 probe 不急改实现**——用户原话（stage2-handoff 第 7 节）

### 5.4 patch 技能沉淀（v4/v5 失败 4 次才修对）

file 里 multi-line string concat 末尾常有字面 `\\n\\n`（4 char backslash + n），patch 时必须用字面 4 字符锚点，否则 newline 转义错配导致 unterminated string literal。

实测 line 203 字符串 `"- 允许留白：...收住。\n"` 末尾是 1 个 newline + closing `"`，line 204 字符串 `"- 贴近真实节奏：...句句工整。\n\n"` 末尾是 2 个 newline + closing `"`。**patch 加 2 行禁忌**必须在 line 203 字符串末后 + line 204 字符串前插 4 个新 string literal，每个 8 空格 `"` 开头 `"` 闭合 + newline。

### 5.5 cherry-pick 优先于 merge（style-distill 25 commit 实际只 2 待合）

**`git merge feat/style-distillation` 会拖 40573 行删除**（40573 个 test_* 文件被删 —— 历史清理动作）—— **绝对不能 merge**。**cherry-pick 单独挑 2 commit 干净**（aafa512 0 冲突 + e9305d3 auto-merge OK）。

---

## 6. 下一步候选（用户已确认留 8 条）

| # | 项 | 说明 | 前置 | 估时 |
|---|---|---|---|---|
| A | **修 v2.11 v3 upload KeyError + 真链路跑通** | v2.11 v3 链路 8 分钟卡在 upload 后 KeyError，**需先查 /api/novels/upload 真实返回结构**——probe 抓 response body，verify key 字段名（`id` vs `novel_id` vs 嵌套） | 1 个 probe 脚本 | 半天 |
| B | **加 reference 至 30+ 章 ✅** | 已完成（v2.9 30 章 9.3 万字，commit 2a97cf0） | — | — |
| C | **#5 nemotron 复跑对比** | OpenRouter nemotron 3 款 0 点配额重置后 3 款全超时（v2.11 v1 probe 实测 nemotron-3-super 145.8s 写 4096 tokens）——nemotron 是新方案对比基线 | 0cca142 数据 | 半天-1 天 |
| D | **style-distill worktree 25 commit 合并 ✅** | 已 cherry-pick 2 commit（4d1fe45 + 5edc8b6），前 23 commit 之前已合 | — | — |
| E | **真链路跑 nemotron 端到端 percentile 实测** | 用 nemotron-3-super 跑 30 章大奉 reference，看 percentile 仍 100% 还是降至 < 97.5% | v2.11 v3 链路修通 + OR key 已注入 | 1 天 |
| F | **加 reference 至 50+ 章** | v2.9 30 章 9.3 万字仍 100% 触发，**50+ 章**试是否能再降（边际效应测试） | 大奉长篇切片脚本 | 半天 |
| G | **客户端浏览器端到端冒烟** | 之前没做过 | 前端 build + Playwright | 半天 |
| H | **彻底收尾 ✅** | 本会话完成 | — | — |

**真正下一步推荐**：A（修 v2.11 v3 upload）+ E（真链路跑 nemotron 端到端）—— 直接对症 6 轮真链路 + 1 轮 probe 暴露的根因。

---

## 7. 用户偏好（沿用 stage2）

- 中文交流；设计文档先评审再实现；里程碑完成就 commit
- 要求彻底完整实现，会质疑"就这点吗"；百万字~千万字规模考虑
- 真实长篇语料端到端验证（大奉打更人 484 万字 / 红楼梦种子 `data/seed/红楼梦.txt` 1047687 字）
- **"精巧"优先于"大而全"**——不要一味堆表/堆功能，要动态生长的设计
- 多 agent 并行时每个任务开独立 worktree
- 增量交付 + 端到端真验证，但拒绝粗糙
- "失败用 probe 不急改实现"（stage2-handoff 第 7 节）
- "先问用户再动手"的红线（stage2-handoff 第 0 节）

## 8. 已知坑（多窗口踩过，stage2 8 条 + 本轮 5 条新）

沿用 stage2 7 节全部坑（路径 / 测试 / 后台进程 / uvicorn / 弱模型 / 期望值 vs 实现 / 限流）。

**本轮新增 5 条**：

1. **pydantic-settings 不读 os.environ**——冒烟脚本遇过，已沉淀到 skill。
2. **测试期望值 vs 实现行为边界**——本会话踩 3 次（11 字字面量、建元 3 年、journey 关键词+豁免硬编码），全部用 probe 查真实值再改测试，已沉淀到 skill。
3. **`provider.py` 漏传 max_tokens = 真 bug**——所有 6 个 provider 实例化点必须显式传 `max_tokens=settings.writer_max_tokens`——provider.py 默认 None，智谱 API 兜底 2048 token 上限。**patch 时**：`models=[...]` 必须用关键字，不能当 positional 第 4 参数传（list 传 extra_body dict 位置会触发 `**list is not a mapping` TypeError）。
4. **智谱 API 不实现 `min_tokens` 字段**——probe 实测 200 OK 但 `completion_tokens` 不变。`frequency_penalty`/`presence_penalty` 智谱支持（200 OK）但对风格无改善。**唯一强制字数手段 = prompt 工程的 min_chars**。
5. **multi-line string concat 末尾字面 `\\n\\n`**——`prompts.py` line 203-204 字符串末尾是字面 backslash + n，patch 时必须用字面 4 字符锚点（v4/v5 patch 失败 4 次才修对）。

---

## 9. API key

- 智谱 glm-4-flash-250414 有效 key：`319c9d6c73d2474d9622e12565420641.h4cClOgRrekKsxrf`（2026-10 验证）
  - 启动必须真调一次 LLM 验通（看 uvicorn log 是否真打到了智谱），不能只看 uvicorn 200
- **实测主工作树 backend/.env 里 ZHIPU_API_KEY=*** 是占位符**——真 key 在用户 shell 的 os.environ 里（`ZHIPU_API_KEY=320f26...ev4x`），冒烟脚本自注入 .env
- OpenRouter key（nemotron-3-super-120b-a12b:free）：`sk-or-v1-REDACTED`（2026-10-17 16:00 验证可用，0cca142 commit 实测 145.8s 3691 字）
  - 同样**别写进 worktree .env**，冒烟脚本启动时注入 + finally 恢复
- MiniMax key：缺（如要测国内站需用户 export）

---

## 10. 设计文档清单

- `docs/kg-v3-design-final.md`：v3.0 最终设计（9 表 + Judge + 8 功能）
- `docs/kg-v3-handoff.md`：stage1 接手文档（历史）
- `docs/kg-v3-stage2-handoff.md`：stage2 接手文档（5/6/7 候选时点）
- `docs/kg-v3-stage3-handoff.md`：**本接手文档**（stage3 全部产出）
- `docs/kg-cascade-protocol-design.md`：级联协议设计
- `docs/kg-e2e-journey-design.md`：e2e user journey 设计
- `docs/kg-judge-era-dimension-design.md`：Judge 时间锚量纲设计
- `docs/kg-nemotron-compare-redirect.md`：#5 nemotron 转向文档
- `docs/style-distillation-v2-survey.md`：文风蒸馏 v2 背景调研
- `docs/style-distillation-v2.1-design.md`：文风蒸馏 v2.1 增量设计
- `docs/style-distillation-session-log.md`：v3→v7 工作记录
- `docs/v21-v211_*_smoke_*.json`：9 轮真链路冒烟 raw 数据归档

---

## 11. 新窗口第一件事 checklist

```bash
# 1. 状态确认
cd "C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator"
git worktree list
git log --oneline -5 master

# 2. 跑 14 套件确认环境通（491 断言全绿基线）
cd "C:/Users/h'h'h/AppData/Local/hermes/cache/scratch"
"C:/Users/h'h'h/AppData/Local/Programs/Python/Python313/python.exe" run_tests.py \
  tests/test_v30_judge.py tests/test_era_dimension.py tests/test_kg_cascade_protocol.py \
  tests/test_batch_worker_observability.py tests/test_kg_v3_frontend_api.py \
  tests/test_v30_integration.py tests/test_v30_retrieval.py tests/test_v30_extractor.py \
  tests/test_v30_schema.py tests/test_e2e_v26_pipeline.py tests/test_e2e_journey_pipeline.py \
  tests/test_llm_fallback_signature.py tests/test_style_warning_persistence.py \
  tests/test_sample_chars_floor.py

# 3. 选下一步（推荐 A 或 E，参见第 6 节）
```

**注意**：worktree backend/.env 的 `ZHIPU_API_KEY=***` 是占位符，跑真链路时必须 export 或在脚本里注入。OpenRouter key 同理。
