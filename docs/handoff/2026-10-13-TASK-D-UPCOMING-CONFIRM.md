# 任务 D · 即将出场角色确认表（plan 端点响应形状扩展）

> 状态：可独立 worktree 开工。**基线 HEAD = `e5e17a3`**
> 工作目录：`/c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/`
> 验收目标：plan 端点响应**新增** `upcoming_characters` 字段 + 新增 `POST /api/projects/{id}/upcoming-characters` 端点 + `build_system_prompt` 在合并后**追加** upcoming 块（**不改 `merged_constraint_items` 签名**）
> 派单对应：v2 文档 §任务 D

---

## 1. 背景与根因

`POST /api/generate-plan` 端点（`backend/app/routers/generation.py:192-226`）**已存在**——返回 `reminders/constraints/prior_summaries/prior_blocks/prior_report/rag_context/system_prompt/user_prompt/style_profile/sentinel`。v1 派单错说"新增端点"，v2 改"**改响应形状**"。

**用户希望**：plan 端点能告诉前端"接下来系统推测哪些角色会出场"，前端展示给用户**确认/改/删**后调 `POST /api/projects/{id}/upcoming-characters` 写回 `plot_bible.confirmed_upcoming`，**再**调 `POST /api/generate` 真正续写——这是"人守最后一关"。

### 1.1 关键修正（v1→v2）

- ❌ 改 `merged_constraint_items(reminders, plot_bible) -> (chars, fores)` 签名（**这是 prompts.py:94 唯一合并口径**，动它会破 P0）
- ✅ 新独立函数 `upcoming_constraints_block(plot_bible)`，`build_system_prompt` 合并后**追加**调用
- ❌ "plot_bible.characters 减去已出场 = 即将出场"（**这会把所有未登场角色都当近期出场**）
- ✅ `plot_bible.characters` ∪ `kg.generated` 中 `mentions>=2 未在 bible` 的新候选

### 1.2 不要做

- ❌ 不动 `merged_constraint_items` 签名（prompts.py:94）
- ❌ 不动 `extractor.py`（E 独占）
- ❌ 不动 `queries.py::get_graph/get_character` 函数签名/字段（**F 独占**——F 改 KG 字段透出，D 改 reminders 路径，不重叠）
- ❌ 不动 `build_user_prompt`（C 独占）
- ❌ 不动 `wizard.py`（B 独占）
- ❌ 不动 `kg/build.py` 的 `_write_result`（F 独占）

---

## 2. 改动清单

### 改动 1 · `backend/app/generation/engine.py` 新增 `build_plan`

在 `build_context`（line 239）之后插入：

```python
def build_plan(db, project: Project) -> dict:
    """plan 端点组装：不调 LLM，把生成所需的全部上下文 + upcoming 候选一起返回。
    
    E-任务 D：upcoming 候选 = bible.characters ∪ kg.generated mentions>=2 未在 bible
    的新候选。**不直接**减已出场（v1 错的——会把所有未登场角色都当近期出场）。
    """
    ctx = build_context(db, project)
    profile = _style_profile(db, project)
    system = build_system_prompt(
        project.title, ctx["reminders"], _style_info(project), project.plot_bible, profile
    )
    user = build_user_prompt(
        ctx["prior_summaries"], ctx["rag_context"], project.outline,
        project.generation_length or 3000, project.plot_bible, project.premise,
        prior_blocks=ctx["prior_blocks"],
    )
    
    # —— upcoming 候选 ——
    bible = project.plot_bible or {}
    bible_chars = {c.get("name") for c in bible.get("characters", []) if isinstance(c, dict) and c.get("name")}
    # 从 KG.generated 找 mentions>=2 且不在 bible 的候选
    from app.models import KgNode
    kg_chars = db.scalars(
        select(KgNode).where(
            KgNode.project_id == project.id,
            KgNode.source == "generated",
            KgNode.type == "character",
        )
    ).all()
    upcoming: list[dict] = []
    for c in bible.get("characters", []):
        if isinstance(c, dict) and c.get("name"):
            upcoming.append({
                "name": c.get("name"),
                "role": c.get("role") or "角色",
                "motivation": c.get("motivation") or "",
                "source": "bible",
                "bible_only": True,  # 标记这是 bible 角色，未必确认要登场
            })
    for n in kg_chars:
        props = n.props or {}
        name = props.get("name")
        if not name or name in bible_chars:
            continue
        if (props.get("mentions") or 0) < 2:
            continue
        upcoming.append({
            "name": name,
            "role": props.get("role") or "角色",
            "motivation": props.get("motivation") or "",
            "source": "kg",
            "mentions": props.get("mentions"),
            "last_chapter": n.last_chapter,
            "bible_only": False,
        })
    
    return {
        **ctx,  # reminders/constraints/prior_*/rag_context
        "system_prompt": system,
        "user_prompt": user,
        "style_profile": _style_profile_digest(project, profile),
        "sentinel": _sentinel_digest(project),  # ← 此函数在 routers/generation.py
        "upcoming_characters": upcoming,
    }
```

