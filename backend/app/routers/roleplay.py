"""与角色对话：选角色 → 提问 → 以他的身份回答。

## 边界

这是**第三板块**（原创 / 续写 / 角色）里的第三条路。它与「写正文」是**两件事**：

- 写正文 → `generations`，进摘要树，参与续写；
- 角色对话 → `roleplay_messages`，只是问答记录，不进正文主线。

## 三条可见性规则在这里的落点

| 路径 | 参考书原文 | 参考角色 | 参考世界观 |
|---|---|---|---|
| 原创 | ❌ | ❌ | ❌ |
| 续写 | ✅（有边界） | ✅ | ✅ |
| **角色** | ⚠️ **只有他出场/说过的** | ⚠️ **只有他** | ❌ |

角色路径给的证据**带来源标注**（「他亲历」/「他在场听到」）——
因为他「在场」不等于「知情」。标注之后由模型自己判断能不能当作记忆说出口。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Project, RoleplayMessage

router = APIRouter(prefix="/api/projects/{project_id}/roleplay", tags=["roleplay"])


def _require_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, detail={"message": "项目不存在"})
    return project


class RoleplayChatBody(BaseModel):
    character: str = Field(..., min_length=1, max_length=64)
    question: str = Field(..., min_length=1, max_length=4000)
    novel_ids: list[int] | None = None
    #: 检索几条证据（默认 6）
    k: int = Field(6, ge=1, le=12)


@router.get("/characters")
def list_characters(project_id: int, db: Session = Depends(get_db)) -> dict:
    """可选的角色清单，**分两组**返回。

    - `project` —— **这个项目里的角色**（剧情圣经 + 图谱抽出来的）。数量少，
      而且通常就是用户想扮演的那几个 ⇒ 默认展示这组。
    - `reference` —— **参考书里的角色**（叙事档案的「角色声音词典」里有全套）。
      实测北荒刀歌一把就有 130+ 个 ⇒ 铺开一屏没法用，所以折叠 + 可搜索。

    两组都要给：只给一组，用户会觉得「书里明明有这个人却选不到」。
    """
    project = _require_project(db, project_id)
    from app.generation.engine import _roleplay_character_name

    project_chars: list[str] = []
    reference_chars: list[str] = []
    seen_p: set[str] = set()
    seen_r: set[str] = set()

    def _add(bucket: list[str], seen: set[str], nm: str) -> None:
        nm = (nm or "").strip()
        if nm and nm not in seen:
            seen.add(nm)
            bucket.append(nm)

    # ① 参考书里的角色（声音词典，每条以「名字：」开头）
    try:
        from app.style.layers import method_rules
        from app.style.narrative import CHARACTER_VOICE_DIMENSION, _VOICE_NAME_PREFIX_RE

        rules = method_rules(project.style_narrative or {})
        for item in (rules.get(CHARACTER_VOICE_DIMENSION) or []):
            raw = item.get("rule", "") if isinstance(item, dict) else str(item)
            m = _VOICE_NAME_PREFIX_RE.match(raw)
            if m:
                _add(reference_chars, seen_r, m.group(0).rstrip("：: ").strip())
    except Exception:  # noqa: BLE001 —— 取不到就给下面那组
        pass

    # ② 剧情圣经里用户自己写的
    bible = project.plot_bible or {}
    for c in (bible.get("characters") or []) if isinstance(bible, dict) else []:
        _add(project_chars, seen_p, str((c.get("name") if isinstance(c, dict) else c) or ""))

    # ③ 图谱抽出来的
    try:
        from app.kg.queries import get_reminders

        rem = get_reminders(db, project.id, source="generated") or {}
        for c in (rem.get("character_constraints") or []):
            _add(project_chars, seen_p, str((c.get("name") if isinstance(c, dict) else c) or ""))
    except Exception:  # noqa: BLE001
        pass

    # 两组去重：同一个角色可能两边都有（先按项目组算，参考书组里去掉重复）
    for nm in list(project_chars):
        if nm in seen_r:
            reference_chars.remove(nm)

    current = _roleplay_character_name(project)
    return {
        "characters": project_chars + reference_chars,
        "project": project_chars,
        "reference": reference_chars,
        "current": current,
    }


@router.post("/chat")
def roleplay_chat(
    project_id: int, body: RoleplayChatBody, db: Session = Depends(get_db),
) -> dict:
    """问一句，答一句。产物落 `roleplay_messages`（不进正文主线）。"""
    project = _require_project(db, project_id)
    from app.roleplay.chat import run_chat

    try:
        result = run_chat(
            db, project, body.character, body.question,
            novel_ids=body.novel_ids, k=body.k,
        )
    except ValueError as exc:
        raise HTTPException(400, detail={"message": str(exc)}) from exc
    except Exception as exc:  # noqa: BLE001 —— 对话失败不该 500 成一团浆糊
        raise HTTPException(502, detail={"message": f"角色回答失败：{type(exc).__name__}"}) from exc

    novel_ids = body.novel_ids
    if novel_ids is None:
        novel_ids = list(getattr(project, "reference_novel_ids", None) or [])
    row = RoleplayMessage(
        project_id=project.id,
        character=result["character"],
        question=body.question,
        answer=result["answer"],
        sources_json=result["sources"],
        novel_ids_json=list(novel_ids),
    )
    db.add(row)
    db.commit()
    return {
        "id": row.id,
        "character": result["character"],
        "question": result["question"],
        "answer": result["answer"],
        "sources": result["sources"],
        "has_evidence": result["has_evidence"],
        "elapsed": result["elapsed"],
    }


@router.get("/messages")
def list_messages(
    project_id: int,
    character: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """对话记录（最新的在前）。"""
    project = _require_project(db, project_id)
    q = db.query(RoleplayMessage).filter(RoleplayMessage.project_id == project.id)
    if character:
        q = q.filter(RoleplayMessage.character == character)
    rows = q.order_by(RoleplayMessage.id.desc()).limit(limit).all()
    return {
        "messages": [
            {
                "id": r.id,
                "character": r.character,
                "question": r.question,
                "answer": r.answer,
                "sources": r.sources_json or [],
                "novel_ids": r.novel_ids_json or [],
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reversed(rows)
        ]
    }
