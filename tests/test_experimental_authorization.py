"""实验性诊断的**授权闸门**：模型自己说「用户同意了」不算数。

## 这条闸门在防什么

`allow_experimental` 原本是**模型自己传**的参数，`diagnostic_tool` 直接把它
透传给 `load_skill` —— 于是「实验技能需用户明确同意」这条纪律形同虚设：
模型说一声就等于用户同意了。

现在放行与否由服务端按**用户这条消息里的原话**决定（模型伪造不了请求体里的
`query`），缺授权就拒，并把该怎么说回灌给 agent，让它转告用户。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest  # noqa: E402


# ====================================================================
# 1) 授权检测：只在用户自己那句话里找
# ====================================================================

@pytest.mark.parametrize('query', [
    '实验性诊断技能我明确同意使用。',
    '同意使用实验性诊断技能',
    '我允许这次跑实验性技能',
    '这次诊断你授权用实验性技能吧',
    '实验性诊断，我同意了',
])
def test_an_explicit_consent_is_recognised(query):
    from app.agents.consent import user_granted_experimental
    assert user_granted_experimental(query) is True


@pytest.mark.parametrize('query', [
    '帮我查一下正文有没有标点问题',          # 压根没提授权
    '不要使用实验性诊断技能',                # 否定
    '不同意使用实验技能',
    '无需实验授权',
    '暂时不用实验技能',
    '这次实验数据不太对',                   # 提到实验但不是授权
    '这段实验报告你同意吗',                  # 「实验」不是实验技能
    '同意，但别用实验性技能',
    '',
    None,
])
def test_a_missing_or_negated_consent_is_not_recognised(query):
    from app.agents.consent import user_granted_experimental
    assert user_granted_experimental(query) is False


def test_consent_must_be_about_the_experiment_not_something_else():
    from app.agents.consent import user_granted_experimental
    assert user_granted_experimental('同意，但别用实验性技能') is False


# ====================================================================
# 2) 闸门：模型的开关不是授权
# ====================================================================

def test_the_models_own_flag_does_not_grant_access():
    """模型传 allow_experimental=true，而用户没授权 → 拒，并说清该怎么说。"""
    from app.agents.diagnostic import diagnostic_tool
    from test_causal_timeline import fixture
    db, p = fixture()
    with pytest.raises(ValueError) as exc:
        diagnostic_tool(db, p.id, task_type='body_consistency',
                        skill_id='body_consistency', skill_version=1,
                        allow_experimental=True, segment_nos=[1])
    message = str(exc.value)
    assert '用户明确授权' in message
    assert '同意使用实验性诊断技能' in message, '要告诉用户该怎么说（回灌）'


def test_the_users_own_words_do_grant_access(monkeypatch):
    """有授权 → 放行，并且**真的**按实验性执行（allow_experimental=True）。"""
    from app.agents import diagnostic as diag
    from app.agents.consent import (reset_experimental_consent,
                                    set_experimental_consent)
    from test_causal_timeline import fixture
    db, p = fixture()
    seen = {}

    def fake_run(db_, project_id, **kwargs):
        seen.update(kwargs)
        return {'run_id': 1, 'status': 'completed', 'assessment': 'observed',
                'model_participated': True, 'unresolved': [], 'checks': [],
                'proposed_actions': [], 'findings': [], 'skill': {},
                'scope': {}, 'input_fingerprint': '', 'evidence_refs': [],
                'coverage': [], 'model_error_category': None,
                'inventory_summary': None}
    monkeypatch.setattr(diag, 'run_diagnostic_task', fake_run)

    token = set_experimental_consent(True)
    try:
        diag.diagnostic_tool(db, p.id, task_type='body_consistency',
                             skill_id='body_consistency', skill_version=1,
                             allow_experimental=True, segment_nos=[1])
    finally:
        reset_experimental_consent(token)
    assert seen['allow_experimental'] is True


def test_the_users_grant_is_a_ceiling_not_a_licence_to_run(monkeypatch):
    """用户授权了，但模型这次没要用实验技能 → 不许自作主张跑实验的。"""
    from app.agents import diagnostic as diag
    from app.agents.consent import (reset_experimental_consent,
                                    set_experimental_consent)
    from test_causal_timeline import fixture
    db, p = fixture()
    seen = {}

    def fake_run(db_, project_id, **kwargs):
        seen.update(kwargs)
        return {'run_id': 1, 'status': 'completed', 'assessment': 'observed',
                'model_participated': True, 'unresolved': [], 'checks': [],
                'proposed_actions': [], 'findings': [], 'skill': {},
                'scope': {}, 'input_fingerprint': '', 'evidence_refs': [],
                'coverage': [], 'model_error_category': None,
                'inventory_summary': None}
    monkeypatch.setattr(diag, 'run_diagnostic_task', fake_run)

    token = set_experimental_consent(True)
    try:
        diag.diagnostic_tool(db, p.id, task_type='body_consistency',
                             skill_id='body_consistency', skill_version=1,
                             allow_experimental=False, segment_nos=[1])
    finally:
        reset_experimental_consent(token)
    assert seen['allow_experimental'] is False


def test_the_grant_does_not_leak_into_the_next_request():
    """授权是**本次请求**的 —— 还原后就没了，不能顺延。"""
    from app.agents.consent import (experimental_consent_granted,
                                    reset_experimental_consent,
                                    set_experimental_consent)
    token = set_experimental_consent(True)
    assert experimental_consent_granted() is True
    reset_experimental_consent(token)
    assert experimental_consent_granted() is False


# ====================================================================
# 3) 路由层：凭据来自请求里的 query（客户端发的，模型改不了）
# ====================================================================

def test_the_router_takes_the_credential_from_the_users_own_message(monkeypatch):
    """用户在 query 里说了同意 → 本次请求里授权生效；没说 → 不生效。"""
    from app.agents import consent as consent_mod
    from app.agents import react_loop as react_mod
    from app.routers import assistant as assistant_router
    from test_causal_timeline import fixture

    db, p = fixture()
    seen = {}

    def fake_run_react(**kwargs):
        seen['granted'] = consent_mod.experimental_consent_granted()
        from app.agents.react_loop import ReactResult
        return ReactResult(answer='ok', mode='react', model_participated=True,
                           stop_reason='final_answer', rounds=[], budget={},
                           degraded=False, degraded_reason='', tool_results={},
                           observations=[], provider_kind='model')

    # `run_react` 是端点内部 import 的 → 打在它自己的模块上
    monkeypatch.setattr(react_mod, 'run_react', fake_run_react)
    monkeypatch.setattr(assistant_router, '_ensure_assistant_provider',
                        lambda: type('P', (), {'model': 'fake', 'label': 'fake'})())

    assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=p.id, query='同意使用实验性诊断技能，帮我看看'),
        db=db)
    assert seen['granted'] is True

    assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=p.id, query='帮我看看正文有没有问题'),
        db=db)
    assert seen['granted'] is False
