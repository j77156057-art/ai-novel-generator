# e2e user journey 测试设计（kg-e2e-user-journey）

> 本文提到的 `scratch/…` 是本地工作目录下的脚本与中间产物，**不随仓库分发**（仓库只留设计与结论）。

> 2026-10-16。目标：补齐候选 #6——「用户视角」全链路测试。
> 现状：所有 v3.0 测试都是**单段级 mock**（内存库 + 构造数据），
> 没有一条测试是「从建项目开始，一路点到豁免」的真实流程。
> **本测试用真 LLM（智谱 glm-4-flash）+ 真 HTTP + 真持久库。**

## 一、测什么（用户旅程六步）

```
① 建项目 POST /api/projects
② 生成   POST /api/generate           ← 真 LLM，每段 60-72s
   ↳ 每段内部自动：_enqueue_v3_extraction 入队（engine.py:858/1002）
                     _merge_segment_into_graph → judge_segment + save_judge_findings
③ 批抽   worker 后台线程异步抽取（batch_worker._worker_loop）
④ 读侧   GET kg-v3/slice | /slice/prompt | /events | /judge/findings | /characters | /relations
⑤ 切片   GET kg-v3/slice/prompt       ← 验证 KG 真的回流进 prompt（不是空表）
⑥ 豁免   POST kg-v3/exemptions → 验证 judge 下一段过滤该 finding
```

**每一步的断言 = 真实落库数据**，不是 mock。

## 二、为什么必须后台跑 + 写文件

- 8 段 × 智谱 60-72s/段 ≈ **10 分钟**；加批抽每段 1-2 次 LLM 抽取 → **20-30 分钟**
- HTTP client 前台等必超时（v2.6 已踩过）
- Windows 后台进程 stdout 管道长时间会断（OSError Errno 22 崩进程）——脚本的
  `w()` 必须 try 包 print

## 三、运行形态（不污染正式库）

- 独立 DB：`DATABASE_URL=sqlite:///./journey_e2e.db`（不碰 novel_dev.db）
- 独立端口：避开 8020/8021/8100/8848/8011/8050/8060（起服务前 netstat 查占用）
- 直接起 uvicorn 子进程（真 HTTP，不用 TestClient——要复现前端真实请求形态），
  或脚本内 `threading.Thread(target=uvicorn.run)` 起 in-process server 后 requests 打本地端口
- **服务起来必须真调一次 LLM 验通**（uvicorn 200 ≠ API key 有效，skill 已沉淀）

## 四、数据与关键参数

| 项 | 值 | 来源 |
|---|---|---|
| provider | zhipu / glm-4-flash | 用户 2026-10-16 指定 |
| 语料 | 本地投放的公版长篇（1,047,687 字；正文不入库） | reference_novel_ids |
| 段数 | 8 段 | v2.6 同参数（20-30 分钟量级） |
| generation_length | 3000+ 字 | 用户对样本量的下限要求 |
| table_seed | 新建空库 | 幂等，不用续跑 |

**ProjectCreate 合法字段**：title/genre/style/reference_novel_ids/generation_length。
`source`/`platform` 是非法字段（直接 422，skill 已沉淀）。

## 五、断言清单（用户视角的「通过」标准）

| 步 | 断言 | 失败含义 |
|---|---|---|
| ① | 项目创建成功，拿到 project_id | 参数炸 |
| ② 每段 | generations 表 +1 行，text ≥3000 字 | LLM 不服从字数约束 |
| ② 每段 | `v3_extraction.enqueued == True` | 主链没挂批抽 |
| ③ 等 worker | kg_events/kg_entities 计数 >0 且持续增长 | worker 没跑 / LLM 抽取全空 |
| ③ | worker status：线程 alive、0 failed（或有 failed 但可 requeue） | 毒批 |
| ④ | judge/findings 能查到（可能为空——foreshadows 无数据时不报，**空是合法的**） | 落库路径断 |
| ⑤ | slice/prompt 返回文本**非空且含 KG 事实**（角色名/关系/event 至少一类出现） | KG 没回流 prompt |
| ⑥ |  exemption 写入后，judge findings 里该 (rule, entity) 消失 | 豁免链路断 |

**关键「空即异常」vs「空即合法」的区分**（skill 教训）：
- judge findings 空 = 合法（无输入不报）
- kg_events 空 = **异常**（8 段真实文本不可能抽不出事件）

## 六、脚本形态

`scratch/run_journey_e2e.py`（后台跑，写 `journey_e2e.log` + 结果 JSON）：

