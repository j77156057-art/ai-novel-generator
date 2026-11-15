# 外部素材检索工具（brief）配置说明

模型可自主调用的 `search_external_material` 工具。只在初稿阶段暴露，重写轮不开放；
**默认关闭**，关掉时不联网、不读缓存、不写轨迹。

代码位置：`backend/app/retrieval/brief/`

- `settings.py`：配置项与默认值
- `backend.py`：`FreeSearchBackend`（HTTP 直连：百度主路 + DuckDuckGo 回退）、`JSONSearchBackend`（付费供应商）
- `browser.py`：`BrowserSearchBackend`（无头模式：Playwright 渲染 `cn.bing.com` 结果页，DOM 取结果）
- `cache.py`：磁盘缓存（版本 + TTL + 近义匹配）
- `library.py`：素材库（入库时间、来源查询、容量淘汰）
- `render.py`：素材块渲染与 `clean_text` 清洗
- `tool.py`：调用入口、预算、重试、留痕、两阶段工具

## 后端类型

| `BRIEF_TOOL_BACKEND` | 行为 |
| --- | --- |
| `auto`（默认） | 配了 `BRIEF_TOOL_SEARCH_URL` 走 JSON 后端，否则走免费后端 |
| `free` | 强制免费后端：HTTP 直连走百度结果页优先、失败/被拦回退 DuckDuckGo html 端点；`HEADLESS=true` 时换无头浏览器实现（只走 Bing） |
| `json` | 强制 JSON 后端：`POST {SEARCH_URL}`，body `{"query","count"}`，可选 Bearer key |

免费后端只抓公开结果页：单线程、进程级最小请求间隔、不并发、不绕验证码、
不伪造登录态。识别到验证码页、跳转验证（`wappass` / `captcha` / `verify`）、
或 HTTP 200 但解析不出结果时，归为 `blocked`，降级为空素材并停止重试。

## 无头浏览器模式（`BRIEF_TOOL_HEADLESS=true`）

百度 HTTP 直连会被 302 到验证码页（`wappass.baidu.com/.../tuxing_v2.html`），
5 个真实业务查询全部 `blocked`。无头渲染对百度同样无效（headless-shell 与完整
Chromium 结果一致），**百度这条路已收掉**，不再做任何绕验证码/指纹伪装的尝试。
无头模式只走 `cn.bing.com`，结果页渲染后从 DOM 取标题/摘要/链接，不再用正则啃原始 HTML。

边界与实测：

- 是 `FreeSearchBackend` 的另一种实现，`SearchBackend` 协议与调用方不改；
- 浏览器实例懒启动、**跨查询复用**，单线程，沿用 `BRIEF_TOOL_MIN_INTERVAL_S`；
- 被拦、超时、DOM 解析为空一律降级为空串，`status` 记 `blocked` 或 `error`，不抛错；
- 代理、登录态、验证码一律不碰；抓不到就如实报抓不到。

实测（2026-11-15，本机 cn.bing.com，3 个业务查询串行、间隔 2s）：
3/3 查询拿到结果页，原始条数 3/3/3，过滤后可用 2/1/2 条，摘要可读。

依赖（均为**可选依赖**，不进 `requirements.txt`）：

```powershell
pip install playwright
python -m playwright install chromium
```

体积实测与预估差距较大，如实记录：

| 项 | 实际 | 备注 |
| --- | --- | --- |
| `playwright` wheel | 42.7MB | 原预估 3–5MB |
| Chromium 完整版 | 452MB | 落在用户级缓存（`%LOCALAPPDATA%\ms-playwright`） |
| Chromium headless shell | 287MB | 同上，二者合计约 739MB |
| 新增占盘合计 | 约 854MB | 原预估 150–180MB |

未安装 playwright 时，`_brief_backend_ready()` 返回 `no_playwright`，工具直接不可见，
不会每轮给模型一个只会报错的死工具。

## 配置项

