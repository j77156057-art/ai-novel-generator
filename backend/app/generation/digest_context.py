"""全书摘要（BookDigest）的**消费**侧：什么时候能进上下文、怎么裁。

## 三条纪律（每条都对应一个真实会出事的地方）

1. **只在「参考书就是用户正在读/续的那本」的路径下注入**（续写、角色聊天）。

   原创路径（参考书只是**写法**参考）**不注入** —— 摘要里装的是**这本书的剧情**，
   把它塞进原创的提示词，模型会照着参考书的剧情写。
   仓库里已有同类教训：叙事规则里的证据引文带着参考小说的专名，
   实测每维前 3 条共 255 处人名泄漏 ⇒ 模型复读参考小说。

2. **可见边界必须 respected**（fail closed，与 `generation/visibility.py` 同口径）。

   续写/角色聊天路径下，参考书就是用户正在读的那本 ⇒ 没设边界 = 整本不可见。
   ⇒ 摘要里超出边界的条目一律丢掉；**没有边界的书整本不参与**。

3. **有界**：摘要可能几万字，进提示词必须有预算。
   超预算时按「时间线在前、角色在后」的优先级截断，并如实说明截断了多少。

## 为什么这三条不是「过度保守」

摘要的**价值**恰恰在于「替代整本进上下文」——
续写时模型缺的是「这本书前面发生了什么」，而原文要几万字，摘要只要几千字。
但它同时是**剧透源**：边界没守住，模型就会写到用户还没读到的情节。
⇒ 边界这条是硬约束，不是可选项。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 注入提示词的摘要上限（字）。
#:
#: 为什么这么小：它是**补充**上下文，不是主体。主体是「上一段正文 + 前情摘要」。
#: 给到 1200 字，模型能拿到「这本书有谁、发生过什么」，
#: 又不至于把提示词预算吃光（提示词每轮都在，缓存命中率比摘要详细度更值钱）。
MAX_DIGEST_CHARS = 1200

#: 检索摘要条目时最多取几条。
MAX_DIGEST_HITS = 5


def _cutoff_map(project, novel_ids: list[int]) -> dict[int, int]:
    """项目上给每本书设的「故事进行到第几章」边界。

    复用 `VisibilityPolicy` 的解析（同一份口径），不要自己再解析一遍。
    """
    from app.generation.visibility import VisibilityPolicy

    policy = VisibilityPolicy(project, list(novel_ids), unbounded_paths=frozenset())
    return dict(policy.cutoffs)


def _load_digests(db, novel_ids: list[int]) -> list[Any]:
    """取这些书的最新 active 摘要（按传入顺序）。"""
    from app.style.digest import latest_digest

    rows = []
    for nid in novel_ids or []:
        row = latest_digest(db, int(nid))
        if row is not None:
            rows.append(row)
    return rows


def _entry_chapter(entry: dict) -> int | None:
    """条目挂在哪一章（时间线用 chapter_no，角色/反应用证据里的章号）。"""
    ch = entry.get("chapter_no")
    if isinstance(ch, int) and not isinstance(ch, bool):
        return ch
    for ev in entry.get("evidence") or []:
        if isinstance(ev, dict):
            c = ev.get("chapter_no")
            if isinstance(c, int) and not isinstance(c, bool):
                return c
    return None


def _within_cutoff(entry: dict, cutoff: int | None) -> bool:
    """条目是否在可见边界内。

    `cutoff is None` ⇒ 这本书**没有边界** ⇒ 在续写/角色聊天路径下整本不可见
    （fail closed，与检索链一致）⇒ 返回 False。
    """
    if cutoff is None:
        return False
    ch = _entry_chapter(entry)
    if ch is None:
        # 挂不上章号的条目（模型没给章号）：**不放行**。
        # 放行等于「一个不知道来自哪一章的内容进了提示词」，
        # 而边界的作用就是保证「用户还没读到的内容不进上下文」。
        return False
    return ch <= cutoff


def _entry_text(entry: dict) -> str:
    """条目的一行文本（检索与渲染都用它）。"""
    if not isinstance(entry, dict):
        return ""
    parts: list[str] = []
    for key in ("event", "name", "role", "arc", "character", "trigger", "says", "does"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "；".join(parts)


def digest_entries(
    db,
    project,
    novel_ids: list[int],
    *,
    kinds: tuple[str, ...] = ("timeline", "characters"),
    cutoffs: dict[int, int] | None = None,
) -> list[dict]:
    """取本书摘要里**在可见边界内**的条目。

    Args:
        kinds: 要哪几块（`timeline` / `characters` / `reactions`）。
        cutoffs: 可见边界；不给就按项目上的 `reference_cutoffs` 现算
            （与检索链同一把尺）。角色聊天那条链自己已经算好了边界，
            传进来即可 —— **两边必须是同一份**，否则会出现
            「检索按第 10 章裁、摘要按第 50 章裁」的错位。

    Returns:
        `[{"novel_id":…, "kind":…, "entry":…, "text":…}]`；
        没有摘要或没有边界的书不产出任何条目。
    """
    if cutoffs is None:
        cutoffs = _cutoff_map(project, novel_ids)
    out: list[dict] = []
    for row in _load_digests(db, novel_ids):
        payload = row.payload or {}
        cutoff = cutoffs.get(row.novel_id)
        for kind in kinds:
            for entry in payload.get(kind) or []:
                if not _within_cutoff(entry, cutoff):
                    continue
                text = _entry_text(entry)
                if not text:
                    continue
                out.append({
                    "novel_id": row.novel_id,
                    "kind": kind,
                    "entry": entry,
                    "text": text,
                    "chapter_no": _entry_chapter(entry),
                })
    return out


def digest_block(
    db,
    project,
    novel_ids: list[int],
    *,
    profile: str,
    budget_chars: int = MAX_DIGEST_CHARS,
) -> str:
    """生成要注入提示词的摘要块（**原创路径恒为空**）。

    原文路径（`profile == "original"`）返回空串 ——
    理由见模块 docstring 第一条纪律。
    """
    from app.generation.profiles import normalize_profile

    if normalize_profile(profile) == "original":
        return ""
    try:
        entries = digest_entries(db, project, novel_ids, kinds=("timeline", "characters"))
    except Exception as exc:  # noqa: BLE001 —— 摘要是增强，绝不能挡住生成
        logger.warning("摘要块构建失败（忽略）: %s", exc)
        return ""
    if not entries:
        return ""

    lines: list[str] = []
    used = 0
    truncated = 0
    for item in entries:
        line = f"· {item['text']}"
        if used + len(line) > budget_chars:
            truncated += 1
            continue
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    head = "【参考书背景摘要】以下是参考书的结构化摘要（时间线与主要角色），用于保持前后一致：\n"
    tail = (
        f"\n（另有 {truncated} 条摘要因篇幅未列出）" if truncated else ""
    )
    return head + "\n".join(lines) + tail


def digest_character_block(
    db,
    project,
    novel_ids: list[int],
    name: str,
    *,
    cutoffs: dict[int, int] | None = None,
    max_reactions: int = 6,
) -> str:
    """某个角色的生平与「情境 → 反应」（角色对话用）。

    这是摘要**最直接**的用处：`reactions` 就是「他面对这种事会怎么说」，
    而这正是角色聊天链路一直缺的（检索只能找到他**说过**的话，
    找不到他**会怎么反应**）。

    边界口径与生成链路一致：没有边界的书整本不可见。
    """
    if not name:
        return ""
    try:
        entries = digest_entries(
            db, project, novel_ids, kinds=("characters", "reactions"), cutoffs=cutoffs
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("角色摘要块构建失败（忽略）: %s", exc)
        return ""

    bio_lines: list[str] = []
    reaction_lines: list[str] = []
    for item in entries:
        entry = item["entry"]
        if item["kind"] == "characters":
            if entry.get("name") != name:
                continue
            bits = [entry.get("role") or "", entry.get("arc") or ""]
            line = "；".join(b for b in bits if b)
            if line:
                bio_lines.append(f"· {line}")
        else:
            if entry.get("character") != name:
                continue
            parts = [entry.get("trigger") or ""]
            if entry.get("says"):
                parts.append(f"会说「{entry['says']}」")
            if entry.get("does"):
                parts.append(f"会{entry['does']}")
            line = "｜".join(p for p in parts if p)
            if line:
                reaction_lines.append(f"· {line}")
    if not bio_lines and not reaction_lines:
        return ""

    out = []
    if bio_lines:
        out.append(f"【{name}的生平摘要】\n" + "\n".join(bio_lines))
    if reaction_lines:
        shown = reaction_lines[:max_reactions]
        out.append(
            f"【{name}的情境反应】以下是他在类似处境下的反应线索：\n"
            + "\n".join(shown)
            + (f"\n（另有 {len(reaction_lines) - len(shown)} 条未列出）"
               if len(reaction_lines) > len(shown) else "")
        )
    return "\n\n".join(out)


def retrieve_digest_entries(
    db,
    novel_id: int,
    query: str,
    *,
    k: int = MAX_DIGEST_HITS,
    cutoff: int | None = None,
) -> list[dict]:
    """从一本书的摘要里检索相关条目（替代「检索整本原文」）。

    打分口径很粗：**查询里的字在条目文本里出现得越多越相关**。
    为什么不接 BM25：摘要条目只有几十上百条，词频统计没有意义；
    而且这里的查询是从前情里拼出来的短句，粗打分足够用。

    边界：`cutoff=None` ⇒ **不放行任何条目**（fail closed）。
    """
    from app.style.digest import latest_digest

    row = latest_digest(db, int(novel_id))
    if row is None:
        return []
    payload = row.payload or {}
    q = (query or "").strip()
    if not q:
        return []
    qchars = {ch for ch in q if ch.strip() and ch not in "的了在是我你他和她也这那"}
    hits: list[dict] = []
    for kind in ("timeline", "characters", "reactions"):
        for entry in payload.get(kind) or []:
            if not _within_cutoff(entry, cutoff):
                continue
            text = _entry_text(entry)
            if not text:
                continue
            score = sum(1 for ch in qchars if ch in text)
            if score <= 0:
                continue
            hits.append({
                "novel_id": int(novel_id),
                "kind": kind,
                "text": text,
                "score": score,
                "chapter_no": _entry_chapter(entry),
                "source": "digest",
            })
    hits.sort(key=lambda h: -h["score"])
    return hits[:k]
