"""Trusted, versioned product playbooks. No attachment/runtime instruction loading."""
from __future__ import annotations
import json
import re
from pathlib import Path
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parent / 'skills'

@dataclass(frozen=True)
class Capability:
    id: str
    version: int
    binding: str
    kind: str = 'read'
    scope_policy: str = 'project_reference_subset'
    side_effects: tuple = ()
    input_schema: tuple = ('project_id', 'scope')
    output_schema: tuple = ('evidence_ref', 'input_fingerprint', 'coverage', 'data')

CAPABILITIES: dict[str, Capability] = {}

#: 可以执行 skill 的**子 agent 角色**。
#:
#: 一份 skill 只声明一个角色：助理 agent（主 agent）挑 skill，子 agent 按角色
#: 的口径执行。角色不同，**裁决口径**就不同 —— 诊断角色只给"看到了什么"，
#: 编辑角色只认"规则算得出来的机械改法"，两者都不许自己写正文。
ROLES: tuple[str, ...] = ('diagnostic', 'editor')

def register_capability(capability: Capability) -> None:
    if capability.id in CAPABILITIES or capability.version < 1 or capability.kind != 'read':
        raise ValueError('invalid_or_duplicate_capability')
    # Binding is an audited local implementation ID, never an import path from JSON.
    if capability.binding not in {'timeline', 'binding', 'claims', 'planning', 'body', 'inventory',
                                  'pending_card', 'text_quality', 'prompt_health'}:
        raise ValueError('unavailable_binding')
    CAPABILITIES[capability.id] = capability

for _id, _binding in [('inspect_timeline_state', 'timeline'),
                       ('inspect_narrative_binding', 'binding'), ('read_distill_evidence', 'claims'),
                       ('inspect_planning_review', 'planning'), ('read_body_consistency', 'body'),
                       ('read_closed_inventory', 'inventory')]:
    register_capability(Capability(_id, 1, _binding))
# This capability constructs a confirmation card only; model/proposal writes are
# explicitly outside read_capability and handled by the user-confirmed endpoint.
register_capability(Capability('prepare_body_repair',1,'pending_card',
    scope_policy='diagnosed_generation_fixed_range',
    input_schema=('diagnostic_run_id','generation_id','start','end','objective','preserve_source_ids','conflict_source_id'),
    output_schema=('pending_action',)))
# 同上：只构造卡。文字修复的卡由助理 agent 用 propose_text_fix 发起，
# 卡落到人手上之后才落库。
register_capability(Capability('prepare_text_repair',1,'pending_card',
    scope_policy='diagnosed_generation_fixed_range',
    input_schema=('diagnostic_run_id','generation_id'),
    output_schema=('pending_action',)))
# 文字质量与提示词体检：确定性规则，只读。
register_capability(Capability('inspect_text_quality', 1, 'text_quality'))
register_capability(Capability('inspect_prompt_health', 1, 'prompt_health'))


