# KG 重生成路径簿记：清空 + 全量 replay 设计

- **日期**：2026-10-17
- **状态**：设计已定、三个前置风险已用代码证据核实（go/no-go 见 §4）；**实现待派**
- **关联 commit**：`7416114`（段级合并收归唯一入口）、`5ea5e0e`（流式簿记）、`e560338`（KG 三项修复）、`1eb00d7`（蒸馏四项修复）

---

## 1. 背景

生成链路目前已有 KG 簿记闭环（四步：图谱 + Fact 账本 + 别名归一 + 规则哨兵）：

- 非流式 `generate_next_segment` → `_merge_segment_into_graph`（`engine.py:757`）
- 流式 `generate_next_segment_stream` → 同上（`engine.py:854`，`5ea5e0e` 补上）
- 唯一编排入口 `app.kg.build.merge_segment_into_kg`（`build.py:207-301`）

**但重生成路径完全没有簿记**：`regenerate_segment_stream`（`engine.py:923-985`）替换 `target.text/summary` 后只调 `rebuild_summary_tree`，从不触碰 KG。

---

## 2. 已核实缺陷

### 洞 A — 重生成零簿记（`engine.py:970-976`）

重生成第 M 段后：

- **旧正文的事实永久残留**：段 M 旧版本引入的角色 / 坑 / 关系 / 别名 / 哨兵状态仍留在 `KgNode` / `KgEdge` / `Fact` 中，成为幽灵数据；
- **新正文的事实永不入账**：段 M 新版引入的新角色 / 新伏笔不会进入后续段的 `reminders`。

后果：`get_reminders` 注入的锁定角色与实际正文不符；`resolve_plotholes` 的结算依据错位；诊断（diagnosis）报的 open 坑与正文不一致。这是"数据与正文漂移"的**确定性来源**。

### 洞 B — 手动建图端点只做半份 + 段号丢失（`wizard.py:303-315`、`build.py:176-191`）

`POST /{project_id}/build-kg-from-generations` → `build_graph_from_text`：

- 只做 `_clear_graph_by_source` + `_write_result`（**图谱一步**），Fact 账本 / 别名 / 哨兵全不更新 → 与图谱状态漂移；
- `_split_text_to_chunks`（`build.py:157`）按**文本长度**切块并自增伪章节号 `1,2,3…`，因此 `KgNode.first_chapter/last_chapter` 落的是 **chunk 序号而非真实段号** → 段号语义丢失。

（`7416114` 已把"段级合并"收归唯一入口，但这两条**全量重建**路径仍是旧的两步 / 半步实现。）

### 残留清单（重生成段 M 后会陈旧的数据）

| 数据 | 存储 | 陈旧表现 | 可增量删除？ |
|---|---|---|---|
| 角色事实 | `Fact(type=character)` | `mentions/speaking` 为累加值，含旧正文；幽灵角色 | 否（累加值无分段明细） |
| 坑事实 | `Fact(type=plothole)` | `status` / `planted_segment` 基于旧正文 | 否 |
| 关系事实 | `Fact(type=relationship)` | `props.segments` 含旧段号 | 是（`segments` 是列表） |
| 哨兵状态 | `Fact(type=state)` | 生死 / 位置等状态被旧正文推进 | 否（覆盖式，无版本） |
| 告警 | `Fact(type=alert)` | 旧段号的告警残留 | 是（`save_findings` 先清后写） |
| 图谱节点 / 边 | `KgNode` / `KgEdge` | 同上，且别名改写后名字已归一 | 否 |
| 别名归一 | `Fact(type=character).aliases` | 由旧正文的首现时序推出 | 否（但可确定性重算，见 §4-R1） |

---

## 3. 为什么只能「清空 + 全量 replay」

增量删除不可行：`Fact` 的 `mentions/speaking` 是**累加值**（`ledger.py:171-173`），`first/last_segment` 是 **min/max**（`ledger.py:194-200`）——账本里没有"段 M 贡献了多少"的分段明细，无法做减法。

