"""正文的**文字质量**审核：标点事故（error）与外来英文（warn）。

要守的两条：
1. **规则判得死的才 error**（标点）—— error 会挡住工序链，误报就堵死真活；
2. **判不准的只报不拦**（外来词）—— 小说里合法地有英文（专名/型号/引文）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from test_causal_timeline import fixture  # noqa: E402
from app.models import Generation  # noqa: E402


def _seed(db, p, text):
    db.add(Generation(project_id=p.id, segment_no=1, text=text))
    db.commit()
def test_halfwidth_punctuation_between_chinese_is_an_error():
    from app.agents.text_quality import run_text_quality_audit
    db, p = fixture()
    # 半角逗号（显式拼出来，免得源码里看���出是哪个）
    _seed(db, p, '他' + ',' + '将砂粒捏碎，放在鼻下闻了闻。')
    report, stats = run_text_quality_audit(db, p)
    errors = [i for i in report.issues if i.severity == 'error']
    assert len(errors) == 1, report.issues
    assert errors[0].rule == 'text_punctuation_accident'
    assert stats['punctuation_hits'] == 1
    # 证据要能指到位置
    assert errors[0].evidence


def test_repeated_punctuation_is_an_error():
    from app.agents.text_quality import run_text_quality_audit
    db, p = fixture()
    _seed(db, p, '他愣住了。。船还在晃。')
    report, _stats = run_text_quality_audit(db, p)
    errors = [i for i in report.issues if i.severity == 'error']
    assert len(errors) == 1, report.issues


def test_clean_chinese_prose_reports_nothing():
    from app.agents.text_quality import run_text_quality_audit
    db, p = fixture()
    _seed(db, p, '少年把竹篙插进三石缝里，撑了两下，船身才稳住。')
    report, stats = run_text_quality_audit(db, p)
    assert not report.issues, report.issues
    assert stats['blocking'] == 0


def test_a_latin_word_glued_to_chinese_is_reported_but_does_not_block():
    """「Already惹了。」—— 紧贴中文的英文词：报出来，但**不挡**。

    不挡的理由：白名单盖不全（品牌/型号/引文/专名都可能是外文），
    而 error 会挡住工序链 —— 误报的代价是「真活被堵死」。
    """
    from app.agents.text_quality import run_text_quality_audit
    db, p = fixture()
    _seed(db, p, '“Already惹了。”徐凤年捂着后脑退回老黄身边。')
    report, stats = run_text_quality_audit(db, p)
    warns = [i for i in report.issues if i.severity == 'warn']
    assert len(warns) == 1, report.issues
    assert warns[0].rule == 'text_foreign_latin'
    assert stats['foreign_latin_hits'] == 1
    assert stats['foreign_latin_glued'] == 1
    assert stats['blocking'] == 0


def test_a_quoted_english_phrase_is_not_reported():
    """引文里**分开**的外文是合法的（“Hello” 他说）—— 不该报。"""
    from app.agents.text_quality import run_text_quality_audit
    db, p = fixture()
    _seed(db, p, '“Hello，”徐凤年说，“你听我解释。”')
    report, stats = run_text_quality_audit(db, p)
    assert stats['foreign_latin_hits'] == 0, report.issues


def test_a_registered_proper_noun_is_not_reported():
    """已登记的专名（剧情圣经里的角色）出现是合法的。"""
    from app.agents.text_quality import run_text_quality_audit
    db, p = fixture()
    p.plot_bible = {'characters': [{'name': 'Alice'}]}
    db.commit()
    _seed(db, p, 'Alice把伞收起来，站在门口看了看天。')
    report, stats = run_text_quality_audit(db, p)
    assert stats['foreign_latin_hits'] == 0, report.issues


def test_a_model_code_is_not_reported():
    from app.agents.text_quality import run_text_quality_audit
    db, p = fixture()
    _seed(db, p, '桌上摊着 PC-2000 的说明书，他翻了两页。')
    report, stats = run_text_quality_audit(db, p)
    assert stats['foreign_latin_hits'] == 0, report.issues


def test_the_agent_can_run_the_text_quality_audit():
    from app.agents.registry import get_tool_registry
    db, p = fixture()
    _seed(db, p, '“Already惹了。”徐凤年捂着后脑退回老黄身边。')
    out = get_tool_registry().invoke('audit_text_quality', db, p.id)
    assert out['found'] is True, out
    assert out['errors'] == 0
    assert out['warnings'] == 1
    assert out['issues'][0]['rule'] == 'text_foreign_latin'


def test_the_punctuation_fix_is_computed_by_the_rule_not_the_model():
    """修法是**机械唯一**的：半角→全角、重复标点收成一个。

    模型不参与改正文 —— 它只负责**提出**；真正的「改后长什么样」由规则算。
    """
    from app.agents.text_quality import fix_punctuation
    fixed, changes = fix_punctuation('他,将砂粒捏碎。。船身晃了晃。')
    assert changes == 2
    assert fixed == '他，将砂粒捏碎。船身晃了晃。'


def test_the_fix_keeps_english_decimal_points():
    """英文里的半角小数点不能被改成全角（规则只改命中中文旁的那几处）。"""
    from app.agents.text_quality import fix_punctuation
    fixed, changes = fix_punctuation('水温 3.5 度，他把桨放下。')
    assert fixed == '水温 3.5 度，他把桨放下。'
    assert changes == 0


def test_the_agent_can_propose_the_fix_as_a_draft():
    """审核 → 修复：agent 起草（只构造卡），用户在卡上确认后才落库。"""
    from app.agents.registry import get_tool_registry
    from app.models import EditProposal
    db, p = fixture()
    _seed(db, p, '他,将砂粒捏碎，放在鼻下闻了闻。')
    gen_id = db.query(Generation).filter_by(project_id=p.id).one().id
    out = get_tool_registry().invoke('propose_text_fix', db, p.id,
                                     generation_id=gen_id)
    assert out['ok'] is True, out
    card = out['pending_action']
    assert card['kind'] == 'proposal'
    assert card['body']['type'] == 'text_edit'
    assert card['body']['target_kind'] == 'generation_text'
    assert card['body']['target_id'] == str(gen_id)
    # 草稿是**改对**的那一版
    assert card['body']['after_text'] == '他，将砂粒捏碎，放在鼻下闻了闻。'
    # 只构造卡：不落库
    assert db.query(EditProposal).count() == 0


def test_the_agent_refuses_to_fix_a_segment_without_punctuation_problems():
    """没有可自动修正的问题 → 拒（外来英文词机器不替人改）。"""
    from app.agents.registry import get_tool_registry
    db, p = fixture()
    _seed(db, p, '“Already惹了。”徐凤年捂着后脑退回老黄身边。')
    gen_id = db.query(Generation).filter_by(project_id=p.id).one().id
    out = get_tool_registry().invoke('propose_text_fix', db, p.id,
                                     generation_id=gen_id)
    assert out['ok'] is False, out
    assert 'pending_action' not in out


# ----------------------------------------------------------------------
# 事实自洽（模型那一层）：只报不挡
# ----------------------------------------------------------------------

class _FakeProvider:
    """假渠道：回一段固定答案。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat(self, system, user, **kwargs):
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_a_fact_contradiction_is_reported_but_never_blocks():
    """模型判出的属性矛盾 → **warn**，不阻塞（模型会错：时间推进/撒谎/猜测都像矛盾）。"""
    from app.agents.text_quality import run_text_quality_audit
    db, p = fixture()
    _seed(db, p, '他那年十四岁。\n沈娘子笑他：十三岁的娃娃，也敢撑船。')
    provider = _FakeProvider(
        '{"contradictions":[{"entity":"陆见川","attribute":"年龄",'
        '"values":["十四岁","十三岁"],"source_ids":["G1P001","G1P002"],'
        '"quote":"十三岁的娃娃","reason":"同一段里两种说法"}]}')
    report, stats = run_text_quality_audit(db, p, provider=provider)
    warns = [i for i in report.issues if i.severity == 'warn']
    assert len(warns) == 1, report.issues
    assert warns[0].rule == 'text_fact_contradiction'
    assert '年龄' in warns[0].title
    # **不阻塞**
    assert stats['blocking'] == 0
    assert stats['fact_contradictions'] == 1
    assert provider.calls == 1