**注意**：`_style_profile_digest` / `_sentinel_digest` 在 `routers/generation.py` 是私有函数——**D 任务需要它们在 engine 层**。**最干净**：把这两个函数从 `routers/generation.py` 搬到 `engine.py`（它们本来就是 engine 的衍生）；或者**复制到 engine.py**（但这是重复代码）。**建议搬到 engine.py**，然后 `routers/generation.py` 改为 import。

搬迁后 `routers/generation.py::generate_plan` 简化为：

```python
@router.post("/generate-plan")
def generate_plan(body: GeneratePlanBody, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, body.project_id)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)
    # 透传 novel_ids 到 build_plan（让 build_plan 内的 build_context 收到）
    # 实际做法：build_plan 接受 (db, project, novel_ids=None) 可选
    return build_plan(db, proj, novel_ids=novel_ids)
```

**为最小破坏**，**不搬迁**——直接在 `engine.py` 复制一份 `_sentinel_digest`/`_style_profile_digest` 实现（这两个函数简单，复制 30 行可接受）；后续清理阶段再做。**纪律**：函数体必须和 `routers/generation.py` 一致，逻辑不要分叉。

### 改动 2 · `backend/app/routers/generation.py::generate_plan` 用 build_plan

line 192-226 的 `generate_plan` 整体简化为：

```python
from app.generation.engine import build_plan

@router.post("/generate-plan")
def generate_plan(body: GeneratePlanBody, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, body.project_id)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)
    plan = build_plan(db, proj, novel_ids=novel_ids)
    return plan
```

并删除原 line 192-226 中重复的 `system/user/style_profile/sentinel` 拼装。

### 改动 3 · `backend/app/routers/projects.py` 新增 `POST /api/projects/{id}/upcoming-characters`

在 `_require_project` 附近插入新端点：

```python
@router.post("/{project_id}/upcoming-characters")
def save_upcoming_characters(
    project_id: int,
    body: dict,  # {"confirmed": [{"name","role","motivation"}, ...]}
    db: Session = Depends(get_db),
) -> dict:
    """用户确认版 upcoming 角色写回 plot_bible.confirmed_upcoming（不覆盖 bible 其余）。"""
    proj = _require_project(db, project_id)
    bible = dict(proj.plot_bible) if isinstance(proj.plot_bible, dict) else {}
    bible["confirmed_upcoming"] = body.get("confirmed") or []
    proj.plot_bible = bible
    db.commit()
    db.refresh(proj)
    return {"confirmed": bible["confirmed_upcoming"]}
```

**注意**：本端点与 wizard.py 同一 router prefix（`/api/projects`），**改 `routers/projects.py` 即可**。**确认下** router prefix：line 27-28 `router = APIRouter(prefix="/api/projects", tags=["projects"])`（routers/projects.py）。

### 改动 4 · `backend/app/generation/prompts.py` 新增独立函数

在 `merged_constraint_items`（line 94）**之后**插入：

```python
def upcoming_constraints_block(plot_bible: dict | None) -> str:
    """把 confirmed_upcoming 渲染成 system 提示块（不参与合并口径）。
    
    E-任务 D：与 merged_constraint_items 解耦——这是 plan 端点"人守最后一关"的产物，
    不该污染 bible 既有合并路径。plot_bible["confirmed_upcoming"] 是用户在前端
    确认/改/删后的最终名单。
    """
    confirmed = (plot_bible or {}).get("confirmed_upcoming") or []
    if not confirmed:
        return ""
    lines = "\n".join(
        f"  · {c.get('name')}"
        f"（{(c.get('role') or '角色')}"
        f"{('；动机：' + c.get('motivation')) if c.get('motivation') else ''}）"
        for c in confirmed
        if isinstance(c, dict) and c.get("name")
    )
    return (
        "【即将出场角色（已由用户确认）】\n"
        f"{lines}\n"
        "续写时必须按此名单安排出场顺序与对白分布，"
        "不得自行增删或改名。"
    )
```

### 改动 5 · `backend/app/generation/prompts.py::build_system_prompt` 追加调用

line 173 附近 `build_system_prompt(...)` 函数体，**在最后一行 `return` 之前**追加：

