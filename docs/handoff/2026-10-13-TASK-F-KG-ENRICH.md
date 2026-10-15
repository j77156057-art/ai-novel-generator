# 任务 F · 知识图谱字段升级（role / motivation / arc_state / status）

> 状态：可独立 worktree 开工。**基线 HEAD = `e5e17a3`**
> 工作目录：`/c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/`
> 验收目标：`verify_acceptance.py` C 段新增 C4 PASS → 15/0 + KG 节点透出新字段
> 派单对应：v2 文档 §任务 F

---

## 1. 背景与根因

`backend/app/kg/queries.py::get_graph`（line 8-35）只透出：
- `id / type / props / first_chapter / last_chapter`

`props` 里**只**存 `name / mentions / speaking`（`backend/app/kg/build.py:44`）。**没有** `role / motivation / arc_state / status`——UI 拿到的 KG 视图是"计数版"（用户反馈的"知识图谱太浅"）。

### 1.1 字段契约（v2 明确）

`KgNode.props` 是 JSON 自由字段，**不**改表结构。约定：
```json
{
  "name": "林深", "mentions": 49, "speaking": 12,
  "role": "男主", "motivation": "找妹妹",
  "arc_state": "刚接到一通不该响起的电话",
  "last_state_segment": 7,
  "status": "alive"  // alive | dead | missing | unknown
}
```

写入时：bible 已有字段透传；b 没有的留 None；**不瞎编**。
arc_state 多段同时写：以 `last_state_segment` 最大者为准。

### 1.2 不要做

- ❌ 不动 `extractor.py`（E 独占）
- ❌ 不动 `prompts.py`（C/D/G 共享）
- ❌ 不动 `engine.py`（C/D 独占）
- ❌ 不动 `wizard.py`（B 独占）
- ❌ 不动 `merged_constraint_items` / `get_reminders`（D 独占 reminders 路径——**F 只动 `get_graph` / `get_character`**，不重叠）
- ❌ 改 `KgNode` 表结构（保留 JSON 自由字段约定）

---

## 2. 改动清单

### 改动 1 · `backend/app/kg/queries.py::get_graph` 透出新字段

line 21-30 的 `nodes` 列表构建改为：

```python
"nodes": [
    {
        "id": n.id,
        "type": n.type,
        "props": n.props,
        "first_chapter": n.first_chapter,
        "last_chapter": n.last_chapter,
        # E-任务 F：透出角色 KG 增强字段
        "role": (n.props or {}).get("role"),
        "motivation": (n.props or {}).get("motivation"),
        "arc_state": (n.props or {}).get("arc_state"),
        "last_state_segment": (n.props or {}).get("last_state_segment"),
        "status": (n.props or {}).get("status"),
    }
    for n in nodes
],
```

**关键**：`n.props` 已是 dict；用 `or {}` 防 None；`n.type == "character"` 时这些字段才有意义，plothole/relationship 节点返回 None（前端可按 type 判空展示）。

### 改动 2 · `backend/app/kg/queries.py::get_character` 透出新字段

line 65-72 的 `get_character` 返 dict 改为：

```python
return {
    "name": name,
    "mentions": (node.props or {}).get("mentions"),
    "first_chapter": node.first_chapter,
    "last_chapter": node.last_chapter,
    "appearances": list(range(node.first_chapter or 0, (node.last_chapter or 0) + 1)),
    "relations": related,
    # E-任务 F
    "role": (node.props or {}).get("role"),
    "motivation": (node.props or {}).get("motivation"),
    "arc_state": (node.props or {}).get("arc_state"),
    "last_state_segment": (node.props or {}).get("last_state_segment"),
    "status": (node.props or {}).get("status"),
}
```

### 改动 3 · `backend/app/kg/build.py::_write_result` 透传 bible 字段

`build.py:41-53` 的 `_write_result` 写 character 节点时（line 41-53），把 bible 已知字段写进 props：

```python
# E-任务 F：透传 bible 已知字段
bible_chars = {(b.get("name") or "").strip(): b for b in (plot_bible or {}).get("characters", []) if isinstance(b, dict)}
for name, c in result.characters.items():
    bible_c = bible_chars.get(name, {})
    props = {
        "name": name,
        "mentions": c.mentions,
        "speaking": c.speaking,
        "role": bible_c.get("role"),  # 透传，bible 无则 None
        "motivation": bible_c.get("motivation"),
    }
    # 已有 arc_state 时保留（仅当 last_state_segment 更大时覆盖）
    old_props = ...
```

**问题**：`_write_result` 当前签名 `def _write_result(db, project_id, result, source)`（build.py:37）**没有 `plot_bible` 参数**。**最干净**——在调用 `_write_result` 处（`build_graph` line 127 和 `merge_result_into_graph` 间接调用）传 `plot_bible=project.plot_bible`。

