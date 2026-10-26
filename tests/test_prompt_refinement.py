"""提示词细化：基线区间 + 要求带例句（docs/style-prompt-refinement-design.md §2.2/§2.4）。

「要求带案例」的材料**本来就在档案里** —— 叙事档案每条规则尾部就附着
≤20 字原文证据（渲染时被剥掉，因为证据含参考书专名）。
本文件钉住：证据被取出、脱敏后作为例句渲染；**没有专名表就不给例句**。
"""
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.style.narrative import _extract_example, describe_narrative  # noqa: E402


# ── 证据取出 ─────────────────────────────────────────────────────────────
def test_extract_example_takes_first_sentence():
    text = "用连续动作描写紧张感（证据：他手里的碗晃了一下，洒出半碗水。）"
    ex = _extract_example(text)
    assert ex == "他手里的碗晃了一下，洒出半碗水", ex


def test_extract_example_respects_length_cap():
    text = "用动作写紧张（证据：" + "长" * 60 + "。）"
    assert len(_extract_example(text)) <= 25


def test_extract_example_empty_when_no_evidence():
    assert _extract_example("用连续动作描写紧张感") == ""


# ── 渲染时附例句 ─────────────────────────────────────────────────────────
def _profile_with_rule(rule: str) -> dict:
    return {"rules": {"人物描写手法": [{"rule": rule}]}, "forbidden": {}}


def test_no_example_without_redactor():
    """没有专名表 ⇒ **不给例句**（证据是参考书原句，不脱敏会带进专名）。"""
    profile = _profile_with_rule(
        "用微表情表现迟疑（证据：徐凤年的手指在袖口里动了一下，又停住。）"
    )
    out = describe_narrative(profile, dims=("人物描写手法",))
    assert "例：" not in out, out
    assert "徐凤年" not in out, out


def test_example_rendered_and_redacted():
    """有专名表 ⇒ 例句脱敏后附在要求后面。"""
    from app.style.entities import Redactor

    red = Redactor({"characters": ["徐凤年"]})
    profile = _profile_with_rule(
        "用微表情表现迟疑（证据：徐凤年的手指在袖口里动了一下，又停住。）"
    )
    out = describe_narrative(profile, dims=("人物描写手法",), redactor=red)
    assert "例：" in out, out
    assert "徐凤年" not in out, out
    assert "袖口里动了一下" in out, out


def test_example_keeps_placeholder_length():
    """占位符**等长** —— 换名字会改变句长，模型从例句里学到的句长就错了。"""
    from app.style.entities import Redactor

    red = Redactor({"characters": ["徐凤年"]})
    profile = _profile_with_rule(
        "用微表情表现迟疑（证据：徐凤年的手指在袖口里动了一下，又停住。）"
    )
    out = describe_narrative(profile, dims=("人物描写手法",), redactor=red)
    ex = out.split("例：", 1)[1].split("）", 1)[0]
    assert "徐凤年" not in ex
    assert len(ex) == len("徐凤年的手指在袖口里动了一下，又停住"), (ex, len(ex))


def test_example_dropped_when_contains_pmi_candidate():
    """兜底闸：专名表**保证不了覆盖**（实测表外仍有专名漏进提示词）。

    ⇒ 再查一遍「判定前的 PMI 候选」——命中就整句丢掉。
    """
    from app.style.entities import Redactor

    red = Redactor({"characters": []})
    profile = _profile_with_rule(
        "用微表情表现迟疑（证据：耶律慕容的脸僵了一下。）"
    )
    # 不给 guard ⇒ 会渲染
    assert "例：" in describe_narrative(profile, dims=("人物描写手法",), redactor=red)
    # 给了 guard（候选里含「耶律慕容」）⇒ 丢掉
    out = describe_narrative(
        profile, dims=("人物描写手法",), redactor=red, example_guard=["耶律慕容"],
    )
    assert "例：" not in out, out


def test_example_kept_when_guard_does_not_match():
    from app.style.entities import Redactor

    red = Redactor({"characters": []})
    profile = _profile_with_rule(
        "用微表情表现迟疑（证据：那只手在袖口里动了一下，又停住。）"
    )
    out = describe_narrative(
        profile, dims=("人物描写手法",), redactor=red, example_guard=["耶律慕容", "北凉"],
    )
    assert "例：" in out, out