```python
# E-任务 D：用户确认的 upcoming 角色（与 merged_constraint_items 解耦）
upcoming_block = upcoming_constraints_block(plot_bible)
if upcoming_block:
    # 插入到「未填伏笔」之后、「风格」之前的位置——保持现有 7 段块的相对顺序
    ret_text = ret_text.replace(
        "【风格】", upcoming_block + "\n【风格】"
    )
```

**关键**：
- 用 `replace` 注入到**已知**的【风格】段之前，**不**改函数结构
- 改完后 `merged_constraint_items` 函数体**不变**——`upcoming_constraints_block` 是**新独立**函数
- 验证：跑现有的 verify_acceptance / test_p0 都不退化（因为新块是**追加**，旧路径的合并口径不动）

### 改动 6 · 前端（vue 文件）

打开 `frontend/src/views/ProjectWorkspace.vue`（基线已 2000+ 行的大工作台）——在"生成"按钮**之前**加一个**确认模态**。

最小变更：
- "生成"按钮 click → 先调 `POST /api/generate-plan` → 拿到 `upcoming_characters` 列表
- 模态：每行 `name / role / motivation` 可编辑 + 复选框（用户可勾掉不想要的）；底部"取消/确认"
- "确认"click → `POST /api/projects/{id}/upcoming-characters` 写回 → 再调 `POST /api/generate`
- 错误处理：plan 端点返回 `upcoming_characters: []` → 跳过模态直接调 generate

`frontend/src/api/types.ts` 新增：

```ts
export interface UpcomingCharacter {
  name: string;
  role?: string;
  motivation?: string;
  source?: "bible" | "kg";
  mentions?: number;
  last_chapter?: number;
  bible_only?: boolean;
}
export interface PlanResp {
  // ... 现有 plan 字段
  upcoming_characters: UpcomingCharacter[];
}
```

`frontend/src/api/client.ts` 新增：

```ts
async generatePlan(projectId: number, novelIds?: number[]): Promise<PlanResp> {
  return this.post<PlanResp>("/api/generate-plan", { project_id: projectId, novel_ids: novelIds });
}
async saveUpcomingCharacters(projectId: number, confirmed: UpcomingCharacter[]): Promise<{ confirmed: UpcomingCharacter[] }> {
  return this.post(`/api/projects/${projectId}/upcoming-characters`, { confirmed });
}
```

---

## 3. 验收

### 3.1 新增 `backend/test_plan_upcoming.py`

```python
"""D 任务验收：plan 端点 upcoming 字段 + confirmed 写回 + system 追加。"""
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.models import Project
from app.generation.prompts import (
    build_system_prompt, upcoming_constraints_block,
    merged_constraint_items,
)
import inspect

client = TestClient(app)


def _pick_project_id() -> int:
    db = SessionLocal()
    p = db.query(Project).first()
    db.close()
    return p.id if p else 1


def test_upcoming_constraints_block_empty():
    assert upcoming_constraints_block({}) == ""
    assert upcoming_constraints_block(None) == ""
    assert upcoming_constraints_block({"confirmed_upcoming": []}) == ""


def test_upcoming_constraints_block_renders():
    out = upcoming_constraints_block({
        "confirmed_upcoming": [
            {"name": "林深", "role": "男主", "motivation": "找妹妹"},
            {"name": "苏晚", "role": "女主"},
        ]
    })
    assert "林深" in out and "男主" in out and "找妹妹" in out
    assert "苏晚" in out and "女主" in out
    assert "已由用户确认" in out


def test_merged_constraint_items_signature_unchanged():
    """关键守门：v1 错说要改 merged_constraint_items 签名；D 任务不许动。"""
    sig = inspect.signature(merged_constraint_items)
    assert list(sig.parameters.keys()) == ["reminders", "plot_bible"]


def test_build_system_prompt_appends_upcoming():
    s = build_system_prompt(
        "测", {"character_constraints": [], "unresolved_plotholes": []},
        None, {"confirmed_upcoming": [{"name": "林深", "role": "男主"}]},
        None,
    )
    assert "即将出场角色" in s and "林深" in s


def test_plan_endpoint_includes_upcoming():
    pid = _pick_project_id()
    r = client.post("/api/generate-plan", json={"project_id": pid})
    assert r.status_code == 200
    data = r.json()
    assert "upcoming_characters" in data
    assert isinstance(data["upcoming_characters"], list)


def test_save_upcoming_writes_back():
    pid = _pick_project_id()
    body = {"confirmed": [{"name": "林深", "role": "男主", "motivation": "找妹妹"}]}
    r = client.post(f"/api/projects/{pid}/upcoming-characters", json=body)
    assert r.status_code == 200
    assert r.json()["confirmed"][0]["name"] == "林深"

    db = SessionLocal()
    p = db.query(Project).get(pid)
    assert p.plot_bible["confirmed_upcoming"][0]["name"] == "林深"
    db.close()


def test_save_upcoming_preserves_other_bible_fields():
    pid = _pick_project_id()
    db = SessionLocal()
    p = db.query(Project).get(pid)
    bible_backup = dict(p.plot_bible) if isinstance(p.plot_bible, dict) else {}
    db.close()
    
    body = {"confirmed": [{"name": "苏晚"}]}
    r = client.post(f"/api/projects/{pid}/upcoming-characters", json=body)
    assert r.status_code == 200

    db = SessionLocal()
    p = db.query(Project).get(pid)
    # 关键：synopsis / direction / conflicts / foreshadowing / characters / relationships 都不丢
    for k in ("synopsis", "direction", "conflicts", "foreshadowing", "characters", "relationships"):
        if k in bible_backup:
            assert k in p.plot_bible, f"字段 {k} 被丢了"
    db.close()
```

