import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.config import settings

Base = declarative_base()

DATABASE_URL = settings.database_url
IS_SQLITE = DATABASE_URL.startswith("sqlite")

connect_args = {"check_same_thread": False} if IS_SQLITE else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, future=True
)


# ── 真实库防误删：结构性防线 ────────────────────────────────────────────────
# 2026-10-17 事故：某个脚本对 `<app.db 的模块级 engine>` 调 `Base.metadata.drop_all()`，
# 而该 engine 的 URL 来自 `settings.database_url`（默认 `sqlite:///./novel_dev.db`，
# **相对 CWD 解析**），于是一句 drop_all 就把真实开发库清空（122.9MB 只剩 1 个已用页，
# 7941 个分片全丢）。
#
# 为什么不能只靠「改默认值」或「让每个脚本自觉加隔离」：
#   - `config.Settings` 用 `env_file=".env"`（相对 CWD 自动加载），任何在 backend/ 下
#     跑的进程都会读到同一份 .env，改默认值并不能把它和真实库分开；
#   - 「每个脚本自己设 DATABASE_URL」靠的是纪律，挡不住未来新写的脚本。
#
# 所以防线放在**驱动层**：监听 `Engine` 类的 `before_cursor_execute`，
# 在真实库上拒绝 `DROP TABLE`。这样无论入口是 pytest、手工脚本还是 `python -c`，
# 只要 engine 指向真实库，破坏性操作就会被拦下。确实需要重置真实库时，
# 显式设 `ALLOW_DESTRUCTIVE_DB=1`（显式即安全）。
_PROTECTED_DB_NAMES = frozenset({"novel_dev.db", "novel_demo.db"})


def _sqlite_basename(url) -> str | None:  # noqa: ANN001
    """取 sqlite URL 的文件名；非 sqlite 或内存库返回 None。"""
    s = str(url)
    if not s.startswith("sqlite"):
        return None
    if ":memory:" in s or s.rstrip("/") in {"sqlite://", "sqlite:///"}:
        return None
    path = s.split("///", 1)[-1].split("?", 1)[0]
    return path.replace("\\", "/").rsplit("/", 1)[-1] or None


def is_protected_db(url) -> bool:  # noqa: ANN001
    """该 URL 是否指向真实库（会被防误删防线保护）。"""
    return _sqlite_basename(url) in _PROTECTED_DB_NAMES


@event.listens_for(Engine, "before_cursor_execute")
def _block_drop_table_on_real_db(  # noqa: ANN001
    conn, cursor, statement, parameters, context, executemany
):
    if os.environ.get("ALLOW_DESTRUCTIVE_DB") == "1":
        return
    if not statement:
        return
    if not statement.lstrip().upper().startswith("DROP TABLE"):
        return
    try:
        url = conn.engine.url
    except Exception:  # pragma: no cover - 取不到 URL 时不拦（宁放过不误伤）
        return
    if is_protected_db(url):
        raise RuntimeError(
            f"拒绝在真实库上执行 DROP TABLE（目标：{url}）。\n"
            "这是 2026-10-17 清库事故（drop_all 清空 novel_dev.db，7941 分片丢失）的防线。\n"
            "若确实要重置该库，请显式设置环境变量 ALLOW_DESTRUCTIVE_DB=1；\n"
            "若只是想跑测试，请让 engine 指向临时库（tests/conftest.py 已自动隔离）。"
        )


if not IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _enable_pgvector(dbapi_conn, *args):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:  # pragma: no cover - 扩展已存在时跳过
            pass
        finally:
            cur.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """服务启动时的表结构初始化（建缺失表 + 补缺失列）。"""
    ensure_schema()


