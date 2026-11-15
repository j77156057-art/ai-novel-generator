# 信息载体从 `kind='item'` 分离（2026-11-10）

## 结论

| 项 | 结果 |
| --- | --- |
| 判据 | `backend/app/kg/info_carrier.py`，形态规则 `R1–R5` + `R0` 默认，不硬编码具体物品名 |
| 存量 | 51 个 `kind='item'`：**会改 3 / 待审 0 / 不动 48** |
| 影响面 | 3 条全部是**项目 1** 的无组、无持有行实体 → 按约定直接执行 |
| 闸门 | 在 `batch_worker._persist_extraction`（判定侧）；`engine._item_holding_text` 一行未动 |
| p1 注入块 | 「文件 / 邮件 / 策划书」消失；「笔 / 咖啡 / 手机 / 电脑」保留 |
| p5 读结果 | 15 项逐字节不变（`p5_block` 前后 1040 字符，`p5_facts` 全等） |
| p7 读结果 | 逐字节不变 |

---

## 1. 判据

### 1.1 为什么要单列

判据回答的是一个**形态问题**，不是「这几个名字要不要改」：

> 一个名字能不能脱离「拿在手上、能丢失、能转交的物理对象」而成立？
> 能 → `item`；不能（它的全部就是信息）→ `info`；两可 → `review`，不自动改。

### 1.2 规则

| 规则 | 判据 | 结论 | 例子 |
| --- | --- | --- | --- |
| `R3_container` | 名字是「装东西的实物」（长词优先，先于其它规则） | `item` | 文件夹、档案袋、信封、公文包 |
| `R1_electronic` | 电子/数字载体，没有可拿在手上的实体 | `info` | 邮件、电子邮件、短信、私信 |
| `R2_collective` | 集合/不可数名词，指一堆信息而非一个物件 | `info` | 资料、档案、数据、信息、文档 |
| `R5_document_product` | 成篇的信息产品，内容即其全部 | `info` | 策划书、报告、合同、名单、清单 |
| `R4_paper_individual` | 可拿、可丢、可转交的纸质单件 | `item` | **信件**、书信、便条、卷轴、日记本 |
| `R2` + 语境 | 两可词有「一叠/堆满/递交/汇报」等载体证据 | `info` | 一叠厚厚的**文件** |
| `R3` + 语境 | 两可词附近出现「文件夹/档案袋/柜子」等实物反证 | `item` | 打开**文件**夹 |
| `R0_default` | 形态无信息载体特征 | `item` | 笔、咖啡、手机、电脑、丹药、凭证、镇国剑 |

### 1.3 三个边界的处理

- **「信件」**：命中 `R4_paper_individual` → `item`。它和「邮件」不同，是能转交的纸。
- **「邮件」**：命中 `R1_electronic` → `info`。现代语境里是电子邮件的集合，不是实物。
- **「文件」**：形态本身 → `review`（可能是纸堆，也可能是实物档案夹），**不硬改**；
  只有语境能证明是成叠载体内容时才升 `info`。项目 1 的两条 mention 正好都有证据：
  - seg1「手中拿着一叠厚厚的**文件**」→ 证据词「一叠」
  - seg2「桌上堆满了**文件**和资料」→ 证据词「堆满」

  这也是本判据**唯一**使用语境的地方；`笔/咖啡/手机/电脑` 走 `R0`，完全不看语境。

> 判据不依赖这七个名字：测试里用未出现过的「镇国剑 / 丹药 / 凭证 / 书信 / 文件夹」
> 验证了同一套规则。

---

## 2. 存量清单（迁移前）

### 会改（3）

| 实体 | 项目 | mentions | 组 / 持有 / 旁注 | 判据 |
| --- | --- | --- | --- | --- |
| `id=56 文件` | 1 | 2（seg1、seg2） | 无 / 无 / 无 | `R2` + 语境「一叠」「堆满」→ `info` |
| `id=57 策划书` | 1 | 2（seg1、seg2） | 无 / 无 / 无 | `R5` 成篇信息产品 → `info` |
| `id=62 邮件` | 1 | 1（seg2） | 无 / 无 / 无 | `R1` 电子载体 → `info` |

### 待审（0）

无。`文件` 的形态判定是 `review`，但项目 1 的语境证据充分，已按规则升为 `info`；
清单里保留上述语境原文，供复核。若换一本书、`文件` 没有任何语境支持，
脚本会把它留在 `待审` 档，不改库。

### 不动（48）

| 范围 | 数量 | 原因 |
| --- | --- | --- |
| 项目 1 真物品：笔、咖啡、手机、电脑 | 4 | `R0_default` → `item` |
| 项目 5 | 43 | 全部有组/持有行（硬约束）；判据也均为 `item` |
| 项目 7 镇国剑 | 1 | `R0_default` → `item` |

> 项目 7 的 `id=68 镇国剑` **自身没有组行**（组 100 属于项目 5 的 `id=26 镇国剑`），
> 读通道靠「组的 `project_id`」把它解析过来。它被判为 `item` 且未改，p7 注入块
> 前后逐字节一致。

### 被硬约束拦下的「本应改」

无。3 条会改候选全部 `groups=[] hold=[] story=[]`，脚本执行前还会再断言一次；
任何命中的候选会直接 `SystemExit`，不会写库。

---

## 3. 执行、快照与回滚

