# AI 小说生成器 · AI Novel Generator

[![CI](https://github.com/j77156057-art/ai-novel-generator/actions/workflows/ci.yml/badge.svg)](https://github.com/j77156057-art/ai-novel-generator/actions/workflows/ci.yml)

> **把「写一部长篇网文」拆成一条可验证的流水线。**
> 混合检索 + 知识图谱防漂移 + 有界前情 + 全局共享的文体蒸馏库 + 一个只提卡、不落库的助理 agent。
>
> A full-stack engine for long-form AI fiction: hybrid RAG retrieval, a knowledge-graph drift guard,
> bounded hierarchical context, a globally shared style-distillation library, and an assistant agent
> that may only propose — never write.

<p align="center">
  <b>FastAPI</b> · <b>Vue 3 + Vite + TypeScript</b> · <b>SQLAlchemy / PostgreSQL+pgvector / SQLite</b> ·
  <b>RRF 混合检索</b> · <b>知识图谱</b> · <b>Agent 工作流</b>
</p>

---

## 这个项目在解决什么

长篇网文真正的难点不是「让模型写一段」，而是**写到第 80 段时，这本书还是不是同一本书**：
写法有没有飘、伏笔有没有忘、人物有没有说出他不该知道的事、前情会不会把上下文撑爆。

这个项目把这些难点点名拆成部件，并且**给每个部件配一把能量化的尺子**：

| 难点 | 做法 |
|---|---|
| 写法会飘 | 参考书蒸馏成**文体档案**（17 维叙事 + 21 个可机检特征），每段注入同一份 |
| 前情会撑爆 | 三层摘要树（段 → 卷 → 主线）按预算裁剪，并**证明前情无洞** |
| 人物会失忆 | 知识图谱在生成前提醒「他是谁、这条伏笔还没收、剧情走到哪」 |
| 会说出不该知道的事 | 角色对话的每段证据标注「亲历 / 在场听到 / 只是被提到」，越界直接挡 |
| 改不动、改错了 | 助理 agent 只能**提卡**；落库走人批闸门，批了就留版本快照，可回滚 |

---

## ✨ 核心能力

- **文体蒸馏库** —— 一本书的写法蒸馏成一份档案（17 维叙事档案 + 21 个可机检文体特征），
  三档（完整 / 平衡 / 快速），落在**全局共享、幂等**的库里，不按项目重复蒸馏。
  档案里的每个数都能用同一把尺子复算；口径一变就作废旧档案重拟合，不让两套数并存。
- **有界前情** —— 段 → 卷 → 主线三层摘要树，按 token 预算裁剪。前情块必须连续覆盖第 1..N 段且无洞，
  而且**覆盖声明写在文本里** —— 外部核对器只读提示词就能算出有没有洞，不必相信模块自报的结果。
- **混合检索 + 知识图谱防漂移** —— 纯 Python BM25 × 向量 embedding，RRF 融合召回；
  知识图谱 v3（9 张表：实体 / 事件 / 关系 / 关系演化 / 伏笔 / 角色状态 / 知识条目 / 时间线因果链），
  段落提交后由后台 worker 用模型异步批抽，再经【世界观事实】块回流进下一段的提示词。
- **三条写作路径，三套可见性边界** —— 只学写法（原创）/ 接着这本书写（续写）/ 角色聊天，
  边界规则各不相同，猜错了就是剧透，或者白白挡住能用的素材。
- **角色** —— 档案、个性、角色卡与声纹、角色对话四块。声纹给的是**可执行锚点**
  （句长档位、句末习惯、自称、口头禅），不是「沉稳寡言」这种形容词；
  对话的每段证据带来源标注，认不出角色在场就往保守那档判 —— 宁可漏判，不误判。
- **世界设定 / 体系与量级 / 伏笔账本** —— 世界观开书前写定，设定与走向分两条通道；
  修炼体系三型（细分 / 序列 / 粗放）正交于档位，粗放型不假设等差，量级按「能造成什么后果」判；
  伏笔的回收章必须有正文证据，编造的不入账。
- **生成工序链** —— `生成 → 段长闸门 → 场景契约审计 → 去重 → AI 味重写 → 文风改写 → 形态校正 → 摘要/KG → 落库`。
  同一把尺量落库的那份，界面上显示的和落库的是同一份。
- **助理 agent，只提卡不落库** —— 28 个工具**全部 `read_only=True`**，注册时硬拒非只读工具；
  要改数据只有一条路：构造**待确认卡**，人点了才执行，执行留版本快照、可回滚。
- **四轴验收 + 盲评包** —— 离线跑验闭环与记忆系统，真模型跑验文风与漂移；
  盲评包出包前先过十道闸门（两侧同等脱敏、题材与年代一致、取样口径对称、题面级专名复查……），
  任一条不成立，判定脚本就拒绝出统计结论。

---

## 🖼 界面

**项目工作台** —— 创作三板块（原创 / 续写 / 角色）、七步工序、生成控制台。
角色个性里的「说话」「会」两条，是从已经写出来的正文里**反推**的，不是手填的。

![项目工作台](docs/images/workspace.png)

**生成的正文** —— 同一页往下滚就是第 2、3 段，界面显示的就是落库的那一份。

![生成的正文](docs/images/workspace_body2.png)

**知识图谱工作台** —— 9 张表实时读侧：切片预览、人物情报、事件时间线、Judge 一致性告警、关系与节奏。
右边的伏笔账本逐条标着「埋在哪一章」，回收章没有正文证据就不认。

![知识图谱工作台](docs/images/kg.png)

**助理工作台** —— 11 步工作流进度、确定性诊断结果，以及一条硬边界：
**助手只提卡，不落库**，需要写操作时它在回答里给一张待确认卡，你点「确认」才会执行。

![助理工作台](docs/images/assistant.png)

---

## 🧪 样例：输入 → 输出

仓库自带一份**自写的**示例语料（`data/demo/示例语料-断铃.txt`，10 章 / 10,443 字，
人物与情节全部虚构，没有版权问题），所以这一节是可以自己复现的。

**输入 · 示例语料的写法**（节选）：

> 雪下了三天，驿道就断了三天。
>
> 沈聿蹲在塌了半边的箭亭底下，用刀尖把火堆里的湿柴拨到一边。火苗缩成一小团，勉强照亮他手边那口铜锅。锅里煮的是半把粟米，水面上浮着草屑，他用木勺搅了两圈，没舀。

**输出 · 同一套流水线生成的正文**（项目「示例项目·雪城磨刀」第 1 段，节选）：

> 雪落在棚顶的油布上，声音很轻，像有人拿指甲在远处刮纸。
>
> 东市的摊子散得差不多了。卖汤饼的老周把炉灰扒出来，泼了半瓢水，白气腾起来，顺着风斜斜地往南走。燕回把磨凳翻过来，凳腿朝上，刮掉嵌在缝里的铁屑。他做这一手很慢，一根一根缝地刮。

参考书提供的是**写法**，不是情节和句子。对照两段能看出来的习惯：

| 写法 | 参考语料 | 系统产出 |
|---|---|---|
| 对白归属跟在引语后 | `「柴是湿的。」沈聿说。` | `「收摊了。」燕回说。` —— 同一形态 |
| 引语怎么落段 | 多数单独成段，其余是「引语＋X说」同段 | 同样是这两种形态 |
| 不写内心独白 | 写动作和神态，不写「他心中一凛」 | 情绪直陈密度 **0**，与参考书一致 |
| 段尾不升华 | 段落落在动作或对话上 | 段尾升华率 **≈ 0**，与参考书一致 |
| 用具体物件替代形容 | 铁屑、磨石、铜锅、铜钱 | 铁屑、磨石、锈末、铜钱 |
| 少用强情绪标点 | 几乎不用感叹号 | 感叹号 **0**、省略号 **0**，与参考书一致 |

机器侧也量了一遍：产出相对这份语料的 21 项文体特征，**9 项落在它的分布带内**；
对白字占比、纯台词段占比、句号密度明显偏高。
这份语料只有 1 万字，分布桶里样本太少、带很窄 —— 换一本 5 万字以上的长篇语料会稳得多。
（完整口径见 `docs/style-metrics-guide.md`。）

---

## 🚀 快速开始

零配置起步：复制 `.env` 就能跑，模型 key 留空会走本地确定性兜底。

```bash
# 后端（默认 SQLite，复制 .env 就能跑）
# 要 PostgreSQL + pgvector：docker compose up -d && pip install -r requirements-postgres.txt
#   并在 .env 里设 DATABASE_URL=postgresql+psycopg2://novel:novel@localhost:5432/novel
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows；macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
uvicorn app.main:app --reload --port 8000

# embedding / llm key 填在 backend/.env；留空 => 本地确定性兜底

# 前端
cd frontend && npm install && npm run dev    # http://localhost:5173 ，/api 代理到 :8000
```

**跑测试**：

```bash
cd backend
pip install -r requirements-dev.txt     # 额外的只有 pytest
cd ..
python -m pytest tests/                 # 1,956 项用例（+ 8 项需外部语料/验收库才跳）
```

> 结尾会有一行 `1948 passed, 8 skipped in ...` —— **别再给这条命令加 `-q`**：
> `pytest.ini` 的 `addopts` 里已经有 `-q`，两个叠起来变成 `-qq`，
> pytest 会**静默吞掉那行汇总**。CI 跑的就是这一条命令。

**先跑一遍自带示例**（不用自己找书）：

```bash
cd backend
python load_corpus.py --only ../data/demo/示例语料-断铃.txt
```

然后在界面上：建项目 → 选这份语料作参考 → 文风蒸馏 → 生成。

**想跑自己的**（三步）：

1. 找一本你有权使用的 `.txt`（UTF-8；带 `第X章` 分章最好），放进 `data/seed/`，
   或用 `NOVEL_DIR` 指向别处；
2. `cd backend && python load_corpus.py` 入库（自动切分 + 抽知识图谱）；
3. 界面上建项目 → 选参考书 → 走文风蒸馏 → 生成。

详见 `data/seed/README-投放真实语料.md`。

主要端点（完整清单在 `docs/`）：

```bash
curl -X POST http://localhost:8000/api/novels/upload -F "file=@小说.txt"   # 导入语料
curl "http://localhost:8000/api/novels/{id}/search?q=林夜&k=5"              # 混合检索
curl -X POST http://localhost:8000/api/projects \
  -H 'Content-Type: application/json' \
  -d '{"title":"我的小说","mode":"quick","target_length":5000}'             # 建项目
curl -X POST "http://localhost:8000/api/projects/{pid}/build-kg?novel_id={id}"   # 知识图谱
curl -X POST http://localhost:8000/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"project_id":1,"novel_ids":[1],"max_chars":2400}'                    # 写一段
curl -X POST http://localhost:8000/api/assistant/proposals                  # 提案闸门
```

---

## 🧭 架构

```mermaid
flowchart TD
    Corpus[语料 Corpus · txt] -->|UTF-8 解析 + 自动分章| Chunks[叙事切分 Chunks]
    Chunks -->|BM25 + 向量 embedding| Store[(PG + pgvector · SQLite)]
    Chunks -->|规则抽取 角色/坑/关系| KG[(知识图谱 KG)]
    Corpus -->|文风蒸馏 三档| Lib[(全局蒸馏库 StyleDistill)]
    KG -->|生成前提醒 reminders| Gen[分段生成环 Generation Loop]
    Store -->|RRF 融合召回 + 原文边界| Gen
    Lib -->|档案 + 规则 + 写法样例| Gen
    Gen -->|段级总结| Tree[层级摘要树 Summary Tree]
    Tree -->|有界前情 compose_prior + 覆盖证明| Gen
    Gen -->|Writer 续写| LLM1[Writer Model]
    Gen -->|诊断 / 提卡| AG[助理 Agent · 28 工具]
    AG -->|提案 Proposal| Gate[提案闸门 人批]
    Gate -->|批准| Gen
    Gate -->|版本快照| Ver[(Versions · diff · restore)]
```

**生成数据流**：`KG reminders` + 用户 **plot-bible** → 合并约束（唯一权威口径，用两次：
静态 system 层 + 近因 user 提醒）→ `summary_tree.compose_prior` 组装**有界前情**
（主线 / 近情 / 卷）→ RAG 上下文（含原文边界裁剪）→ LLM 写 → 段级摘要 → 写进 `generations`
并卷进摘要树（L1 → 按需 L2/L3）→ 下一段读树。

---

## 🛠 技术栈

| Layer | Choice |
|---|---|
| Backend | **FastAPI** (Python 3.13), SQLAlchemy 2, Pydantic v2 (Strict) |
| Storage | PostgreSQL + **pgvector** (prod) · SQLite (dev/test) |
| Retrieval | Pure-Python **BM25 × dense-vector RRF fusion** |
| Knowledge graph | Deterministic rule extractor + LLM batch extraction (async, same interface) |
| Generation | Bounded-prior engine + segment summary chain + 工序链 |
| Style | **Globally-shared distillation library**, 3 tiers, idempotent upsert |
| Frontend | **Vue 3 + Vite + TypeScript**, semantic design tokens (WCAG AA) |
| Agents | Writer/Assistant role isolation, `ProviderRegistry` two-track, proposal→approval→commit/rollback |
| LLM providers | DashScope · OpenRouter · MiniMax · OpenAI-compatible · Local deterministic fallback |

> **依赖只用 pip 就能装上**：`requirements.txt` 里全是纯 wheel（不需要 C 编译器、
> 不需要 PostgreSQL 开发环境），PG 驱动单独放在 `requirements-postgres.txt` ——
> `psycopg2-binary==2.9.9` 没有 Python 3.13 的预编译轮子，放进主依赖会让 pip 退回源码编译、
> 去要本机 `pg_config`，空白机器上必然卡死。

---

## 📦 仓库结构

| 目录 | 内容 |
|---|---|
| `backend/app/` | 后端全部实现：混合检索 / 知识图谱 / 生成环 / 文体蒸馏 / 助理 agent |
| `frontend/` | Vue 3 + Vite 前端：创作三板块、助理工作台、模型设置 |
| `tests/` | **1,956** 项 pytest 用例（单源真源，`pytest tests/`） |
| `frontend/tests/` | 前端测试：10 个纯逻辑文件（50 项，`node --test`）+ 4 个组件级文件（33 项，vitest + jsdom） |
| `docs/` | 74 份设计文档与指南：架构怎么定的、判据为什么这么定 |
| `docs/images/` | README 用的界面截图 |
| `skills/` | 可复用的**方法单元**（agent skill）：盲评包脱敏与防泄露（SKILL.md + 事故复盘 + 泄露扫描脚本） |
| `ENGINEERING.md` | 工程纪律：每一条规矩背后都是一次真实踩坑 |
| `data/demo/` | 自写的示例语料（无版权，可直接入库复现 README 的样例） |
| `data/seed/` | 语料**投放区**（空目录 + 投放说明） |

**刻意不放进仓库的东西**（有意为之，不是遗漏）：

- **参考书原文与语料** —— 版权正文不进 git，只在本机投放；
- **模型跑出来的验收产物** —— 含原文片段的中间 JSON 不随仓库分发；
- **本机绝对路径** —— 全部改成相对路径或环境变量，换台机器 clone 就能跑；
- **任何真实作品的书名与人物名** —— 文中出现的书名与人物**全部是自造的示例**，
  不指向任何真实作品或出版书（详见 `ENGINEERING.md`「发布卫生」）。
- **早期提交里的原始语料书名** —— 当前版本已全部中性化，但 **git 历史没有重写**：
  早期提交仍保留着最初用于文风蒸馏的那本语料的书名与人物名。这是**有意的取舍** ——
  提交历史本身就是这个项目的过程证据，为了名字干净而重写历史，等于把证据也一起抹掉；
  而语料正文任何时候都不在仓库里，历史里也只有名字，没有正文。

---

## 🔬 工程保证

这个项目把**证据**当交付物。下面每一条都能被第三方复算、重跑或查证：

- **可复算的量** —— 档案里的每个数都能用同一套尺子重算；口径变更会作废旧档案并重新拟合，
  而不是让两套数并存。
- **覆盖不变量自带证明** —— 覆盖声明写进前情文本本身，外部核对器只读提示词就能算出有没有洞，
  不必相信模块自报的 `uncovered`。
- **边界有测试钉着** —— `tests/test_assistant_readonly.py`（助理只读）、
  `tests/test_reference_boundary.py`（原文边界）、`tests/test_action_proposals.py`（提案闸门）、
  `tests/test_workflow_steps.py`（工作流）；另有深度攻击与真实浏览器端到端两套对抗性验证。
- **四轴验收能真跑** —— 离线跑（无模型，验闭环与记忆系统）与真模型跑（验文风与漂移的机器侧证据）
  用**同一把尺、同一份种子设定**；阈值取自真实语料的分位数，不是拍的。
  真跑还会产出盲评材料（题面 / 密封映射 / 记录表）与二项检验判定脚本，让人评那一段也能算成判定。
  真跑抓到过单测与 CI 全绿也抓不到的缺陷（每第 10 段生成必崩 / 路线规划端点参数不匹配 / 心理维度渲染未定义名）。
- **空白机自检** —— 仓库**已发布的那棵树**（`git archive HEAD` 导到干净目录）真跑过一遍：
  新建 venv → `pip install -r backend/requirements.txt` → `pytest tests/` 全绿 →
  照 README 复制 `.env` → 空 SQLite 起 uvicorn → `/openapi.json` 与 `/api/novels`
  真打 HTTP 200 → 前端 `npm ci && npm run build` 产出 dist（零 key、零配置、零手工干预）。
- **CI 在 Linux 上跑** —— `.github/workflows/ci.yml`：每次 push / PR 跑**后端全量 pytest** 与
  **前端测试 + 构建**（`npm test` + `npm run build`，后者自带 `vue-tsc` 类型检查）。
  跑的就是 README「跑测试」那一节写的命令 —— 不给 CI 另写一套更严格的检查。
- **文档里的数字被机器守着** —— `tests/test_readme_test_count.py`：
  README 写的用例数必须等于 `pytest` **实际收集到的**条数，且 README 里几处写法必须彼此一致
  ⇒ 改了测试忘了改 README，这条测试就红。

设计取舍与踩坑记录在 `ENGINEERING.md`，逐个能力的判据写在 `docs/`。

---

## ⚖️ 已知限制

下面几条是**有意为之的取舍**，说清楚比藏着好：

- **产出离参考书还有距离** —— 风格迁移能看出来，但逐维量下来，句长、对白占比、纯台词段占比
  仍明显偏离参考书的分布带；参考语料越大、蒸馏越充分，收得越紧。这是当前最实在的短板。
- **提示词覆写只增不减** —— 三个追加槽能加说明，改不了代码里的维度级行为；
  覆写也删不掉默认块里的角色锁定与伏笔纪律。
- **候选池默认关** —— 同一提示采多份、每份走完整条链再量成品，收益是「落库的那份一定被尺子判过」，
  代价是最坏约 17 倍耗时。默认路径已有形态校正与引导语补写兜底，所以默认关。
- **机械形态校正改不动本质** —— 它只改段落边界（一个字都不新增），
  实测把纯台词段占比从 71.5% 拉到 42.2%（参考书 22.7%）；
  剩下的差距要靠「引导语补写」（调模型补一句动作/神态，**会新增文字**），补写后才进带。
- **混书检测的阈值是取舍** —— 判据要求外书占全书章数 ≤ 25%（超过就不是「一本书里混了几章」，
  而是「这本书本身就是拼的」）。这组阈值放宽会让候选膨胀、真阳性反而检不出来。
- **盲评包要「同题材、同档位」才作数** —— 工具会自己检查两侧是否同年代、版式与脱敏痕迹是否对称、
  题面里还残留没遮掉的专名、每一题两侧是不是同题材；不合格的包直接标成「不是盲评包」，
  判定脚本不出统计结论。两侧档位不同（拿出版级作品当人类侧）时，差值**不归因于被测物**。
- **不含版权正文** —— 参考书原文与模型跑出来的验收产物被 git 忽略，语料只在本机投放
  （见 `data/seed/README-投放真实语料.md`）。
