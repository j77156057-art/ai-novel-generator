"""角色个性（persona）· 阶段化蒸馏进度（2026-10-28）。

## 为什么有这份测试

真跑那本新语料（《雾港守夜录》，自造示例名）时，产物在指标上全对（形态在带内、抄书 0 命中、专名 0 泄漏），
**但人读着是扁的**：所有角色一个腔调。查下来是「角色个性没有数据通路」——
原创路线下 `prompts._character_profile_block` 直接 return ""，
而角色约束块只有「身份 + 动机」，system 里「说话」出现 **0 次**。

这份测试钉三件事：

1. **persona 进得了提示词**，且**没有 persona 时逐字节不变**（老项目不受影响）；
2. **反推结果不许编造**：带引号的片段必须能在正文里查到；
3. **进度按阶段推进**，绝不提前 100%（改前 0.6 秒内从 17% 跳到 100%）。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.generation.persona import (  # noqa: E402
    SOURCE_DERIVED,
    character_line,
    character_snippets,
    derive_personas,
    normalize_persona,
    persona_is_grounded,
    render_persona,
)

# ====================================================================
# 1. 规范化与渲染
# ====================================================================

def test_规整成四字段():
    raw = {
        "说话方式": "自称「我」，句短",
        "行为倾向": ["被质问时→先沉默再反问"],
        "性格底线": "不会当众示弱",          # 字符串也认
        "关键经历": ["七年前那一夜他活下来了"],
        "多余的字段": "丢掉",
    }
    p = normalize_persona(raw)
    assert set(p) == {"说话方式", "行为倾向", "性格底线", "关键经历"}
    assert p["性格底线"] == ["不会当众示弱"]


def test_渲染成人话():
    p = {"说话方式": "句子短，从不解释只给结论",
         "行为倾向": ["被质疑守灯时→先沉默三秒再反问"],
         "性格底线": ["不会承认弑神者身份"],
         "关键经历": ["七年前神陨之夜他是唯一活下来的执灯人"]}
    text = render_persona(p)
    assert "说话：" in text and "会：" in text and "不会：" in text and "经历：" in text


def test_没有persona时角色行逐字节不变():
    """老项目（没有 persona）的提示词必须一字不差。"""
    c = {"name": "沈砚", "role": "守将之子", "motivation": "把父亲的刀带回去"}
    assert character_line(c) == "  · 沈砚（守将之子；动机：把父亲的刀带回去）"


def test_有persona时角色行带个性():
    c = {"name": "顾星河", "role": "执灯人", "motivation": "查明真相",
         "persona": {"说话方式": "句短，不解释"}}
    line = character_line(c)
    assert line.startswith("  · 顾星河（执灯人；动机：查明真相）")
    assert "说话：句短，不解释" in line


# ====================================================================
# 2. 不许编造
# ====================================================================

def test_引文必须在正文里():
    text = "陆沉舟说：「让我看看那张星图。」然后把纸摊在桌上。"
    ok = {"说话方式": "会用「让我看看那张星图」这种直接的请求"}
    bad = {"说话方式": "常用「你说什么我听不懂」推托"}
    assert persona_is_grounded(ok, text) is True
    assert persona_is_grounded(bad, text) is False


class _FakeProvider:
    """假 provider：返回固定 JSON，记下被调用几次。"""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def chat(self, system, user, constraints=None):
        import json
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


def test_反推丢掉编造字段():
    text = "陆沉舟说：「让我看看那张星图。」"
    prov = _FakeProvider({"characters": [{
        "name": "陆沉舟",
        "说话方式": "会用「让我看看那张星图」这种直接的请求",
        "性格底线": ["不会说「这件事我不知道」"],   # 编的，正文里没有
    }]})
    out = derive_personas(prov, {"陆沉舟": [text]})
    assert prov.calls == 1, "一批角色只该调一次模型"
    p = out["陆沉舟"]
    assert "说话方式" in p
    assert "性格底线" not in p, "带查不到的引文的字段必须整条丢掉"


def test_反推一次调用覆盖多角色():
    prov = _FakeProvider({"characters": [
        {"name": "甲", "说话方式": "短句"},
        {"name": "乙", "说话方式": "长句"},
    ]})
    out = derive_personas(prov, {"甲": ["甲在这里"], "乙": ["乙在那里"]})
    assert prov.calls == 1
    assert set(out) == {"甲", "乙"}


def test_片段取名字周围的窗口():
    text = "开头。" * 50 + "顾星河握住灯钳。" + "结尾。" * 50
    snips = character_snippets(text, "顾星河", limit=3, window=20)
    assert snips and "顾星河" in snips[0]


# ====================================================================
# 3. 阶段化进度：不提前 100%
# ====================================================================

def test_进度按阶段推进且只有最后才100():
    from app.style import narrative as nv

    assert nv._DISTILL_TOTAL_PHASES == 6
    phases = [nv.PH_PREPARE, nv.PH_SAMPLE, nv.PH_EXTRACT,
              nv.PH_VOICE, nv.PH_L2, nv.PH_SUMMARY]
    assert phases == [1, 2, 3, 4, 5, 6], "阶段序号必须连续"
    # 每一个「开始某阶段」上报的都是 current = 阶段序号 - 1 ⇒ 只有走完第 6 阶段才是 100%
    percents = [round(100 * (p - 1) / nv._DISTILL_TOTAL_PHASES) for p in phases]
    assert percents[0] == 0
    assert percents[-1] < 100, "最后一个阶段的起点不能是 100%"
    assert percents == sorted(percents), "百分比必须单调"


# ====================================================================
# 4. 落库：persona 必须真的写进库（JSON 列标脏那个坑）
# ====================================================================

@pytest.fixture()
def mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    import app.models  # noqa: F401

    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    try:
        yield db, Session
    finally:
        db.close()


def _seed_project(db, *, with_persona: bool = False):
    from app.models import Generation, Project

    persona = {"说话方式": "句短，只给结论", "来源": "model"} if with_persona else None
    char = {"name": "顾星河", "role": "执灯人", "motivation": "查明真相"}
    if persona:
        char["persona"] = persona
    p = Project(id=1, title="t", generation_length=3000,
                plot_bible={"characters": [char], "synopsis": "s", "direction": "d"})
    db.add(p)
    db.add(Generation(project_id=1, segment_no=1,
                      text="顾星河说：「灯还亮着。」他把灯钳搁回铁架。"))
    db.commit()
    return p


def test_refresh_personas_真的写进库(mem_session):
    """回写必须**换一条连接**读得到。

    2026-10-28 踩过：`plot_bible` 是 `Column(JSON)`，赋值新对象后
    SQLAlchemy 的变更检测不一定认得出它变了 ⇒ commit 静默不写，
    而函数返回 changed>0 ⇒ 调用方以为「更新了 N 个角色」，
    下一段的提示词里一个 persona 都没有。必须用 `flag_modified` 显式标脏。
    """
    from app.generation.persona import refresh_personas

    db, Session = mem_session
    _seed_project(db)

    class _Prov:
        def chat(self, system, user, constraints=None):
            return ('{"characters": [{"name": "顾星河", "说话方式": "句短，只给结论",'
                    ' "行为倾向": ["被问话时→先做完手里的事再答"]}]}')

    n = refresh_personas(db, db.query(__import__("app.models", fromlist=["Project"]).Project).first(),
                         _Prov(), max_segments=6)
    assert n == 1
    db.commit()

    # —— 换一条连接读（模拟「下一段生成时的另一个 session」）——
    other = Session()
    try:
        p2 = other.query(__import__("app.models", fromlist=["Project"]).Project).first()
        persona = (p2.plot_bible["characters"][0] or {}).get("persona") or {}
        assert persona.get("说话方式") == "句短，只给结论", \
            "persona 没有真的落库（JSON 列没标脏 ⟹ commit 静默不写）"
        assert persona.get("来源") == "derived"
    finally:
        other.close()


def test_refresh_personas_不动用户自己写的(mem_session):
    """`来源=user` 的 persona 一个字都不动（用户写的权威）。"""
    from app.generation.persona import refresh_personas
    from app.models import Project

    db, Session = mem_session
    p = _seed_project(db, with_persona=True)
    p.plot_bible["characters"][0]["persona"]["来源"] = "user"
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(p, "plot_bible")
    db.commit()

    class _Prov:
        def chat(self, system, user, constraints=None):
            return '{"characters": [{"name": "顾星河", "说话方式": "被改掉了"}]}'

    n = refresh_personas(db, db.query(Project).first(), _Prov(), max_segments=6)
    assert n == 0
    other = Session()
    try:
        p2 = other.query(Project).first()
        assert p2.plot_bible["characters"][0]["persona"]["说话方式"] == "句短，只给结论"
    finally:
        other.close()


# ====================================================================
# 5. 人写角色 + AI 帮着完善（2026-10-28）
# ====================================================================
#
# 用户的诉求：「让用户可以自己写角色的性格，自己来决定角色应该有什么样的表现，
# 再由 ai 来帮忙完善」。
#
# 三条承诺，各有测试钉住：
#   1. 人写的内容**永远保留**（AI 只能追加）；
#   2. 人写的内容**不被反推覆盖**（来源=user 受保护）；
#   3. AI **不落库**（它给的是建议）。

def test_合并_用户原文在前_AI只追加():
    from app.generation.persona import merge_persona

    user = {"说话方式": "句子短，从不解释", "性格底线": ["不会当众示弱"]}
    ai = {"说话方式": "不解释，只给结论", "行为倾向": ["被追问时→先翻原件再答"]}
    merged = merge_persona(user, ai)
    assert merged["说话方式"].startswith("句子短，从不解释"), "用户原文必须保留"
    assert "不解释，只给结论" in merged["说话方式"]
    assert merged["性格底线"] == ["不会当众示弱"], "用户没写的字段才用 AI 的"
    assert merged["行为倾向"] == ["被追问时→先翻原件再答"]
    # 列表字段：用户写的在前，AI 的追加在后
    merged2 = merge_persona({"行为倾向": ["A"]}, {"行为倾向": ["A", "B"]})
    assert merged2["行为倾向"] == ["A", "B"], "AI 不该把用户写的重复一遍，也不该改顺序"


def test_完善_只给建议不落库():
    from app.generation.persona import complete_persona

    class _Prov:
        def chat(self, system, user, constraints=None):
            assert "补充" in system, "提示必须要求模型输出「补充」而不是「改写」"
            assert "保留" in system, "提示必须强调保留用户写的内容"
            return ('{"补充": {"说话方式": "补：语气更冷"}, '
                    '"说明": "补了语气"}')

    char = {"name": "顾星河", "role": "执灯人", "motivation": "查明真相"}
    out = complete_persona(_Prov(), char, {"说话方式": "句子短"}, "")
    assert out and out["补充"].get("说话方式") == "补：语气更冷"
    assert out["说明"] == "补了语气"


def test_完善_编造的引文会被丢掉():
    from app.generation.persona import complete_persona

    class _Prov:
        def chat(self, system, user, constraints=None):
            return ('{"补充": {"说话方式": "常说「你这蠢货」"}, "说明": "x"}')

    char = {"name": "顾星河", "role": "执灯人", "motivation": "查明真相"}
    out = complete_persona(_Prov(), char, {}, "")
    assert not out or not out["补充"], "正文里没有的引文不能进建议"


def test_保存用户的个性_标来源user且不被反推覆盖(mem_session):
    from app.generation.persona import save_persona
    from app.models import Project

    db, Session = mem_session
    p = _seed_project(db)
    saved = save_persona(db, p, "顾星河", {"说话方式": "我写的：只用短句"})
    assert saved.get("说话方式") == "我写的：只用短句"
    assert saved.get("来源") == "user"

    # 换一条连接读 ⇒ 真的落库了
    other = Session()
    try:
        p2 = other.query(Project).first()
        persona = p2.plot_bible["characters"][0].get("persona") or {}
        assert persona.get("说话方式") == "我写的：只用短句"
        assert persona.get("来源") == "user"
    finally:
        other.close()


def test_用户写的个性不被反推覆盖(mem_session):
    """`来源=user` ⇒ 反推**不碰**它（用户的决定权最大）。"""
    from app.generation.persona import refresh_personas
    from app.models import Project

    db, Session = mem_session
    p = _seed_project(db)
    from sqlalchemy.orm.attributes import flag_modified

    p.plot_bible["characters"][0]["persona"] = {
        "说话方式": "我写的：只用短句", "来源": "user"}
    flag_modified(p, "plot_bible")
    db.commit()

    class _Prov:
        def chat(self, system, user, constraints=None):
            raise AssertionError("用户写过就不该再调模型")

    n = refresh_personas(db, db.query(Project).first(), _Prov(), max_segments=6)
    assert n == 0
    other = Session()
    try:
        p2 = other.query(Project).first()
        persona = p2.plot_bible["characters"][0].get("persona") or {}
        assert persona.get("说话方式") == "我写的：只用短句"
    finally:
        other.close()


def test_合并_去掉AI对用户原文的重述():
    """AI 常把用户那句原样重述一遍再往下写 —— 合并时要去掉重复。

    实测（2026-10-28）：用户写「句子短，从不解释」，
    AI 回了「句子短，从不解释；不用感叹句，音量比平常低」，
    直接拼接会变成「句子短，从不解释；句子短，从不解释；不用感叹句…」。
    """
    from app.generation.persona import merge_persona

    merged = merge_persona(
        {"说话方式": "句子短，从不解释"},
        {"说话方式": "句子短，从不解释；不用感叹句，音量比平常低"},
    )
    assert merged["说话方式"] == "句子短，从不解释；不用感叹句，音量比平常低"
    assert merged["说话方式"].count("句子短") == 1
