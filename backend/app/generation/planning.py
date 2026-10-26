"""Explicit causal planning drafts stored inside the existing timeline JSON."""
from typing import Annotated, Literal
import hashlib
import json
from pydantic import Field, StringConstraints
from app.schemas import StrictRequest
from app.generation.timeline import PlotNode, TimelineState, state_to_dict

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]

UNKNOWN_VALUES = frozenset({"unknown", "待定", "未定", "未说明", "未指定"})
PLANNING_FIELDS = ("premise", "motivation", "action", "result", "time",
                   "location", "participants", "state_before", "state_after")

#: 规划调用的限次重试上限。模型偶尔会返回不合格式的 JSON（实测 3 次里失败 2 次），
#: 一次不成就把整份草案判成 needs_planning —— 与蒸馏层「限次重试、保留原工件」的纪律不一致。
MAX_PLANNING_ATTEMPTS = 3

#: 重试时追加的纠错提示。**不重发同一个提示词**：模型并不知道自己上次错在哪，
#: 原样重试只是赌它下次不犯同样的错（实测 15 次真实调用里 6 次首次返回不合格式，
#: 其中一次连错两回）。把「哪里不合格」告诉它，第二次调用才是真正不同的请求。
_RETRY_HINT = (
    "\n\n—— 纠正上一次不合格式的输出 ——\n"
    "你上一次的返回被丢弃了，因为它没有通过校验（{detail}）。请重新输出，"
    "并严格满足：只输出一个 JSON 对象本身（不要 ```json 代码块、不要任何解释文字、"
    "不要在字段之间多写逗号，也不要多写括号或引号）；"
    "要求的字段一个都不能少，数量必须与要求完全一致。"
)


def _safe_detail(exc: Exception) -> str:
    """只回传**安全**的失败信息：异常类型 + JSON 语法错误的位置 / 缺失字段名。

    绝不回显原始响应（可能含异常串），也不回显 pydantic 的输入回显
    （ValidationError 的消息里带模型原文）。
    """
    detail = getattr(exc, 'detail', None)
    if isinstance(detail, str) and detail:
        return detail          # 异常自带的「安全摘要」（如照搬命中数）
    if isinstance(exc, json.JSONDecodeError):
        return f'{type(exc).__name__} @ char {exc.pos}'
    errors = getattr(exc, 'errors', None)
    if callable(errors):
        try:
            items = errors() or []
        except Exception:  # noqa: BLE001 - 拿不到就只报类型
            items = []
        locs = []
        for item in items[:5]:
            loc = '.'.join(str(x) for x in (item.get('loc') or ()))
            kind = item.get('type') or ''
            locs.append(f'{loc}({kind})' if loc else kind)
        if locs:
            return f'{type(exc).__name__}: ' + ', '.join(locs)
    return type(exc).__name__


def model_json(provider, system: str, user: str, parse, *,
               attempts: int = MAX_PLANNING_ATTEMPTS):
    """带限次重试的模型 JSON 调用。

    返回 (结果, 失败记录列表)。每次失败记下错误分类与安全细节（**不回显原始
    响应**，它可能含异常串）；成功即停。重试用尽后由调用方按失败类型决定错误码。

    重试不是原样重发：格式失败后会把失败分类与位置追加到 user 末尾再问一次，
    让模型知道上一次错在哪。请求失败（模型压根没回）时不追加提示——
    那不是模型的错，换一次请求即可。
    """
    import time

    total = max(1, attempts)
    failures: list[dict] = []
    current_user = user
    for index in range(1, total + 1):
        try:
            raw = provider.chat(system, current_user)
        except Exception as exc:  # noqa: BLE001 - 单次失败不放弃整轮
            failures.append({'attempt': index, 'code': 'model_request_failed',
                             'type': type(exc).__name__})
            continue
        try:
            return parse(raw), failures
        except Exception as exc:  # noqa: BLE001
            detail = _safe_detail(exc)
            # 失败可以自带分类（照搬 → possible_copy）与自己的纠错提示
            # （把命中的片段回灌给模型，让它自己改写）。
            code = getattr(exc, 'code', None) or 'invalid_model_structure'
            record = {'attempt': index, 'code': code, 'type': type(exc).__name__,
                      'detail': detail}
            matches = getattr(exc, 'matches', None)
            if matches:
                # 只留「维度 + 命中的逐字片段」，够用户看懂命中了什么；
                # 不带 rule_excerpt（那是一大段参考原文）。
                record['matches'] = [{'dimension': m.get('dimension'),
                                      'shared_text': m.get('shared_text')}
                                     for m in list(matches)[:5]]
            failures.append(record)
            if index < total:
                hint = getattr(exc, 'retry_hint', None)
                current_user = user + (hint if hint else _RETRY_HINT.format(detail=detail))
                time.sleep(0.5)  # 还要再试，才值得等；最后一次失败不用空等
    return None, failures

class CausalNode(StrictRequest):
    title: Text
    premise: Text
    motivation: Text
    action: Text
    result: Text
    cost: Text
    time: Text
    location: Text
    participants: Annotated[list[Text], Field(min_length=1)]
    state_before: Text
    state_after: Text

class OutlineRequest(StrictRequest):
    confirm_bible: Literal[True]
    goals: Annotated[list[Text], Field(min_length=1)]
    node_count: int = Field(default=3, ge=3, le=12)
    preview: bool = True

