"""语料清洗接入导入链测试（第 8 步交付 2）。

覆盖：
  (a) `clean_novel_text` 能剔除水印、报告含检出串/次数/删字数/**≥2 条原文样本**；
  (b) 清洗**确定性**：同一输入两次结果逐字节相同；
  (c) 无污染文本原样返回、报告标记 cleaned=False；
  (d) `ingest_text` 把报告持久化到 `RawNovel.clean_report`，且**入库分片已无残留水印**。
"""

from __future__ import annotations

import json
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from app.ingestion.clean import build_clean_report, clean_novel_text  # noqa: E402

# 水印被焊在每句句末（紧跟「。」），重复 90 次 → 满足位置分布判据。
# 前缀**刻意变化**（说道/想着/笑道…）：判据在「前缀恒定」时会把前缀也算进注入内容
# （见 corpus_health._dedupe_nested 的设计），那会误删正文的「说道」。变化前缀能让
# 去嵌套正确收敛到核心串「测试水印」。
_WM = "测试水印"
# 前缀末字**两两不同**：否则「X测试水印」会因恒定前缀被去嵌套并入注入串，误删正文。
# 取 12 种（> DEFAULT_MIN_NESTING_BREADTH=8）：真水印表现为「被粘到多种前导字上」，
# 前导字种类数须不低于 breadth 下限才会被检出。
_TAILS = ("说道", "点头", "微笑", "叹气", "皱眉", "转身",
          "沉吟", "开口", "出手", "抬眼", "喃喃", "低语")
_DIRTY = "".join(f"第{i}章 正文内容若干{_TAILS[i % 12]}{_WM}。" for i in range(90))
# 干净文本：重复次数 < min_count(60)，天然不可能被检出。
_CLEAN = "".join(f"第{i}章 正文内容若干{_TAILS[i % 12]}结束。" for i in range(30))
# 供入库测试用：**不带**「第X章」行首标记，否则 parse_txt 会把整行当章标题、body 为空。
_DIRTY_BODY = "".join(f"正文内容若干{_TAILS[i % 12]}{_WM}。" for i in range(90))
_CLEAN_BODY = "".join(f"正文内容若干{_TAILS[i % 12]}结束。" for i in range(30))


@pytest.fixture(autouse=True)
def _fresh_db():
    """每个用例重建表（engine 已被 conftest 隔离到临时库）。"""
    import app.models  # noqa: F401  确保模型注册到 Base.metadata，create_all 才建表
    from app.db import Base, engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db():
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ====================================================================
# (a) 检出与剔除
# ====================================================================


def test_clean_detects_and_strips_watermark():
    cleaned, report = clean_novel_text(_DIRTY)

    assert _WM not in cleaned, "水印未被剔除"
    assert report["cleaned"] is True
    assert report["removed_chars"] == len(_DIRTY) - len(cleaned)
    assert report["removed_chars"] > 0
    assert report["chars_in"] == len(_DIRTY)
    assert report["chars_out"] == len(cleaned)

    mark = report["marks"][0]
    assert mark["text"] == _WM
    assert mark["count"] == 90
    assert mark["final"] == 90
    assert mark["final_ratio"] >= 0.9
    # 留痕：每串至少 2 条**原始上下文**样本，供人复核
    assert len(mark["samples"]) >= 2
    for s in mark["samples"]:
        assert _WM in s


def test_report_is_json_serializable():
    _, report = clean_novel_text(_DIRTY)
    dumped = json.dumps(report, ensure_ascii=False)
    assert _WM in dumped
    assert "removed_chars" in dumped


def test_clean_does_not_touch_normal_phrases():
    """正常高频短语（句中、不焊句末）不被误删。"""
    normal = "他点了点头，说道：“好。”\n" * 90
    cleaned, report = clean_novel_text(normal)
    assert cleaned == normal
    assert report["cleaned"] is False
    assert report["removed_chars"] == 0


# ====================================================================
# (b) 确定性
# ====================================================================


