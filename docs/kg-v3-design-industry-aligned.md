# KG v3.0 工业界对齐设计（百万字 + 5 个产品功能）

> 反思：从工业界（Novelium / Think Machine / Narrative Graph / graphify-novel
> / SAGA / NovelScribe / STORYTELLER / E2RAG / CreAgentive）研究后——
> 我之前的设计**漏掉了 5 个产品功能**，**双图设计**应该分 entity + event
> 两张表，事件应该用 **SVO triplet** 存。

## 工业界共识（2024-2025）

### 双图设计（E2RAG / CreAgentive）

- **Entity Graph**——人物/地点/物品/势力
- **Event Graph**——事件-事件因果链
- **分别检索**——LLM 不会被无关事实干扰

### SVO Triplet（STORYTELLER / ICLR 2026）

- 事件存为 **(subject, verb, object, time_anchor, location, confidence)**
- 比自然语言压缩——精确检索 + 精确生成
- 84.33% 胜率

### 5 个产品功能（Novelium 卖点）

1. **Character Knowledge Tracking**——谁什么时候知道什么
2. **Post-generation Consistency Check**——生成后自动检测矛盾
3. **Spatial/Temporal Logic**——空间/时间逻辑校验
4. **Cascade Analysis**——改大纲/改人物触发级联分析
5. **Pacing Analysis**——节奏密度曲线

## 4 张表（不是 8 张，不是 3 张）

### 1. `kg_entities`（实体图，Novelium-style）

```sql
CREATE TABLE kg_entities (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    kind VARCHAR(16) NOT NULL,         -- 'character' | 'location' | 'item' | 'faction'
    canonical_name VARCHAR(128) NOT NULL,
    aliases JSON DEFAULT '[]',         -- ["小顾", "辰逸哥哥"]
    attrs JSON DEFAULT '{}',           -- 软 schema（运行时校验）
    first_chapter INTEGER,
    last_chapter INTEGER,
    mention_count INTEGER DEFAULT 0,   -- 热力
    confidence FLOAT DEFAULT 1.0,
    created_at DATETIME,
    updated_at DATETIME,
    UNIQUE(project_id, kind, canonical_name)
);
CREATE INDEX idx_kg_entity_project_kind ON kg_entities(project_id, kind);
CREATE INDEX idx_kg_entity_mentions ON kg_entities(project_id, mention_count DESC);
```

**attrs 软 schema（按 kind）**：

```python
ATTRS_SCHEMA = {
    "character": {
        "age": int, "gender": str, "occupation": str,
        "status": str,        # 'alive' | 'dead' | 'missing'
        "current_location_id": int,
        "personality_traits": list,
    },
    "location": {
        "type": str,          # '城市' | '建筑' | '自然景观' | '虚拟'
        "parent_location_id": int,
        "region": str,
    },
    "item": {
        "category": str, "is_unique": bool,
        "current_owner_id": int,
    },
    "faction": {
        "leader_id": int, "territory_location_id": int,
    },
}
```

### 2. `kg_relations`（关系图，CreAgentive Role Graph）

```sql
CREATE TABLE kg_relations (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    from_id INTEGER NOT NULL,            -- 任何 kg_entities.id
    to_id INTEGER NOT NULL,              -- 任何 kg_entities.id
    rel_type VARCHAR(32) NOT NULL,       -- 'parent_of' | 'lover_of' | 'rival_of' | 'friend_of' | 'owns' | 'located_in' | 'member_of' | 'hostile_to' | 'allied_with'
    attrs JSON DEFAULT '{}',             -- {"since_chapter": 12, "intensity": 0.8}
    first_chapter INTEGER,
    last_chapter INTEGER,
    confidence FLOAT DEFAULT 1.0,
    UNIQUE(project_id, from_id, to_id, rel_type)
);
```

**注意**：kg_relations 是**实体-实体**关系——**不存**事件-实体关系（那是 kg_event_actors）。

