"""角色卡 + 声纹 + 台词归属（2026-10-29）。

## 背景（发布版冒烟库真实数据）

用户提了两个需求：

> 「新建角色时 AI 自动添加性格、行为、关系、口头禅……但注意，生成的不要千篇一律，
>  得有各自的性格，不能出现几个人说话方式差不多的情况」
> 「似乎没有记录角色使用的武器、使用的功法、有什么能力……要不要做一个角色卡」

**取证结论**：

1. `kg_entities.attrs` 里 38 个 character **每个都有** `arc/current_goal/secrets`，
   **没有一个**有武器/功法/能力 ⇒ 角色卡确实缺。
2. 「说话方式差不多」是真的，而且**写了 persona 的项目更严重**：

   | 项目 | 有 persona | 角色 | 风格向量平均距离 | 最近一对 |
   |---|---|---|---|---|
   | 5 | 有 | 5 | 0.230 | **0.037** |
   | 1 | 无 | 8 | 0.399 | 0.138 |

   五个角色平均台词长度全挤在 7.0~8.6 字 ⇒ **光写「沉稳寡言」不够，
   必须写可执行锚点（句长档位/句末习惯/自称/口头禅）才比得出差别**。
3. **台词归属污染**：反推用的是「名字周围 180 字窗口」⇒ 混进别人的台词。
   实测白烬的 persona 把「沉日者。」「你迟了。」记成自己的口头禅，
   而那两句是沉日者的 —— 因为该行同时出现两个名字。

## 本文件钉住的口径

- **归属**：一行里出现 ≥2 个角色名 ⇒ 归属不明 ⇒ 不算任何人的台词（宁可少）。
- **角色卡**：身份 / 能力（武器·功法·能力·能力上限）/ 声纹 / 关系。
- **声纹必须是锚点**，不是形容词；两个角色锚点撞 ≥3 项 ⇒ 判「撞车」。
- **都自称「我」不算撞**（中文里绝大多数人自称我，没有区分度）。
- **批量生成互相避让**：把已有声纹喂给模型 + 撞了就重写。
- **抽卡有出处**：武器/功法/能力的名称必须能在正文里查到，否则删掉。
- **人写优先**：用户保存的卡标 `来源=user`，AI 只补空、不推翻。
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

from app.generation.character_card import (  # noqa: E402
    LENGTH_BANDS,
    normalize_card,
    render_card,
    render_cast,
    voice_collisions,
    voice_signature,
)
from app.generation.character_card_gen import (  # noqa: E402
    _collide,
    _ground_card,
    merge_card,
)
from app.generation.persona import (  # noqa: E402
    attributed_lines,
    character_snippets,
)


# ======================================================================
# 1. 台词归属：一行两个名字 ⇒ 不算任何人的
# ======================================================================

def test_行内出现别的角色名就不算他的台词():
    """实测事故：白烬把沉日者的「沉日者。」记成自己的口头禅。"""
    text = "\n".join([
        "白烬抬头。「沉日者。」",
        "「你迟了。」",
        "白烬往前一步。「你记得自己的脸吗？」",
    ])
    lines = attributed_lines(text, "白烬", other_names=["白烬", "沉日者"])
    # 第一行同时有白烬和沉日者 ⇒ 归属不明 ⇒ 丢掉
    assert all("沉日者" not in ln for ln in lines), lines
    # 第三行只有白烬 ⇒ 留下
    assert any("你记得自己的脸吗" in ln for ln in lines), lines


def test_独占的行_全部保留():
    text = "白烬说：「让死者有名字。」\n别人在别处说话。"
    lines = attributed_lines(text, "白烬", other_names=["白烬", "沉日者"])
    assert lines == ["白烬说：「让死者有名字。」"], lines


def test_没传other_names时退化成旧行为():
    """向后兼容：不传就不过滤（老调用点不会突然全空）。"""
    text = "\n".join(["白烬抬头。「沉日者。」"])
    snips = character_snippets(text, "白烬", limit=3)
    assert snips and "白烬" in snips[0]


def test_传了other_names就过滤():
    text = "\n".join(["白烬抬头。「沉日者。」", "白烬说：「走吧。」"])
    snips = character_snippets(text, "白烬", other_names=["白烬", "沉日者"])
    assert len(snips) == 1 and "走吧" in snips[0], snips


# ======================================================================
# 2. 角色卡：结构规整与渲染
# ======================================================================

def test_能力组收三种写法():
    card = normalize_card({
        "武器": ["断刀：锈迹斑斑", {"名称": "短匕", "说明": "藏在靴筒"}],
        "功法": "换气",
        "能力": ["听声辨位"],
        "能力上限": ["不会水"],
    })
    ab = card["能力"]
    assert ab["武器"][0] == {"名称": "断刀", "说明": "锈迹斑斑"}, ab["武器"]
    assert ab["武器"][1] == {"名称": "短匕", "说明": "藏在靴筒"}
    assert ab["功法"] == [{"名称": "换气", "说明": ""}]
    assert ab["能力"][0]["名称"] == "听声辨位"
    assert ab["能力上限"] == ["不会水"]


def test_渲染出武器与能力上限():
    card = normalize_card({
        "性别": "男", "身份职业": "守城士兵",
        "武器": ["断刀"], "能力上限": ["不会水"],
        "自称": "我", "句长": "短",
    })
    text = render_card(card)
    assert "断刀" in text
    assert "做不到" in text and "不会水" in text
    assert "句长短" in text


def test_渲染多角色时明确要求拉开距离():
    cards = {
        "甲": normalize_card({"句长": "短", "句末": "直陈收尾（不用语气词）"}),
        "乙": normalize_card({"句长": "长", "句末": "常以反问收尾"}),
    }
    text = render_cast(cards, ["甲", "乙"])
    assert "甲" in text and "乙" in text
    assert "彼此不同" in text


def test_单角色不加拉开距离的提醒():
    cards = {"甲": normalize_card({"句长": "短"})}
    text = render_cast(cards, ["甲"])
    assert "彼此不同" not in text


# ======================================================================
# 3. 声纹撞车检测（「不千篇一律」的守门）
# ======================================================================

def test_锚点撞三项就报撞车():
    cards = {
        "甲": normalize_card({"自称": "在下", "句长": "短",
                              "句末": "直陈收尾（不用语气词）", "语气底色": "压着说话"}),
        "乙": normalize_card({"自称": "在下", "句长": "短",
                              "句末": "直陈收尾（不用语气词）", "语气底色": "压着说话"}),
    }
    col = voice_collisions(cards)
    assert len(col) == 1, col
    assert set(col[0]["人物"]) == {"甲", "乙"}
    assert len(col[0]["相同"]) >= 3


def test_都自称我不算撞车():
    """中文里绝大多数人自称「我」—— 把它当相同项会把所有人都判成撞车。"""
    cards = {
        "甲": normalize_card({"自称": "我", "句长": "短", "句末": "常以反问收尾"}),
        "乙": normalize_card({"自称": "我", "句长": "长", "句末": "直陈收尾（不用语气词）"}),
    }
    assert voice_collisions(cards) == []


def test_拉开距离后不报撞车():
    cards = {
        "甲": normalize_card({"自称": "在下", "句长": "短",
                              "句末": "直陈收尾（不用语气词）"}),
        "乙": normalize_card({"自称": "小的", "句长": "长",
                              "句末": "常以反问收尾"}),
    }
    assert voice_collisions(cards) == []


def test_声纹签名只取锚点():
    sig = voice_signature(normalize_card({
        "自称": "在下", "句长": "短", "句末": "常以反问收尾",
        "语气底色": "压着说话", "口头禅": ["活下去", "别问"],
        "外貌": "左手有疤",
    }))
    assert sig["自称"] == "在下"
    assert sig["口头禅数"] == 2
    assert "外貌" not in sig  # 外貌不是声纹


def test_句长档位互不重叠():
    """档位必须分得开，否则「换个档」等于没换。"""
    bands = sorted(LENGTH_BANDS.values())
    for (a_lo, a_hi), (b_lo, b_hi) in zip(bands, bands[1:]):
        assert a_hi < b_lo, (a_lo, a_hi, b_lo, b_hi)


# ======================================================================
# 4. 人写优先：AI 只补空、不推翻
# ======================================================================

def test_合并时用户写的字段不被覆盖():
    user = {"身份": {"性别": "男", "身份职业": "守城士兵"},
            "声纹": {"自称": "我", "句长": "短"}}
    ai = {"身份": {"性别": "女", "身份职业": "商贩", "外貌": "瘦高"},
          "声纹": {"自称": "我", "句长": "长", "句末": "常以反问收尾"}}
    merged = merge_card(user, ai)
    assert merged["身份"]["性别"] == "男", merged["身份"]
    assert merged["身份"]["身份职业"] == "守城士兵"
    assert merged["身份"]["外貌"] == "瘦高"      # 用户没写的，AI 补上
    assert merged["声纹"]["句长"] == "短"        # 用户写的，不动
    assert merged["声纹"]["句末"] == "常以反问收尾"


# ======================================================================
# 5. 抽卡要有出处（不许凭空造武器）
# ======================================================================

def test_正文查不到的武器被删掉():
    card = {"能力": {"武器": [
        {"名称": "断刀", "说明": "锈迹斑斑"},
        {"名称": "青龙偃月刀", "说明": "编的"},
    ]}}
    out = _ground_card(card, "沈砚手里握着断刀，刀身有暗红纹路。")
    names = [it["名称"] for it in out["能力"]["武器"]]
    assert names == ["断刀"], names


def test_量词前缀也认():
    card = {"能力": {"武器": [{"名称": "断刀", "说明": ""}]}}
    out = _ground_card(card, "他抱起那把断刀就往水门跑。")
    assert out["能力"]["武器"], out


def test_正文查不到的口头禅被删掉():
    card = {"声纹": {"口头禅": ["活下去", "我要成仙"]}}
    out = _ground_card(card, "他说：「活下去。」")
    assert out["声纹"]["口头禅"] == ["活下去"], out


def test_能力全无依据就整组去掉():
    card = {"能力": {"武器": [{"名称": "不存在的剑", "说明": ""}]},
            "声纹": {"句长": "短"}}
    out = _ground_card(card, "他在雨里走了一夜。")
    assert "能力" not in out, out
    assert out["声纹"]["句长"] == "短"


# ======================================================================
# 6. 批量生成的避让逻辑（冲突重写）
# ======================================================================

def test_碰撞检测与冲突检测同口径():
    a = {"自称": "在下", "句长": "短", "句末": "常以反问收尾"}
    b = {"自称": "在下", "句长": "短", "句末": "常以反问收尾"}
    hit = _collide(a, b)
    assert len(hit) == 3, hit


def test_生成器在没有provider时安全返回空():
    from app.generation.character_card_gen import generate_cards

    out = generate_cards(None, {"title": "测试"}, ["甲"])
    assert out["角色"] == {}


def test_生成器在没有任何角色时安全返回空():
    from app.generation.character_card_gen import generate_cards

    out = generate_cards(object(), {"title": "测试"}, [])
    assert out["角色"] == {}
