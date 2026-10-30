"""「混进了别的书」检测（`app.corpus_health.find_foreign_blocks`）。

## 为什么要有这个测试

盗版站的「单本下载」把好几本书拼成一个 txt。实测 `十日终焉.txt`（402 万字）里
插进了另一本书的 **16 章 / 21.8 万字**，而这 16 章是**夹在本书的章之间**的
（中间 61 章是本书的正文）。

后果是「投语料」这件事测的根本不是「这一本书的风格」：文体档案是混合体的统计，
风格样例可能取到外书的段落，检索会召回外书的情节。

## 这条测试钉住的判据

1. **阳性对照**：合成的「本书 + 外书」必须被判出来，且报告的独占词是外书的人名；
2. **不能整段删**：外书章**夹在**本书章中间时，剔除必须只删外书那几章，
   本书的正文一字不能少（这是实测踩过的坑：按首尾跨度删会误删 16 万字本书正文）；
3. **阴性对照**：一本书里文风偏跳脱的章节**不能**被判成混书
   （第一版「风格离群」判据在《大奉打更人》上 60 段全是误报）；
4. **只报不删是默认**：`find_foreign_blocks` 不改文本，剔除要走
   `strip_foreign_blocks`；
5. **确定性**：同一输入两次结果逐字节一致（不掺随机、不调模型）。

## 造数据的方式

人名与词库都是**虚构**的（不碰任何真实作品的人物名）：
本书 = 陆昭/沈砚/顾迟/温野 + 一套句库；外书 = 姜离/苏鹤/玄机/夜游 + 另一套句库。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.corpus_health import find_foreign_blocks, strip_foreign_blocks  # noqa: E402

HOST_CAST = ["陆昭", "沈砚", "顾迟", "温野"]
HOST_FILLER = [
    "把手里那本册子翻到最后一页",
    "抬头看了看屋檐下的雨线",
    "伸手把窗棂上那盏灯拨亮些",
    "听见院门被人推开的声音",
    "把袖口挽起来一截又放下",
    "在门槛边站了一会儿没进去",
    "把那封没写完的信折好收起",
    "听着更漏一格一格地走过去",
]
FOREIGN_CAST = ["姜离", "苏鹤", "玄机", "夜游"]
FOREIGN_FILLER = [
    "踏入满是剑意的血河深处",
    "以真名引动九天玄雷",
    "在帝君座前俯身听令",
    "把幽冥之气收进袖底",
    "踏碎虚空落到白骨堆上",
    "以万魂为引重开封印",
    "踏着祭文的余烬走向神台",
    "在灵台上与古神对视",
]

#: 每章的行数（要保证每章 >1200 字，低于这个长度的章不参与判定）。
LINES_PER_CHAPTER = 90


def _chapter(no: int, title: str, cast: list[str], filler: list[str]) -> str:
    """造一章：足够长（>1200 字）、人名均匀、句库固定。"""
    lines = [f"第{no}章 {title}"]
    for i in range(LINES_PER_CHAPTER):
        who = cast[i % len(cast)]
        tail = filler[(i // len(cast)) % len(filler)]
        lines.append(f"{who}{tail}，没有回头。")
    return "\n".join(lines)


def _host_book(n: int) -> list[str]:
    return [_chapter(i + 1, f"旧事{i + 1}", HOST_CAST, HOST_FILLER) for i in range(n)]


def _foreign_chapter(no: int) -> str:
    return _chapter(no, f"异闻{no}", FOREIGN_CAST, FOREIGN_FILLER)


def _join(chapters: list[str]) -> str:
    return "\n\n".join(chapters) + "\n"


def test_positive_control_detects_inserted_book():
    """阳性对照：本书 + 外书 ⇒ 必须判出外书那几章，独占词是外书的人名。"""
    text, foreign_at = _mixed_book()

    blocks = find_foreign_blocks(text)
    assert blocks, "混进来的书没被判出来（阳性对照失败）"

    hit_chapters: set[int] = set()
    for b in blocks:
        hit_chapters.update(b["chapters"])
    assert hit_chapters == foreign_at, (
        f"命中的章不对：期望 {sorted(foreign_at)}，实际 {sorted(hit_chapters)}"
    )

    terms = {t["term"] for b in blocks for t in b["terms"]}
    assert FOREIGN_CAST[0] in terms or FOREIGN_CAST[1] in terms, (
        f"独占词里没有外书的人名：{sorted(terms)[:12]}"
    )
    # 本书的人名不能出现在独占词里（它们遍布全书，不是「这一小片的人」）
    assert not (set(HOST_CAST) & terms), f"本书的人名被当成了独占词：{sorted(terms)[:12]}"


def test_strip_removes_only_the_foreign_chapters():
    """剔除必须只删外书那几章 —— 本书夹在中间的正文一字不能少。"""
    text, foreign_at = _mixed_book()

    blocks = find_foreign_blocks(text)
    cleaned, report = strip_foreign_blocks(text, blocks)

    assert report["removed"] > 0
    # 外书的人名与词库消失
    for name in FOREIGN_CAST:
        assert name not in cleaned, f"外书人名「{name}」还在"
    assert "剑意" not in cleaned and "血河" not in cleaned
    # 本书的人名与词库一字不少
    for name in HOST_CAST:
        assert cleaned.count(name) == text.count(name), f"本书人名「{name}」被误删"
    for word in ("册子", "雨线", "窗棂"):
        assert cleaned.count(word) == text.count(word), f"本书词「{word}」被误删"
    # 夹在中间的本书章还在（按章标题核对）
    for i in sorted(set(range(24)) - foreign_at):
        assert f"第{i + 1}章 旧事{i + 1}" in cleaned, f"本书第 {i + 1} 章被删了"


def test_negative_control_single_book_is_clean():
    """阴性对照：一本书里文风偏跳脱的章节，不能被判成「混进来的书」。"""
    chapters = _host_book(18)
    # 把第 6 章换成文风不同的内容（同一批人物 ⇒ 不是另一本书）
    odd = [" ".join([]) + f"第6章 异闻\n" + "\n".join(
        f"{HOST_CAST[i % len(HOST_CAST)]}忽然换了一种说法，句子变得很短。"
        for i in range(LINES_PER_CHAPTER)
    )]
    chapters[5] = odd[0]
    text = _join(chapters)
    assert find_foreign_blocks(text) == [], "单本书被判成了混书（阴性对照失败）"


def test_rare_single_chapter_is_not_flagged():
    """只有一章提到外书名字 ⇒ 不算「一本书」，不该判。"""
    chapters = _host_book(18)
    chapters[7] = _foreign_chapter(8)
    text = _join(chapters)
    assert find_foreign_blocks(text) == [], "单章外来内容被判成了混书"


def test_detection_is_read_only_and_deterministic():
    """检测不改文本；同一输入两次结果逐字节一致。"""
    text, _foreign_at = _mixed_book()

    before = text
    first = find_foreign_blocks(text)
    second = find_foreign_blocks(text)
    assert text == before, "检测过程改动了原文"
    assert first == second, "两次检测结果不一致（不确定）"


def test_too_small_or_headingless_text_is_not_judged():
    """书太小 / 没有章节标记 ⇒ 判不了，不硬猜。"""
    assert find_foreign_blocks("") == []
    assert find_foreign_blocks("短文本") == []
    no_headings = "\n".join("没有任何章节标记的一段文字。" * 60 for _ in range(20))
    assert find_foreign_blocks(no_headings) == []


# ====================================================================
# 入库链路：体检报告（只报） + 一键剔除（显式开关）
# ====================================================================


@pytest.fixture()
def db_session():
    from app.db import Base, SessionLocal, engine
    import app.models  # noqa: F401  —— 导入模型，Table 才会进 Base.metadata

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _mixed_book() -> tuple[str, set[int]]:
    """本书 24 章 + 外书 5 章（夹在中间），返回 (整本书原文, 外书章的下标)。

    外书占 5/24 ≈ 21% 章 —— 在判据的「混入占比 ≤ 25%」上限之内
    （超了就不是「一本书里混了几章」，而是「这本书本身就是拼的」）。
    外书章**分散在两处**（第 5~7 章与第 13~14 章），
    中间夹着本书的章 —— 测的就是「不能按首尾跨度整段删」。
    """
    chapters = _host_book(24)
    foreign_at = {4, 5, 6, 12, 13}
    merged = [(_foreign_chapter(i + 1) if i in foreign_at else ch)
              for i, ch in enumerate(chapters)]
    return _join(merged), foreign_at


def _mixed_book_text() -> str:
    return _mixed_book()[0]


def test_ingest_reports_foreign_blocks_but_keeps_them(db_session):
    """默认：体检报告**标出**混书，但一个字都不删。"""
    from app.ingestion.store import ingest_text

    res = ingest_text(db_session, _mixed_book_text(), title="混了书的语料", platform="upload")
    report = res["clean_report"]
    foreign = report.get("foreign_blocks") or {}
    assert foreign.get("detected"), "体检报告里没有混书结论"
    assert foreign.get("block_count", 0) >= 1
    assert foreign.get("chars", 0) > 0
    # 证据要能给人复核：独占词 + 样本
    first = (foreign.get("blocks") or [])[0]
    assert first.get("terms"), "混书结论没有给独占词"
    assert first.get("sample"), "混书结论没有给样本"

    # 正文里外书的人名还在（没删）
    from app.models import NovelChunk
    from sqlalchemy import select
    chunks = db_session.scalars(
        select(NovelChunk).where(NovelChunk.novel_id == res["novel_id"])
    ).all()
    joined = "\n".join(c.text or "" for c in chunks)
    assert FOREIGN_CAST[0] in joined, "默认不该删正文"


def test_ingest_drops_foreign_blocks_when_explicitly_asked(db_session):
    """显式开关：剔除混进来的书，本书正文一字不少。"""
    from app.ingestion.store import ingest_text
    from app.models import NovelChunk
    from sqlalchemy import select

    text = _mixed_book_text()
    res = ingest_text(db_session, text, title="要剔除的语料", platform="upload",
                      drop_foreign_blocks=True)
    foreign = (res["clean_report"] or {}).get("foreign_blocks") or {}
    assert foreign.get("detected") and foreign.get("dropped"), "没记下剔除动作"
    assert foreign.get("removed_chars", 0) > 0

    chunks = db_session.scalars(
        select(NovelChunk).where(NovelChunk.novel_id == res["novel_id"])
    ).all()
    joined = "\n".join(c.text or "" for c in chunks)
    for name in FOREIGN_CAST:
        assert name not in joined, f"外书人名「{name}」还在"
    for name in HOST_CAST:
        assert name in joined, f"本书人名「{name}」被误删"


def test_corpus_audit_surfaces_foreign_blocks(db_session):
    """审核规则：语料体检里的混书结论要能出现在审核面板的 issues 里。"""
    from app.agents.audit import run_corpus_audit
    from app.ingestion.store import ingest_text

    res = ingest_text(db_session, _mixed_book_text(), title="要审核的语料", platform="upload")
    report, _stats = run_corpus_audit(db_session, novel_ids=[res["novel_id"]])
    rules = [i.rule for i in report.issues]
    assert "foreign_book_blocks" in rules, f"审核没报混书：{rules}"
    issue = next(i for i in report.issues if i.rule == "foreign_book_blocks")
    assert issue.severity == "warn"
    assert issue.evidence, "混书结论没有证据"


def test_pending_ingest_card_carries_foreign_report(db_session):
    """投放卡：结论要**跟着卡一起下发**，且剔除默认关闭。

    为什么这是两件事：
    - 结论不在卡上 ⇒ 用户点确认之前看不到「里面混了什么」，等于盲投；
    - 剔除默认打开 ⇒ 一条统计判据就能替用户删掉整章正文。
    所以卡上必须**带结论**（detail.foreign_blocks）且
    **带一个默认 false 的剔除位**（body.drop_foreign_blocks）。
    """
    from app.agents import attachments
    from app.models import Project

    project_id = 1
    project = db_session.get(Project, project_id)
    if project is None:
        project = Project(id=project_id, title="测试项目", target_length=3000,
                          generation_length=1200, genre="都市异能")
        db_session.add(project)
        db_session.commit()
    meta = attachments.stage(
        db=db_session, project_id=project_id, filename="混了书的语料.txt",
        data=_mixed_book_text().encode("utf-8"),
    )
    card = attachments.propose_ingest(
        db=db_session, project_id=project_id, attachment_id=meta["attachment_id"],
        instruction="请把这份语料投放入库",
    )
    assert card is not None, "投放指令没有产出待确认卡"
    foreign = (card["detail"] or {}).get("foreign_blocks") or {}
    assert foreign.get("detected"), "投放卡没带混书结论"
    assert foreign.get("block_count", 0) >= 1
    assert card["body"].get("drop_foreign_blocks") is False, "剔除默认必须是关的（只报不删）"
    assert card["endpoint"] == "/api/assistant/attachments/ingest"
