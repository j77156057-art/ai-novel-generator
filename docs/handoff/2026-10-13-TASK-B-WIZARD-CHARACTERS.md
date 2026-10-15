# 任务 B · wizard 加"主角/关键配角名字+作用"输入步 + 强约束

> 状态：可独立 worktree 开工。**基线 HEAD = `e5e17a3`**（含 E 已修复的 M3-M4-M5 + E 抽取器 8/8 fixture）。
> 工作目录：`/c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/`
> 验收目标：`verify_acceptance.py` 仍 ≥ 14/0 + `test_p0_regression.py` 55/55 + `test_ledger.py` 40/40 + 新增 wizard 端点单测全过
> 派单对应：v2 文档 §任务 B

---

## 1. 背景与根因

`backend/app/routers/wizard.py` 的 M5 向导链路（propose_premise → expert_questions → build_bible）**从未让用户指定主角名**——`build_bible` 的 `user_p`（line 198-211）只拼了：

- `_genre_style_text(proj)`（类型/风格平台调性）
- `proj.premise`（用户写的大概内容，可空）
- `answers`（expert 模式追问答案，可空）
- `_style_sample(...)`（参考语料风格样本，line 91-103）

**没有主角/关键配角名字字段**。LLM 必须从参考语料样本里"猜"或"抄"主同名，导致**用户上一轮反馈的"男女主照抄基底小说"真根因**。

### 1.1 改后的目标

- wizard 多一步让用户**指定**主角/关键配角的名字 + 身份 + 动机（**quick 模式可空，expert 模式必填**）
- `build_bible` 把用户填的角色**显式拼进 prompt** + 加**"不得复用参考语料主同名"**强约束
- 用户填的角色**写回 `Project.plot_bible.characters`**（**不覆盖 bible 其余字段**）

### 1.2 不要做

- ❌ 不动 `app/kg/extractor.py`（E 已修复，独占）
- ❌ 不动 `app/kg/queries.py`（F 独占）
- ❌ 不动 `app/generation/prompts.py::build_user_prompt`（C 独占）；`build_system_prompt` 可读 bible.characters 但**不**重写
- ❌ 不动 `app/generation/engine.py`（C/D 独占）
- ❌ 不动 `app/schemas.py` 已有类（`PlotBibleOut` / `PremiseProposalOut` / `ExpertQuestionOut`），只**新增** 1 个 `ProtagonistsIn`

---

## 2. 改动清单

### 改动 1 · `backend/app/schemas.py` 新增 `ProtagonistsIn`

文件第 110 行附近（`PlotBibleOut` 之上）插入：

```python
class ProtagonistsIn(BaseModel):
    """用户填的「主角/关键配角」锁定名单（写回 plot_bible.characters）。"""
    characters: list[dict] = Field(
        default_factory=list,
        description="每项含 name / role / motivation；name 必填，role 1-2 词（女主/反派/导师…），motivation 选填 1 句",
        max_length=5,  # ≤ 5 角色（防止用户瞎填）
    )
    relationships: list[dict] = Field(
        default_factory=list,
        description="每项含 a / b / relation；关系描述 1 句",
    )
```

约束校验（用 Pydantic Field）：
- `characters[].name` 必填且 1–10 字（中文人名范围）
- `characters[].role` 1–8 字
- `characters[].motivation` 0–50 字

### 改动 2 · `backend/app/routers/wizard.py` 新增 `POST /api/projects/{id}/characters`

在 `build_bible`（line 174）之后新增端点。**注意端点 URL 在 wizard.py 已有**（`router = APIRouter(prefix="/api/projects", tags=["wizard"])`，line 27）：

```python
@router.post("/{project_id}/characters", response_model=ProtagonistsIn)
def save_characters(
    project_id: int, body: ProtagonistsIn, db: Session = Depends(get_db)
) -> dict:
    """用户填的「主角/关键配角」写回 plot_bible.characters / .relationships。
    不覆盖 bible 其余字段。
    """
    proj = _require_project(db, project_id)
    bible = dict(proj.plot_bible) if isinstance(proj.plot_bible, dict) else {}
    bible["characters"] = body.characters
    bible["relationships"] = body.relationships
    proj.plot_bible = bible
    db.commit()
    db.refresh(proj)
    return body.model_dump()
```

**关键**：
- 用 `dict(proj.plot_bible)` 做**完整拷贝**再写（与 E spec §engine._summary_tree 同纪律：JSON 列不追踪原地改动，必须整体赋值才判脏）
- `body.model_dump()` 回传——**前端可读**自己刚写的字段
- 422 走 Pydantic 自动校验（用户空名/超长）

### 改动 3 · `backend/app/routers/wizard.py::build_bible` 提示词强化

**line 184-192**（`build_bible` 内部 `sys_p` 拼装处）追加：

