# 任务 C · 总字数字段 + 节拍规划

> 状态：可独立 worktree 开工。**基线 HEAD = `e5e17a3`**
> 工作目录：`/c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/`
> 验收目标：`verify_acceptance.py` C 段新增 C3 PASS → 15/0 + `test_p0_regression.py` 55/55 + `test_ledger.py` 40/40
> 派单对应：v2 文档 §任务 C

---

## 1. 背景与根因

- `Project.generation_length`（`backend/app/models.py:52`）只存"单次生成字数"，**没有目标总字数**
- `build_user_prompt`（`backend/app/generation/prompts.py:173`）只传 `max_chars`（单次字数）→ 续写时模型不知道"整本要写多少字"
- `build_bible` 的 `synopsis` 字段提示词要求 "300 字内"（line 199）→ 长篇 N 段根本装不下
- 结果：模型按当前段孤立续写，写到后面没东西写 / 信息密度膨胀

### 1.1 单位契约（v2 明确）

- `target_total_chars`：**字**（不是万）
- 用户界面展示 "X 万字" → 写入时 × 10000
- N = ceil(target_total_chars / generation_length)
- 日常比例：15%（"人写"经验）
- 转折/高潮比例：20%
- **不破坏**：前情 `compose_prior` 预算裁剪逻辑（这是另一条独立管线）

### 1.2 不要做

- ❌ 不动 `app/kg/extractor.py`（E 独占）
- ❌ 不动 `app/kg/queries.py`（F 独占）
- ❌ 不动 `app/kg/build.py`（F 独占）
- ❌ 不动 `app/routers/wizard.py::build_bible`（B 独占）—— 只能**读** `proj.plot_bible`
- ❌ 不动 `app/routers/projects.py`（B 已有 mutations；本任务不用它）
- ❌ 不动 `app/generation/prompts.py::build_system_prompt`（D 独占）
- ❌ 限制 `build_bible` 提示词的字数（**v1→v2 修正**：v1 说 "≤ 800 字" 会卡 N>20 的长篇，v2 取消字数限制，让模型按需展开）

---

## 2. 改动清单

### 改动 1 · `backend/app/models.py` `Project` 表新增字段

在 `generation_length`（line 52）之后插入：

```python
target_total_chars = Column(Integer, nullable=True)  # 字；None = 未指定，不强求规划
```

### 改动 2 · `backend/app/db.py::_migrate_sqlite_columns` 列表加

line 53-64 的 `migrations` 列表里追加：

```python
("projects", "target_total_chars", "INTEGER"),
```

**纪律**（spec §1.2）：新列只能加到 `models.py` + `db.py` 列表这 2 处。

### 改动 3 · `backend/app/generation/prompts.py::build_user_prompt` 节拍块

**当前签名**（line 173-180）：
```python
def build_user_prompt(
    prior_summaries, rag_context, outline, max_chars,
    plot_bible=None, premise=None, prior_blocks=None,
) -> str:
```

**新签名**（**追加 3 个 keyword-only 参数**）：
```python
def build_user_prompt(
    prior_summaries, rag_context, outline, max_chars,
    plot_bible=None, premise=None, prior_blocks=None,
    *,                          # ← keyword-only 分隔
    target_total_chars: int | None = None,
    segment_count: int = 0,
    segment_no: int | None = None,
) -> str:
```

**实现**：在 `directive` 变量定义处（line ~219）改为：