### 3. `kg_events`（事件图，E2RAG Event Subgraph）

```sql
CREATE TABLE kg_events (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    sequence INTEGER NOT NULL,           -- 跨项目连续编号
    chapter INTEGER,
    segment_no INTEGER,
    event_type VARCHAR(32) NOT NULL,     -- 'incident' | 'revelation' | 'foreshadowing_plant' | 'foreshadowing_payoff' | 'death' | 'romance_milestone' | 'battle' | 'decision' | 'discovery' | 'time_marker'
    summary VARCHAR(500),                -- 自然语言摘要
    -- SVO triplet (STORYTELLER 风格)
    subject_id INTEGER,                  -- 主体 entity_id
    verb VARCHAR(64),                    -- 动作：'meets' | 'discovers' | 'kills' | 'marries' | 'reveals' | 'fights'
    object_id INTEGER,                   -- 客体 entity_id
    location_id INTEGER,                 -- 地点 entity_id
    time_anchor_id INTEGER,              -- 时间锚 entity_id（kind='time_anchor'）
    -- 元数据
    confidence FLOAT DEFAULT 1.0,
    evidence_segment_ids JSON DEFAULT '[]',
    extra_actors JSON DEFAULT '[]',      -- 参与角色超过 2 个时存这里
    created_at DATETIME,
    UNIQUE(project_id, sequence)
);
CREATE INDEX idx_kg_event_chapter ON kg_events(project_id, chapter);
CREATE INDEX idx_kg_event_type ON kg_events(project_id, event_type);
```

**关键洞察**：
- 事件**不**是 `kind='event'` 的对象——**事件和实体是 2 张表**——**E2RAG 学术共识**
- SVO triplet：**精确检索 + 精确生成**
- 伏笔 = `event_type='foreshadowing_plant'` + `event_type='foreshadowing_payoff'` + link

### 4. `kg_event_links`（事件因果链，CreAgentive Plot Graph）

```sql
CREATE TABLE kg_event_links (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    from_event_id INTEGER NOT NULL,
    to_event_id INTEGER NOT NULL,
    link_type VARCHAR(16) NOT NULL,      -- 'caused_by' | 'causes' | 'follows' | 'resolves' | 'reverses' | 'parallel'
    confidence FLOAT DEFAULT 1.0,
    UNIQUE(project_id, from_event_id, to_event_id, link_type)
);
```

**伏笔**就是 `link_type='resolves'`：`foreshadowing_plant.event → foreshadowing_payoff.event`——**伏笔状态机 = link 的状态**。

### 5. `kg_entity_knowledge`（人物情报追踪，Novelium 卖点）

```sql
CREATE TABLE kg_entity_knowledge (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,          -- 人物 id
    known_entity_id INTEGER NOT NULL,    -- 该人物知道的另一个实体（或事件 id）
    known_kind VARCHAR(16),              -- 'entity' | 'event' | 'fact'
    learned_at_event_id INTEGER,         -- 在哪个事件学到的
    learned_at_chapter INTEGER,
    confidence FLOAT DEFAULT 1.0,
    UNIQUE(project_id, entity_id, known_entity_id, learned_at_event_id)
);
```

**示例**：
- 林悠然 知道 顾辰逸（kind=entity，事件 5 "两人初遇"）
- 林悠然 知道 顾辰逸父亲身份（kind=event，事件 200 "身份揭晓"）
- 顾辰逸 知道 林悠然在陆家嘴工作（kind=event，事件 50 "入职")

**这是 Novelium 卖点的数据结构**——LLM 生成时**用这个反查"林悠然现在不知道王夫人的事"**——避免**预知外挂**。

### 6. `kg_mentions`（反向索引，所有产品的标配）

```sql
CREATE TABLE kg_mentions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    generation_id INTEGER NOT NULL,
    segment_no INTEGER,
    context_window VARCHAR(500),
    mention_kind VARCHAR(16),            -- 'narrative' | 'dialogue' | 'reference' | 'introduced'
    UNIQUE(project_id, entity_id, generation_id)
);
```

