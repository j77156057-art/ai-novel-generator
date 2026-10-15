# KG v3.0 重新设计：动态生长

> 反思：8 张表 + 强 schema 的方向**错了**。精巧方案 = **1-2 张核心表
> + 节点随故事长出来**。

## 核心理念

- **最小表 + 强 link**——别按领域分（character/location/item/event 各 1 张表）
- **统一 1 张 objects 表**——`kind` 字段在运行时决定类型
- **`attrs` 始终 JSON**——不强 schema，新类型零成本
- **关系是图，不分边表/事件表**——1 张 links 表
- **mention 是反向索引**——1 张 mentions 表
- **动态生成**：故事写到 1 万字时，KG 也许只有 50 个节点；写到 100 万字时，节点长到 5 万——**自然增长**

## 三张表（不是八张）

### 1. `kg_objects`（一切实体和事件）

```sql
CREATE TABLE kg_objects (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    kind VARCHAR(16) NOT NULL,            -- 'character' | 'location' | 'item' | 'event' | 'time' | 'fact'
    canonical_name VARCHAR(128),          -- NULL for events
    aliases JSON DEFAULT '[]',            -- ["小顾", "辰逸哥哥"]
    attrs JSON DEFAULT '{}',              -- 完全自由——核心 attrs schema 写在代码里
    confidence FLOAT DEFAULT 1.0,
    first_segment_no INTEGER,             -- 第一次出现段
    last_segment_no INTEGER,              -- 最近一次段
    mention_count INTEGER DEFAULT 0,      -- 提及次数（热力）
    created_at DATETIME,
    updated_at DATETIME,
    UNIQUE(project_id, kind, canonical_name)  -- 同项目同 kind 同名不重
);
CREATE INDEX idx_kg_obj_project_kind ON kg_objects(project_id, kind);
CREATE INDEX idx_kg_obj_mentions ON kg_objects(project_id, mention_count DESC);
```

**attrs 完全自由**——但代码层有"软 schema"（运行时校验）。

```python
# 软 schema（运行时检查，不是 DB 强制）
ATTRS_SCHEMA = {
    "character": {
        "status": str,            # 'alive' | 'dead' | 'missing'
        "age": int,
        "gender": str,
        "current_location_id": int,
    },
    "event": {
        "summary": str,           # 一句话
        "actors": list,           # [object_id, ...]
        "location_id": int,
        "time_anchor_id": int,
        "chapter": int,
    },
    "foreshadowing": {
        "status": str,            # 'planted' | 'developing' | 'paid_off' | 'dropped'
        "planted_object_id": int, # 事件
        "payoff_object_id": int,  # 事件
    },
    # kind 'location' / 'item' / 'time' 不强 schema——完全自由
}
```

**关键**：**`kind` 不需要迁移**——想加新类型（'faction' / 'magic_system'）就加，attrs 用 default。

### 2. `kg_links`（一切关系和因果）

```sql
CREATE TABLE kg_links (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    from_id INTEGER NOT NULL,             -- 任何 kg_objects.id
    to_id INTEGER NOT NULL,               -- 任何 kg_objects.id
    link_type VARCHAR(32) NOT NULL,       -- 'parent_of' | 'lover_of' | 'member_of' | 'owns' | 'located_in' | 'caused_by' | 'causes' | 'follows' | 'resolves' | 'mentions'
    attrs JSON DEFAULT '{}',              -- 关系属性
    weight FLOAT DEFAULT 1.0,             -- 关系强度
    first_segment_no INTEGER,
    confidence FLOAT DEFAULT 1.0,
    created_at DATETIME
);
CREATE INDEX idx_kg_link_from ON kg_links(project_id, from_id);
CREATE INDEX idx_kg_link_to ON kg_links(project_id, to_id);
CREATE INDEX idx_kg_link_type ON kg_links(project_id, link_type);
```

**精妙**：因为 `from_id` 和 `to_id` 是**任意 kg_objects.id**——

- 人物 → 人物（lover_of, parent_of）
- 人物 → 地点（located_in, member_of）
- 人物 → 物品（owns）
- **事件 → 事件**（caused_by, follows, resolves）——**事件因果链是同一种 link**
- 物品 → 地点（located_in）

**不需要 4 张关系表**。

**伏笔**也是 link：
- `foreshadowing.planted_object_id` → `foreshadowing.payoff_object_id` 用 `link_type='resolves'` 表

### 3. `kg_mentions`（反向索引）

```sql
CREATE TABLE kg_mentions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    object_id INTEGER NOT NULL,
    generation_id INTEGER NOT NULL,
    segment_no INTEGER,
    context_window VARCHAR(500),          -- 出现时上下文（前后 200 字）
    mention_kind VARCHAR(16),             -- 'narrative' | 'dialogue' | 'reference' | 'introduced'
    UNIQUE(project_id, object_id, generation_id)
);
CREATE INDEX idx_kg_mention_obj ON kg_mentions(project_id, object_id);
CREATE INDEX idx_kg_mention_gen ON kg_mentions(project_id, generation_id);
```

**3 张表 + 1 个 attrs JSON 软 schema = 撑百万字**。

## 动态生成演示

