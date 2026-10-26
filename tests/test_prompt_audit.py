"""系统提示词体检 + 两条轨的工序链。

体检查的是「蒸馏产物有没有真进到 writer 吃进去的那份提示词」——
这类失败**不会报错**，只会让文风蒸馏白做、或让正文开始像参考书。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from test_causal_timeline import fixture  # noqa: E402


def _profile(*, dims=None, forbidden=None):
    """造一份叙事档案（method 层维度 → 规则列表）。"""
    profile = {'method': dict(dims or {})}
    if forbidden:
        profile['forbidden'] = forbidden
    return profile


def _rules_by_id(report):
    return {getattr(i, 'rule', ''): i for i in report.issues}


def test_narrative_not_injected_is_an_error():
    """档案存着但渲染成空块 → 蒸馏白做 → 阻塞。"""
    from app.agents.prompt_audit import run_prompt_audit
    db, p = fixture()
    p.style_narrative = _profile(dims={'一个不存在的维度': [{'rule': '某条规则'}]})
    db.commit()
    report, stats = run_prompt_audit(db, p)
    assert stats['has_narrative'] is True
    assert stats['narrative_injected'] is False
    issues = _rules_by_id(report)
    assert 'prompt_narrative_not_injected' in issues
    assert issues['prompt_narrative_not_injected'].severity == 'error'


def test_voice_dictionary_in_the_archive_is_not_a_leak_by_itself():
    """档案里留着【角色声音词典】**不是**泄漏 —— 续写模式正需要它。

    泄漏的判据是**渲染结果**：原创模式的 `include_voice=False`，渲染块里
    本来就没有这个维度。老写法查的是「档案里有没有这个维度」→ 任何含真书
    档案的项目在原创模式都被判成泄漏（实测：雪中悍刀行真书档案必误报，
    并被生成闸门挡住）。
    """
    from app.agents.prompt_audit import run_prompt_audit
    db, p = fixture()
    p.style_narrative = _profile(
        dims={'对白风格': [{'rule': '台词短促，少用长句修饰'}],
              '角色声音词典': [{'rule': '轩辕青锋：语气冷淡，带有嘲讽'}]})
    db.commit()
    report, stats = run_prompt_audit(db, p)
    assert stats['narrative_injected'] is True
    issues = _rules_by_id(report)
    assert 'prompt_voice_leak_in_original' not in issues
    # 档案里保留它是对的（续写模式要用）
    assert 'prompt_narrative_not_injected' not in issues


def test_voice_leak_is_caught_when_the_original_prompt_actually_includes_it(monkeypatch):
    """真的漏了（原创模式把声音词典注进去了）→ 必须报出来。

    这条守着不变量本身：判据换成「渲染结果」之后，防护不能跟着消失。
    """
    from app.agents.prompt_audit import run_prompt_audit
    from app.generation import profiles
    db, p = fixture()
    p.style_narrative = _profile(
        dims={'对白风格': [{'rule': '台词短促，少用长句修饰'}],
              '角色声音词典': [{'rule': '轩辕青锋：语气冷淡，带有嘲讽'}]})
    db.commit()
    monkeypatch.setitem(profiles.PROFILE_RULES['original'], 'include_voice', True)
    report, _stats = run_prompt_audit(db, p)
    issues = _rules_by_id(report)
    assert 'prompt_voice_leak_in_original' in issues
    assert issues['prompt_voice_leak_in_original'].severity == 'error'


def test_forbidden_word_conflicting_with_narrative_is_an_error():
    """档案说「不许出现金印」，规则正文就在写金印 → 自相矛盾 → 阻塞。"""
    from app.agents.prompt_audit import run_prompt_audit
    db, p = fixture()
    p.style_narrative = _profile(
        dims={'对白风格': [{'rule': '人物提到金印时只用短句，不加解释'}]},
        forbidden={'物件': ['金印']})
    db.commit()
    report, _stats = run_prompt_audit(db, p)
    issues = _rules_by_id(report)
    assert 'prompt_forbidden_self_conflict' in issues
    assert issues['prompt_forbidden_self_conflict'].severity == 'error'


def test_forbidden_word_that_only_appears_in_the_forbidden_list_is_not_a_conflict():
    """禁用词只出现在**禁用清单自己**里 → 不是自相矛盾。

    渲染块本来就把禁用清单写进去（`【禁用转折词】禁止使用：然而、但是…`），
    拿整块去比等于**禁用清单和自己匹配** —— 只要档案有禁用清单就恒真。
    实测：真书档案上这条检查必然误报。
    """
    from app.agents.prompt_audit import run_prompt_audit
    db, p = fixture()
    p.style_narrative = _profile(
        dims={'对白风格': [{'rule': '台词短促，少用长句修饰'}]},
        forbidden={'转折词': ['然而', '但是', '与此同时']})
    db.commit()
    report, _stats = run_prompt_audit(db, p)
    issues = _rules_by_id(report)
    assert 'prompt_forbidden_self_conflict' not in issues


def test_healthy_profile_passes_the_prompt_audit():
    from app.agents.prompt_audit import run_prompt_audit
    db, p = fixture()
    p.style_narrative = _profile(dims={'对白风格': [{'rule': '台词短促，少用长句修饰'}]})
    db.commit()
    report, stats = run_prompt_audit(db, p)
    assert stats['narrative_injected'] is True
    assert not [i for i in report.issues if i.severity == 'error']


def test_workflow_has_two_tracks_and_a_dispatch_owner():
    """工序链是两条轨，每道工序都写明派给谁做。"""
    from app.agents.workflow import STEPS
    tracks = {s.track for s in STEPS}
    assert tracks == {'prompt', 'body'}
    assert all(s.dispatch for s in STEPS)
    ids = [s.id for s in STEPS]
    assert 'review_prompt' in ids
    prompt_steps = [s for s in STEPS if s.track == 'prompt']
    body_steps = [s for s in STEPS if s.track == 'body']
    assert [s.id for s in prompt_steps] == [
        'corpus_ready', 'clean_check', 'distill', 'review_narrative', 'review_prompt']
    # 正文轨：闭环那四类动作（验收 / 修复 / 重建 / 解锁）各占一步，
    # 顺序就是闭环的真实顺序：审 → 改 → 重建 → 解锁 → 收敛。
    assert [s.id for s in body_steps] == [
        'write_body', 'review_body', 'repair', 'derived_rebuild', 'unlock', 'converged']
    # 提示词轨的体检在正文轨之前：writer 吃到的提示词要先过关
    assert ids.index('review_prompt') < ids.index('write_body')
    # 收敛永远在最后
    assert ids[-1] == 'converged'


def _seed_corpus(db, p):
    from app.models import NovelChunk, RawNovel
    novel = RawNovel(title='测试语料', status='imported')
    db.add(novel)
    db.flush()
    db.add(NovelChunk(novel_id=novel.id, chapter_no=1, idx=1, text='这一段有句号。'))
    p.reference_novel_ids = [novel.id]
    db.commit()


def test_prompt_step_blocks_the_chain_when_prompt_audit_fails():
    """提示词体检不过 → 工序链卡在提示词轨，不进正文轨。

    这份假档案同时触发了**档案审查**（维度不在清单里），
    所以链上先卡在「档案审查」——但提示词体检那一步也必须是阻塞的，
    两道审核都得拦住，不能只靠一道。
    """
    from app.agents.workflow import evaluate
    db, p = fixture()
    _seed_corpus(db, p)
    p.style_narrative = _profile(dims={'一个不存在的维度': [{'rule': '某条规则'}]})
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['review_prompt']['state'] == 'blocked', by_id['review_prompt']
    assert state['status'] == 'blocked'
    # 提示词轨没过 → 不进正文轨（正文审查排在后面，状态不会是 done）
    assert state['current_step'] in ('review_narrative', 'review_prompt')
    assert by_id['review_body']['state'] != 'done'


def test_agent_can_audit_the_prompt():
    """agent 只读工具 audit_prompt：能查出问题，且不写库。"""
    from app.agents.registry import get_tool_registry
    from app.models import WorkflowRun
    db, p = fixture()
    p.style_narrative = _profile(dims={'一个不存在的维度': [{'rule': '某条规则'}]})
    db.commit()
    out = get_tool_registry().invoke('audit_prompt', db, p.id)
    assert out['found'] is True
    assert out['errors'] >= 1
    assert any(i['rule'] == 'prompt_narrative_not_injected' for i in out['issues'])
    assert db.query(WorkflowRun).filter_by(project_id=p.id).count() == 0
