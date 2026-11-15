# 「角色/设定」编辑诉求现在的落点审计

> 只调研，不改代码。结论来自代码查证（file:line）+ 提示词探针 + 一次性项目端到端实测（独立 DB、进程内 TestClient、离线 provider；未启 `:8000`、未动项目 5、未写真实 `kg_*`）。
>
> **证据等级说明**：第 0-4 节已有代码查证 + 端到端实测（第 6 节，独立 DB + 进程内 TestClient + 离线 provider）；实测数字见第 3 节「实测」列与第 6.4 节。没有改任何业务代码。

## 0. 先报 5 个真 bug

| # | 现象 | 证据 |
|---|---|---|
| B1 | 引导里说「要改角色设定，请到 KG 工作台（/kg）里的角色卡改」，但 KG 工作台**没有任何实体写入口** | 指路：[wizard_chat.py:323-327](backend/app/wizard_chat.py#L323-L327)、[wizard_flow.py:529-531](backend/app/wizard_flow.py#L529-L531)；实测 `GET /kg-v3/characters` = 200，`POST/PATCH/DELETE` 均 **405**；路由定义 [kg_v3.py:26-391](backend/app/routers/kg_v3.py#L26-L391) |
| B2 | `background / 身世 / 功法 / 等级 / power_level / level / 品阶` 写进 bible 后**零渲染**（静默无效，比没字段更危险） | 渲染白名单在 [prompts.py:238-246](backend/app/generation/prompts.py#L238-L246)；实测加这些键 system md5 完全不变 |
| B3 | bible 的 `characters` 删光后，旧的 KG 角色约束**回流**（作者以为删干净了，模型仍被旧角色绑定） | [prompts.py:137](backend/app/generation/prompts.py#L137) `if bible_chars and ...`：空列表 = 未宣告清册 = 不过滤；实测删光后 system 重新出现旧角色「林夜」 |
| B4 | 关系冲突扫描**永远不命中**：bible 的规范键是 `relation`（渲染与前端都用它），冲突扫描读 `type/rel_type` | 渲染 [prompts.py:380](backend/app/generation/prompts.py#L380)；前端 [ProjectWorkspace.vue:2023](frontend/src/views/ProjectWorkspace.vue#L2023)；扫描 [bible.py:100-105](backend/app/kg/bible.py#L100-L105)。实测：用 `relation` 返回 `conflicts=[]`，改成 `type` 才报 `relationship_type` |
| B5 | 同一角色**同时**写 `境界` 和 `量级` 时，提示词只渲染量级行，境界名整条消失（改境界看起来没生效） | [power_prompt.py:107-116](backend/app/generation/power_prompt.py#L107-L116) `if detail: ... elif realm:`；实测同时写时 `realm_rendered=false`、`scale_rendered=true` |

## 1. 数据落点与渲染口径（读懂六条的前提）

- **`plot_bible` 是自由 dict**，落在 `projects.plot_bible`；改一次另写一条 `kg_bible_revisions`（`bible_json / diff_json / conflicts_json`）。没有角色表、没有角色字段级 schema。
- **真正渲染进提示词的键**：
  - system：角色只有 `name / role / motivation`（[prompts.py:238-246](backend/app/generation/prompts.py#L238-L246)）；`foreshadowing`；`era_constraint`；`power_constraint`（[prompts.py:262-263](backend/app/generation/prompts.py#L262-L263)）。
  - user **仅非时间线模式**：`synopsis / direction / conflicts / relationships(读 relation) / foreshadowing`（[prompts.py:354-387](backend/app/generation/prompts.py#L354-L387)）。**时间线模式整块剔除**。
  - 量级/境界块只读角色卡的 `量级 / 量级细层 / 量级序号 / 修炼.当前境界 / 境界 / 出手规模`（[power_prompt.py:87-121](backend/app/generation/power_prompt.py#L87-L121)），由 [engine.py:322](backend/app/generation/engine.py#L322) 注入。**每个角色只出一行：有量级就写量级，没有再写境界**（[power_prompt.py:107-116](backend/app/generation/power_prompt.py#L107-L116)）。
- **`gender` 不渲染**；`等级 / 品阶 / background / 身世 / 功法` 也不渲染（实测 system md5 零变化）。
- **生成的角色来源是两路合并**：bible.characters（权威在前）+ 旧图谱 KgNode（`source="generated"`，[queries.py:102-145](backend/app/kg/queries.py#L102-L145)），合并/过滤口径唯一（[prompts.py:118-148](backend/app/generation/prompts.py#L118-L148)）。
- 新 v3 图谱 `kg_entities`：**只由批量抽取写**（[batch_worker.py:1824](backend/app/kg/batch_worker.py#L1824)），无手动入口；它的 `recent_entities` 会进正常续写的提示词（[retrieval_v3.py:68-78](backend/app/kg/retrieval_v3.py#L68-L78)），**不按 bible 过滤**；该 slice 只在正常续写注入（[engine.py:1567](backend/app/generation/engine.py#L1567)），重写路径不注入。

## 2. 六种诉求逐条

### 1）改角色名字 —— 能改「名单」，不能改「正文」
- **落点**：`plot_bible.characters[].name` → `projects.plot_bible` + `kg_bible_revisions`。
- **写入通道**：UI「编辑（级联协议）」JSON 模态 → `POST /projects/{id}/bible/apply`（[ProjectWorkspace.vue:130-166](frontend/src/views/ProjectWorkspace.vue#L130-L166) → [projects.py:575-596](backend/app/routers/projects.py#L575-L596)）；`PATCH /projects/{id}` 也能整块覆盖（[projects.py:522-561](backend/app/routers/projects.py#L522-L561)、[schemas.py:83-102](backend/app/schemas.py#L83-L102)），但无 revision、无冲突扫描。助手对话不改，只指路（死路 B1）。
- **已生成正文**：不动。旧名还在正文里，只能逐段重写。
- **后续生成**：system 换成新名（实测 system md5 `c700f8a4…`，含「林砚」、不含「林夜」）；bible 非空时旧 KgNode 角色被声明过滤。但 ①`detect_conflicts` 只按**新名**精确匹配（[bible.py:66-78](backend/app/kg/bible.py#L66-L78)），旧实体不报冲突（实测 `conflicts=[]`）；②`rebuild_generated_kg` 会从旧正文重新抽回旧名（[build.py:411+](backend/app/kg/build.py#L411)）；③v3 `recent_entities` 不按 bible 过滤，旧名会从 slice 漏进 prompt——**实测走真实续写：system 无旧名，但 user 含「林夜」且含【近期出场实体】标记**（user md5 `daa5f029…`）。
- **死路**：没有「全文改名/别名映射」；跨段的旧名与库里新名对不上，只能人工重写相关段。

### 2）改角色等级 / 境界 / 品阶 —— 只有「境界 / 量级」这条真的通
- **落点**：无角色等级/品阶 DB 字段。境界只能写在 `characters[].境界` 或 `修炼.当前境界`（另可配 `量级 / 量级细层 / 量级序号 / 出手规模`），由 power 块渲染。
- **写入通道**：同上（bible JSON / PATCH）。
- **已生成正文**：不动。
- **后续生成**：power 块变（实测：`境界=筑基三层` → `· 林砚＝境界「筑基三层」（量级 unknown，不按境界折算）`；`修炼.当前境界=炼气九层` → 同格式；`量级=B/序号=3/出手规模=一城` → `· 林砚＝B（seq 3/15）：一城`）。重写路径也带该块（E8a/E8b 均实测）。
- **死路**：写 `等级 / power_level / level / 品阶` 一律**零渲染**（B2，实测 system md5 与基线完全相同）。角色「品阶」在 v3 里只存在于 item 的 `attrs.grade`，不适用于人。
- **隐藏陷阱**：`境界` 与 `量级` 同时写时只渲染量级（B5）——作者改了境界却看不到，误以为没生效。要么只写境界，要么接受量级优先。
- **境界不会被抽取覆盖**：`state_type` 白名单只有 `location|injury|mood|goal|inventory|alive`（[extractor_v3.py:136](backend/app/kg/extractor_v3.py#L136)），没有境界档。
- **`realm_order` 是另一回事**：`plot_bible.realm_order` 只给哨兵做境界事件抽取（[sentinel.py:1099-1107](backend/app/kg/sentinel.py#L1099-L1107)、[sentinel.py:1137-1142](backend/app/kg/sentinel.py#L1137-L1142)），并在预检里做摘要（[generation.py:251-270](backend/app/routers/generation.py#L251-L270)），**不渲染成某个角色的等级**。

### 3）改功法 —— 底层没有这个概念
- **落点**：无。bible 没有功法键；v3 六类里没有「功法」，只能被抽成 `power_system`（可修炼/可晋级的体系，含术/法/功/诀/境）或 `item`（**秘籍实物**）（[extractor_v3.py:496-530](backend/app/kg/extractor_v3.py#L496-L530)）。
- `NON_ITEM_NAMES` 里 `狮子吼=功法` 是**参考语料侧的人工归类表**，用于把非物品名挡在物品之外（[item_category.py:42-55](backend/app/kg/item_category.py#L42-L55)），与用户项目、bible、生成都无关，也不是可编辑入口。
- **写入通道**：无。改功法既没有 API/UI，也不会被 bible 渲染（写 `功法` 零变化）。
- **影响**：对正文、对后续生成都无影响（因为根本进不去）。
- **死路**：要支持得先建「功法/技能」实体或 bible 键并接进提示词。

### 4）改角色背景（身世、来历） —— 没有专用字段，只能借道
- **落点**：无 `background/身世/来历` 键。能影响生成的只有 `role`、`motivation`（system），以及 `synopsis/direction`（user，且**仅非时间线**）。`KgEntity.attrs` 的 `arc / current_goal / secrets` 只用于角色对话卡（[assistant_wizard.py:1221](backend/app/routers/assistant_wizard.py#L1221)），不进生成提示词。
- **写入通道**：bible JSON / PATCH。
- **已生成正文**：不动。
- **后续生成**：塞进 `role/motivation` 有效（实测 `· 林砚（主角；动机：幼年灭门，誓要复仇）`）；塞进 `synopsis/direction` 在时间线模式下**整块被剔除**（[prompts.py:354-387](backend/app/generation/prompts.py#L354-L387)），等于没写（实测非时间线有「整体梗概/后续走向」，传 `node_prompt` 后两者都消失）。
- **死路**：`background/身世/来历` 零渲染（B2，实测 system md5 与基线完全相同）；无字段可落。

### 5）增加角色 —— 能进「约束名单」，进不了「知识图谱」
- **落点**：`plot_bible.characters` 加一条（`name` 必填，`role/motivation` 可选）。
- **写入通道**：bible JSON / PATCH；助手对话不改。
- **已生成正文**：不动。
- **后续生成**：system 角色约束多一行，立刻生效（实测 system 含「顾辰逸」且 md5 变化）。**但** `kg_entities` 不会凭空多出该角色（实测 `kg_entities` 总数不变、角色清单仍只有「林夜」），KG 工作台/角色卡/关系图都看不到；只有等它在正文里被写出来并被抽取，才会进 v3。
- **打架点**：生成读「bible + 旧 KgNode」，v3 slice 读 `recent_entities`。新角色只进前者；旧角色可能同时出现在后者，两者的「当前阵容」会暂时不一致。
- **死路**：想让新角色在 KG 工作台可见、有角色卡，当前没有手动建实体入口（B1）。

### 6）改角色数量（删角色、合并角色）
- **删**：从 `characters` 移除即生效（bible 非空时过滤旧 KgNode，实测删「顾辰逸」后 system 不再含他）。**但删光会关闭过滤**（B3），旧 KG 角色全部回流（实测 `characters=[]` 后 system 重新出现「林夜」）；v3 `recent_entities` 仍可能带出已删角色，形成「库里删了、提示词里还有」。
- **合并**：无任何机制。bible 没有 `aliases`；`KgEntity.aliases` 只由抽取自动合并（[batch_worker.py:1816-1820](backend/app/kg/batch_worker.py#L1816-L1820)），无手动归并入口；实测 `GET /kg-v3/characters` = 200，而 `POST/PATCH/DELETE` 全 405，`aliases` 始终为 `[]`。
- **已生成正文**：不动；合并后两个名字都在旧正文里，只能逐段重写。
- **后续生成**：删角色受 B3 影响；合并只能二选一保留、另一条从 bible 删，旧 KG/正文里的另一名无法清除。

## 3. 总表

| 诉求 | 现在能不能改 | 数据落点 | 对已生成正文 | 对后续生成 | 实测 |
|---|---|---|---|---|---|
| 改角色名字 | **能**（只改名单） | `plot_bible.characters[].name` + `kg_bible_revisions` | 不动，旧名留正文，需逐段重写 | system 用新名；旧 KgNode 被过滤；**不报冲突**；旧名经 v3 slice 漏进 user | system 新名 ✓ / 旧名 ✗；`conflicts=[]`；真实续写 user 含旧名 + 【近期出场实体】 |
| 改等级/境界/品阶 | **部分**：境界/量级能；等级/品阶不能 | `characters[].境界`、`修炼.当前境界`、`量级*`、`出手规模` → power 块；无 DB 字段 | 不动 | 下一段 system 的【量级与体系】变；重写也带；`等级/品阶` 零渲染；境界被量级遮盖 | 境界/修炼/量级各自 md5 均变；`等级/品阶/power_level/level` md5 不变；同时写时境界不渲染 |
| 改功法 | **不能** | 无；v3 只归 `power_system`/`item`，`NON_ITEM_NAMES` 是参考语料人工表 | 不动 | 无影响（进不去） | 写 `功法` 后 system md5 不变、文本不出现 |
| 改角色背景 | **不能**（无专用字段） | 只能借 `role/motivation`（system）或 `synopsis/direction`（仅非时间线） | 不动 | 借道 `role/motivation` 有效；`background/身世` 零渲染；时间线模式连 `synopsis` 也剔除 | background 组 md5 不变；motivation 组变化；时间线两段全消失 |
| 增加角色 | **能**（只进约束名单） | `plot_bible.characters` 加一条 | 不动 | system 立刻多一条约束；KG/角色卡/工作台不出现，与 v3 slice 暂时不一致 | system 含「顾辰逸」；`kg_entities` 数量不变、清单仍只有「林夜」 |
| 删/合并角色 | 删：**能**（有空列表陷阱）；合并：**不能** | 删=`characters` 移除；合并无落点 | 不动 | 删光→过滤关闭、旧角色回流；无别名/归并机制 | 删一人后 system 无他；删光后「林夜」回流；实体写路由 405、`aliases=[]` |

## 4. 结论

**「加个入口就能用」（底层已有落点或渲染，缺的只是更好用的表单）：**
- 改角色名字（但需接受「旧正文只能逐段重写」）
- 增加角色
- 删角色（但必须先修 B3：删光后要能显式「清空角色」而不是关闭过滤）
- 改境界 / 量级（已有 power 块渲染；需先决定「境界与量级谁优先」，见 B5）

**「底层没有概念、得先建模」：**
- 改角色等级 / 品阶（无角色等级字段；要么映射到 `量级`，要么新增字段并接进 power 块）
- 改功法（无实体类型、无 bible 键、无渲染）
- 改角色背景 / 身世（无字段、无渲染；只能借 `motivation`）
- 合并角色（无 `aliases` / 归并机制，KG 无写入口）

**先修再谈入口（真 bug）：** B1 假指路、B2 静默无效键、B3 空列表陷阱、B4 关系冲突永不命中、B5 境界被量级遮盖。

## 5. 执行时的收尾（仅清理，不动业务代码）

- 删除一次性脚本与探针：`backend/.tmp_verify/audit_live.py`、`audit.db`、`audit_result.json`、`bible_render_probe.py`。
- 确认未改 `:8000`、未动项目 5、未写 `kg_*` 真实数据。

## 6. 已执行：端到端实测（一次性项目 + 隔离 DB）

### 6.1 环境与红线（实际执行口径）

1. **没有复制 `novel_dev.db`**：改用独立空库 `backend/.tmp_verify/audit.db`，由 `ensure_schema()` 建同构表结构。这比复制一个正在使用的 100MB+ SQLite 文件更安全，也确保零真实数据进入。
2. `DATABASE_URL` 指向该空库，进程内 `TestClient`（不启 uvicorn）；一次性项目 `project_id=1`，标题「编辑落点审计-一次性」。
3. 生成用脚本内的离线 `Rec`（继承 `LLMProvider`，记录 system/user 原文并回固定正文），避免真实模型抖动。
4. 造数：bible 角色「林夜」；1 段正文含「林夜」；旧 KgNode(character, source=generated)=林夜；v3 KgEntity(character)=林夜 + 最近段 KgMention。摘要与 `synopsis` 刻意不含「林夜」，保证 user 里出现旧名只可能来自 v3 slice。
5. 执行命令：`python .tmp_verify/audit_live.py`，退出码 0。

### 6.2 实验矩阵（已跑）

| 实验 | 操作 | 结果 |
|---|---|---|
| E1 改名 | 把 name 改为「林砚」并 apply | system 含「林砚」不含「林夜」；`conflicts=[]`；纯提示词 user 无旧名；`slice_for_prompt/format_slice` 仍含「林夜」；`kg_entities` 角色仍只有「林夜」；正文仍为 `林夜握紧铜钥匙，走进雨里。` |
| E1b 真实续写 | `generate_next_segment`，捕获 chat | system 无旧名；user 含「林夜」且含【近期出场实体】→ **旧名确实经 v3 slice 进入 user**；写到第 2 段 |
| E2 境界/等级 | 逐个写并比对 system md5 | `境界` / `修炼.当前境界` / `量级*` 三者均变；`等级/品阶/power_level/level` md5 **完全不变**；`境界+量级` 同时写 → 只渲染 `· 林砚＝B（seq 3/15）：一城`，境界不出现 |
| E3 功法 | 写 `功法=天地一刀斩` | system md5 与基线相同，文本不出现该词 |
| E4 背景 | 写 `background/身世/来历`；再写 `motivation` | 前者 system md5 不变；后者变 `· 林砚（主角；动机：幼年灭门，誓要复仇）`；传 `node_prompt` 后「整体梗概/后续走向」都消失 |
| E5 增加角色 | characters 追加「顾辰逸」 | system 多一行含「顾辰逸」；`kg_entities` 总数不变，角色清单仍只有「林夜」 |
| E6 删除/合并 | 删 1 人；删光；请求写接口 | 删「顾辰逸」后 system 无他；`characters=[]` 后「林夜」**回流**；`GET /kg-v3/characters`=200，`POST/PATCH/DELETE` 全 **405**；`aliases=[]` |
| E7 关系冲突键 | `relation` vs `type` | 用 `relation` → `conflicts=[]`；用 `type` → 报 `relationship_type`（`enemy` vs `师徒`） |
| E8 重写路径 | `regenerate_segment_stream` 捕获 stream | system 含「林砚」；境界单独写时渲染（E8a），同时写量级时只剩量级（E8b）；user **无**【近期出场实体】（重写不注入 v3 slice）；落库正文 = provider 文本，无约束/指令残留；`done.kg.rebuilt=true` |

### 6.3 B2 / B3 修复记录（2026-11-11）

#### 6.3.1 B2：bible 键封闭契约

**结论**：不再靠「恰好被某个模块读到」判定有效。渲染器支持的键落成
`app/kg/bible.py::SUPPORTED_BIBLE_KEYS`（顶层）与
`SUPPORTED_CHARACTER_KEYS`（角色卡），写入入口
（`POST /bible/apply`、`PATCH /projects/{id}`）统一过
`validate_bible_keys()`；支持之外一律回执告警。

**处置表：**

| bible 键 | 实际渲染 | 处置 |
|---|---|---|
| `characters`（`name/名字/role/motivation`） | system 角色行 | 支持 |
| `characters[].background` / `身世` / `来历` / `背景` | **本轮接入**：system 角色行「背景：…」 | 支持（md5 变化） |
| `characters[].境界` / `修炼.当前境界` / `量级` / `量级细层` / `量级序号` | 量级块 | 支持 |
| `characters[].出手规模` | 量级块（**仅当同时有量级证据**） | 支持；只有出手规模时给 `shape` 告警 |
| `relationships` / `foreshadowing` / `synopsis` / `direction` / `conflicts` | user 动态指引 / system 伏笔 | 支持 |
| `power_ladder` / `量级阶梯` | 量级块（15 层） | 支持 |
| `system_type` / `体系类型` / `power_system_type` / `境界体系类型` / `realm_system_type` / `systems` | 量级块（体系类型） | 支持；`systems` 需各条一致才生效 |
| `realm_order` | **不进生成提示词**，供 `sentinel.py` 抽境界事件 | 支持（跨系统生效，已注明） |
| `baseline` 等基线键、`world`/`世界观`/`设定`/`背景` 容器 | era 约束块，**仅 `era_guard_enabled=True` 时** | 支持；字符串容器给 `shape` 告警；默认开关关闭时不进 system |
| `功法` | 否 | 告警 `unsupported`：归 `power_system`/`item` |
| `等级` / `品阶` / `power_level` / `level` | 否 | 告警 `unsupported`：用 `境界` 或量级字段 |
| `gender` | 否 | 告警 `unsupported`：写进 `role`/`motivation` |
| 顶层 `background` / `身世` / `来历` | 否 | 告警 `unsupported`：写进 `characters[]` |
| 未登记的任意键 | 否 | 告警 `unknown`（以后加新键也不能再静默） |
| `note` | 否（向导降级说明） | 元数据，不告警 |

**实测（隔离库 + 进程内 TestClient，一次性项目）**：

- 基线 `sys_md5=e0608b589e81ebe0026d2d32fd7e6174`
- `background` / `身世` / `来历` → `sys_md5=848dad9826602bef8cfc08e74f3d0ef9`（变化 True），system 含「背景：幼年灭门，誓要复仇」
- `功法/等级/品阶=上品/power_level/level/gender`（角色卡与顶层各一轮）→ md5 **全部不变**，回执均带 `unsupported` 告警
- 未登记键 `future_key_2030` → `unknown`；`出手规模` 无量级 → `shape`；`世界观` 写成字符串 → `shape`
- 注意：`品阶="一品"` 会让 `power_prompt._uses_15_ladder` 的**全文关键词扫描**误命中 15 层块，从而改变 md5——这不是品阶被渲染，是另一处启发式副作用，本轮未扩展修复。

#### 6.3.2 B3：空列表语义

`merged_constraint_items` 现在区分三态：

| plot_bible | 语义 | 行为 |
|---|---|---|
| 没有 `characters` 键 | 老项目未宣告清册 | 不过滤，KG 角色照旧合并 |
| `characters` 非空 | 显式清单 | 未列入的 KG 角色剔除（与改前一致） |
| `characters: []` | **本项目现在没有任何角色** | 剔除全部 KG 角色 |

**实测（同一一次性项目，旧码用 `git show HEAD:` 载入对照）**：

- 非空清册：旧码 / 新码 `sys_md5` 均为 `2ff60fd5ecb9a9a66902c26b5a4f321a`，顾辰逸被过滤
- 空清册：旧码 `b6a26a98ad48e07784b652c919bd633e` 且含「林夜」；新码 `f598b17f9f652e15dbbd90f12ed8657a`，system 不含林夜、含「暂无锁定角色」
- 缺 `characters` 键：`sys_md5=ec0e89dd9cb866b698c7a684263dc916`，林夜仍在（兼容老项目）

#### 6.3.3 改动与守卫

- 代码：`app/kg/bible.py`（契约 + 校验 + apply 回执）、`app/generation/prompts.py`（B3 + 角色背景渲染）、`app/routers/projects.py`（PATCH 告警头）、前端回执展示。
- 测试：新增 `tests/test_bible_key_contract.py`（17 例）；`backend/tests/test_kg_cascade_protocol.py` 的「空列表不过滤」断言按 B3 语义反转为「空列表 = 无角色」；`backend/test_p0_regression.py` 注释同步。既有非空过滤断言未放宽。
- 守卫复跑（只读）：`kg_mentions=34`、gid45 覆盖 125、六探针 digest 不变、p1/p5 指纹不变、11 条持有行不变；未启动/重启 `:8000`，未写真实 `kg_*`。

### 6.4 角色改名入口（2026-11-11）

用户口径：**影响前文的改名不允许，只在角色刚出场、影响不大时允许。**

#### 6.4.1 判定读哪张表（Q1）

| 候选 | 性质 | 结论 |
|---|---|---|
| `generations.text` | 段级正文，每段一行；重生成原地替换 `text/summary` | **唯一全文事实源，判定用它** |
| `kg_mentions` | 只有 `segment_no` + `context_window` 片段；`generation_id` 由 `batch_worker._seg_to_gen` 逐字比对正文后绑定 | 抽取索引，**不能**当正文：抽取漏掉的提及不在表里 |
| `generations.summary` | 前情提要，由正文派生，进下一次 prompt | 只**报告**命中数，不参与判定（用户口径是正文） |

两者一致性：mention 是正文的子集；正文是权威。一次 `SELECT segment_no, text, summary`
取全，不重复扫表。

**匹配口径（名字形态问题）**：精确子串，不做模糊匹配。

- 带称谓「林夜兄」仍算「林夜」的一次提及（`str.count` 本就包含它），不会漏判；
- 但「李浩」是「李浩微」的前缀，本库项目 1 里两者是**不同角色**（实体 #66/#67）。
  若直接 `count`，短名会被长名抬高。因此计数时排除「落在其他角色更长 canonical
  name 内」的位置；别名不参与排除（别名常是同一个人的另一种叫法）。
- 结果：`_count_name("林夜兄来了。林夜兄走了。", "林夜", set()) == 2`；
  `_count_name("李浩微进门。李浩看了他一眼。", "李浩", {"李浩微"}) == 1`。

#### 6.4.2 真实分布与阈值（为什么是 3 不是 5）

本库已生成正文很少（项目 1 只有 3 段、项目 5 只有 2 段），不足以定阈值；
改用同库参考语料《大奉打更人》做分布（925 章、565 万字，按 `chapter_no` 聚合，
一章 ≈ 一段），统计 49 个具名角色**首次出场那一章**的出现次数：

| 指标 | 值 |
|---|---|
| n | 49 |
| min / p10 / p25 / p50 | 1 / 1 / 2 / 4 |
| p75 / p90 / max | 11 / 20 / 41 |
| ≤3 次占比 | 22/49 = 45% |
| 计数分布（1..5） | 11 / 6 / 5 / 5 / 2 |

本库正文侧：次要角色密度 2–5 次/段（失踪者 B 4 次/2 段、失踪者 A 10 次/2 段），
主要角色 20–47 次/段，与参考语料同量级。

**取 3**：中位数是 4，3 严格落在「低于中位数的轻量出场」——一次引见 + 一两次
称呼/动作，可在最后一段内一次改完。取 5 会越过中位数，把「已经写过两三个回合」
的角色也当成刚出场；这类角色改名后需要返工的提及更多，违背「影响不大」的口径。

#### 6.4.3 判定表

| 正文覆盖 | 判定 | 处置 |
|---|---|---|
| 从未出现（含手动加进 bible 的角色） | 允许（无条件） | 改 bible.name；图谱无实体则不联动 |
| 只在最后一段，次数 ≤ 3 | 允许 | 改 bible.name + 关系端点；实体改名 + 旧名进 aliases |
| 只在最后一段，次数 > 3 | 拒绝 | 回执给段号/次数/上限 + 替代路径 |
| 跨 ≥ 2 段（任意次数） | 拒绝 | 回执给段号区间 + 总次数 + 替代路径 |

拒绝也返回 HTTP 200 + `ok=false`：这是业务判定，不是请求错误，前端原样展示
`receipt` 与 `alternatives`。

#### 6.4.4 三种情形实测（一次性库 + 进程内 TestClient）

- 刚出场：第 4 段 3 次 → `ok=true`，system 含「林砚」、不含「林夜」，
  `sys_md5` 由 `2a8970d7…` 变为 `f241b1d9…`（变化）。
- 跨多段：第 1–2 段 17 次 → `ok=false`。回执原文：
  「林夜」已经在第 1–2 段出现 17 次（跨 2 段），改名会让前文对不上。
  替代路径：接受现状 / 重写第 1–2 段 / 旧名登记别名（并明确说明别名不是改名）。
  项目 2 的 bible 未改、revision 数为 0。
- 从未出现：「秦霜」→「秦双」→ 无条件允许，`total=0`，同步关系端点。
- 非空清册过滤：system 仍不含未宣告的 KG 角色「顾辰逸」。

#### 6.4.5 手动角色怎么判（Q2）

判定只看正文覆盖，不看来源。手动加进 bible、正文从未出现的角色 `total=0`，
无条件允许；图谱里通常也没有同名实体，改名只动 bible（回执写明「无需联动」）。

#### 6.4.6 kg_entities 旧名怎么办（Q3）

- 允许改名时：`kg_entities.canonical_name` 同步改为新名，旧名写入 `aliases`；
  `kg_mentions.entity_id` 不变，历史提及与计数不丢。
- 旧段重抽：抽取器只读正文（不知道 aliases），重抽只会产出旧名。本轮在
  `batch_worker._upsert_entity` 增加**精确 alias 匹配**：无同名 canonical 行时，
  若词等于某实体的 alias，则并回该实体，不新建。实测重抽旧名后实体数仍为 1，
  与改名实体同一行。
- 残留：v2 `facts` / `build.py` 的历史行仍可能含旧名（事实不可改写），但 bible
  清册过滤保证它不会进 system；v3 slice 用新名。
- 图谱里已有新名实体时直接 400（避免两条同名 canonical 撞唯一键）。

#### 6.4.7 aliases 支持结论

- v3 实体层**支持**：`kg_entities.aliases`，`entity_lookup` 与 `_lookup_entity_id`
  已按 alias 反查；本轮补上了写入口的 alias 归并。
- bible 角色卡**没有** aliases 字段，system 提示词只读 `bible.name`。因此
  「旧名保留为别名」只能保住检索/图谱里的解析，**不能**替代改名；拒绝回执里
  已明确写出这一点，避免用户误以为别名等于改名。

#### 6.4.8 改动与守卫

- 代码：`app/kg/character_rename.py`（判定 + 回执 + bible/实体联动）、
  `app/routers/projects.py`（`POST /projects/{id}/bible/characters/rename`）、
  `app/kg/batch_worker.py`（精确 alias 归并）、前端角色标签「改名」入口与回执模态。
- 测试：新增 `tests/test_character_rename.py`（12 例），覆盖三种情形 + 阈值边界
  （恰好 3 / 4 / 多一段）+ 名字形态（带称谓 / 长名包含短名）+ 实体别名 +
  旧名回流 + 撞名 400；既有断言未放宽。
- 守卫复跑（只读）：`kg_mentions=34`、gid45 覆盖 125、六探针 digest 不变、
  p1/p5 指纹不变、11 条持有行不变、`generated.db` sha 不变；未启动/重启 `:8000`。
- 说明：根测试的 `test_db_isolation` 在本次跑测中报告 `novel_dev.db` 文件 sha 变化，
  但上述命名守卫全部未变，且 `kg_entities/kg_mentions` 最大 `created_at` 仍为
  2026-11-10；本次所有用例均走 `get_db` 依赖覆盖或临时库，未写真实 `kg_*`。

### 6.5 B1 / B4 / B5 与角色编辑三入口（2026-11-11）

#### 6.5.1 B1：假指路

- 改文案：`wizard_chat.py` / `wizard_flow.py` 三处不再把人指去 KG 工作台（那里
  `POST/PATCH/DELETE` 仍是 405），统一改为「项目工作台 → 剧情圣经 → 角色」。
- 这条路是真的：三个写入口（加 / 删 / 改境界）连同已完成的改名，都落在这块 UI 上；
  2026-11-11 用隔离后端 + 隔离前端在真实浏览器里点通了（见 6.5.5 截图）。
- 没有给 KG 工作台补写接口——本轮不需要，且它属于另一条线的范围。

#### 6.5.2 B5：境界与量级是同一维度的两种刻度

结论：**同一维度（战力），不是两个维度**。依据是代码事实，不是感觉：

- `power_scale_15.json` 的 `realm_map.tiers[].stage_seq` 就是「境界阶位 → 量级 seq」
  的默认映射；`default_scale_for_realm()` 直接按境界序数折算量级。
- `量级` 是跨体系可比的物理尺度，`境界` 是体系内名义阶位；两者描述的是同一个
  「多强」，只是坐标系不同（`system_type` 里粗放型甚至明确「境界序数不可比，
  强弱只按出手规模」）。
- 数据层没有「合并会丢信息」的情况：两者都是自由文本，合并只是渲染成一行。

改法：两者同时存在时渲染 `境界「A」（量级 B，seq n/15）：出手规模`——境界名在前
（读者看到的是这个名字），量级放括号里当可比刻度。只有量级时维持原样；只有境界时
仍标 `量级 unknown`（不按境界折算）。

实测（一次性库）：

```text
· 林砚＝境界「金丹」（量级 B，seq 3/15）：一城
含境界名: True | 含量级: True | 境界在前: True
```

#### 6.5.3 B4：关系键归一

`relationships[]` 里实际在用的写法（代码 + 前端 + 历史数据）：

| 位置 | 关系类型键 | 两端键 |
|---|---|---|
| 渲染 `prompts.py` | `relation` | `a` / `b` |
| 前端 `ProjectWorkspace.vue` | `relation` | `a` / `b` |
| 改名同步（6.4） | — | `a` / `b` / `from` / `to` |
| 冲突扫描（改前） | `type` / `rel_type` | `a`/`from`、`b`/`to` |

改法：在 `bible.py` 收一张键表 `RELATION_TYPE_KEYS = (relation, type, rel_type, 关系,
关系类型)`、`RELATION_FROM_KEYS` / `RELATION_TO_KEYS`，冲突扫描与改名同步都走
`_first_text` 归一。写侧不改名（原键保留），读侧全部等价。

实测：`relation` / `type` / `rel_type` / `关系` 四种写法都命中同一条
`relationship_type` 冲突；端点 `a/b` 与 `from/to` 也等价；类型一致时 `conflicts=[]`。

#### 6.5.4 三个入口

| 入口 | 端点 | 行为 | 错误 |
|---|---|---|---|
| 加角色 | `POST /projects/{id}/bible/characters/add` | 追加 `characters[]`，写 revision；新角色正文未出场（`total=0`），改名无条件允许 | 重名 / 未知键 / 已知不支持键 → 400 |
| 删角色 | `POST /projects/{id}/bible/characters/remove` | 移除卡片 + 以它为端点的关系；删到空保留 `characters=[]`（B3 空清册语义） | 角色不存在 → 400 |
| 改境界 | `POST /projects/{id}/bible/characters/realm` | 写 `characters[].境界`；卡里已有 `修炼.当前境界` 时写那里（渲染优先级更高，避免两个值打架） | 不在 `realm_order` → 400 并列出可选值；项目未配 `realm_order` 则放行并在回执注明无法校验 |

共同点：全部走 `apply_bible_revision`（revision + diff + 冲突 + 键告警）；不改 schema；
不改已生成正文；前端三个入口都有按钮、模态与回执。

一次性端到端实测（临时库 + 进程内 TestClient，system prompt md5 前后对比）：

```text
改前        system md5: 2b1e...  含 林夜 True | 含 顾辰逸 True | 含未宣告者 False
加 沈砚      system md5: 9f3a...  变化 True | 含 沈砚 True | 含背景 True
加卡带功法   HTTP 400  「角色卡里有不会被使用的字段，已拒绝写入：characters[0].功法：…」
新角色改名   HTTP 200  total=0 | ok=True
改 林夜→金丹 system md5: c57d...  变化 True | 含 境界「金丹」True | 含 境界「炼气」False
境界不在清单 HTTP 400  「「元婴」不在本项目的 realm_order 里；可选境界：炼气、筑基、金丹」
删 林夜      system md5: 7a22...  变化 True | 含 林夜 False | 含 顾辰逸 True | 关系已移除
删 顾辰逸    system md5: a8f0...  变化 True
删 沈砚之    system md5: 4c9e...  变化 True | 三个名字全不在 | 含「暂无锁定角色」True
最终 bible   characters=[]（键存在）| revision 数 7
```

> 上述 md5 为方便阅读做了截断，完整值见 `backend/.tmp_verify/edit_e2e.py` 的运行输出；
> 该脚本与临时库只用于验证，不参与运行。

#### 6.5.5 前端与截图

- `vue-tsc --noEmit` 通过，`npm run build` 通过。
- 隔离部署：后端 `127.0.0.1:8011`（临时库 `ui_e2e.db`），前端 Vite `localhost:5183`
  （`API_TARGET` 指向 8011）。**未碰 `:8000`、项目 5、真实 `kg_*`**。
- 真实页面截图（`浏览器 → 项目工作台 → 5 构思剧情 → 剧情圣经 · 角色`）：

| 入口 | 操作前 | 操作后 |
|---|---|---|
| 加角色 | `screenshots/character-add-before.png` | `screenshots/character-add-after.png`（出现「沈砚 · 北境刀客」） |
| 改境界 | `screenshots/character-realm-before.png`（弹窗当前值「炼气」） | `screenshots/character-realm-after.png`（回执「已把「林夜」的境界改为「金丹」（写入 境界，量级块会立即使用）」） |
| 删角色 | `screenshots/character-delete-before.png` | `screenshots/character-delete-after.png`（标签消失 + 删除回执） |

#### 6.5.6 测试与守卫

- 新增 `tests/test_character_edit.py`（21 例）：加角色 5 例（含未知键 / 功法拒绝、
  重名、新角色改名 total=0）、删角色 3 例（含删到空 + system 过滤）、改境界 4 例
  （写入字段 / 不在清单 / 修炼优先 / 未配阶梯）、B5 双写 2 例、B4 三种写法 4 例 +
  端点两种 2 例 + 类型一致 1 例。既有断言未放宽。
- 根套件：`test_character_edit` + `test_character_rename` + `test_bible_key_contract`
  + `test_power_gate` 共 59 个测试点通过。
- `backend/tests/test_kg_cascade_protocol.py` 49 通过；`backend/test_p0_regression.py` 55/55。
- 守卫复跑：`kg_mentions=34`、gid45 覆盖 125、六探针 digest 不变（`617c601c` /
  `24e7c486`）、p1/p5 五段指纹全部一致、11 条持有行不变、`generated.db` sha
  `0a6b0025…` 不变。

### 6.6 原始输出（可复核）

```text
BASE sys_len=1105 user_len=312 sys_md5=e14a0d6c874f4e0fada1a693fa8b51f7 user_md5=c983a332768bc214dc7f39f6d7174672
E1 sys_new=1 sys_old=0 conflicts=0 plain_user_old=0 slice_old=1 sys_md5=c700f8a4fba0390a4ed759269a7eaeb3 user_md5=c983a332768bc214dc7f39f6d7174672 text_same=1
E1b chats=1 sys_old=0 new=1 user_old=1 slice_marker=1 user_md5=daa5f0297de7b398c2aace74f1207a23 seg=2
E2 realm=1 cult=1 scale=1 inert=0 both_realm=0 both_scale=1
E2 lines: · 林砚＝B（seq 3/15）：一城
E3 changed=0 text=0
E4 bg=0 mot=1 tl_syn=1 tl_dir=1
E5 sys_new=1 changed=1 kg_count=1 kg_chars=['林夜']
E6 del_one_has_gu=0 after_del_old_linye=0 del_all_old_back=1 codes={'GET': 200, 'POST': 405, 'PATCH': 405, 'DELETE': 405} aliases=[[]]
E7 relation_len=0 type_len=1 kinds=['relationship_type']
E8a name=1 realm=1 scale=0 slice=0 saved_same=1 residue=0 kg={'rebuilt': True, 'segments': 2, 'replayed': 2, 'failed': [], 'complete': True, 'source': 'generated'}
E8b name=1 realm=0 scale=1 slice=0 saved_same=1 residue=0 kg={'rebuilt': True, 'segments': 2, 'replayed': 2, 'failed': [], 'complete': True, 'source': 'generated'}
```

### 6.7 回填说明

- 已把实测结果写入第 0 节 B1/B4/B5 证据、第 1-2 节条目与第 3 节「实测」列。
- 与代码推断一致；唯一需要修正的认知是 **境界与量级互斥渲染**（原先只说「两者都渲染」）。
- 收尾复核：脚本与临时库已删除；`novel_dev.db` 未打开写入；`:8000` 未启动。