## 5 个产品功能怎么用这 4 张表

### 1. 人物情报追踪（Character Knowledge Tracking）

```python
def can_character_know(entity_id: int, knowledge_entity_id: int) -> bool:
    """Novelium 风格：角色 X 知道 Y 吗？"""
    return db.scalar(
        select(KgEntityKnowledge).where(
            KgEntityKnowledge.entity_id == entity_id,
            KgEntityKnowledge.known_entity_id == knowledge_entity_id,
        )
    ) is not None
```

**用法**：生成第 100 段时，林悠然提到"王夫人是董事长夫人"——但 100 章**她还不知道这个**——**后处理检测**——告警/拒绝。

### 2. 生成后一致性检测

```python
def detect_contradictions(text: str, kg: KGProject) -> list[str]:
    """用 KG 反向校验新生成的文本。"""
    issues = []
    # 死人说话
    for char in extract_characters(text):
        if char.status == "dead" and is_speaking(text, char):
            issues.append(f"{char.name} 已死但说话")
    # 瞬移
    for char in extract_characters(text):
        last_loc = char.attrs["current_location_id"]
        if new_loc_in_text and distance(last_loc, new_loc) > 1:
            issues.append(f"{char.name} 在章 N 在 A，但章 N+1 在 B（瞬移）")
    # 知道不该知道
    if is_knowing_about_event(text, entity, "未揭晓事件"):
        issues.append(f"{entity.name} 不应知道事件 X")
    return issues
```

**这就是 sentinel 的扩展**——v2.6 sentinel 只检查死人和境界——v3.0 扩展到：
- 死人说话 ✓（已有）
- 物品归属冲突 ✓（已有）
- 境界倒退 ✓（已有）
- 瞬移 ❌（v3.0 新增）
- 预知外挂 ❌（v3.0 新增）
- 重复关系 ❌（v3.0 新增）

### 3. 空间/时间逻辑

```python
def check_spatial_logic(text: str, char: Character, kg: KGProject) -> list[str]:
    """leenbj 风格：禁止瞬移。"""
    loc_now = char.attrs["current_location_id"]
    loc_in_text = extract_location(text)
    if loc_in_text and not has_travel_event_between(loc_now, loc_in_text):
        return [f"{char.name} 瞬移到 {loc_in_text}（无交通工具）"]
    return []
```

### 4. 大纲级联（Cascade Analysis）

```python
def cascade_analysis(project_id: int, changed_event_id: int) -> list[str]:
    """修改事件 N → 找出所有依赖事件 N 的事件/伏笔/段落。"""
    # 找所有 caused_by N 的事件
    affected_events = db.scalars(
        select(KgEventLink).where(KgEventLink.from_event_id == changed_event_id)
    ).all()
    # 找所有段落 mention 了这些事件涉及的实体
    affected_segments = ...
    return affected_events, affected_segments
```

**作者改大纲时**——告诉作者"改了第 5 段，会影响第 30/45/80 段"。

### 5. 节奏分析（Pacing Analysis）

```python
def pacing_curve(project_id: int) -> list[tuple[int, float]]:
    """每章"剧情密度"曲线。"""
    return db.execute(text("""
        SELECT chapter, COUNT(*) AS density
        FROM kg_mentions m
        JOIN generations g ON g.id = m.generation_id
        WHERE g.project_id = ?
        GROUP BY chapter
        ORDER BY chapter
    """), (project_id,)).fetchall()
```

**Novelium 的核心可视化**——拖沓的章（低密度）/快进的章（高密度）。

## 抽取策略

### 异步批抽（v2.6 痛点改进）

```python
# worker 后台跑
async def extract_batch(generation_ids: list[int]):
    """一次抽 5-10 段合并——节省 50%+ LLM 调用 + 跨段一致性。"""
    texts = [get_text(gid) for gid in generation_ids]
    combined = "\n\n---\n\n".join(texts)
    result = await llm.extract(combined)  # 1 次 LLM 调用
    return result
```

