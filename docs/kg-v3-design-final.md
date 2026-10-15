# KG v3.0 工业界对齐设计 v2（吸收 3 个产品经验）

> 上一版（v3.0 industry-aligned）学了 3 个产品：**The Narrative Graph（商业）**、
> **Novelium（专业写作软件）**、**leenbj/novel-creator-skill（开源）**。
>
> 这次吸收 3 个产品的**具体实践**——加 5 个字段、2 张表、5 个产品功能、
> 1 个独立 agent。

## 3 个产品的关键洞察

### The Narrative Graph（95% 一致性）
- **Judge Agent 独立**——不是 prompt 后处理，是独立 agent
- **Triple-Peer Review**——3 agent 互相挑刺
- **Compliance Log**——所有话都要被 KG 验证

### Novelium（专业写作软件）
- **5 大矛盾检测**：time stretch / deceased / knows / location / storylines
- **实时 inline 提示**——边写边标红
- **Mark as intentional**——作者豁免机制
- **物理时间校验**——怀孕月份 / 季节

### leenbj skill（开源完整 schema）
- **8 种节点**：character / location / faction / item / event / foreshadow / worldrule / power_system
- **Edge strength + evolution**——关系有强度有演变
- **Timeline 包含 location_changes + status_changes**
- **Foreshadow 状态机**：planted/recalled/expired
- **4 阶段操作协议**：每章写后 / 改纲级联 / 一致性校验 / 导出

## 8 张表（v3.0 final）

### 1. `kg_entities`（6 种节点类型）

```sql
CREATE TABLE kg_entities (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    kind VARCHAR(16) NOT NULL,           -- 'character' | 'location' | 'faction' | 'item' | 'worldrule' | 'power_system'
    canonical_name VARCHAR(128) NOT NULL,
    aliases JSON DEFAULT '[]',
    attrs JSON DEFAULT '{}',             -- 软 schema：arc / current_goal / secrets / power_level / region / leader_id ...
    first_chapter INTEGER,
    last_chapter INTEGER,
    mention_count INTEGER DEFAULT 0,
    confidence FLOAT DEFAULT 1.0,
    created_at DATETIME,
    updated_at DATETIME,
    UNIQUE(project_id, kind, canonical_name)
);
```

**关键字段**（来自 leenbj skill）：
- `attrs.arc`——角色弧
- `attrs.current_goal`——当前目标
- `attrs.secrets`——秘密数组
- `attrs.power_level`——力量等级
- `attrs.region`——所属地区

### 2. `kg_relations`（关系图，**带强度+演变**）

```sql
CREATE TABLE kg_relations (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    from_id INTEGER NOT NULL,
    to_id INTEGER NOT NULL,
    rel_type VARCHAR(32) NOT NULL,       -- 'ally' | 'enemy' | 'mentor' | 'subordinate' | 'romantic' | 'belongs_to' | 'located_at' | 'owns'
    strength FLOAT DEFAULT 0.5,         -- ★ leenbj skill: 关系强度 0-1
    since_chapter INTEGER,
    description VARCHAR(200),
    confidence FLOAT DEFAULT 1.0,
    created_at DATETIME,
    updated_at DATETIME,
    UNIQUE(project_id, from_id, to_id, rel_type)
);
```

### 3. `kg_relation_evolution`（★ 关系演变表）

```sql
CREATE TABLE kg_relation_evolution (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    edge_id INTEGER NOT NULL,            -- FK -> kg_relations.id
    chapter INTEGER,
    strength FLOAT NOT NULL,             -- 新强度
    note VARCHAR(200),                    -- 叙事支撑
    created_at DATETIME
);
```

**示例**（太子和魏征的关系）：
- ch5: strength=0.3, note="初次合作"
- ch15: strength=0.8, note="共渡危机"
- ch40: strength=0.5, note="政见分歧"

**这个表 v3.0 之前我没设计**——是 leenbj 的核心。

### 4. `kg_events`（事件图，**SVO triplet + location 必填**）

```sql
CREATE TABLE kg_events (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    chapter INTEGER,
    segment_no INTEGER,
    event_type VARCHAR(32) NOT NULL,     -- 'incident' | 'revelation' | 'foreshadow_plant' | 'foreshadow_payoff' | 'death' | 'romance_milestone' | 'battle' | 'decision' | 'time_marker'
    summary VARCHAR(500),
    subject_id INTEGER,                  -- ★ SVO: 主体
    verb VARCHAR(64),                    -- ★ SVO: 动作
    object_id INTEGER,                   -- ★ SVO: 客体
    location_id INTEGER,                 -- ★ Novelium 物理时间校验需要
    time_anchor_id INTEGER,              -- FK -> kg_time_anchors
    in_story_date VARCHAR(64),           -- ★ leenbj: "贞观三年秋" 故事内时间
    extra_actors JSON DEFAULT '[]',      -- 参与角色超过 2 个时存这里
    confidence FLOAT DEFAULT 1.0,
    evidence_segment_ids JSON DEFAULT '[]',
    created_at DATETIME,
    UNIQUE(project_id, sequence)
);
```

