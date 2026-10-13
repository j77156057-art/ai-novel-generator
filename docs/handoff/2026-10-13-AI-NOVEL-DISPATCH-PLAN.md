# AI 网文生成器 · 5 件事修复分派单（2026-10-13）

> 状态：分派单。**每个任务都能独立 worktree 开工**，完工后回 main 通过 ff-merge。
> 总指挥：WorkBuddy（你正在看的会话）。分派后我会做收口 + 验收。
> 工作目录约定：所有改动落在各自 worktree，**禁止直推 main**；完工后 push 分支并通知 WorkBuddy 收口。

---

## 全局规约（每个任务都要遵守）

- **路径**：`/c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/`
- **DB**：`backend/novel_dev.db` 是 dev 本地库，**gitignored**；不要把它纳入改动或 commit
- **schema 迁移**：`backend/app/db.py::_migrate_sqlite_columns` 是唯一增量 ALTER 入口，**新列必须先加到 `app/models.py`，再把 (table, col, type) 三元组加进 `migrations` 列表**——不要用其他方式加列
- **既有测试门槛**（开工前基线 / 完工后必须仍是绿的）：
  - `python verify_acceptance.py` → PASS 14/0
  - `python verify_sentinel.py` → PASS 96+/1（G1b 已知红，见任务 E）
  - `python test_p0_regression.py` → 55/55
  - `python test_ledger.py` → 40/40
  - `cd frontend && npm run typecheck` → 0 error
- **代码风格**：中文注释 / 三段式评审焦点（边界 / 失败模式 / 性能）/ 改动落定后 commit 用 `git commit -F <msg.txt> -- <paths>` 避开 shim `-m` bug
- **变更验证**：每改一处都要跑能跑该处的最近测试；改 prompt 要跑 `python verify_acceptance.py` 看 C 段（`verify_acceptance.py` 是 prompt 落定性的唯一活态闸门）
- **未在本任务里的代码不要动**——尤其是 `app/kg/extractor.py`（任务 E 才碰）

---

## 任务 A · 填/切 LLM Key，让"可选内容生成"真正生效（1 行 + 配置）

**负责人**：任意（**建议先做，1 分钟，立等可取**）
**Worktree**：`fix/llm-key-wizard-live`
**单测**：手工点 wizard 一次，3 个端点都返回模型生成内容（不是固定模板）

### 改动

`backend/.env`（仅这一行）：

```ini
# 现状
LLM_PROVIDER=zhipu
ZHIPU_API_KEY=
OPENROUTER_API_KEY=<旧 key 留着当 fallback>

# 二选一
# 1) 填 zhipu（GLM-4-Flash 永久免费）
LLM_PROVIDER=zhipu
ZHIPU_API_KEY=<用户提供的真 key>

# 2) 切回 openrouter（仅当用户能换账号或等次日重置）
LLM_PROVIDER=openrouter
```

### 验收

```bash
cd backend
python -c "from app.routers.wizard import _has_real_llm; print(_has_real_llm())"
# 必须输出 True
```

接着起服务 `uvicorn app.main:app --port 8099`，用 seed 项目调 `POST /api/projects/{pid}/propose-premise`：
返回里**至少 1 个 proposals 不在** `_offline_proposals` 的固定模板里（`backend/app/routers/wizard.py:202-220` 列出那 3 条）。

### 失败回退

若 1 分钟内 `_has_real_llm()` 还是 False，**不要**回退 `verify_acceptance.py` 任何断言——直接报"key 未生效"给 WorkBuddy。

---

## 任务 B · wizard 加「主角/关键配角名字+作用」输入步 + 强约束（中等改动）

**负责人**：前端 + 后端各 1（**可单人串做**）
**Worktree**：`feat/wizard-characters-input`
**单测**：`verify_acceptance.py` 全绿 + 新增 1 条 `test_wizard_characters_input.py`（≤ 80 行）
**预估**：150 行后端 + 100 行前端 + 80 行测试

### 必改点

1. **`backend/app/schemas.py`**：新增 `ProtagonistsIn`（`{characters: [{name, role, motivation}, ...], relationships: [{a,b,relation}]}`），≤ 5 角色
2. **`backend/app/routers/wizard.py`**：
   - 新增 `POST /api/projects/{id}/characters`（保存用户填的 roles 到 `Project.plot_bible.characters`，**不覆盖 bible 其余字段**）
   - 修改 `build_bible` 的 `sys_p`：在 prompt 末尾加
     ```
     - **必须使用用户提供的角色名**：用户已提供以下锁定角色（不得复用参考语料主同名，不得擅自改名）：
       {project.plot_bible.characters}
     ```
   - 修改 `build_bible` 的 `user_p`：在"参考语料风格样本"前，**显式列出**用户填的 characters，让 LLM 看见
