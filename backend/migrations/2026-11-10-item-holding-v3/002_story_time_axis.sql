-- 2026-11-10 · 持有表第二条轴：故事时间
--
-- 只新建一张旁注表，**不改** kg_item_holding 的列、约束、索引；
-- 现有 58 行不受影响（本文件本轮未在真库执行）。
-- 全部 IF NOT EXISTS，可重复执行；与 generated_store.SCHEMA 的建表风格一致。
--
-- 设计依据见 reports/kg-holding-timeline.md。
--   story_group 取值约定：
--     'dafeng'  —— 大奉纪年链（开国 / 贞德 / 元景），跨章可比
--     'ch:95'   —— 以第 95 章叙述时为原点的相对链，仅链内可比
--     NULL      —— 只有叙述序，只能按 evidence_chapter 排
--   story_from / story_to：
--     同一 story_group 内单调递增的整数，不同 group 之间不可比。
--     大奉历编码 = era_base + year*10000 + month*100 + day
--       era_base: 开国 0 / 贞德 1000000 / 元景 2000000
--     distance_known=0 表示只知先后、不知距离，读侧不得做减法。

CREATE TABLE IF NOT EXISTS kg_holding_story_segment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 与章节轴的关系：主线段的旁注指向 kg_item_holding.id；
    -- 回溯 / 预叙分支不进章节轴，这里为 NULL。
    holding_id INTEGER,

    item_id INTEGER,
    item_name TEXT NOT NULL CHECK (length(trim(item_name)) > 0),
    hold_type TEXT NOT NULL CHECK (hold_type IN ('own', 'custody')),

    -- 持有者：与 kg_item_holding 同口径。旁注行由 record_holding 同值写入，
    -- 分支行自带（分支在章节轴上没有对应行）。
    owner_id INTEGER,
    owner_name TEXT,
    owner_kind TEXT,
    owner_state TEXT NOT NULL
        CHECK (owner_state IN ('held', 'unowned', 'unknown', 'sealed')),

    -- 故事时间轴
    story_group TEXT,
    story_from INTEGER,
    story_to INTEGER,
    precision TEXT NOT NULL DEFAULT 'unknown'
        CHECK (precision IN ('era', 'year', 'month', 'day', 'moment', 'order', 'unknown')),
    distance_known INTEGER NOT NULL DEFAULT 0 CHECK (distance_known IN (0, 1)),
    anchor_label TEXT NOT NULL DEFAULT '',
    anchor_ref_chapter INTEGER,

    -- 叙述关系：forward 顺序讲述 / retro 追述 / prefigure 预叙
    narration TEXT NOT NULL DEFAULT 'forward'
        CHECK (narration IN ('forward', 'retro', 'prefigure')),

    -- 转手侧：赠予、夺、赐句里「给出方」记在这里，持有者仍取受赠方。
    -- transfer_event_id 是 v3 没有整数事件 id 时的空位，等价字段就是这两列。
    transfer_from_id INTEGER,
    transfer_from_name TEXT,

    evidence_chapter INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5
        CHECK (confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL,

    -- 一组「有区间就有标尺 / 有标尺就有区间」的最小自洽约束
    CHECK ((story_group IS NULL) = (story_from IS NULL)),
    CHECK (story_to IS NULL OR story_to > story_from),
    CHECK (
        (precision = 'unknown' AND story_from IS NULL)
        OR (precision <> 'unknown' AND story_from IS NOT NULL)
    ),
    -- 标尺与精度不许错配
    CHECK (COALESCE(story_group, '') <> 'dafeng'
           OR precision IN ('era', 'year', 'month', 'day', 'moment')),
    CHECK (COALESCE(story_group, '') NOT LIKE 'ch:%'
           OR precision IN ('moment', 'order')),
    -- 回溯/预叙只能落在分支行上，不许冒充主线旁注
    CHECK (narration = 'forward' OR holding_id IS NULL),
    -- 持有者口径与 kg_item_holding 完全一致
    CHECK (
        (owner_state = 'held'
         AND (owner_id IS NOT NULL
              OR (owner_name IS NOT NULL AND length(trim(owner_name)) > 0)))
        OR
        (owner_state IN ('unowned', 'sealed', 'unknown')
         AND owner_name IS NULL)
    ),
    CHECK (transfer_from_name IS NULL OR length(trim(transfer_from_name)) > 0)
);

-- 一条章节轴段最多一条旁注（要两个故事位置就写两条段，不是两条旁注）
CREATE UNIQUE INDEX IF NOT EXISTS ux_story_segment_holding
    ON kg_holding_story_segment (holding_id)
    WHERE holding_id IS NOT NULL;

-- 分支行去重：同一物品、同一持有线、同一标尺、同一区间只许一条。
-- 注意不能只按 (item, hold_type, story_group) 去重——同一标尺下本来就有多个位置。
CREATE UNIQUE INDEX IF NOT EXISTS ux_story_segment_branch
    ON kg_holding_story_segment
       (COALESCE(item_id, -1), item_name, hold_type,
        COALESCE(story_group, ''), COALESCE(story_from, -1), COALESCE(story_to, -1))
    WHERE holding_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_story_segment_item
    ON kg_holding_story_segment (item_id, story_group, story_from);
CREATE INDEX IF NOT EXISTS ix_story_segment_owner
    ON kg_holding_story_segment (owner_id, story_group, story_to);