def validate_manifest(value: dict, capabilities: dict | None = None) -> dict:
    caps = CAPABILITIES if capabilities is None else capabilities
    required = {'id', 'version', 'status', 'applicable_tasks', 'agent_roles', 'required_capabilities',
                'steps', 'acceptance', 'source_evidence', 'failure_boundaries', 'allowed_actions', 'supersedes'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('invalid_manifest_fields')
    if not isinstance(value['id'], str) or not re.fullmatch(r'[a-z][a-z0-9_]*', value['id']):
        raise ValueError('invalid_manifest_id')
    if type(value['version']) is not int or value['version'] < 1:
        raise ValueError('invalid_manifest_version')
    if value['status'] not in {'experimental', 'validated', 'deprecated'}:
        raise ValueError('invalid_manifest_status')
    for key in ('applicable_tasks', 'agent_roles', 'required_capabilities', 'steps', 'acceptance',
                'source_evidence', 'failure_boundaries', 'allowed_actions'):
        if not isinstance(value[key], list):
            raise ValueError('invalid_manifest_list:' + key)
    if len(value['agent_roles']) != 1 or value['agent_roles'][0] not in ROLES \
            or not value['steps'] or not value['acceptance']:
        raise ValueError('invalid_manifest_contract')
    if value['allowed_actions'] != []:
        raise ValueError('unsupported_action')
    if value['status'] == 'validated' and (not value['source_evidence'] or any(
        not isinstance(e, dict) or not all(e.get(k) for k in
        ('run_ref', 'code_version', 'input_fingerprint', 'scope', 'provider_role', 'independent_verification'))
        for e in value['source_evidence'])):
        raise ValueError('validated_requires_evidence')
    dependencies = set()
    for dep in value['required_capabilities']:
        if (not isinstance(dep, dict) or set(dep) != {'id', 'min_version'}
                or not isinstance(dep['id'], str) or type(dep['min_version']) is not int or dep['min_version'] < 1):
            raise ValueError('invalid_capability_dependency')
        cap = caps.get(dep['id'])
        if cap is None or cap.version < dep['min_version']:
            raise ValueError('missing_capability:' + dep['id'])
        dependencies.add(dep['id'])
    seen = set()
    for step in value['steps']:
        if (not isinstance(step, dict) or set(step) != {'id', 'capability', 'requires', 'observe'}
                or not isinstance(step['requires'], list) or not isinstance(step['observe'], list)
                or step['capability'] not in dependencies or not set(step['requires']) <= seen
                or step['id'] in seen):
            raise ValueError('invalid_step')
        seen.add(step['id'])
    for item in value['acceptance']:
        if not isinstance(item, dict) or set(item) != {'id', 'requires_evidence'} or item['requires_evidence'] is not True:
            raise ValueError('invalid_acceptance')
    return value


def load_skill(skill_id: str, version: int, *, allow_experimental: bool = False,
               capabilities: dict | None = None) -> dict:
    if not isinstance(skill_id, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', skill_id) or type(version) is not int or version < 1:
        raise ValueError('invalid_skill_identifier')
    path = ROOT / skill_id / f'v{version}.json'
    if ROOT.resolve() not in path.resolve().parents:
        raise ValueError('untrusted_skill')
    # 版本/技能不存在要说清楚是「**没有这个版本**」，不是「清单坏了」。
    # 实测（真实通道）：模型传了 body_consistency v4（只有 v1/v2），
    # 旧口径报 `invalid_manifest` —— 模型以为自己把清单搞坏了，
    # 实际上只是版本号写错，报错得让它能自愈（把可用版本列出来）。
    if not path.exists():
        skill_dir = ROOT / skill_id
        if not skill_dir.is_dir():
            available = sorted(p.parent.name for p in ROOT.glob('*/v*.json'))
            raise ValueError(
                'unknown_skill:%s（可用技能：%s）' % (
                    skill_id, '、'.join(sorted(set(available))) or '无'))
        versions = sorted(p.stem for p in skill_dir.glob('v*.json'))
        raise ValueError(
            'skill_version_not_found:%s v%s（可用版本：%s）' % (
                skill_id, version, '、'.join(versions) or '无'))
    try:
        value = validate_manifest(json.loads(path.read_text(encoding='utf-8')), capabilities)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError('invalid_manifest') from exc
    if value['id'] != skill_id or value['version'] != version:
        raise ValueError('manifest_identity_mismatch')
    if value['status'] == 'deprecated' or (value['status'] == 'experimental' and not allow_experimental):
        raise ValueError('skill_status_not_allowed:' + value['status'])
    return value


def discover_skills(*, allow_experimental: bool = False) -> list[dict]:
    """Small discovery catalog, never injects step bodies."""
    result = []
    for path in sorted(ROOT.glob('*/v*.json')):
        try:
            version = int(path.stem[1:])
            m = load_skill(path.parent.name, version, allow_experimental=allow_experimental)
            result.append({k: m[k] for k in ('id', 'version', 'status', 'applicable_tasks')})
        except ValueError:
            continue
    return result
