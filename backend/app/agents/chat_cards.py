"""从助手回答里生成「待确认卡片」（只构造，不执行）。

剧情讨论走的是纯流式问答，不做工具循环；但如果模型在回答里**建议了一个
写操作**，界面就该像原型那样给一张确认卡，而不是只留一句「要不要我……」。

两种来源：

1. **生成邀请**：回答里出现「要不要我写下一段 / 点下面的生成下一段」这类
   提议 → 生成 `invoke_generation` 卡（续写一段，不覆盖任何正文）。
2. **设定修改**：模型按提示词约定，在回答末尾单独一行输出
   `【写设定】{"character": "...", "field": "...", "value": "..."}` →
   折叠成 `proposal` 卡（fact_resolution，改 plot_bible 里已有角色的字符串
   字段）。标记会从正文剥掉，用户看到的是干净的讨论内容。

## 纪律

- 只提案，不执行：卡片确认后仍走既有 `/api/assistant/proposals` 或
  `/api/assistant/invoke-generation`。
- 宁可漏提，不可错提：角色不存在、字段不是已有字符串、值越界，一律不提卡。
- 值来自模型，但落库的是**用户确认时看到的那一版**；服务端不替用户改设定。
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from app.agents.pending_actions import build_pending_action

#: 回答里「提议落笔」的信号。只认明确的邀请句，不认单纯提到「下一段」。
_GENERATION_INVITE = re.compile(
    r"(?:要不要我|需要我|我可以|是否要|要我)"
    r"[^。！？\n]{0,16}?(?:写|生成|续写|落笔|开始写)"
    r"|(?:点(?:击)?|按)下面的?[「\"']?(?:生成下一段|继续写|写下一段)",
)
#: 模型按提示词约定输出的设定修改标记。
#: 全角与半角方括号都收：实测模型会写成 `[写设定]`。
_BIBLE_MARKERS: tuple[str, ...] = ("【写设定】", "[写设定]")

#: 单次允许改的字段层级上限（card.身份.身份职业 → 3 段）。
_MAX_FIELD_DEPTH = 3
#: 新值长度上限：角色卡字段是一句话，不该塞整段正文。
_MAX_VALUE_CHARS = 400
#: 改设定提案的 after_text 上限（与服务端提案文本上限保持同一量级）。
_MAX_BIBLE_CHARS = 20000


class ActionMarkerFilter:
    """把末尾的【写设定】标记从流里扣住，避免半截 JSON 闪给用户。

    标记一定在回答末尾；一旦出现，后续内容全部暂存，等收尾时统一解析。
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> str:
        self._buf += text
        idx = _first_marker_at_line_start(self._buf)
        if idx != -1:
            out = self._buf[:idx]
            self._buf = self._buf[idx:]
            return out
        # 末尾可能是「行首标记」的前缀（被 token 边界切开）→ 先扣住。
        # 行内出现的 `【写设定】`（模型在讨论这个标记）不能扣，否则会把
        # 后半句解释吞掉，用户看到的就是一句话被拦腰截断。
        keep = _partial_marker_len(self._buf)
        if keep and _line_prefix_ok(self._buf, len(self._buf) - keep):
            out = self._buf[:-keep]
            self._buf = self._buf[-keep:]
            return out
        out = self._buf
        self._buf = ""
        return out

    def flush(self) -> str:
        """结束时把暂存的尾巴原样交给调用方。

        标记若在末尾，调用方会通过 `split_bible_marker` 的返回把它剥掉；
        标记没按约定放末尾时，这段尾巴必须补发，不能吞掉正文。
        """
        buf = self._buf
        self._buf = ""
        return buf


def _first_marker(text: str) -> int:
    """最早出现的标记下标；没有返回 -1。"""
    indexes = []
    for marker in _BIBLE_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            indexes.append(idx)
    return min(indexes) if indexes else -1


def _line_prefix_ok(text: str, idx: int) -> bool:
    """标记是否位于行首（该行前面只有空白）。

    提示词约定标记必须**单独一行**。模型在解释这个标记时会写成
    ``...末尾那行`【写设定】{...}`...``——那是行内引用，不是提议，
    不能当成卡片把后半句解释切掉。
    """
    line_start = text.rfind("\n", 0, idx) + 1
    return text[line_start:idx].strip(" \t\r") == ""


def _first_marker_at_line_start(text: str) -> int:
    """最早出现的「行首」标记下标；行内引用一律忽略。"""
    indexes: list[int] = []
    for marker in _BIBLE_MARKERS:
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx == -1:
                break
            if _line_prefix_ok(text, idx):
                indexes.append(idx)
                break
            start = idx + 1
    return min(indexes) if indexes else -1


def _partial_marker_len(text: str) -> int:
    """末尾与任一标记前缀重合的最长长度（用于流式扣字）。"""
    best = 0
    for marker in _BIBLE_MARKERS:
        for size in range(1, len(marker)):
            if text.endswith(marker[:size]):
                best = max(best, size)
    return best


