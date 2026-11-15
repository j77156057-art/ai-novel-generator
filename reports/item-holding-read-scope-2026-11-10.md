# 物品持有读取口径（a 修订版）落地与重验 — 2026-11-10

## 结论

| 验收项 | 结果 |
|---|---|
| 项目 7 从 `no_record` 读到真实持有 | **通过**：`镇国剑：权属（own）归镇北王（99–106）；权属（own）归元景帝（106–∞）` |
| 项目 5 读结果一字不变 | **通过**：15 个物品逐条对比改前基线，差异 **0** |
| `project_id=None` 行为明确 | **通过**：抛 `ValueError`，不再静默跨项目合并后判 `ambiguous` |
| `flush_project` 契约 | **通过**：worker 已在处理时，flush 等待该批结束再排干剩余（实测 1.10s = 0.6s 等 worker + 0.6s 自己排干） |
| 数据 | 未改任何数据。`generated.db` 6 张表与 `novel_dev.db` 的 generations / kg_entities / kg_mentions / agent_runs 行数均与改前一致 |
| 历史数据修复 | **未动**（按要求压着） |

---

## 1. 口径落地（三条）

### 1.1 正主判定 = 谁拥有 `kg_item_group` 行

`_load_registry` 现在把 `g.project_id` 一并读出，写进每个组字典：

```python
groups[gid] = {"id": gid, "project_id": row["project_id"], ...}
```

归属不再由 `reference_novel_ids` 的交集推断，而是组的 `project_id` 本身。
`reference_novel_ids` 只出现在 `_family_project_ids()` 里，作用仅限于**枚举候选项目**。

### 1.2 实体解析锁在组的 `project_id` 里

`_resolve_entities(db, primary_project_id, scope_projects, items, registry)` 先按
「调用方原话 → approved 写法 → 组 → 组的 project_id」求出目标项目，再只在该项目里
取实体：

```python
def _target_projects(name):
    surf = registry["by_surface"].get(name)
    if surf and surf["status"] == "approved":
        return {registry["groups"][surf["group_id"]]["project_id"]}
    return {primary_project_id}      # 查不到组才回退本项目
```

项目 7 查「镇国剑」→ 写法命中组 100（project_id=5）→ 只在项目 5 的实体里取到 26，
项目 7 自己的实体 68 不再参与，`len(entity_ids) > 1` 的歧义分支不会被触发。

### 1.3 `project_id=None` 明确报错

```python
if project_id is None:
    raise ValueError("current_item_holdings 必须传 project_id：……")
```

产线调用点 `engine._item_holding_text` 一直传 `project.id`，不受影响；读通道本身
（`_load_registry(path, None)`）仍保留「不过滤」能力，仅供人工诊断，不挂在生成侧。

### 1.4 附带：同名跨组/跨项目如实报歧义

`_load_registry` 新增 `conflicts`：同一个名字在多个组登记（含跨项目）时，
`_build_units` 直接给 `ambiguous` 并列出冲突组名，不挑、不猜。当前库里该集合为空
（53 个组的规范名与 approved 写法无重复），属于给未来多语料项目留的护栏。

### 1.5 查询次数

- 本项目自己拥有组行（项目 5，产线热路径）：**登记表 1 次**，与改前相同。
- 本项目无组行（项目 7）：先 1 次空查询，再枚举候选项目，再 1 次候选登记表查询。
  这是冷路径，且仍为常数次，不随物品数量增长。

---

## 2. 重验证据

### 2.1 项目 7 的注入块（改前 → 改后）

项目 7 现有 mentions 只在 seg1（`镇国剑`，entity 68）。`_item_holding_text(db, project7, 1)`：

改前：
```
- 镇国剑：无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）
```
改后（**一字不改**）：
```
- 镇国剑：权属（own）归镇北王（99–106）；权属（own）归元景帝（106–∞）
```

seg2 的 `_active_item_entries` 为空（项目 7 的 seg2 抽取行仍卡在 `running`，没有
mention），所以 seg2 块为空——这是抽取侧遗留，不是读通道作用域问题。

明细核对：