def ensure_schema(target_engine=None) -> None:  # noqa: ANN001  (SQLAlchemy Engine | None)
    """建缺失表 + 对已存在的 sqlite 库补齐新增列（幂等）。

    抽出成公共函数供**两个入口**复用，避免离线脚本绕过迁移：

    - 服务启动 `init_db()` —— 用模块级默认 engine；
    - 离线脚本 `load_corpus.py` —— 用它自己的**绝对路径** engine。

    为什么要给离线脚本复用：`load_corpus.py` 直接 `create_engine()` 建连接
    （不经 `app.db`），若不走这一段，已存在的生产库里就没有第 8 步新增的
    `raw_novels.clean_report` 列，导入时会 `no such column: clean_report` 失败。

    Args:
        target_engine: 目标引擎。缺省用本模块的 `engine`。
    """
    import app.models  # noqa: F401  (确保模型注册到 Base.metadata)

    eng = target_engine if target_engine is not None else engine
    Base.metadata.create_all(bind=eng)

    # —— 增量迁移：对已存在的 sqlite 库补齐新增列，避免清库重灌 ——
    if str(eng.url).startswith("sqlite"):
        _migrate_sqlite_columns(eng)


def _migrate_sqlite_columns(target_engine=None) -> None:  # noqa: ANN001
    """补齐 sqlite 库缺失的列。`target_engine` 缺省用模块级 `engine`。"""
    from sqlalchemy import text

    # (表, 列, 类型) —— 仅当列不存在时才 ALTER ADD。
    #
    # 幂等性：每项都先PRAGMA table_info 查列，缺才ALTER；重复调用 init_db()
    # 不会重复加列、不会报错。新表（prompt_overrides）不需要出现在这里——
    # Base.metadata.create_all 会建好；这里只处理「已存在的库补列」。
    migrations = [
        ("projects", "genre", "VARCHAR(32)"),
        ("projects", "style", "VARCHAR(32)"),
        ("projects", "premise", "TEXT"),
        ("projects", "reference_novel_ids", "JSON"),
        # —— 「当前书」：单值、可空。NULL = 未指定，读侧不按书过滤（现有行为）——
        # 与 reference_novel_ids（多值参考语料集合）区分：后者回答「用了哪些书」，
        # 这一列回答「此刻按哪一本」。漏这一项的后果与 style_narrative 相同：
        # 老库 SELECT projects.* 会 no such column: projects.active_novel_id。
        ("projects", "active_novel_id", "INTEGER"),
        # —— 「续写书」：单值、可空。NULL = 原创/未指定 ——
        # 回答的是「从哪本书往后写」，与 active_novel_id（事实来自哪本书）分开。
        ("projects", "continuation_novel_id", "INTEGER"),
        ("projects", "plot_bible", "JSON"),
        ("projects", "generation_length", "INTEGER NOT NULL DEFAULT 3000"),
        ("projects", "summary_tree", "JSON"),
        ("projects", "style_profile", "JSON"),
        # —— 第 10 步 P2 叙事档案：独立于 style_profile 的 JSON 列——
        # 漏这一项的后果：create_all 只给**新库**建该列，已存在的库永远补不上，
        # 任何 SELECT projects.* 都会 `no such column: projects.style_narrative`。
        # 详见 tests/test_schema_migration_completeness.py。
        ("projects", "style_narrative", "JSON"),
        # —— 文风蒸馏三档：项目级「用哪一档算」的偏好（full/balanced/fast）——
        # nullable：既有项目的 style_narrative 是按默认完整档算的，补 NULL 不破坏它们。
        # 漏这一项的后果与 style_narrative 相同：老库 SELECT projects.* 直接炸。
        ("projects", "narrative_tier", "VARCHAR(16)"),
        # —— 蒸馏采样章数覆盖：None=档位默认；0=全本；N=最多 N 章 ——
        # nullable：老项目补 NULL = 继续用档位默认口径，行为不变。
        ("projects", "narrative_sample_chapters", "INTEGER"),
        # —— 引导会话的流程改写（跳过/排序/自定义问题，白名单约束）——
        # 老库补默认空对象，answers/history 语义不受影响。
        ("assistant_wizard_sessions", "overrides", "JSON NOT NULL DEFAULT '{}'"),
        # —— 蒸馏任务看护：最后一次进度心跳 + 预计耗时（外推，非实测）——
        # 老库补 NULL：心跳缺省时按 started_at/created_at 兜底，判断不因此静默失效；
        # expected_minutes 为 NULL 时只做「无进度时长」判断，不编超时倍数。
        ("distill_runs", "progress_at", "DATETIME"),
        ("distill_runs", "expected_minutes", "INTEGER"),
        # —— §11.3 时间线状态存档：节点+进度一体化 JSON ——
        # 漏这一项的后果：create_all 只给**新库**建该列，已存在的库永远补不上，
        # 任何 SELECT projects.* 都会 `no such column: projects.plot_timeline`，
        # 时间线状态就退化回「每次重启重放」旧行为（功能不挂，但失去持久化收益）。
        ("projects", "plot_timeline", "JSON"),
        # —— v2.4 高潮密度蒸馏档：avg_node_segments 从语料章节情绪反推 ——
        # 漏这一项后果与 plot_timeline 相同：老库 SELECT projects.* 直接炸。
        # 该列 nullable：未蒸馏时为空，回退到首轮默认 avg_node_segments=5。
        ("projects", "plot_rhythm", "JSON"),
        ("raw_novels", "style_profile", "JSON"),
        # —— 第 8 步：语料清洗报告（导入时剔除水印的留痕）——
        ("raw_novels", "clean_report", "JSON"),
        # —— 复读闸门标记：NULL = 未检测（历史行 / 回滚恢复行），不是「正常」——
        # 漏这一项的后果：老库 INSERT 含 quality_json 直接炸，生成整条链失败。
        ("generations", "quality_json", "TEXT"),
        # —— 2026-11-18 B 子系统第二刀：当次 prompt/权威快照 ——
        # 只加列不回填：NULL = 该行生成于本列落地前（不是「没有提示词」）。
        ("generations", "prompt_json", "TEXT"),
        ("generations", "authority_json", "TEXT"),
        ("kg_nodes", "source", "VARCHAR(64)"),
        # —— v3.0 KG 书维度：实体/提及归属哪本书 ——
        # NULL = 项目级通配（归不出书的行 / 老数据），读侧与加列前完全一致。
        # 漏这一项的后果：老库 SELECT kg_entities.* 直接 no such column: novel_id。
        ("kg_entities", "novel_id", "INTEGER"),
        ("kg_mentions", "novel_id", "INTEGER"),
        # 事件同样要能区分「本项目原创」与「从某本书迁入/借用」；NULL = 项目级通配。
        ("kg_events", "novel_id", "INTEGER"),
        # —— 关系边/伏笔补书维度（add-only，不回填）——
        # 此前这两张表没有 novel_id，同一项目下两本书的关系与伏笔混在一张表里。
        # NULL = 项目级通配 / 老数据；读侧按「当前书 + NULL」过滤，历史 NULL 不猜。
        # 漏这一项的后果：老库 SELECT kg_relations.* / kg_foreshadows.* 直接
        # no such column: novel_id，写侧盖书也写不进去。
        ("kg_relations", "novel_id", "INTEGER"),
        ("kg_foreshadows", "novel_id", "INTEGER"),
        # —— ADR-2026-10-12 第 5 步第 4 步：EditProposal 补齐提案生命周期字段——
        # 全部 nullable：既有行（第 2/3 步写入的）不该被 NOT NULL 破坏。
        ("edit_proposals", "project_id", "INTEGER"),
        ("edit_proposals", "type", "VARCHAR(32)"),
        ("edit_proposals", "target_kind", "VARCHAR(32)"),
        ("edit_proposals", "scope_json", "JSON"),
        ("edit_proposals", "before_text", "TEXT"),
        ("edit_proposals", "after_text", "TEXT"),
        ("edit_proposals", "reason", "TEXT"),
        ("edit_proposals", "evidence_json", "JSON"),
        ("edit_proposals", "version_id", "INTEGER"),
        ("edit_proposals", "created_by", "VARCHAR(64)"),
        ("edit_proposals", "created_at", "DATETIME"),
        ("edit_proposals", "approved_by", "VARCHAR(64)"),
        ("edit_proposals", "approved_at", "DATETIME"),
        ("edit_proposals", "applied_at", "DATETIME"),
        # —— 第 7 步：AgentRun 增加调用性质说明列（invoke-generation 用）——
        # nullable：既有审计行（第 2/3/4 步写入的）没有这项，补 NULL 不破坏它们。
        ("agent_runs", "note", "TEXT"),
        # —— v3.0 KG：secret/event 类情报的事实文本（known_kind 消费路径）——
        # 漏这一项的后果与 style_narrative 相同：老库 INSERT 含 known_text 直接炸。
        ("kg_entity_knowledge", "known_text", "VARCHAR(200)"),
        # —— v3.0 KG：关系边的正式/候选状态（集合端点门降级用）——
        # 老行补列即有 'active'，迁移只加列、不改列，读路径按 active 过滤。
        ("kg_relations", "status", "VARCHAR(16) NOT NULL DEFAULT 'active'"),
        # —— Version 补齐快照指纹与归属——
        ("versions", "project_id", "INTEGER"),
        ("versions", "kind", "VARCHAR(16)"),
        ("versions", "content_hash", "VARCHAR(64)"),
        ("versions", "source_proposal_id", "INTEGER"),
        ("versions", "is_restore", "BOOLEAN NOT NULL DEFAULT 0"),
        # —— AI 腔记忆升级为可复用方案库：solution 系列六列 ——
        # 为什么必须进清单：本表虽是基线之后新建的表，但「表已存在、列不存在」
        # 的老库（建表那次提交之后、本次升级之前的库）create_all 不会补列——
        # 它只补缺失的表。任何 SELECT 都会 no such column。
        # 全部 NOT NULL 列都带 DEFAULT：老行补列即有值，迁移只加列、不删列、
        # 不改列；last_reused_at 可空表示「从未被复用」。
        ("ai_flavor_memories", "solution", "TEXT NOT NULL DEFAULT ''"),
        ("ai_flavor_memories", "solution_origin", "VARCHAR(16) NOT NULL DEFAULT ''"),
        ("ai_flavor_memories", "times_reused", "INTEGER NOT NULL DEFAULT 0"),
        ("ai_flavor_memories", "last_reused_at", "DATETIME"),
        ("ai_flavor_memories", "recurrences", "INTEGER NOT NULL DEFAULT 0"),
        ("ai_flavor_memories", "flagged_ineffective", "BOOLEAN NOT NULL DEFAULT 0"),
        # —— 重写快照的在途租约（2026-11-12）——
        # 只加列、**不回填**：存量 pending 行三列保持 NULL。按 created_at 补
        # 心跳等于给僵尸行发一张新租约（会让崩溃残留多活一个判据周期）；
        # 补 pid 更糟，可能撞上被复用的 pid。NULL 的语义是「升级前写入、
        # 无租约证据」，启动收割按「pid 不存在」清掉。
        ("generation_snapshots", "owner_pid", "INTEGER"),
        ("generation_snapshots", "owner_started_at", "VARCHAR(64)"),
        ("generation_snapshots", "heartbeat_at", "DATETIME"),
        # —— 重写的局部范围校验（2026-11-12）——
        # 「指令限定局部、结果却像整段重写」的提示结构。只加列、不回填：
        # 存量快照的 NULL 语义是「未做过这项判定」，而不是「没有偏差」。
        ("generation_snapshots", "scope_mismatch", "JSON"),
        # —— 重写的篇幅跌幅（2026-11-12）——
        # 与 scope_mismatch 分列：首尾是否保留、篇幅是否缩水是两类问题。
        ("generation_snapshots", "length_drop", "JSON"),
        # —— 含糊指令的候选澄清（2026-11-12）——
        # {round, candidates, fingerprint, note}；只加列、不回填，NULL = 非澄清行。
        ("generation_snapshots", "clarification", "JSON"),
        # —— 目录检索轨迹表（设计稿 §5.8）：轨迹与检索流水是同一张表 ——
        # 这张表可能在本次升级之前就被 create_all 建过（只有 id/session_id/tool 等旧列），
        # 而 create_all 只补缺失表、不补列，所以新字段必须逐项进清单。
        # 全部带 DEFAULT 或可空：老行（目前为空表，但口径要一致）补列即有值。
        ("retrieval_logs", "project_id", "INTEGER"),
        ("retrieval_logs", "agent_kind", "VARCHAR(16) NOT NULL DEFAULT 'author'"),
        ("retrieval_logs", "turn_no", "INTEGER NOT NULL DEFAULT 1"),
        ("retrieval_logs", "seq", "INTEGER NOT NULL DEFAULT 1"),
        ("retrieval_logs", "segment_no", "INTEGER"),
        ("retrieval_logs", "hit_ids", "JSON"),
        ("retrieval_logs", "budget_remaining", "JSON"),
        ("retrieval_logs", "budget_pool", "VARCHAR(16)"),
        ("retrieval_logs", "adopted_rule_ids", "JSON"),
        ("retrieval_logs", "soft_flags", "JSON"),
        # —— KG 按章版本化（2026-11-14）——
        # 同章重写不物理删行：旧版本 is_current=0 保留可回溯，读侧（切片/检索/
        # 状态渲染）只认 is_current=1；version 记「同章第几次入库」。
        # 老行补列即有 1/1 = 当前第 1 版，行为与加列前一致（不隐藏任何存量数据）。
        # 漏这一项的后果：老库 SELECT 这些表直接 no such column: is_current。
        ("kg_events", "is_current", "BOOLEAN NOT NULL DEFAULT 1"),
        ("kg_events", "version", "INTEGER NOT NULL DEFAULT 1"),
        ("kg_mentions", "is_current", "BOOLEAN NOT NULL DEFAULT 1"),
        ("kg_mentions", "version", "INTEGER NOT NULL DEFAULT 1"),
        ("kg_relations", "is_current", "BOOLEAN NOT NULL DEFAULT 1"),
        ("kg_relations", "version", "INTEGER NOT NULL DEFAULT 1"),
        ("kg_character_state", "is_current", "BOOLEAN NOT NULL DEFAULT 1"),
        ("kg_character_state", "version", "INTEGER NOT NULL DEFAULT 1"),
        # 错位行重标的留痕（见 app/kg/relabel_misplaced.py）：NULL = 未经修复
        ("kg_events", "note", "TEXT"),
        ("kg_character_state", "note", "TEXT"),
        # —— 2026-11-16 从 main（freeze/main-cb05a45）补齐的 main 独有列 ——
        # 全部 add-only：nullable，或 NOT NULL 但带 DEFAULT（老行补列即有值）。
        # 不删列、不改列、不回填历史行；备份 = 升级前复制 novel_dev.db。
        # 漏项的后果与 style_narrative 相同：老库 SELECT 这些表直接 no such column。
        ("projects", "style_rewrite", "INTEGER NOT NULL DEFAULT 0"),
        ("projects", "narrative_scope", "JSON"),
        ("projects", "character_template_id", "VARCHAR(96)"),
        ("projects", "reference_cutoffs", "JSON"),
        ("projects", "story_authority", "VARCHAR(32) NOT NULL DEFAULT 'generated_branch'"),
        ("projects", "continuation_start_chapter", "INTEGER"),
        # distill_runs.kind 区分 narrative / character_template / book_digest 三种产物；
        # 老行补 'narrative' = 与加列前的唯一行为一致（当时只有叙事蒸馏）。
        ("distill_runs", "kind", "VARCHAR(24) NOT NULL DEFAULT 'narrative'"),
        ("distill_runs", "scope_json", "JSON"),
        ("distill_runs", "novel_id", "INTEGER"),
        # 清洗前原文与上传原始正文：空 = 未留存（不是「清洗无损」）。
        ("novel_chunks", "raw_text", "TEXT"),
        ("raw_novels", "source_text", "TEXT"),
        ("raw_novels", "source_chapters", "JSON"),
        # 可读档位键（tier:signature:hash8）；老行补 NULL = 未生成过可读键。
        ("style_distills", "product_key", "VARCHAR(96)"),
    ]
    # v3.0 KG 索引定义（ensure_schema 同步建，_migrate_sqlite_columns 老库补建）
    v30_indexes = [
        # (table, idx_name, cols_sql, note)
        ("kg_entities", "ix_kg_entity_project_kind", "project_id, kind", "切片/批抽扫描 + 归一"),
        ("kg_entities", "ix_kg_entity_project_canonical", "project_id, canonical_name", "归一 by canonical_name"),
        ("kg_relations", "ix_kg_relation_project", "project_id", "切片扫关系"),
        ("kg_relation_evolution", "ix_kg_relation_evolution_edge", "edge_id", "关系曲线展开"),
        ("kg_events", "ix_kg_event_project_sequence", "project_id, sequence", "切片取最近 N 段 + 批抽 max(sequence)"),
        ("kg_events", "ix_kg_event_project_segment", "project_id, segment_no", "按段号反查事件"),
        ("kg_event_links", "ix_kg_event_link_from", "from_event_id", "因果链正向"),
        ("kg_event_links", "ix_kg_event_link_to", "to_event_id", "因果链反向"),
        ("kg_character_state", "ix_kg_char_state_project_char", "project_id, character_id", "按角色取最新状态"),
        ("kg_character_state", "ix_kg_char_state_project_segment", "project_id, segment_no", "按段号查状态"),
        ("kg_foreshadows", "ix_kg_foreshadow_project_status", "project_id, status", "active 过滤 + Judge 超期"),
        ("kg_entity_knowledge", "ix_kg_entity_knowledge_project_entity", "project_id, entity_id", "Judge 预知 + 切片 known_facts"),
        ("kg_mentions", "ix_kg_mention_project_segment", "project_id, segment_no", "切片核心：按段号取最近 N 段"),
        ("kg_mentions", "ix_kg_mention_project_entity", "project_id, entity_id", "mention_count 聚合"),
    ]
    # v3.0 KG 索引定义（ensure_schema 同步建，_migrate_sqlite_columns 老库补建）
    v30_indexes = [
        # (table, idx_name, cols_sql, note)
        ("kg_entities", "ix_kg_entity_project_kind", "project_id, kind", "切片/批抽扫描 + 归一"),
        ("kg_entities", "ix_kg_entity_project_canonical", "project_id, canonical_name", "归一 by canonical_name"),
        ("kg_relations", "ix_kg_relation_project", "project_id", "切片扫关系"),
        ("kg_relation_evolution", "ix_kg_relation_evolution_edge", "edge_id", "关系曲线展开"),
        ("kg_events", "ix_kg_event_project_sequence", "project_id, sequence", "切片取最近 N 段 + 批抽 max(sequence)"),
        ("kg_events", "ix_kg_event_project_segment", "project_id, segment_no", "按段号反查事件"),
        ("kg_event_links", "ix_kg_event_link_from", "from_event_id", "因果链正向"),
        ("kg_event_links", "ix_kg_event_link_to", "to_event_id", "因果链反向"),
        ("kg_character_state", "ix_kg_char_state_project_char", "project_id, character_id", "按角色取最新状态"),
        ("kg_character_state", "ix_kg_char_state_project_segment", "project_id, segment_no", "按段号查状态"),
        ("kg_foreshadows", "ix_kg_foreshadow_project_status", "project_id, status", "active 过滤 + Judge 超期"),
        ("kg_entity_knowledge", "ix_kg_entity_knowledge_project_entity", "project_id, entity_id", "Judge 预知 + 切片 known_facts"),
        ("kg_mentions", "ix_kg_mention_project_segment", "project_id, segment_no", "切片核心：按段号取最近 N 段"),
        ("kg_mentions", "ix_kg_mention_project_entity", "project_id, entity_id", "mention_count 聚合"),
    ]
    eng = target_engine if target_engine is not None else engine
    with eng.begin() as conn:
        for table, col, coltype in migrations:
            existing = {
                r[1]
                for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            }
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
        # 老库补建 v3.0 索引（IF NOT EXISTS 幂等，新库 create_all 已建则跳过）
        for table, idx_name, cols_sql, _note in v30_indexes:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols_sql})"))
        # 实体唯一键改「含书维度」的表达式索引：同名不同书各成一行，NULL 折成 -1
        # 才挡得住同名通配行重复（与 kg_item_group 的表达式索引同口径）。
        # 注意：老库若仍是表级 UNIQUE(project_id, kind, canonical_name)，这句只是
        # 多建一个索引、**去不掉旧约束**——真正换键要跑
        # migrations/2026-11-17-kg-entity-mention-novel-v1/apply_kg_book.py。
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_kg_entity ON kg_entities"
            " (project_id, kind, canonical_name, COALESCE(novel_id, -1))"))

        # 关系边/伏笔的按书索引：加列与建索引同一事务，避免「列有了、索引没建」
        # 的中间态（老库补列后立刻全表扫）。create_all 已给新库建好，这里幂等兜底。
        for stmt in (
            "CREATE INDEX IF NOT EXISTS ix_kg_relation_project_novel"
            " ON kg_relations (project_id, novel_id)",
            "CREATE INDEX IF NOT EXISTS ix_kg_foreshadow_project_novel"
            " ON kg_foreshadows (project_id, novel_id)",
        ):
            conn.execute(text(stmt))

        # —— 重写快照表（generation_snapshots）索引兜底 ——
        # 表本身由 create_all 建（新库/老库都一样），这里只补索引：部分唯一索引
        # uq_gs_pending 是「同段并发重写」的唯一闸门，缺了它两个请求会各写各的
        # 快照、正文互相覆盖。手工建过表的库（见迁移脚本 DDL）也不重复报错。
        exists = conn.execute(text(
            "SELECT 1 FROM sqlite_master"
            " WHERE type='table' AND name='generation_snapshots'")).fetchone()
        if exists:
            for stmt in (
                "CREATE INDEX IF NOT EXISTS ix_gs_project_segment"
                " ON generation_snapshots (project_id, segment_no, id)",
                "CREATE INDEX IF NOT EXISTS ix_gs_batch"
                " ON generation_snapshots (batch_id, segment_no)",
                "CREATE INDEX IF NOT EXISTS ix_gs_status_created"
                " ON generation_snapshots (status, created_at)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_gs_pending"
                " ON generation_snapshots (project_id, segment_no)"
                " WHERE status = 'pending'",
            ):
                conn.execute(text(stmt))

        # —— 轨迹表索引兜底 ——
        # 老库若在这张表定型前就建过它，create_all 不会补索引；段级聚合
        # （按 session+segment 回填 adopted_rule_ids）没有索引会全表扫。
        exists = conn.execute(text(
            "SELECT 1 FROM sqlite_master"
            " WHERE type='table' AND name='retrieval_logs'")).fetchone()
        if exists:
            for stmt in (
                "CREATE INDEX IF NOT EXISTS idx_retrieval_logs_session_segment"
                " ON retrieval_logs (session_id, segment_no)",
                "CREATE INDEX IF NOT EXISTS idx_retrieval_logs_agent_created"
                " ON retrieval_logs (agent_kind, created_at)",
            ):
                conn.execute(text(stmt))
