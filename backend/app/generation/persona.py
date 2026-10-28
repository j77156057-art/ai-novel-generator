"""角色**个性档案**（persona）：说话方式 / 行为倾向 / 性格底线 / 关键经历。

## 为什么要有这个模块

实测（2026-10-28，用一本新投放的长篇语料真跑）：原创路线下，
system 提示词里的角色约束块只有三项 ——

    · 顾星河（人类最后灯塔的执灯人…；动机：先想证明守灯能够救人…）

「动机」出现 6 次，「说话」「口头禅」「口吻」出现 **0 次**。
模型没有「这个人怎么说话」的依据 ⇒ 所有角色一个腔调 ⇒ 人读着扁平。

平台**早就有**这套字段（`app/style/character_profile.py` 的六字段人设：
身份 / 说话方式 / 行为倾向 / 性格底线 / 关系网 / 关键经历），
但它绑在「参考书角色」上：`prompts._character_profile_block` 在
`original` 模式直接 `return ""` ⇒ **我们自己角色的个性没有数据通路**。

⇒ 本模块补这条通路，两条来源：

1. **合成剧情圣经时**（`wizard.build_bible`）：模型凭设定给出初始个性；
2. **写完正文后**（`derive_personas`）：从**我们自己写出来的台词**反推，
   把「想象的个性」换成「实测的个性」。

字段名与 `character_profile.py` **同形**（中文字段），所以那套
claims 约束与渲染口径能直接复用。

## 不可编造：反推结果的护栏

反推是从正文里归纳，**不许写出正文里没有的台词**。
`_quotes_in_text` 抽出结果里所有带引号的片段，逐个回查原文 ——
查不到就整条丢弃（宁可少字段，不可错字段）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

#: persona 的四个字段（与 `character_profile.py` 同形）。
PERSONA_FIELDS: tuple[str, ...] = ("说话方式", "行为倾向", "性格底线", "关键经历")

#: 字段类型：字符串 / 字符串列表。
_FIELD_LISTS = {"行为倾向", "性格底线", "关键经历"}

#: 来源标记：model=合成圣经时给的；derived=从正文反推的；user=用户自己写的。
SOURCE_MODEL = "model"
SOURCE_DERIVED = "derived"
SOURCE_USER = "user"

#: 带引号的片段（「…」 / "…" / 『…』）—— 反推结果里出现它就必须能在正文里查到。
_QUOTE_RE = re.compile(r"[「『“\"]([^」』”\"]{2,60})[」』”\"]")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _as_list(value: Any, limit: int = 6) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value] if value.strip() else []
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        items = [str(value).strip()]
    return items[:limit]


def normalize_persona(raw: Any) -> dict:
    """把模型吐的 persona 规整成四字段（缺的补空，多的丢掉）。

    只做**结构**整理，不做内容判断 —— 内容判断在 `derive_personas` 的护栏里。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for field in PERSONA_FIELDS:
        val = raw.get(field)
        if field in _FIELD_LISTS:
            items = _as_list(val)
            if items:
                out[field] = items
        else:
            text = _as_text(val)
            if text:
                out[field] = text
    return out


def persona_source(raw: Any) -> str:
    """persona 的来源标记（默认 model）。"""
    if isinstance(raw, dict):
        src = str(raw.get("来源") or raw.get("source") or "").strip()
        if src in (SOURCE_MODEL, SOURCE_DERIVED, SOURCE_USER):
            return src
    return SOURCE_MODEL


def render_persona(persona: Any) -> str:
    """把 persona 渲染成一行人话（进角色约束块）。

    形态：`说话：… ｜ 会：… ｜ 不会：… ｜ 经历：…`（缺项不出现）。
    """
    if not isinstance(persona, dict):
        return ""
    parts: list[str] = []
    voice = _as_text(persona.get("说话方式"))
    if voice:
        parts.append(f"说话：{voice}")
    beh = _as_list(persona.get("行为倾向"))
    if beh:
        parts.append("会：" + "；".join(beh[:3]))
    lines = _as_list(persona.get("性格底线"))
    if lines:
        parts.append("不会：" + "；".join(lines[:3]))
    events = _as_list(persona.get("关键经历"))
    if events:
        parts.append("经历：" + "；".join(events[:2]))
    return " ｜ ".join(parts)