| 持有者 | hold_type | valid_from | valid_to | reason | evidence |
|---|---|---|---|---|---|
| 镇北王 | own | 99 | 106 | gift | 当年元景帝请出神剑，赠予镇北王的往事 |
| 元景帝 | own | 106 | ∞ | transfer | 一柄三尺长的铜剑破水而出，飞入元景帝手中 |

### 2.2 项目 5 逐条对比

对 15 个物品（镇国剑 / 地书 / 神剑 / 黑金长刀 / 三号碎片 / 玉石小镜 / 法器铜锣 /
银枪 / 金牌 / 腰牌 / 长枪 / 铜锣 / 镇国宝剑 / 洛玉衡祖传神剑 / 不存在的物品）
比较 `format_holding_facts` 输出：

```
项目 5 差异项数: 0 []
```

### 2.3 `project_id=None`

```
ValueError: current_item_holdings 必须传 project_id：读通道不允许跨项目合并同名候选
（项目 5 与项目 7 都有「镇国剑」时会从 no_record 变成 ambiguous）。
全库诊断请直接查库，不要走生成侧读通道。
```

---

## 3. `flush_project` 契约

改动在 `backend/app/kg/batch_worker.py`：

- 新增模块级 `_run_gate = threading.RLock()`；
- `_run_batches(project_id)` 变成「持闸门 → 调 `_run_batches_locked`」的薄壳，
  worker 与 flush 共用同一把闸门；
- `flush_project` 的 docstring 把契约写死：**返回 = 队列已空且没有在飞的批**。

`_enqueue_v3_extraction` 自起线程的行为**未改**（生产靠它）。

验证（monkeypatch 掉真正的抽取，只测互斥时序）：

```
calls: [('start', 0.0), ('end', 0.6), ('start', 0.6), ('end', 1.2)]
flush 耗时: 1.10s
结论: PASS 等到了 worker 的批
```

即：flush 在 0.0s 被调用，没有秒返回，而是等 worker 的批在 0.6s 结束后，再自己
排干一次（0.6s），整体 1.10s。

多进程重复入队只在 `recover_running` 的 docstring 里留了 `TODO(多进程，今天不触发)`，
未改逻辑。

---

## 4. 改了哪些文件

| 文件 | 改动 |
|---|---|
| `backend/app/kg/item_holding_read.py` | 新增 `_reference_ids` / `_family_project_ids`；`_resolve_entities` 改为「按组的 project_id 解析」；`_load_registry` 支持项目集合、组带 `project_id`、新增 `conflicts`；`_build_units` 处理 `conflicts`；`current_item_holdings` 禁止 `project_id=None`、热路径仍 1 次登记表查询；模块 docstring 补读作用域 |
| `backend/app/kg/batch_worker.py` | 新增 `_run_gate`；`_run_batches` 变闸门薄壳 + `_run_batches_locked`；`flush_project` 契约写入 docstring；`recover_running` 加多进程 TODO |

未改：`engine._enqueue_v3_extraction`、任何写库逻辑、任何历史数据。

---

## 5. 测试

```
python -m pytest tests/test_kg_item_holding_read.py tests/test_item_group_read.py tests/test_item_generic.py -q
34 passed
```

脚本型用例（pytest 收集会因 `sys.exit` 报 INTERNALERROR，需单独跑）：

```
python tests/test_batch_worker_observability.py   → 37 通过, 0 失败
python tests/test_e2e_journey_pipeline.py         → 19 通过 / 0 失败
python tests/test_v30_integration.py              → 32 通过 / 1 失败
```

`test_v30_integration.py` 的唯一失败项是 `rhythm top1 是林悠然`（kg rhythm 端点
排序，`expected 顾辰逸`），与本次改动的读通道 / flush 无关；但未在改动前复跑该
脚本，不能断言它是既有失败。

---

## 6. 待办（未做）

1. **历史数据修复**：按用户要求继续压着，等本轮重验结论确认后再派。
2. 项目 7 的 seg2 抽取行仍 `running`（旧 harness 遗留），因此 seg2 没有 item mention；
   下次启动 `recover_running` 会自动重捞。
3. 多进程 `recover_running` 重复入队：仅注释，未改。
