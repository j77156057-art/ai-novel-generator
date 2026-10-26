"""角色对话：证据来源标注、写法样板、档案裁剪、角色指定。

这些规则以前**全是死的**，真跑才暴露：
- `character_filtered` 从来没被设过 ⇒ 角色路径的写法样板恒为空；
- `PROFILE_RULES["roleplay"]["narrative_dims"] = ()` 全仓没人读 ⇒ 死规则；
- `_roleplay_character_name` 只查 plot_bible ⇒ 界面上的角色（图谱抽出来的）取不到；
- 角色声音词典不按角色筛 ⇒ 几个人的腔调混在一起。
"""
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.generation.engine import _roleplay_character_name  # noqa: E402
from app.roleplay.evidence import classify_origin  # noqa: E402
from app.style.exemplar import exemplars_from_character_hits  # noqa: E402


# ── 来源标注 ─────────────────────────────────────────────────────────────
def test_origin_speaking_is_firsthand():
    """他自己开过口 ⇒ 亲历。"""
    text = "因为在马厩躺了会，头上粘上几根马草的老黄挠挠头道：“剑匣三层六格。”"
    origin, basis = classify_origin(text, "老黄")
    assert origin == "亲历", basis


def test_origin_others_talking_to_him_is_heard():
    """别人在跟他说 ⇒ 他在场听到，不是亲历。"""
    text = "手没闲着拿了根黄瓜的徐凤年苦笑道：“老黄，你给我说说，这剑匣里有几把剑？”"
    origin, _basis = classify_origin(text, "老黄")
    assert origin == "在场听到"


def test_origin_narration_only_is_heard():
    """叙述里提到他、但没有他的对白 ⇒ 在场听到。"""
    text = "老黄想了想，一脸为难道：“少爷，我不懂这些。”"
    # 这句他其实开了口，但名字与说话动词之间隔了逗号 —— 保守判成「在场听到」
    origin, _basis = classify_origin(text, "老黄")
    assert origin in ("亲历", "在场听到")


def test_origin_conservative_direction():
    """漏判的方向必须是**保守**的：不会把别人的话算成他的记忆。"""
    text = "徐凤年站在城门前，看着老黄离去的背影，想起了很多事。"
    origin, _basis = classify_origin(text, "老黄")
    assert origin == "在场听到"


# ── 写法样板：从角色检索命中的块里取他的台词 ───────────────────────────
def test_exemplars_from_character_hits():
    hits = [{
        "text": "手没闲着拿了根黄瓜的徐凤年苦笑道：“老黄，你给我说说，这剑匣里有几把剑？”\n"
                "因为在马厩躺了会的老黄挠挠头道：“剑匣三层六格，原先有天下十大名剑里的六把。”\n"
                "徐凤年无言以对。\n"
                "老黄憨憨道：“若少爷想要耍剑，俺留下三四把便是。”\n"
                "徐凤年摇头道：“不了，少爷巴不得你背上百八十把剑。”",
        "chunk_id": 1, "chapter_no": 14, "idx": 0,
    }]
    ex = exemplars_from_character_hits(hits, "老黄")
    assert len(ex) == 1
    assert ex[0]["character_filtered"] is True
    assert "老黄挠挠头道" in ex[0]["text"]


def test_exemplars_skip_blocks_without_him():
    """块里没有他（他被别称代称）⇒ 不做样板。"""
    hits = [{
        "text": "徐凤年笑了一声，看向那位缺胳膊的老马夫。",
        "chunk_id": 2, "chapter_no": 20, "idx": 0,
    }]
    assert exemplars_from_character_hits(hits, "老黄") == []


# ── 档案裁剪（死规则复活） ──────────────────────────────────────────────
def _profile_with_voice():
    return {
        "rules": {
            "对白风格": [{"rule": "短句多含轻佻戏谑，长句多用于正式议事"}],
            "伏笔与悬念设计": [{"rule": "悬念先用异常细节埋设"}],
            "角色声音词典": [
                {"rule": "老黄：自称多省略，惯用点头、咧嘴和憨笑"},
                {"rule": "徐凤年：自称「本世子」，语气自得"},
            ],
        },
        "forbidden": {},
    }


def test_dims_empty_removes_method_dimensions():
    from app.style.narrative import describe_narrative

    body = describe_narrative(_profile_with_voice(), dims=(), voice_character="老黄")
    assert "对白风格" not in body
    assert "伏笔" not in body


def test_voice_character_filters_voice_dictionary():
    """只留被扮演的那一个角色的说话方式。

    注意：渲染时**角色名会被剥掉**（参考书的人名不能进用户的小说），
    所以断言的是各自的**腔调特征**，不是名字。
    """
    from app.style.narrative import describe_narrative

    body = describe_narrative(_profile_with_voice(), dims=(), voice_character="老黄")
    assert "惯用点头、咧嘴和憨笑" in body, body
    assert "本世子" not in body, body


def test_voice_character_none_keeps_all():
    from app.style.narrative import describe_narrative

    body = describe_narrative(_profile_with_voice(), dims=(), voice_character=None)
    assert "本世子" in body


# ── 角色指定 ─────────────────────────────────────────────────────────────
class _P:
    def __init__(self, bible=None):
        self.plot_bible = bible or {}
        self.id = 1


def test_roleplay_character_from_plot_bible():
    assert _roleplay_character_name(_P({"roleplay_character": "老黄"})) == "老黄"


def test_roleplay_character_falls_back_to_first_bible_character():
    assert _roleplay_character_name(_P({"characters": [{"name": "徐凤年"}]})) == "徐凤年"


def test_roleplay_character_falls_back_to_graph_characters():
    """界面上的角色来自**图谱抽取**，不在 plot_bible 里 —— 以前这里取不到人。

    取不到 ⇒ 角色定向检索压根没跑（实测 rag_items 0 条）⇒ 角色「什么都不知道」。
    """
    rem = {"character_constraints": [{"name": "徐凤年"}, {"name": "老黄"}]}
    assert _roleplay_character_name(_P({}), rem) == "徐凤年"


def test_roleplay_character_none_when_nothing():
    assert _roleplay_character_name(_P({}), {}) is None


# ── 没配模型时的「答非所问」要拦下来 ─────────────────────────────────────
def test_offline_fallback_answer_is_replaced_with_the_truth():
    """空白机（没填 key）上第一眼看到的就是兜底输出 —— 不能让角色念一段旁白。

    兜底 provider 回的是「【离线兜底·确定性续写】……续写正文占位」，
    那是对**正文生成**说的话；放进角色对话等于答非所问。
    """
    from app.llm.provider import LocalFallbackLLMProvider
    from app.roleplay.chat import finalize_answer

    fallback = LocalFallbackLLMProvider()
    raw = fallback.chat("system", "user")
    assert "续写正文占位" in raw, "前提：兜底确实是这么回的"

    out = finalize_answer(fallback, raw)
    assert "续写正文占位" not in out, out
    assert "还没有配置模型" in out, out


def test_normal_provider_answer_is_kept_as_is():
    """真模型给的回答原样保留（只做去空白与思考块剥离）。"""
    from app.roleplay.chat import finalize_answer

    class _Fake:
        pass

    out = finalize_answer(_Fake(), "  他想了想：“少爷那时候胖得像年糕。”  ")
    assert out == "他想了想：“少爷那时候胖得像年糕。”", out