def character_line(char: Any) -> str:
    """角色约束块里的一行：`· 名字（身份；动机：…）｜ 说话：… ｜ 会：…`。

    与改动前**逐字节相同**（没有 persona 时不追加任何东西）——
    这样老项目的提示词不受影响。
    """
    if not isinstance(char, dict) or not char.get("name"):
        return ""
    head = f"  · {char.get('name')}（{char.get('role') or '角色'}"
    head += (f"；动机：{char.get('motivation')}）" if char.get("motivation") else "）")
    extra = render_persona(char.get("persona"))
    if extra:
        head += " ｜ " + extra
    return head


def _quotes_in_text(text: str) -> list[str]:
    return [m.group(1).strip() for m in _QUOTE_RE.finditer(text or "") if m.group(1).strip()]


def persona_is_grounded(persona: Any, source_text: str) -> bool:
    """persona 里带引号的片段是否都能在原文里查到。

    带引号的片段最可能是「编造的台词」⇒ 查不到就说明它没依据。
    """
    if not isinstance(persona, dict):
        return False
    haystack = source_text or ""
    for field in PERSONA_FIELDS:
        val = persona.get(field)
        text = "；".join(val) if isinstance(val, (list, tuple)) else _as_text(val)
        for q in _quotes_in_text(text):
            if q and q not in haystack:
                return False
    return True


def _drop_ungrounded(persona: dict, source_text: str) -> dict:
    """逐字段丢掉带「查不到的引文」的字段。"""
    out: dict[str, Any] = {}
    for field, val in persona.items():
        text = "；".join(val) if isinstance(val, (list, tuple)) else _as_text(val)
        bad = False
        for q in _quotes_in_text(text):
            if q and q not in source_text:
                bad = True
                break
        if not bad:
            out[field] = val
    return out


