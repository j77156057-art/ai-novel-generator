"""文风档案体检：禁用词按**语法角色**判 + 「修档案」入口（2026-10-28）。

## 为什么有这份测试

真机验收时，纯子串判据把

    场景常先以人物当下一刻的细微反应或即时判断切入

里的「下一刻」判成转折连词冲突 ⇒ `error` ⇒ **挡住生成**
（量化：蒸馏库 4 份档案 2 份命中、验收库 2/2 项目命中 ⇒ 真档案几乎必然被挡）。

修判据最怕的是「**尺没自证**」—— 放宽过头会把真冲突也放过，
正文里重新长出那些转折词。所以这份测试的顺序是：

1. **阳性对照**（先）：清单里每一个词，用它的**连词用法**写进规则正文
   ⇒ 必须**全部**判成冲突。尺先自证。
2. **阴性对照**（后）：名词性用法 ⇒ 不判冲突（但要报出来）。
3. 修档案：三种修法 + 端点行为（preview 不落库 / apply 落库并重跑体检 / 留痕）。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.style.forbidden_usage import (  # noqa: E402
    MODE_DROP_RULE,
    MODE_REMOVE_WORD,
    MODE_UNBAN,
    ROLE_ATTRIBUTIVE,
    ROLE_CONNECTIVE,
    analyze,
    classify_occurrence,
    is_phrase_template,
    plan_repair,
)

#: 标准禁用清单里的转折词（与蒸馏产物同款）。
TURN_WORDS = [
    "然而", "但是", "不过", "与此同时", "紧接着",
    "下一刻", "旋即", "随即", "顷刻间", "刹那之间", "电光火石之间",
]


def _narrative(rules: dict[str, list[str]], forbidden: dict[str, list[str]]) -> dict:
    """造一份最小档案（结构化规则 + 禁用清单）。"""
    return {
        "rules": {dim: [{"rule": r} for r in items] for dim, items in rules.items()},
        "forbidden": {dim: list(items) for dim, items in forbidden.items()},
    }


# ====================================================================
# 1. 阳性对照：真冲突必须仍然判成冲突
# ====================================================================

@pytest.mark.parametrize("word", TURN_WORDS)
def test_转折词的连词用法一律判成冲突(word):
    """尺的自证：清单里每个词的**连词用法**都必须是冲突。

    每句都把词放在转折的位置上（后面跟逗号或动词），不是名词性用法。
    """
    text = f"动作推到顶点，{word}另一头的人先动了。"
    assert classify_occurrence(text, text.index(word), word) == ROLE_CONNECTIVE

    narr = _narrative(
        {"语序编排": [text]},
        {"禁用转折词": [word]},
    )
    analysis = analyze(narr, text)
    assert analysis.conflict_words == [word], (
        f"「{word}」的连词用法被判成了非冲突 —— 判据放宽过头了")


@pytest.fixture()
def mem_session():
    """自建内存库 + 会话（不碰真实库的项目档案）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    import app.models  # noqa: F401

    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    try:
        yield db
    finally:
        db.close()


@pytest.mark.parametrize("word", TURN_WORDS)
def test_连词用法在体检里是error(word, mem_session):
    """真冲突 ⇒ `error` ⇒ 仍然挡住生成。"""
    from app.agents.prompt_audit import run_prompt_audit
    from app.models import Project

    db = mem_session
    proj = Project(id=1, title="t", generation_length=3000)
    proj.style_narrative = _narrative(
        {"语序编排": [f"动作推到顶点，{word}另一头的人先动了。"]},
        {"禁用转折词": [word]},
    )
    db.add(proj)
    db.commit()

    report, _stats = run_prompt_audit(db, proj)
    errors = [i for i in report.issues
              if getattr(i, "rule", "") == "prompt_forbidden_self_conflict"]
    assert errors, f"「{word}」的连词用法没有报出冲突"
    assert getattr(errors[0], "severity", "") == "error"


# ====================================================================
# 2. 阴性对照：名词性用法不判冲突（但要报出来）
# ====================================================================

def test_名词性用法不判冲突():
    """「下一刻的细微反应」= 名词性状语，不是转折连词 ⇒ 不判冲突。"""
    text = "场景常先以人物当下一刻的细微反应或即时判断切入，再补环境与前因。"
    assert classify_occurrence(text, text.index("下一刻"), "下一刻") == ROLE_ATTRIBUTIVE

    narr = _narrative(
        {"语序编排": [text]},
        {"禁用转折词": ["下一刻"]},
    )
    analysis = analyze(narr, text)
    assert analysis.conflicts == [], "名词性用法被判成冲突了（这正是要修的误报）"
    assert analysis.attributive_words == ["下一刻"], "名词性用法应当被报出来（只是不挡生成）"