```python
# —— 节拍指令（E-任务）——
if target_total_chars and target_total_chars > 0:
    n_total = max(1, -(-target_total_chars // max_chars))  # ceil div
    pct = (segment_count / n_total * 100) if n_total else 0
    daily_chars = int(max_chars * 0.15)  # 日常段 15% 配额
    if segment_count == 0:
        # 首段，无前置节拍
        directive = (
            f"请续写约 {max_chars} 字（首段）。"
            f"整本目标 {target_total_chars} 字 / 共约 {n_total} 段 / 留 15% 给日常/换幕，"
            f"留 20% 给冲突升级/转折。本段为基础设定/开局钩子，节奏可慢。"
        )
    else:
        # 续写段，按节拍给提示
        kind = (
            "主线推进" if pct < 65
            else ("冲突升级/转折" if pct < 85 else "高潮/收束")
        )
        directive = (
            f"请续写约 {max_chars} 字（{kind}段）。"
            f"整本目标 {target_total_chars} 字 / 共约 {n_total} 段，"
            f"已写 {segment_count} 段（{pct:.0f}%），"
            f"本段为第 {segment_no or segment_count+1} 段。"
            f"建议本段留 {daily_chars} 字给日常/换幕（按既定 15% 比例）。"
        )
else:
    # 旧路径：未指定 target，回退
    directive = f"请续写约 {max_chars} 字，风格与节奏以系统提示中的【风格】与【文风要求】为准。"
```

**关键**：
- `*-(-x//y)` 是 Python 向上取整的惯用法
- `pct >= 65` 是"主线推进末段"，`>= 85` 是"高潮/收束"——经验值
- **`target_total_chars=None` 走旧路径**——零破坏（baseline 14/0 不退化）

### 改动 4 · `backend/app/generation/engine.py::generate_next_segment` 透传

`engine.py:386` 附近 `user = build_user_prompt(...)` 调用改为：

```python
user = build_user_prompt(
    ctx["prior_summaries"], ctx["rag_context"], project.outline,
    max_chars, project.plot_bible, project.premise,
    prior_blocks=ctx["prior_blocks"],
    target_total_chars=project.target_total_chars,
    segment_count=count_already_written(db, project.id),  # ← 新建 helper
    segment_no=next_segment_no_estimate,  # ← 紧跟现有 _next_segment_no
)
```

**注意**：
- `count_already_written(db, project.id)` 实际就是 `len(db.scalars(select(Generation.id).where(Generation.project_id == project.id)).all())`
- `next_segment_no_estimate` 用现有的 `_next_segment_no(db, project.id)`
- **必须用 `count_already_written` 而非 `len`**，因为 `db.flush()` 之后才能拿到正确 ID

`generate_next_segment_stream`（`engine.py:415`）同样改。

### 改动 5 · `backend/app/routers/wizard.py::build_bible` synopsis 字段去字数限制

**当前**（line 199）：
```python
'"synopsis": 完整剧情梗概(300字内), '
```

**改为**（去 300 字限制，加节拍说明）：
```python
'"synopsis": 完整剧情梗概（按本项目 target_total_chars 决定详略；N 段表后面给出）, '
```

并在该 sys_p 后**追加节拍指令**（与 v1 派单一致）：

```python
target = proj.target_total_chars
gen_len = proj.generation_length or 3000
if target and target > 0:
    n_total = max(1, -(-target // gen_len))
    daily_n = int(n_total * 0.15)
    twist_n = int(n_total * 0.20)
    main_n = n_total - daily_n - twist_n
    sys_p += (
        f'\n- 节拍规划：整本目标 {target} 字（{target//10000} 万）/ 单段 {gen_len} 字 / '
        f'共 N={n_total} 段。留 {daily_n} 段写日常/补世界观/换幕（缓解紧绷），'
        f'留 {twist_n} 段做冲突升级/转折，剩余 {main_n} 段做主线推进。'
        f'输出格式："第 k 段：核心冲突 + 角色状态 + 是否含日常" 的 N 段表格，'
        f'**不限制字数**（按 N 段自然展开，N>20 时也照常给完整表格）。'
    )
```

---

## 3. 验收

### 3.1 新增 `backend/verify_acceptance.py` C3 段（追加 1 条 check）

读 `verify_acceptance.py` 找 C 段（`# C. 账本增长 → 提示词膨胀`）的尾部，在 `check("C2b ...", ...)` 之后追加：