**关键洞察**：`in_story_date` 是**关键字段**——Novelium 的物理时间校验靠这个。

### 5. `kg_event_links`（事件因果链）

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

### 6. `kg_character_state`（★ 角色状态变化表，leenbj 核心）

```sql
CREATE TABLE kg_character_state (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,        -- FK -> kg_entities (kind=character)
    chapter INTEGER,
    state_type VARCHAR(16) NOT NULL,      -- 'location' | 'injury' | 'mood' | 'goal' | 'inventory' | 'alive'
    new_value VARCHAR(500),               -- 变化后的值（JSON 编码）
    cause_event_id INTEGER,               -- 导致变化的事件
    created_at DATETIME
);
CREATE INDEX idx_kg_char_state_char ON kg_character_state(project_id, character_id, chapter);
```

**示例**：
- 林悠然 ch50: state_type=location, new_value="上海陆家嘴办公室"
- 林悠然 ch80: state_type=injury, new_value="右手骨折"
- 林悠然 ch100: state_type=goal, new_value="找到父亲死因"

**这是 Novelium "Character Dashboard" 的数据结构**——**每章追踪状态变化**。

### 7. `kg_foreshadows`（★ 伏笔状态机表，**独立不混入 kg_events**）

```sql
CREATE TABLE kg_foreshadows (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    name VARCHAR(200) NOT NULL,
    planted_event_id INTEGER,             -- 埋下时的事件
    payoff_event_id INTEGER,              -- 揭晓时的事件（NULL=未揭晓）
    status VARCHAR(16) NOT NULL,         -- 'planted' | 'developing' | 'paid_off' | 'dropped' | 'expired'
    planted_chapter INTEGER,
    planned_payoff_chapter INTEGER,       -- 计划揭晓章
    actual_payoff_chapter INTEGER,        -- 实际揭晓章
    description VARCHAR(500),
    confidence FLOAT DEFAULT 1.0,
    created_at DATETIME,
    UNIQUE(project_id, name)
);
```

**关键洞察**（leenbj skill 启发）：
- **伏笔是独立表**——**不**用 kg_event_links 间接表达
- 5 状态机：`planted/developing/paid_off/dropped/expired`
- **`expired`**——超期未回收（Novelium: "伏笔超期未回收"）

### 8. `kg_entity_knowledge`（人物情报追踪，Novelium 卖点）

```sql
CREATE TABLE kg_entity_knowledge (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,           -- 人物 id
    known_entity_id INTEGER NOT NULL,     -- 知道的另一个 entity / event / fact
    known_kind VARCHAR(16),               -- 'entity' | 'event' | 'secret'
    learned_at_event_id INTEGER,          -- 在哪个事件学到的
    learned_at_chapter INTEGER,
    confidence FLOAT DEFAULT 1.0,
    UNIQUE(project_id, entity_id, known_entity_id, learned_at_event_id)
);
```

**Novelium 卖点**：每个角色有"情报"——LLM 生成时**反查"林悠然现在知道什么"**。

### 9. `kg_mentions`（反向索引）

```sql
CREATE TABLE kg_mentions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    generation_id INTEGER NOT NULL,
    segment_no INTEGER,
    context_window VARCHAR(500),
    mention_kind VARCHAR(16),
    UNIQUE(project_id, entity_id, generation_id)
);
```

## 8 个产品功能（比之前 +3 个）

| # | 功能 | 表 | 工业界来源 |
|---|---|---|---|
| 1 | **人物情报追踪** | kg_entity_knowledge | Novelium |
| 2 | **生成后一致性检测** | kg + state | leenbj + sentinel |
| 3 | **物理时间校验** | kg_events.in_story_date | Novelium |
| 4 | **空间/时间逻辑** | kg_character_state | leenbj |
| 5 | **大纲级联** | kg_event_links | leenbj |
| 6 | **节奏分析** | kg_mentions | Novelium |
| 7 | **关系演变曲线** | kg_relation_evolution | **leenbj 独有** |
| 8 | **作者豁免** | kg + meta | **Novelium 独有** |

## 抽取策略（4 阶段，leenbj 协议）

```python
# 阶段 1：每段写后（自动）
async def extract_after_segment(generation_id: int):
    text = get_text(generation_id)
    result = await llm.extract(text)
    return result

# 阶段 2：更新 KG（5 个动作）
def update_kg(extraction: dict):
    upsert_entities(extraction["entities"])             # 1
    upsert_relations(extraction["relations"])            # 2
    append_events(extraction["events"])                 # 3
    update_character_state(extraction["state_changes"])  # 4
    update_foreshadows(extraction["foreshadows"])        # 5

# 阶段 3：一致性校验（5 类）
def validate(project_id: int, candidate_text: str = None):
    issues = []
    issues += check_deceased_speaking()                   # 死人说话
    issues += check_impossible_sequences()               # 时间穿越
    issues += check_teleportation()                       # 瞬移
    issues += check_unauthorized_knowledge()             # 预知外挂
    issues += check_foreshadow_expiry()                  # 伏笔超期
    return issues

# 阶段 4：作者豁免
def mark_as_intentional(issue_id: int, author_note: str):
    db.insert(kg_author_exemption(...))   # 豁免表
```

