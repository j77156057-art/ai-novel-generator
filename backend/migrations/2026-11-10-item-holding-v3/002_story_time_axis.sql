-- 2026-11-10 · 持有表第二条轴：故事先后
--
-- 只排先后，不记绝对时间。年号只作可选证据写在 anchor_label，缺了不补。
-- 只新建一张旁注表，**不改** kg_item_holding 的列、约束、索引；
-- 现有 58 行不受影响（本文件未在真库执行）。
-- 全部 IF NOT EXISTS，可重复执行；与 generated_store.SCHEMA 一致。
--
-- 设计依据见 reports/kg-holding-timeline.md。
--
--   holding_id 非空 —— 主线段的旁注，指向 kg_item_holding.id
--   holding_id 为空 —— 回溯 / 预叙分支，不进章节轴，不触发关段
--
--   order_key  同一物品内可比，越大越新；NULL 表示正文看不出先后
--   order_basis explicit 正文有先后 / chapter 看不出来，退到章节序
--   刻度约定   order_key = chapter * 100，显式相对序用同一刻度插空
--              例：「开国之初」= 0；介于第 50、51 章之间 = 5050
--   这个键只表示先后，不表示距离，读侧不得做差。

CREATE TABLE IF NOT EXISTS kg_holding_story_segment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 与章节轴的关系
    holding_id INTEGER,
    item_id INTEGER,
    item_name TEXT NOT NULL CHECK (length(trim(item_name)) > 0),
    hold_type TEXT NOT NULL CHECK (hold_type IN ('own', 'custody')),

    -- 持有者：与 kg_item_holding 同口径。旁注行由 record_story_segment 同值写入，
    -- 分支行自带（分支在章节轴上没有对应行）。
    owner_id INTEGER,
    owner_name TEXT,
    owner_kind TEXT,
    owner_state TEXT NOT NULL
        CHECK (owner_state IN ('held', 'unowned', 'unknown', 'sealed')),

    -- 先后轴
    order_key INTEGER,
    order_basis TEXT NOT NULL DEFAULT 'chapter'
        CHECK (order_basis IN ('explicit', 'chapter')),
    anchor_label TEXT NOT NULL DEFAULT '',

    -- 叙述关系：forward 顺序讲述 / retro 追述 / prefigure 预叙
    narration TEXT NOT NULL DEFAULT 'forward'
        CHECK (narration IN ('forward', 'retro', 'prefigure')),

    -- 转手侧：赠予、夺、赐句里「给出方」记在这里，持有者仍取受赠方。
    transfer_from_id INTEGER,
    transfer_from_name TEXT,

    evidence_chapter INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5
        CHECK (confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL,

    -- 两档只有两种合法组合：有键=explicit，无键=chapter
    CHECK ((order_basis = 'explicit') = (order_key IS NOT NULL)),
    CHECK (order_key IS NULL OR order_key >= 0),
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

-- 一条章节轴段最多一条旁注
CREATE UNIQUE INDEX IF NOT EXISTS ux_story_segment_holding
    ON kg_holding_story_segment (holding_id)
    WHERE holding_id IS NOT NULL;

-- 分支行去重：同一物品、同一持有线、同一先后键只许一条。
-- 不能只按 (item, hold_type) 去重——同一物品本来就有多个先后位置。
CREATE UNIQUE INDEX IF NOT EXISTS ux_story_segment_branch
    ON kg_holding_story_segment
       (COALESCE(item_id, -1), item_name, hold_type, COALESCE(order_key, -1))
    WHERE holding_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_story_segment_item
    ON kg_holding_story_segment (item_id, order_key);
CREATE INDEX IF NOT EXISTS ix_story_segment_owner
    ON kg_holding_story_segment (owner_id, order_key);