```python
# E-任务 C3：target_total_chars 与节拍指令
db = SessionLocal()
proj = db.get(Project, 1)
old_target = proj.target_total_chars
proj.target_total_chars = 1_000_000  # 100 万字
db.commit()
db.close()
try:
    u = build_user_prompt(
        [], [], None, 3000, None, None,
        target_total_chars=1_000_000, segment_count=10, segment_no=11,
    )
    check("C3 节拍指令含「目标字数/段数/日常比例」",
          "1000000" in u and "段" in u and "日常" in u, f"截 500: {u[:500]}")
finally:
    db = SessionLocal()
    p = db.get(Project, 1)
    p.target_total_chars = old_target
    db.commit()
    db.close()
```

### 3.2 跑全闸门

```bash
cd backend
python verify_acceptance.py 2>&1 | tail -3
# 期望：PASS 15  FAIL 0  (C3 PASS, 原 14/0 不退)

python test_p0_regression.py 2>&1 | grep "结果"
# 期望：55/55

python test_ledger.py 2>&1 | tail -2
# 期望：40/40

python test_extractor_v2.py 2>&1 | tail -2
# 期望：8/8（E 已修，不退化）
```

### 3.3 直接构造 prompt 验

```bash
cd backend
python - <<'PY'
from app.generation.prompts import build_user_prompt
u = build_user_prompt(
    [], [], None, 3000, None, None,
    target_total_chars=1_000_000, segment_count=10, segment_no=11,
)
assert "1000000" in u
assert "节拍" in u or "段" in u
print("OK 节拍指令出现")
print(u[-300:])  # 末尾 300 字
PY
```

---

## 4. Worktree 流程

```bash
cd /c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator
git worktree add worktrees/feat-target-total-pacing e5e17a3
cd worktrees/feat-target-total-pacing

# 改 backend/app/models.py + db.py + generation/prompts.py + generation/engine.py + routers/wizard.py
# 新增 verify_acceptance.py C3 段
# 跑 §3.2

cat > /tmp/msg_c.txt <<'EOF'
feat(pacing): target_total_chars + 节拍指令（首/主/转/收 4 类）

C 任务：从基线 e5e17a3 拉 worktree feat-target-total-pacing。

1) Project 新增 target_total_chars: int|None（db.py 同步加迁移）
2) build_user_prompt 新增 keyword-only 参数 target_total_chars/segment_count/segment_no
   - target=None 走旧路径（基线 14/0 不退化）
   - target 给定时按 (主线<65% / 冲突 65-85% / 高潮≥85%) 三类给节拍提示
3) engine.py 透传：从 project.target_total_chars + 已写段数 算节拍
4) build_bible sys_p 去 "300 字内" 限制，按 target 加节拍规划指令
5) verify_acceptance C 段新增 C3 check（节拍指令含目标/段数/日常比例）

不动 extractor.py / queries.py / build.py / build_system_prompt / router.projects / wizard build_bible 主体。
EOF
git -c commit.gpgsign=false add backend/app/models.py backend/app/db.py \
    backend/app/generation/prompts.py backend/app/generation/engine.py \
    backend/app/routers/wizard.py backend/verify_acceptance.py
git -c commit.gpgsign=false commit -F /tmp/msg_c.txt

# 通知 WorkBuddy 收口
```

---

## 5. 失败回退

- C3 check fail → 看 `build_user_prompt` 实际输出尾部 500 字，确认节拍指令真在；如缺，**是 dict comprehension 错**。
- verify_acceptance 14/0 → 9/0 → **回滚**——说明改动破坏了旧路径。`target_total_chars=None` 必走旧逻辑。
- 任何 engine.py 集成测试 fail → 看 `count_already_written` 是否有误。

---

## 6. 不要做

- ❌ push（无 remote）
- ❌ 改 `extractor.py`（E 独占）
- ❌ 改 `queries.py`（F 独占）
- ❌ 改 `build.py`（F 独占）
- ❌ 改 `build_system_prompt`（D 独占）
- ❌ 改 `build_bible` 主体（只能追加 sys_p；不能改 synopsis/direction 等其他字段的提示词）
- ❌ 在 `build_user_prompt` 改 prior 块（这是另条独立管线）

---

**End of task C spec**