3. **`backend/app/routers/projects.py`**：补一个 `patch_characters` 或直接复用 wizard 路径
4. **`frontend/concepts/*.html`（vue 文件对应的入口）**：在 wizard 步 2（"内容"）之后加一步"主角与配角"
   - 表单：`name`（必填）/ `role`（必填 1–2 词：女主/反派/导师…）/ `motivation`（选填 1 句话）
   - 关系区：`<select>` 选两个角色 + `<input>` 关系描述
   - 保存按钮调 `POST /api/projects/{id}/characters`
5. **回归**：现成所有生成 / 抽取 / 提醒测试都仍是绿的——bible prompt 是**追加**不是替换
6. **错误模式**：
   - 用户没填就提交 → 后端 422，前端不许通过
   - 用户填了空名 → 后端过滤，前端标红
   - 名字撞已有 KG 角色 → **不合并**（避免偷偷把用户角色合到参考语料的角色上）；提示"已存在但未引用"

### 验收

```bash
cd backend
python -c "
import requests
# 假设项目 1 存在
r = requests.post('http://127.0.0.1:8099/api/projects/1/characters',
    json={'characters':[{'name':'林深','role':'男主','motivation':'找妹妹'}]})
print(r.status_code, r.json())
"
# 必须 200, characters[0].name == '林深'
```

且跑全套：
```bash
python verify_acceptance.py && python test_p0_regression.py && python test_ledger.py
```

---

## 任务 C · 总字数字段 + 节拍规划（核心改动）

**负责人**：1 人
**Worktree**：`feat/target-total-and-pacing`
**单测**：`verify_acceptance.py` C 段新增 C3（"节拍块必须随 target 出现"），跑全套测试
**预估**：schema + 1 行 db 迁移 + 3 文件改动 ≈ 200 行 + 60 行测试

### 必改点

1. **`backend/app/models.py`** `Project` 表新增：
   ```python
   target_total_chars = Column(Integer, nullable=True)  # None = 用户没指定，不强求规划
   ```
2. **`backend/app/db.py::_migrate_sqlite_columns`** 列表加：
   ```python
   ("projects", "target_total_chars", "INTEGER"),
   ```
3. **`backend/app/routers/wizard.py`** `build_bible` prompt 改造：把 synopsis 字段从"300 字内"改为：
   ```
   整本目标字数 {target_total_chars or '未指定'} 万。
   若指定：按每段 {generation_length} 字 → 共 {N} 段；
     留 {int(0.15*N)} 段写日常/补世界观/换幕以缓解紧绷；
     留 {int(0.2*N)} 段做冲突升级/转折；
     其余段做主线推进。
   输出：一份"第 k 段：核心冲突 + 角色状态 + 是否含日常"的 N 段表格（≤ 800 字）。
   ```
4. **`backend/app/generation/prompts.py::build_user_prompt`** 在 `directive` 段改为：
   ```
   整本目标字数 {target_total_chars} 字 / 单段 {generation_length} 字 / 目标共 {N} 段
   已写 {segment_count} 段（{segment_count/N*100:.0f}%）/ 本段为第 {k} 段
   建议本段留 {daily_or_clause} 字给日常/换幕（按已规划节拍）
   ```
5. **`backend/app/generation/engine.py::build_context`** 透传 `project.target_total_chars` 与 `segment_count` 给 prompts
6. **错误模式**：
   - `target_total_chars=None` → prompt 走"未指定，不强求规划"分支（**不要**在 N=0 时炸）
   - `target_total_chars < generation_length` → prompt 警告"目标不足 1 段，建议 ≥ 1 段长"，但**不阻断**（用户可能在试水）

### 验收

```bash
python - <<'PY'
from app.db import init_db, SessionLocal
init_db()
db = SessionLocal()
p = db.get(__import__('app.models', fromlist=['Project']).Project, 1)
p.target_total_chars = 1000000  # 100 万
db.commit()
PY

python - <<'PY'
from app.generation.prompts import build_user_prompt
u = build_user_prompt([], [], None, 3000, None, None, prior_blocks=None)
# 粗验：必须含 "目标" 与 "段" 字样
assert "目标" in u and "段" in u, u[:500]
print("OK 节拍块已出现")
PY

# 完整套件仍绿
python verify_acceptance.py   # PASS 14+/0 (新增 C3)
python test_p0_regression.py  # 55/55
```

---

