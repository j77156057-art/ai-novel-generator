# -*- coding: utf-8 -*-
"""章节复述闸门：检测「新章开头重演上一章场景」并约束改写。

真机回归（2026-11-22 五章）第 2→3 章：开头 1200 字共享 35 个 8 字片段、
7 个 12 字片段，而其它相邻章为 0–8 个 / 0 个。测试锁住这条分离度。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.generation import replay_guard  # noqa: E402

#: 上一章里的一段（≈240 字），下一章开头原样重演它。
PRIOR_SCENE = (
    "当晚十一点四十，许照躺在二楼屋里刷手机，听见楼下有动静。"
    "是车库方向。箱子磕在水泥地上，闷闷的，一下，又一下。"
    "他爬起来，从楼梯口的工具包里摸出扳手，别在腰后，拎着钥匙串下楼。"
    "走到电梯口，他按了一下。电梯门没开。显示屏上跳着十一点五十八。"
    "他又按了一下。数字跳到十一点五十九，电梯里哐一声，像什么东西卡住了。"
    "许照拍了拍电梯门，没反应。他掏出手机看了一眼，过了几秒，跳到十二点整。"
    "一楼车库的卷帘门拉起来一半，里头亮着一盏灯，灯泡上糊着灰，光发黄。"
    "钱多多背对着他，正把一只半人高的纸箱往三轮车上搬。"
    "许照站在车库门口，拿扳手敲了敲门框。钱多多回头，脸上先是一笑。"
)

#: 与上一章无关的新场景（正常续写）。
NEW_SCENE = (
    "第二天上午，许照蹲在建材市场门口的台阶上啃包子，手机夹在耳朵和肩膀之间。"
    "王老板从店里探出头，问他那车水泥的账记完没有。他把账本合上，说记完了。"
    "回到巷口的时候，卖早点的两口子正收摊，油锅里的余温把塑料棚熏得发白。"
    "他数了数口袋里的零钱，拐进五金店买了两盒生料带，又挑了一把新的卷尺。"
    "上楼时声控灯坏了三天，物业报修单填了两回，没人来。"
    "他摸黑上到四楼，听见楼下铁门轴转的吱呀声，脚步声不紧不慢，往楼上走。"
    "他停住手，侧耳听了一会儿，脚步声到三楼停了，然后是关门声。"
    "三楼。404那姑娘。许照进了屋，没开大灯，只把玄关的小灯按亮了。"
    "他从枕头底下摸出那本账本，翻开最后一页，那半行被撕掉的字还在。"
)


def _filler(n: int) -> str:
    """与 PRIOR_SCENE 无 8 字重合的填充文本（重复拼接不会产生跨段长匹配）。"""
    unit = "窗外的云压得低，巷口的电线杆上落了两只麻雀，叫了一声就飞走了。"
    return (unit * (n // len(unit) + 1))[:n]


def test_detects_retold_scene():
    new_text = PRIOR_SCENE + _filler(800)
    check = replay_guard.scene_replay_check(new_text, [PRIOR_SCENE])
    assert check["checked"] is True
    assert check["replay"] is True
    assert check["hits"] >= replay_guard.MIN_HITS
    assert check["coverage"] >= replay_guard.MIN_COVERAGE
    assert check["strong_hits"] >= replay_guard.MIN_STRONG_HITS
    assert check["samples"], "判为复述时必须给出可举证的重复片段"


def test_ignores_new_scene():
    new_text = NEW_SCENE + _filler(700)
    check = replay_guard.scene_replay_check(new_text, [PRIOR_SCENE])
    assert check["checked"] is True
    assert check["replay"] is False
    assert check["samples"] == []


def test_skips_when_text_or_history_missing():
    assert replay_guard.scene_replay_check("短。", [PRIOR_SCENE])["checked"] is False
    assert replay_guard.scene_replay_check(PRIOR_SCENE * 2, [])["checked"] is False


def test_block_lists_prior_and_forbids_rewrite():
    block = replay_guard.build_replay_guard_block([{
        "chapter": 2,
        "label": "第2章《子时后禁止开电梯》",
        "summary": "钱多多半夜搬渗水箱，电梯到子时停住。",
        "outline": "第2章大纲",
    }])
    assert "第2章《子时后禁止开电梯》" in block
    assert "钱多多半夜搬渗水箱" in block
    assert "不得" in block
    assert "复述" in block
    assert replay_guard.build_replay_guard_block([]) == ""


class _Provider:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def variant(self, **_kwargs):
        return self

    def chat(self, _system, _user, **_kwargs):
        self.calls += 1
        return self.reply


def _prior():
    return [{
        "chapter": 2,
        "label": "第2章《子时后禁止开电梯》",
        "text": PRIOR_SCENE,
        "summary": "钱多多半夜搬渗水箱，电梯到子时停住。",
        "outline": "第2章大纲",
    }]


def test_rewrite_accepts_fixed_text_with_kept_length():
    original = PRIOR_SCENE + _filler(1000)
    check = replay_guard.scene_replay_check(original, [PRIOR_SCENE])
    assert check["replay"] is True
    fixed = _filler(1100)
    got, info = replay_guard.replay_rewrite_once(
        _Provider(fixed), original, check, _prior(), max_chars=4000)
    assert info["applied"] is True
    assert got == fixed
    assert info["after"]["replay"] is False


def test_rewrite_keeps_original_when_output_empty():
    original = PRIOR_SCENE + _filler(1000)
    check = replay_guard.scene_replay_check(original, [PRIOR_SCENE])
    got, info = replay_guard.replay_rewrite_once(
        _Provider(""), original, check, _prior(), max_chars=4000)
    assert info["applied"] is False
    assert info["reason"] == "空输出"
    assert got == original


def test_rewrite_rejects_when_replay_still_present():
    original = PRIOR_SCENE + _filler(1000)
    check = replay_guard.scene_replay_check(original, [PRIOR_SCENE])
    got, info = replay_guard.replay_rewrite_once(
        _Provider(original), original, check, _prior(), max_chars=4000)
    assert info["applied"] is False
    assert info["reason"] == "复述仍在"
    assert got == original


# ── 数据层与重生成路径接线 ────────────────────────────────────────────────


def _memory_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    import app.models  # noqa: F401 —— 注册全部表

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    return eng, sessionmaker(bind=eng, autoflush=False)()


def test_replay_prior_rows_excludes_current_and_uses_summary_text():
    from app.generation.engine import _replay_prior_rows
    from app.models import Generation, Project

    eng, s = _memory_session()
    p = Project(title="replay-prior", outline=[
        "第1章《一》：甲", "第2章《二》：乙", "第3章《三》：丙"])
    s.add(p)
    s.flush()
    s.add_all([
        Generation(project_id=p.id, segment_no=1, chapter_range="第1章《一》",
                   text="甲" * 300, summary={"text": "第一章摘要"}),
        Generation(project_id=p.id, segment_no=2, chapter_range="第2章《二》",
                   text="乙" * 300, summary={"text": "第二章摘要"}),
        Generation(project_id=p.id, segment_no=3, chapter_range="第3章《三》",
                   text="丙" * 300, summary={"text": "第三章摘要"}),
    ])
    s.commit()

    rows = _replay_prior_rows(s, p, 3)
    assert [r["chapter"] for r in rows] == [1, 2]
    assert rows[-1]["summary"] == "第二章摘要"
    assert "第2章" in rows[-1]["outline"]
    # 非分章生成不做判断
    assert _replay_prior_rows(s, p, None) == []
    s.close()
    eng.dispose()


def test_regenerate_applies_replay_guard(monkeypatch):
    from unittest.mock import patch

    from app.generation import engine
    from app.models import Generation, Project

    eng, s = _memory_session()
    p = Project(title="regen-replay", target_length=30000, generation_length=3000,
                mode="quick", outline=["第1章《一》：甲", "第2章《二》：乙"])
    s.add(p)
    s.flush()
    s.add(Generation(project_id=p.id, segment_no=1, chapter_range="第1章《一》",
                     text=PRIOR_SCENE + _filler(600), summary={"text": "第一章摘要"}))
    s.add(Generation(project_id=p.id, segment_no=2, chapter_range="第2章《二》",
                     text="旧正文" * 200, summary={"text": "旧摘要"}))
    s.commit()

    replayed = PRIOR_SCENE + _filler(1000)
    fixed = _filler(1300)
    seen: dict = {}

    def fake_rewrite(provider, text, check, prior, max_chars):
        seen["check"] = check
        return fixed, {"applied": True, "before": check,
                       "after": {"replay": False}}

    class FP:
        def chat(self, _system, _user, **_kwargs):
            return "x"

        def stream_chat(self, _system, _user, **_kwargs):
            yield replayed

        def summarize(self, text):
            return f"sum({len(text)})"

        def summarize_rollup(self, _texts, level):
            return f"roll({level})"

    with patch("app.generation.engine._style_narrative", return_value=None), \
         patch("app.generation.engine._enqueue_v3_extraction",
               return_value={"enqueued": False}), \
         patch("app.generation.engine.current_node_prompt", return_value=None), \
         patch("app.generation.engine.replay_rewrite_once", side_effect=fake_rewrite):
        chunks = list(engine.regenerate_segment_stream(
            s, p, segment_no=2, provider=FP(), chapter_label="第2章《二》"))

    done = chunks[-1]
    assert done["type"] == "done"
    assert seen["check"]["replay"] is True
    assert done["replay_guard"]["rewrite"]["applied"] is True
    s.expire_all()
    target = (s.query(Generation)
              .filter_by(project_id=p.id, segment_no=2).one())
    assert target.text == fixed
    import json

    q = target.quality_json
    if isinstance(q, str):
        q = json.loads(q)
    assert (q or {}).get("replay_guard", {}).get("rewrite", {}).get("applied") is True
    s.close()
    eng.dispose()