class OutlineApply(StrictRequest):
    candidate: dict

class OutlineClarify(OutlineApply):
    answers: dict

class BudgetRevision(StrictRequest):
    source_fingerprint: Text
    node_id: Text
    segment_budget: int = Field(strict=True, ge=1, le=100)
    reason: Text
    confirm: Literal[True]

SYSTEM = '''你是小说剧情规划编辑。根据 premise、已确认剧情圣经、作者目标，输出因果相连的场景节点。
严格只输出 JSON {"nodes":[...]}。每个节点必须有以下字段：title,premise（事件前提）,motivation（人物为何行动）,action,result,cost,time,location,participants（字符串数组）,state_before,state_after；除participants外均为字符串。
未知信息明确写 unknown；不适用的代价写 NA。不强迫冲突或代价，不编造已知事实。给出具体可判定的行动结果与前后状态，节点间解释时间流逝、通行时间、信息获得方式和人物位置。
若给了 plot_structure，那是**这位作者的剧情结构手法**（节拍顺序、场景展开套路、伏笔与钩子）：
用它来安排节点的推进顺序、场景怎么展开、在哪里埋钩子；但**不得照搬其中的人物、地名与具体情节**。
遵守人物白名单与时间地点边界，不增加幕后组织、死亡或超自然。结构完整只是草案，不能冒充剧情已执行。'''

def source_fingerprint(project):
    source={'premise':project.premise,'bible':project.plot_bible,'outline':project.outline,'timeline':project.plot_timeline}
    return hashlib.sha256(json.dumps(source,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def validate_nodes(raw, count=None):
    if not isinstance(raw,list) or not 3 <= len(raw) <= 12 or (count is not None and len(raw)!=count):
        raise ValueError('invalid node count')
    return [CausalNode.model_validate(n).model_dump() for n in raw]


def _is_unknown(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() in UNKNOWN_VALUES
    if isinstance(value, list):
        return not value or any(_is_unknown(item) for item in value)
    return False


def _unknown_fields(nodes: list[dict]) -> list[dict]:
    missing = []
    for index, node in enumerate(nodes):
        for field in PLANNING_FIELDS:
            if _is_unknown(node.get(field)):
                missing.append({"node_id": f"n{index + 1}", "field": field,
                                "value": node.get(field)})
    return missing


def build_clarification_report(nodes: list[dict], source_fingerprint: str) -> dict:
    """Turn missing planning premises into an explicit, non-verifying result."""
    unknown = _unknown_fields(nodes)
    clarifications = []
    for item in unknown:
        field = item["field"]
        clarifications.append({
            "question": f"请明确节点 {item['node_id']} 的{field}。",
            "why_required": "正文生成和连续性验收需要该设定；模型不得替用户决定。",
            "dependent_nodes": [item["node_id"]],
            "existing_evidence": [],
            "authorized_fabrication": False,
            "field": field,
        })
    return {
        "status": "clarification_required" if unknown else "outline_draft",
        "structurally_complete": not unknown,
        "verified": False,
        "unknown_fields": unknown,
        "clarifications": clarifications,
        "source_fingerprint": source_fingerprint,
        "candidate_version": 1,
    }


def validate_planning_candidate(candidate: dict, current_fingerprint: str) -> dict:
    if candidate.get("source_fingerprint") != current_fingerprint:
        raise ValueError("planning candidate fingerprint is stale")
    nodes = validate_nodes(candidate.get("nodes"))
    report = build_clarification_report(nodes, current_fingerprint)
    return {**report, "nodes": nodes}


def submit_clarification_candidate(candidate: dict, answers: dict, current_fingerprint: str) -> dict:
    """Apply explicit user answers to a draft while keeping verification honest."""
    if not isinstance(answers, dict):
        raise ValueError("clarification answers must be an object")
    checked = validate_planning_candidate(candidate, current_fingerprint)
    allowed = {f"{item['node_id']}.{item['field']}" for item in checked['unknown_fields']}
    if set(answers) - allowed:
        raise ValueError("answers must address current clarification fields")
    nodes = [dict(node) for node in checked["nodes"]]
    for item in checked["unknown_fields"]:
        key = f"{item['node_id']}.{item['field']}"
        if key in answers:
            node = nodes[int(item["node_id"][1:]) - 1]
            node[item["field"]] = answers[key]
    nodes = validate_nodes(nodes)
    report = build_clarification_report(nodes, current_fingerprint)
    return {**candidate, **report,
            "nodes": nodes, "outline": [node["title"] for node in nodes],
            "apply_state": "candidate_only"}

def timeline_for(nodes, metadata, total_segments):
    order=[f'n{i+1}' for i in range(len(nodes))]
    mapped={}
    for nid,n in zip(order,nodes):
        entry=f"时间：{n['time']}；地点：{n['location']}；参与者：{'、'.join(n['participants'])}；前提：{n['premise']}；动机：{n['motivation']}；起始状态：{n['state_before']}"
        exit=f"行动：{n['action']}；结果：{n['result']}；代价：{n['cost']}；结束状态：{n['state_after']}"
        mapped[nid]=PlotNode(nid,'main',n['title'],'',entry,exit,segment_budget=max(total_segments//len(nodes),1))
    state=TimelineState(mapped,order,total_main=len(order),current_node=order[0],planning={**metadata,'nodes':nodes})
    return state_to_dict(state)
