# AI 网文生成器 · 5 件事修复分派单 v2（2026-10-13）

> **v2 修订要点**（相对 v1）：(1) 共同基线 `577724f` 已落地，无需各自从 1b4ac85 拉；(2) D 任务端点已存在（routers/generation.py:192），改为"改响应形状"；(3) D 不再改 `merged_constraint_items` 签名；(4) D/F 文件重叠通过 ownership matrix 串行解；(5) E 验收改用文本 fixture，不依赖本地 db；(6) 单位（字 vs 万）讲清；(7) `.env` 真 key 不入文档不入 commit。

---

## 共同基线（v2 关键约束）

| 项 | 值 |
|---|---|
| **基线 HEAD** | `577724f chore: 收 M3-M4-M5 完整实现为共同基线` |
| **基线 commit** | 60 文件 / 14687 净新增，含全部 M3/M4/M5 实现 |
| **基线测试** | verify_acceptance 14/0 · verify_sentinel 96/1（G1b 已知）· test_p0_regression 55/55 · test_ledger 40/40 |
| **本仓库无 GitHub remote** | 纯本地仓；worktree 间合并走 `git merge --no-ff`，**不 push** |
| **新建 worktree 命令** | `git worktree add worktrees/<branch> 577724f`（**必须从 577724f 拉**，禁从 1b4ac85） |

**每个 task 的 worktree** 都从 `577724f` 拉，**不依赖其他 task 的 commit**——这是 v2 强制约束。所有跨 task 依赖（如 B→D、F→D）通过**接口契约 + ownership matrix** 而不是 commit 顺序解决。

---

## 全局规约

- **路径**：`/c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/`
- **DB**：`backend/novel_dev.db` dev 本地库，**gitignored**，禁止 commit
- **真 API key**（A 任务）：**永远不入文档、不入 commit**；只在你本机 `.env` 落，verify 通过即生效
- **schema 迁移**：`backend/app/db.py::_migrate_sqlite_columns` 唯一 ALTER 入口；新列先 `models.py` 后 `migrations` 列表三元组
- **既有测试门槛**（完工后必须仍 ≥ 基线）：
  - `verify_acceptance.py` ≥ 14/0
  - `verify_sentinel.py` ≥ 96/1（G1b 红已立 E 任务）
  - `test_p0_regression.py` 55/55
  - `test_ledger.py` 40/40
  - `frontend` 改完后 `npm run typecheck` 0 error
- **commit 规约**：`git -c commit.gpgsign=false commit -F <msg.txt> -- <paths>`（用文件，避开 shim `-m` 吞消息 bug）
- **不要跨 task 改对方文件**——见 ownership matrix
- **不要碰 `app/kg/extractor.py`**（任务 E 独占）

---

## File Ownership Matrix（v2 解决 D/F 重叠的关键）

| 文件 | A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|---|
| `backend/.env` | ✅ 独占 | | | | | | |
| `backend/app/schemas.py` | | ✅ | | | | | |
| `backend/app/routers/wizard.py` | | ✅ | | | | | |
| `backend/app/routers/projects.py` | | ✅ | | (子字段) | | | |
| `backend/app/routers/generation.py` | | | | ✅ | | | |
| `backend/app/models.py` | | | ✅ (target_total_chars) | | | | |
| `backend/app/db.py` (migrations) | | | ✅ | | | | |
| `backend/app/generation/prompts.py` | | | ✅ (build_user_prompt 加节拍) | ✅ (新独立函数) | | | ✅ (premise 上提) |
| `backend/app/generation/engine.py` | | | (透传参数) | ✅ (build_plan / build_context 扩展) | | | (透传) |
| `backend/app/kg/build.py` | | | | | | ✅ | |
| `backend/app/kg/queries.py` | | | | | | ✅ (透出 role/motivation/arc_state) | |
| `backend/app/kg/sentinel.py` | | | | | | (只读) | |
| `backend/app/kg/extractor.py` | | | | | ✅ 独占 | | |
| `frontend/src/views/*` | | ✅ wizard 步 | | ✅ 确认模态 | | ✅ KG 视图 | |