def test_clean_is_deterministic():
    c1, r1 = clean_novel_text(_DIRTY)
    c2, r2 = clean_novel_text(_DIRTY)
    assert c1 == c2
    assert json.dumps(r1, ensure_ascii=False, sort_keys=True) == json.dumps(
        r2, ensure_ascii=False, sort_keys=True
    )


# ====================================================================
# (c) 无污染
# ====================================================================


def test_clean_report_flags_no_pollution():
    cleaned, report = clean_novel_text(_CLEAN)
    assert cleaned == _CLEAN
    assert report["cleaned"] is False
    assert report["marks"] == []
    assert report["detected_count"] == 0


def test_build_clean_report_shapes():
    r = build_clean_report(_DIRTY, _DIRTY, [])
    assert set(r) >= {
        "version", "cleaned", "chars_in", "chars_out",
        "removed_chars", "removed_ratio", "detected_count", "marks",
        "rejected_count", "rejected_candidates", "thresholds",
    }


# ====================================================================
# (c') P0 回归：正常对白问句不得被当成水印（刻度标点口径）
# ====================================================================


# 大量正常问句对白：全都以『？』收尾。旧口径（任意句末标点并集）会把『？』计入
# 分子 → 误判为水印删掉；标定口径只看『。』 → 比例 0，必须零命中。
_QUESTIONS = ("怎么办", "没事吧", "怎么了", "是吗", "对吧")
_DIALOGUE = "".join(f"“{_QUESTIONS[i % 5]}？”" for i in range(400))


def test_normal_dialogue_questions_not_flagged():
    from app.corpus_health import find_repeated_watermarks, scan_watermarks

    hits = find_repeated_watermarks(_DIALOGUE)
    assert hits == [], f"正常问句被误判为水印：{[h['text'] for h in hits]}"

    # 被拒候选里应能看到它们 + 拒绝原因（不静默丢弃）
    scan = scan_watermarks(_DIALOGUE)
    rejected = {r["text"]: r for r in scan["rejected"]}
    for q in ("怎么办", "没事吧", "怎么了"):
        assert q in rejected, f"{q} 未出现在被拒候选里"
        assert "『。』" in rejected[q]["reason"]
        assert rejected[q]["final_ratio"] == 0.0


def test_clean_keeps_dialogue_questions_intact():
    cleaned, report = clean_novel_text(_DIALOGUE)
    assert cleaned == _DIALOGUE, "清洗删掉了正常对白问句"
    assert report["removed_chars"] == 0
    assert report["cleaned"] is False
    assert report["rejected_count"] >= 3


# ====================================================================
# (d) 入库链路接入
# ====================================================================


def test_ingest_persists_report_and_clean_chunks(db):
    from app.ingestion.store import ingest_text
    from app.models import NovelChunk, RawNovel

    res = ingest_text(db, _DIRTY_BODY, title="脏文本.txt", platform="upload")

    novel = db.get(RawNovel, res["novel_id"])
    assert novel is not None
    report = novel.clean_report
    assert isinstance(report, dict)
    assert report["cleaned"] is True
    assert report["marks"][0]["text"] == _WM
    assert len(report["marks"][0]["samples"]) >= 2

    # 入库的分片里**不应**再有水印
    chunks = db.query(NovelChunk).filter(NovelChunk.novel_id == novel.id).all()
    assert chunks, "未入库任何分片"
    joined = "\n".join(c.text for c in chunks)
    assert _WM not in joined, "入库分片里仍残留水印 —— 清洗没有接进导入链"


def test_ingest_response_carries_report(db):
    from app.ingestion.store import ingest_text

    res = ingest_text(db, _DIRTY_BODY, title="脏文本2.txt", platform="upload")
    assert isinstance(res["clean_report"], dict)
    assert res["clean_report"]["removed_chars"] > 0


def test_ingest_clean_text_marks_not_cleaned(db):
    from app.ingestion.store import ingest_text
    from app.models import RawNovel

    res = ingest_text(db, _CLEAN_BODY, title="干净.txt", platform="upload")
    novel = db.get(RawNovel, res["novel_id"])
    assert novel.clean_report["cleaned"] is False
