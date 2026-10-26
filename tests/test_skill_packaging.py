"""skill 沉淀回归：本项目实测有效的做法要能作为 skill 被助理 agent 发现并调用。

三份新 skill 都来自真实跑通的工作：
- text_quality_audit   文字质量审核（标点 error / 外来词 warn）
- text_quality_repair  标点事故的机械修复（只构造待确认卡）
- prompt_health_audit  提示词体检（蒸馏产物有没有真进提示词）

三条纪律在这里被钉住：
1. 清单能被发现、被加载，角色（谁执行）由清单自己声明；
2. 子 agent 按角色给**不同口径**的结论（editor 只认机械修法）；
3. 全程只读：跑完 skill 不许改一个字正文。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from test_causal_timeline import fixture  # noqa: E402

from app.agents.diagnostic import (  # noqa: E402
    ROLE_SYSTEM_PROMPTS, read_capability, run_diagnostic_task,
)
from app.agents.diagnostic_skills import (  # noqa: E402
    ROLES, discover_skills, load_skill,
)

BAD_TEXT = ('少年把竹篙插进石缝,船身晃了晃。。他稳住气,又撑了两下。'
            '窗外的雾散了一点,远处的灯塔亮起来,一下,又灭了。')


class Recording:
    """记下每次调用的 system 与 user，用来断言子 agent 的**角色口径**。"""

    def __init__(self, data=None):
        self.data = data or {'assessment': 'observed', 'reason': '机械修法已算出'}
        self.systems = []
        self.users = []

    def chat(self, system, user, **kwargs):
        self.systems.append(system)
        self.users.append(user)
        return json.dumps(self.data, ensure_ascii=False)


# ----------------------------------------------------------------------
# 清单：能被发现、能被加载、角色由清单声明
# ----------------------------------------------------------------------

def test_the_three_new_skills_are_discoverable():
    ids = {entry['id'] for entry in discover_skills(allow_experimental=True)}
    assert {'text_quality_audit', 'text_quality_repair', 'prompt_health_audit'} <= ids


def test_each_skill_declares_its_own_executor_role():
    assert load_skill('text_quality_audit', 1, allow_experimental=True)['agent_roles'] == ['diagnostic']
    assert load_skill('text_quality_repair', 1, allow_experimental=True)['agent_roles'] == ['editor']
    assert load_skill('prompt_health_audit', 1, allow_experimental=True)['agent_roles'] == ['diagnostic']
    assert set(ROLES) == {'diagnostic', 'editor'}


def test_an_unknown_role_is_refused():
    """角色必须是已注册的执行口径 —— 清单不能自己发明一个角色。"""
    from app.agents.diagnostic_skills import validate_manifest
    manifest = load_skill('text_quality_audit', 1, allow_experimental=True)
    broken = dict(manifest, agent_roles=['poet'])
    with pytest.raises(ValueError, match='invalid_manifest_contract'):
        validate_manifest(broken)


def test_a_skill_without_its_capability_is_refused():
    from app.agents.diagnostic_skills import validate_manifest
    manifest = load_skill('text_quality_audit', 1, allow_experimental=True)
    broken = dict(manifest, required_capabilities=[{'id': 'no_such_capability', 'min_version': 1}])
    with pytest.raises(ValueError, match='missing_capability'):
        validate_manifest(broken)


# ----------------------------------------------------------------------
# 能力：只读、看得见覆盖范围、不静默
# ----------------------------------------------------------------------

def test_text_quality_capability_reports_issues_and_mechanical_fixes():
    from app.models import Generation
    db, p = fixture()
    db.add(Generation(project_id=p.id, segment_no=1, text=BAD_TEXT))
    db.commit()

    obs = read_capability(db, p.id, 'inspect_text_quality', {})
    assert obs['capability'] == 'inspect_text_quality'
    assert obs['coverage']['source'] == 'Generation.text'
    assert obs['coverage']['complete'] is True
    rules = {i['rule'] for i in obs['data']['issues']}
    assert 'text_punctuation_accident' in rules
    # 修法由规则算出，模型不重新措辞
    assert obs['data']['fixes'], '规则应给出机械修法'
    fix = obs['data']['fixes'][0]
    assert fix['after'].count('，') == 5, '5 处半角逗号换全角'
    assert '。。' not in fix['after'], '重复标点收成一个'
    assert fix['changes'] == 6, '5 处半角 + 1 处重复标点'
    # 事实自洽那一层没跑 —— 必须**看得见**，不能当成「查过没问题」
    assert obs['data']['stats']['fact_checked'] is False


def test_prompt_health_capability_is_read_only():
    db, p = fixture()
    obs = read_capability(db, p.id, 'inspect_prompt_health', {})
    assert obs['capability'] == 'inspect_prompt_health'
    assert 'issues' in obs['data'] and 'stats' in obs['data']
    assert obs['coverage']['complete'] is True


# ----------------------------------------------------------------------
# 端到端：助理 agent 调起子 agent，角色口径不同，全程不写正文
# ----------------------------------------------------------------------

def test_text_quality_audit_runs_on_the_diagnostic_role():
    from app.models import Generation
    db, p = fixture()
    db.add(Generation(project_id=p.id, segment_no=1, text=BAD_TEXT))
    db.commit()
    provider = Recording()

    r = run_diagnostic_task(db, p.id, task_type='text_quality_audit',
                            skill_id='text_quality_audit', allow_experimental=True,
                            provider=provider)

    assert r['status'] == 'completed', r
    assert r['agent_role'] == 'diagnostic'
    assert r['model_participated'] is True
    assert '角色 diagnostic' in provider.systems[0]
    assert db.query(Generation).filter_by(project_id=p.id).one().text == BAD_TEXT


def test_text_quality_repair_runs_on_the_editor_role_and_never_writes():
    """editor 角色的口径：只认机械修法，不许把「发现问题」说成「改好了」。"""
    from app.models import Generation
    db, p = fixture()
    db.add(Generation(project_id=p.id, segment_no=1, text=BAD_TEXT))
    db.commit()
    provider = Recording()

    r = run_diagnostic_task(db, p.id, task_type='text_quality_repair',
                            skill_id='text_quality_repair', allow_experimental=True,
                            provider=provider)

    assert r['status'] == 'completed', r
    assert r['agent_role'] == 'editor'
    system = provider.systems[0]
    assert '角色 editor' in system
    assert '只认**规则算得出来的机械改法**' in system
    # 机械修法在观察里，人点确认才写
    obs = r['observations'][0]['data']
    assert obs['fixes'][0]['after']
    assert db.query(Generation).filter_by(project_id=p.id).one().text == BAD_TEXT


def test_prompt_health_audit_runs_and_reports():
    db, p = fixture()
    provider = Recording()
    r = run_diagnostic_task(db, p.id, task_type='prompt_health_audit',
                            skill_id='prompt_health_audit', allow_experimental=True,
                            provider=provider)
    assert r['status'] == 'completed', r
    assert r['agent_role'] == 'diagnostic'
    assert r['observations'][0]['capability'] == 'inspect_prompt_health'


def test_a_skill_only_writes_its_own_audit_rows():
    """跑 skill 期间只允许写 agent_runs / evidence 两张审计表。"""
    from sqlalchemy import event
    from app.models import Generation
    db, p = fixture()
    db.add(Generation(project_id=p.id, segment_no=1, text=BAD_TEXT))
    db.commit()

    def guard(conn, cursor, sql, *args):
        head = sql.lstrip().split()[0].upper()
        if head in {'INSERT', 'UPDATE', 'DELETE'}:
            assert 'agent_runs' in sql.lower() or 'evidence' in sql.lower(), sql[:120]
    event.listen(db.bind, 'before_cursor_execute', guard)

    run_diagnostic_task(db, p.id, task_type='text_quality_repair',
                        skill_id='text_quality_repair', allow_experimental=True,
                        provider=Recording())
    assert db.query(Generation).filter_by(project_id=p.id).one().text == BAD_TEXT


def test_the_assistant_can_ask_for_the_new_tasks():
    """助理 agent 的工具参数里要能看到新任务类型（否则它永远选不到）。"""
    from app.agents.registry import _DIAGNOSTIC_PARAMETERS
    enum = _DIAGNOSTIC_PARAMETERS['properties']['task_type']['enum']
    assert 'text_quality_audit' in enum
    assert 'text_quality_repair' in enum
    assert 'prompt_health_audit' in enum


def test_every_role_has_exactly_one_system_prompt():
    """角色与口径是一一对应的 —— 少一条就会掉回默认诊断口径。"""
    assert set(ROLE_SYSTEM_PROMPTS) == set(ROLES)
    for role, prompt in ROLE_SYSTEM_PROMPTS.items():
        assert f'角色 {role}' in prompt


# ====================================================================
# 清单加载失败要说清是「没有这个版本」，不是「清单坏了」
# ====================================================================

def test_a_missing_version_lists_the_available_ones():
    """实测（真实通道）：模型传了 body_consistency v4（只有 v1/v2），
    旧口径报 `invalid_manifest` —— 模型以为自己把清单搞坏了。
    报错得让它能自愈：把可用版本列出来。"""
    from app.agents.diagnostic_skills import load_skill
    with pytest.raises(ValueError) as exc:
        load_skill('body_consistency', 4, allow_experimental=True)
    message = str(exc.value)
    assert 'skill_version_not_found' in message
    assert 'v1' in message and 'v2' in message


def test_an_unknown_skill_lists_the_available_ones():
    from app.agents.diagnostic_skills import load_skill
    with pytest.raises(ValueError) as exc:
        load_skill('no_such_skill', 1, allow_experimental=True)
    message = str(exc.value)
    assert 'unknown_skill' in message
    assert 'body_consistency' in message