故唯一正确的做法是：**清空 generated 侧全部派生数据 → 按段号顺序把每段重新 merge 一遍**。段 M 旧版本的贡献在新一轮 replay 中被源文本天然取代。

---

## 4. 三个前置风险的 go/no-go 结论（已核实）

### R1 — 清空账本是否丢失不可再生数据？ → **GO（可逆）**

- `Fact.aliases` 落在规范名 Fact 行上（`ledger.py:434-441`），被吸收行删除（`ledger.py:443`）。
- 写入 `aliases` 的唯一路径是 `apply_merges`，其调用者只有 `resolve_aliases`（`ledger.py:572`），而 `resolve_aliases` **仅**被 `merge_segment_into_kg`（`build.py:282`）调用。
- **全仓无任何端点可手动应用 review 档合并**（grep `apply_merges|resolve_aliases`：仅 `build.py` + `test_ledger.py` 命中）。
- `resolve_aliases` 只应用 auto 档，而 `plan_alias_merges` 是**纯函数**（`ledger.py:255`，输出按名字排序、不依赖 dict 迭代序）。auto 档判据（粘连残渣 + 生成时序归一）在 replay 时输入相同 → 输出相同。
- **结论**：generated 侧全部事实均为**机器派生、确定性可重算**，清空 + replay 不丢任何用户输入。
- **清空范围必须限定 `source="generated"`**：user / bible 档事实（若存在）与 `novel:{id}` 参考语料图一律不动。

### R2 — replay 是否可同步执行？ → **GO（全链路无 LLM，毫秒级/段）**

`merge_segment_into_kg` 全链路无 LLM 调用：

- `extract_from_text`（`build.py:194`）→ `RuleBasedExtractor`（规则抽取）
- `merge_result_into_graph` / `upsert_facts` / `resolve_aliases`（DB + 纯函数）
- `audit_segment`（`sentinel.py:1031`）→ `extract_events` / `check_segment` / `apply_event`（规则；`build.py:229` 注释亦声明"零 LLM 硬冲突检查"）

参考性能锚：`build.py:198` 记的"真实语料单次抽取约 16s"是 **120 万字 / 1066 chunk** 的全量；单段约 3000 字 → 2–3 chunk，量级为 ms。**结论**：典型项目（数十段）replay 应在数百 ms ~ 数秒，同步可行。**待实测确认**（见 §8）。

### R3 — 并发安全？ → **需 per-project 锁（GO with design）**

无现成 KG 锁（仅 `app/llm/registry.py:54` 有 LLM 锁可作先例）。风险交错：重生成 replay（含清空）与并发 `generate_next_segment` 的 merge 同时发生 → 一方覆盖另一方。

方案：`threading.RLock` per project（模块级 `dict[project_id, RLock]` + 全局锁守护 dict）：

- replay 全程持锁（读 generations + 清空 + 逐段 merge 都在临界区内）；
- `merge_segment_into_kg` 的变更段持同一把锁；
- 用 **RLock** 是因为 replay 内部要反复调用 merge（同线程需可重入），换线程则串行。

两种合法交错都正确：(replay 先完成 → 新段 merge 增量叠加其上) 或 (新段 merge 先完成 → replay 读取的段列表已含它)。

---

## 5. 方案设计

### 5.1 新增编排函数（`app/kg/build.py`）

```python
def rebuild_generated_kg(db, project_id: int, source: str = "generated") -> dict:
    """重生成 / 手动建图专用：清空 generated 侧全部派生数据，按段号顺序 replay。

    为什么是全量 replay 而非增量：见设计文档 §3（累加值无分段明细，无法做减法）。
    """
    with _project_kg_lock(project_id):
        gens = db.scalars(
            select(Generation)
            .where(Generation.project_id == project_id)
            .order_by(Generation.segment_no)
        ).all()
        _clear_graph_by_source(db, project_id, source)     # 图谱
        db.query(Fact).filter(
            Fact.project_id == project_id, Fact.source == source
        ).delete()                                          # 账本（含 state / alert）
        db.commit()
        replayed, failed = 0, []
        for g in gens:
            if not (g.text or "").strip():
                continue
            st = merge_segment_into_kg(db, project_id, g.text, g.segment_no, source=source)
            replayed += 1
            if st.get("merged") is False:
                failed.append({"segment_no": g.segment_no, "reason": st.get("reason")})
        return {"rebuilt": True, "segments": len(gens),
                "replayed": replayed, "failed": failed, "source": source}
```