全部通过环境变量或 `backend/.env` 提供，前缀 `BRIEF_TOOL_`；进程环境变量优先。
`.env` 由 `BriefSettings.from_env()` 自行读取（只取该前缀），读不到或格式不对时
静默回落默认值，不报错。

### 本轮新增

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BRIEF_TOOL_BACKEND` | `auto` | 后端类型：`auto` / `free` / `json`，非法值回落 `auto` |
| `BRIEF_TOOL_MIN_INTERVAL_S` | `1.5` | 免费后端全局最小请求间隔（秒）。实例内再兜底 `max(1.0, ...)`，配置更小也不会更密。无头 Bing 实测按 2s 间隔串行可用 |
| `BRIEF_TOOL_USER_AGENT` | 桌面 Chrome UA | 免费后端请求 UA。待验证：未做 UA 对比 |
| `BRIEF_TOOL_ALLOW_FALLBACK` | `true` | 百度失败/被拦时是否回退 DuckDuckGo |
| `BRIEF_TOOL_HEADLESS` | `false` | 免费后端是否改用无头浏览器（Playwright + Chromium，只走 Bing）。默认关：它是可选依赖，不能拖累默认路径 |

### 素材库

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BRIEF_TOOL_LIBRARY_DIR` | 空 | 素材库目录；留空取 `CACHE_DIR` 同级的 `brief_library`（即 `data/brief_library`，已 gitignore） |
| `BRIEF_TOOL_LIBRARY_TTL_S` | `5184000`（60 天） | 未使用素材的存活期；`0` 表示不按时间淘汰。**已使用过的条目不受 TTL 约束** |
| `BRIEF_TOOL_LIBRARY_MAX_ITEMS` | `200` | 库内条数上限，超限即淘汰。待验证：未按真实库规模测过 |
| `BRIEF_TOOL_LIBRARY_CANDIDATES` | `5` | 每次检索后交给模型的候选条数上限（只给概要） |
| `BRIEF_TOOL_ADOPT_OVERLAP` | `0.2` | 判定「正文采纳」的词面重合阈值；只做粗判，宁可低估 |