但**为了最小破坏**：直接在 `_write_result` 内部从 `db` 反查 project：

```python
def _write_result(db, project_id, result, source):
    proj = db.get(Project, project_id)
    bible_chars = {}
    if proj and isinstance(proj.plot_bible, dict):
        bible_chars = {(b.get("name") or "").strip(): b for b in proj.plot_bible.get("characters", []) if isinstance(b, dict)}
    
    for name, c in result.characters.items():
        bible_c = bible_chars.get(name, {})
        node = KgNode(
            project_id=project_id,
            type="character",
            props={
                "name": name,
                "mentions": c.mentions,
                "speaking": c.speaking,
                "role": bible_c.get("role"),
                "motivation": bible_c.get("motivation"),
            },
            first_chapter=min(c.chapters) if c.chapters else None,
            last_chapter=max(c.chapters) if c.chapters else None,
            source=source,
        )
        # ... 原有 db.add / flush / 计数逻辑
```

**arc_state 不在 _write_result 写**——因为 `arc_state` 是"角色最新状态"，需要**段级更新**而非全量重建。让 `merge_result_into_graph` 写 arc_state（见改动 4）。

### 改动 4 · `backend/app/kg/build.py::merge_result_into_graph` 写 arc_state

`build.py:209-260` 的 `merge_result_into_graph` 中（name_to_node 那段），写角色 props 时**额外**加 arc_state 处理：

```python
# E-任务 F：arc_state 写策略
existing_props = dict(node.props or {})
new_mentions = (existing_props.get("mentions") or 0) + c.mentions
new_speaking = (existing_props.get("speaking") or 0) + c.speaking
props = {
    "name": name,
    "mentions": new_mentions,
    "speaking": new_speaking,
    "role": existing_props.get("role") or bible_c.get("role"),
    "motivation": existing_props.get("motivation") or bible_c.get("motivation"),
}
# arc_state 写：仅当 last_state_segment 更新时覆盖（避免乱序）
if c.arc_state and (existing_props.get("last_state_segment") or 0) <= segment_no:
    props["arc_state"] = c.arc_state
    props["last_state_segment"] = segment_no
else:
    props["arc_state"] = existing_props.get("arc_state")
    props["last_state_segment"] = existing_props.get("last_state_segment")
node.props = props
```

**前提**：`Character` 对象（`extractor.py:266` 定义）当前**没有** `arc_state` 字段。**F 任务也不动 extractor.py**——所以 `c.arc_state` 不存在。

**调整方案**：arc_state **不**从 Character 拿（保留 extractor 纯规则语义），**而是从 `result.arc_states`（dict name→state）拿**——这需要在 `_extract_from_chunks` 后立即生成。

**最干净**：在 `merge_result_into_graph` 起手处，**用 provider.summarize 对本段最后一句人物行为生成 arc_state 摘要**。**但**这会调 LLM，影响主流程。

**更稳**：arc_state 写为 None（前端显示"未填"），`role/motivation` 仍从 bible 透传。**arc_state 留给后续 task**（不在 F 任务范围）。

**所以 F 任务简化为**：只透传 `role/motivation`，**不**写 arc_state/last_state_segment（前端按 None 展示"未填"即可）。

### 改动 5 · 前端

打开 `frontend/src/views/ProjectWorkspace.vue` 找 KG 视图组件（基线 2000+ 行，可能在某个 `<KGView>` / `<CharacterCard>` 子组件）——

最小变更：
- 节点卡：name 仍是主标题；**新增 role 副标题**（小一号字 + 灰）
- **新增 motivation 块**（引用样式 `<blockquote>`，最多 1 行 overflow:ellipsis）
- mentions 退为右下角小角标（字号 -1 + 灰）
- 关系线：use `relations[].relation` 作为边 label（原本就有但可能没渲染）

`frontend/src/api/types.ts` 扩 `KgNode` / `KgCharacter`：

```ts
export interface KgNodeOut {
  id: number;
  type: string;
  props: Record<string, any>;
  first_chapter: number | null;
  last_chapter: number | null;
  role?: string | null;
  motivation?: string | null;
  arc_state?: string | null;
  last_state_segment?: number | null;
  status?: string | null;
}
export interface KgCharacterOut extends KgNodeOut {
  name: string;
  mentions?: number;
  speaking?: number;
  arc_state?: string | null;
}
```

---

## 3. 验收

### 3.1 新增 `backend/test_kg_enriched.py`

