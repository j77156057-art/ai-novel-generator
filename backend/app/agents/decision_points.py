"""工序链里需要**用户决断**的岔路口（决策点）。

## 为什么是「选项」而不是「问一句」

工序链走到某一步时，有些岔路口**不能由系统替作者决定**：蒸馏用哪一档、
每段写多少字、一次生成几章。这些决定直接影响成本、节奏与产物形态，
猜错的代价远大于多问一句。

但「问一句」也不够 —— 模型自由发挥会问出没法点、没法复用的开放问题。
所以这里是**选项集**：每个选项带 id、标题、说明，以及它对应的请求体补丁
（`body_patch`）。用户在界面上点一个即可；前端把选中的补丁合并进请求体
**原样回传** —— 请求体始终由服务端拼好，前端不参与构造。

## 与既有卡片的关系

`app.agents.pending_actions` 的卡片契约是
`{kind, title, detail, endpoint, method, body}`。选择框是同一套契约，
多一个 `kind='choice'` 与 `detail.options`；端点由**决策点注册表**给出
（和别的 kind 由 kind 表给出一样，服务端说了算，前端不参与）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Option:
    """一个可选项。

    `body_patch` 是选中它时要合并进请求体的字段（服务端已经知道每个选项
    会带来什么参数，前端不需要也不允许自己拼）。
    """

    id: str
    label: str
    description: str = ''
    body_patch: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionPoint:
    """一个决策点：题目 + 选项集 + 选完之后要打的端点与基础请求体。"""

    key: str
    title: str
    question: str
    endpoint: str
    body: dict[str, Any]
    options: tuple[Option, ...]
    step_id: str = ''          # 属于工序链的哪一步
    method: str = 'POST'
    #: 用户提到这些词时，才把这个决策点摆出来（确定性，不调模型）。
    triggers: tuple[str, ...] = ()


#: 通用启动词：用户说「开始 / 走流程」时，把链上**当前**的决策点摆出来。
GENERIC_START_WORDS: tuple[str, ...] = (
    '开始', '启动', '做起来', '走流程', '下一步', '接下来', '继续做',
)


# ====================================================================
# 决策点注册表（唯一真源：选项、端点、请求体都在这里）
# ====================================================================

#: 蒸馏档位：完整 / 平衡 / 快速。
#: 语义见 app.style.narrative 的档位注释；这里只管「让人选」。
DISTILL_TIER = DecisionPoint(
    key='distill_tier',
    title='文风蒸馏用哪一档？',
    question='蒸馏档位决定提炼多少语料：越完整越准也越慢越费额度。',
    endpoint='/api/projects/{project_id}/distill',
    body={'tier': ''},
    step_id='distill',
    triggers=('蒸馏', '文风', '档位', '提炼', '仿写', '学习', '风格档案'),
    options=(
        Option('full', '完整档',
               '全量语料提炼，最准；耗时长、额度消耗最大。',
               {'tier': 'full'}),
        Option('balanced', '平衡档',
               '采样语料提炼，效果与成本居中（多数项目选它）。',
               {'tier': 'balanced'}),
        Option('fast', '快速档',
               '少量采样试跑，先看方向再决定要不要跑完整档。',
               {'tier': 'fast'}),
    ),
)

#: 每段写多少字。
SEGMENT_LENGTH = DecisionPoint(
    key='segment_length',
    title='每段写多少字？',
    question='每段字数决定节奏与额度：字数越多，每段越完整，也越费额度。',
    endpoint='/api/projects/{project_id}',
    body={'generation_length': ''},
    step_id='review_body',
    method='PATCH',
    triggers=('字数', '每段', '多长', '篇幅'),
    options=(
        Option('1000', '1000 字',
               '短段快、试跑用；同一场景会被拆得更碎。',
               {'generation_length': 1000}),
        Option('2000', '2000 字',
               '常见选择：一段一个完整场景。',
               {'generation_length': 2000}),
        Option('3000', '3000 字',
               '长段，场景更完整；额度消耗也最大。',
               {'generation_length': 3000}),
    ),
)

#: 一次生成几章。
CHAPTERS_PER_RUN = DecisionPoint(
    key='chapters_per_run',
    title='一次生成几章？',
    question='一次多章省事，但一旦不满意要整批重来；一章一章来更稳。',
    endpoint='/api/assistant/invoke-generation',
    body={'instruction': '', 'segments': ''},
    step_id='review_body',
    triggers=('生成', '续写', '写', '继续', '几章', '一次'),
    options=(
        Option('1', '一次 1 章',
               '最稳：每章都能先看再决定要不要继续。',
               {'segments': 1}),
        Option('3', '一次 3 章',
               '省事：一次给一段完整的小高潮。',
               {'segments': 3}),
        Option('5', '一次 5 章',
               '最快：适合已经定好方向的批量推进。',
               {'segments': 5}),
    ),
)

DECISION_POINTS: dict[str, DecisionPoint] = {
    p.key: p for p in (DISTILL_TIER, SEGMENT_LENGTH, CHAPTERS_PER_RUN)
}

#: 工序 id → 该工序上的决策点（按顺序）。工序链走到这一步时，
#: 按用户说了什么挑一个匹配的问；都没匹配上就不问（别拿无关的问题打断人）。
DECISION_BY_STEP: dict[str, tuple[str, ...]] = {
    'distill': (DISTILL_TIER.key,),
    'write_body': (CHAPTERS_PER_RUN.key, SEGMENT_LENGTH.key),
}


def point_matches_query(point: DecisionPoint, query: str) -> bool:
    # 这个决策点该不该在**此刻**摆出来（确定性，不调模型）：
    #   - 用户提到该决策点的触发词 → 摆；
    #   - 用户说「开始 / 走流程 / 下一步」这类通用启动词 → 摆链上当前的决策点。
    # 为什么要判定词：不然每条消息都弹同一个选择框，
    # 用户问「第 3 段有什么问题」也被问「蒸馏用哪一档」—— 那是噪音，不是帮助。
    text = query or ''
    if any(word in text for word in point.triggers):
        return True
    return any(word in text for word in GENERIC_START_WORDS)


def decision_for_step(step_id: str, query: str = '') -> DecisionPoint | None:
    # 某道工序需要用户决断时的决策点（没有则 None）。
    # query 给了就按触发词挑最贴的那一个；没给就取第一个。
    keys = DECISION_BY_STEP.get(step_id) or ()
    points = [DECISION_POINTS[k] for k in keys if k in DECISION_POINTS]
    if not points:
        return None
    if not query:
        return points[0]
    for point in points:
        if point_matches_query(point, query):
            return point
    return None


def build_choice_action(point: DecisionPoint, *, project_id: int,
                        body_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """组装一张「选择框」卡片（用户点一个选项即可继续）。

    端点与请求体都由**决策点注册表**给出 —— 前端只负责把用户选中的
    `body_patch` 合并进 `body` 后原样回传。
    """
    if not point.options:
        raise ValueError(f'决策点 {point.key!r} 没有任何选项')
    body = dict(point.body)
    body['project_id'] = int(project_id)
    if body_extra:
        body.update(body_extra)
    endpoint = point.endpoint.format(project_id=int(project_id))
    return {
        'kind': 'choice',
        'title': point.title,
        'detail': {
            'question': point.question,
            'decision_key': point.key,
            'step_id': point.step_id,
            'options': [
                {
                    'id': o.id,
                    'label': o.label,
                    'description': o.description,
                    'body_patch': dict(o.body_patch),
                }
                for o in point.options
            ],
        },
        'endpoint': endpoint,
        'method': point.method,
        'body': body,
    }