**冲突解决**：
- `prompts.py` 同时被 C/D/G 改 → **C 先合 → D 再合 → G 最后合**（在收口时按此顺序 ff-merge）
- `engine.py` 同时被 C/D 改 → **C 先合 → D 再合**（同理）
- `queries.py` 被 D 和 F 改 → D 改的是 `get_reminders`/`build_plan` 路径，F 改的是 `get_graph`/`get_character`——**无函数重叠**，可并行
- 若 worktree 实际冲突 → 收口时手动 rebase + 解决；任何 task **不在自己 worktree 改对方文件**

---

## 任务 A · LLM Key 落地（WorkBuddy 亲自做，1 行 .env）

**负责人**：WorkBuddy（**不外派**——真实 key 只能你本机落）
**Worktree**：无（直接改本机 .env，不 commit）
**改动**：仅 `backend/.env`（**不入仓**）

```ini
# 二选一（你给 key 时告诉我用哪个）
# 1) 填 zhipu（GLM-4-Flash 永久免费）
LLM_PROVIDER=zhipu
ZHIPU_API_KEY=<你给的真 key，落到本机 .env 后不回显>

# 2) 切回 openrouter（账号次日凌晨重置限额）
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=<你给的真 key>
```

**验收（两步都必过）**：
```bash
cd backend
python -c "from app.routers.wizard import _has_real_llm; print(_has_real_llm())"
# 必须输出 True

# 然后起服务，调真端点
uvicorn app.main:app --port 8099 &
curl -X POST http://127.0.0.1:8099/api/projects/1/propose-premise
# 返回的 proposals 至少 1 条**不在** wizard.py:202-220 那 3 条固定模板里
```

**失败回退**：若 1 分钟内 `_has_real_llm()` 还是 False，**不要**回退任何测试，直接报"key 未生效"给 WorkBuddy。

**对派单 v1 的修正**：v1 把 A 派给"任意人"——v2 改 WorkBuddy 亲自做，理由是真实 key 只能你本机持有。其他人不该拿这个 key。

---

## 任务 B · wizard 加"主角/关键配角名字+作用"输入步 + 强约束

**负责人**：接 B/C/D/F/G 的人（已确认接 B）
**Worktree**：`worktrees/feat-wizard-characters-input`（从 `577724f` 拉）
**单测**：`verify_acceptance.py` 仍 ≥ 14/0；新增 `backend/test_wizard_characters.py`（≤ 80 行）
**预估**：150 行后端 + 100 行前端 + 80 行测试

### 必改点

1. `backend/app/schemas.py`：新增 `ProtagonistsIn`（`{characters: [{name, role, motivation}], relationships: [{a, b, relation}]}`），≤ 5 角色
2. `backend/app/routers/wizard.py`：
   - 新增 `POST /api/projects/{id}/characters`（保存到 `Project.plot_bible.characters`，**不覆盖 bible 其余字段**）
   - 修改 `build_bible` 的 `sys_p` 末尾追加：
     ```
     - **必须使用用户提供的角色名**：用户已锁定以下角色（不得复用参考语料主同名，不得擅自改名）：
       {project.plot_bible.characters}
     ```
   - `user_p` 在"参考语料样本"前显式列出 user 填的 characters
3. `backend/app/routers/projects.py`：**只读** `plot_bible.characters` 字段，不改 schema
4. `frontend/src/views/ProjectsView.vue` 或 `ProjectWorkspace.vue`（看现有布局）：wizard 步 2 后插一步"主角与配角"——表单 + 关系区 + 保存按钮调 `POST /api/projects/{id}/characters`
5. **错误模式**：
   - 用户空名提交 → 后端 422；前端不许通过
   - 名字撞已有 KG 角色 → **不合并**，提示"已存在但未引用"
6. **不动**：`extractor.py`（E 独占）/ `engine.py`（C/D 独占）/ `queries.py`（F 独占）/ `prompts.py`（C/D/G 共享但 B 不动）