### 已有

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BRIEF_TOOL_ENABLED` | `false` | 总开关 |
| `BRIEF_TOOL_BRIEF_VERSION` | `v1` | 参与缓存键；改清洗规则时递增即整体失效 |
| `BRIEF_TOOL_CACHE_DIR` | `data/brief_cache` | 相对后端进程 cwd；已被 `.gitignore` 排除 |
| `BRIEF_TOOL_SEARCH_URL` | 空 | JSON 后端的地址；空表示不可用 |
| `BRIEF_TOOL_SEARCH_API_KEY` | 空 | JSON 后端的 key |
| `BRIEF_TOOL_MAX_CALLS` | `3` | 每章调用次数上限 |
| `BRIEF_TOOL_TIMEOUT_S` | `3.0` | 单次检索超时（两类后端共用） |
| `BRIEF_TOOL_CHAPTER_BUDGET_S` | `10.0` | 本章检索总预算 |
| `BRIEF_TOOL_MAX_RESULTS` | `3` | 单次最多返回条数 |
| `BRIEF_TOOL_MAX_QUERY_CHARS` | `80` | 查询词截断长度 |
| `BRIEF_TOOL_MAX_BLOCK_CHARS` | `1200` | 注入上下文的素材块字数上限 |
| `BRIEF_TOOL_MIN_SNIPPET_CHARS` | `16` | 摘要短于此值就丢弃（只剩标题/栏目名）；待验证，未做标注集校准 |
| `BRIEF_TOOL_RETRIES` | `2` | 单次检索重试次数（`blocked` 不重试） |
| `BRIEF_TOOL_BACKOFF_BASE_S` | `1.0` | 重试退避基数 |
| `BRIEF_TOOL_BACKOFF_CAP_S` | `8.0` | 重试退避封顶 |
| `BRIEF_TOOL_CACHE_SIMILARITY` | `0.9` | 近义阈值**下限**；实际阈值取 `max(本值, 长度自适应值)`，见下节 |
| `BRIEF_TOOL_CACHE_SCAN_LIMIT` | `200` | 近义匹配最多扫描条数 |
| `BRIEF_TOOL_CACHE_TTL_S` | `2592000`（30 天） | 缓存有效期；`0` 表示永不过期 |
| `BRIEF_TOOL_EMPTY_TEXT` | 空串 | 无素材时的返回值 |

## 素材库（两阶段：先入库，再由模型挑）

检索结果不再直接拼进初稿 prompt，而是走「落库 → 候选 → 选择」三步：

1. `search_external_material(query)`：过滤后的条目**先写入素材库**，每条带
   入库时间戳、来源查询、原始标题与摘要（按内容哈希去重，同一内容只保留首次入库）。
   返回的是**候选清单**，每条只列 `id`、入库天数、来源查询、标题与 ≤40 字摘要截断
   （`CANDIDATE_DIGEST_CHARS`），不含摘要正文。只给标题分不出广告/聚合页，摘要给到
   40 字足够判断内容方向，又不占用正文上下文。
2. 模型调用 `select_external_material(ids)` 指定要用的条目；只有被选中的条目
   才会以素材块（带「外部资料 · 仅作素材」块头）注入 prompt。
3. 本章没选中的条目继续留在库里；后续章节可以直接选，也可以先补充检索再挑。
   不需要素材的章节可以一条都不选、一次都不查。

两条路径一致：联网抓取与缓存命中都会「先入库、再给候选」，不存在绕过素材库
直接进正文的通道。

### 链路各环由谁触发、留痕落在哪

| 环节 | 触发方 | 留痕位置 |
| --- | --- | --- |
| 是否检索、检索什么 | 模型自行决定（`search_external_material`，初稿素材轮才可见） | `retrieval_logs.query` / `status` / `latency_ms`；`soft_flags.brief_cached` / `brief_similarity` / `brief_filter` |
| 入库 | `BriefTool._call` → `MaterialLibrary.add` | `retrieval_logs.extra.library = {added,candidates,size}`；同值落 `soft_flags.brief_candidates` |
| 候选清单回到模型 | 工具结果消息（role=`tool`），含 id/入库天数/标题/≤40 字摘要 | 同上（候选条数） |
| 选哪几条 | 模型调用 `select_external_material`；若查完直接停，`engine._ask_brief_selection` 补一轮「只给选择工具」 | `retrieval_logs.extra = {selected,requested,injected,ids}`；`soft_flags.brief_injected` |
| 注入正文 | `select()` 把选中条目渲染成素材块，`engine._material_suffix` 拼到初稿 prompt | 注入字数记在 `returned_chars` |
| 正文是否采纳 | 章末 `engine` 用**最终正文**调 `BriefTool.note_chapter_text()` | `soft_flags.brief_adopted` / `brief_adopt_ratio` |

模型一次都不调用时，不联网、不写轨迹、不动素材库；这条路径与接线前的提示逐字相同。

### 检索预算的口径

`BRIEF_TOOL_CHAPTER_BUDGET_S` **只累计检索本身**（网络请求 + 退避），
不计模型决策耗时。曾经按「start_chapter 起的墙钟」计算，慢模型首轮 12.7s
就把 10s 预算吃光，导致 8 次检索全部 `budget_exhausted`、一条素材都拿不到；
改为累计检索耗时后同一场景下 3 次检索全部成功。

### 容量与淘汰

- 未使用条目（`use_count = 0`）超过 `LIBRARY_TTL_S` 后被淘汰；**已使用过的条目不按 TTL 淘汰**。
- 总条数超过 `LIBRARY_MAX_ITEMS` 时，按
  `(是否使用过, 最近使用时间, 入库时间)` 升序淘汰——先丢从未用过的，再丢最久没用的，
  同组内先丢入库最旧的。已用条目因此不会因为「新料多」被整体挤掉。
- 淘汰在每次入库/取候选时执行，规则与默认值都在 `settings.py`，可用环境变量覆盖。

### 可观测：注入了几条、正文真用了几条

| 软档字段 | 含义 |
| --- | --- |
| `brief_candidates` | 本次检索后交给模型的可选条目数 |
| `brief_injected` | `select` 实际注入 prompt 的条目数 |
| `brief_adopted` | 正文里词面重合达到 `ADOPT_OVERLAP` 的条目数 |
| `brief_adopt_ratio` | `adopted / injected` |

调用方在拿到正文后调用 `BriefTool.note_chapter_text(text)` 完成采纳统计：
把注入条目的摘要与正文做字符二元组覆盖度比较，达到阈值即算「采纳」。
这是粗判——大幅改写会漏判，因此只用于事后判断素材有没有帮上忙，不参与生成决策。
只记调用次数看不出素材是否有效，所以注入/采纳两个量必须一起看。

### 旧料风险

跨章复用会把**入库天数**写进每条候选（`N 天前入库 · 来源查询「…」· 标题`），
让模型自己知道这是几天前甚至几十天前抓的、可能已经过期。工具不做时效性硬判定，
只如实提供时间信息。

## 工具可见性（三态）

可见性只看「有没有能干活的后端」，不再和 `search_url` 这个 JSON 后端专属字段绑定：

| 状态 | 含义 | 留痕 |
| --- | --- | --- |
| `disabled` | 开关没开 | `brief_tool.state = "disabled"` |
| `no_backend` | 开关开了但没有可用后端：JSON 缺 `SEARCH_URL`、无头缺 playwright、配置或初始化异常 | `brief_tool.state = "no_backend"`，`reason` 记细分原因 |
| `visible` | 工具已给出 | `brief_tool.state = "visible"` |

旧条件 `enabled and search_url 非空` 会把免费/无头后端整体藏掉，链路根本跑不起来，已废弃。

## 降级与留痕

任何失败（未配置、超时、结构异常、被拦）都返回空串，不抛错、不阻断正文；
唯一的例外是预算用尽，它返回一句「改用选择工具」的提示（同样不进正文）：

- `retrieval_logs.status`：成功 `ok`、无结果 `empty`、失败 `error`、被反爬拦下 `blocked`；
  检索次数/时长用尽记 `budget_exhausted`，此时工具返回一段「请改用选择工具」的提示
  （只进工具消息，不进正文）——返回空串会让模型误以为「这次没查到」而反复换词重试；
- `soft_flags` 附带 `brief_cached`、`brief_similarity`（近义命中相似度）、
  `brief_filter`（过滤计数）、`brief_candidates`（候选条数）、`brief_injected`
  （注入条数）、`brief_adopted` / `brief_adopt_ratio`（正文采纳粗判）、`brief_error`；
- 同一查询或近义查询命中缓存时不会产生第二次网络请求。

### `blocked` 与 `error` 的区别（不要当成同一种）

| 状态 | 含义 | 是否重试 | 处理建议 |
| --- | --- | --- | --- |
| `blocked` | 对方站点明确拒绝自动化访问：验证码页、跳转 `wappass`/`captcha`/`verify`、HTTP 200 但结果页解析为空、HTTP 403/429 | **不重试**（硬闯只会更糟） | 降级为空素材；等冷却或换来源，不要在同一章内反复试 |
| `error` | 我方或链路的暂时性问题：连接失败、超时、5xx、响应结构异常 | 按 `RETRIES` 退避重试 | 降级为空素材；排查网络/地址/供应商 |

两者的共同点是都不阻断正文；区别在于 `blocked` 是**外部主动拒绝**，
重试无意义，统计上要单独看，避免把「被拦」误判成「代码坏了」。

## 噪声过滤

位置：`clean_text` 之后、`render_block` 之前；新抓取与缓存命中两条路径都会过滤。
缓存里保存的是**原始结果**，因此调整过滤规则不需要让缓存整体失效。

判定顺序固定（先命中先计数）：`short` → `promo` → `nav` → `duplicate` → `offtopic`。

| 原因 | 判定依据（结构性特征，不含站名/域名/题材词） |
| --- | --- |
| `short` | 清洗后摘要短于 `MIN_SNIPPET_CHARS`（默认 16 字）。标题式条目的摘要为空或只有栏目名，提供不了细节 |
| `promo` | 强广告诉求词命中任意一个，或弱广告诉求词命中 ≥3 种。广告页由交易/拉新动词堆叠而成，正常业务描述不会密集出现 |
| `nav` | 导航/栏目词命中 ≥2 种，或下载站动词命中 ≥2 种，或长度 ≥24 且空格/分隔符密度 ≥6%。导航站与堆词页的文本由栏目名、关键词串组成，缺少完整句子 |
| `duplicate` | 摘要长度不超过标题的 1.6 倍，且标题二元组有 ≥85% 出现在摘要里。摘要只是复述标题时没有新信息 |
| `offtopic` | 查询按空白/标点切词后，命中标题+摘要的词占比低于 `QUERY_COVERAGE_MIN`（默认 0.25）。2026-11-15 实测 cn.bing.com 会把「旧城改造 征收 流程」拆成单字匹配，返回「旧」字词条与房产广告；这类结果进 prompt 只会误导写作 |

计数写入 `retrieval_logs.soft_flags.brief_filter`：

```json
{"raw": 3, "kept": 0, "dropped": 3, "drop_ratio": 1.0,
 "reasons": {"short": 1, "nav": 1, "duplicate": 1}}
