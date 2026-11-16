# 语料摄取「无渠道」实跑结论：corpus_ingest.py 4 处维持 B

日期：2026-11-17
范围：`backend/app/kg/corpus_ingest.py` 的 4 处 `if provider is None`（`ingest_novel:606`、`retry_failed_split:708`、`fill_gaps:799`、`backfill_targeted:907`）
性质：只读验证，未改任何代码，报告不提交。

## 1. 配置实核（先打印，再动手）

临时目录 `D:\Temp\kg-ingest-check-20261117-031419`，以 **CWD=临时目录 + PYTHONPATH=backend** 启动，
`backend/.env` 不参与加载（`Settings` 的 `env_file=".env"` 相对 CWD，`config.py:33-34`）。

第一轮发现机器级环境变量 `MINIMAX_API_KEY` 存在（`legacy key 是否为空: minimax_api_key=False`）。
虽然 distiller 配置的是 zhipu、用不到它，为满足「真的没有 key」的要求，**清掉该环境变量后完整重跑了一遍**，以下为第二轮（严格）结果：

```
cwd: D:\Temp\kg-ingest-check-20261117-031419
backend/.env 是否存在于 cwd: False
database_url: sqlite:///D:/Temp/kg-ingest-check-20261117-031419/kgcheck-strict.db
llm_provider: dashscope
distiller_provider: zhipu
legacy key 是否为空: zhipu=True openrouter=True siliconflow=True minimax=True openai=True dashscope=True
进程环境变量是否存在: 6 个 legacy key + DISTILLER/WRITER/ASSISTANT_API_KEY 全部 False
role=writer: provider='' api_key_empty=True
role=assistant: provider='zhipu' api_key_empty=True model='glm-4-flash'
role=distiller: provider='zhipu' api_key_empty=True model='glm-4-flash'
get_distiller_provider -> LocalFallbackLLMProvider is_local_fallback=True
get_llm_provider      -> LocalFallbackLLMProvider is_local_fallback=True
```

即：**没有任何可用 key，两个 provider 都是本地模板兜底**。无网络调用——整批 4 个窗口耗时 0.065s，窗口错误文本里是模板自己的「【离线兜底·确定性续写】」，不是远端返回。

## 2. 整批失败的实际表现

种子语料：1 本书 12 章（每章 186 字），3 章/窗 → 4 个窗口。

```
ingest_novel 返回: {"windows":4,"done":0,"failed":4,"entities":0,"relations":0,
                    "events":0,"foreshadows":0,"elapsed_sec":0.0}
墙钟耗时: 0.065s
```

CLI 等价命令（`python -m app.kg.corpus_ingest --novel-id 1 --project-id 1`）：

```
INFO novel=1 章=12 窗口=4 新登记=0 本轮待跑=4
INFO novel=1 进度 4/4 done=0 failed=4 事件=0 实体=0 5084.4 窗/分钟
{"windows": 4, "done": 0, "failed": 4, "entities": 0, "relations": 0, "events": 0, "foreshadows": 0, "elapsed_sec": 0.0}
```

其余三个模式的实测输出（各自独立临时库副本）：

| 模式 | 返回 |
| --- | --- |
| `--retry-failed-split` | `{"windows":4,"chapters":12,"ok":0,"failed":12,...}` |
| `--fill-gaps` | `{"chapters":12,"ok":0,"failed":24,"still_empty":0,"events":0,"rounds":2,"remaining_gaps":12,...}` |
| `--targeted` | `{"windows":4,"ok":0,"failed":4,"entities":0,"relations":0}` |

四种入口都做到了「失败计数非零」，没有一种把模板产出当成成功。

## 3. 数据库真实状态

主跑（`kgcheck-strict.db`）：

```
窗口状态分布: {"failed": 4}
1:1-3   failed attempts=1 n_entities=0 n_events=0
  error: parse_failed: 无法从模型输出解析出 JSON（前 80 字：'【离线兜底·确定性续写】已锁定角色：（暂无锁定角色）。待解悬念：（暂无未填坑）。…'）
1:4-6   failed attempts=1 同上
1:7-9   failed attempts=1 同上
1:10-12 failed attempts=1 同上

kg_entities=0  kg_events=0  kg_mentions=0
实体名含兜底标记的脏数据: 0
coverage: {"total_chapters":12,"chapters_with_events":0,"max_event_chapter":null,"events":0}
```