## 任务 D · 即将出场角色确认表（plan 端点回显 → 前端勾选 → 再调 generate）

**负责人**：1 人（前端 + 后端都做）
**Worktree**：`feat/upcoming-characters-confirm`
**预估**：后端 ~120 行（plan 端点 + upcoming 端点）、前端 ~150 行（确认模态）
**单测**：新增 `test_plan_upcoming.py`（≤ 100 行）

### 必改点

1. **`backend/app/generation/engine.py`** 新增 `build_plan(...)`：在 `build_context` 基础上多组装一份 `upcoming_characters`（来自 `plot_bible.characters` 减去 `kg.generated` 已出场 + 加 `kg.generated` 中 `mentions>=2 但没在 bible` 的新候选），**不调 LLM**
2. **`backend/app/routers/generation.py`** 新增端点 `POST /api/generate-plan`（**已有**同名端点，**改其形状**：返回里加 `upcoming_characters` 数组），前端可见 schema 不变（向字段加）
3. **`backend/app/routers/projects.py`** 新增 `POST /api/projects/{id}/upcoming-characters`：用户提交的确认版写回 `plot_bible.confirmed_upcoming`（数组，**不覆盖 bible**），下一次 `generate` 时通过 `merged_constraint_items` 注入到 system 提示的"将出场角色"块
4. **`backend/app/generation/prompts.py::build_system_prompt`** 在"角色人设约束"前加：
   ```
   【即将出场角色（已由用户确认）】
   {confirmed_upcoming}
   续写时必须按此名单安排出场顺序与对白分布，不得自行增删。
   ```
5. **`backend/app/generation/prompts.py::merged_constraint_items`** 把 `confirmed_upcoming` 加入合并口径（与 `bible.characters` 一起，**bible 优先**）
6. **前端**：在 wizard 末步与每段"生成"按钮前加一个**确认模态**：列出 plan 端点返回的 `upcoming_characters`，每行可勾选 / 改名 / 删除；用户点"确认 → 生成"才调 `POST /generate`

### 验收

```bash
# 1) 准备一个 project 有 plot_bible.characters = [{name:'林深',...}]
# 2) 不调 LLM 也能拿 plan
python - <<'PY'
from app.generation.engine import build_plan
from app.db import SessionLocal
db = SessionLocal()
plan = build_plan(db, db.get(__import__('app.models', fromlist=['Project']).Project, 1))
assert "upcoming_characters" in plan
print("OK plan 端点含 upcoming_characters")
PY

# 2) 用户确认后, 写回 plot_bible.confirmed_upcoming
curl -X POST http://127.0.0.1:8099/api/projects/1/upcoming-characters \
  -H 'Content-Type: application/json' \
  -d '{"confirmed": [{"name":"林深","role":"男主","motivation":"找妹妹"}]}'

# 3) 完整套件绿
python verify_acceptance.py && python test_p0_regression.py
```

### 错误模式

- 用户没填 confirmed → generate 端点不阻断，但 plan 端点返回的 upcoming 是**系统推测的**，模态提示"未确认，将按系统推测继续"
- 用户在模态里把 LLM 推的"反派 A"删了 → confirmed 里没有，**不进入** system 提示

---

## 任务 E · 抽取器扩 3 字名 / 姓氏 / 噪声护栏（独立 BUG，上一轮 G1b 真因）

**负责人**：1 人（**最容易踩坑，独立测**）
**Worktree**：`fix/extractor-3char-and-noise`
**单测**：`verify_extraction_quality.py` 已有则跑、没有就建 `test_extractor_v2.py`（≤ 150 行覆盖 8 个真实网文场景）
**预估**：120–200 行 extractor 改动 + 50 行新词表 + 150 行测试

### 必改点

1. **`backend/app/kg/extractor.py`**：
   - `_SURNAMES` 加：南、宫、慕容、欧阳、上官、皇甫、令狐、尉迟、诸葛（**至少把能验证通过的 30+ 复姓 / 18 个现代网文高频姓加齐**）
   - `_NAME_CHARS` 加：爵、落、辰、墨、辰、衍、渊、凌、晚、宁、歆、璃、楚、霆、霖、霆、翊、衍、琛、瑜 等（**实测 6 篇 200 字样，加齐 80+ 个名字用字**）
   - `_NAME_WORD_STOP` 显式加：`房间` / `终于` / `剩下` / `以前` / `现在` / `以后`（**实测 G1b 抓出的噪声**）
   - `_leading_name`：先试 3 字（合法 + 不在 stop + 首字在姓氏表 OR 名字用字组合）→ 不行回退 2 字
   - 入图门槛注释里那段"`chapters >= 2` 在单段抽取不可达"必须保留（**别动**）——这是已知设计；用户也认