**第 1 段生成后**：
```python
# extractor 抽
objects = [
    {"kind": "character", "canonical_name": "林悠然", "attrs": {"age": 30, "status": "alive"}},
    {"kind": "location", "canonical_name": "上海", "attrs": {}},
]
links = [
    {"from": "林悠然", "to": "上海", "link_type": "located_in"},
]
mentions = [
    {"object_name": "林悠然", "segment_no": 1, "context_window": "林悠然站在办公室窗前", "mention_kind": "introduced"},
]
```

**第 50 段生成后**：
```python
# 累计对象约 80 个（40 角色 + 30 地点 + 10 物品）
# 累计 link 约 200 个
# 累计 mention 约 500 个
# KG 表大小 1MB
```

**第 1000 段生成后**：
```python
# 累计对象约 800 个
# 累计 link 约 3000 个
# 累计 mention 约 8000 个
# KG 表大小 8MB
```

**第 3000 段生成后**（百万字）：
```python
# 累计对象约 3000 个（character 1000 + location 300 + item 200 + event 1500）
# 累计 link 约 10000 个
# 累计 mention 约 30000 个
# KG 表大小 30MB
```

**SQLite 100MB 数据库不算大**——百万字也才 30MB KG。

## 为什么这样精巧

| 维度 | 8 张表方案 | 3 张表方案 |
|---|---|---|
| 表数 | 8 | 3 |
| 迁移成本 | 高（schema 变要 ALTER TABLE） | 极低（加 `kind` 值 = 0 schema 变） |
| 新类型支持 | 改 DB schema | 加一个 `kind` 字符串 |
| 关系类型支持 | 改 DB schema | 加一个 `link_type` 字符串 |
| 检索 SQL 复杂度 | 多表 JOIN | 简单 WHERE + JSON_EXTRACT |
| 索引数 | 10+ | 5 |
| 性能 | 百万级联表 JOIN 慢 | 单表扫描 + JSON 检索 |
| 跨项目复用 | 难（每张表语义不同） | 易（同一张表 + project_id 隔离） |

**关键洞察**：3 张表 + 1 个软 schema = **关系数据库的"对象存储"**——和 MongoDB 思路同源。

## 检索 API

```python
def slice_for_prompt(
    project_id: int,
    current_segment_no: int,
    window: int = 5,
) -> dict:
    """按当前段动态取 KG 切片——喂 prompt。

    返回：
    - characters_now: 当前段涉及的活跃角色
    - location_now: 当前地点
    - recent_events: 最近 window 段的事件
    - active_foreshadowings: planted/developing 的伏笔
    - nearby_objects: 与当前对象相关联的（按 link 拉）
    """
    objs_now = db.scalars(
        select(KgObject).where(
            KgObject.project_id == project_id,
            KgObject.last_segment_no >= current_segment_no - window,
        )
    ).all()
    # ... 组装
```

**v2.6 痛点解决**——**动态取切片**，不 dump 全部。

## 抽取策略

### 异步批抽

```python
# worker（后台）
async def extract_batch(generation_ids: list[int]):
    """一次抽 5 段合并抽——节省 50%+ LLM 调用 + 跨段一致性。"""
    texts = [get_text(gid) for gid in generation_ids]
    combined = "\n\n---\n\n".join(texts)
    result = await llm.extract(combined)  # 1 次 LLM 调用
    # 拆回 5 段各自的 objects/links/mentions
```

**每秒 5-10 段抽完**——百万字 = 3000 段 = 300 次 LLM 调用 = **$0（用本地模型）/ $3（GPT-4-mini）**。

## 迁移路径

**v2.6 → v3.0 不做数据迁移**——
- v2 表保留只读
- v3 表新项目用
- 旧项目查 v2 表

**v2.6 extractor 同时写 v2 + v3**——慢慢双写过渡。

## 工作量

| 步骤 | 代码量 | 时间 |
|---|---|---|
| 3 张表 schema + 模型 | ~200 行 | 0.5d |
| 软 schema + 校验 | ~150 行 | 0.5d |
| v3 extractor | ~400 行 | 1.5d |
| 异步批抽 worker | ~200 行 | 1d |
| slice_for_prompt | ~250 行 | 1d |
| 测试 | ~300 行 | 1d |
| **总计** | **~1500 行** | **5.5d** |

**比 8 表方案少 30%**。

## 关键工程决策

1. **不要按领域分表**——user / role / permission 分表的设计在 KG 里**反模式**
2. **`attrs` JSON 软 schema**——比 DDL 强 schema 灵活 100x
3. **mention 是反向索引**——别拿 objects.attrs 数组存 segment 引用
4. **事件 = kind=event 的 object**——不要单独 events 表
5. **因果 = link_type='caused_by'**——不要单独 causality 表
6. **伏笔 = link_type='resolves'**——不要单独 foreshadowings 表

**一句话**：**3 张表 + 软 schema + 图模型 = 百万字**。

## 演进方向

**v3.0** 跑通后：
- v3.1 加向量索引（kg_objects 加 embedding 字段）——按语义检索
- v3.2 加图谱可视化（前端 vis.js）
- v3.3 加 KG 摘要压缩（百万字后 attrs 太长，LLM 压一次）
- v3.4 跨项目 KG 共享（kind/attrs schema 共享库）

## 你下一步

- A. 直接进 v3.0 实现（3 张表 + 软 schema + 异步批抽 + 检索）
- B. 先跑通 v2.7——加 attrs JSON 自由 + mention 表 1 张小改动试水
- C. 进一步讨论——也许 1 张表就够（objects+links 合一）？
- D. 改设计
