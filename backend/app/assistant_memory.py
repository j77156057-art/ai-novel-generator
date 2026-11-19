"""助理的**记忆**：短期对话上文 + 项目级长期记忆。

## 两类记忆，两种用途

1. **短期**：`assistant_messages` 里最近几轮问答。只用来理解指代
   （「它」「上一段」「那本书」），让同一会话能接着上文说。不落新表——
   对话流本来就已经在存了。
2. **长期**：`assistant_memories` 里的**自动抽取**结论
   （「这本书用第一人称」「别写电话号码」）。条数少、寿命长、每次都进
   提示词。

## 长期记忆为什么是自动抽取，且只抽「陈述句」

用户不需要先说「记住」。每轮问答结束后由 Assistant 模型做一次有界抽取：
只留**用户明确表达、以后仍然成立**的偏好或设定，一次性的请求（「这次写
第 3 段」）不算。

两个约束把成本和噪声都压住：

- **只对陈述句跑**：问句（以「？」结尾、含「怎么/为什么/是不是」）直接跳过，
  省掉一次模型调用——查资料不会产生长期偏好；
- **有界输出**：一次最多 2 条、单条 40 字，抽取提示词里带上已有记忆并要求
  去重。抽错了可以在「记忆」抽屉里删掉。

## 失败策略

记忆是**增强**，不是主链路：抽取失败、模型不可用、库写不进去，都只记日志，
绝不让一次问答因此 500（与 `assistant_store` 同口径）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import AssistantMemory, AssistantMessage

logger = logging.getLogger(__name__)

#: 每个项目能存多少条长期记忆。超过就拒绝写入并提示先删——
#: 静默淘汰会让用户以为「它记着」，实际却被系统丢掉了。
MAX_MEMORIES = 60

#: 单条记忆的最大长度（超出截断，不报错）。
MAX_TEXT_CHARS = 300

#: 每次问答最多带多少条记忆进提示词（按创建顺序取前面的）。
PROMPT_MEMORIES = 40

#: 记忆块整体的字符上限，防止长期记忆把上下文挤爆。
PROMPT_BLOCK_CHARS = 2000

#: 带进提示词的最近对话轮数（一问一答算一轮）。
RECENT_TURNS = 3

#: 只有在「这句话需要靠上文理解」时才拼最近对话。
#: 为什么要这道闸：把上文无条件塞进提示词，弱模型会**模仿上一条回复**
#: （实测：问一句新设定，它回了一句上一轮的「已从记忆里删除…」）。
#: 上文的用途只有一个——解析指代；不需要指代时它只会带来污染。
_REFERENTIAL = re.compile(
    r"(它|它们|这个|那个|这段|那段|这一?[章段]|上面|刚才|上一条|上轮|"
    r"前面|接着|继续|再写|改成|那句)"
)

#: 单条历史消息在提示词里的最大长度。
RECENT_MESSAGE_CHARS = 160

#: 一轮最多自动抽取几条（多出来的多半是复述）。
AUTO_EXTRACT_MAX = 2

#: 抽取结果单条的字数上限（超出丢掉，不截断——半句话记下来只会误导）。
AUTO_ITEM_CHARS = 40

#: 判断「这句话是不是陈述」用的问句信号。
_QUESTION_SIGNALS = re.compile(
    r"(怎么|为什么|如何|是不是|多少|哪一?[个段章]|能不能|可不可以|有没有)"
)
#: 检索/查看类祈使句：没有问号，但也不产生长期偏好。
_LOOKUP_SIGNALS = re.compile(r"(帮我|看看|查一下|找一下|读一下|念一下|讲一下)")
#: 偏好的措辞：出现这些词就按陈述处理（即使句子里带「第 N 段」）。
_PREFERENCE_SIGNALS = re.compile(
    r"(以后|之后|一直|始终|永远|每次|都要|不要|不能|必须|别再|别写|别用|不准|禁止)"
)
#: 连续空白压成一个空格（存库时保持单行）。
_WS = re.compile(r"\s+")

_AUTO_SYSTEM = (
    "你是写作助手的记忆整理器。从给定的一轮问答里，抽取**值得长期记住**的"
    "稳定偏好或设定：写作口径（人称/视角/时态）、称呼与别名、禁忌与红线、"
    "读者定位、篇幅节奏等。\n"
    "规则：\n"
    "1. 只抽用户明确表达、以后仍然成立的信息；一次性请求（这次写第几段、"
    "这一段怎么改）不要抽。\n"
    "2. 不要抽助手自己说的话，不要抽问题的答案。\n"
    "3. 已有记忆里出现过的不再重复。\n"
    "4. 每条一句话，不超过 40 字，不写「用户说」之类的前缀。\n"
    "5. 没有可记的就返回空数组。\n"
    "只输出 JSON 数组，例如：[\"这本书用第一人称\"]"
)

_MERGE_SYSTEM = (
    "你是记忆管理员。把「已有记忆」和「候选记忆」合并成一份**最终清单**。\n"
    "规则：\n"
    "1. 已有记忆的原文尽量保持不变（不要改写措辞）。\n"
    "2. 判重看**意思**，不看字面：候选只要说的是同一个约束就丢弃。\n"
    "   例：已有「对话使用第一人称」，候选「这书采用第一人称」＝重复，丢弃。\n"
    "   例：已有「避免提及电话号码」，候选「禁忌：不写电话号码」＝重复，丢弃。\n"
    "3. 候选之间也按同一口径去重，保留最清楚的一条。\n"
    "4. 每条不超过 40 字，最多 {limit} 条。\n"
    "只输出 JSON 数组（最终清单），例如：[\"对话使用第一人称\"]"
)


def normalize_text(value: str) -> str:
    """存库/去重共用的规范化：压空白、去首尾引号与标点。"""
    text = _WS.sub(" ", (value or "").strip())
    return text.strip(" 　\"'“”‘’")


def should_extract(query: str) -> bool:
    """这句话值不值得跑一次抽取。

    只对**陈述句**抽取：问句与检索类祈使句基本不会产生长期偏好，跳过能省掉
    大多数模型调用（用户在助手页的主要行为是提问）。带「以后/一直/别写」
    这类措辞的句子即使夹着「第 N 段」，仍按陈述处理。
    """
    text = (query or "").strip()
    if len(text) < 6:
        return False
    if _PREFERENCE_SIGNALS.search(text):
        return True
    if text.endswith(("?", "？")):
        return False
    if _QUESTION_SIGNALS.search(text):
        return False
    if _LOOKUP_SIGNALS.search(text):
        return False
    return True


def _json_items(raw: str, limit: int) -> list[str] | None:
    """解析模型回复里的 JSON 数组。

    返回 `None` = 根本没解析出数组（模型没按格式回）；`[]` = 合法空数组。
    两者必须分开：前者应退回启发式结果，后者是「确实没有新增」。
    """
    text = (raw or "").strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    items: list[str] = []
    for item in data[: max(1, limit) * 3]:
        if not isinstance(item, str):
            continue
        value = normalize_text(item)
        if not value or len(value) > AUTO_ITEM_CHARS:
            continue
        items.append(value)
    return items[: max(1, limit)]


def parse_extracted(raw: str, limit: int = AUTO_EXTRACT_MAX) -> list[str]:
    """从模型回复里取 JSON 数组；取不到就返回空列表（不猜、不按行硬拆）。

    模型偶尔会裹一层 ```json 代码块或前后加解释，所以只截取最外层的
    `[ ... ]`，解析失败就放弃这一轮——宁可少记，也不把半句解释写进记忆。
    """
    return _json_items(raw, limit) or []


def list_memories(
    db: Session, project_id: int, limit: int = MAX_MEMORIES
) -> list[AssistantMemory]:
    """按写入顺序取该项目的长期记忆（最早的在前）。"""
    rows = db.scalars(
        select(AssistantMemory)
        .where(AssistantMemory.project_id == project_id)
        .order_by(AssistantMemory.id)
        .limit(max(1, min(int(limit), MAX_MEMORIES)))
    ).all()
    return list(rows)


def remember(
    db: Session, project_id: int, text: str, *, source: str = "auto"
) -> tuple[AssistantMemory | None, bool, str]:
    """写入一条长期记忆。

    返回 `(行, 是否新建, 原因)`：重复内容返回已有行且 `新建=False`；
    条数到上限时返回 `(None, False, "full")`，由调用方决定怎么提示。
    """
    value = normalize_text(text)[:MAX_TEXT_CHARS]
    if len(value) < 2:
        return None, False, "empty"

    existing = db.scalars(
        select(AssistantMemory).where(
            AssistantMemory.project_id == project_id,
            AssistantMemory.text == value,
        )
    ).first()
    if existing is not None:
        return existing, False, "duplicate"

    count = db.scalar(
        select(func.count())
        .select_from(AssistantMemory)
        .where(AssistantMemory.project_id == project_id)
    ) or 0
    if count >= MAX_MEMORIES:
        return None, False, "full"

    row = AssistantMemory(project_id=project_id, text=value, source=source)
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
    except Exception:  # noqa: BLE001 - 记忆写失败不该拖垮问答
        logger.exception("助理记忆写入失败 project_id=%s", project_id)
        db.rollback()
        return None, False, "error"
    return row, True, "created"


def delete_memory(db: Session, project_id: int, memory_id: int) -> bool:
    """按 id 删除一条（幂等：不存在返回 False）。"""
    row = db.get(AssistantMemory, memory_id)
    if row is None or row.project_id != project_id:
        return False
    try:
        db.delete(row)
        db.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("助理记忆删除失败 id=%s", memory_id)
        db.rollback()
        return False


def _needs_dialog(query: str | None) -> bool:
    """这句话是否需要靠上文才能读懂（指代词/承接语/极短句）。"""
    if query is None:
        return True
    text = (query or "").strip()
    if not text:
        return True
    if len(text) <= 12:
        # 「继续」「为什么」这类短句几乎一定依赖上文。
        return True
    return bool(_REFERENTIAL.search(text))


def _recent_dialog(
    db: Session, project_id: int, *, exclude_text: str | None = None
) -> list[tuple[str, str]]:
    """最近几轮普通问答（一问一答配对），按时间正序。"""
    limit = RECENT_TURNS * 2
    rows = db.scalars(
        select(AssistantMessage)
        .where(
            AssistantMessage.project_id == project_id,
            AssistantMessage.kind == "plain",
            AssistantMessage.role.in_(("user", "assistant")),
        )
        .order_by(AssistantMessage.id.desc())
        .limit(limit + 1)
    ).all()
    ordered = list(reversed(rows))
    if exclude_text:
        # 当前这句还没落库；但如果同一句刚问过（库里已有「用户问 + 助手答」），
        # 那一对属于**本轮**而不是上文，必须排掉——否则提示词里会出现
        # 「用户：X」和「问题：X」两遍，模型容易把旧答案当成本轮结论。
        tail = normalize_text(exclude_text)
        if (
            len(ordered) >= 2
            and ordered[-1].role == "assistant"
            and ordered[-2].role == "user"
            and normalize_text(ordered[-2].text) == tail
        ):
            ordered = ordered[:-2]
        while ordered and ordered[-1].role == "user" and normalize_text(ordered[-1].text) == tail:
            ordered.pop()
    return [(row.role, row.text) for row in ordered[-limit:]]


def prompt_block(
    db: Session,
    project_id: int,
    *,
    query: str | None = None,
    exclude_text: str | None = None,
) -> str:
    """拼「长期记忆 + 最近对话」两段提示词。取不到就返回空串。

    `query` 给了就按需决定要不要带最近对话：只有出现指代词/承接语时才带。
    长期记忆无条件带——它是长期生效的约束，不是上下文。
    """
    parts: list[str] = []
    try:
        memories = list_memories(db, project_id, limit=PROMPT_MEMORIES)
    except Exception:  # noqa: BLE001 - 读记忆失败不影响问答
        logger.exception("助理记忆读取失败 project_id=%s", project_id)
        memories = []
    if memories:
        lines = ["【长期记忆】（从以往对话里自动记住的偏好，回答时默认遵守，不要逐条复述）"]
        lines.extend(f"- {row.text}" for row in memories)
        parts.append("\n".join(lines))

    try:
        dialog = (
            _recent_dialog(db, project_id, exclude_text=exclude_text)
            if _needs_dialog(query)
            else []
        )
    except Exception:  # noqa: BLE001
        logger.exception("助理对话上下文读取失败 project_id=%s", project_id)
        dialog = []
    if dialog:
        lines = [
            "【最近对话】（只用来理解指代；不要模仿、不要复述其中助手的答复，"
            "也不要把它当成本轮结论）"
        ]
        for role, text in dialog:
            who = "用户" if role == "user" else "助手"
            body = normalize_text(text)[:RECENT_MESSAGE_CHARS]
            lines.append(f"{who}：{body}")
        parts.append("\n".join(lines))

    block = "\n\n".join(parts)
    if len(block) > PROMPT_BLOCK_CHARS:
        block = block[:PROMPT_BLOCK_CHARS].rstrip() + "\n…（记忆已截断）"
    return block


def _existing_for_prompt(db: Session, project_id: int) -> str:
    try:
        rows = list_memories(db, project_id, limit=PROMPT_MEMORIES)
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return "（暂无）"
    return "\n".join(f"- {row.text}" for row in rows)


def extract_candidates(
    db: Session,
    project_id: int,
    *,
    query: str,
    answer: str,
    provider: Any,
    force: bool = False,
) -> list[str]:
    """跑一次抽取，只返回候选文本（不写库）。

    抽出来给两条路用：问答后的 `auto_extract` 直接写入；一次性回填脚本先
    预览再决定写不写。两条路共用同一份提示词与解析，避免「预览的和实际写的
    不是一回事」。

    `force=True` 跳过「只抽陈述句」的闸门（回填历史时用：历史里混着提问，
    但提问里也可能带设定，交给模型判断比启发式更准）。
    """
    if provider is None:
        return []
    if not force and not should_extract(query):
        return []
    if not (answer or "").strip():
        return []
    user = (
        f"已有记忆（不要重复）：\n{_existing_for_prompt(db, project_id)}\n\n"
        f"本轮用户说：{normalize_text(query)[:500]}\n"
        f"本轮助手答：{normalize_text(answer)[:500]}"
    )
    try:
        raw = provider.chat(_AUTO_SYSTEM, user)
    except Exception as exc:  # noqa: BLE001 - 抽取失败不影响本次问答
        logger.warning("助理记忆自动抽取失败 project_id=%s：%s", project_id, exc)
        return []
    return parse_extracted(raw if isinstance(raw, str) else str(raw or ""))


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（两条都 ≤40 字，DP 足够快）。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, other in enumerate(b, 1):
            if ch == other:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _looks_same(a: str, b: str) -> bool:
    """两条记忆是不是在说同一个约束（确定性兜底，补模型判重的漏）。

    模型做语义合并时会漏掉「这本书用第一人称」vs「对话使用第一人称」这类
    改写；这里用「最长公共子串 ≥4 字且占较短一条的 35%」拦一道。
    阈值刻意不能太低：`主角叫陈小满` 与 `主角叫王大勇` 公共前缀只有 3 个字，
    低于 4 就不会被误判成同一条。
    """
    left, right = normalize_text(a), normalize_text(b)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    shared = _lcs_len(left, right)
    return shared >= 4 and shared >= 0.35 * min(len(left), len(right))


def merge_candidates(
    existing: list[str],
    candidates: list[str],
    provider: Any,
    *,
    limit: int = MAX_MEMORIES,
) -> list[str]:
    """把一批候选和已有记忆合并成**一份最终清单**（不是只回新增项）。

    为什么让模型回整份清单、再由调用方做差集：只让它回「新增哪些」时，
    它会把「对话使用第一人称」的同义改写「这书按第一人称写」当成新条目；
    而「把已有 + 候选合并成一份清单」是更自然的任务，模型倾向保留已有原文。

    模型不可用/没按格式回时退回「已有 + 精确去重后的候选」。
    """
    current = [normalize_text(text) for text in existing if normalize_text(text)]
    seen = set(current)
    unique: list[str] = []
    for item in candidates:
        value = normalize_text(item)
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    fallback = (current + unique)[:limit]
    if not unique:
        return current[:limit]
    if provider is None:
        return fallback

    user = (
        "已有记忆：\n"
        + ("\n".join(f"- {text}" for text in current) or "（暂无）")
        + "\n\n候选记忆：\n"
        + "\n".join(f"- {text}" for text in unique)
    )
    try:
        raw = provider.chat(_MERGE_SYSTEM.format(limit=limit), user)
    except Exception as exc:  # noqa: BLE001 - 合并失败退回精确去重
        logger.warning("助理记忆合并失败：%s", exc)
        return fallback
    merged = _json_items(raw if isinstance(raw, str) else str(raw or ""), limit)
    if merged is None:
        return fallback
    # 模型也会漏判：对「已有清单里没有的新条目」再过一遍确定性相似度，
    # 同义改写一律丢掉，宁可少记一条，也不让同一个约束占两行。
    existing_set = set(current)
    result: list[str] = []
    for item in merged:
        value = normalize_text(item)
        if not value:
            continue
        if value in existing_set:
            if value not in result:
                result.append(value)
            continue
        if any(_looks_same(value, text) for text in existing_set):
            continue
        if value not in result:
            result.append(value)
    return result[:limit]


def auto_extract(
    db: Session,
    project_id: int,
    *,
    query: str,
    answer: str,
    provider: Any,
) -> list[AssistantMemory]:
    """一轮问答结束后自动抽取长期记忆，返回本轮新增的行。

    同步执行（问答返回前完成），因为记忆必须在下一轮就生效；用陈述句
    启发式把大多数提问挡在外面，额外开销只落在「用户在陈述设定」的轮次。
    """
    items = extract_candidates(
        db, project_id, query=query, answer=answer, provider=provider
    )
    added: list[AssistantMemory] = []
    for item in items:
        row, created, reason = remember(db, project_id, item, source="auto")
        if created and row is not None:
            added.append(row)
        elif reason == "full":
            logger.info("助理记忆已满，跳过 project_id=%s", project_id)
            break
    return added


def row_to_dict(row: AssistantMemory) -> dict[str, Any]:
    return {
        "id": row.id,
        "text": row.text,
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