def test_a_broken_model_answer_degrades_to_the_deterministic_rules():
    """模型答得不合格/直接报错 → 不猜，只跑确定性规则（不拖垮整次审核）。"""
    from app.agents.text_quality import run_text_quality_audit
    db, p = fixture()
    _seed(db, p, '他,将竹篙插进石缝。')
    for payload in (RuntimeError('boom'), 'not json at all',
                    '{"contradictions": "not a list"}'):
        provider = _FakeProvider(payload)
        report, stats = run_text_quality_audit(db, p, provider=provider)
        assert stats['blocking'] == 1, stats  # 标点那条照旧
        assert not [i for i in report.issues
                    if i.rule == 'text_fact_contradiction']


def test_a_crash_in_the_fact_layer_is_visible_in_the_stats():
    """这一层挂掉**不静默**：统计里要看得见，否则「没报」会被读成「查过没问题」。

    这条纪律是实测换来的：漏了一个 import，NameError 被静默降级吞掉，
    表现成「没发现矛盾」——看起来像正常结果。
    """
    from app.agents.text_quality import run_text_quality_audit
    db, p = fixture()
    _seed(db, p, '他那年十四岁。')

    class _Boom:
        def chat(self, *args, **kwargs):
            raise ValueError('provider exploded')

    report, stats = run_text_quality_audit(db, p, provider=_Boom())
    assert 'fact_error' in stats, stats
    assert 'ValueError' in stats['fact_error']


def test_the_fact_check_is_skipped_without_a_provider():
    """不给渠道就不调模型（工序链的读路径上就是这个口径）。"""
    from app.agents.text_quality import run_text_quality_audit
    db, p = fixture()
    _seed(db, p, '他那年十四岁。')
    report, stats = run_text_quality_audit(db, p)
    assert stats['fact_checked'] is False
    assert not [i for i in report.issues if i.rule == 'text_fact_contradiction']
