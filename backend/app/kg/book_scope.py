# -*- coding: utf-8 -*-
"""「当前书」的唯一取值点（读侧 / 写侧共用）。

书维度有两个字段，回答问题不同：

- ``active_novel_id``：这个项目的书级事实（物品组/写法/持有/实体）来自哪本书；
- ``continuation_novel_id``：这个项目正在从哪本书往后写；原创项目为 ``NULL``。

两者都空 = ``None``：所有按书过滤的查询都退化为「不按书过滤」，与加书维度
之前逐字一致。**顺序只写在这里**，调用点不要各写一遍（写侧曾经差点按
``active`` 取值，与读侧的续写优先分叉）。
"""
from __future__ import annotations


def book_for_project(project) -> int | None:
    """本项目此刻按哪本书收窄书级事实。

    顺序固定：``continuation_novel_id`` → ``active_novel_id`` → ``None``。

    - 续写书优先：注入正文的是**硬约束**（「镇北王 own 99–106」），约束必须对
      正在写的那本成立；事实来源书只作「原创但借了某本图谱」时的回退。
    - 两列都空 → ``None``，读通道不按书过滤，与加这两列之前逐字一致。
    - 对象缺列（老库/老对象）时按缺省 ``None`` 处理，不猜一个 id 出来。
    """
    continuation = getattr(project, "continuation_novel_id", None)
    if continuation is not None:
        return int(continuation)
    active = getattr(project, "active_novel_id", None)
    if active is not None:
        return int(active)
    return None


def prompt_scope_book_ids(project) -> tuple[int | None, ...]:
    """生成 prompt 的 KG 切片允许出现的书维度集合。

    与 ``book_for_project`` 的区别：那边的 ``None`` 表示「不按书过滤」（写侧
    兼容老数据），这里返回的是**读侧白名单**，永远非空：

    - 项目有续写书/来源书 → ``(本书, None)``：本书事实 + 项目级通配；
    - 两列都空（原创项目）→ ``(None,)``：只认项目级事实。此时带 ``novel_id``
      的行都是「从某本参考书抽出来的」，按约定不得进入本书上下文。

    只用项目字段，不写死书名或项目 id；新增一本书不需要改这里。
    """
    own = book_for_project(project)
    return (own, None) if own is not None else (None,)


def scope_sql_values(scope: tuple[int | None, ...]) -> tuple[int, ...]:
    """把书维度白名单折成 SQL 可比值：``NULL`` 用 ``-1`` 代表。

    与 ``COALESCE(novel_id, -1)`` 同口径（实体唯一键、事件过滤都这么写）。
    """
    return tuple(-1 if b is None else int(b) for b in scope)