2. **回归闸门**：加 1 条专门的「真实网文 8 场景」用例：
   - 南宫爵+安小落（项目 20 风格）/ 林深+苏晚（林深 × 49 次）/ 秦九歌+陆见（3 字名混 2 字）/ 复姓欧阳靖+普通李明
   - 每条必须能抽到主名（不一定要全抽，但**主名不能漏**）
3. **别动的**：`app/kg/queries.py`（任务 D 才碰 `merged_constraint_items`）
4. **可接受的副作用**：可以**少抽一些噪声**（白名单扩了），但**不能多抽噪声**（停用词没扩 → 房间/终于还能进 → 失败）

### 验收

```bash
cd backend
python - <<'PY'
import sqlite3
from app.kg.extractor import RuleBasedExtractor
from app.kg.build import extract_from_text
con = sqlite3.connect("novel_dev.db")
rows = con.execute("select text from generations where project_id=20 order by segment_no").fetchall()
con.close()
seen = set()
for (t,) in rows:
    r,_ = extract_from_text(t, segment_no=1)
    seen.update(list(r.characters))
# 关键校验
for must in ["南宫爵","安小落"]:
    assert must in seen, f"漏: {must}, 抽到: {sorted(seen)}"
# 噪声校验
for noise in ["南宫","安小","房间","终于"]:
    assert noise not in seen, f"仍含噪声: {noise}"
print("OK 修好了, 抽到:", sorted(seen))
PY

# 既有套件仍绿（含已知的 G1b 这条）
python verify_sentinel.py 2>&1 | tail -5
# 期望：PASS 96+/0  (G1b 应当转绿)
python verify_acceptance.py   # 14/0
python test_p0_regression.py  # 55/55
python test_ledger.py         # 40/40
python test_m3.py && python test_m3_relations.py && python test_m3_api.py
```

### 失败回退

若 `_valid_name('南宫爵')` 仍 False 或 `房间` 仍进图，**别继续改**——把现场 dump 出来 + 当前 3 字段名/反例给我（WorkBuddy 收口人），不要硬塞。

---

## 任务 F · 知识图谱 schema 升级（role / motivation / arc_state / relationships）

**负责人**：1 人
**Worktree**：`feat/kg-schema-enrich`
**单测**：新增 `test_kg_enriched.py`（≤ 100 行）+ `verify_acceptance.py` C 段加 1 条 C4（"角色节点含 role/motivation/arc_state"）
**预估**：schema + 1 行 db 迁移 + 4 文件改动 ≈ 180 行 + 100 行测试

### 必改点

1. **`backend/app/models.py`** `KgNode` 表 `props` 列保留为 JSON 自由字段（**不**改表结构），但**约定**：
   ```json
   {
     "name": "林深",
     "mentions": 49,
     "speaking": 12,
     "role": "男主",
     "motivation": "找妹妹",
     "arc_state": "刚接到一通不该响起的电话",
     "last_state_segment": 7,
     "status": "alive"  // alive | dead | missing | unknown
   }
   ```
2. **`backend/app/db.py::_migrate_sqlite_columns`** **不加列**——props 是 JSON，自由加字段就行
3. **`backend/app/kg/build.py::_write_result`** 与 `merge_result_into_graph`：写入时把 `role/motivation/arc_state` 透传（来自 `plot_bible.characters` 已知角色；bible 没写的留 None，**不瞎编**）
4. **`backend/app/kg/queries.py::get_graph` / `get_character`**：把 `role/motivation/arc_state/last_state_segment/status` 透出（**不是默认给 None**，是字典里 key 存在）
5. **`backend/app/generation/engine.py::_merge_segment_into_graph`** 在合并时，**仅当**角色 bible 已有字段 → 写；bible 没有 → 不动既有；**同时**更新 `arc_state` 为本段最后一句人物行为摘要（≤ 30 字；用 `provider.summarize` 或本地截断）
6. **前端**：`frontend/concepts/*.html` 的知识图谱组件：
   - 节点卡：name 为主标题，role 副标题，arc_state 侧栏，mentions 退为弱信息（字号 -1、灰）
   - 关系线：use `relations[].relation` 作为边标签（原本就有但可能没渲染）
7. **错误模式**：
   - bible 没角色 + 抽取器抽到 → 写入时 role/motivation 是 None，UI 标"未填"
   - arc_state 改写冲突（多段同时写同一角色）→ **以 last_chapter 最大者为准**

### 验收

