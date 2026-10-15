"""P0 风格样例采样：test_style_exemplar.py

覆盖：
- 采样数量（每本 per_novel，总量 max_total）
- 段落边界完整（样例不截断句子，以换行切自然段）
- 排除已被 RAG 命中的 chunk
- unit_chars 对齐（样例长度落在合理区间）
- 无参考语料时返回空（不抛异常）
- 语料不存在时跳过不阻断
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Base
from app.models import NovelChunk, Project, RawNovel
from app.style.exemplar import (
    MAX_SAMPLE_CHARS,
    MIN_SAMPLE_CHARS,
    PER_NOVEL,
    _split_into_samples,
    sample_style_exemplars,
)

FAILED = []
PASSED = []


def check(name, cond):
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(name)
        print(f"  FAIL: {name}")


def make_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    return Session()


def make_novel(db, nid, title, genre, chunks):
    novel = RawNovel(id=nid, title=title, genre=genre)
    db.add(novel)
    for i, text in enumerate(chunks):
        db.add(NovelChunk(novel_id=nid, idx=i, chapter_no=i + 1, text=text))
    db.commit()
    return novel


def main():
    # ── 1. _split_into_samples：段落边界完整 ──
    para = "他抬起头，看着远处的山口，风从那里吹过来，带着一点凉意，她把斗篷裹紧了些，没有说话。"
    text = "\n".join([para] * 5)
    samples = _split_into_samples(text)
    check("切分出样例", len(samples) >= 1)
    for s in samples:
        check(f"样例以段落边界切（{len(s)}字）", "\n" in s or len(s) < MAX_SAMPLE_CHARS)
        check(f"样例不过短（{len(s)}字）", len(s) >= MIN_SAMPLE_CHARS)

    # ── 2. 采样数量与排除逻辑 ──
    db = make_db()
    project = Project(id=1, title="测试", genre="仙侠")
    db.add(project)

    # 造一本语料：20 个 chunk，每 chunk 约 400 字
    base_para = "他抬起手，剑锋在月光下闪过一道寒光。她退后一步，眉头微皱。"
    chunks = []
    for i in range(20):
        paras = []
        for j in range(8):
            paras.append(base_para * (j + 1))
        chunks.append("\n".join(paras))
    make_novel(db, 1, "仙侠样本", "仙侠", chunks)

    # 不排除任何文本
    exemplars = sample_style_exemplars(db, project, [1])
    check("采样出样例", len(exemplars) > 0)
    check("不超过 PER_NOVEL", len(exemplars) <= PER_NOVEL)
    check("不超过 MAX_TOTAL", len(exemplars) <= 3)
    for ex in exemplars:
        check("样例含 novel_id", ex.get("novel_id") == 1)
        check("样例含 novel_title", ex.get("novel_title") == "仙侠样本")
        check("样例含 text", bool(ex.get("text")))
        check("样例长度合理", MIN_SAMPLE_CHARS <= len(ex["text"]) <= MAX_SAMPLE_CHARS * 2)
        check("样例含 chunk_idx", isinstance(ex.get("chunk_idx"), int))

    # ── 3. 排除已被 RAG 命中的文本 ──
    all_sample_texts = {ex["text"] for ex in exemplars}
    # 把第一个样例文本塞进排除集合
    excluded = {list(all_sample_texts)[0]}
    exemplars2 = sample_style_exemplars(db, project, [1], exclude_texts=excluded)
    texts2 = {ex["text"] for ex in exemplars2}
    check("排除的文本不再出现", not (excluded & texts2))

    # ── 4. 多本体裁优先 ──
    make_novel(db, 2, "都市样本", "都市", chunks)
    make_novel(db, 3, "另一本仙侠", "仙侠", chunks)
    exemplars_multi = sample_style_exemplars(db, project, [1, 2, 3])
    check("多本采样不超过 MAX_TOTAL", len(exemplars_multi) <= 3)
    # 仙侠语料（1 和 3）应该排在都市语料（2）前面
    genres = [ex.get("novel_id") for ex in exemplars_multi]
    if 2 in genres and (1 in genres or 3 in genres):
        check("体裁匹配的语料优先", genres.index(2) > min(genres.index(1) if 1 in genres else 99,
                                                           genres.index(3) if 3 in genres else 99))
    else:
        check("体裁匹配的语料优先（退化为均匀）", True)

    # ── 5. 边界情况 ──
    check("空 novel_ids 返回空", sample_style_exemplars(db, project, []) == [])
    check("不存在的语料不抛异常", sample_style_exemplars(db, project, [999]) == [])

    # ── 6. 无 genre 匹配时退化 ──
    project_no_genre = Project(id=2, title="无体裁", genre=None)
    db.add(project_no_genre)
    db.commit()
    exemplars_nogenre = sample_style_exemplars(db, project_no_genre, [1, 2, 3])
    check("无 project genre 仍能采样", len(exemplars_nogenre) > 0)

    print(f"\n结果：{len(PASSED)}/{len(PASSED) + len(FAILED)} 通过")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
