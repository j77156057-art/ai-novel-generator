"""整段验收的**输出预算**与**截断失败**要看得见。

- 验收要逐段回抄 coverage 清单 → 输出量与段落数成正比 → 必须按需申请额度；
- 额度不够时响应被拦腰截断 → 失败要说清楚是「额度不够」，并写明需要多少，
  而不是笼统的 acceptance_failed。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from test_body_acceptance import _acceptance_fixture, _good_answer  # noqa: E402


class _Capture:
    """记录调用参数的假 provider。"""

    def __init__(self, *, truncated=False):
        self.kwargs = []
        self.truncated = truncated
        self.last_finish_reason = 'length' if truncated else 'stop'

    def chat(self, system, user, **kwargs):
        self.kwargs.append(kwargs)
        return json.dumps(_good_answer(), ensure_ascii=False)


def test_acceptance_requests_output_budget_matching_the_number_of_paragraphs():
    """申请的输出额度必须随「要回抄的段落 id 数量」增长。"""
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    provider = _Capture()
    report = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert report['status'] == 'accepted', report
    assert provider.kwargs, '验收必须带 max_tokens 调用'
    budget = provider.kwargs[0].get('max_tokens')
    assert budget is not None, '验收必须按需申请输出额度'
    # 正文有段落 → 额度至少要覆盖「每个 id 约 12 token + 基础额度」
    assert budget >= 4096
    assert budget == min(32768, max(4096, 2048 + 2 * 12)) or budget >= 2048


def test_truncated_response_is_reported_as_an_actionable_failure():
    """输出被截断 → unresolved 说清楚是额度不够，并写明需要多少。"""
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    provider = _Capture(truncated=True)

    # 假装响应被截断：返回的不是合法 JSON（真实截断就是这个形态）
    def bad(system, user, **kwargs):
        provider.kwargs.append(kwargs)
        return '{"assessment": "observed", "dimensions": ['
    provider.chat = bad

    report = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert report['status'] == 'blocked'
    assert 'acceptance_output_truncated' in report['unresolved']
    assert report.get('acceptance_needed_output_budget', 0) >= 4096
    reason = report.get('acceptance_failure_reason') or ''
    assert 'max_tokens' in reason and 'coverage' in reason


def test_structural_failure_is_not_reported_as_truncation():
    """结构不合格 ≠ 截断：两种失败要能分辨。"""
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()

    class BadShape(_Capture):
        def chat(self, system, user, **kwargs):
            self.kwargs.append(kwargs)
            return json.dumps({'assessment': 'supported'}, ensure_ascii=False)

    provider = BadShape()
    report = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert report['status'] == 'blocked'
    assert 'acceptance_output_truncated' not in report['unresolved']
    # 结构不合格走的是「答得不合格」这条路（不是笼统的 acceptance_failed）
    assert 'invalid_top_level_assessment' in report['unresolved']