```

`raw` / `kept` / `dropped` / `drop_ratio` 能看出筛掉多少；`reasons` 能看出为什么筛。
全部被筛掉时素材块为空，状态记 `empty`（不是 `ok`），避免「检索成功但没素材」被当成可用。

## 近义缓存阈值（随长度调整）

阈值 = `max(BRIEF_TOOL_CACHE_SIMILARITY, 长度下限)`：

| 归一化查询长度 | 长度下限 | 理由 |
| --- | --- | --- |
| < 12 字 | `1.00` | 只认归一化后的精确命中；短查询二元组太少，一个字的差异占比过大 |
| 12–24 字 | `0.95` | 13 字加一个语气词只有 0.923，语义已变，必须重新检索 |
| > 24 字 | `0.92` | 长查询里单个字的增删对整体语义影响小，可以复用 |

算术依据：n 个字有 n-1 个二元组，末尾多一个字时 Jaccard = (n-1)/n；
n=13 时正好 0.923——这就是 0.9 会把「有哪些」并进「有哪些呢」的原因。

对照样例（`normalize_query` 之后比较）：

| 样例 | 相似度 | 应该 | 实际 |
| --- | --- | --- | --- |
| 「旧改征收 的实际环节」/「旧改征收的实际环节」 | 1.0000 | 命中 | 命中（归一化后相等） |
| 28 字查询末尾多「啊」 | ≈0.964 | 命中 | 命中（≥0.92） |
| 「常见办公软件的快捷键有哪些」/「…有哪些呢」 | 0.9231 | 不命中 | 不命中（<0.95） |
| 「社区物业日常对接」/「…对接事项」 | 0.78 | 不命中 | 不命中（<1.00） |
| 「接单结算」/「接单结算啊」 | 0.75 | 不命中 | 不命中（<1.00） |

## 示例

```dotenv
BRIEF_TOOL_ENABLED=true
BRIEF_TOOL_BACKEND=free
BRIEF_TOOL_MIN_INTERVAL_S=1.5
BRIEF_TOOL_ALLOW_FALLBACK=true
```