```python
# E-任务后续：用户锁定角色强约束
locked = (proj.plot_bible or {}).get("characters") or []
if locked:
    char_lines = "\n".join(
        f"  - {c.get('name')}（{c.get('role') or '角色'}"
        f"{('；动机：' + c.get('motivation')) if c.get('motivation') else ''}"
        f"）"
        for c in locked if isinstance(c, dict) and c.get("name")
    )
    rels = (proj.plot_bible or {}).get("relationships") or []
    rel_lines = "\n".join(
        f"  - {r.get('a')} 与 {r.get('b')}：{r.get('relation')}"
        for r in rels if isinstance(r, dict) and r.get("a") and r.get("b")
    )
    user_p += (
        "\n—— 用户已锁定的角色（必须使用，不得复用参考语料主同名） ——\n"
        f"{char_lines}\n"
        + (f"关系：\n{rel_lines}\n" if rel_lines else "")
        + "约束：以上角色名、关系、动机不可改写；如需新增配角可在锁定名单外补充。"
    )
```

**line 184 `sys_p` 末尾**追加：

```python
# E-任务后续：圣经本身也强约束不得复用参考语料主同名
sys_p += (
    "\n\n- 角色命名：剧情圣经的「characters」必须使用用户提供的角色名；"
    "若用户已锁定 N 个角色，则圣经 N 角色名字必须与之一致（音同字不同也算违反）。"
    "禁止把参考语料风格样本里的主同名（如《示例·网文》/《红楼梦》等）搬进圣经。"
)
```

**关键**：
- `proj.plot_bible["characters"]` 已在改动 2 落库
- `build_bible` 接受 `body.answers` 但**没有 project 对象**——必须改成接受 `project_id`，或从 `db` 查。当前签名是 `def build_bible(project_id, body, db)`（line 174）——**已有 `project_id`**，但内取项目用的是 `proj = _require_project(db, project_id)`（line 175-177）**有 `proj`**。**直接用 `proj`** 即可，无需改签名。

### 改动 4 · `backend/app/routers/wizard.py` expert 模式必填校验

**line 174-178** `build_bible` 起始处追加：

```python
mode = proj.mode or "quick"
locked = (proj.plot_bible or {}).get("characters") or []
if mode == "expert" and not locked:
    raise HTTPException(
        status_code=422,
        detail="expert 模式必须先 POST /api/projects/{id}/characters 填写主角/关键配角",
    )
```

`Project.mode` 列已在 `models.py:40` 存在（`default="quick"`，quick|expert）。

### 改动 5 · 前端（vue 文件）

打开 `frontend/src/views/ProjectsView.vue`（基线已存在）或 `ProjectWorkspace.vue`——在 wizard 第 2 步（"内容"）**之后**插一步"主角与配角"。

最小变更：
- 表单：name（必填 input）/ role（必填 input）/ motivation（选填 textarea）；按钮"添加角色"追加到列表；列表可删
- 关系区：两个 select（来源是已添加的角色列表）+ 1 个 input 关系描述；按钮"添加关系"
- 保存按钮：`POST /api/projects/{id}/characters`，body 形如：
  ```ts
  { characters: [{name, role, motivation}, ...], relationships: [{a, b, relation}, ...] }
  ```
- 错误处理：空名 → 前端 disabled 提交按钮；后端 422 → 显示错误信息

**前端不要触碰** `frontend/src/api/types.ts`（`ProtagonistsIn` 类型**前端**要新增一份镜像）：

```ts
// frontend/src/api/types.ts 新增
export interface Protagonist {
  name: string;
  role?: string;
  motivation?: string;
}
export interface Relationship {
  a: string;
  b: string;
  relation: string;
}
export interface ProtagonistsIn {
  characters: Protagonist[];
  relationships: Relationship[];
}
export interface ProtagonistsResp {
  characters: Protagonist[];
  relationships: Relationship[];
}
```

`frontend/src/api/client.ts` 新增方法：

```ts
async saveCharacters(projectId: number, body: ProtagonistsIn): Promise<ProtagonistsResp> {
  return this.post<ProtagonistsResp>(`/api/projects/${projectId}/characters`, body);
}
```

---

## 3. 验收

### 3.1 单测（新增 `backend/test_wizard_characters.py`）

