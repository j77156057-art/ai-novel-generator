# 一致性检查层（coherence-v1）：设计与实测

> 目标：把第 2 章那批「手工改掉、没进链路」的错误变成**通用检查**。换一本书，只要项目数据齐备，同一套代码仍然有效——代码里不出现任何书名、人名、年份、章号。
>
> 结果：新增 `backend/app/coherence.py`（纯函数、无 DB 依赖）+ 一层接线（`engine.py`）+ 15 个合成数据单测。第 1 章 0 确定 / 3 疑似；第 2 章修补前 3 确定 / 4 疑似（四类全部命中）；修补后 0 确定 / 1 疑似。

---

## 1. 设计

### 1.1 位置与边界

```
生成正文
  ├── 文风闸门  app/style/consistency.py  →  passed / 九项数值      （原样不动）
  └── 一致性层  app/coherence.py         →  quality_json["coherence"]（新增，并排）
```

- **不改闸门**：`generation_gate()` 的九项阈值、`length_limits()`、`passed` 的判定与数值口径一行没动。
- **不改蒸馏侧**：`kg/`、`style/` 下的代码只读审计，未改。
- **纯函数**：`run_coherence(text, *, plot_bible, outline, chapter, prior_chapters, entity_names)` 只吃字符串与 JSON，不碰 DB、不调模型。接线所需的取数在 `engine._coherence_report()` 里，有异常一律吞掉返回 `None`——一致性层故障不允许拖垮生成主流程。

### 1.2 数据来源（判定所需的一切都从项目读）

| 输入 | 取自 | 用途 |
|---|---|---|
| `plot_bible` | `projects.plot_bible` | 角色名 / 别名 / 称谓 → 说话人与持物人；`story_year` 等字段 → 年代锚点 |
| `outline` | `projects.outline`（逐章字符串） | 伏笔线索、揭示节点、本章「不写」清单 |
| `chapter` | `generations.chapter_range` 解析（`chapter_index()`） | 定位「第 N 章才允许出现」 |
| `prior_chapters` | 本章之前最近 4 条 `generations.text` | 跨章时间线、指代的前文来源 |
| `entity_names` | `kg_entities` 中 `novel_id IS NULL` 的本项目实体 | 指代是否有来源、时间线主语 |

> 注意：`kg_entities` 里 `novel_id=7` 的那 12,039 条是**参考书摄入**的实体（另一本书的世界观），不能当成本书设定。接线时显式过滤 `novel_id IS NULL`，只留本项目自己产出的 16 条。

### 1.3 四类规则

| 类 | check | 形式信号 | 等级 |
|---|---|---|---|
| 说话人 | `quote_two_turns` | 同一对引号内，问号后紧跟 ≥6 字且含回答标记；且问句是请示式（`行不行` / `能不能` / `可不可以`…）或本身带第二人称 | 确定 |
| 说话人 | `echo_answer` | 相邻两段一问一答，最长公共子串 ≥6（后一句逐字复述前一句） | 疑似 |
| 说话人 | `two_quotes_no_cue` | 同一段两组引号、中间无说话动词且未点人名、两对人称发生对调 | 疑似 |
| 说话人 | `possession_conflict` | 说话人自称「我拿到了 X」，而正文此前写的是别人在拿 X | 确定 |
| 时间线 | `chapter_age_gap` | 同章两种相对年代，中点相差 ≥8 年 | 疑似 |
| 时间线 | `cross_chapter_year_clash` | 同一实体：一处绝对年份、一处相对年代；有锚点折算后差 >8 → 确定，无锚点 → 疑似 | 确定 / 疑似 |
| 时间线 | `timeline_anchor`（note） | 有相对时间但项目无锚点 → 明确写「无法换算」，**不猜** | 说明 |
| 指代 | `no_antecedent` | 「X 有什么关系 / 是什么…」中的 X 不在前文、角色名、实体名里 | 确定 |
| 指代 | `definite_np_unknown` | 「那张 X / 这个 X」的 X 是名词短语且前文无来源 | 疑似 |
| 伏笔 | `future_clue_leak` | 第 N+1 章及以后大纲的引号线索出现在本章（人名、本章大纲自有内容除外） | 疑似 |
| 伏笔 | `premature_identity` | 大纲仍有「X 是谁」未揭，正文却出现「A 是 B 寄的」这类断言 | 疑似 |
| 伏笔 | `chapter_prohibition` | 本章大纲「不写 / 不解释 / 不引入」的宾语出现在正文 | 确定 |