要点：

- 清空**必须**先图谱（`_clear_graph_by_source`）再账本，两者都在锁内、同一事务提交前完成，避免中间态被其它请求读到；
- 只删 `source == "generated"`；参考语料图与 user/bible 档不受影响；
- 复用 `_clear_graph_by_source`（`build.py:21`），不另写清空逻辑。

### 5.2 调用点 1 — `regenerate_segment_stream`（`engine.py`）

在 `target.text = text`（`engine.py:970`）之后、`rebuild_summary_tree` 之前插入：

```python
db.commit()              # 关键：新文本必须先落库，replay 读 Generation 才拿到新版
kg_stats = None
try:
    from app.kg.build import rebuild_generated_kg
    kg_stats = rebuild_generated_kg(db, project.id)
except Exception as exc:  # noqa: BLE001 —— 簿记失败绝不打断 SSE（正文已落库）
    logging.getLogger(__name__).warning("重生成 KG replay 失败，正文已落库: %s", exc)
    kg_stats = {"rebuilt": False, "reason": f"{str(exc)[:100]}"}
```

并在 `done` 事件加 `"kg": kg_stats`（与流式路径 `engine.py:876` 的 done 键对齐）。

> ⚠️ 顺序陷阱：`rebuild_generated_kg` 读 `Generation` 表拿各段正文。若 new text 未 commit，replay 读到的是旧文本 → 等于没修。故 `db.commit()` 必须在 replay 之前。

### 5.3 调用点 2 — `build_kg_from_generations` 端点（`wizard.py:303`）

把 `build_graph_from_text(...)` 换成 `rebuild_generated_kg(db, project_id)`，顺带修复"半份"与"段号丢失"。保留原空文本 400 校验。

---

## 6. 失败语义

沿用既有纪律：**簿记失败绝不阻断主流程**（正文已落库）。重生成路径同理——replay 失败 → 记日志 + `done` 事件带 `{"rebuilt": False, reason}`，不抛给 SSE。与 `5ea5e0e` 的流式处理完全同构。

细化（2026-10-17，见 §8.1）：单个**段**的半途失败（账本/哨兵步）不构成整次 rebuild 失败——该段在 stats 里带 `partial: True`，并被计入返回体 `failed`；`complete: bool`（`= failed == []`）整体暴露「是否缺段」。且失败步各自 `db.rollback()`，不留半成品。

---

## 7. 测试计划

| 编号 | 用例 | 断言 |
|---|---|---|
| T1 | 生成 3 段后重生成第 2 段，新文本不含旧角色 | 旧角色从 `KgNode`/`Fact`(generated) 消失 |
| T2 | 重生成后哨兵状态按新文本重建 | 旧状态（如 `dead`）不残留，新状态生效 |
| T3 | replay 段号正确 | `KgNode.first_chapter/last_chapter ==` 真实段号（非 chunk 序号） |
| T4 | 幂等 | 连续两次 rebuild，`KgNode`/`Fact` 计数与内容一致 |
| T5 | 端点收归 | `build-kg-from-generations` 走 rebuild → Fact 与图谱同源、段号正确 |
| T6 | 失败不阻断 | 注入 merge 异常 → `done` 事件仍发出、带 `rebuilt:False`；正文已落库 |
| T7（可选） | 并发 | replay 与 merge 交错不产生幽灵 / 丢失（线程测试） |

测试环境提示：pytest 只在 `backend/venv`；`pytest.ini` 的 `testpaths=../tests`（根 `tests/`）；**全量回归必须加 `--basetemp=D:/Temp/xxx`**，否则沙箱 tmp 回收会造 EXIT=1 假失败。