### 验收

```bash
cd backend
python -c "
import requests
r = requests.post('http://127.0.0.1:8099/api/projects/1/characters',
    json={'characters':[{'name':'林深','role':'男主','motivation':'找妹妹'}]})
assert r.status_code == 200 and r.json()['characters'][0]['name']=='林深'
print('OK characters 端点')
"
# 完整套件
python verify_acceptance.py  # 仍 14/0
python test_p0_regression.py  # 55/55
python test_ledger.py         # 40/40
```

---

## 任务 C · 总字数字段 + 节拍规划

**负责人**：接 B/C/D/F/G 的人（同 B）
**Worktree**：`worktrees/feat-target-total-and-pacing`（从 `577724f` 拉）
**单测**：`verify_acceptance.py` C 段新增 C3（"节拍块必须随 target 出现"）；C 段总数到 15/0
**预估**：schema + 1 行 db 迁移 + 3 文件改动 ≈ 200 行 + 60 行测试

### 单位契约（v2 明确）

- `target_total_chars`：**字**（不是万）。用户界面展示 "X 万字" → 写入时 × 10000
- 内部计算 `N = target_total_chars / generation_length`（向上取整，最少 1 段）
- 日常比例：固定 15%（按"人写"经验）
- 转折/高潮比例：固定 20%

### 必改点

1. `backend/app/models.py` `Project` 表新增：
   ```python
   target_total_chars = Column(Integer, nullable=True)  # 字；None = 未指定，不强求规划
   ```
2. `backend/app/db.py::_migrate_sqlite_columns` 列表加：
   ```python
   ("projects", "target_total_chars", "INTEGER"),
   ```
3. `backend/app/routers/wizard.py` `build_bible` 提示词改造：synopsis 字段由"300 字内"改为
   ```
   整本目标字数 {target_total_chars} 字 / 单段 {generation_length} 字 / 共 N={N} 段
   留 {int(0.15*N)} 段写日常/补世界观/换幕
   留 {int(0.2*N)} 段做冲突升级/转折
   其余 N - 0.15N - 0.2N 段做主线推进
   输出格式：第 k 段：核心冲突 + 角色状态 + 是否含日常（表格，N 段全部列出，**不限于 800 字**）
   ```
   > **v1 → v2 修正**：v1 说"≤ 800 字"会卡 N>20 的长篇，v2 改为不限字数，让模型按需展开
4. `backend/app/generation/prompts.py::build_user_prompt` directive 段改为：
   ```
   整本目标字数 {target_total_chars} 字 / 单段 {generation_length} 字 / 目标共 {N} 段
   已写 {segment_count} 段（{pct:.0f}%）/ 本段为第 {k} 段
   建议本段留 {daily_chars} 字给日常/换幕（按已规划节拍）
   ```
5. `backend/app/generation/engine.py::build_context` 透传 `project.target_total_chars` + 算 `segment_count`（已有 Generation 数） + 算 `N` + 算 `daily_chars` 给 prompts
6. **错误模式**：
   - `target_total_chars=None` → prompt 走"未指定"分支，**不阻断**（用户试水）
   - `target_total_chars < generation_length` → prompt 警告"目标不足 1 段，建议 ≥ 1 段长"，**不阻断**

### 验收

```bash
cd backend
python - <<'PY'
from app.db import init_db, SessionLocal
init_db()
db = SessionLocal()
from app.models import Project
p = db.get(Project, 1)
p.target_total_chars = 1000000  # 100 万
db.commit()
PY

python - <<'PY'
from app.generation.prompts import build_user_prompt
u = build_user_prompt([], [], None, 3000, None, None,
                      target_total_chars=1000000, segment_count=10, segment_no=11)
assert "目标" in u and "节拍" in u, u[:500]
print("OK 节拍块已出现")
PY
# 真实函数签名可能不同，调用方按 prompts.py:173 实际签名为准

# 完整套件
python verify_acceptance.py  # 15/0 (C3 PASS)
python test_p0_regression.py  # 55/55
python test_ledger.py         # 40/40
```