### LLM 输出 schema 校验

```python
def validate_extraction(result: dict, soft_schema: dict) -> bool:
    """schema 漂移防御。"""
    for event in result.get("events", []):
        if event["event_type"] not in EVENT_TYPE_ENUM:
            event["event_type"] = "incident"  # 兜底
        if not all(k in event for k in ["subject_id", "verb", "object_id"]):
            return False
    return True
```

## v3.0 vs v2.6 vs 工业界对照

| 维度 | v2.6 | v3.0 | 工业界（Novelium 等） |
|---|---|---|---|
| 实体表 | kg_nodes | kg_entities | entity graph |
| 关系表 | kg_edges | kg_relations | role graph |
| **事件表** | **无** | **kg_events** | **event graph** |
| **因果链** | **无** | **kg_event_links** | **plot graph** |
| **人物情报** | **无** | **kg_entity_knowledge** | **Novelium 卖点** |
| **伏笔状态机** | facts.plothole | kg_event_links.resolves | graphify-novel |
| **生成后质检** | sentinel（弱） | detect_contradictions | Consistency Guardian |
| **节奏分析** | **无** | pacing_curve | Novelium |
| **大纲级联** | **无** | cascade_analysis | leenbj skill |
| **空间/时间逻辑** | **无** | check_spatial_logic | leenbj skill |
| **多 agent 挑刺** | **无** | 未来 v4 | Narrative Graph |
| **自动建 bible** | **无** | 未来 v4 | graphify-novel |
| 抽取策略 | 同步（阻塞） | 异步批抽 | 各家不同 |

## v3.0 落地的工作量

| 步骤 | 代码量 | 时间 |
|---|---|---|
| 6 张表 schema + 模型 | ~250 行 | 0.5d |
| 软 schema 校验 | ~150 行 | 0.5d |
| v3 extractor（含 SVO） | ~500 行 | 1.5d |
| 异步批抽 worker | ~200 行 | 1d |
| 5 个产品功能 API | ~400 行 | 1.5d |
| detect_contradictions 扩展 | ~200 行 | 0.5d |
| 测试 | ~400 行 | 1d |
| **总计** | **~2100 行** | **6.5d** |

**比之前 8 张表方案略多**——但**功能**是工业界级别的（5 个产品功能 vs 之前的 1 个）。

## 演进路径

**v3.0**（6.5d）——双图 + 5 个产品功能
**v3.1**（2d）——多 agent 互相挑刺（Narrative Graph 风格）
**v3.2**（1d）——自动 story bible scaffolding（graphify-novel 风格）
**v3.3**（2d）——Neo4j 替代 SQLite（如果真要撑 100 万字）
**v3.4**（3d）——前端可视化（关系图/时间线/节奏图）

## 给你的回答

**"工业界怎么用 KG"**—— **5 个核心产品功能**：
1. 人物情报追踪
2. 生成后一致性检测
3. 空间/时间逻辑
4. 大纲级联分析
5. 节奏密度分析

**v3.0 设计修正**：
- **承认 E2RAG 双图**——entity + event 是 2 张表，**不**是 1 张 objects
- **承认 STORYTELLER SVO**——事件用 (subject, verb, object) triplet
- **承认 Novelium 人物情报**——加 kg_entity_knowledge 表
- **承认 leenbj 空间逻辑**——加 check_spatial_logic
- **承认 graphify-novel 伏笔**——伏笔 = event + link
- **承认 Novelium 节奏**——kg_mentions 反向索引够

**6 张表 + 软 schema + 5 个产品功能 = 工业界级 KG**。

**你下一步**：
- A. 让我重写 v3.0 完整文档（按工业界对齐）
- B. 先做 1-2 个最高价值功能（人物情报 + 矛盾检测）
- C. 讨论某具体功能
- D. 改设计——你有别的工业产品参考