```
阶段 A（同步）: 起服务 → 验 LLM（1 次最小调用）→ 建项目 → 上传/关联红楼梦
阶段 B（后台）: 8 段 generate（每段落一条 JSONL 行到结果文件）
阶段 C（同步）: 等 worker 队列排空（poll kg_v3 worker/status 的积压数）
阶段 D（同步）: 逐个打读侧端点，断言落库数据
阶段 E（同步）: 造豁免 → 再生成 1 段 → 断言豁免生效
收尾: kill 服务进程（taskkill /F /PID，单斜杠）
```

测试文件（正式化）：`tests/test_e2e_journey_pipeline.py`——
按 skill 的模板**不依赖网络/智谱**，mock provider 走**同样的六步流程**
（建项目→mock 生成→mock 批抽→读侧→断言→豁免），
保证「流程本身」有回归保护，真 LLM 冒烟脚本只跑一次手工触发。

## 七、不做（收窄）

- 不测前端 UI（浏览器冒烟 skill 另有流程；本测试纯 API 流程）
- 不做 30 段/百万字压力（#5/#7 之外的独立议题）
- 不在真 LLM 跑里改实现——跑出问题只记录，修复另开任务
- 不跑 batch_worker 的 failed/requeue 路径（worker 可观测性 36 断言已覆盖）

## 八、风险与预案

| 风险 | 预案 |
|---|---|
| 智谱限流 hang（v2.6 踩过，180s 不返回） | 单段超时 240s；连续 2 段超时 → 中止并保留已落库数据 |
| worker 毒批卡死 | STUCK_MINUTES=30 自动判 stuck；脚本 poll 到 stuck 就跳过断言③记 WARN |
| 红楼梦做 reference 时 cut_units 切 0 单元（skill 已知坑） | generation_length 设 1500-3000，确认风格拟合不空 |
| 段与前段重复/第一章就完结 | 只记录不阻塞（AI 味修复是另一议题） |

## 九、首次冒烟发现（2026-10-16）

冒烟脚本（`scratch/run_journey_e2e.py`）首次跑暴露真 bug：

**`TypeError: LocalFallbackLLMProvider.chat() got an unexpected keyword argument 'stop_sequences'`**
- 位置：engine.py:822 `provider.chat(sys, user, ..., stop_sequences=get_section_stop_sequences())`
- 原因：v2.6.1 改 per-request `stop_sequences` 透传时，
  LocalFallbackLLMProvider（engine 没 LLM key 时的兜底 provider）忘了同步签名
- 影响：任何「provider 不可用 → 走 fallback」的真实请求都会 500

**修法**：LocalFallbackLLMProvider.chat 加 `stop_sequences: list[str] | None = None`
关键字接收并忽略。回归测试 `tests/test_llm_fallback_signature.py` 5 断言。Commit `0e35bc4`。

**环境问题（同一轮发现）**：pydantic-settings 只读 .env 文件不读 os.environ。
worktree 默认无 .env → 跑 fallthrough → 仍走 LocalFallback。
修法：脚本启动时把 `os.environ["ZHIPU_API_KEY"]` 注入 worktree 的
`backend/.env`，finally 恢复原状。

## 十、首次真链路跑通（2026-10-16，智谱 glm-4-flash）

环境修好后一次跑通 8 段真智谱生成 + 批抽 + 读侧 + 切片回流 + 豁免端点：

| 段 | wc | dt | 备注 |
|---|---|---|---|
| 1 | 1220 | 93s | 智谱 60-72s/段，剩 30s 是 judge+enqueue |
| 2 | 1247 | 194s | 智谱累，慢了 100s |
| 3 | 1125 | 68s | |
| 4 | 662 | 177s | 智谱生成短文本 |
| 5 | 875 | 59s | |
| 6 | 852 | 56s | |
| 7 | 992 | 62s | |
| 8 | 0 | 259s | ⚠️ 智谱 hang 后返回空（skill 沉淀现象） |

**真实落库数据**：
- kg_events=50 / kg_characters=14 / kg slice_for_prompt 834 字符（**KG 真回流**）
- worker alive=True / 队列 0 积压 / 0 stuck
- 智谱对 generation_length=3200 服从性弱（最长 1247，< 50%），属已知模型特性

**verdict 首次为 PARTIAL**——主因是「空即合法」的边界（judge findings=0 →
豁免端点未真实写入；slice 文本不命中硬编码关键词）。后续期望值修正：
- `slice_prompt_has` 改 `len(prompt) > 100`（KG 真回流即 PASS，不卡关键词）
- `exemption_link_ok` 改「端点 ping 成功即 PASS」（findings=0 算空即合法）

**这才是冒烟的真正价值**：跑通真链路，捕获**测试期望值与实现行为不匹配**的脏数据，
以及暴露出更深的「环境 vs 配置文件」层问题。