---

## 任务 D · 即将出场角色确认表（plan 端点响应形状扩展）

**负责人**：接 B/C/D/F/G 的人（同 B）——**接单人自己说接 D**
**Worktree**：`worktrees/feat-upcoming-characters-confirm`（从 `577724f` 拉）
**预估**：~120 行后端 + 150 行前端（确认模态）
**单测**：新增 `backend/test_plan_upcoming.py`（≤ 100 行）

### v1 → v2 关键修正

| v1 错 | v2 对 |
|---|---|
| "新增 `build_plan`" | **端点已存在**（routers/generation.py:192-226，签名 `def generate_plan(body: GeneratePlanBody, db)`）。**改响应形状**而非新增 |
| "新增 `POST /api/projects/{id}/upcoming-characters`" | **保留**：写到 `Project.plot_bible.confirmed_upcoming` 字段（**新增字段**，bible JSON 内，不动 bible 已有 schema） |
| "改 `merged_constraint_items` 签名加入 confirmed_upcoming" | **不改动 merged_constraint_items**。新独立函数 `upcoming_constraints_block(project)`，`build_system_prompt` 在合并后**追加**这个块（不动现有合并口径） |
| "plot_bible.characters 减去已出场 = 即将出场" | 改为 `plot_bible.characters` ∪ `kg.generated` 中 `mentions>=2 未在 bible` 的新候选，**用户确认后**才进入 confirmed_upcoming（避免把"未登场角色"全当作近期出场） |

### 必改点

1. `backend/app/generation/engine.py` 新增 `build_plan(db, project)`：
   - **不调 LLM**，纯组装
   - 计算 `upcoming_characters` = (bible.characters ∪ kg 候选) − kg.generated 已出场
   - 返字段：`{upcoming_characters: [{name, role, motivation, source}], ...现有 plan 字段}`
2. `backend/app/routers/generation.py::generate_plan` 调 `build_plan`，把 `upcoming_characters` 加进响应 dict
3. `backend/app/routers/projects.py` 新增 `POST /api/projects/{id}/upcoming-characters`（body: `{confirmed: [...]}`）：写 `project.plot_bible["confirmed_upcoming"]`，**不覆盖 bible 其余**
4. `backend/app/generation/prompts.py` **新增独立函数**：
   ```python
   def upcoming_constraints_block(plot_bible: dict) -> str:
       """把 confirmed_upcoming 渲染成 system 提示块（不参与合并口径）。"""
       confirmed = (plot_bible or {}).get("confirmed_upcoming") or []
       if not confirmed:
           return ""
       lines = "\n".join(f"  · {c.get('name')}（{c.get('role') or '角色'}）" for c in confirmed)
       return f"【即将出场角色（用户已确认）】\n{lines}\n续写时必须按此名单安排出场。"
   ```
5. `backend/app/generation/prompts.py::build_system_prompt` 在合并块后**追加**调用 `upcoming_constraints_block(plot_bible)`——**不改 `merged_constraint_items`**
6. 前端：plan 确认模态（ProjectWorkspace.vue 或新组件）——列出 upcoming_characters，每行可勾/改名/删除；点"确认 → 生成"调 `POST /api/projects/{id}/upcoming-characters` 再调 `POST /api/generate`

### 验收

```bash
cd backend
# 1) plan 端点含 upcoming_characters
python - <<'PY'
from app.generation.engine import build_plan  # 新函数
from app.db import SessionLocal
db = SessionLocal()
from app.models import Project
plan = build_plan(db, db.get(Project, 1))
assert "upcoming_characters" in plan
print("OK plan 端点含 upcoming_characters")
PY

# 2) user 确认后写回 bible.confirmed_upcoming
curl -X POST http://127.0.0.1:8099/api/projects/1/upcoming-characters \
  -H 'Content-Type: application/json' \
  -d '{"confirmed": [{"name":"林深","role":"男主","motivation":"找妹妹"}]}'

# 3) 不改 merged_constraint_items 签名验证
python - <<'PY'
import inspect
from app.generation.prompts import merged_constraint_items
sig = inspect.signature(merged_constraint_items)
assert list(sig.parameters.keys()) == ['reminders', 'plot_bible'], sig
print("OK merged_constraint_items 签名未变")
PY

# 4) 完整套件
python verify_acceptance.py  # 14/0
python test_p0_regression.py  # 55/55
```

