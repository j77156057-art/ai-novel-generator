"""重生成路径不再白付「两遍全量语料加载」——constraints 单独组装（2026-10-17）。

## 被钉住的缺陷

`engine.regenerate_segment_stream` 原有一行：

    constraints = build_context(db, project, novel_ids=novel_ids)["constraints"]

只为取 `constraints` 一个键，`build_context` 的其余产物（RAG 命中文本、风格样例、
前情块、`rag_has_forbidden` 判定）**全部丢弃**。代价是每本参考语料被：
  - `hybrid_search` 加载一次全量 `NovelChunk`（`retrieval/search.py` 里
    `select(NovelChunk).where(novel_id == ...)`），逐 chunk 算 BM25 + 余弦；
  - `sample_style_exemplars` 再加载一次全量 `NovelChunk`
    （`style/exemplar.py` 里同样的 `select(...).order_by(idx)`）。

而**同一个调用链**的 `_build_generation_prompts` 在 user prompt 里明确传
`[]  # 重生成不带 RAG 素材` —— 作者有意不让重生成带检索素材。于是实现为
「不会用到的 RAG」付了两次全量加载：**与作者意图相悖的纯浪费，不是功能缺陷**。

## 本文件守护的六条

  T1 红→绿：重生成期间 `hybrid_search` / `sample_style_exemplars` 调用次数均为 0。
  T2 红→绿（更强，钉事实而非入口）：**在档案块被隔离的夹具下**，SQL 层断言这条
     路径不产生任何含 `novel_chunks` 的语句。理由：T1 只钉两个已知入口，将来若有
     人从别处加载分片，T1 会漏；T2 把这个事实变成可执行断言。
     注意范围：真实项目里文风档案拟合**必须**读语料（`SELECT novel_chunks.text`），
     那是另一条有意为之的成本，不在 T2 断言范围——其边界由 T2b 单独钉住。
  T2b 正向对照：不做任何 monkeypatch 的真实场景下，**允许**文风档案的单列整文加载
     （且必须真的发生），**禁止**整行加载（`hybrid_search` / `sample_style_exemplars`
     的形状）。防「把 `build_context` 塞回来还狡辩只剩档案加载」。
  T3 不变式（恒真型）+ 变异取证：重生成与续写路径下发的 `constraints` 逐项相等，
     且两条来源（plot_bible / KG reminders）都在场、顺序为 bible 在前。
  T3b 结构断言 + 变异取证：`reminders` 在重生成里**只加载一次**，且喂给 system 的
     对象与喂给 `constraints` 的对象是**同一个**（本次提交最核心的性质：
     防两次查询之间的并发写入造成自相矛盾提示词）。
  T4 不变式（恒真型）+ 变异取证：重生成的 `(system, user)` sha256 逐字节不变。

T3/T3b/T4 在修复前后都成立（它们断言的是「口径没变」），因此**不计入红→绿信用**，
只以变异取证证明其有效性（临时注入错误 → 必须变红 → 还原 → 必须绿）。

## 隔离说明

`_style_profile` / `_style_narrative` 在下面统一置 `None`：它们自身确实会读语料
（文风档案要实测分片），但那是**另一条已存在且有意为之**的语料使用（进 system 的
静态档案块），不属于本单范围。置 None 才能把 T2 的 SQL 断言聚焦在 `build_context`
的浪费上。`_require_distill_ready` 也一并短路——本文件不测蒸馏预检。
唯一例外是 T2b：它故意**不**隔离档案块，专门用来钉住「允许什么、禁止什么」。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from sqlalchemy import event  # noqa: E402

import app.generation.engine as engine_mod  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import (  # noqa: E402
    generate_next_segment_stream,
    regenerate_segment_stream,
)
from app.kg.build import merge_segment_into_kg  # noqa: E402
from app.models import (  # noqa: E402
    Generation,
    KgNode,
    NovelChunk,
    Project,
    RawNovel,
)

#: 固定夹具正文（重生成的目标段与前一段）。
SEG1 = "林夜推开沉重的木门。\n林夜说道：“我回来了。”"
SEG2 = "白光抬起头，望向窗外。\n白光说道：“天快亮了。”"

#: 固定夹具 `plot_bible`：里面既有角色也有伏笔，才可能让 `_constraints_from`
#: 的变异（把 plot_bible 换成 None）真的在 constraints 上留下差异。
PLOT_BIBLE = {
    "characters": [
        {"name": "林夜", "role": "主角"},
        {"name": "白光", "role": "同伴"},
    ],
    "foreshadowing": ["黑铁钥匙的来历", "白光的真实身份"],
}

#: 重生成路径 `(system, user)` 的 sha256 金标准。
#:
#: 2026-10-17 实测于父提交 `4592119`（同一夹具、同一 RecordingProvider），
#: 值为 `b6e8ad8cb67db18ce1b388268b634fb51b5484de4eb2a8ec513800b6209efed3`；
#: 在修复后的新 HEAD 上复测必须**同值**（父子同值 = 本次改动逐字节没动提示词）。
#: 这两行若变了，说明重生成的提示词口径被动过，必须**重新论证**而不是随手改常数。
#: 口径：`sha256(system + "\x00" + user)`，UTF-8。
PROMPT_SHA256 = "b6e8ad8cb67db18ce1b388268b634fb51b5484de4eb2a8ec513800b6209efed3"


class RecordingProvider:
    """确定性假 provider：返回固定正文，并记录交给它的 system / user / constraints。

    `summarize_rollup` 也给出固定值——`_build_generation_prompts` 在
    `before_segment` 非空时会用它补齐被截断的粗层（`rollup_volumes`），
    固定返回值让 T4 的哈希不受 provider 内部实现影响。
    """

    def __init__(self, text: str = "他站在原地没动，手指慢慢收拢。") -> None:
        self.text = text
        self.systems: list[str] = []
        self.users: list[str] = []
        self.constraints: list[dict | None] = []

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        self.systems.append(system)
        self.users.append(user)
        self.constraints.append(constraints)
        return self.text

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def summarize_rollup(self, texts: list[str], level: str) -> str:
        return f"{level}滚上摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None):
        self.systems.append(system)
        self.users.append(user)
        self.constraints.append(constraints)
        yield self.text


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _isolate_archives(monkeypatch) -> None:
    """隔离静态档案块与蒸馏预检（见模块 docstring「隔离说明」）。"""
    monkeypatch.setattr(engine_mod, "_require_distill_ready", lambda db, p: None)
    monkeypatch.setattr(engine_mod, "_style_profile", lambda db, p: None)
    monkeypatch.setattr(engine_mod, "_style_narrative", lambda db, p: None)


def _make_corpus(db, name: str, n_chapters: int = 12) -> int:
    """造一本参考语料（`n_chapters` 个分片），返回 novel id。"""
    novel = RawNovel(title=f"参考语料{name}", status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for i in range(n_chapters):
        db.add(
            NovelChunk(
                novel_id=novel.id,
                chapter_no=i + 1,
                idx=i,
                text="医院里她抬起头，看着走廊尽头的光。" + "他说：走吧。" * (i % 3 + 1),
            )
        )
    db.commit()
    return novel.id


def _make_project(db, title: str, novel_ids: list[int] | None = None) -> Project:
    proj = Project(
        title=title,
        target_length=5000,
        generation_length=3000,
        mode="quick",
        status="created",
        reference_novel_ids=list(novel_ids or []),
        plot_bible=dict(PLOT_BIBLE),
        outline=["林夜回到旧宅", "白光在窗边等他"],
        premise="一个雨夜的重逢",
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _seed_segment(db, project: Project, segment_no: int, text: str) -> None:
    db.add(
        Generation(
            project_id=project.id,
            segment_no=segment_no,
            text=text,
            summary={"text": f"段{segment_no}摘要"},
        )
    )
    db.commit()
    merge_segment_into_kg(db, project.id, text, segment_no, source="generated")


#: 只在 KG（`source="generated"`）里有、`plot_bible` 里**没有**的角色/伏笔。
#:
#: 为什么必须有：`constraints` 的两条来源是「plot_bible（最高权威层）」与「图谱抽取」。
#: 若夹具里 KG 抽出来的角色名与 bible 重名、且一个伏笔都没抽到，那么
#: `_constraints_from(reminders, bible)` 与 `_constraints_from({}, bible)` 结果**完全
#: 相同**——「同一个 reminders 对象同时喂 system 与 constraints」这条本次提交最核心的
#: 性质就会没有任何测试守着。故这两个值必须与 `PLOT_BIBLE` 无交集，直接插 `KgNode` 造。
KG_ONLY_CHARACTER = "守夜人"
KG_ONLY_PLOTHOLE = "铜牌背面的符号是谁刻的"


def _seed_kg_reminders(db, project: Project) -> None:
    """给项目插一条 KG 独有角色 + 一条 `status="open"` 的 KG 独有伏笔。

    直接插 `KgNode` 而不是靠正文抽取：抽取结果依赖正则与清洗规则，换一版实现就可能
    抽不到，夹具会静默退化成「KG 零贡献」——那正是本文件曾经漏测的形态。
    """
    db.add(
        KgNode(
            project_id=project.id,
            type="character",
            props={"name": KG_ONLY_CHARACTER, "mentions": 3},
            first_chapter=1,
            last_chapter=2,
            source="generated",
        )
    )
    db.add(
        KgNode(
            project_id=project.id,
            type="plothole",
            props={"question": KG_ONLY_PLOTHOLE, "status": "open"},
            first_chapter=2,
            source="generated",
        )
    )
    db.commit()


def _fixture(db, title: str = "重生成浪费测试", corpus_count: int = 2):
    """固定夹具：`corpus_count` 本参考语料 + 已落库的段 1 / 段 2。"""
    ids = [_make_corpus(db, f"{title}-{i}") for i in range(corpus_count)]
    proj = _make_project(db, title, ids)
    _seed_segment(db, proj, 1, SEG1)
    _seed_segment(db, proj, 2, SEG2)
    return proj, ids


# ---------------------------------------------------------------------------
# T1（红→绿）：检索与采样都不再发生
# ---------------------------------------------------------------------------


def test_regenerate_no_longer_searches_or_samples_corpora(db_session, monkeypatch):
    """重生成期间 `hybrid_search` / `sample_style_exemplars` 的调用次数必须为 0。

    修复前实测（父提交 `4592119`，2 本语料）：`hybrid_search` **2** 次（每本 1 次），
    `sample_style_exemplars` **1** 次——`build_context` 只调它一次，函数内部**自己**
    遍历全部 `novel_ids`（`exemplar.py:122-129` 用 `db.get(RawNovel, nid)` 逐本取），
    所以「按语料本数计次」是错的，它的计次恒为 1，代价在内部那遍全量 chunk 加载。
    修复后两者都是 0 → 绿。
    """
    _isolate_archives(monkeypatch)
    calls = {"hybrid": 0, "exemplar": 0}

    def _fake_hybrid(*args, **kwargs):
        calls["hybrid"] += 1
        return []

    def _fake_exemplars(*args, **kwargs):
        calls["exemplar"] += 1
        return []

    monkeypatch.setattr(engine_mod, "hybrid_search", _fake_hybrid)
    monkeypatch.setattr(engine_mod, "sample_style_exemplars", _fake_exemplars)

    proj, novel_ids = _fixture(db_session)
    assert len(novel_ids) == 2, "夹具必须带 2 本参考语料，否则本次红→绿不可观测"

    prov = RecordingProvider()
    events = list(
        regenerate_segment_stream(db_session, proj, segment_no=2, provider=prov)
    )
    assert any(e.get("type") == "done" for e in events), "重生成未产出 done 事件"
    assert prov.constraints, "重生成未把 constraints 交给 provider"

    assert calls["hybrid"] == 0, (
        f"重生成仍在跑 RAG 检索（hybrid_search 被调用 {calls['hybrid']} 次）——"
        "其结果被 build_context 的调用方全部丢弃，纯浪费全量 NovelChunk 加载"
    )
    assert calls["exemplar"] == 0, (
        f"重生成仍在采样风格样例（sample_style_exemplars 被调用 {calls['exemplar']} 次）——"
        "同样会再加载一遍全部 NovelChunk，结果同样被丢弃"
    )


# ---------------------------------------------------------------------------
# T2（红→绿，且比 T1 更强）：SQL 层证明不再碰 novel_chunks
# ---------------------------------------------------------------------------


def test_regenerate_executes_no_sql_against_novel_chunks(db_session, monkeypatch):
    """在本夹具（`_require_distill_ready` 短路、`_style_profile`/`_style_narrative`
    隔离为 None）下，重生成路径**不产生任何**含 `novel_chunks` 的 SQL —— 即
    `build_context` 带来的 RAG 检索与风格样例两条全量加载均已消失。文风档案拟合
    （`SELECT novel_chunks.text … ORDER BY idx`）属另一条有意为之的语料使用，
    不在本断言范围（其边界由下一条正向对照用例
    `test_only_style_fit_touches_chunks_not_whole_rows` 钉住）。

    比 T1 强在哪：T1 只钉住两个已知入口函数，将来若有人从第三个地方加载语料分片，
    T1 会静默放过；T2 直接钉住「这条路径不读语料分片」这个**事实**（在本夹具范围内）。

    修复前实测（父提交 `4592119`）：4 条 `FROM novel_chunks` → 红。
    修复后：一条都没有 → 绿。
    """
    _isolate_archives(monkeypatch)
    proj, novel_ids = _fixture(db_session)
    assert novel_ids, "夹具必须带参考语料，否则本断言恒真、无意义"

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        events = list(
            regenerate_segment_stream(
                db_session, proj, segment_no=2, provider=RecordingProvider()
            )
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert any(e.get("type") == "done" for e in events), "重生成未产出 done 事件"
    assert statements, "没有捕获到任何 SQL——监听器没挂上，本断言无意义"

    offenders = [s for s in statements if "novel_chunks" in s]
    assert offenders == [], (
        "重生成路径读了语料分片（SQL 含 novel_chunks）："
        f"{len(offenders)} 条，示例={offenders[:1]}。"
        "重生成的 user prompt 是无 RAG 的（`[]  # 重生成不带 RAG 素材`），"
        "读这些分片纯属浪费"
    )


# ---------------------------------------------------------------------------
# T2b（正向对照）：只允许文风档案的「整文加载」，禁止整行加载
# ---------------------------------------------------------------------------


def test_only_style_fit_touches_chunks_not_whole_rows(db_session):
    """**不做任何 monkeypatch** 的真实场景下，重生成只允许文风档案拟合读语料。

    为什么需要这条：T2 的「零 `novel_chunks`」是在档案块被隔离的夹具下成立的。
    真实项目里文风档案（进 system 的静态块）**必须**读语料——那是另一条有意为之、
    且必要的成本。若只用 T2，读者会误以为「这条路径从不碰语料」；更糟的是，
    将来有人把 `build_context` 塞回来时，也可以狡辩「剩下那些只是档案加载」。
    本条把这条边界钉成可执行断言：**允许** `SELECT novel_chunks.text …`（档案拟合的
    整文加载），**禁止**整行加载（`novel_chunks.id,` 与 `novel_chunks.text` 同时出现，
    那是 `hybrid_search` / `sample_style_exemplars` 的特征形状）。

    夹具：2 本语料 + `style_narrative` 指纹**自然命中**（不 patch
    `_require_distill_ready`，让它走真实判据）+ `style_profile` 冷（必然触发拟合）。
    """
    proj, novel_ids = _fixture(db_session, title="档案边界")
    assert len(novel_ids) == 2, "本用例要 2 本语料，才能让档案加载与整行加载都可辨"

    from app.style.narrative import narrative_fingerprint

    # 叙事档案：写入与 `_require_distill_ready` / `project_narrative` 完全一致的指纹，
    # 于是「预检放行」与「档案缓存命中」都走**真实**代码路径，不靠 patch。
    proj.style_narrative = {
        "fingerprint": narrative_fingerprint(novel_ids),
        "rules": {"人物描写手法": [{"rule": "出场先写环境再写人（证据：她抬起头）"}]},
        "forbidden": {"禁用转折词": ["就在这时"]},
    }
    db_session.commit()

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        events = list(
            regenerate_segment_stream(
                db_session, proj, segment_no=2, provider=RecordingProvider()
            )
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert any(e.get("type") == "done" for e in events), "重生成未产出 done 事件"

    chunk_stmts = [s for s in statements if "novel_chunks" in s]
    assert chunk_stmts, (
        "没有任何 novel_chunks SQL——文风档案没被触发加载，本用例失去对照意义"
        "（冷档案必须真的去读语料）"
    )

    whole_row = [
        s for s in chunk_stmts
        if "novel_chunks.id," in s and "novel_chunks.text" in s
    ]
    assert whole_row == [], (
        "重生成出现了**整行**语料加载（`SELECT novel_chunks.id, … novel_chunks.text …`）——"
        "这是 `hybrid_search` / `sample_style_exemplars` 的形状："
        f"{len(whole_row)} 条，示例={whole_row[:1]}。"
        "文风档案只需要 `novel_chunks.text`（`style/pipeline._load_novel_text`），"
        "出现整行说明 `build_context` 又被塞回重生成路径了"
    )

    text_only = [
        s for s in chunk_stmts if s.strip().startswith("SELECT novel_chunks.text")
    ]
    assert len(text_only) == len(novel_ids), (
        "文风档案的整文加载次数与语料本数不符："
        f"实测 {len(text_only)} 条 `SELECT novel_chunks.text`，期望 {len(novel_ids)} 条"
        "（`style/pipeline._load_novel_text` 每本一次，2026-10-17 于本夹具实测）。"
        "这条一变，说明「冷档案一次性成本」的形状变了——必须重新论证，"
        "不能随手改常数"
    )


# ---------------------------------------------------------------------------
# T3（不变式 + 变异取证）：constraints 口径与续写路径同源
# ---------------------------------------------------------------------------


def test_regenerate_constraints_match_continue_path(db_session, monkeypatch):
    """同一固定夹具状态下，重生成与续写交给 provider 的 `constraints` 必须逐项相等，
    且**两条来源都在场**：plot_bible（最高权威层）与 KG 抽取。

    **恒真型断言（修复前后都成立）**：它守的是「口径不变」这一不变量。
    有效性靠变异取证（本文件交付时实测）：
      - 重生成侧整体丢 reminders（`_constraints_from({}, project)`，路径局部）→ 红；
      - `_constraints_from` 丢掉角色来源（KG `character_constraints`）→ 红。

    夹具必须同时有「bible 独有」与「KG 独有」的角色与伏笔，否则
    `_constraints_from(reminders, bible)` 与 `_constraints_from({}, bible)` 会**同值**，
    本次提交最核心的性质（同一 reminders 对象喂 system 与 constraints）就没有测试守着。
    这正是本用例当初漏测的形态，故用 `_seed_kg_reminders` 直接插 `KgNode` 造数据。

    两个项目各建一份**内容相同**的夹具（语料、段落、plot_bible、KG 提醒全同），
    避免「先跑哪条路径」通过 KG 写入互相污染 reminders。
    """
    _isolate_archives(monkeypatch)

    proj_regen, _ = _fixture(db_session, title="口径-重生成")
    _seed_kg_reminders(db_session, proj_regen)
    proj_cont, _ = _fixture(db_session, title="口径-续写")
    _seed_kg_reminders(db_session, proj_cont)

    regen_prov = RecordingProvider()
    list(
        regenerate_segment_stream(
            db_session, proj_regen, segment_no=2, provider=regen_prov
        )
    )
    cont_prov = RecordingProvider()
    list(generate_next_segment_stream(db_session, proj_cont, provider=cont_prov))

    assert regen_prov.constraints, "重生成未把 constraints 交给 provider"
    assert cont_prov.constraints, "续写未把 constraints 交给 provider"

    regen = regen_prov.constraints[0]
    cont = cont_prov.constraints[0]

    # —— 两条来源都在场（缺任一来源的夹具会让下面的等值断言失去意义）——
    assert "林夜" in regen["characters"], f"缺 plot_bible 侧角色：{regen['characters']}"
    assert KG_ONLY_CHARACTER in regen["characters"], (
        f"缺 KG 独有角色「{KG_ONLY_CHARACTER}」——reminders 对 constraints 零贡献，"
        f"「同一 reminders 对象喂两处」这条性质没有被守住：{regen['characters']}"
    )
    assert "黑铁钥匙的来历" in regen["plotholes"], f"缺 plot_bible 侧伏笔：{regen['plotholes']}"
    assert KG_ONLY_PLOTHOLE in regen["plotholes"], (
        f"缺 KG 独有伏笔「{KG_ONLY_PLOTHOLE}」——同上：{regen['plotholes']}"
    )

    # —— 顺序：bible 在前、KG 补后（`prompts.merged_constraint_items` 的权威顺序）——
    chars_at = {name: i for i, name in enumerate(regen["characters"])}
    assert chars_at["林夜"] < chars_at[KG_ONLY_CHARACTER], (
        f"角色顺序错：bible 角色必须排在 KG 角色之前：{regen['characters']}"
    )
    assert chars_at["白光"] < chars_at[KG_ONLY_CHARACTER], (
        f"角色顺序错：bible 角色必须排在 KG 角色之前：{regen['characters']}"
    )
    holes_at = {text: i for i, text in enumerate(regen["plotholes"])}
    assert holes_at["黑铁钥匙的来历"] < holes_at[KG_ONLY_PLOTHOLE], (
        f"伏笔顺序错：bible 伏笔必须排在 KG 伏笔之前：{regen['plotholes']}"
    )

    regen_json = json.dumps(regen, sort_keys=True, ensure_ascii=False)
    cont_json = json.dumps(cont, sort_keys=True, ensure_ascii=False)
    assert regen_json == cont_json, (
        "重生成的 constraints 与续写路径不同源——"
        "会出现「system 提示列了一套锁定名单、尾部 constraints 列了另一套」"
        f"的自相矛盾提示词\n重生成: {regen_json}\n续写  : {cont_json}"
    )


# ---------------------------------------------------------------------------
# T3b（结构断言 + 变异取证）：reminders 只加载一次，且 system 与 constraints 同一对象
# ---------------------------------------------------------------------------


def test_reminders_loaded_once_and_shared_with_system_prompt(db_session, monkeypatch):
    """重生成里 `reminders` 必须**只加载一次**，且喂给 system 的那个对象
    与算 `constraints` 用的是**同一个**。

    为什么这条必须单独存在：本次提交除了省一次查询，更核心的理由是**防并发写入**
    ——原实现是两次独立查询，两次之间若另一请求落库了新角色/已填伏笔，就会出现
    「system 列了一套锁定名单、尾部 constraints 列了另一套」的自相矛盾提示词。
    这条性质无法由 T3（只比 constraints）或 T4（只比提示词字节）单独守住：
       - 若 `regenerate_segment_stream` 不把 reminders 传下去，constraints 完全不受
         影响 → T3 仍绿；
       - 提示词字节也可能不变（同一事务内两次查询结果相同）→ T4 仍绿。
    故直接断言**调用次数 == 1** 与**对象同一性**（`is`）。

    计数口径（为什么必须是**本线程 + 本项目**）：`engine.get_reminders` 是模块全局名，
    monkeypatch 之后**全进程**任何线程、任何 project_id 调到它都会 +1。而真实运行时
    确有别的执行上下文会用这个全局名——同步路由跑在 AnyIO 线程池里，
    `POST /api/generate-plan` → `routers/generation.py:206` → `build_context` →
    `engine.py:383` 就是一条（2026-10-17 全量插桩实测：一次全量跑里 41 次调用中
    有 2 次来自 `AnyIO worker thread`）。若按全进程计数，别的测试残留的 worker 线程
    只要在窗口内查一次，本条就会假红（已用确定性脚本复现：全局计数 2 → FAIL，
    按线程+项目过滤 1 → PASS）。所以过滤条件写进断言，**不是**放宽容错。

    变异取证（交付时实测，均在本仓库之外的独立 worktree 内注入）：
      - 删掉 `reminders=reminders` 这个关键字实参（让 `_build_generation_prompts`
        自行加载）→ 本线程本项目内 `get_reminders` 变成 **2 次** → 次数断言红；
      - 把实参换成 `dict(reminders)`（**等值但不同对象**）→ 次数仍为 1，仅
        **同一性断言红**——证明 `is` 那一条不是冗余装饰，它守的正是「同一个对象」。
    """
    _isolate_archives(monkeypatch)
    proj, _ = _fixture(db_session, title="单次加载")

    real_get_reminders = engine_mod.get_reminders
    #: (线程 ident, project_id, 真实返回值)。**必须原样返回 out**，否则下面的
    #: 对象同一性断言就没有意义（`is` 比的是真正下发出去的那个对象）。
    reminder_calls: list[tuple[int, object, dict]] = []

    def _counting_get_reminders(*args, **kwargs):
        # `get_reminders(db, project_id, source=...)`：第二位恒为 project_id，
        # 但调用方也可能用关键字传，两种都要取到。
        pid = args[1] if len(args) > 1 else kwargs.get("project_id")
        out = real_get_reminders(*args, **kwargs)
        reminder_calls.append((threading.get_ident(), pid, out))
        return out

    monkeypatch.setattr(engine_mod, "get_reminders", _counting_get_reminders)

    handed_to_prompts: list[object] = []
    real_build_prompts = engine_mod._build_generation_prompts

    def _spy_build_prompts(*args, **kwargs):
        handed_to_prompts.append(kwargs.get("reminders"))
        return real_build_prompts(*args, **kwargs)

    monkeypatch.setattr(engine_mod, "_build_generation_prompts", _spy_build_prompts)

    list(
        regenerate_segment_stream(
            db_session, proj, segment_no=2, provider=RecordingProvider()
        )
    )

    mine = [
        c for c in reminder_calls
        if c[0] == threading.get_ident() and c[1] == proj.id
    ]
    assert len(mine) == 1, (
        f"重生成期间 `get_reminders` 被调用 {len(mine)} 次（应为 1 次；"
        f"**仅统计本线程 ident={threading.get_ident()}、本项目 id={proj.id}** 的调用）。"
        "多于 1 次说明 system 与 constraints 各自独立查了一次——两次查询之间若发生"
        "并发写入，就会出现「system 列了一套锁定名单、尾部 constraints 列了另一套」"
        "的自相矛盾提示词"
    )
    assert handed_to_prompts, "未观察到 `_build_generation_prompts` 调用，本断言无意义"
    assert handed_to_prompts[0] is mine[0][2], (
        "传给 `_build_generation_prompts` 的 reminders 与本次加载的**不是同一个对象**"
        f"（{type(handed_to_prompts[0]).__name__} vs "
        f"{type(mine[0][2]).__name__}）——system 与 constraints 必须同源"
    )


# ---------------------------------------------------------------------------
# T4（不变式 + 变异取证）：提示词逐字节不变
# ---------------------------------------------------------------------------


def test_regenerate_prompt_bytes_unchanged(db_session, monkeypatch):
    """重生成的 `(system, user)` sha256 必须等于金标准常量（父子同值）。

    **恒真型断言（修复前后都成立）**：本次改动只搬移 constraints 的组装位置，
    不该动提示词一个字节。有效性同样靠变异取证：把 `_build_generation_prompts`
    里的 `[],` 换成 `["x"]`（或让 reminders 少一个字段）→ 本条必红。
    """
    _isolate_archives(monkeypatch)
    proj, _ = _fixture(db_session)

    prov = RecordingProvider()
    list(regenerate_segment_stream(db_session, proj, segment_no=2, provider=prov))
    assert prov.systems and prov.users, "重生成未把提示词交给 provider"

    payload = prov.systems[0] + "\x00" + prov.users[0]
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert digest == PROMPT_SHA256, (
        "重生成的 (system, user) 与金标准不一致——"
        "提示词口径被动过了，必须重新论证而不是随手改常数\n"
        f"实际: {digest}\n期望: {PROMPT_SHA256}"
    )
