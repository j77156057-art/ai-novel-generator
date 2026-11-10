"""助理在引导里的**受控写操作**：规划、执行、审计、撤销。

边界（产品红线，代码里逐条落实）：

- 只改**当前项目**的项目级配置与本轮会话状态：
  `project.narrative_tier / narrative_sample_chapters / generation_length /
  genre / style / outline`、`session.answers`、`session.overrides`。
- 不改全局配置（LLM provider / 设置项）、不改代码、不访问其他项目、
  不删除或覆盖已有正文（Generation / 物品持有表完全不碰）。
- 流程改写走白名单：只能跳过 `SKIPPABLE`、只能在非受保护步骤间换序、
  只能插入字段已登记（`CUSTOM_FIELDS`）的自定义问题。
- 每次写操作落一行 `WizardActionAudit`，回复里给出「旧值 → 新值 + 影响」，
  除「触发蒸馏」外都可撤销（任务已起跑，撤销只能恢复参数）。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app import wizard_facts as facts
from app import wizard_flow as flow
from app.models import WizardActionAudit

# ---------------------------------------------------------------------------
# 动作清单（也是报告里的「动作清单表」）
# ---------------------------------------------------------------------------

ACTIONS: dict[str, dict[str, Any]] = {
    "set_tier": {
        "target": "project.narrative_tier",
        "params": "tier ∈ {full, balanced, fast}",
        "confirm": False,
        "undoable": True,
        "note": "只改偏好，当前档案不变；下次蒸馏生效。",
    },
    "set_sampling": {
        "target": "project.narrative_sample_chapters",
        "params": "chapters = 0（全本）或 1..语料总章数",
        "confirm": False,
        "undoable": True,
        "note": "进蒸馏指纹，改完必须重跑；回执给预计耗时。",
    },
    "trigger_distill": {
        "target": "distill run",
        "params": "force: bool",
        "confirm": True,
        "undoable": False,
        "note": "后台任务，不可取消；撤销只能恢复参数。",
    },
    "set_length": {
        "target": "project.generation_length",
        "params": f"{flow.LENGTH_MIN}..{flow.LENGTH_MAX}",
        "confirm": False,
        "undoable": True,
        "note": "下一段生成生效，不改已写正文。",
    },
    "set_genre": {
        "target": "project.genre",
        "params": "flow.GENRE_HINTS 的键",
        "confirm": False,
        "undoable": True,
        "note": "影响后续生成与大纲。",
    },
    "set_style": {
        "target": "project.style",
        "params": "flow.STYLE_HINTS 的键",
        "confirm": False,
        "undoable": True,
        "note": "影响后续生成的叙述节奏。",
    },
    "set_outline": {
        "target": "session.answers.outline",
        "params": "bool",
        "confirm": False,
        "undoable": True,
        "note": "只影响本轮引导提交。",
    },
    "skip_step": {
        "target": "session.overrides.skip",
        "params": "flow.SKIPPABLE 且非 PROTECTED_STEPS 的步骤",
        "confirm": False,
        "undoable": True,
        "note": "该步按默认值处理；不改已选答案。",
    },
    "move_step": {
        "target": "session.overrides.order",
        "params": "两个可动步骤（非受保护、非当前步）",
        "confirm": False,
        "undoable": True,
        "note": "只换顺序，不增删步骤。",
    },
    "insert_question": {
        "target": "session.overrides.custom",
        "params": "field ∈ {premise}，最多 1 个，问题 ≤200 字",
        "confirm": False,
        "undoable": True,
        "note": "答案写进项目简介；流程末尾会显示在汇总里。",
    },
}

FORBIDDEN = (
    "改全局配置（模型/provider/设置项）",
    "改代码或迁移数据库",
    "读写其他项目的数据",
    "删除或覆盖已有正文（Generation）",
    "写知识图谱或物品持有表",
)

#: 流程内可动的步骤（与 flow.SKIPPABLE / 受保护集合一起用）。
_MOVABLE = {"genre", "style", "length", "chapters", "outline", "scene"}

_STEP_ALIAS = {
    "风格": "style",
    "题材": "genre",
    "类型": "genre",
    "字数": "length",
    "篇幅": "length",
    "段数": "chapters",
    "章节数": "chapters",
    "大纲": "outline",
    "情境": "scene",
}

_STEP_LABELS = {
    "style": "风格",
    "genre": "题材",
    "length": "字数",
    "chapters": "段数",
    "outline": "大纲",
    "scene": "情境",
    "distill": "蒸馏",
    "corpus": "语料",
}


def _label(step: str) -> str:
    return _STEP_LABELS.get(step, step)


# ---------------------------------------------------------------------------
# 规划：一句自然语言 → 动作
# ---------------------------------------------------------------------------


def _key_of_tier(word: str) -> str | None:
    if "完整" in word or "全量" in word:
        return "full"
    if "平衡" in word:
        return "balanced"
    if "快速" in word:
        return "fast"
    return None


def _step_of(word: str) -> str | None:
    for label, key in _STEP_ALIAS.items():
        if label in word:
            return key
    return None


def plan(text: str, ctx: dict[str, Any], current_step: str | None = None) -> dict[str, Any] | None:
    """把一句「帮我改 X」规划成动作；不是动作就返回 None（交给作答/追问解析）。

    关键防误伤：当目标就是当前这一步、且用户只是说了个选项（没有「改/换/设」
    这类动词）时，返回 None —— 让它继续走正常作答，避免绕过 answers/history。
    """
    t = (text or "").strip()
    if not t:
        return None
    has_edit_verb = bool(re.search(r"(改成|改为|换成|换为|设为|设成|调到|调成|调整|修改|改一下)", t))

    # 纯追问（「完整档和平衡档区别？」）先让位给解释，不要误触发写操作。
    if not has_edit_verb:
        from app.wizard_chat import looks_like_question

        if looks_like_question(t) and not re.search(
            r"(全本|整本|撤销|撤回|加.{0,4}问题|插入.{0,4}问题|添加.{0,4}问题)", t
        ):
            return None

    # 撤销
    if re.search(r"^(撤销|撤回|undo)", t, re.I) or re.search(r"撤销(刚才|上次|这次|刚)", t):
        return {"action": "undo"}

    # 触发蒸馏
    if re.search(r"(开始|触发|执行|跑|启动).{0,6}蒸馏|蒸馏.{0,4}(开始|跑起来)", t):
        return {"action": "trigger_distill", "force": "重新" in t or "强制" in t}

    # 采样章数（没有对应的引导步骤，永远是动作）
    if re.search(r"(采样|抽样|章数)", t):
        if re.search(r"(全本|整本|全部|所有章|整本灌)", t):
            return {"action": "set_sampling", "chapters": 0}
        n = _first_int(t)
        if n is not None:
            return {"action": "set_sampling", "chapters": n}

    # 档位：必须出现「档」，否则要带明确的改动词，避免追问里的「完整」被误判
    m = re.search(r"(完整|平衡|快速|全量)\s*档", t)
    if not m and has_edit_verb:
        m = re.search(r"(完整|平衡|快速|全量)", t)
    if m and (has_edit_verb or current_step != "distill"):
        key = _key_of_tier(m.group(1))
        if key:
            return {"action": "set_tier", "tier": key}

    # 流程：跳过 / 换序 / 加问题（先于字段动作，避免「跳过风格」被当成改风格）
    if re.search(r"(跳过|略过|不要问|别问)", t):
        step = _step_of(t)
        if step:
            return {"action": "skip_step", "step": step}
        if "蒸馏" in t:
            return {"action": "skip_step", "step": "distill"}
        if "语料" in t:
            return {"action": "skip_step", "step": "corpus"}

    m = re.search(
        r"(?:把|将)?\s*(风格|题材|类型|字数|篇幅|段数|章节数|大纲|情境)\s*(?:这步|这一项)?\s*"
        r"(?:放到|挪到|移到|调到|排到)\s*(风格|题材|类型|字数|篇幅|段数|章节数|大纲|情境)\s*(前面|后面)",
        t,
    )
    if m:
        a, b = _step_of(m.group(1)), _step_of(m.group(2))
        if a and b:
            return {
                "action": "move_step",
                "step": a,
                "anchor": b,
                "position": "before" if m.group(3) == "前面" else "after",
            }

    m = re.search(r"(?:加|添加|插入|增加)(?:一个|一条)?(?:问题|提问)[：:，,]?\s*(.+)", t)
    if m:
        return {"action": "insert_question", "question": m.group(1).strip()}

    # 字段动作：目标不是当前步，或明确带「改/换/设」
    if re.search(r"字数|篇幅", t) and (has_edit_verb or current_step != "length"):
        n = _first_int(t)
        if n is not None:
            return {"action": "set_length", "value": n}
    if re.search(r"题材|类型", t) and (has_edit_verb or current_step != "genre"):
        for genre in flow.GENRE_HINTS:
            if genre in t:
                return {"action": "set_genre", "value": genre}
    if re.search(r"风格|平台", t) and (has_edit_verb or current_step != "style"):
        for style in flow.STYLE_HINTS:
            if style in t:
                return {"action": "set_style", "value": style}
    if re.search(r"大纲", t) and (has_edit_verb or current_step != "outline"):
        if re.search(r"(关|不要|不用|不开|去掉|省掉|取消)", t):
            return {"action": "set_outline", "value": False}
        if re.search(r"(开|要|先出|加上|恢复)", t):
            return {"action": "set_outline", "value": True}

    return None


def _first_int(text: str) -> int | None:
    from app.wizard_chat import first_number

    return first_number(text)


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.utcnow()


def _tier_name(key: str | None) -> str:
    if not key:
        return "未设置"
    try:
        return facts.tier_by_key(key).name
    except Exception:
        return str(key)


def _capture(session) -> dict[str, Any]:
    """撤销快照：会话侧只存会被流程动作改动的三样。"""
    return {
        "answers": dict(getattr(session, "answers", None) or {}),
        "overrides": dict(getattr(session, "overrides", None) or {}),
        "step_key": getattr(session, "step_key", None),
        "history": list(getattr(session, "history", None) or []),
    }


def _audit(
    db: Session,
    project,
    session,
    *,
    action: str,
    target: str,
    old: Any,
    new: Any,
    impact: str,
    undoable: bool = True,
    undo_payload: dict[str, Any] | None = None,
) -> WizardActionAudit:
    row = WizardActionAudit(
        project_id=int(project.id),
        session_id=int(getattr(session, "id", 0) or 0) or None,
        actor="assistant",
        step_key=getattr(session, "step_key", None),
        action=action,
        target=target,
        old_value=old,
        new_value=new,
        impact=impact,
        undoable=bool(undoable),
        undo_payload=undo_payload or {},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _ok(message: str, row: WizardActionAudit | None, **extra: Any) -> dict[str, Any]:
    out = {
        "message": message,
        "action": {
            "id": row.id if row else None,
            "action": row.action if row else extra.get("action"),
            "target": row.target if row else extra.get("target"),
            "old": row.old_value if row else extra.get("old"),
            "new": row.new_value if row else extra.get("new"),
            "impact": row.impact if row else extra.get("impact"),
        },
        "undo_id": row.id if row and row.undoable else None,
        "undoable": bool(row.undoable) if row else False,
    }
    # 用 setdefault：`action` / `target` 这些键上面已经按审计行组成了回执对象，
    # 不能再被 extra 里的字符串覆盖（历史 bug：无变化时回执变成 "set_sampling"）。
    for k, v in extra.items():
        out.setdefault(k, v)
    return out


def apply(
    db: Session,
    project,
    session,
    action: dict[str, Any],
    ctx: dict[str, Any],
    background_tasks=None,
) -> dict[str, Any]:
    """执行一个已规划的动作；失败抛 ValueError（由路由转成 400）。"""
    kind = action.get("action")

    if kind == "undo":
        return undo_last(db, project, session)

    if kind == "set_tier":
        key = str(action.get("tier") or "")
        cfg = facts.tier_by_key(key)  # 未知抛 ValueError
        old = getattr(project, "narrative_tier", None)
        if old == key:
            return _ok(f"蒸馏档位本来就是{cfg.name}，没改。", None, action=kind, target="project.narrative_tier")
        project.narrative_tier = key
        db.commit()
        total = int(ctx.get("corpus_total_chapters") or 0)
        chars = int(ctx.get("corpus_total_chars") or 0)
        sampled = facts.sampled_chapters_for(cfg, total)
        sampled_chars = facts.sampled_chars_for(cfg, total, chars)
        est = facts.estimate_minutes(sampled_chars)
        impact = (
            f"当前档案不变，要重跑蒸馏才会按新档算；按当前语料约 {sampled} 章 / "
            f"{sampled_chars / 10000:.0f} 万字，预计约 {est['minutes']} 分钟"
            f"（{est['basis']}）。"
        )
        row = _audit(
            db, project, session,
            action=kind, target="project.narrative_tier", old=old, new=key,
            impact=impact, undo_payload={"field": "narrative_tier", "value": old},
        )
        return _ok(
            f"改了：蒸馏档位 {_tier_name(old)} → {cfg.name}。影响：{impact}",
            row,
        )

    if kind == "set_sampling":
        n = int(action.get("chapters") if action.get("chapters") is not None else -1)
        total = int(ctx.get("corpus_total_chapters") or 0)
        if n < 0:
            raise ValueError("采样章数要写成 0（全本）或正整数")
        if n > 0 and total and n > total:
            n = total
        old = getattr(project, "narrative_sample_chapters", None)
        if old == n:
            return _ok("采样章数没变。", None, action=kind, target="project.narrative_sample_chapters")
        project.narrative_sample_chapters = n
        db.commit()
        chars = int(ctx.get("corpus_total_chars") or 0)
        # 采样覆盖只对按比例采样的档位生效：按当前偏好档位（没选过就是完整档）
        # 算“实际会蒸馏多少字”，再折算耗时。
        from app.style.narrative import tier_with_sampling

        cfg = facts.tier_by_key(getattr(project, "narrative_tier", None) or "full")
        if cfg.sampling_mode == "percentage":
            cfg = tier_with_sampling(cfg, n)
        sampled = facts.sampled_chapters_for(cfg, total)
        sampled_chars = facts.sampled_chars_for(cfg, total, chars)
        est = facts.estimate_minutes(sampled_chars)
        new_txt = "全本（不抽样）" if n == 0 else f"最多 {n} 章"
        old_txt = "按档位默认" if old is None else ("全本" if int(old) <= 0 else f"最多 {old} 章")
        impact = (
            f"采样覆盖进蒸馏指纹，改完必须重跑才生效，不会命中旧档案；"
            f"按当前语料约 {sampled} 章 / {sampled_chars / 10000:.0f} 万字，"
            f"预计约 {est['minutes']} 分钟（{est['basis']}）。维度和清洗强度不变。"
        )
        row = _audit(
            db, project, session,
            action=kind, target="project.narrative_sample_chapters", old=old, new=n,
            impact=impact,
            undo_payload={"field": "narrative_sample_chapters", "value": old},
        )
        return _ok(
            f"改了：蒸馏采样 {old_txt} → {new_txt}。影响：{impact}",
            row,
        )

    if kind == "trigger_distill":
        from app.routers.projects import DistillStartRequest, start_distill

        if background_tasks is None:
            raise ValueError("当前请求没有后台任务上下文，无法起蒸馏")
        res = start_distill(
            int(project.id),
            DistillStartRequest(tier=None, force=bool(action.get("force"))),
            background_tasks,
            db,
        )
        impact = (
            f"任务 #{res.get('job_id')} 已提交（{res.get('status')}，tier={res.get('tier')}）；"
            "后台跑，进度在项目工作台看。这条不可撤销——任务已经起跑。"
        )
        row = _audit(
            db, project, session,
            action=kind, target="distill", old=None, new=res,
            impact=impact, undoable=False, undo_payload={},
        )
        return _ok(
            f"已提交蒸馏 {impact}",
            row,
            job=res,
        )

    if kind == "set_length":
        n = int(action.get("value") or 0)
        if not (flow.LENGTH_MIN <= n <= flow.LENGTH_MAX):
            raise ValueError(f"字数要在 {flow.LENGTH_MIN}–{flow.LENGTH_MAX} 之间")
        old = getattr(project, "generation_length", None)
        project.generation_length = n
        db.commit()
        impact = "下一段生成生效，已写正文不改。"
        row = _audit(
            db, project, session,
            action=kind, target="project.generation_length", old=old, new=n,
            impact=impact, undo_payload={"field": "generation_length", "value": old},
        )
        return _ok(f"改了：单段字数 {old or '默认'} → {n}。影响：{impact}", row)

    if kind in ("set_genre", "set_style"):
        field = "genre" if kind == "set_genre" else "style"
        value = str(action.get("value") or "")
        valid = flow.GENRE_HINTS if field == "genre" else flow.STYLE_HINTS
        if value not in valid:
            raise ValueError(f"不支持的{field}：{value}；可选：{'、'.join(valid)}")
        old = getattr(project, field, None)
        setattr(project, field, value)
        db.commit()
        impact = "影响后续生成与大纲；已写正文不改。"
        row = _audit(
            db, project, session,
            action=kind, target=f"project.{field}", old=old, new=value,
            impact=impact, undo_payload={"field": field, "value": old},
        )
        return _ok(f"改了：{ '题材' if field == 'genre' else '风格' } {old or '未设置'} → {value}。影响：{impact}", row)

    if kind == "set_outline":
        value = bool(action.get("value"))
        answers = dict(getattr(session, "answers", None) or {})
        old = answers.get("outline")
        answers["outline"] = value
        session.answers = answers
        db.commit()
        impact = "只影响本轮引导提交时要不要先出大纲；已有正文不改。"
        row = _audit(
            db, project, session,
            action=kind, target="session.answers.outline", old=old, new=value,
            impact=impact, undo_payload={"answers": dict(answers)},
        )
        return _ok(
            f"改了：大纲 {'开' if value else '关'}（原值 {old if old is not None else '未设置'}）。影响：{impact}",
            row,
        )

    if kind == "skip_step":
        step = str(action.get("step") or "")
        if step not in flow.SKIPPABLE:
            raise ValueError(f"这一步不能跳过：{step or '未指定'}")
        if step in flow.PROTECTED_STEPS:
            raise ValueError("方向/语料/确认是受保护步骤，不能跳过")
        before = _capture(session)
        ov = dict(before["overrides"])
        ov["skip"] = sorted(set(list(ov.get("skip") or []) + [step]))
        session.overrides = ov
        advanced = False
        if getattr(session, "step_key", None) == step:
            nxt = flow.next_step(
                (session.answers or {}).get("branch"),
                step,
                dict(session.answers or {}),
                {**ctx, "overrides": ov},
            )
            if nxt:
                session.history = list(before["history"]) + [step]
                session.step_key = nxt
                advanced = True
        db.commit()
        impact = (
            "以后不再问这一步，提交时按它的默认值处理；之前已经选过的值不丢。"
        )
        row = _audit(
            db, project, session,
            action=kind, target="session.overrides.skip", old=before["overrides"], new=ov,
            impact=impact, undo_payload=before,
        )
        msg = f"改了：本轮跳过「{_label(step)}」这一步。影响：{impact}"
        if advanced:
            msg += "当前已经跳到下一题。"
        return _ok(msg, row)

    if kind == "move_step":
        step = str(action.get("step") or "")
        anchor = str(action.get("anchor") or "")
        position = str(action.get("position") or "before")
        if step not in _MOVABLE or anchor not in _MOVABLE or step == anchor:
            raise ValueError("只能调整风格/题材/字数/段数/大纲/情境之间的顺序")
        if getattr(session, "step_key", None) in (step, anchor):
            raise ValueError("当前正在问的这一步不能挪，先答完或回退")
        before = _capture(session)
        order = flow.step_order(
            (session.answers or {}).get("branch"),
            dict(session.answers or {}),
            {**ctx, "overrides": before["overrides"]},
        )
        core = [s for s in order if s in _MOVABLE]
        if step not in core or anchor not in core:
            raise ValueError("这两个步骤不在本轮流程里")
        core.remove(step)
        idx = core.index(anchor) + (1 if position == "after" else 0)
        core.insert(idx, step)
        ov = dict(before["overrides"])
        ov["order"] = core
        session.overrides = ov
        db.commit()
        impact = "只改本轮提问顺序，不增删步骤、不改已选答案。"
        row = _audit(
            db, project, session,
            action=kind, target="session.overrides.order", old=before["overrides"], new=ov,
            impact=impact, undo_payload=before,
        )
        return _ok(
            f"改了：把「{_label(step)}」放到「{_label(anchor)}」"
            f"{'前' if position == 'before' else '后'}面。影响：{impact}",
            row,
        )

    if kind == "insert_question":
        question = str(action.get("question") or "").strip()
        if not question:
            raise ValueError("自定义问题不能为空")
        if len(question) > 200:
            raise ValueError("自定义问题最多 200 字")
        before = _capture(session)
        ov = dict(before["overrides"])
        custom = list(ov.get("custom") or [])
        if len(custom) >= 1:
            raise ValueError("本轮最多插入 1 个自定义问题（想换先撤销上一个）")
        key = f"{flow.CUSTOM_PREFIX}{len(custom) + 1}"
        current = getattr(session, "step_key", None) or "genre"
        after = current if current not in ("confirm", flow.FIRST_STEP) else "genre"
        custom.append(
            {
                "key": key,
                "title": "补充问题",
                "label": question[:12],
                "question": question,
                "field": "premise",
                "after": after,
            }
        )
        ov["custom"] = custom
        session.overrides = ov
        db.commit()
        impact = (
            "下一题会先问这个；答案写进项目简介（premise），不改变已有 answers/history 语义。"
        )
        row = _audit(
            db, project, session,
            action=kind, target="session.overrides.custom", old=before["overrides"], new=ov,
            impact=impact, undo_payload=before,
        )
        return _ok(f"加了：自定义问题「{question}」。影响：{impact}", row)

    raise ValueError(f"未知动作：{kind}")


def _apply_undo(db: Session, project, session, row: WizardActionAudit) -> dict[str, Any]:
    payload = row.undo_payload or {}
    if "field" in payload:
        setattr(project, payload["field"], payload.get("value"))
    if "answers" in payload:
        session.answers = dict(payload["answers"])
    if "overrides" in payload:
        session.overrides = dict(payload["overrides"])
    if "step_key" in payload:
        session.step_key = payload["step_key"]
    if "history" in payload:
        session.history = list(payload["history"])
    row.undone_at = _utcnow()
    db.commit()

    return {
        "message": (
            f"已撤销：{row.action}（{row.target}）。"
            f"恢复成 {row.old_value if row.old_value is not None else '未设置'}；"
            f"刚才那次改动的值 {row.new_value} 不再生效。"
        ),
        "action": {
            "id": row.id,
            "action": "undo",
            "target": row.target,
            "old": row.new_value,
            "new": row.old_value,
            "impact": "撤销后参数回到改动前。",
        },
        "undo_id": None,
        "undoable": False,
    }


def undo_by_id(db: Session, project, session, audit_id: int) -> dict[str, Any]:
    """撤销指定的一次写操作（必须在同一项目、可撤销、尚未撤销）。"""
    row = db.get(WizardActionAudit, int(audit_id))
    if row is None or int(row.project_id) != int(project.id):
        raise ValueError("找不到这条操作记录")
    if not row.undoable:
        raise ValueError("这次操作不可撤销（例如已经起跑的蒸馏任务）")
    if row.undone_at is not None:
        raise ValueError("这次操作已经撤销过了")
    return _apply_undo(db, project, session, row)


def undo_last(db: Session, project, session) -> dict[str, Any]:
    """撤销本会话最近一次可撤销的写操作。"""
    stmt = (
        db.query(WizardActionAudit)
        .filter(
            WizardActionAudit.project_id == int(project.id),
            WizardActionAudit.undoable.is_(True),
            WizardActionAudit.undone_at.is_(None),
        )
        .order_by(WizardActionAudit.id.desc())
        .limit(1)
    )
    row = stmt.one_or_none()
    if row is None:
        return {"message": "没有可撤销的操作。", "action": None, "undo_id": None, "undoable": False}
    return _apply_undo(db, project, session, row)


def history(db: Session, project_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """审计列表（给前端/报告对账）。"""
    rows = (
        db.query(WizardActionAudit)
        .filter(WizardActionAudit.project_id == int(project_id))
        .order_by(WizardActionAudit.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "session_id": r.session_id,
            "actor": r.actor,
            "step_key": r.step_key,
            "action": r.action,
            "target": r.target,
            "old": r.old_value,
            "new": r.new_value,
            "impact": r.impact,
            "undoable": r.undoable,
            "undone_at": r.undone_at.isoformat() if r.undone_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