### 错误模式

- 用户没填 confirmed → `upcoming_constraints_block` 返空串，generate 不阻断；plan 端点仍回 `upcoming_characters`（系统推测），模态提示"未确认，将按系统推测继续"
- 模态删了 LLM 推的"反派 A" → confirmed 不含 → 不进 system 提示

---

## 任务 E · 抽取器 3 字名 + 噪声护栏（独立 BUG，单独 worktree）

**负责人**：交最熟抽取器的人（**不是你 / 不是接 B/C/D/F/G 的人**——这条单独派）
**Worktree**：`worktrees/fix-extractor-3char-and-noise`（从 `577724f` 拉）
**单测**：**新增** `backend/tests/fixtures/extractor_v2_cases.json`（文本 fixture，8 个真实网文场景）；**新增** `backend/test_extractor_v2.py`（≤ 150 行）；**不依赖** novel_dev.db
**预估**：120–200 行 extractor 改动 + 50 行新词表 + 150 行测试

### v1 → v2 关键修正

| v1 错 | v2 对 |
|---|---|
| 验收用 `novel_dev.db` project 20 | **验收用文本 fixture**（确定性强、可复现） |
| "加 80 个名字用字" | **8 个真实网文场景跑通**即可；过度扩词表会扩大误报，宁可少加 |
| "G1b 转绿"是结果 | **G1b 转绿 = 已知 96/1 变 96/0**；总闸门目标 `verify_sentinel 96/0`（v1 写"96+/0"含糊） |

### 必改点

1. `backend/app/kg/extractor.py`：
   - `_SURNAMES` 加：南、宫、慕容、欧阳、上官、皇甫、令狐、尉迟、诸葛（30+ 复姓 / 18 个现代网文高频姓）
   - `_NAME_CHARS` 加：爵、落、辰、墨、渊、凌、晚、宁、歆、璃、楚、霆、霖、翊、琛、瑜 等（**仅 fixture 验证必需的字**，不堆 80 个）
   - `_NAME_WORD_STOP` 显式加：`房间` / `终于` / `剩下` / `以前` / `现在` / `以后`（G1b 抓出的噪声）
   - `_leading_name`：先试 3 字（合法 + 不在 stop + 首字在姓氏表 OR 名字用字组合）→ 不行回退 2 字
2. `backend/tests/fixtures/extractor_v2_cases.json`（新建）：
   ```json
   [
     {"name":"南宫爵+安小落","text":"...","expect":["南宫爵","安小落"]},
     {"name":"林深+苏晚","text":"...","expect":["林深","苏晚"]},
     ...
   ]
   ```
3. **别动的**：`app/kg/queries.py` / `app/kg/build.py` / `app/generation/*`（D/F 才碰）
4. **可接受的副作用**：可以少抽一些噪声（白名单扩了），但**不能多抽噪声**（停用词没扩 → 房间/终于还能进 → 失败）

### 验收（用 fixture，**不查 db**）

```bash
cd backend
# 1) 8 个 fixture 全部通过
python test_extractor_v2.py
# 期望：8/8 PASS
# 关键校验：每个 case 的 expect 全部抽出，噪声（房间/终于/南宫/安小）未误入

# 2) G1b 转绿
python verify_sentinel.py 2>&1 | tail -3
# 期望：PASS 96  FAIL 0

# 3) 既有套件不退化
python verify_acceptance.py  # 14/0
python test_p0_regression.py  # 55/55
python test_ledger.py  # 40/40
python test_m3.py && python test_m3_relations.py && python test_m3_api.py
```

