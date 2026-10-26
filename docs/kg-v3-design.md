# KG v3.0 知识图谱架构（百万字小说支撑）

> v2.6 的 KG 撑不起百万字——缺地点/事件/伏笔状态机/时间锚，**核心瓶颈在
> "KG 没驱动生成"——只是存档和诊断**。v3.0 重构。

## 设计原则

1. **三层 KG**——结构（谁/在哪/是什么）/ 事件（发生了什么/什么时候）/ 时间（锚点）
2. **按需检索**——生成时按当前剧情点**动态取切片**，不 dump 全部 facts
3. **异步抽取**——分段落库后**后台批抽**，不阻塞主链
4. **强 schema**——facts.props JSON 改成强类型字段，避免跨段属性漂移

## 八张表

### 结构层（谁/在哪/是什么）

#### 1. `kg_entities`（统一实体表，替代 kg_nodes）
```sql
CREATE TABLE kg_entities (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    type VARCHAR(16) NOT NULL,           -- 'character' | 'location' | 'item' | 'faction' | 'concept'
    canonical_name VARCHAR(128) NOT NULL,
    aliases JSON DEFAULT '[]',           -- ["小顾", "辰逸哥哥", ...]
    attrs JSON DEFAULT '{}',             -- 强类型 schema 后的属性（见下）
    first_chapter INTEGER,
    last_chapter INTEGER,
    mention_count INTEGER DEFAULT 0,     -- 提及时被引用次数（热力）
    source_chapter INTEGER,              -- 首次引入章
    confidence FLOAT DEFAULT 1.0,        -- 抽取可信度（0-1）
    created_at DATETIME,
    updated_at DATETIME,
    UNIQUE(project_id, type, canonical_name)
);
CREATE INDEX idx_kg_entities_project_type ON kg_entities(project_id, type);
CREATE INDEX idx_kg_entities_mentions ON kg_entities(project_id, mention_count DESC);
```

**attrs schema（按 type 不同）**：

```python
# character
{"age": 30, "gender": "F", "occupation": "经理", "personality": ["雷厉风行"],
 "status": "alive" | "dead" | "missing" | "unknown",
 "current_location_id": int, "current_faction_id": int}

# location
{"region": "上海", "type": "城市" | "建筑" | "自然景观", "parent_location_id": int}

# item
{"category": "武器" | "信物" | "文件", "owner_entity_id": int, "is_unique": bool}

# faction / concept 自由
```

#### 2. `kg_relations`（替代 kg_edges，强类型）
```sql
CREATE TABLE kg_relations (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    from_entity_id INTEGER NOT NULL,
    to_entity_id INTEGER NOT NULL,
    rel_type VARCHAR(32) NOT NULL,        -- 'family' | 'lover' | 'friend' | 'rival' | 'belongs_to' | 'owns' | 'located_in' | 'member_of' | 'knows' | 'hostile_to'
    attrs JSON DEFAULT '{}',              -- {"since_chapter": 12, "intensity": 0.8}
    first_chapter INTEGER,
    last_chapter INTEGER,
    confidence FLOAT DEFAULT 1.0,
    UNIQUE(project_id, from_entity_id, to_entity_id, rel_type)
);
```

**强类型 rel_type 枚举**——避免 LLM 抽"恋人""老婆""my wife"三个标签。

### 事件层（发生了什么/什么时候）—— 核心新增

#### 3. `kg_events`（已发生事件表，百万字关键）
```sql
CREATE TABLE kg_events (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    sequence INTEGER NOT NULL,            -- 跨项目连续编号（1, 2, 3...）
    chapter INTEGER,
    segment_no INTEGER,
    time_anchor_id INTEGER,               -- FK -> kg_time_anchors.id
    event_type VARCHAR(32) NOT NULL,      -- 'incident' | 'revelation' | 'turning_point' | 'foreshadowing_plant' | 'foreshadowing_payoff' | 'death' | 'romance_milestone' | 'battle' | 'decision' | 'discovery'
    summary VARCHAR(500) NOT NULL,        -- 一句话摘要
    actors JSON DEFAULT '[]',             -- [entity_id, ...]
    location_entity_id INTEGER,
    related_events JSON DEFAULT '[]',     -- [{event_id, relation: 'caused_by'/'causes'/'follows'}, ...]
    evidence_segment_ids JSON DEFAULT '[]', -- 引用段 id 列表
    confidence FLOAT DEFAULT 1.0,
    created_at DATETIME,
    UNIQUE(project_id, sequence)
);
CREATE INDEX idx_kg_events_chapter ON kg_events(project_id, chapter);
CREATE INDEX idx_kg_events_type ON kg_events(project_id, event_type);
CREATE INDEX idx_kg_events_time ON kg_events(project_id, time_anchor_id);
```