补充观察（`--retry-failed-split` 的库）：窗口 error 被改写为 `拆章后仍缺章：[1, 2, 3]`，**原始原因（离线兜底）被覆盖**，但状态仍是 `failed`、实体/事件仍为 0。

模板输出没有进入任何写入路径：解析阶段就被 `parse_failed` 拦下（`extractor_v3.py:827-833`），`persist_window` 未执行。

## 4. 界面实际显示（截图见 §7）

页面 `http://localhost:5274/projects/1/kg`（后端 8102，临时库）：

| 标签页 | 实际文案 |
| --- | --- |
| 切片预览（默认） | `近期出场实体 0` / **`窗口内无实体`**、`未回收伏笔 0` / `无未回收伏笔`、`近期事件 0` / **`窗口内无事件`**、`（空切片）` |
| 人物情报 | `角色 0` / **`项目里还没有角色（先生成几段让批抽数据进来）`** / `点左侧角色查看情报` |
| 事件时间线 | `0 事件 · 0 因果链` / **`还没有事件（生成后批抽自动落库）`** |
| Worker 状态 | `success 0 · failed 0 · running 0 · stuck 0 · empty 0`、线程 `alive`、**`还没有 AgentRun 记录（先生成几段让批抽落库）`** |

四个页面都**没有**出现「N 个窗口失败」「渠道不可用」「摄取失败」等字样。
原因：`KgCorpusWindow` 只有写侧（`models.py:573`、`corpus_ingest.py`），**没有任何读侧 API / 前端入口**；Worker 状态页读的是 `AgentRun`，与语料窗口无关。

## 5. 判定

**4 处全部维持 B（可保留），不升 A。**

依据是**数据状态**，不是界面文案：

1. 模板兜底的输出没有变成真实结果——`extract_batch` 解析失败 → 窗口置 `failed`，实体/事件/提及三张表都是 0，没有任何「假实体」写库、也没有进入提示词或分析结论；
2. 失败在数据层可见且可归因：窗口 `status=failed`、`error` 里直接带着模板的自标识「【离线兜底·确定性续写】」，CLI 也打印 `failed=4`；
3. 未满足 A 的定义（「本地模板兜底的输出被当成真实结果消费」）：真正的兜底产物连解析都过不去，属于「跑不成」，不属于「跑成了假东西」。

界面确实会让人误读成「本书没实体/没事件」，但这个误读**不由这 4 处决定**：把 4 处改成渠道守卫，KG 页面仍是同一套空态，因为窗口表根本没有读侧出口。它属于另一个问题（读侧缺窗口状态展示），已单独记录，不在本结论里改。

### 若将来要动（当前不动手）

一句话最小改法：4 个入口统一加 `if provider is None or isinstance(provider, LocalFallbackLLMProvider): raise RuntimeError("渠道不可用：未配置 API Key，请到「模型设置」选择渠道")`，让 CLI 立刻失败并说清原因，而不是每窗跑一遍 parse_failed。改动收益是「错误信息更准、少跑 N 次无效调用」，不改变落库与界面行为，故仍不构成 A 类。

## 6. 环境与清理

- 临时后端 `127.0.0.1:8102`、前端 `localhost:5274` 均已停止，两个端口无监听；
- 临时库共 6 个（`kgcheck.db`、`kgcheck-strict.db`、`kgcheck-cli.db`、`kgcheck-split.db`、`kgcheck-fill.db`、`kgcheck-targeted.db`）与脚本保留在 `D:\Temp\kg-ingest-check-20261117-031419`；
- 生产库 `backend/novel_dev.db`：自检前后 **size 均为 434,814,976 字节**；mtime 由 03:14:09 变为 03:17:40，来自另一会话常驻的 8000 端口 uvicorn（PID 17856），非本次自检写入。只读复核：`projects=7`、本项目临时标题命中 `0`、`raw_novels=6`。

## 7. 截图路径

- `D:\Temp\kg-ingest-check-20261117-031419\shots\kg-01-default.jpg`（切片预览）
- `D:\Temp\kg-ingest-check-20261117-031419\shots\kg-02-people.jpg`（人物情报）
- `D:\Temp\kg-ingest-check-20261117-031419\shots\kg-03-timeline.jpg`（事件时间线）
- `D:\Temp\kg-ingest-check-20261117-031419\shots\kg-04-worker.jpg`（Worker 状态）