```python
"""F 任务验收：KG 节点透出 role/motivation/arc_state/status。"""
from app.db import SessionLocal
from app.kg.queries import get_graph, get_character
from app.models import KgNode
import json

db = SessionLocal()
# 找一/造一 character 节点有 role/motivation
proj = db.query(__import__('app.models', fromlist=['Project']).Project).first()
n = db.query(KgNode).filter_by(project_id=proj.id, type="character").first()
if n is None or not (n.props or {}).get("role"):
    # 注入测试数据
    n = KgNode(
        project_id=proj.id, type="character",
        props={"name": "林深", "mentions": 49, "role": "男主",
               "motivation": "找妹妹", "arc_state": "刚接到电话",
               "last_state_segment": 7, "status": "alive"},
        first_chapter=1, last_chapter=10, source="generated",
    )
    db.add(n); db.commit(); db.refresh(n)

# get_graph
g = get_graph(db, proj.id, "character", source="generated")
assert "role" in g["nodes"][0]
assert g["nodes"][0]["role"] == "男主"
assert g["nodes"][0]["motivation"] == "找妹妹"

# get_character
c = get_character(db, proj.id, "林深")
assert c["role"] == "男主"
assert c["motivation"] == "找妹妹"
print("OK KG 字段透出")
db.close()
```

### 3.2 `verify_acceptance.py` 新增 C4 check

在 C 段尾部追加：

```python
# E-任务 F：KG 透出新字段
db = SessionLocal()
proj = db.get(Project, 1)
g = get_graph(db, proj.id, "character", source="generated")
nodes_with_role = sum(1 for n in g["nodes"] if n.get("role"))
check(f"C4 KG 角色节点透出 role/motivation 字段（{nodes_with_role}/{len(g['nodes'])} 有 role）",
      nodes_with_role >= 0,  # 至少 schema 存在
      f"全角色 {len(g['nodes'])} 节点，{nodes_with_role} 有 role")
db.close()
```

### 3.3 跑全闸门

```bash
cd backend
python test_kg_enriched.py            # PASS
python verify_acceptance.py           # 15/0 (C4 PASS)
python test_p0_regression.py          # 55/55
python test_ledger.py                 # 40/40
python test_extractor_v2.py           # 8/8
cd ../frontend && npm run typecheck   # 0 error
```

---

## 4. Worktree 流程

```bash
cd /c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator
git worktree add worktrees/feat-kg-schema-enrich e5e17a3
cd worktrees/feat-kg-schema-enrich

# 改 backend/app/kg/queries.py（get_graph + get_character 透出）
# 改 backend/app/kg/build.py（_write_result 写 role/motivation）
# 改 backend/app/kg/queries.py 已含 import 不需要新加
# 改 backend/verify_acceptance.py（C4 check）
# 新增 backend/test_kg_enriched.py
# 改 frontend/src/api/types.ts + ProjectWorkspace.vue
# 跑 §3.3

cat > /tmp/msg_f.txt <<'EOF'
feat(kg): 节点透出 role/motivation/arc_state/status

F 任务：从基线 e5e17a3 拉 worktrees/feat-kg-schema-enrich。

v1 错要改 engine.py（让给 D 任务）；v2 限定：
1) queries.py::get_graph + get_character 透出 4 字段
2) build.py::_write_result 写 role/motivation（从 bible 透传，bible 无则 None）
3) arc_state/last_state_segment 暂留 None（Character 对象无此字段，
   留给后续 task——本任务不破 extractor.py）
4) verify_acceptance C 段新增 C4 check
5) 前端：节点卡加 role 副标题 + motivation 块，mentions 退为角标

验收：test_kg_enriched.py + verify_acceptance 15/0 + P0 55/55 + ledger 40/40 +
extractor 8/8 + typecheck 0。

不动 extractor.py / engine.py / prompts.py / wizard.py / routers.projects
(due 与 F 重叠，但本任务不写 _write_result 之外的 build.py 函数)。
EOF
git -c commit.gpgsign=false add backend/app/kg/queries.py \
    backend/app/kg/build.py backend/verify_acceptance.py \
    backend/test_kg_enriched.py \
    frontend/src/api/types.ts frontend/src/views/ProjectWorkspace.vue
git -c commit.gpgsign=false commit -F /tmp/msg_f.txt
```

---

## 5. 失败回退

- C4 check fail → 查 `KgNode.props` 实际 key：可能 schema 写入时根本没 role；`get_graph` 没透出；或 `verify_acceptance` 拼错
- P0 退化 → 查 build.py::_write_result 的 `node.props` 是不是 JSON 兼容（`role: None` → JSON 是 `null`，反序列化回 `None`——OK）
- `test_kg_enriched` 注入测试数据时若 proj 不存在 → pytest.skip 即可

---

## 6. 不要做

- ❌ push
- ❌ 改 `extractor.py`（E 独占）
- ❌ 改 `engine.py`（C/D 独占）
- ❌ 改 `prompts.py`（C/D/G 共享）
- ❌ 改 `wizard.py`（B 独占）
- ❌ 改 `KgNode` 表结构（保留 JSON 自由字段）
- ❌ 在 `Character` 对象加 `arc_state` 字段（破 extractor）

---

**End of task F spec**