报告粒度：每条 `Finding` 带 `category / check / level / paragraph（段号，1-based）/ message / quote / evidence`。时间线的 `evidence` **永远是成对的两处原文**（含来自哪一章、第几段）。

### 1.4 判定等级与「只报不拦」

三档：`definite`（形式信号足以定性）、`suspected`（需要人看一眼）、`notes`（判不了，说明原因）。默认配置下**任何一级都不影响 `passed`、不触发重写**，只写进 `quality_json`。

### 1.5 接线与配置

`backend/app/config.py` 新增：

```python
coherence_guard_enabled: bool = True
coherence_guard_mode: str = "report"   # report | rewrite | off
```

- `report`（默认）：只算、只记录；
- `rewrite`：definite 级问题计入 `_rewrite_rounds` 的重写理由（`_coherence_reasons()`）；
- `off`：连计算都跳过。

`engine._commit_segment()` 在 INSERT 前算一次，结果并入 `quality_json["coherence"]`：

```json
"coherence": {
  "version": "coherence-v1",
  "chapter": 2,
  "counts": {"definite": 3, "suspected": 4},
  "findings": [{"category": "speaker", "check": "possession_conflict", "level": "definite",
                "paragraph": 105, "message": "...", "quote": "...", "evidence": [...]}],
  "notes": [{"check": "timeline_anchor", "reason": "..."}],
  "anchors": {"year": null, "source": "plot_bible 未声明年代锚点"}
}
```

`_rewrite_rounds()` 每轮记录新增 `coherence_after`，不改原有 `reasons_after / passed_after`。

### 1.6 兼容性

- `quality_json` 是 TEXT，新增键是**加字段**，不重命名、不改语义；老行缺 `coherence` 照常可读。
- 读取侧 `repetition.parse_quality()` 增加一行 `"coherence": data.get("coherence")`：老数据得到 `null`（未检测），与「检测过且零问题」区分开。
- 现有测试与闸门数值未受影响（见 §2.4）。

---

## 2. 实测（项目 6，第 1、2 章）

数据：`generations.id=25`（第 1 章，5,997 字符）、`id=26`（第 2 章修补后，5,404 字符）；修补前文本由 `patch_ch02.py` 的六组改动**逆替换**还原（5,393 字符）。`plot_bible` 未声明年份锚点，`outline` 共 14 章。

| 文本 | 确定 | 疑似 | 命中类别 |
|---|---|---|---|
| 第 1 章（id=25） | 0 | 3 | 说话人（3 处未加提示语的对话切换） |
| 第 2 章 修补前 | 3 | 4 | 说话人 / 时间线 / 指代 / 伏笔（**四类全覆盖**） |
| 第 2 章 修补后（id=26） | 0 | 1 | 指代（1 处「那张说明书」首次出现即用定指） |

### 2.1 修补前的逐条命中

| 段号 | 类别 | check | 等级 | 报出的问题 |
|---|---|---|---|---|
| 79 | 说话人 | `quote_two_turns` | 确定 | 「我现在补行不行？现在十九点零几，夜间无法核验，只能明早九点。」 |
| 105 | 说话人 | `possession_conflict` | 确定 | 「周德贵」自称拿到东西，但第 54 段写的是「陈小满」抱紧「纸箱」 |
| 109 | 指代 | `no_antecedent` | 确定 | 「水费」全章仅此一处，前文与设定都没有 |
| 116 | 伏笔 | `premature_identity` | 疑似 | 大纲仍有「群主」未揭，正文却写「东西是周德贵寄的」 |
| 132 | 时间线 | `chapter_age_gap` | 疑似 | 「报纸」记作 8 年前，「药」记作 20~29 年前，跨度约 16 年 |
| 132 | 时间线 | `cross_chapter_year_clash` | 疑似 | 第 1 章第 24 段「日期是九八年」 vs 本章「日期印着八年前」，无锚点无法折算 |
| 133 | 指代 | `definite_np_unknown` | 疑似 | 「那张说明书」首次出现即用定指 |

### 2.2 与手工修补的对应关系

| 手工修的点 | 本层是否报出 | 位置 |
|---|---|---|
| 顾青梧段两人对话并进同一对引号 | 是（确定） | P79 |
| 周德贵电话段台词归属颠倒 | 是（确定） | P105 |
| 时间线「八年前 / 二十多年前 / 九八年」 | 是（疑似，两处原文并列） | P132 |
| 指代「水费」无出处 | 是（确定） | P109 |
| 提前点破寄件人 | 是（疑似） | P116 |

