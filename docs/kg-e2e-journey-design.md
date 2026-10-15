# e2e user journey 测试设计（kg-e2e-user-journey）

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
| 语料 | 红楼梦 (data/seed/红楼梦.txt, 1047687 字) | reference_novel_ids |
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

测试文件（正式化）：`backend/tests/test_e2e_journey_pipeline.py`——
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