---

## 8. 未决 / 需拍板

1. **单段 replay 失败语义（✅ 已决，2026-10-17）**：选择「**失败绝不静默 + 失败绝不泄漏半成品**」，**不做事务级整体回滚**。三条保证：

   - **不泄漏半成品**：`merge_segment_into_kg` 的图谱步 / 账本步 / 哨兵步失败时各自 `db.rollback()`，丢弃该步未提交的行。此前三处 except 只吞异常、不 rollback，而第 4 步 `audit_segment` 结尾有 `db.commit()`（`sentinel.py:1059`）——于是「前一步写了一半的 pending 行」会被哨兵步的 commit **静默持久化**（已实测复现）。此修复对**每段生成链路（非流式 + 流式）同样生效**，不限于 rebuild。
   - **失败不静默**：步骤 2/3/4 失败时返回 stats 新增 `partial: True`（步骤 1 失败沿用 `{"merged": False, "reason": ...}`）。`rebuild_generated_kg` 把 `merged is False` **或** `partial` 的段计入 `failed`，并新增整体键 `complete: bool`（`= failed == []`）；`rebuilt` 的既有语义**不变**（= 操作跑过），以不破坏既有消费方。
   - **不中止主流程**：单段失败不打断 replay（与既有「不阻断」纪律一致），仅如实回报。

   **为什么不做 all-or-nothing**：真正的整体回滚需二选一 ——
   (a) 把 `commit` 开关穿透 `merge_result_into_graph` / `upsert_facts` / `apply_merges` / `replace_state_facts` / `save_findings` / `audit_segment` **六个函数**的事务纪律（blast radius 大，且会动到生成链路）；
   (b) 用影子 source（如 `generated__rebuild`）写完后整体 swap —— 但 `plan_alias_merges` 的生成时序归一闸门里有 `if source == "generated"`（`ledger.py:309`），换 source 会**关掉别名自动归并**，破坏已被 QA 独立验证的 R1 性质。
   两者代价都远超收益：本路径是规则抽取 + DB，无 LLM、无网络，现实故障源基本只有数据/约束异常。**故取「失败不静默、不泄漏半成品」，不做事务级原子性。**

   对应测试：`tests/test_regenerate_kg_replay.py` 的 R1（失败步不留 pending 行）、R2（partial→failed→complete:False）、R3（全成功 complete:True）、R4（图谱步失败无残留）。
2. **replay 性能实测**：§4-R2 是量级推断，需一个**真实多段项目**实测 `rebuild` 墙钟时间；若 > 5s 则需后台化（参照 `DistillRun` 模式）或只 replay 受影响范围。
3. **alert 历史瞬时清空**：全清 generated Fact 会带走所有段的告警，随后 replay 逐段重建（`save_findings` 按段先清后写）→ 最终一致；但 rebuild 窗口内告警列表瞬时为空。单机单用户场景可接受，若并发读需注意。

---

## 附：证据索引

| 结论 | 证据 |
|---|---|
| 重生成零簿记 | `engine.py:923-985`（无 KG 调用） |
| 手动端点半份 + 段号丢失 | `wizard.py:314` + `build.py:157,176-191` |
| 四步唯一入口 | `build.py:207-301` |
| 累加语义不可减 | `ledger.py:171-173,194-200` |
| aliases 落库位置 | `ledger.py:434-443` |
| 无手动应用 review 合并的路径 | grep `apply_merges|resolve_aliases` → 仅 `build.py`+`test_ledger.py` |
| 别名规划确定性 | `ledger.py:255-382`（纯函数 + 排序输出） |
| 全链路无 LLM | `build.py:194-301`、`sentinel.py:1031-1067` |
| 清空图谱现有实现 | `build.py:21-32` |
| 状态 / 告警同为 Fact | `sentinel.py:764-794,1056-1058` |
| 锁先例 | `llm/registry.py:54-56` |