修补后同一份文本只剩 1 条疑似（P132「那张说明书」首次出现即定指），四类确定问题全部归零。

### 2.3 没报出来的一处（如实说明）

门卫段那处「陈师傅让我顺路带点东西」被并进保安的引号——**本层未单独报出**。原因：该句没有问号、没有第二人称、也没有与说话人冲突的持物事实，纯形式信号不足以定性；同一段的「周叔上午就搬走了，您不知道？」是正常告知句，强行报会误伤修补后的正文。本层在同一章用 P79、P105 两条确定命中覆盖了「说话人归属」这一类，但这一处仍需人工或语义模型判。

### 2.4 单测与回归

`backend/tests/test_coherence.py`，15 条，全部通过。用**合成数据**（人名「林昭/沈砚」、年份 2003、器物「铜钥匙/契约」）覆盖：并句判定、自问自答不误报、人称对调、跨章相对/绝对年代、有锚点升为确定、无锚点不许造年份、指代有无前文、未来线索提前、本章禁写清单、人名不算线索、称谓从 `voice.address` 的值取、章号解析、输出结构稳定。

```bash
cd backend && python -m pytest tests/test_coherence.py -q
# 15 passed
```

---

## 3. 只读审计：写死「某一本书」的常量与判断

> 只出清单，**未改动**任何 `kg/`、`style/` 文件。

| # | 位置 | 写死的内容 | 应改由什么驱动 |
|---|---|---|---|
| 1 | `app/kg/canon_source.py:826,845` | 「地书碎片」「地书聊天群」被硬编码为「派生 item 实体，不建实体」 | 项目的派生实体白名单（`plot_bible` 或 kg 配置），或由实体归一规则推导，不写具体名词 |
| 2 | `app/kg/generated_store.py:723` | 只有**注释示例**用了本书实体名（「地书碎片」挂组 9）；逻辑本身是查 approved 写法，未写死 | 注释示例改占位；无逻辑改动 |
| 3 | `app/kg/sentinel.py:171-176` | `REALM_SYSTEMS` 写死三套境界阶梯（凡人/炼气/筑基…、黄阶/玄阶…、一星…）作为**默认值** | 已支持 `realm_order` 入参：默认值应改为从 `plot_bible` 的力量体系字段 / 项目体裁配置读取 |
| 4 | `app/kg/extractor.py:82` | 注释里写死语料人物出现次数（「朱银锣38 / 姜律中36 …」） | 注释改为相对描述或删除（不影响逻辑） |
| 5 | `app/style/consistency.py:184,237,287` | 分位阈值表来自「novel 7 全本 11,334 块 / 3,170 章」 | 项目档案（`projects.style_profile`）或参考书重算；换书必须重算并升版本号。**蒸馏侧，按约定不改** |
| 6 | `app/style/narrative.py:752` | 注释以「novel 7 实测」为依据给阈值 | 同上；注释里写清依据即可 |
| 7 | `app/style/narrative.py:1068` | 提示词示例用「宋书航 / 宋师兄」 | 改为占位符（`<角色名>` / `<称谓>`），避免示例本身带书 |
| 8 | `app/style/stylometry.py:57,793` | 注释引用「豆棚闲话 11.2 万字」 | 保留为统计依据说明，但需在换书时重算；不改代码 |
| 9 | `app/generation/power_scale.py:36` | 文档示例「白烬（无面亡者统领）」 | 改为占位示例 |
| 10 | `app/models.py:522,1553` | 注释引用《修真聊天群》3173 章、`novel 7` | 注释改为「参考书 / 参考语料」，不带具体书名 |
| 11 | `app/generation/engine.py:1159,1881` | 「豆棚闲话 11.2 万字，9 个留出单元」阈值依据 | 与 #5 同源：换书需重算留出单元分离度 |
| 12 | `app/kg/export_story.py:10` | CLI 示例 `--title 修真聊天群` | 改为 `<书名>` 占位 |
| 13 | `app/coherence.py` | 本轮已自检：0 处书名/人名/年份/章号（注释里的具体例子已改成占位） | — |

附：`migrations/*/apply_*.py`、`tests/stress/*` 里写死 `PROJECT_ID = 5/1` 属一次性脚本与夹具，允许，但不得进 `app/`。

