"""验证 _is_real_plothole 闸门：真问句放行、金句陈述拦掉。用 demo 库真实 chunk跑。"""
import sys

sys.path.insert(0, ".")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.kg.extractor import RuleBasedExtractor, resolve_plotholes
from app.models import NovelChunk

engine = create_engine("sqlite:///novel_demo.db")
with Session(engine) as db:
    chunks = db.scalars(select(NovelChunk).order_by(NovelChunk.id)).all()
    print(f"语料：{len(chunks)} chunk / "
          f"{sum(len(c.text or '') for c in chunks)} 字\n")

    #闸门单测（正例/反例）
    from app.kg.extractor import _is_real_plothole, _UNFINISHED_RE, _QUESTION_MARK_RE

    cases = [
        # (句子, 期望 True=真坑 / False=假坑)
        ('林夜终于问出口："那光是什么？"', True),
        ("你再动一下，我就把警报按下去。", False),
        ("不会被遗忘。", False),
        ("本身就是最贵的代价。", False),
        ("直到很多章以后，他才明白，不被遗忘，本身就是最贵的代价。", False),
        ("苏晚回头看他，眼神复杂", False),
        # 真坑：主角失忆是典型信息缺口（无问号，但含疑问词「怎么」）
        ("林夜记不清自己是怎么倒下的", True),
        # 真坑：疑问词「到底」
        ("到底是谁在清道夫", True),
        ("那光会不会消失？", True),
        ("这是不是意味着她一直在骗我", True),
    ]
    print("=== 闸门单测 ===")
    ok = 0
    for sent, expect in cases:
        hq = bool(_QUESTION_MARK_RE.search(sent))
        un = bool(_UNFINISHED_RE.search(sent))
        got = _is_real_plothole(sent, hq, un)
        mark = "PASS" if got == expect else "FAIL"
        ok += got == expect
        print(f"  [{mark}] expect={expect!s:5} got={got!s:5} {sent}")
    print(f"  --> {ok}/{len(cases)} 通过\n")

    print("=== 整链抽取（修复后） ===")
    res = RuleBasedExtractor().extract(chunks)
    resolve_plotholes(res, chunks)
    print(f"  角色 {len(res.characters)}: "
          f"{ {n: c.mentions for n, c in res.characters.items()} }")
    print(f"  坑 {len(res.plotholes)}:")
    for ph in res.plotholes.values():
        print(f"    cue={ph.cue!r} ch={ph.first_chapter}-{ph.last_chapter} "
              f"status={ph.status} question={ph.question!r}")
    print(f"  关系 {len(res.relationships)}")