### 3.2 跑全闸门

```bash
cd backend
python test_plan_upcoming.py           # 7/7 PASS
python verify_acceptance.py            # ≥ 14/0
python test_p0_regression.py           # 55/55
python test_ledger.py                  # 40/40
python test_extractor_v2.py            # 8/8（E 不退化）
cd ../frontend && npm run typecheck    # 0 error
```

---

## 4. Worktree 流程

```bash
cd /c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator
git worktree add worktrees/feat-upcoming-characters e5e17a3
cd worktrees/feat-upcoming-characters

# 改 backend/app/generation/engine.py（新增 build_plan）
# 改 backend/app/generation/prompts.py（新增 upcoming_constraints_block + build_system_prompt 追加）
# 改 backend/app/routers/generation.py（generate_plan 简化为调 build_plan）
# 改 backend/app/routers/projects.py（新增 upcoming-characters 端点）
# 新增 backend/test_plan_upcoming.py
# 改 frontend/src/api/types.ts / client.ts / views/ProjectWorkspace.vue
# 跑 §3.2

cat > /tmp/msg_d.txt <<'EOF'
feat(plan): upcoming 角色确认表（plan 端点扩展 + 独立 upcoming 块）

D 任务：从基线 e5e17a3 拉 worktree feat-upcoming-characters。

v1 错要改 merged_constraint_items 签名（破 P0）；v2 改 5 步：
1) engine.py 新增 build_plan(db, project, novel_ids=None)——不调 LLM，
   返回 ctx + system + user + upcoming_characters（不调 LLM 算 upcoming）
2) generate-plan 端点简化为 build_plan 调用
3) projects.py 新增 POST /api/projects/{id}/upcoming-characters
   写回 plot_bible.confirmed_upcoming（不覆盖 bible 其余字段）
4) prompts.py 新增独立 upcoming_constraints_block(plot_bible)，
   build_system_prompt 在【风格】段之前用 replace 注入（不改 merged 函数）
5) 前端：plan 端点响应加 upcoming_characters 字段；模态可勾/改/删；
   "确认"调 upcoming-characters 后再调 generate

验收：test_plan_upcoming.py 7/7 + merged_constraint_items 签名守门 +
verify_acceptance ≥ 14/0 + P0 55/55 + ledger 40/40 + extractor 8/8 + typecheck 0。

不动 extractor.py / queries.py (F 独占) / build.py (F 独占) / build_user_prompt (C 独占) / wizard.py (B 独占)。
EOF
git -c commit.gpgsign=false add backend/app/generation/engine.py \
    backend/app/generation/prompts.py backend/app/routers/generation.py \
    backend/app/routers/projects.py backend/test_plan_upcoming.py \
    frontend/src/api/types.ts frontend/src/api/client.ts \
    frontend/src/views/ProjectWorkspace.vue
git -c commit.gpgsign=false commit -F /tmp/msg_d.txt
```

---

## 5. 失败回退

- `test_plan_upcoming.py` fail → 看 `inspect.signature(merged_constraint_items)` 是否仍是 `(reminders, plot_bible)`——**是 D 任务的硬守门**
- 4 套总闸门退化 → `git reset --hard HEAD`，找 root cause
- 前端 `generatePlan` 调用无 `upcoming_characters` 字段 → 看后端 `build_plan` 是否漏返回

---

## 6. 不要做

- ❌ push
- ❌ 改 `merged_constraint_items` 签名（**硬守门**）
- ❌ 改 `extractor.py` / `queries.py` / `build.py`
- ❌ 改 `build_user_prompt`
- ❌ 改 `wizard.py`

---

**End of task D spec**
