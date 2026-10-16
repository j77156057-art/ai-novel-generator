"""知识图谱查询：角色出场、坑状态、生成前提醒（M3.3 注入约束的供给方）。"""

from sqlalchemy import select

from app.models import KgEdge, KgNode


def get_graph(db, project_id: int, type_: str = "all", source: str | None = None) -> dict:
    nodes_q = select(KgNode).where(KgNode.project_id == project_id)
    if source is not None:
        nodes_q = nodes_q.where(KgNode.source == source)
    if type_ != "all":
        nodes_q = nodes_q.where(KgNode.type == type_)
    nodes = db.scalars(nodes_q).all()
    edges = db.scalars(
        select(KgEdge).where(
            KgEdge.from_id.in_([n.id for n in nodes]) | KgEdge.to_id.in_([n.id for n in nodes])
        )
    ).all() if nodes else []
    return {
        "nodes": [
            {
                "id": n.id,
                "type": n.type,
                "props": n.props,
                "first_chapter": n.first_chapter,
                "last_chapter": n.last_chapter,
            }
            for n in nodes
        ],
        "edges": [
            {"from": e.from_id, "to": e.to_id, "type": e.type, "props": e.props}
            for e in edges
        ],
    }


def get_character(db, project_id: int, name: str) -> dict | None:
    nodes = db.scalars(
        select(KgNode).where(
            KgNode.project_id == project_id, KgNode.type == "character"
        )
    ).all()
    node = next(
        (n for n in nodes if (n.props or {}).get("name") == name), None
    )
    if node is None:
        return None
    edges = db.scalars(
        select(KgEdge).where(
            (KgEdge.from_id == node.id) | (KgEdge.to_id == node.id)
        )
    ).all()
    related = []
    for e in edges:
        other_id = e.to_id if e.from_id == node.id else e.from_id
        other = db.get(KgNode, other_id)
        related.append(
            {
                "name": (other.props or {}).get("name", other.id),
                "relation": (e.props or {}).get("relation"),
                "chapters": (e.props or {}).get("chapters", []),
            }
        )
    return {
        "name": name,
        "mentions": (node.props or {}).get("mentions"),
        "first_chapter": node.first_chapter,
        "last_chapter": node.last_chapter,
        # 刻意**不返回** appearances：KgNode 只持久化 first/last_chapter，
        # 原先的 list(range(first, last+1)) 是把「首末章」编造成「连续出场列表」——
        # 角色只在第 3、50 章出场也会返回 48 章。已 grep 确认仓库内无任何消费方
        # （前端仅有 types.ts 的类型声明、agent registry 原样透传 profile），
        # 故按「无消费方就删除伪造字段」处理，而不是换个形状继续骗人。
        # 若将来真要出场列表，得先把真实章节列表落库（props.chapters），
        # 不能再从 first/last 反推。
        "relations": related,
    }


# 角色约束 / 未填坑清单曾有「无界」问题：get_reminders 按 project_id+type 全量取，
# 没有 LIMIT / 排序 / 相关度过滤，导致 system 提示词随账本规模线性膨胀
# （实测 11.6 字/角色，第 300 段仅角色清单就 ~3,560 字），绕过前情块的预算裁剪。
# 现按「最近出场 + 提及数」取 top-K 并截断：清单贡献随账本规模**饱和**而非增长。
# top_k=None 表示不封顶（仅供一次性人工查阅的 API 用）；生成路径用默认 REMINDER_TOP_K。
REMINDER_TOP_K = 50  # 角色清单贡献 ≈ 11.6 字/角色 → 封顶 50 ≈ 580 字，约等于一个前情预算块(600)


def _char_relevance(c: "KgNode") -> tuple:
    """角色相关度：最近出场段号优先，其次提及数。都越高越该进约束清单。"""
    props = c.props or {}
    last = c.last_chapter if c.last_chapter is not None else -1
    mentions = props.get("mentions") or 0
    return (last, mentions)


def _hole_recency(h: "KgNode") -> int:
    """未填坑相关度：最近开坑的优先（更可能仍是当前叙事主线）。"""
    return h.first_chapter if h.first_chapter is not None else -1


def get_reminders(db, project_id: int, source: str = "generated",
                  top_k: int = REMINDER_TOP_K) -> dict:
    """M3.3：生成前提醒——未填坑 + 角色人设约束（供 M4 注入提示词防漂移）。

    source 必须显式指定：图谱按来源隔离（参考语料图 novel:{id} / 生成后图 generated），
    生成续写时只能取**用户自己小说**的事实（generated）——否则参考语料自身的角色
    （例如《红楼梦》的林黛玉）会被当成「本项目已锁定角色」注入提示词，与参考语料
    风格检索混淆成事实约束。这是「设计上隔离、实现上没贯穿」的修复点。

    top_k：清单封顶（默认 REMINDER_TOP_K）。生成路径**必须**封顶，否则提示词随账本
    线性膨胀（见 docs/验收指标.md §6）。top_k=None 不封顶，仅供一次性人工查阅的 API。
    """
    holes = db.scalars(
        select(KgNode).where(
            KgNode.project_id == project_id,
            KgNode.type == "plothole",
            KgNode.source == source,
        )
    ).all()
    chars = db.scalars(
        select(KgNode).where(
            KgNode.project_id == project_id,
            KgNode.type == "character",
            KgNode.source == source,
        )
    ).all()

    open_holes = [h for h in holes if (h.props or {}).get("status") == "open"]
    open_holes.sort(key=_hole_recency, reverse=True)
    chars.sort(key=_char_relevance, reverse=True)

    if top_k is not None:
        open_holes = open_holes[:top_k]
        chars = chars[:top_k]

    return {
        "unresolved_plotholes": [
            {
                "question": (h.props or {}).get("question"),
                "first_chapter": h.first_chapter,
            }
            for h in open_holes
        ],
        "character_constraints": [
            {
                "name": (c.props or {}).get("name"),
                "mentions": (c.props or {}).get("mentions"),
                "first_chapter": c.first_chapter,
                "last_chapter": c.last_chapter,
            }
            for c in chars
        ],
    }