## 与"故事世界"其他层的配合

| 层 | 配合方式 | 频率 |
|---|---|---|
| **语料蒸馏**（style_narrative）| 喂给 LLM 生成 prompt | 每次生成 |
| **大纲**（outline）| plot_timeline 节点规划 | 创建项目 |
| **节点**（plot_timeline）| 关联 kg_events，驱动 current_node_prompt | 每段 |
| **主线/分支** | 节点的 kind 字段，roll_branch 检测 KG signal | 节点切换 |
| **时间脉络** | kg_events.in_story_date | 每段 |
| **KG** | **所有层都引用** | 持续 |
| **生成** | prompt 拿所有层切片 | 每段 |
| **质检** | Judge Agent 用 KG 校验 | 每段 |

## 独立 Judge Agent（Narrative Graph 启发）

```python
class JudgeAgent:
    """独立进程，只读 KG，校验生成结果。

    关键设计：Judge Agent 不参与生成，只做合规检查。
    Writer Agent 不感知 Judge Agent 的存在。
    """
    def __init__(self, db: Session, project_id: int):
        self.kg = KGProject(db, project_id)

    def check(self, candidate_text: str) -> ComplianceLog:
        log = ComplianceLog()
        log.add("deceased_speaking", self.check_deceased_speaking(candidate_text))
        log.add("teleportation", self.check_teleportation(candidate_text))
        log.add("unauthorized_knowledge", self.check_unauthorized_knowledge(candidate_text))
        log.add("impossible_sequences", self.check_impossible_sequences(candidate_text))
        log.add("foreshadow_expiry", self.check_foreshadow_expiry())
        return log
```

## 抽取 LLM 输出 schema（软 schema）

```python
EXTRACTION_SCHEMA = {
    "entities": [
        {
            "kind": "character|location|faction|item|worldrule|power_system",
            "canonical_name": str,
            "aliases": list,
            "attrs": {
                "arc": str,
                "current_goal": str,
                "secrets": list,
                "power_level": str,
            },
        }
    ],
    "relations": [
        {
            "from": str,             # entity name
            "to": str,               # entity name
            "rel_type": str,
            "strength": float,       # 0-1
            "since_chapter": int,
        }
    ],
    "events": [
        {
            "sequence": int,         # 跨项目连续
            "chapter": int,
            "event_type": str,
            "summary": str,
            "subject": str,           # entity name
            "verb": str,
            "object": str,            # entity name
            "location": str,          # entity name（必填）
            "in_story_date": str,     # 故事内时间
        }
    ],
    "state_changes": [
        {
            "character": str,
            "state_type": "location|injury|mood|goal|inventory",
            "new_value": str,
            "cause_event_sequence": int,
        }
    ],
    "foreshadows": [
        {
            "name": str,
            "status": "planted|developing|paid_off|dropped",
            "planted_chapter": int,
            "planned_payoff_chapter": int,
        }
    ],
}
```

## 工作量（v3.0 final）

| 步骤 | 代码量 | 时间 |
|---|---|---|
| 9 张表 schema + 模型 | ~350 行 | 1d |
| 软 schema + 校验 | ~200 行 | 0.5d |
| v3 extractor | ~600 行 | 2d |
| 异步批抽 worker | ~200 行 | 1d |
| Judge Agent（独立） | ~300 行 | 1d |
| 8 个产品功能 API | ~500 行 | 1.5d |
| Inline 提示 + 作者豁免 | ~200 行 | 0.5d |
| 测试 | ~500 行 | 1.5d |
| **总计** | **~2850 行** | **9d** |

**比 v3.0 industry-aligned 多 35%**——因为加了 4 张新表 + Judge Agent + 8 个产品功能。

## 演进路径

- **v3.0**（9d）——9 张表 + Judge Agent + 8 个产品功能 + 4 阶段操作协议
- **v3.1**（2d）——多 agent 互相挑刺（Narrative Graph Triple-Peer）
- **v3.2**（1d）——自动 story bible scaffolding
- **v3.3**（2d）——Neo4j 替代 SQLite
- **v3.4**（3d）——前端可视化（关系图/时间线/节奏图/Inline 提示）

## 核心 3 个产品给我的最终启示

1. **The Narrative Graph**：KG 必须驱动独立的 Judge Agent——不是 prompt 后处理
2. **Novelium**：KG 必须是**实时的**——Inline 提示+作者豁免
3. **leenbj skill**：KG 必须有 **状态变化**（character_state + relation_evolution）——**不是只记当前**，要记**过程**

**我们 v2.6 的 KG 缺这 3 块**——这就是差距。

## 你下一步

- A. 让我写完整 v3.0 final 文档
- B. 选 1-2 个产品功能先做（**物理时间校验**最简单 / **人物情报追踪**最实用）
- C. 讨论某个具体功能
- D. 看其他产品
- E. 直接进 v3.0 实现
