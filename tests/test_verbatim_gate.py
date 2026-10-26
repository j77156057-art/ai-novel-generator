"""正文级照搬门禁（app/generation/verbatim.py）。

**尺必须先自证** —— 这是本文件的第一条规矩：
断言「没抄」之前，先证明这把尺在**真有照搬**时会响。
（2026-10-26 踩过：第一版尺全报 0，是阳性对照把它坏的揪出来的 ——
 未归一化空白 + 连续 n-gram 跨位置误拼接。）
"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from app.db import Base
from app.models import NovelChunk, Project

# 一段够长的参考原文（要 > 阈值，否则测不出东西）
CORPUS = (
    '青石城的东门临着官道，守卒每日卯时换岗，铁甲摩擦声隔着半条街都能听见。'
    '卖药的老头挑着空担子从城门进来，扁担两头挂着褪色的红布，脚下不沾泥。'
    '少年抱着父亲的断刀站在廊下，看着那把刀出了神，连檐上的雨落进瓦缝都没察觉。'
    '老头在井边歇脚，扁担搁在膝头，忽然开口问他：你是哪家的孩子。'
    '少年没答，只把断刀往怀里收了收。老头也不追问，舀了半瓢水慢慢喝着。'
)
THRESHOLD = 25


def _index(corpus=CORPUS):
    from app.generation.verbatim import VerbatimIndex
    return VerbatimIndex.from_chunks([corpus])


# —— 尺的自证（阳性对照）————————————————————————————————

def test_positive_control_corpus_against_itself_must_hit():
    """语料自己 vs 语料 ⇒ 必须命中，且长度 = 整段。

    这条不通过，下面所有「没抄」的断言都没有意义。
    """
    idx = _index()
    spans = idx.find_spans(CORPUS, threshold=THRESHOLD)
    assert spans, "尺坏了：语料跟自己都没测出重合"
    assert max(s['length'] for s in spans) >= len(CORPUS) - 5


def test_positive_control_whitespace_is_normalized():
    """空白不同也要命中 —— 语料是清洗过的，生成文本有自己的换行缩进。

    不归一化会**系统性漏报**（实测：第一版尺全报 0 就是这么来的）。
    """
    idx = _index()
    noisy = '\n\n'.join(CORPUS[i:i + 9] for i in range(0, len(CORPUS), 9))
    noisy = noisy.replace('青', ' 青 ')
    spans = idx.find_spans(noisy, threshold=THRESHOLD)
    assert spans, "空白形态不同就漏报了 —— 尺没归一化"


def test_paraphrase_must_not_hit():
    """换了措辞的同义内容 ⇒ **不得**命中（否则门槛会堵死真活）。"""
    idx = _index()
    para = (
        '青石城的东门外就是官道，守门的兵每天天亮就换岗，铁甲互相碰撞的声音'
        '隔着半条街都能听见。挑着空担子的卖药老汉从城里进来，扁担两头挂着'
        '褪了色的红布，鞋底一点泥也没有。少年把父亲的断刀抱在怀里，站在廊下'
        '出了神，连屋檐上的雨水落进瓦缝里都没察觉。老汉在井沿歇脚，把扁担'
        '横在膝盖上，忽然问他：你是谁家的孩子。少年没有回答，只把断刀往怀里'
        '又收了收。老汉也不追着问，舀了半瓢水慢慢喝着。'
    )
    assert idx.find_spans(para, threshold=THRESHOLD) == []


# —— 阈值边界 ————————————————————————————————————————————

def test_threshold_boundary_24_is_clean_25_is_copy():
    """24 字不拦、25 字拦 —— 阈值要卡得准。"""
    idx = _index()
    piece = CORPUS[10:10 + 25]
    assert len(piece) == 25
    assert idx.find_spans(piece, threshold=25), "25 字整段没命中"
    assert idx.find_spans(piece[:24], threshold=25) == [], "24 字被判成照搬了"


def test_only_the_longest_common_run_is_reported():
    """命中的应是**连续**那一段，而不是把 n-gram 命中拼起来的长度。

    注意：报告的长度可能比正文里嵌的那段**多几个字** ——
    延伸是往两侧逐字比的，语料里紧邻的字符恰好与正文相邻字符相同就并进去了。
    所以断言用「包含 + 真在语料里」，不用死等长度。
    """
    idx = _index()
    text = '开头是新的内容' + CORPUS[20:120] + '结尾也是新的内容'
    spans = idx.find_spans(text, threshold=THRESHOLD)
    assert spans
    assert CORPUS[20:120] in spans[0]['text']
    assert spans[0]['text'] in idx.corpus      # 报出来的必须是语料里真连续的一段


def test_no_false_join_across_positions():
    """两段分别出现在语料**不同位置**的片段，拼起来不得算成一段重合。

    这就是第一版尺的错：连续 12-gram 全命中可能来自不同位置，
    按「整段验证失败就整段丢弃」会把真的照搬算成 0。
    """
    idx = _index()
    half_a = CORPUS[0:40]
    half_b = CORPUS[300:340]
    joined = half_a + half_b          # 两段之间没有任何共同上下文
    spans = idx.find_spans(joined, threshold=THRESHOLD)
    for s in spans:
        # 每一处命中都必须**真的**连续出现在语料里
        assert s['text'] in idx.corpus


# —— 降级：宁可漏判，不堵死真活 ————————————————————————

def test_scan_without_index_is_skipped_not_blocking():
    from app.generation.verbatim import scan_verbatim
    report = scan_verbatim('随便一段文字', None, threshold=THRESHOLD)
    assert report['verdict'] == 'skipped'
    assert report['match_count'] == 0


def test_scan_reports_clean_when_no_overlap():
    from app.generation.verbatim import scan_verbatim
    report = scan_verbatim('他把手里的东西放下，转身走了出去。', _index(), threshold=THRESHOLD)
    assert report['verdict'] == 'clean'
    assert report['match_count'] == 0


def test_short_overlap_only_warns_and_does_not_block():
    """12~24 字只**告警**，不拦（误报会堵死真活）。"""
    from app.generation.verbatim import scan_verbatim
    report = scan_verbatim(CORPUS[10:10 + 20], _index(), threshold=THRESHOLD)
    assert report['match_count'] == 0
    assert report['warn_count'] >= 1
    assert report['verdict'] == 'warning'


# —— 引擎里的门禁 ————————————————————————————————————————

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('VERBATIM_CACHE_DIR', str(tmp_path / 'verbatim'))
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(engine)
    s.add(NovelChunk(novel_id=1, chapter_no=1, idx=0, text=CORPUS))
    p = Project(title='测试', premise='雨夜查旧档', outline=[], reference_novel_ids=[1])
    s.add(p)
    s.commit()
    yield s, p
    s.close()


def _make_provider(reply):
    class Writer:
        def __init__(self):
            self.calls = []

        def chat(self, system, user, **kw):
            self.calls.append((system, user))
            return reply

    return Writer()


def test_gate_passes_when_no_copy(tmp_path, monkeypatch, db):
    session, project = db
    from app.generation.engine import _maybe_fix_verbatim
    from app.generation.verbatim import build_index

    build_index(session, [1])
    provider = _make_provider('不会用到的回复')
    text, info = _maybe_fix_verbatim(session, project, '他把手里的东西放下，转身走了出去。',
                                     provider, novel_ids=[1])
    assert text == '他把手里的东西放下，转身走了出去。'
    assert info['verdict'] == 'clean'


def test_gate_skips_when_index_not_ready(tmp_path, monkeypatch, db):
    """索引还没建好 ⇒ **放行**（并后台去建），不能把生成卡死。"""
    session, project = db
    from app.generation import verbatim
    from app.generation.engine import _maybe_fix_verbatim

    verbatim._INDEX_CACHE.clear()
    called = []
    monkeypatch.setattr(verbatim, 'warm_index_async',
                        lambda db, ids: called.append(list(ids)))
    provider = _make_provider('不会用到的回复')
    text, info = _maybe_fix_verbatim(session, project, '一段普通正文。', provider, novel_ids=[1])
    assert info['verdict'] == 'skipped'
    assert info['reason'] == 'index_not_ready'
    assert called, "没有触发后台建索引"


def test_gate_rewrites_and_passes_after_model_fixes_it(tmp_path, monkeypatch, db):
    """命中 → 回灌 → 模型换掉措辞 → 正文放行。"""
    session, project = db
    from app.generation.engine import _maybe_fix_verbatim
    from app.generation.verbatim import build_index

    build_index(session, [1])
    # 改写后的长度必须跟原文差不多（低于 60% 判「改坏了」，不采纳）
    fixed = ('他把手里那卷东西慢慢放下，站起身，拍了拍衣角上的灰，'
             '朝门外走出去，脚步声很快散在巷子里，没再回头。')
    provider = _make_provider(fixed)
    original = CORPUS[10:10 + 60]
    assert len(fixed) >= len(original) * 0.6
    text, info = _maybe_fix_verbatim(session, project, original, provider, novel_ids=[1])
    assert info['verdict'] == 'clean'
    assert info.get('rewritten') is True
    assert text == fixed
    # 回灌的提示里必须**指出**命中片段（不然模型不知道改哪儿）
    assert provider.calls, "没有回灌调用"
    assert original[:10] in provider.calls[0][1]


def test_gate_blocks_when_model_cannot_fix_it(tmp_path, monkeypatch, db):
    """命中 → 回灌 → 还是照搬 ⇒ 抛 VerbatimViolation（可重试失败）。"""
    session, project = db
    from app.generation.engine import _maybe_fix_verbatim
    from app.generation.verbatim import VerbatimViolation, build_index

    build_index(session, [1])
    provider = _make_provider(CORPUS[10:10 + 60])   # 改不动
    with pytest.raises(VerbatimViolation) as exc:
        _maybe_fix_verbatim(session, project, CORPUS[10:10 + 60], provider, novel_ids=[1])
    assert exc.value.code == 'possible_copy'
    assert exc.value.spans
    assert '参考语料' in exc.value.retry_hint


def test_gate_rejects_a_rewrite_that_lost_content(tmp_path, monkeypatch, db):
    """回灌改写把内容削掉大半 ⇒ 判「改坏了」，**不采纳**（宁可这一段不落库）。"""
    session, project = db
    from app.generation.engine import _maybe_fix_verbatim
    from app.generation.verbatim import VerbatimViolation, build_index

    build_index(session, [1])
    provider = _make_provider('他走了。')          # 3 个字：内容被削光了
    original = CORPUS[10:10 + 60]
    with pytest.raises(VerbatimViolation):
        _maybe_fix_verbatim(session, project, original, provider, novel_ids=[1])


def test_gate_skips_when_no_reference_corpus(tmp_path, monkeypatch, db):
    session, project = db
    from app.generation.engine import _maybe_fix_verbatim
    provider = _make_provider('不会用到的回复')
    project.reference_novel_ids = []      # 这个项目没有参考语料
    session.commit()
    text, info = _maybe_fix_verbatim(session, project, '一段普通正文。', provider, novel_ids=[])
    assert info['verdict'] == 'skipped'
    assert info['reason'] == 'no_reference_corpus'


# —— 索引缓存 ————————————————————————————————————————————

def test_index_cache_reuses_by_fingerprint(tmp_path, monkeypatch, db):
    """指纹不变 ⇒ 第二次不重建；语料变了 ⇒ 指纹要变。"""
    session, _ = db
    from app.generation import verbatim

    verbatim._INDEX_CACHE.clear()
    fp1 = verbatim.corpus_fingerprint(session, [1])
    idx = verbatim.build_index(session, [1])
    assert idx is not None
    assert verbatim.load_index(session, [1]) is idx      # 进程内命中

    session.add(NovelChunk(novel_id=1, chapter_no=1, idx=1, text='新增的一段语料内容，用来改变指纹。'))
    session.commit()
    fp2 = verbatim.corpus_fingerprint(session, [1])
    assert fp1 != fp2, "语料变了指纹却没变 —— 索引会过期"


def test_index_roundtrip_on_disk(tmp_path, monkeypatch, db):
    """索引落盘后能读回来，且读回来的索引判定结果一致。"""
    session, _ = db
    from app.generation.verbatim import VerbatimIndex, build_index

    idx = build_index(session, [1])
    assert idx is not None
    path = tmp_path / 'roundtrip.vbi'
    idx.save(path)
    again = VerbatimIndex.load(path)
    assert again.find_spans(CORPUS[10:10 + 60], threshold=THRESHOLD)
    assert again.find_spans('完全无关的一句话内容。', threshold=THRESHOLD) == []


# —— 路由层：失败要给人话 ————————————————————————————————

def test_router_returns_readable_conflict():
    """照搬失败 ⇒ 409 + 人话；内部代号只出现在 status 里。"""
    from fastapi import HTTPException
    from app.generation.verbatim import VerbatimViolation
    from app.routers.generation import _verbatim_conflict

    exc = VerbatimViolation({'spans': [{'text': '青石城的东门临着官道，守卒每日卯时换岗'}]})
    http_exc = _verbatim_conflict(exc)
    assert http_exc.status_code == 409
    detail = http_exc.detail
    assert detail['status'] == 'possible_copy'
    assert '参考语料' in detail['message']
    assert 'possible_copy' not in detail['message']   # 内部代号不进给人看的文案
