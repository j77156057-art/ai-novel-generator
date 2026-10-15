"""提示词运行时覆写（ADR-2026-10-12 第 5 步第 4 步）。

## 硬约束：只加块，不改源码

`backend/app/generation/prompts.py` 是 b58c763 基线的一部分，**一个字节都不动**。
「改提示词」在本步里的实现方式是：

- 代码默认值留在 `prompts.py`（`build_system_prompt` / `build_user_prompt`）；
- 覆写内容落`prompt_overrides` 表（键=prompt_key，值=追加文本）；
- 运行时由 `resolve_prompt_block()` 读出覆写并拼在对应块之后。

未设置覆写时 `resolve_prompt_block()` 返回空串，调用方拼接结果与改之前
**逐字节相同** —— 这是本模块唯一需要守住的兼容底线。

## 为什么是「追加块」而不是「整段替换」

整段替换要求覆写方完整复述系统提示词的全部约束（角色锁定 / 伏笔纪律 /
文风要求 / 续写纪律），漏一句就会静默削弱 b58c763 的不变量。追加块把这个
风险从「必须一字不差」降级为「加一段说明」。

## 与 proposal 的关系

覆写只能经由 `POST /api/assistant/proposals/{id}/apply` 落库（proposal
`type=prompt_edit` / `target_kind=prompt_override`）。本模块的
`set_active_override()` 是被apply 事务调用的**下层实现**，不接受独立写入端点。

历史永不删除：新覆写把旧的置 `active=False`，回滚靠再写一条内容相同的
active 行（而不是物理删除）。
"""

from __future__ import annotations

from typing import Any

# ====================================================================
# 可覆写的提示词键
# ====================================================================


class PromptKey:
    """可覆写的提示词块键。

    刻意只开放三个块：它们是「附加说明」性质（风格附加、额外约束、结尾指令），
    不会被覆写「关掉」角色锁定或伏笔纪律 —— 那些在代码默认块里，
    覆写只能在其后追加，不能删除。
    """

    STYLE = "style_suffix"          # 【风格】之后的附加调性说明
    CONSTRAINT = "constraint_note"  # 【续写纪律】之后的额外硬约束
    DIRECTIVE = "directive_suffix"  # 【续写指令】之后的额外指令


PROMPT_KEYS: tuple[str, ...] = (
    PromptKey.STYLE,
    PromptKey.CONSTRAINT,
    PromptKey.DIRECTIVE,
)

#: 代码默认块：空串= 没有任何覆写时的生效内容。
#: 放在这里而不是从 prompts.py 反解，是为了让「默认就是什么都不加」这件事
#: 显式可见 —— 反解会把「默认块」与「覆写块」两种来源混在同一个返回值里，
#: 调用方就再也分不清当前生效的是代码还是用户改的。
CODE_DEFAULTS: dict[str, str] = {key: "" for key in PROMPT_KEYS}

#: 单条覆写文本长度上限（防爆库 + 防注入超长提示）。
MAX_OVERRIDE_LEN = 4000


# ====================================================================
# 读
# ====================================================================


def is_valid_key(prompt_key: str) -> bool:
    return prompt_key in PROMPT_KEYS


def validate_key(prompt_key: str) -> str:
    """键合法性校验；非法键抛 ValueError（router 转 422）。"""
    key = (prompt_key or "").strip()
    if key not in PROMPT_KEYS:
        raise ValueError(
            f"prompt_key={prompt_key!r} 不在可覆写范围 {list(PROMPT_KEYS)} 内"
        )
    return key


def get_active_override(db: Any, project_id: int, prompt_key: str) -> Any | None:
    """取该项目该键当前生效的覆写行；没有则 None。

    多条 active 时取 id 最大的一条（理论上不该出现，`set_active_override`
    在同事务里保证唯一；万一出现也不抛错 —— 让「取最新」这个确定行为
    兜住历史脏数据）。
    """
    from sqlalchemy import select

    from app.models import PromptOverride

    rows = list(
        db.scalars(
            select(PromptOverride)
            .where(
                PromptOverride.project_id == project_id,
                PromptOverride.prompt_key == prompt_key,
                PromptOverride.active.is_(True),
            )
            .order_by(PromptOverride.id.desc())
        ).all()
    )
    return rows[0] if rows else None