def test_整句模板按字面判冲突():
    """禁用项是**整句模板**时没有「用法差别」这回事 ⇒ 一律判冲突。"""
    assert is_phrase_template("你知道吗？") is True
    assert is_phrase_template("他终于明白了活着的意义。") is True
    assert is_phrase_template("下一刻") is False

    text = "对白直接用一句「你知道吗？」把设定讲完。"
    narr = _narrative({"对白风格": [text]}, {"禁用解释性对白": ["你知道吗？"]})
    analysis = analyze(narr, text)
    assert analysis.conflict_words == ["你知道吗？"], "整句模板必须按字面判冲突"


def test_模型看不见的词不判冲突():
    """规则里的词被截断掉（没进提示词）⇒ 模型看不见 ⇒ 不该替它挡生成。"""
    narr = _narrative(
        {"语序编排": ["场景常先以人物当下一刻的细微反应或即时判断切入。"]},
        {"禁用转折词": ["下一刻"]},
    )
    # 渲染块里根本没有这个词（模拟 `_clip` 把它截掉了）
    analysis = analyze(narr, "场景常先以人物当细微反应或即时判断切入。")
    assert analysis.conflicts == [], "模型看不见的词不该判成冲突"


def test_名词性用法在体检里是warn(mem_session):
    """名词性用法 ⇒ `warn`：不挡生成，但**不隐藏**。"""
    from app.agents.prompt_audit import run_prompt_audit
    from app.models import Project

    db = mem_session
    proj = Project(id=1, title="t", generation_length=3000)
    proj.style_narrative = _narrative(
        {"语序编排": ["场景常先以人物当下一刻的细微反应或即时判断切入。"]},
        {"禁用转折词": ["下一刻"]},
    )
    db.add(proj)
    db.commit()

    report, _stats = run_prompt_audit(db, proj)
    conflicts = [i for i in report.issues
                 if getattr(i, "rule", "") == "prompt_forbidden_self_conflict"]
    warns = [i for i in report.issues
             if getattr(i, "rule", "") == "prompt_forbidden_word_in_rules"]
    assert conflicts == [], "名词性用法不该报成冲突（error 会挡住生成）"
    assert warns, "名词性用法要报出来（warn）"
    assert getattr(warns[0], "severity", "") == "warn"


# ====================================================================
# 3. 修档案：三种修法
# ====================================================================

def test_修档案_remove_word_删词留规则():
    text = "场景常先以人物当下一刻的细微反应或即时判断切入。"
    narr = _narrative({"语序编排": [text, "另一条不相干的规则。"]},
                       {"禁用转折词": ["下一刻"]})
    plan = plan_repair(narr, "下一刻", MODE_REMOVE_WORD)
    assert plan is not None
    assert plan.affected == 1
    rules = plan.narrative["rules"]["语序编排"]
    assert rules[0]["rule"] == "场景常先以人物当细微反应或即时判断切入。"
    assert rules[1]["rule"] == "另一条不相干的规则。"   # 别的规则一个字都不动
    assert "下一刻" not in rules[0]["rule"]


def test_修档案_drop_rule_删掉整条():
    text = "场景常先以人物当下一刻的细微反应或即时判断切入。"
    narr = _narrative({"语序编排": [text, "另一条规则。"]},
                       {"禁用转折词": ["下一刻"]})
    plan = plan_repair(narr, "下一刻", MODE_DROP_RULE)
    assert plan is not None
    rules = plan.narrative["rules"]["语序编排"]
    assert len(rules) == 1 and rules[0]["rule"] == "另一条规则。"


def test_修档案_unban_从清单里去掉():
    narr = _narrative({"语序编排": ["一条规则。"]},
                       {"禁用转折词": ["下一刻", "然而"]})
    plan = plan_repair(narr, "下一刻", MODE_UNBAN)
    assert plan is not None
    assert plan.narrative["forbidden"]["禁用转折词"] == ["然而"]


def test_修档案_词不在档案里返回None():
    narr = _narrative({"语序编排": ["一条规则。"]}, {"禁用转折词": ["下一刻"]})
    assert plan_repair(narr, "不存在的词", MODE_REMOVE_WORD) is None


def test_修档案_不动证据引文():
    """证据引文是**语料原句**，渲染前会被剥掉 ⇒ 词只在证据里就别改它。

    真跑时踩到：修复把「（证据：下一刻，灰衣老者猛然停下身形）」改成了
    「（证据：，灰衣老者猛然停下身形）」—— 改坏了语料原句，还留下一个孤零零的逗号，
    而提示词里那句话**根本没进**（证据会被剥掉）。
    """
    only_evidence = "动作短促推进（证据：下一刻，灰衣老者猛然停下身形）"
    narr = _narrative({"节奏控制": [only_evidence]}, {"禁用转折词": ["下一刻"]})
    assert plan_repair(narr, "下一刻", MODE_REMOVE_WORD) is None, \
        "只在证据引文里的词不该触发修改"

    in_body = "场景常先以人物当下一刻的细微反应切入（证据：只是略微失神）"
    narr2 = _narrative({"语序编排": [in_body]}, {"禁用转折词": ["下一刻"]})
    plan = plan_repair(narr2, "下一刻", MODE_REMOVE_WORD)
    assert plan is not None
    after = plan.narrative["rules"]["语序编排"][0]["rule"]
    assert after == "场景常先以人物当细微反应切入（证据：只是略微失神）", \
        "证据引文必须原样保留"


