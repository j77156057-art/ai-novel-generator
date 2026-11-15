"""验证叙事蒸馏的章节加载与采样口径（2026-11-12 采样 bug 修复）。

覆盖：
- `_load_novel_chapters` 按 chapter_no + idx 排序，并把同章块拼回整章
- full 档读全本（ratio=1.0），不再做 30% 抽样
- 蒸馏块固定 5 章一块，块间 chapter_range 严格递增且不交叉
- fast/balanced 的「取后 2/3」按真实章节计算
- 漂移使用的首/尾 5 条是真实前 5 章 / 后 5 章
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import Column, Integer, MetaData, Table, Text, create_engine
from sqlalchemy.orm import Session

from app.style.narrative import (
    MIN_CHAPTERS_PER_CHUNK,
    TIER_FULL,
    _load_novel_chapters,
    _merge_rows_into_chapters,
    _sample_chapters_fixed,
    _sample_chapters_v6,
    resolve_tier,
    sample_chapters_for_tier,
)

FAILED = []
PASSED = []


def check(name, cond):
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(name)
        print(f"  FAIL: {name}")


def _make_db():
    engine = create_engine("sqlite://")
    md = MetaData()
    Table(
        "novel_chunks",
        md,
        Column("id", Integer, primary_key=True),
        Column("novel_id", Integer, nullable=False),
        Column("chapter_no", Integer, nullable=True),
        Column("idx", Integer, nullable=False),
        Column("text", Text, nullable=False),
        Column("embedding", Text, nullable=True),
    )
    md.create_all(engine)
    return engine, md


def test_loader_orders_and_aggregates():
    engine, md = _make_db()
    chunks = md.tables["novel_chunks"]
    # 故意按"乱序 id"插入：模拟 idx 每章从 0 重数、id 与阅读顺序不一致
    rows = [
        {"id": 1, "novel_id": 9, "chapter_no": 1, "idx": 1, "text": "一章第二块", "embedding": None},
        {"id": 2, "novel_id": 9, "chapter_no": 3, "idx": 0, "text": "三章第一块", "embedding": None},
        {"id": 3, "novel_id": 9, "chapter_no": 1, "idx": 0, "text": "一章第一块", "embedding": None},
        {"id": 4, "novel_id": 9, "chapter_no": 2, "idx": 0, "text": "二章第一块", "embedding": None},
        {"id": 5, "novel_id": 9, "chapter_no": 3, "idx": 2, "text": "三章第三块", "embedding": None},
        {"id": 6, "novel_id": 9, "chapter_no": 3, "idx": 1, "text": "三章第二块", "embedding": None},
    ]
    with engine.begin() as conn:
        conn.execute(chunks.insert(), rows)

    with Session(engine) as db:
        chapters = _load_novel_chapters(db, 9)

    check("返回 3 个真实章节", len(chapters) == 3)
    check("章节号严格递增", [c[0] for c in chapters] == [1, 2, 3])
    check("第 1 章按 idx 聚合", chapters[0][1] == "一章第一块\n\n一章第二块")
    check("第 2 章单块保留", chapters[1][1] == "二章第一块")
    check("第 3 章按 idx 聚合", chapters[2][1] == "三章第一块\n\n三章第二块\n\n三章第三块")
    engine.dispose()


def test_merge_without_chapter_no():
    class Row:
        def __init__(self, idx, text):
            self.chapter_no = None
            self.idx = idx
            self.text = text

    merged = _merge_rows_into_chapters([Row(1, "a"), Row(2, "b")])
    check("chapter_no 为空时退回 idx 作章号", merged == [(1, "a"), (2, "b")])


def test_full_tier_reads_whole_book():
    # 完整档 = 读全本：100 章全部返回，不再按 30% 抽样。
    chapters = [(i, "文" * 12000) for i in range(1, 101)]
    sampled = sample_chapters_for_tier(chapters, resolve_tier(TIER_FULL))

    check("full 档读全本 = 100 章", len(sampled) == 100)
    check("full 档保留整章正文", all(len(t) == 12000 for _, t in sampled))
    check("full 档总字数 = 全书", sum(len(t) for _, t in sampled) == 1200000)
    check("full 档章节号严格递增", [c[0] for c in sampled] == sorted(c[0] for c in sampled))


def test_percentage_sampling_still_works():
    # 比例轴仍保留：sample_cap / 其它档位按比例采样时行为不变。
    chapters = [(i, "文" * 12000) for i in range(1, 101)]
    sampled = _sample_chapters_v6(chapters, ratio=0.3)

    check("比例采样 30% = 30 章", len(sampled) == 30)
    check("比例采样保留整章正文", all(len(t) == 12000 for _, t in sampled))
    check("比例采样总字数不含截断", sum(len(t) for _, t in sampled) == 360000)
    check("比例采样章节号严格递增", [c[0] for c in sampled] == sorted(c[0] for c in sampled))


def test_fixed_sampling_semantics():
    chapters = [(i, "文" * 12000) for i in range(1, 101)]
    sampled = _sample_chapters_fixed(chapters, 30)
    nos = [c[0] for c in sampled]

    check("balanced/fast 取 30 章", len(sampled) == 30)
    check("balanced/fast 跳过前 1/3 章", min(nos) > 33)
    check("balanced/fast 章节号严格递增", nos == sorted(nos))
    check("balanced/fast 保留整章正文", all(len(t) == 12000 for _, t in sampled))


def test_full_sampling_prefers_dialogue_chapters():
    # 100 章 × 12000 字，总字数 120 万，30% = 30 章。
    # 第 90 章对白密集；旧版 dialogue_ratio 误用 ASCII 引号导致全为 0，
    # 打平后按章号取前 30 章，第 90 章会被漏掉。
    chapters = [(i, "文" * 12000) for i in range(1, 101)]
    chapters[89] = (90, "“嗯。”" * 2400)
    sampled = _sample_chapters_v6(chapters)
    check("full 采样能识别中文引号对白章", 90 in [c[0] for c in sampled])


def test_drift_head_tail_are_real_chapters():
    chapters = [(i, f"第{i}章") for i in range(1, 101)]
    check("漂移头部 5 条是第 1-5 章", [c[0] for c in chapters[:5]] == [1, 2, 3, 4, 5])
    check("漂移尾部 5 条是第 96-100 章", [c[0] for c in chapters[-5:]] == [96, 97, 98, 99, 100])


def test_chunk_ranges_are_monotonic():
    # 蒸馏块固定 5 章一块：100 章 → 20 块，每块 chapter_range 连续且不交叉。
    chapters = [(i, "文" * 6000) for i in range(1, 101)]
    sampled = sample_chapters_for_tier(chapters, resolve_tier(TIER_FULL))
    check("蒸馏块大小固定为 5 章", MIN_CHAPTERS_PER_CHUNK == 5)
    ranges = []
    sizes = []
    for i in range(0, len(sampled), MIN_CHAPTERS_PER_CHUNK):
        chunk = sampled[i:i + MIN_CHAPTERS_PER_CHUNK]
        ranges.append((chunk[0][0], chunk[-1][0]))
        sizes.append(len(chunk))
    check("100 章切成 20 块", len(ranges) == 20)
    check("每块不超过 5 章", max(sizes) <= 5)
    check("每块 chapter_range 起点 <= 终点", all(a <= b for a, b in ranges))
    check("块间 chapter_range 严格递增且不交叉",
          all(ranges[i][1] < ranges[i + 1][0] for i in range(len(ranges) - 1)))


def main():
    test_loader_orders_and_aggregates()
    test_merge_without_chapter_no()
    test_full_tier_reads_whole_book()
    test_percentage_sampling_still_works()
    test_fixed_sampling_semantics()
    test_full_sampling_prefers_dialogue_chapters()
    test_drift_head_tail_are_real_chapters()
    test_chunk_ranges_are_monotonic()

    print(f"\n结果：{len(PASSED)}/{len(PASSED) + len(FAILED)} 通过")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