**event_type 枚举**——v2.6 的"伏笔"只是 plotholes 里一个状态，**没有"何时/怎么/由谁埋下"**——v3.0 升级为 event。

#### 4. `kg_event_causality`（事件因果链，跨卷支撑）
```sql
CREATE TABLE kg_event_causality (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    from_event_id INTEGER NOT NULL,
    to_event_id INTEGER NOT NULL,
    causality_type VARCHAR(16) NOT NULL,  -- 'caused_by' | 'causes' | 'follows' | 'parallel' | 'resolves' | 'reverses'
    confidence FLOAT DEFAULT 1.0,
    UNIQUE(project_id, from_event_id, to_event_id, causality_type)
);
```

**示例**：
- 事件 100 "林悠然入职" caused_by 事件 90 "改革公告"
- 事件 200 "顾辰逸父亲身份揭晓" causes 事件 210 "悠然卷入家族纷争"
- 事件 500 "辰逸为悠然放弃家族" resolves 事件 100 的伏笔

**百万字小说里"看到第 500 章引用第 100 章"靠这个表**。

### 时间层（锚点）

#### 5. `kg_time_anchors`（时间锚点表）
```sql
CREATE TABLE kg_time_anchors (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    chapter INTEGER,
    segment_no INTEGER,
    marker VARCHAR(200) NOT NULL,         -- "三个月后" | "次年春" | "周一上午 9 点" | "辰逸到访后第 2 天"
    era_label VARCHAR(64),                -- 自由标签 "第 1 卷开始"
    sequence INTEGER NOT NULL,            -- 项目内连续编号
    UNIQUE(project_id, sequence)
);
```

**不强制"绝对时间"**——网文里"3 个月后"是常用模糊锚点。LLM 抽什么就存什么。

#### 6. `kg_foreshadowings`（伏笔状态机，替代 facts.plothole 的弱状态）
```sql
CREATE TABLE kg_foreshadowings (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    planted_event_id INTEGER,             -- FK -> kg_events.id（埋下时）
    payoff_event_id INTEGER,              -- FK -> kg_events.id（揭晓时，NULL = 未揭晓）
    status VARCHAR(16) NOT NULL,          -- 'planted' | 'developing' | 'paid_off' | 'dropped' | 'redirected'
    plant_text VARCHAR(500),              -- 原文片段
    payoff_text VARCHAR(500),
    planned_payoff_chapter INTEGER,       -- 计划揭晓章（剧情圣经里）
    actual_payoff_chapter INTEGER,        -- 实际揭晓章
    confidence FLOAT DEFAULT 1.0,
    created_at DATETIME,
    UNIQUE(project_id, planted_event_id)
);
```

**关键升级**：伏笔从"facts 里的一个 prop" → **独立表 + 状态机**——支持 planted/developing/paid_off/dropped/redirected 五种状态。

### 检索层

#### 7. `kg_pending_extractions`（异步抽取队列）
```sql
CREATE TABLE kg_pending_extractions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    generation_id INTEGER NOT NULL,       -- 触发的 generation.id
    segment_no INTEGER,
    text_length INTEGER,
    status VARCHAR(16) NOT NULL,          -- 'pending' | 'processing' | 'done' | 'failed'
    attempt_count INTEGER DEFAULT 0,
    last_error VARCHAR(500),
    enqueued_at DATETIME,
    processed_at DATETIME,
    UNIQUE(project_id, generation_id)
);
```

**关键设计**：抽取从"同步阻塞生成"改为**异步队列**——生成主链**不等待**抽取。批处理 worker 一次抽 5-10 段。

#### 8. `kg_entity_mentions`（实体-段关联，检索用）
```sql
CREATE TABLE kg_entity_mentions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    generation_id INTEGER NOT NULL,
    segment_no INTEGER,
    context_window TEXT,                  -- 出现时上下文（前后 200 字）
    mention_type VARCHAR(16),             -- 'narrative' | 'dialogue' | 'reference'
    UNIQUE(project_id, entity_id, generation_id)
);
CREATE INDEX idx_kg_mentions_entity ON kg_entity_mentions(project_id, entity_id);
```

**关键设计**：实体-段反向索引——**检索"林悠然最近出现在哪些段"** ms 级。

## 升级对照表（v2.6 → v3.0）

