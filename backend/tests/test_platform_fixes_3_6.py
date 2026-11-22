# -*- coding: utf-8 -*-
"""平台修复 3–6：归属选稿罚分、导出章标题、段模式复述闸门、改写重试。"""
from __future__ import annotations

import pathlib
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.db import Base  # noqa: E402
from app.models import Generation, Project  # noqa: E402


def _session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    return eng, sessionmaker(bind=eng, autoflush=False)()


def _rate(rate: float, hits: int, dao: int) -> dict:
    return {"rate": rate, "front": hits, "back": 0, "mid": 0, "front_dao": dao}


# ── 3. 归属选稿罚分 ──────────────────────────────────────────────────────


def test_candidate_score_prefers_near_target_over_low_dao():
    from app.style.attribution_fix import candidate_score

    # 30% 归属、全用「道：」；10% 归属、几乎不用「道：」
    near = candidate_score(abs(0.304 - 0.326), _rate(0.304, 30, 30), "mixed")
    far = candidate_score(abs(0.101 - 0.326), _rate(0.101, 10, 1), "mixed")
    assert near < far, "离目标近的候选必须胜出，不能被道占比罚分压掉"


# ── 4. 导出章标题 ────────────────────────────────────────────────────────


def test_export_body_adds_chapter_headings_once():
    from app.routers.projects import _export_body

    body = _export_body([
        ("第1章《押一付三》", "一正文"),
        ("第1章《押一付三》", "续段"),
        ("第2章《子时后禁止开电梯》", "二正文"),
        ("段3", "段模式正文"),
        ("第3章《炸炉》", "   "),
    ])
    assert body.count("第1章《押一付三》") == 1
    assert body.index("第1章《押一付三》") < body.index("一正文")
    assert body.index("续段") < body.index("第2章《子时后禁止开电梯》")
    assert "段3" not in body


# ── 5. 段模式复述闸门 ────────────────────────────────────────────────────


def test_replay_prior_rows_supports_segment_mode():
    from app.generation.engine import _replay_prior_rows

    eng, s = _session()
    p = Project(title="seg-replay")
    s.add(p)
    s.flush()
    for no in (1, 2, 3):
        s.add(Generation(project_id=p.id, segment_no=no, chapter_range=f"段{no}",
                         text=f"第{no}段正文" * 80,
                         summary={"text": f"第{no}段摘要"}))
    s.commit()

    rows = _replay_prior_rows(s, p, None, before_segment=3)
    assert [r["label"] for r in rows] == ["段1", "段2"]
    assert rows[-1]["summary"] == "第2段摘要"
    assert rows[-1]["outline"] == ""
    # 没有章号也没有段号 → 闸门关闭
    assert _replay_prior_rows(s, p, None) == []
    s.close()
    eng.dispose()


# ── 6. 改写失败重试 ──────────────────────────────────────────────────────


class _SequenceProvider:
    """按顺序返回预设回复（用完后重复最后一条）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def variant(self, **_kwargs):
        return self

    def chat(self, _system, _user, **_kwargs):
        idx = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        return self.replies[idx]


_PRIOR_SCENE = (
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


def _filler(n: int) -> str:
    unit = "窗外的云压得低，巷口的电线杆上落了两只麻雀，叫了一声就飞走了。"
    return (unit * (n // len(unit) + 1))[:n]


def test_replay_rewrite_retries_after_empty_output():
    from app.generation.replay_guard import replay_rewrite_once, scene_replay_check

    original = _PRIOR_SCENE + _filler(1000)
    check = scene_replay_check(original, [_PRIOR_SCENE])
    assert check["replay"] is True
    fixed = _filler(1300)
    prov = _SequenceProvider(["", fixed])
    got, info = replay_rewrite_once(
        prov, original, check,
        [{"chapter": 2, "label": "第2章", "text": _PRIOR_SCENE, "summary": "摘要"}],
        max_chars=4000)
    assert info["applied"] is True
    assert info["attempt"] == 2
    assert got == fixed
    assert prov.calls == 2


def test_voice_rewrite_retries_after_empty_output():
    from app.generation.engine import _dialogue_voice_check, _voice_rewrite_once

    eng, s = _session()
    p = Project(title="voice-retry", plot_bible={"characters": [{
        "name": "许照",
        "card": {"声纹": {"自称": "我", "口头禅": ["得嘞"]}},
    }]})
    s.add(p)
    s.commit()

    original = "许照站在门口，没开口。许照换了个手，还是没说话。"
    check = _dialogue_voice_check(p, original)
    assert check["weak"] == ["许照"]

    fixed = "许照说：「我得嘞，水表我看过。」许照说：「这楼我熟。」"
    prov = _SequenceProvider(["", fixed])
    got, info = _voice_rewrite_once(s, p, prov, original, check, 4000, "第1章")
    assert info["applied"] is True, info
    assert info["attempt"] == 2
    assert got == fixed
    s.close()
    eng.dispose()