def resolve_prompt_block(db: Any, project_id: int, prompt_key: str) -> tuple[str, str]:
    """解析某提示词键的生效文本，返回 `(text, source)`。

    - 有 active 覆写 → `("<覆写文本>", "runtime_override")`
    - 否则→ `("", "code_default")`

    调用方把 `text` 拼在对应代码默认块之后即可；`source` 用于
    `GET /api/assistant/prompts/active` 回答「现在生效的是覆写还是代码默认」。
    """
    key = validate_key(prompt_key)
    row = get_active_override(db, project_id, key)
    if row is not None:
        return (row.override_text or "", "runtime_override")
    return (CODE_DEFAULTS.get(key, ""), "code_default")


def active_prompt_report(db: Any, project_id: int) -> list[dict[str, Any]]:
    """全部可覆写键的当前生效来源（`GET /api/assistant/prompts/active` 用）。"""
    report: list[dict[str, Any]] = []
    for key in PROMPT_KEYS:
        text, source = resolve_prompt_block(db, project_id, key)
        row = get_active_override(db, project_id, key)
        report.append(
            {
                "prompt_key": key,
                "source": source,
                "text": text,
                "override_id": row.id if row is not None else None,
                "source_proposal_id": (
                    row.source_proposal_id if row is not None else None
                ),
                "created_at": (
                    row.created_at.isoformat()
                    if row is not None and row.created_at is not None
                    else None
                ),
            }
        )
    return report


def list_override_versions(db: Any, project_id: int) -> list[dict[str, Any]]:
    """该项目全部覆写历史（含已失效的），按时间倒序。

    `GET /api/assistant/prompts/versions` 用 —— 与 `versions` 表并列，
    因为「提示词的版本历史」有两个视角：内容快照（Version 表）与
    覆写操作序列（本表）。两者都保留，审计时才不会缺一半。
    """
    from sqlalchemy import select

    from app.models import PromptOverride

    rows = list(
        db.scalars(
            select(PromptOverride)
            .where(PromptOverride.project_id == project_id)
            .order_by(PromptOverride.id.desc())
        ).all()
    )
    return [
        {
            "id": r.id,
            "prompt_key": r.prompt_key,
            "active": bool(r.active),
            "text": r.override_text,
            "source_proposal_id": r.source_proposal_id,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ====================================================================
# 写（仅由 apply 事务调用，不对外暴露端点）
# ====================================================================


def set_active_override(
    db: Any,
    *,
    project_id: int,
    prompt_key: str,
    override_text: str,
    source_proposal_id: int | None = None,
    created_by: str | None = None,
) -> Any:
    """把某键的覆写设为 `override_text`；旧的 active 行降级为失效（**不删**）。

    调用方（`proposals.write_target`）已经在事务里，本函数**不 commit**：
    覆写行与业务表写入、Version 快照共用同一个事务边界 ——
    覆写写成功但版本快照失败时，覆写也一起回滚。

    顺序上先降旧再插新：即便中途抛错，同一 (project, key) 也不会出现
    「零条 active」以外的状态（数据库层面此刻还没提交）。
    """
    from app.models import PromptOverride

    key = validate_key(prompt_key)
    text = override_text or ""
    if len(text) > MAX_OVERRIDE_LEN:
        raise ValueError(
            f"提示词覆写超长（{len(text)} > {MAX_OVERRIDE_LEN}）"
        )

    # 降级所有旧 active 行（历史保留，便于 versions 端点回看）
    for row in (
        db.query(PromptOverride)
        .filter(
            PromptOverride.project_id == project_id,
            PromptOverride.prompt_key == key,
            PromptOverride.active.is_(True),
        )
        .all()
    ):
        row.active = False
    db.flush()

    override = PromptOverride(
        project_id=project_id,
        prompt_key=key,
        override_text=text,
        active=True,
        source_proposal_id=source_proposal_id,
        created_by=created_by,
    )
    db.add(override)
    db.flush()
    return override