def _safe_json(raw: Any) -> Any:
    """从模型输出里抠出 JSON（容忍 ```json 包裹与前后废话）。"""
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def derive_personas(provider: Any, sources: dict[str, list[str]]) -> dict[str, dict]:
    """从**已经写出来的正文**反推一批角色的写作特征（**一次调用**）。

    只认正文里有依据的；带引号的片段查不到原文 ⇒ 整条丢弃。

    Args:
        provider: LLM provider（None ⇒ 直接返回空）。
        sources: `{角色名: [该角色在正文里出现过的片段]}`。

    Returns:
        `{角色名: persona}`（四字段，可能缺项；不含「来源」——由调用方补）。
    """
    items = {n: [s for s in (snips or []) if s] for n, snips in (sources or {}).items()}
    items = {n: v for n, v in items.items() if n and v}
    if not provider or not items:
        return {}
    try:
        sys_p = (
            "你从**已经写出来的正文**里，归纳这些角色的写作特征，供后续续写保持同一个人。\n"
            "只写正文里有依据的；没有依据的字段给空数组，不要编。\n"
            "不要写正文里没有过的台词、动作或经历；引号里的内容必须逐字出现在正文里。\n\n"
            "每个角色要抽的四个字段：\n"
            "1. **说话方式**：自称、句长倾向、语气底色、口头禅（正文里真出现过的）。\n"
            "2. **行为倾向**：遇到具体情境他会怎么做——要给「情境 → 反应」，不是形容词。\n"
            "3. **性格底线**：什么事他不会做、什么话他不会说（用否定式）。\n"
            "4. **关键经历**：决定他现在状态的事（正文里真发生过的），按时间顺序。\n\n"
            "输出 JSON：\n"
            '{"characters": [{"name": "角色名", "说话方式": "...", "行为倾向": ["..."], '
            '"性格底线": ["..."], "关键经历": ["..."]}]}\n'
            "正文片段里没有依据的字段就写空数组。"
        )
        body_parts = []
        for name, snips in items.items():
            body_parts.append(f"—— 角色「{name}」的正文片段 ——\n" + "\n".join(snips))
        user = "\n\n".join(body_parts) + "\n\n按上述 JSON 输出（每个角色一条）。"
        raw = provider.chat(sys_p, user)
        parsed = _safe_json(raw)
        out: dict[str, dict] = {}
        for item in (parsed.get("characters") or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            persona = normalize_persona(item)
            if not persona:
                continue
            source_text = "\n".join(items.get(name) or [])
            if source_text and not persona_is_grounded(persona, source_text):
                persona = _drop_ungrounded(persona, source_text)
            if persona:
                out[name] = persona
        return out
    except Exception:  # noqa: BLE001 —— 反推失败绝不能阻断生成
        return {}


def character_snippets(text: str, name: str, limit: int = 6, window: int = 180) -> list[str]:
    """从正文里取某角色出现过的片段（名字周围的窗口）。

    不用 KG（异步、可能还没抽完）—— 直接从正文找，最可靠也最省事。
    """
    if not text or not name:
        return []
    out: list[str] = []
    i = text.find(name)
    while i >= 0 and len(out) < limit:
        out.append(text[max(0, i - window): i + len(name) + window])
        i = text.find(name, i + len(name))
    return out


# ====================================================================
# 人写角色 + AI 帮着完善（2026-10-28）
# ====================================================================
#
# 用户的诉求原话：「让用户可以自己写角色的性格，自己来决定出现的角色应该有什么样的
# 表现，再由 ai 来帮忙完善」。
#
# 落成三条规则：
#
# 1. **人写的归人**：用户写进 `persona` 的内容标 `来源=user`
#    ⇒ 反推（`refresh_personas`）**不覆盖**它（既有行为）。
# 2. **AI 只补不改**：完善时模型只被允许输出「**补充**」，
#    合并规则是「用户原文 + AI 补充」⇒ 人写的内容永远保留。
# 3. **AI 不落库**：`complete_persona` 只返回建议，要不要采纳由人点。

#: 来源标记：用户自己写的（受保护，反推与完善都不覆盖）。
SOURCE_USER_OWNED = SOURCE_USER


def _merge_field(field: str, user_value: Any, ai_extra: Any) -> Any:
    """合并「用户原文」与「AI 补充」：**用户原文在前，AI 只追加**。

    AI 常常把用户写的那句**原样重述一遍再往下写**（「句子短，从不解释；句子短，从不解释；不用感叹句…」）。
    拼接前先把 AI 补充里与用户原文重复的那部分**去掉**，否则提示词里会出现同一句话两遍。
    """
    if field in _FIELD_LISTS:
        user_items = _as_list(user_value)
        extra_items = _as_list(ai_extra)
        out = list(user_items)
        for x in extra_items:
            if x and x not in out:
                out.append(x)
        return out[:8]
    user_text = _as_text(user_value)
    extra_text = _as_text(ai_extra)
    if not extra_text:
        return user_text
    if not user_text:
        return extra_text
    if extra_text in user_text:
        return user_text
    # 去掉 AI 补充里**重复用户原文**的那一段（只去开头那一处常见的重述）。
    if extra_text.startswith(user_text):
        tail = extra_text[len(user_text):].lstrip("；;，, ")
        if not tail:
            return user_text
        extra_text = tail
    return user_text + "；" + extra_text


def merge_persona(user_persona: Any, ai_extra: Any) -> dict:
    """把 AI 的补充并进用户写的 persona（用户原文优先，AI 只追加）。"""
    user_norm = normalize_persona(user_persona) if user_persona else {}
    ai_norm = normalize_persona(ai_extra) if ai_extra else {}
    out: dict[str, Any] = {}
    for field in PERSONA_FIELDS:
        merged = _merge_field(field, user_norm.get(field), ai_norm.get(field))
        if merged:
            out[field] = merged
    return out


def save_persona(db: Any, project: Any, name: str, persona: Any) -> dict:
    """把用户写的角色个性写进剧情圣经（**只动 persona，不碰 name/role/motivation**）。

    标 `来源=user` ⇒ 之后的「从正文反推」不会覆盖它。
    返回写进去的那份（调用方拿它刷新界面）。
    """
    from sqlalchemy.orm.attributes import flag_modified

    bible = project.plot_bible
    if isinstance(bible, str):
        try:
            bible = json.loads(bible)
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(bible, dict):
        return {}
    chars = bible.get("characters")
    if not isinstance(chars, list):
        return {}
    target = None
    for c in chars:
        if isinstance(c, dict) and str(c.get("name") or "") == str(name or ""):
            target = c
            break
    if target is None:
        return {}
    normalized = normalize_persona(persona)
    if normalized:
        normalized["来源"] = SOURCE_USER
    target["persona"] = normalized
    # JSON 列必须显式标脏（见 refresh_personas 里那段说明）
    project.plot_bible = json.loads(json.dumps(bible, ensure_ascii=False))
    flag_modified(project, "plot_bible")
    db.commit()
    return normalized


def complete_persona(provider: Any, character: dict, user_persona: Any = None,
                      note: str = "") -> dict:
    """AI 帮着完善角色个性 —— **只返回建议，不落库**。

    模型被明确要求：**只能在用户已写的内容后面补充，不得推翻**；
    输出字段是「补充」而不是「改写」。

    Returns:
        `{"补充": {...四字段...}, "说明": "..."}`；失败时 `{}`。
    """
    if not provider:
        return {}
    name = str((character or {}).get("name") or "")
    if not name:
        return {}
    user_norm = normalize_persona(user_persona) if user_persona else {}
    try:
        sys_p = (
            "你在帮作者完善一个角色的**写作档案**。作者已经写了一些，"
            "你的任务是**在他写的内容基础上补充**，不是替换。\n"
            "硬要求：\n"
            "1. **绝不推翻作者写的**——他写过的部分要原样保留，你的补充写在后面；\n"
            "2. 作者没写的字段，你给出具体内容；\n"
            "3. 不要编造正文里没依据的台词；引号里的内容必须来自给定片段；\n"
            "4. 行为倾向要给「情境 → 反应」，性格底线用否定式，关键经历要具体到事件。\n\n"
            "要补的四个字段：\n"
            "1. **说话方式**：自称、句长倾向、语气底色、口头禅。\n"
            "2. **行为倾向**：遇到具体情境他会怎么做。\n"
            "3. **性格底线**：什么事他不会做、什么话他不会说。\n"
            "4. **关键经历**：决定他现在状态的事。\n\n"
            "输出 JSON：\n"
            '{"补充": {"说话方式": "...", "行为倾向": ["..."], "性格底线": ["..."], '
            '"关键经历": ["..."]}, "说明": "一句话说明你补了什么"}'
        )
        user = ("角色档案：\n" + json.dumps({
            "name": name,
            "身份": (character or {}).get("role") or "",
            "动机": (character or {}).get("motivation") or "",
        }, ensure_ascii=False)
            + ("\n\n作者已经写好的部分（**保留，只在其后补充**）：\n"
               + json.dumps(user_norm, ensure_ascii=False))
            + (("\n\n作者的要求：\n" + note[:400]) if note else ""))
        raw = provider.chat(sys_p, user)
        parsed = _safe_json(raw)
        extra = normalize_persona(parsed.get("补充") if isinstance(parsed, dict) else None)
        if not extra:
            return {}
        # 护栏：AI 的补充里带引号的片段必须能在**已知材料**里查到。
        haystack = json.dumps({"c": character, "u": user_norm}, ensure_ascii=False)
        if not persona_is_grounded(extra, haystack):
            extra = _drop_ungrounded(extra, haystack)
        if not extra:
            return {}
        return {"补充": extra, "说明": str((parsed or {}).get("说明") or "")[:300]}
    except Exception:  # noqa: BLE001 —— 完善失败不该让用户写的东西丢掉
        return {}


def has_complete_personas(bible: Any) -> bool:
    """剧情圣经里的角色是不是**都已经有「说话方式」**。

    用来决定「要不要多花一次模型调用去补齐」——模型通常在合成圣经时
    就给齐了，这时不该白付一次调用。没有角色时返回 True（没什么可补）。
    """
    chars = (bible or {}).get("characters") if isinstance(bible, dict) else None
    if not isinstance(chars, list) or not chars:
        return True
    for c in chars:
        if not isinstance(c, dict):
            continue
        persona = c.get("persona") if isinstance(c.get("persona"), dict) else {}
        if not str(persona.get("说话方式") or "").strip():
            return False
    return True


def refresh_personas(db: Any, project: Any, provider: Any, *,
                     max_segments: int = 6) -> int:
    """从已写正文反推角色个性，回写剧情圣经（**fail open**：失败不阻断生成）。

    触发条件：
    - 项目**已有正文**（没有正文就没有可归纳的依据）；
    - 该角色的 persona 缺失，或它的时间戳早于最新一段正文（说明正文更新了）。

    写入：只动 `characters[i].persona`，**不动** name / role / motivation（用户写的权威）；
    `来源=derived` 的字段覆盖 `来源=model` 的同名字段（实测值比想象值可信）。

    取最近 `max_segments` 段正文（整批取 6 段 ≈ 1.8 万字，够看出口癖与句长；
    再多只增加 token，不会让结论更准）。
    """
    try:
        from app.models import Generation

        bible = project.plot_bible
        if isinstance(bible, str):
            import json as _json
            try:
                bible = _json.loads(bible)
            except Exception:  # noqa: BLE001
                return 0
        if not isinstance(bible, dict):
            return 0
        chars = bible.get("characters") or []
        if not chars:
            return 0

        gens = db.query(Generation).filter(Generation.project_id == project.id).order_by(
            Generation.segment_no.desc()).limit(max_segments).all()
        if not gens:
            return 0
        text = "\n".join((g.text or "") for g in reversed(gens))
        if not text.strip():
            return 0
        # `Generation` **没有时间列**（只有 id / segment_no / text / summary）——
        # 「正文更新了没有」只能用**段号指纹**判断，不能用时间戳。
        # （2026-10-28 踩过：用 g.created_at 会 AttributeError，被 fail-open 的
        #  except 吞成「更新了 0 个角色」，看上去像「反推没结果」。）
        latest = max(int(g.segment_no or 0) for g in gens)
        stamp = f"seg{latest}"
        stale: list[dict] = []
        sources: dict[str, list[str]] = {}
        for c in chars:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            persona = c.get("persona") if isinstance(c.get("persona"), dict) else {}
            if persona.get("来源") == SOURCE_USER:
                continue  # 用户自己写的，不动
            updated = str(persona.get("更新于") or "")
            if updated and updated == stamp:
                continue  # 已按这一段正文反推过（段号指纹相同）
            snips = character_snippets(text, str(c.get("name")))
            if not snips:
                continue
            stale.append(c)
            sources[str(c.get("name"))] = snips
        if not stale or not sources:
            return 0

        derived = derive_personas(provider, sources)
        if not derived:
            return 0
        # 段号指纹（不是时间戳）：`Generation` 没有时间列，见上面。
        now = stamp
        changed = 0
        for c in stale:
            name = str(c.get("name") or "")
            new = derived.get(name)
            if not new:
                continue
            old = c.get("persona") if isinstance(c.get("persona"), dict) else {}
            merged = dict(old)
            merged.update(new)
            merged["来源"] = SOURCE_DERIVED
            merged["更新于"] = now
            c["persona"] = merged
            changed += 1
        if changed:
            # —— JSON 列必须**显式标脏**（2026-10-28，踩了两轮）——
            # `plot_bible` 是 `Column(JSON)`；赋值一个新对象后，SQLAlchemy 的
            # 变更检测**不一定**认得出它变了（同 session 里有时读到新值、但 UPDATE
            # 语句不发出）⇒ commit 静默等于什么都没写，而这里 return 的是
            # changed>0 ⇒ 调用方以为「更新了 5 个角色」，下一段的提示词里一个都没有。
            # 实测：插探针看到 `changed=5 / bible 有 persona: 5`，
            # 而 commit 之后新连接读库仍是 0。改用 flag_modified 显式标脏。
            from sqlalchemy.orm.attributes import flag_modified

            project.plot_bible = json.loads(json.dumps(bible, ensure_ascii=False))
            flag_modified(project, "plot_bible")
            db.commit()
        return changed
    except Exception:  # noqa: BLE001 —— 反推/回写失败绝不能阻断生成
        return 0