- 迁移脚本：[apply_info_carrier.py](backend/migrations/2026-11-10-info-carrier-v1/apply_info_carrier.py)
- 快照：`backend/migrations/2026-11-10-info-carrier-v1/snapshots/info_carrier_20261110-225639.json`
- 数据库备份：`backend/backups/novel_dev-pre-info-carrier-20261110-225639.db`（131,416,064 字节）
- 执行命令：

```powershell
python backend/migrations/2026-11-10-info-carrier-v1/apply_info_carrier.py --dry-run
python backend/migrations/2026-11-10-info-carrier-v1/apply_info_carrier.py
```

- 回滚命令：

```powershell
python backend/migrations/2026-11-10-info-carrier-v1/apply_info_carrier.py `
  --rollback backend/migrations/2026-11-10-info-carrier-v1/snapshots/info_carrier_20261110-225639.json
```

回滚已在**副本**上实测：3 条 `info` 全部恢复为 `item`，项目 1 的 item 数从 4 回到 7。

迁移只 `UPDATE kg_entities.kind`，不碰 `kg_item_group` / `kg_item_surface` /
`kg_item_holding` / `kg_holding_story_segment`，也不删 `kg_mentions`
（文件 2 条、策划书 2 条、邮件 1 条全部保留，只是不再作为物品进入注入链）。

---

## 4. 判定侧闸门

新增 [`info_carrier.py`](backend/app/kg/info_carrier.py)，
并在 [`batch_worker.py`](backend/app/kg/batch_worker.py) 的
`_persist_extraction` 里、`canon_source.normalize_extraction` 之后调用
`_reclassify_info_carriers`：

1. 只处理 `kind == 'item'` 且判据为 `info` 的名字；
2. **本项目已有同名 item 实体时一律跳过**——这行可能挂着组/持有行，
   改 kind 会打断读通道；
3. 只改内存里的 `result["entities"][i]["kind"]`，随后照常 upsert；
4. 不读 `generated.db`（热路径不跨层 JOIN），保护完全靠第 2 条。

注入侧未改：`engine._item_holding_text` → `current_item_holdings` 的口径保持上一轮
定稿的版本；`item_holding_read.py` 本轮零改动。

---

## 5. 验证

### 5.1 项目 1 注入块（原文）

| 段 | 迁移前 | 迁移后 |
| --- | --- | --- |
| seg1 | `- 文件、策划书、笔：均无持有记录（no_record；…）` | `- 笔：无持有记录（no_record；…）` |
| seg2 | `- 文件、策划书、笔、咖啡、手机、电脑、邮件：均无持有记录（no_record；…）` | `- 笔、咖啡、手机、电脑：均无持有记录（no_record；…）` |

- 「文件 / 邮件 / 策划书」不再出现；
- 「笔 / 咖啡 / 手机 / 电脑」全部保留。

### 5.2 项目 5

同一组 15 个物品名（镇国剑、地书、神剑、黑金长刀、三号碎片、玉石小镜、法器铜锣、
银枪、金牌、腰牌、长枪、铜锣、镇国宝剑、洛玉衡祖传神剑、不存在的物品）前后对比：

- `p5_block`：**全等**（1040 字符）；
- `p5_facts`：**全等**（15 项，逐条结构对比）。

### 5.3 项目 7

`p7 seg1` 前后全等：

```
- 镇国剑：权属（own）归镇北王（99–106）；权属（own）归元景帝（106–∞）
```

### 5.4 测试

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_info_carrier.py tests/test_kg_item_holding_read.py tests/test_item_generic.py -q` | **28 passed** |
| `python tests/test_e2e_journey_pipeline.py` | **19 通过 / 0 失败** |
| `python tests/test_batch_worker_observability.py` | **37 通过 / 0 失败** |
| `python tests/test_v30_integration.py` | **32 通过 / 1 失败**，仍是已知的 `rhythm top1 是林悠然`，与本次无关 |

新增测试覆盖：电子载体、成篇信息产品、纸质信件、容器优先、`文件` 的两可判定、
真物品不误伤、以及「已存在 item 行时闸门不改」。

---

## 6. 代码改动

| 文件 | 改动 |
| --- | --- |
| `backend/app/kg/info_carrier.py` | 新增判据（词表 + 规则 + 三档 + `gate_kind`） |
| `backend/app/kg/batch_worker.py` | 新增 `_reclassify_info_carriers`，在 `_persist_extraction` 接入 |
| `backend/app/models.py` | `kind` 注释补 `info` |
| `backend/tests/test_info_carrier.py` | 新增判据 + 闸门测试 |
| `backend/migrations/2026-11-10-info-carrier-v1/` | 迁移脚本、快照目录（回滚同脚本 `--rollback`） |

`extractor_v3.VALID_ENTITY_KINDS` 保持六类不变——LLM 的 kind 契约没有变，
`info` 只由判定侧产生。下游 `KIND_ORDER` 对未知 kind 本就排在末位，无需改。

---

## 7. 遗留与注意

1. **闸门不读 `generated.db`**：若未来出现「有组行、却没有同名 item 实体」的异常登记，
   闸门无法感知。当前所有组都由 item 实体登记而来，这个前提成立；真出现时由登记入口
   或下一次盘点发现。
2. **待审档目前为空**：判据保留 `review` 出口；换书后 `文件/卷宗/材料/记录`
   在没有语境支持时会落在这里，脚本不改库。
3. `test_v30_integration` 的 `rhythm top1` 失败按用户要求不处理；项目 7 的 seg2
   仍为 running，本轮未触碰。