### 失败回退

若 `_valid_name('南宫爵')` 仍 False 或 `房间` 仍进图，**别继续改**——把现场 dump 出来 + 当前 3 字段名/反例给 WorkBuddy 收口人。

---

## 任务 F · KG schema 升级（role / motivation / arc_state）—— 串行在 D 之后

**负责人**：接 B/C/D/F/G 的人（同 B，但 F 单独 worktree，**从 577724f 拉**，等 D 合并完 ff-merge 时按顺序并）
**Worktree**：`worktrees/feat-kg-schema-enrich`（从 `577724f` 拉，**与 D 并行无依赖**——D 改 `engine.py`/`prompts.py`，F 改 `queries.py`/`build.py`，**无文件重叠**）
**单测**：`verify_acceptance.py` C 段加 1 条 C4（"角色节点含 role/motivation/arc_state"）；新增 `test_kg_enriched.py` ≤ 100 行
**预估**：~180 行 + 100 行测试

### v1 → v2 关键修正

| v1 错 | v2 对 |
|---|---|
| 改 `engine.py` `_merge_segment_into_graph` 写 arc_state | **F 不改 engine.py**——那文件 D 已经动了。F 改 `kg/build.py` 的 `_write_result` 写 arc_state，调用方是 `merge_result_into_graph`（**已存在，F 不改其签名**） |
| 同时改 `queries.py` `get_graph/get_character` | **保留**（D 不改 queries.py） |

### 必改点

1. `backend/app/models.py` `KgNode.props` 保留 JSON 自由字段，**约定**：
   ```json
   {
     "name": "林深",
     "mentions": 49, "speaking": 12,
     "role": "男主", "motivation": "找妹妹",
     "arc_state": "刚接到一通不该响起的电话",
     "last_state_segment": 7,
     "status": "alive"  // alive | dead | missing | unknown
   }
   ```
2. `backend/app/db.py::_migrate_sqlite_columns` **不加列**（props JSON 自由加字段）
3. `backend/app/kg/build.py::_write_result` + `merge_result_into_graph`：写入时把 `role/motivation` 透传（**来自 `plot_bible.characters` 已知角色**；bible 没写的留 None，**不瞎编**）；`arc_state` 改写时**仅当** `last_state_segment` 是最大者时覆盖
4. `backend/app/kg/queries.py::get_graph` / `get_character`：把 `role/motivation/arc_state/last_state_segment/status` 透出（key 存在，None 也透）
5. `frontend/src/views/ProjectWorkspace.vue`（或 KG 视图组件）：节点卡主标题=name，副标题=role，侧栏=arc_state，mentions 退为弱信息
6. **错误模式**：
   - bible 没角色 + 抽取器抽到 → 写入时 role/motivation 是 None，UI 标"未填"
   - arc_state 多段同时写 → **以 last_state_segment 最大者为准**（避免乱序覆盖）
7. **不动**：`engine.py`（D 独占）/ `extractor.py`（E 独占）/ `prompts.py`（C/D/G 共享但 F 不动）

### 验收

```bash
cd backend
# 1) 节点字段透出
python - <<'PY'
import sqlite3, json
con = sqlite3.connect("novel_dev.db")
row = con.execute("select project_id, props from kg_nodes where type='character' limit 1").fetchone()
print("props keys:", sorted((json.loads(row[1]) or {}).keys()))
# 期望含 name, mentions, speaking, role, motivation, arc_state
con.close()
PY

# 2) 完整套件
python verify_acceptance.py  # 15/0  (C4 PASS)
python verify_sentinel.py    # ≥ 96/1
python test_p0_regression.py  # 55/55
python test_ledger.py         # 40/40
```

---

## 任务 G · system 提示上提 premise（最小收口）