| v2.6 | v3.0 | 原因 |
|---|---|---|
| `kg_nodes` | `kg_entities` (type 字段) | 统一人物/地点/物品/势力 |
| `kg_edges` | `kg_relations` (rel_type 枚举) | 强类型关系，避免漂移 |
| `facts` JSON props | 拆到 `kg_entities.attrs` + `kg_foreshadowings` | 强 schema |
| 无 | `kg_events` | 核心新增——剧情事实 |
| 无 | `kg_event_causality` | 跨卷引用支撑 |
| 无 | `kg_time_anchors` | 时间锚点 |
| `facts.plothole` | `kg_foreshadowings` (5 状态) | 伏笔状态机 |
| 同步抽取 | `kg_pending_extractions` 队列 | 异步批抽，不阻塞 |
| 无 | `kg_entity_mentions` 反向索引 | 检索加速 |

## 检索 API（v3.0 新增）

```python
def slice_for_node_prompt(
    project_id: int,
    current_event_id: int | None,        # 当前剧情点
    window: int = 5,                     # 拉最近 window 个段
) -> dict:
    """按当前剧情点动态取 KG 切片——喂给 prompt。

    返回：
    - entities_now: 当前段涉及的角色/地点/物品（含最新 attrs）
    - recent_events: 最近 5 段的事件摘要
    - active_foreshadowings: planted/developing 的伏笔
    - nearby_locations: 当前位置 + 邻近地点
    - timeline_markers: 最近时间锚
    """
```

**关键**：检索结果**带原文片段**（context_window）—— 喂给 LLM 时是**有血有肉**的事实，不是 schema 化的实体。

## 抽取策略

### 同步 vs 异步

**v2.6 同步**：每段生成 → 立即调 LLM 抽 1 段 → 写 KG
- 优点：KG 实时
- 缺点：生成主链**多 5-10 秒**（LLM 抽取耗时）

**v3.0 异步**：每段生成 → 落库 → 入队列 → 后台 worker 批抽
- 优点：主链不阻塞
- 缺点：KG 延迟 1-2 段

**选择异步**——**生成速度 > KG 实时性**——百万字生成快更重要。

### LLM 抽取合并

**v2.6 单段抽**：每段 1 次 LLM
**v3.0 批抽**：worker 一次取 5-10 段合并抽
- 节省 50%+ LLM 调用
- **跨段一致性**（同一角色跨 5 段一起抽，alias 合并更准）

## 迁移路径

v3.0 **不**做数据迁移——直接新建 v3 表，v2 表保留只读。

**v2.6 + v3.0 并行**——v2 表作为**兼容层**——extractor 同时写 v2 + v3（新项目只写 v3）。

## 工作量估计

| 步骤 | 代码量 | 时间 |
|---|---|---|
| 8 张表 schema + 迁移 | ~300 行 | 1d |
| 模型定义（SQLAlchemy） | ~400 行 | 1d |
| v3 extractor（强 schema） | ~600 行 | 2d |
| 异步 worker（批抽） | ~200 行 | 1d |
| slice_for_node_prompt 检索 | ~300 行 | 1d |
| 测试 | ~400 行 | 1d |
| 端到端 | - | 1d |
| **总计** | **~2200 行** | **8d** |

## 给用户的话

v3.0 是一次**完整重构**——8 张表 + 异步批抽 + 按需检索 API。

**关键洞察**：
- 存储不是瓶颈（百万字 ~1 万实体，每条 1KB = 10MB，SQLite 撑得住）
- **瓶颈是检索**——按当前剧情点**动态切片**才能喂 LLM
- **瓶颈是抽取成本**——异步批抽 + 跨段一致性
- **瓶颈是 schema 一致性**——强类型 props 字段，跨段不漂移

## 设计取舍

1. **不强制绝对时间**——网文里"3 个月后"就够了，不要"YYYY-MM-DD"
2. **不强 schema events**——event_type 枚举覆盖 80% 场景，留 20% 给 JSON extras
3. **不存"情绪"**——情绪是生成产物，**不该**进 KG（会导致 KG 充斥主观判断）
4. **不做跨项目 KG 共享**——每个项目独立——但 entity_type/rel_type 全局枚举共享
5. **不自动化 alias 合并**——保留人工裁决（sentinel.alias_split 流程）——LLM 合并太激进易错

## 后续 v3.1+

- **嵌入向量索引**——kg_events 加 embedding 字段，支持"按语义检索最近事件"
- **实体图谱可视化**——前端用 vis.js/d3.js 渲染 kg_entities + relations
- **KG 摘要压缩**——百万字后 entities_attrs 太长，做属性 L1 摘要
- **跨卷大纲联动**——v3.0 后卷可"前情追溯"——v3.1 加跨卷事件链查询

## 你说继续设计，先把这份设计文档给你

**你下一步**：
- A. 直接进 v3.0 实现（8 张表 → extractor → 检索）
- B. 先做 v2.7 加 4 张表**最小可用**——只 events/foreshadowings/time_anchors 3 张（**先跑通再扩展**）
- C. 改设计——你有不同看法
- D. 先看别的部分（比如生成侧 / 摘要链断裂）
