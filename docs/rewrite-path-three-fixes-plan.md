# 重写路径三项修复 · 实施计划

## Context

端到端验证暴露三个问题，全部集中在助手重写路径（`regenerate_segment_stream` + `finalize_snapshot`）：

- **A（真 bug）**：重写后 KG 全量 replay 读到旧正文。根因已定位：`finalize_snapshot` 用
  `UPDATE ... synchronize_session=False`，`SessionLocal` 是 `expire_on_commit=False`；重写分支
  `db.commit()` 后没有刷新 `target`，紧接着 `rebuild_generated_kg(db, ...)` 的
  `select(Generation)` 直接命中 identity map 里的旧对象；`db.refresh(target)` 在 replay **之后**
  才执行。旧路径（`snapshot_id is None`）直接赋值内存对象，所以没有这个问题。
- **B（工程兜底）**：用户指令只限定局部（如「收拾段尾」），模型整段重写，系统零感知。
- **C（采样缺口）**：`record_segment_check` 只挂在 `_commit_segment`，重写走 `finalize_snapshot`
  直写，文风影子永远采不到样本。

目标：三处修复各自落地、可验证；`scope_mismatch` 只提示、不拦截、不自动重试；只动 p5 段 2，
不提交、不推远程。

## A. 修复 replay 读旧正文

**文件**：[engine.py](backend/app/generation/engine.py)

1. **先取证（临时探针）**：在 `rebuild_generated_kg` 调用前插入临时日志，打印两路读取结果：
   - `db.scalars(select(Generation)...)` 的该段正文前 40 字（= replay 实际会读到的 ORM 值）；
   - `db.scalar(select(Generation.text)...)` 的同一段前 40 字（= 数据库真实值）。
   用临时 pytest（`tests/_tmp_probe_replay.py`，跑完删除）在独立测试库上跑一次快照式重写，
   确认「ORM=旧正文 / DB=新正文」。同时断言 facts 里出现旧正文独有句、不出现新正文独有句。
2. **修复**：在快照分支 `db.commit()` 之后、`rebuild_generated_kg` 之前加
   `db.refresh(target)`，并补注释说明 identity map / `synchronize_session=False` 的原因。
   该 refresh 同时修好紧随其后的 `rebuild_summary_tree`（它也 `select(Generation)` 读摘要）。
3. **复验并清理**：重跑临时探针，预期 ORM 与 DB 都是新正文，facts 出现新正文独有句、
   不出现旧正文独有句。删除临时探针日志与临时测试文件。
4. `done` 事件仍带 `kg` 统计；保留末尾现有的 `db.refresh(target)`（无害，保证摘要树提交后的最终态）。

## B. 局部指令范围校验（不阻断）

**新增文件**：`backend/app/generation/scope_guard.py`（纯函数，便于单测）

- `declared_local(instruction)`：指令命中任一限定词即视为「用户声明了局部」。
  关键词与理由：
  - `只` / `仅` / `保留` / `不要动` / `别动` / `不改`：显式限定改动范围或要求保留原内容；
  - `结尾` / `段尾` / `开头` / `最后一句` / `第一句` / `第N句` / `某处` / `局部` / `此处` / `这一句`：
    以位置/句子粒度声明局部。`只/仅` 单字误命中（如「不仅」）并不可怕——它必须和
    「前后缀都很短」同时成立才提示，而那种情况下提示本身是合理的。
- `detect_scope_mismatch(instruction, before, after) -> dict | None`
  - 仅当 `instruction` 命中限定词、`before`/`after` 均 ≥ 100 字时判定
    （`SCOPE_MIN_CHARS = 100`：不足百字属碎片/测试段，比例失真）；
  - 公共前缀 `<= 20` 且公共后缀 `<= 20` 视为「整段重写」。
    `SCOPE_PREFIX_MAX = SCOPE_SUFFIX_MAX = 20` 的依据：中文小说平均句长 25–30 字，
    20 字不足一个短句；**不用 0**——模型常保留开头几个字/结尾标点，20 能覆盖这种「伪局部」，
    又能放过真正保留整段开头的局部改写（那种前缀通常是几十到几百字）。
  - 返回字段：`declared_local`、`common_prefix_chars`、`common_suffix_chars`、
    `before_chars`、`after_chars`、`chars_delta`、`thresholds`、`message`（一句人话）。
  - 前后缀按非重叠方式计算（后缀最多算到前缀结束处），避免超短文本重复计数。