**负责人**：接 B/C/D/F/G 的人（同 B，单独 worktree）
**Worktree**：`worktrees/feat-prompt-premise-lift`（从 `577724f` 拉）
**单测**：`verify_acceptance.py` C 段加 1 条 C5（"premise 进 system 静态层"）
**预估**：~30 行

### 必改点

1. `backend/app/generation/prompts.py::build_system_prompt`：入参新增 `premise: str | None = None`；在"角色人设约束"前加：
   ```
   【基调锚点（用户给定，整个创作期保持）】
   {premise or "（未给定，凭类型/风格自主开局）"}
   ```
2. `backend/app/generation/engine.py`：`generate_next_segment` 与 `generate_next_segment_stream` 把 `project.premise` 传进去
3. `backend/app/generation/engine.py::build_context`：把 `project.premise` 算进 system 层（不入合并口径）
4. **错误模式**：
   - premise=None → 该块显示"未给定"，**不报错**
   - premise > 500 字 → 截断到 500 字（防 system 层膨胀）

### 验收

```bash
cd backend
python - <<'PY'
from app.generation.prompts import build_system_prompt
s = build_system_prompt("测", {"character_constraints":[],"unresolved_plotholes":[]},
                        None, None, None, premise="找妹妹")
assert "基调锚点" in s and "找妹妹" in s
print("OK premise 已上提 system")
PY
python verify_acceptance.py  # 16/0  (C5 PASS)
```

---

## 收口顺序（WorkBuddy 视角）

按 ownership matrix 串行 + 不冲突文件并行：

```
A (WorkBuddy 改 .env)
   ↓
B (wizard)  ─┐
C (节拍)    ─┼─ 并行（不同文件）
G (premise) ─┘
   ↓
D (upcoming) ─── 独占 prompts.py / engine.py / generation.py
   ↓
F (KG schema) ── 独占 queries.py / build.py，与 D 无文件重叠
   ↓
E (extractor) ── 独立，单独 worktree，验收用 fixture
```

每个 task 完工后：
1. 跑该 task 自己的验收
2. 跑全基线（`verify_acceptance`+`verify_sentinel`+`test_p0`+`test_ledger`）
3. 通知 WorkBuddy 收口
4. WorkBuddy 跑总闸门 → `git merge --no-ff <branch>` 到 master（**本仓无 remote**）→ 通知用户回归

---

## 总闸门期望

| 套件 | 基线 | 收口目标 |
|---|---|---|
| verify_acceptance | 14/0 | 16/0（C3+C4+C5 PASS） |
| verify_sentinel | 96/1 | 96/0（E 完成后 G1b 转绿） |
| test_p0_regression | 55/55 | 55/55 |
| test_ledger | 40/40 | 40/40 |
| frontend typecheck | 0 error | 0 error |

**任何套件低于基线 = 不收口，回滚。**

---

## 派单 v2 总结表（发给协作者）

| ID | 标题 | Worktree | 行数估 | 优先级 | 关键修正 |
|---|---|---|---|---|---|
| A | LLM Key 落地 | 无 | 1 行 .env | **P0 立刻** | 改为 WorkBuddy 亲做 |
| B | wizard 主角名输入 | feat-wizard-characters-input | ~330 | P0 | 同 v1 |
| C | 总字数+节拍 | feat-target-total-and-pacing | ~260 | P0 | 单位讲清（字非万）；800 字限制去掉 |
| D | 即将出场确认表 | feat-upcoming-characters-confirm | ~370 | P1 | 端点已存在改形状；不改 merged_constraint_items 签名 |
| E | 抽取器 3 字名 | fix-extractor-3char-and-noise | ~400 | P1 | 验收改 fixture 不查 db；总闸门目标 96/0 |
| F | KG 字段升级 | feat-kg-schema-enrich | ~280 | P2 | 不改 engine.py（让给 D） |
| G | premise 上提 | feat-prompt-premise-lift | ~30 | P2 | 同 v1 |

**共同基线 HEAD = 577724f（已 commit）**
**v2 全部从 577724f 拉 worktree，不依赖其他 task 的 commit**

---

**End of dispatch plan v2**