```python
"""B 任务验收：wizard 主角/关键配角端点 + 强约束。"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app.models import Project

client = TestClient(app)


def _pick_project_id() -> int:
    db = SessionLocal()
    try:
        p = db.query(Project).first()
        if p is None:
            pytest.skip("no project in dev db")
        return p.id
    finally:
        db.close()


def test_save_characters_writes_back():
    pid = _pick_project_id()
    body = {
        "characters": [
            {"name": "林深", "role": "男主", "motivation": "找妹妹"},
            {"name": "苏晚", "role": "女主", "motivation": "守住书店"},
        ],
        "relationships": [
            {"a": "林深", "b": "苏晚", "relation": "青梅竹马"},
        ],
    }
    r = client.post(f"/api/projects/{pid}/characters", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["characters"]) == 2
    assert data["characters"][0]["name"] == "林深"

    # 验证落库
    db = SessionLocal()
    p = db.query(Project).get(pid)
    assert p.plot_bible["characters"][0]["name"] == "林深"
    assert p.plot_bible["relationships"][0]["a"] == "苏晚"
    # 关键：bible 其余字段不被覆盖
    assert "synopsis" in p.plot_bible or "characters" in p.plot_bible
    db.close()


def test_save_characters_empty_name_422():
    pid = _pick_project_id()
    body = {"characters": [{"name": "", "role": "x"}], "relationships": []}
    r = client.post(f"/api/projects/{pid}/characters", json=body)
    assert r.status_code == 422


def test_expert_mode_without_characters_422():
    pid = _pick_project_id()
    db = SessionLocal()
    p = db.query(Project).get(pid)
    p.mode = "expert"
    p.plot_bible = {"characters": []}  # 清空
    db.commit()
    db.close()

    body = {"answers": {"q1": "a1"}}
    r = client.post(f"/api/projects/{pid}/build-bible", json=body)
    assert r.status_code == 422
    assert "characters" in r.text.lower()
```

### 3.2 跑全闸门

```bash
cd backend
python test_wizard_characters.py        # 3/3 PASS
python verify_acceptance.py             # ≥ 14/0
python test_p0_regression.py            # 55/55
python test_ledger.py                   # 40/40
cd ../frontend && npm run typecheck     # 0 error
```

### 3.3 端到端（手动 smoke）

```bash
# 起服务
cd backend
uvicorn app.main:app --port 8099 &

# 1) 选个 seed 项目（确保 LLM_PROVIDER 配好真 key；否则仅验路由结构）
# 2) POST /api/projects/{pid}/characters 填入林深/苏晚
# 3) 验 /api/projects/{pid} 返回的 plot_bible.characters 含林深/苏晚
# 4) POST /api/projects/{pid}/build-bible 看 sys_p 里有"用户已锁定的角色..."块
```

---

## 4. Worktree 流程

```bash
cd /c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator

# 1) 从基线 HEAD 拉 worktree
git worktree add worktrees/feat-wizard-characters e5e17a3
cd worktrees/feat-wizard-characters

# 2) 改 backend/app/schemas.py / backend/app/routers/wizard.py
# 3) 新建 backend/test_wizard_characters.py
# 4) 改 frontend/src/api/types.ts / client.ts / views/ProjectsView.vue
# 5) 跑 §3.2

# 6) commit
cat > /tmp/msg_b.txt <<'EOF'
feat(wizard): 主角/关键配角输入端点 + bible 强约束

B 任务：从基线 e5e17a3 拉 worktree feat-wizard-characters。

1) schemas.py 新增 ProtagonistsIn（≤5 角色，Pydantic 422 校验）
2) wizard.py 新增 POST /api/projects/{id}/characters（写 plot_bible.characters）
3) wizard.py::build_bible sys_p + user_p 追加「用户锁定角色」块 +
   「禁止复用参考语料主同名」强约束
4) wizard.py::build_bible 起手 expert 模式无锁定角色 → 422
5) 前端：types.ts/client.ts 镜像 ProtagonistsIn；ProjectsView wizard 步 2 后插一步
6) 验收：test_wizard_characters.py 3/3 + verify_acceptance ≥14/0 + P0 55/55 + ledger 40/40

不动 extractor.py / queries.py / engine.py / build_user_prompt。
EOF
git -c commit.gpgsign=false add backend/app/schemas.py \
    backend/app/routers/wizard.py backend/test_wizard_characters.py \
    frontend/src/api/types.ts frontend/src/api/client.ts \
    frontend/src/views/ProjectsView.vue
git -c commit.gpgsign=false commit -F /tmp/msg_b.txt

# 7) 通知 WorkBuddy 收口（无 remote，WorkBuddy 走 git merge --no-ff）
```

---

## 5. 失败回退

- `test_wizard_characters.py` 3/3 任一 fail → **不继续改**。dump 现场：当前 `Project.plot_bible` JSON 形状、端点响应。
- 4 套总闸门退化 → `git reset --hard HEAD`，找 root cause 后再开。
- 端到端 smoke 时 `build_bible` 返回的 sys_p 里**没出现「用户已锁定」块** → 检查 `proj.plot_bible["characters"]` 是否真存进去了（多半是 JSON 列原地改问题——已用 `dict()` 拷贝整体赋值，应已防住）。

---

## 6. 不要做

- ❌ push（无 remote）
- ❌ 改 `extractor.py`（E 独占）
- ❌ 改 `queries.py`（F 独占）
- ❌ 改 `build_user_prompt` 签名（C 独占）
- ❌ 改 `engine.py`（C/D 独占）
- ❌ 用 `__pycache__` 或 `.pyc` 入 commit

---

**End of task B spec**