def split_bible_marker(
    answer: str,
) -> tuple[str, dict[str, Any] | None, bool]:
    """剥掉【写设定】/ [写设定] 标记，返回 `(正文, 标记负载, 是否出现标记)`。

    模型常把标记写成 `【写设定】{...}。`（末尾多一个句号）或后面跟一句
    说明，所以这里不看「是否恰好位于末尾」，而是：找到标记 → 从标记后的
    第一个 `{` 到最后一个 `}` 取 JSON → 标记及其后内容全部从正文剥掉。

    JSON 不合法或不是对象时只剥标记、不提卡（fail closed）——绝不让
    半截 JSON 留在回答里，也不猜模型想改什么。
    """
    text = answer or ""
    idx = _first_marker_at_line_start(text)
    if idx == -1:
        return text, None, False
    marker = next(m for m in _BIBLE_MARKERS if text.startswith(m, idx))
    clean = text[:idx].rstrip()
    raw = text[idx + len(marker):]
    start, end = raw.find("{"), raw.rfind("}")
    payload: dict[str, Any] | None = None
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    return clean, payload, True


def build_generation_card(
    *, project_id: int, query: str, answer: str
) -> dict[str, Any] | None:
    """回答里明确邀请落笔时，提出一张「生成下一段」确认卡。"""
    tail = (answer or "")[-600:]
    if not _GENERATION_INVITE.search(tail):
        return None
    instruction = (query or "").strip() or "续写下一段"
    return build_pending_action(
        kind="invoke_generation",
        title="生成下一段（确认后才执行）",
        detail={
            "action": "generate_next",
            "segment_no": None,
            "project_id": int(project_id),
            "destructive": False,
            "risk_hint": "新增一段，不覆盖任何现有正文",
            "instruction": instruction,
        },
        body={
            "project_id": int(project_id),
            "action": "generate_next",
            "instruction": instruction,
        },
    )


def _resolve_character(bible: dict[str, Any], name: str) -> dict[str, Any] | None:
    characters = bible.get("characters")
    if not isinstance(characters, list):
        return None
    for item in characters:
        if isinstance(item, dict) and str(item.get("name") or "") == name:
            return item
    return None


def _set_nested(character: dict[str, Any], field: str, value: str) -> tuple[bool, str]:
    """按点路径改已有字符串字段；返回 `(是否成功, 旧值)`。"""
    keys = [part for part in str(field or "").split(".") if part]
    if not keys or len(keys) > _MAX_FIELD_DEPTH:
        return False, ""
    node: Any = character
    for key in keys[:-1]:
        if not isinstance(node, dict) or key not in node:
            return False, ""
        node = node[key]
    if not isinstance(node, dict) or keys[-1] not in node:
        return False, ""
    old = node[keys[-1]]
    # 只改已有字符串字段：不新增键、不把列表/对象替换成一句话。
    if not isinstance(old, str):
        return False, ""
    node[keys[-1]] = value
    return True, old


def build_bible_edit_card(
    *, project: Any, payload: dict[str, Any] | None
) -> dict[str, Any] | None:
    """把【写设定】标记折成一张改 plot_bible 的提案卡；不可信就不提。"""
    if not isinstance(payload, dict):
        return None
    name = str(payload.get("character") or "").strip()
    field = str(payload.get("field") or "").strip()
    value = str(payload.get("value") or "").strip()
    if not name or not field or not value:
        return None
    if len(value) > _MAX_VALUE_CHARS:
        return None

    bible = project.plot_bible if isinstance(project.plot_bible, dict) else None
    if not bible:
        return None
    character = _resolve_character(bible, name)
    if character is None:
        return None

    new_bible = copy.deepcopy(bible)
    new_character = _resolve_character(new_bible, name)
    if new_character is None:
        return None
    ok, old = _set_nested(new_character, field, value)
    if not ok or old == value:
        return None

    after_text = json.dumps(new_bible, ensure_ascii=False)
    if len(after_text) > _MAX_BIBLE_CHARS:
        return None

    return build_pending_action(
        kind="proposal",
        title=f"改角色卡：{name} · {field}（确认后才写入）",
        detail={
            "type": "fact_resolution",
            "target_kind": "project_plot_bible",
            "target_id": "plot_bible",
            "character": name,
            "field": field,
            "old_value": old,
            "new_value": value,
            "requires_confirmation": True,
        },
        body={
            "project_id": int(project.id),
            "type": "fact_resolution",
            "target_kind": "project_plot_bible",
            "target_id": "plot_bible",
            "after_text": after_text,
            "reason": f"按对话结论改角色卡：{name} · {field}",
            # 改的是权威设定层，不按「低风险」处理。
            "risk_level": "medium",
        },
    )