**落点**：

1. `engine.regenerate_segment_stream`：取目标段时先保存 `_before_text = target.text or ""`；
   在最终 `text`（含时代自检后）确定后调用 `detect_scope_mismatch(instruction, _before_text, text)`。
2. `done` 事件新增 `"scope_mismatch": <dict|None>`。
3. `snapshot.finalize_snapshot` 增加可选参数 `scope_mismatch=None`，与 `applied` 同事务写入
   快照行（新列 `generation_snapshots.scope_mismatch JSON`）。
4. `assistant_generation.py` 的重写响应新增 `"scope_mismatch": done.get("scope_mismatch")`。
   **不改 `answer`、不重试、不拒绝落库**；前端无现成消费位，本次不动前端。

**列与迁移**：

- `models.py::GenerationSnapshot` 增加 `scope_mismatch = Column(JSON, nullable=True)`；
- `db.py::_migrate_sqlite_columns` 迁移清单追加该列（重启自动补列）；
- `migrations/2026-11-11-rewrite-snapshot-v1/apply_generation_snapshots.py`：
  `EXPECTED_COLS`、`CREATE_TABLE_DDL` 追加该列；自动 ADD 白名单从 `LEASE_COLS` 扩为
  `AUTO_ADD_COLS`（租约三列 + `scope_mismatch`），并更新注释；
- `snapshot.snapshot_to_dict` 输出该字段。

## C. 文风影子接入重写路径

**文件**：[engine.py](backend/app/generation/engine.py)

在重写落库、replay、摘要树重建、最终 `db.refresh(target)` 之后、`yield done` 之前：

```python
try:
    from app.style.consistency import record_segment_check
    record_segment_check(db, project, target)
except Exception:
    logging.getLogger(__name__).warning("style shadow check skipped", exc_info=True)
```

必须放在最终 refresh 之后，保证影子采到的是**新正文**；双层 try/except 与 `_commit_segment`
同口径，校验器任何故障都不影响重写成功。

## 测试（`tests/test_rewrite_snapshot.py` 追加）

1. **A**：`test_rewrite_replay_uses_new_text`
   - 旧正文含独有疑问句「你还能保护你的手下吗？」，新正文含另一独有疑问句；
   - 重写后查 `Fact(type='plothole', source='generated')`：新句 key 出现、旧句 key 不出现；
   - 断言 L1 摘要为本次新摘要（证明 `rebuild_summary_tree` 也读到新摘要）。
2. **B**：`test_scope_mismatch_unit`（纯函数：关键词、前后缀、阈值、非阻断语义）
   与 `test_scope_mismatch_in_done_and_response`（引擎 done + 快照行 + HTTP 响应字段）。
3. **C**：`test_rewrite_writes_style_shadow`
   - 重写前记录 `StyleCheck` 条数，重写后新增恰好 1 条且 `verdict='shadow'`、
     `text_md5 == md5(新正文)`；
   - monkeypatch `record_segment_check` 抛异常，重写仍成功落库（证明确实吞异常）。

先跑 `pytest tests/test_rewrite_snapshot.py -q` 全绿，再跑与文风/KG 相关的既有测试。

## 真实验收（严格按用户清单）

1. 重启一次后端，记录重启前 pid、重启后 pid、`/healthz` 的 `build_time` / `fresh`。
2. 对 **p5 段 2** 做一次真实重写（局部限定指令，触发 `scope_mismatch`）：
   - 改前备份该段 text/summary 的 md5；
   - 取证快照行 `applied`、`before_hash/after_hash`、租约列；
   - `style_checks` 新增一条（改前/改后计数差 = 1）；
   - KG replay 后 facts 含新正文独有内容、不含旧正文独有句；
   - 响应与 done 事件含 `scope_mismatch` 具体数字。
3. 回滚本次重写：`generations.text` md5 与改前备份逐字一致、summary 一致、回滚快照行正常。
4. 守卫复核：`kg_mentions=34`、gid45 五个写法 47/4/1/2/76 并集 125 章、六探针、11 条持有行可解析。
5. 失败即停，如实报告状态；不提交、不推远程，除 p5 段 2 的重写/回滚外不做其他写操作。

## 交付摘要

明确写清：`scope_mismatch` 是**提示而非拦截**——不重试、不拒绝落库，只为统计模型服从度留痕；
A/C 为实际修复，A 附带「replay 读到新正文」的前后对照证据。
