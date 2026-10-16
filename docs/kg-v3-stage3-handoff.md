# KG v3.0 阶段 3 接手文档

> 生成时间：2026-10-16。当前 HEAD：`9305501`（worktrees/master 分支）。
> 阶段 2 候选 5/6/7 **全部完成**——本接手文档面向下一阶段。
> 接手后第一件事：看本节 0（并发状态）+ 跑核心 5 套件确认环境通。

---

## 0. 接手第一件事：工作树状态

```
干净的。worktrees/master HEAD = 9305501，未提交改动 = 0。
主工作树 / 其它 worktree 待用户核对（git worktree list）。
```

若发现并发 agent 未提交改动，按 stage2 接手文档第 0 节三选一处理。

---

## 1. 环境（与 stage2 同）

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
9305501 e2e journey 设计文档：补第十节真链路跑通数据
0e35bc4 修 LocalFallbackLLMProvider 签名（5 断言）
152d027 e2e user journey 冒烟脚本
babbe09 e2e user journey 回归测试（19 断言）
b73f421 e2e user journey 设计文档
27bb430 Judge 时间锚量纲转换实现（57 断言）
c54efa6 Judge 时间锚量纲转换设计
ebb9809 保留并发 agent 未提交改动
e31a2a9 docs: 接手文档划掉大纲级联候选
71b8678 v3.0 大纲级联协议阶段 2-3 收尾
75819ba v3.0 大纲级联协议阶段 2-3 后端
```

加上 stage2 的 6 个 commit（`581e813` 起），KG v3.0 阶段 2 全部 3 个候选完成。

## 3. 三个候选的完成细节

### 3.1 Judge 时间锚量纲转换（27bb430/c54efa6，已完成）

- `app/kg/era_convert.py` 解析层：parse_story_time / era_year / extract_era_name
- `app/kg/data/era_names.json` 449 年号（教育部《重编国语辞典》+ opencc 繁简）
- 口径 C（用户拍板，相邻同 era 才比 year；零参数）
- 三道 fail-closed 边界防红楼梦全文 15 处假阳性（跨词切碎/量词误食/著录长句）
- 同名年号按起止区间消歧（建元=西汉/东晋/南齐 三处）
- 旧 test_v30_judge.py 「非年份时间不判」断言按新行为改

### 3.2 e2e user journey 测试（babbe09/152d027/b73f421/0e35bc4，已完成）

- mock 回归 `tests/test_e2e_journey_pipeline.py` 19 断言（六步 + 空数据语义）
- 真 LLM 冒烟 `scratch/run_journey_e2e.py`（后台跑 8 段智谱 10-15 分钟）
- 真链路首跑 7/8 段正常（662-1247 字），1 段智谱 hang
- kg_events=50 / kg_characters=14 / slice/prompt 834 字符（**KG 真回流**）

### 3.3 冒烟暴露的 2 个真 bug

1. `LocalFallbackLLMProvider.chat` 缺 `stop_sequences` 签名
   （v2.6.1 per-request 透传时漏同步）→ `0e35bc4` 修
2. pydantic-settings 不读 os.environ → 冒烟脚本自修（启动时把 env key
   注入 worktree 的 .env，finally 恢复原状）。**不要**把 key 写进 worktree .env。

## 4. 测试约定与当前绿数

- 独立脚本：`cd backend && env -u PYTHONPATH -u VIRTUAL_ENV "<venv python>" tests/test_xxx.py`
- 冒烟脚本位置：`C:\Users\h'h'h\AppData\Local\hermes\cache\scratch\run_journey_e2e.py`
  （启动要 export `ZHIPU_API_KEY`，跑完自动清 worktree .env）
- 核心 6 套件当前全绿（cascade_protocol 49 / batch_worker_observability 36 /
  kg_v3_frontend_api 41 / v30_integration 33 / v30_judge 31 / era_dimension 57）
  + 4 套件（v30_retrieval 39 / v30_extractor 102 / v30_schema 39 / e2e_v26 9
  / e2e_journey 19 / llm_fallback_signature 5）—— **12 套件 460 断言全绿**
- **接手后第一件事：跑一遍核心 6 套件确认环境通**

## 5. 下一步候选（仅剩 1 项）

| # | 项 | 说明 | 难度 |
|---|---|---|---|
| 5 | **真环境 nemotron 复跑对比** | OpenRouter 配额重置后（每天 0 点），用 nemotron-3-ultra 对比智谱弱模型的抽取质量（foreshadows/knowledge 智谱全 0 行）。**先查当天剩余额度**（免费档 50 次/日），耗尽会全 429 | 半天 |

另有打磨项（非候选）：前端关系图升级力导向 / 移动端适配 / KG 工作台数据刷新按钮
统一（现在每 tab 各自刷新，切 tab 惰性加载只在首次）。

## 6. 用户偏好（沿用 stage2）

- 中文交流；设计文档先评审再实现；里程碑完成就 commit
- 要求彻底完整实现，会质疑"就这点吗"；百万字~千万字规模考虑
- 真实长篇语料端到端验证（红楼梦种子 `data/seed/红楼梦.txt`，1047687 字）
- "精巧"优先于"大而全"——不要一味堆表/堆功能，要动态生长的设计
- 多 agent 并行时每个任务开独立 worktree
- 增量交付+端到端真验证，但拒绝粗糙

## 7. 已知坑（多窗口踩过，stage2 8 条 + 本轮 2 条新）

沿用 stage2 7 节全部坑（路径 / 测试 / 后台进程 / uvicorn / 弱模型 / 期望值 vs 实现 / 限流）。
**本轮新增 2 条**：

1. **pydantic-settings 不读 os.environ**——冒烟脚本遇过，已沉淀到 skill。
2. **测试期望值 vs 实现行为边界**——本会话踩 3 次（11 字字面量、建元 3 年、journey 关键词+豁免硬编码），
   全部用 probe 查真实值再改测试，已沉淀到 skill。

## 8. API key

- 智谱 glm-4-flash 有效 key 在 `backend/.env` 的 ZHIPU_API_KEY（2026-10-15 验证可用）
- **实测主工作树 .env 里 ZHIPU_API_KEY=*** 是占位符**——真 key 在用户 shell
  的 os.environ 里（ZHIPU_API_KEY=320f26...），冒烟脚本自注入 .env
- OpenRouter key 在 `backend/.env` 的 OPENROUTER_API_KEY（nemotron-3-ultra-550b-a55b:free）
  —— 也需查真实值
- 启动后必须真调一次 LLM 验通，不能只看 uvicorn 200

## 9. 设计文档清单

- `docs/kg-v3-design-final.md`：v3.0 最终设计（9 表 + Judge + 8 功能）
- `docs/kg-v3-handoff.md`：stage1 接手文档（历史）
- `docs/kg-v3-stage2-handoff.md`：stage2 接手文档（5/6/7 候选时点）
- `docs/kg-cascade-protocol-design.md`：级联协议设计
- `docs/kg-batch-observability-design.md`：批抽可观测性设计
- `docs/kg-judge-era-dimension-design.md`：**本轮新增**——年号纪年量纲转换（口径 C + 三道边界 + 跨年号实测）
- `docs/kg-e2e-journey-design.md`：**本轮新增**——e2e user journey（设计 + mock 模板 + 真链路跑通数据）
- skill `ai-novel-generator` 已沉淀本轮所有踩坑（pydantic-settings / 期望值边界 / era_convert 等）

## 10. 接手后第一件事

按 stage2 同款：
1. 核 HEAD 与并发状态：`git log --oneline -3` + `git status --short`
2. 批量跑核心 6 套件：
   ```
   cd "C:/Users/h'h'h/AppData/Local/hermes/cache/scratch"
   "C:/Users/h'h'h/AppData/Local/Programs/Python/Python313/python.exe" run_tests.py \
     tests/test_v30_judge.py tests/test_era_dimension.py \
     tests/test_kg_cascade_protocol.py tests/test_batch_worker_observability.py \
     tests/test_kg_v3_frontend_api.py tests/test_v30_integration.py
   ```
3. 紧凑回报（每套件 exit 码 + N 断言通过/失败）
4. **核心 5 套件 + era_dimension 全绿** = 环境 OK
5. 若要做 #5 nemotron：先 `urllib` 调一次 `/api/v1/chat/completions` 验额度
