"""风格样例这条通道：能不能真的把「怎么写」教给模型。

## 为什么要有这组测试

生成文本与参考书的**句子长度差 4~5 倍**（参考书 900 字 10 句，生成 900 字 45 句），
是这套系统当前最大的文体偏差。逐个排除下来：

- 提示里给了数字（句长 56±18.8 字）→ 模型不照做（实测 10.9 字/句）；
- 叙事规则块 → 三变体实验证明**不是**它造成的（去掉几乎不变）；
- 给了原文但**埋得浅、口径弱**（2×600 字、弱措辞、放在中间）→ 也几乎不动；
- 给了原文**加长 + 放在提示末尾 + 明说「照它的句法写」** → 句号密度 z 从 +11.8 降到 +8.3。

所以这一组测试钉住的是**最后一行的三个条件**，别让它们被改回去：

1. 样例**够长**（≥700 字，200~600 字的样例看不出「句子怎么呼吸」）；
2. 样例取自**成熟期**（早期章节文风还没稳定）；
3. 样例块在 user 提示的**最末**，口径写明「照着写」；
4. 写法参考路径（原创）下参考书**整本可见** —— 否则样例通道从来没通过过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


def _db():
    from app.db import Base
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _novel(db, nid=1, chapters=20, paras_per_chunk=8):
    from app.models import NovelChunk, RawNovel
    db.add(RawNovel(id=nid, title="样本语料", genre="仙侠"))
    base = "他抬起手，剑锋在月光下闪过一道寒光。她退后一步，眉头微皱。"
    for i in range(chapters):
        paras = [base * (j + 1) for j in range(paras_per_chunk)]
        db.add(NovelChunk(novel_id=nid, idx=i, chapter_no=i + 1, text="\n".join(paras)))
    db.commit()


# ====================================================================
# 1) 成熟期优先
# ====================================================================

def test_mature_pool_keeps_the_later_half_of_the_chapters():
    from app.style.exemplar import _mature_pool
    pool = [(ch, 0, ch, f"样例{ch}", {}) for ch in range(1, 21)]
    kept = _mature_pool(pool)
    chapters = sorted({p[0] for p in kept})
    assert chapters, "成熟期不该为空"
    assert min(chapters) > 10, chapters


def test_mature_pool_does_not_empty_a_tiny_corpus():
    """只有一两章时过滤会把候选清空 —— 那等于「没有样例」，比早期样例更糟。"""
    from app.style.exemplar import _mature_pool
    pool = [(1, 0, 1, "样例", {})]
    assert _mature_pool(pool) == pool
    pool2 = [(1, 0, 1, "a", {}), (2, 0, 2, "b", {})]
    assert _mature_pool(pool2) == pool2


def test_sampled_exemplars_come_from_the_mature_period():
    from app.models import Project
    from app.style.exemplar import sample_style_exemplars
    db = _db()
    _novel(db)
    project = Project(id=1, title="测试", genre="仙侠")
    db.add(project)
    db.commit()
    exemplars = sample_style_exemplars(db, project, [1])
    assert exemplars
    chapters = [ex["chapter_no"] for ex in exemplars]
    assert min(chapters) > 10, chapters


# ====================================================================
# 2) 样例要够长
# ====================================================================

def test_samples_are_long_enough_to_show_the_syntax():
    """200~600 字的样例看不出「句子怎么呼吸」；实测 600 字以内几乎不改变句法。

    口径是**目标长度**而不是硬性下限：典型语料（chunk ≈1200 字）必须挑到 ≥700 字的样例；
    短语料挑不到够长的，就给短的 —— 宁可短也别一个样例都没有。
    """
    from app.models import Project
    from app.style.exemplar import (
        MAX_SAMPLE_CHARS,
        MIN_SAMPLE_CHARS,
        TARGET_SAMPLE_CHARS,
        sample_style_exemplars,
    )
    assert TARGET_SAMPLE_CHARS >= 700, TARGET_SAMPLE_CHARS
    assert MAX_SAMPLE_CHARS >= 1200, MAX_SAMPLE_CHARS
    assert MIN_SAMPLE_CHARS <= TARGET_SAMPLE_CHARS

    db = _db()
    _novel(db, chapters=20, paras_per_chunk=40)
    project = Project(id=1, title="测试", genre="仙侠")
    db.add(project)
    db.commit()
    exemplars = sample_style_exemplars(db, project, [1])
    assert exemplars
    for ex in exemplars:
        assert len(ex["text"]) >= TARGET_SAMPLE_CHARS, len(ex["text"])


def test_a_short_corpus_still_gets_exemplars():
    """短语料（chunk ≈280 字）挑不到 700 字的样例 —— 仍然要给样例，不能一个都没有。"""
    from app.models import Project
    from app.style.exemplar import sample_style_exemplars
    db = _db()
    _novel(db, chapters=4, paras_per_chunk=4)
    project = Project(id=1, title="测试", genre="仙侠")
    db.add(project)
    db.commit()
    exemplars = sample_style_exemplars(db, project, [1])
    assert exemplars
    assert all(ex["text"] for ex in exemplars)


# ====================================================================
# 3) 样例块在提示末尾，口径写明「照着写」
# ====================================================================

def _user_prompt(exemplars):
    from app.generation.prompts import build_user_prompt
    return build_user_prompt(
        [], [], None, 2000, None, "测试基调",
        style_exemplars=exemplars,
    )


def test_exemplar_block_is_the_last_block_of_the_user_prompt():
    from app.generation.prompts import build_user_prompt
    ex = [{"novel_id": 1, "novel_title": "样本", "text": "他抬起手，剑锋在月光下闪过一道寒光。"}]
    prompt = build_user_prompt([], [], None, 2000, None, "基调", style_exemplars=ex)
    assert "【句法样板" in prompt
    assert prompt.index("【续写指令】") < prompt.index("【句法样板")
    # 样板是最后一块：它后面只剩样板正文
    tail = prompt.split("【句法样板")[-1]
    assert "【续写指令】" not in tail


def test_exemplar_wording_asks_the_model_to_copy_the_syntax():
    from app.generation.prompts import build_user_prompt
    ex = [{"novel_id": 1, "novel_title": "样本", "text": "他抬起手，剑锋在月光下闪过一道寒光。"}]
    prompt = build_user_prompt([], [], None, 2000, None, "基调", style_exemplars=ex)
    assert "照着写" in prompt
    assert "句子长度" in prompt
    assert "不要复用" in prompt


def test_no_exemplars_means_no_block():
    from app.generation.prompts import build_user_prompt
    prompt = build_user_prompt([], [], None, 2000, None, "基调", style_exemplars=None)
    assert "【句法样板" not in prompt


# ====================================================================
# 4) 写法参考路径下参考书整本可见
# ====================================================================

class _Proj:
    story_authority = "generated_branch"
    reference_cutoffs = None
    genre = None


def test_without_a_boundary_the_whole_book_is_hidden_by_default():
    from app.generation.visibility import VisibilityPolicy
    pol = VisibilityPolicy(_Proj(), [1])
    assert pol.reason(1, 30) == "missing_cutoff"


def test_the_writing_reference_path_may_read_the_whole_book():
    """原创路径：参考书是学写法的，不存在「故事后续」要防。"""
    from app.generation.visibility import VisibilityPolicy
    pol = VisibilityPolicy(_Proj(), [1], unbounded_paths=frozenset({"original"}))
    assert pol.reason(1, 30) is None
    # 章号仍得是真的
    assert pol.reason(1, 0) == "missing_source_chapter"


def test_an_explicit_boundary_always_wins():
    """用户显式设了边界就按边界走，哪怕在写法参考路径里。"""
    class _P(_Proj):
        reference_cutoffs = [{"novel_id": 1, "chapter": 10}]

    from app.generation.visibility import VisibilityPolicy
    pol = VisibilityPolicy(_P(), [1], unbounded_paths=frozenset({"original"}))
    assert pol.reason(1, 10) is None
    assert pol.reason(1, 11) == "future_source"


def test_only_the_original_path_is_unbounded():
    from app.generation.profiles import PROFILES, STYLE_REFERENCE_PATHS
    assert STYLE_REFERENCE_PATHS == frozenset({"original"})
    assert "continuation" not in STYLE_REFERENCE_PATHS
    assert "roleplay" not in STYLE_REFERENCE_PATHS
    assert set(STYLE_REFERENCE_PATHS) <= set(PROFILES)