```bash
cd backend
# 1) schema 兼容
python -c "from app.kg.queries import get_graph; print('OK 导入无错')"

# 2) 节点字段透出
python - <<'PY'
import sqlite3, json
con = sqlite3.connect("novel_dev.db")
# 任一有 character 节点的项目
row = con.execute("select project_id, props from kg_nodes where type='character' limit 1").fetchone()
print("现 props keys:", sorted((json.loads(row[1]) or {}).keys()))
# 期望含 name, mentions, speaking, (role, motivation, arc_state 至少在新建节点上)
con.close()
PY

# 3) 完整套件
python verify_acceptance.py   # 14+/0  C4 PASS
python verify_sentinel.py 2>&1 | tail -3   # 不退化
python test_p0_regression.py  # 55/55
python test_ledger.py         # 40/40
```

---

## 任务 G · system 提示上提 premise + 节拍指令（收口缝合）

**负责人**：1 人
**Worktree**：`feat/prompt-premise-lift`
**单测**：`verify_acceptance.py` C 段加 C5（"premise 进 system 静态层 + 节拍指令存在"）
**预估**：30 行

### 必改点

1. **`backend/app/generation/prompts.py::build_system_prompt`**：
   - 入参新增 `premise: str | None = None`
   - 在"角色人设约束"前加：
     ```
     【基调锚点（用户给定，整个创作期保持）】
     {premise or "（未给定，凭类型/风格自主开局）"}
     ```
2. **`backend/app/generation/engine.py::generate_next_segment`** 与 `generate_next_segment_stream`：把 `project.premise` 传进去
3. **`backend/app/generation/engine.py::build_context`** 把 `project.premise` 算进 `constraints` 块（不入合并口径，但要在 system 层）
4. **错误模式**：
   - premise=None → 该块显示"未给定"，**不报错**
   - premise 长度 > 500 字 → 截断到 500 字（防 system 层膨胀）

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
python verify_acceptance.py   # 14+/0  C5 PASS
```

---

## 收口顺序（WorkBuddy 视角）

收到任一任务 PR 后，按下列顺序做：

1. **A**（立刻）→ 你点 wizard 验证 3 个端点都是真模型
2. **C** 优先于 **D**（节拍字段是 D 的前提）
3. **B** 优先于 **D**（主角名是 upcoming 名单的种子）
4. **G** 单独最小，可与 C/D 并行
5. **E** 与 B/C/D 解耦（独立 BUG），可最晚做
6. **F** 与 D 解耦（schema 升级独立）但等 A 后做（避免反复迁移）
7. 全部完工后跑 `python verify_acceptance.py && python verify_sentinel.py && python test_p0_regression.py && python test_ledger.py && cd frontend && npm run typecheck && npm run build`

---

## 派单时刻立即生效的约束

- **每个 task 单独 worktree**：禁止改 main、禁止在 main 上跑
- **每个 task 完工 push 后**通知 WorkBuddy 收口（写一行 PR 描述 + 跑全套命令的输出尾部即可）
- **冲突时**WorkBuddy 全权裁决，**不要跨 task 改对方文件**
- **schema 变更**只能改 `app/models.py` + `app/db.py::migrations` 二处
- **改动不达验收** = **不要 merge 自己的 PR**，回滚

---

**派单总览表**（你截图发给其他 AI 即可）：

| ID | 标题 | Worktree | 行数 | 优先级 | 负责人建议 |
|---|---|---|---|---|---|
| A | LLM Key 切换 | fix/llm-key-wizard-live | 1 行 + 配 | P0 立刻 | 任何人 |
| B | wizard 主角名输入 | feat/wizard-characters-input | ~330 | P0 | 前端+后端 1 人 |
| C | 总字数+节拍 | feat/target-total-and-pacing | ~260 | P0 | 后端 1 人 |
| D | 即将出场确认表 | feat/upcoming-characters-confirm | ~370 | P1 | 前端+后端 1 人 |
| E | 抽取器 3 字名+噪声 | fix/extractor-3char-and-noise | ~400 | P1 | 后端 1 人 |
| F | KG 字段升级 | feat/kg-schema-enrich | ~280 | P2 | 后端+前端 |
| G | system 上提 premise | feat/prompt-premise-lift | ~30 | P2 | 后端 1 人 |

---

**WorkBuddy 收口前要做的事**：

1. 收 7 个 PR（每个跑对应验收）
2. 跑总闸门（`verify_acceptance`+`verify_sentinel`+`test_p0`+`test_ledger`+`npm run build`）
3. ff-merge 到 main（每条独立 commit，**禁止 squash**）
4. 通知用户回归

**End of dispatch plan**
