-- 2026-11-10 · 物品实例维度（同名多实物）
--
-- 背景：九条「地书碎片」判错的根因是这个名字同时罩着多块实物。没有实例身份，
-- 既不能判断写的是哪一块，也不能让两块同时处于持有状态。
--
-- 选型：单建实例表 kg_item_instance（片号/出处/首次出现章），
--       持有表与旁注表各加一个纯整数引用列 instance_id。
-- 理由：
--   1. 片号、原文依据、首次出现章是实例属性，放在注册表里只存一份；
--      持有行只带一个引用，不用在每行重复片号，也不会各写各的。
--   2. ALTER TABLE ADD COLUMN 是 SQLite 唯一能原地做的结构变更，
--      但它加不了 CHECK / UNIQUE，所以列上不加约束，靠写入口校验
--      （record_item_instance / record_holding / record_story_segment）。
--   3. 片号必须来自正文（「三号碎片」「第一块」）；抽不到就不建实例、
--      不建段，绝不自行编号。
--
-- 唯一约束怎么共存：
--   - 表级 UNIQUE(item_id, hold_type, valid_from) 对 item_id IS NULL 天然失效
--     （SQLite 里 NULL 互不相等）。地书碎片的 item_id 为空，所以两块碎片
--     同章开段不会撞这条；代价是它对碎片也不提供去重，由写入口兜。
--   - 开放段唯一索引 ux_item_holding_open 原来只到 (item_id, item_name, hold_type)，
--     两块碎片同时开放会撞。必须重建，把 COALESCE(instance_id,-1) 并进键。
--   - 分支唯一索引 ux_story_segment_branch 同理。
--
-- 本文件只跑一次（ALTER 不能重复执行）；重复执行由落库脚本按 PRAGMA 跳过。
-- 不写任何数据，58 行不受影响。

CREATE TABLE IF NOT EXISTS kg_item_instance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_name TEXT NOT NULL CHECK (length(trim(item_name)) > 0),
    item_id INTEGER,
    instance_label TEXT NOT NULL CHECK (length(trim(instance_label)) > 0),
    evidence_chapter INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (item_name, instance_label)
);

ALTER TABLE kg_item_holding ADD COLUMN instance_id INTEGER;
ALTER TABLE kg_holding_story_segment ADD COLUMN instance_id INTEGER;

DROP INDEX IF EXISTS ux_item_holding_open;
CREATE UNIQUE INDEX ux_item_holding_open
    ON kg_item_holding (COALESCE(item_id, -1), item_name, hold_type,
                        COALESCE(instance_id, -1))
    WHERE valid_to IS NULL;

DROP INDEX IF EXISTS ux_story_segment_branch;
CREATE UNIQUE INDEX ux_story_segment_branch
    ON kg_holding_story_segment
       (COALESCE(item_id, -1), item_name, hold_type,
        COALESCE(instance_id, -1), COALESCE(order_key, -1))
    WHERE holding_id IS NULL;