def test_修档案_不动原档案():
    """修复方案是**拷贝**上的改动：原档案不能被动。"""
    narr = _narrative({"语序编排": ["当下一刻的细微反应切入。"]},
                       {"禁用转折词": ["下一刻"]})
    before = narr["rules"]["语序编排"][0]["rule"]
    plan_repair(narr, "下一刻", MODE_REMOVE_WORD)
    assert narr["rules"]["语序编排"][0]["rule"] == before


# ====================================================================
# 4. 修档案端点：preview 不落库 / apply 落库并自证 / 找不到词要报错
# ====================================================================

def _make_project(db, narrative: dict):
    from app.models import Project

    proj = Project(id=1, title="t", generation_length=3000)
    proj.style_narrative = narrative
    db.add(proj)
    db.commit()
    return proj


def _repair(db, **kw):
    from app.routers.projects import (
        StyleNarrativeRepairRequest,
        repair_style_narrative,
    )

    body = StyleNarrativeRepairRequest(**kw)
    return repair_style_narrative(project_id=1, body=body, db=db)


def test_端点_preview不落库(mem_session):
    db = mem_session
    narr = _narrative({"语序编排": ["当下一刻的细微反应切入。"]},
                       {"禁用转折词": ["下一刻"]})
    proj = _make_project(db, narr)

    res = _repair(db, word="下一刻", mode=MODE_REMOVE_WORD, preview=True)
    assert res["ok"] is True
    assert res["applied"] is False
    assert res["changes"], "preview 要说清将要改什么"
    # 档案一个字节都没动
    db.refresh(proj)
    assert proj.style_narrative["rules"]["语序编排"][0]["rule"] == \
        "当下一刻的细微反应切入。"


def test_端点_apply落库并重跑体检(mem_session):
    db = mem_session
    narr = _narrative(
        {"语序编排": ["动作推到顶点，下一刻另一头的人先动了。"]},
        {"禁用转折词": ["下一刻"]},
    )
    proj = _make_project(db, narr)

    res = _repair(db, word="下一刻", mode=MODE_REMOVE_WORD, preview=False)
    assert res["applied"] is True
    db.refresh(proj)
    assert "下一刻" not in proj.style_narrative["rules"]["语序编排"][0]["rule"]
    # 留痕：改前的原文在档案里
    history = proj.style_narrative.get("repair_history") or []
    assert history and history[-1]["word"] == "下一刻"
    assert history[-1]["changes"][0]["before"]
    # 自证：体检里那个冲突没了
    assert res["audit"]["errors"] == 0, "修完了还报 error，说明没修干净"


def test_端点_修完冲突消失(mem_session):
    """真冲突修完后，体检不再报 error。"""
    db = mem_session
    narr = _narrative(
        {"语序编排": ["动作推到顶点，下一刻另一头的人先动了。"]},
        {"禁用转折词": ["下一刻"]},
    )
    _make_project(db, narr)
    res = _repair(db, word="下一刻", mode=MODE_REMOVE_WORD, preview=False)
    assert res["audit"]["errors"] == 0
    assert all(i["rule"] != "prompt_forbidden_self_conflict"
               for i in res["audit"]["issues"])


def test_端点_词不在档案里返回404(mem_session):
    from fastapi import HTTPException

    db = mem_session
    _make_project(db, _narrative({"语序编排": ["一条规则。"]},
                                  {"禁用转折词": ["下一刻"]}))
    with pytest.raises(HTTPException) as exc:
        _repair(db, word="查无此词", mode=MODE_REMOVE_WORD, preview=False)
    assert exc.value.status_code == 404


def test_端点_模式不认识返回400(mem_session):
    from fastapi import HTTPException

    db = mem_session
    _make_project(db, _narrative({"语序编排": ["一条规则。"]},
                                  {"禁用转折词": ["下一刻"]}))
    with pytest.raises(HTTPException) as exc:
        _repair(db, word="下一刻", mode="乱写", preview=False)
    assert exc.value.status_code == 400


def test_端点_没有档案返回400(mem_session):
    from fastapi import HTTPException

    from app.models import Project

    db = mem_session
    db.add(Project(id=1, title="t", generation_length=3000))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _repair(db, word="下一刻", mode=MODE_REMOVE_WORD, preview=False)
    assert exc.value.status_code == 400