**优先级**：#1 / #2 / #3 是会改变行为的**逻辑**硬编码（换书后仍在按《修真聊天群》的世界观处理实体），应当最先改为数据驱动；其余是注释/示例/统计依据，属可读性与可维护性问题。

---

## 4. 换一本书时需要提供哪些数据

> 判定所需的一切都能从项目里读到，所以「换书」= 把下面这张表填满。缺哪一项，对应的检查就降级（只报疑似或不报），不会误判。

| 必需 | 项目字段 | 用来做什么 | 缺失后果 |
|---|---|---|---|
| ✅ | `projects.outline`（逐章一条字符串） | 伏笔线索、揭示节点、本章「不写」清单 | 伏笔类全不报 |
| ✅ | `projects.plot_bible.characters[]`：`name`、`aliases`、`voice.address` | 说话人、称谓、持物人 | 说话人只报形式信号（`quote_two_turns`），别名/称谓类判定失效 |
| ✅ | 本章之前的 `generations.text`（至少前 1 章） | 跨章时间线、指代来源 | 跨章检查退化为仅本章 |
| ✅ | `generations.chapter_range`（可解析出「第 N 章」） | 定位「第 N 章才允许出现」 | 伏笔类不报 |
| ✅ | `projects.generation_length` | 长度硬闸（既有） | 用默认 4,000 |
| 建议 | `kg_entities`（`novel_id IS NULL`，本项目实体） | 器物/地点等专名 | 指代只能靠角色名与前文，召回下降 |
| 建议 | `plot_bible` 里的年代锚点（`story_year` / `year` / `era` / `timeline` / `setting`，或全文唯一的 4 位年份） | 相对年代换算绝对年份 | 时间线只报疑似，不报确定 |
| 建议 | `plot_bible.foreshadowing[]`（本项目为空，故线索全部从 `outline` 抽） | 更精确的揭示排期 | 用大纲代替，粒度略粗 |
| 建议 | `plot_bible.characters[].voice` 的 `tics / sentence / under_pressure / sample_lines` | 口吻（既有闸门在用，本层不读） | 与本层无关 |

**写作约定（数据侧的隐式契约）**：大纲里用「不写 X」「不解释 X」「不引入 X」表达禁止项，用「X 是谁 / 是什么 / 为什么」表达待揭问题，用引号或书名号标出关键线索。本层就是按这三种写法抽线索的——不写这些标记，伏笔类会漏报（但不会误报）。

---

## 5. 复现方式

```bash
# 单测（合成数据，证明换书有效）
cd backend && python -m pytest tests/test_coherence.py -q
# 15 passed
```

实跑任意一章（在 `backend/` 下执行，只读、不写库）：

```python
from sqlalchemy import text as sql
from app.db import SessionLocal
from app.coherence import run_coherence
from app.models import Generation, Project

db = SessionLocal()
p = db.get(Project, 6)
prior, seen = [], 0
for g in db.query(Generation).filter(Generation.project_id == 6).order_by(Generation.id):
    if g.id == 26:
        break
    prior.append({"chapter": seen + 1, "text": g.text})
    seen += 1
ents = [r[0] for r in db.execute(sql(
    "SELECT canonical_name FROM kg_entities WHERE project_id=6 AND novel_id IS NULL"))]
res = run_coherence(db.get(Generation, 26).text, plot_bible=p.plot_bible,
                    outline=p.outline, chapter=2, prior_chapters=prior, entity_names=ents)
print(res["counts"], res["findings"][0])
```

落库路径：`generations.quality_json["coherence"]`；接口读取侧：`repetition.parse_quality()` 的 `quality.coherence`（老数据为 `null`）。

---

## 6. 已知边界

1. **语义级说话人错误抓不到**：门卫段那种「把别人的话并进陈述句」没有形式信号（§2.3）。
2. **年代锚点缺失时只报疑似**：不臆测年份，宁可少报。
3. **相对年代是区间**（「二十多年前」→ 20~29 年前，取中点），跨度阈值 8 年是经验值，对同一件旧物才可靠；跨器物只作提示。
4. **人名识别依赖 `plot_bible` / `kg_entities`**：正文里从没登记过的称呼（「陈师傅」这类）认不出，相关判定会漏。
5. **只读不写**：本层不改正文、不改 `passed`；要它参与重写必须显式开 `COHERENCE_GUARD_MODE=rewrite`。
