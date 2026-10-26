"""事实账本：带别名的扁平表 + 别名归一（外部评审 A 的 Q3/Q6/Q7 落地）。

## 为什么是扁平表而不是图

这里实际要回答的问题是「本段相关的那 ~20 条事实是什么」——过滤/检索问题，
不是多跳遍历问题。为此建图遍历设施只增维护成本，查询侧一次 WHERE 就够。

## 与 KgNode 的分工

- `KgNode`：从文本抽出来的实体与关系，`novel:{id}` / `generated` 各自成图；
- `Fact`（本模块）：**裁决层**。`aliases` 是名字表，`source`/`confidence` 是权威档位。
  名字表的权威在这里，图谱里的名字由 `apply_merges` 按它改写后保持一致。

## 别名归一：为什么自动档只收两类（重要，实测结论）

原计划按「字符串包含关系」自动合并，规则写完先在真实语料（1066 分片/120 万字）上测了
precision，结论是**不能这么干**：

| 规则族 | 实测 |
|---|---|
| 姓氏前缀 + 后缀包含 | 4 对正确 / 3 对错误：`姐儿→凤姐儿`（姐儿是泛称）、`元帅→康元帅`（官职）、`满面→容满面`（笑容满面残片） |
| 敬称后缀剥离 | 方向不可判：`凤姐儿→凤姐`(比 0.22) 该剥，但 `尤二姐→尤二`(0.77)、`马道婆→马道`(0.60) 里「姐/婆」是**名字本身**的一部分，比值相近无法区分 |
| 「多出的字在人名用字集内」前缀包含 | 灾难级：`贾母→贾母王`、`袭人→袭人麝`、`李纨→李纨探`——长名其实是并列短语（`贾母王夫人`＝贾母＋王夫人） |
| 后缀包含 + 唯一长名 + 高频闸门 | `宝玉→甄宝玉`(比 153) 被闸门正确挡住，但 `宝钗→薛宝钗`(比 64) 是同一人被误挡 |

即：字符串层拿不到可接受精度，而每一条错误合并都是**静默篡改身份**——把 A 的台词算到 B 头上，
再注入提示词。所以 `plan_alias_merges` 的自动档**只保留有零反例证据的两类**，其余全部降级为
「带证据的待裁决提案」（`review`），落库为 `confidence="low"`，不自动应用。
这与 Q6「别名词表未就绪前禁用自动裁决」、Q4「标红给用户看，不自动重写」是同一条纪律。

## 自动档凭什么可信

1. **粘连/切片残渣**（`听宝玉` → `宝玉`）：长名以粘连引导字开头，它是文本碎片而非人名。
   实测语料零反例（`听宝玉`/`听贾母`/`听黛玉`/`听袭人` 全部指向其尾部的真名）。
2. **生成时序归一**：这是**生成小说**独有的强证据，也是别名词表真正的用武之地——
   模型先写全名 `宁折枝`（第 3 段），后端再简称 `宁折`（第 7 段），方向由**出现时序**决定，
   不需要猜身份：后出现的短名并回先出现的全名。前提是长名已获说话/命名证据（是真的角色），
   且短名恰好只有一个这样的长名归属（否则属歧义，转提案）。
   反向（先短后长）不自动并——那可能是新角色，转提案。

注意时序法**只对 generated 生效**。参考语料没有「作者笔下的时序」这种意义，
对 `novel:{id}` 只产出提案。
"""

from __future__ import annotations

from sqlalchemy import select

from app.kg.build import _segment_pairs, extract_from_text
from app.kg.extractor import _GLUE_TAIL_CHARS
from app.models import Fact, KgEdge, KgNode

# 字符类型：fact 的 type 字段取值（可扩展，不在此封死）
T_CHARACTER = "character"
T_PLOTHOLE = "plothole"
T_RELATIONSHIP = "relationship"

#: 只有这几类事实是**由正文抽取**得出的，可以在正文修订后重建。
#: 其余类型（作者豁免、哨兵写下的角色状态、逐段告警）不由正文决定，不得随重建抹掉。
REBUILDABLE_FACT_TYPES: frozenset[str] = frozenset({
    T_CHARACTER, T_PLOTHOLE, T_RELATIONSHIP,
})

# 权威档位：数值越大越优先（Q6 的分层裁决：作者意图 > 高置信抽取 > 低置信抽取）
AUTHORITY = {"user": 3, "bible": 2, "generated": 1, "novel": 0}


def authority_of(source: str) -> int:
    """按 source 前缀判权威档位（`novel:123` 归入参考语料档）。"""
    if source.startswith("novel:") or source.startswith("novel_"):
        return AUTHORITY["novel"]
    return AUTHORITY.get(source, 0)


# ---------------------------------------------------------------------------
# 写入层
# ---------------------------------------------------------------------------

def facts_from_extraction(result, segment_no: int) -> list[dict]:
    """把抽取结果摊平成中性 fact 列表。

    刻意不直接吃 `ExtractionResult` 类型——账本是裁决层，不该绑死在某一个抽取器上；
    换成 LLM 抽取器后只要也产出同样形状的 dict 即可复用（Q3 的可替换性）。
    """
    facts: list[dict] = []
    for name, c in result.characters.items():
        facts.append(
            {
                "type": T_CHARACTER,
                "key": name,
                "props": {
                    "mentions": c.mentions,
                    "speaking": bool(c.speaking),
                    "named": bool(c.named),
                },
                "first_segment": min(c.chapters) if c.chapters else segment_no,
                "last_segment": segment_no,
                "confidence": "high",
            }
        )
    for ph in result.plotholes.values():
        facts.append(
            {
                "type": T_PLOTHOLE,
                "key": ph.question or ph.key,
                "props": {
                    "cue": ph.cue,
                    "status": ph.status,
                    # 伏笔台账要回答「埋于第几段」，故首现段必须留
                    "planted_segment": ph.first_chapter or segment_no,
                },
                "first_segment": ph.first_chapter or segment_no,
                "last_segment": segment_no,
                "confidence": "high",
            }
        )
    for (a, b), edge in result.relationships.items():
        facts.append(
            {
                "type": T_RELATIONSHIP,
                "key": f"{a}→{b}",
                "props": {
                    "from": a,
                    "to": b,
                    "relation": edge.relation,
                    "segments": sorted(edge.chapters),
                },
                "first_segment": segment_no,
                "last_segment": segment_no,
                "confidence": "high",
            }
        )
    return facts


def upsert_facts(
    db, project_id: int, facts: list[dict], source: str = "generated", *, commit: bool = True
) -> dict:
    """增量 upsert。语义：同名累加计数/扩段范围，不清空、不重建。

    `source` 参与键：同一 project 下参考语料侧与生成侧是两套事实，
    绝不能因为名字相同就把参考语料角色的统计并进生成侧（P0-1 的隔离纪律）。

    `commit=False` 时只 flush 不提交——给「先清后写」的重建原语用，
    让整段重建落在一个事务里，失败时能整体回滚。
    """
    if not facts:
        return {"upserted": 0, "created": 0, "updated": 0}

    rows = db.scalars(
        select(Fact).where(Fact.project_id == project_id, Fact.source == source)
    ).all()
    index: dict[tuple[str, str], Fact] = {(f.type, f.key): f for f in rows}

    n_new = n_upd = 0
    for item in facts:
        key = (item["type"], item["key"])
        cur = index.get(key)
        if cur is None:
            cur = Fact(
                project_id=project_id,
                type=item["type"],
                key=item["key"],
                props=dict(item.get("props") or {}),
                aliases=[],
                source=source,
                confidence=item.get("confidence", "high"),
                first_segment=item.get("first_segment"),
                last_segment=item.get("last_segment"),
            )
            db.add(cur)
            db.flush()
            index[key] = cur
            n_new += 1
            continue

        props = dict(cur.props or {})
        new = dict(item.get("props") or {})
        for k, v in new.items():
            if isinstance(v, int) and not isinstance(v, bool):
                props[k] = (props.get(k) or 0) + v
            elif isinstance(v, bool):
                props[k] = bool(props.get(k)) or v
            elif isinstance(v, list):
                merged = list(props.get(k) or [])
                merged.extend(x for x in v if x not in merged)
                props[k] = merged
            elif k not in props:
                props[k] = v
        cur.props = props
        # 权威只升不降：低置信重复出现不得覆盖高置信（Q6 分层裁决）
        if authority_of(source) >= 0 and item.get("confidence") == "high":
            cur.confidence = "high"
        for attr, fn in (("first_segment", min), ("last_segment", max)):
            old, new_v = getattr(cur, attr), item.get(attr)
            if new_v is not None:
                setattr(
                    cur, attr,
                    new_v if old is None else fn(old, new_v),
                )
        n_upd += 1

    if commit:
        db.commit()
    else:
        db.flush()
    return {"upserted": n_new + n_upd, "created": n_new, "updated": n_upd}


def rebuild_facts(
    db,
    project_id: int,
    *,
    source: str = "generated",
    segments=None,
    commit: bool = True,
) -> dict:
    """按当前正文重建某一 source 的事实账本（全量替换，不是增量）。

    `segments`：当前生成分支的正文段列表 `[(segment_no, text), ...]`，必须非空。

    语义：
      1. 删除该 project + source 下**由正文抽取得出的**事实行（`REBUILDABLE_FACT_TYPES`）；
         作者豁免、哨兵写下的角色状态、逐段告警**一律保留** —— 它们不由正文决定，
         抹掉等于替作者改主意、或让「已死」的角色复活。
      2. 逐段 `extract_from_text` → `upsert_facts`，与生成期增量路径同口径。
      3. `resolve_aliases` 归一别名。
      4. 提交并返回统计。

    `commit=False` 时只 flush——供 `derived_rebuild` 把「清图 + 重写账本 +
    写派生状态」收进同一个事务，失败时整体回滚，不留半重建的账本。
    """
    pairs = _segment_pairs(segments)
    if not pairs:
        raise ValueError("segments_required")

    rows = db.scalars(
        select(Fact).where(Fact.project_id == project_id, Fact.source == source)
    ).all()
    deleted = 0
    for row in rows:
        # 只重建**由正文抽取**得出的那几类事实。其它类型不能跟着正文重建：
        #   - `exemption` 是作者自己的裁决（routers/kg_v3.py 写入），抹掉等于替作者改主意；
        #   - `state`（角色生死/持有物）由哨兵在生成时写下，正文里**推不出来**，
        #     删掉等于让「已死」的角色复活，反过来污染后续诊断；
        #   - `alert` 是哨兵/判官的逐段告警，要重算得重跑哨兵，不能靠正文重建。
        if row.type not in REBUILDABLE_FACT_TYPES:
            continue
        db.delete(row)
        deleted += 1
    db.flush()

    n_new = n_upd = n_upserted = 0
    for segment_no, text in pairs:
        if not text.strip():
            continue
        result, _ = extract_from_text(text, segment_no)
        stats = upsert_facts(
            db,
            project_id,
            facts_from_extraction(result, segment_no),
            source=source,
            commit=False,
        )
        n_new += stats["created"]
        n_upd += stats["updated"]
        n_upserted += stats["upserted"]
    resolve_aliases(db, project_id, source=source, commit=False)

    if commit:
        db.commit()
    else:
        db.flush()
    kept = db.scalars(
        select(Fact).where(Fact.project_id == project_id, Fact.type == "exemption")
    ).all()
    return {
        "deleted": deleted,
        "created": n_new,
        "updated": n_upd,
        "upserted": n_upserted,
        "exemptions_kept": len(kept),
    }


def get_facts(
    db, project_id: int, type_: str | None = None, source: str = "generated"
) -> list[dict]:
    q = select(Fact).where(Fact.project_id == project_id, Fact.source == source)
    if type_ is not None:
        q = q.where(Fact.type == type_)
    return [
        {
            "type": f.type,
            "key": f.key,
            "props": f.props,
            "aliases": f.aliases or [],
            "source": f.source,
            "confidence": f.confidence,
            "first_segment": f.first_segment,
            "last_segment": f.last_segment,
        }
        for f in db.scalars(q).all()
    ]


# ---------------------------------------------------------------------------
# 别名归一：规划（纯函数，可离线单测）+ 应用
# ---------------------------------------------------------------------------

# 泛称/职衔停用表：这些是「对某类人的称呼」，不是某个人的名字。
# 与抽取器里的 _DESC_STOP 同类（词表护栏，不是算法）。实测它们正是同族规则的
# 主要错误来源：`姐儿 ⊂ 凤姐儿`、`元帅 ⊂ 康元帅`、`夫人 ⊂ {王夫人,邢夫人}`
# ——按字符串包含会把这些泛称并到某个具体角色身上，属于静默篡改身份。
_GENERIC_REFERENT = {
    "姐儿", "姑娘", "丫头", "婆子", "夫人", "太太", "奶奶", "娘娘",
    "元帅", "将军", "都督", "大人", "老爷", "公子", "少爷", "相公",
    "师父", "长老", "道长", "大师", "先生", "陛下", "大王", "主公",
    "众人", "世人", "在下", "诸位", "人家", "旁人", "众人", "小厮",
}


def _is_glue_led(word: str) -> bool:
    """以粘连引导字开头 → 多半是文本碎片而非人名（「听宝玉」「听贾母」）。

    只判**头部**：尾部的「宝玉听」「凤姐道」已由抽取器的 Pass 1.6 粘连归一处理，
    此处再判一次会把「王守道」这类真实名误砍成「王守」（实测语料粘连体已为 0，
    重复判只增加风险不增收益）。头部碎片（听X）Pass 1.6 抓不到——它的判据是
    「末字是粘连尾字」，而「听宝玉」末字是「玉」——所以这里补上。
    """
    return bool(word) and word[0] in _GLUE_TAIL_CHARS


def plan_alias_merges(char_facts: list[dict], source: str = "generated") -> dict:
    """规划别名合并：返回 {"auto": [...], "review": [...]}。

    auto 项形如 {"alias": 短名, "canonical": 规范名, "reason": ...}，
    语义是「把 alias 并入 canonical」。

    纯函数 + 全部输出按名字排序：结果不依赖 dict/set 迭代序，
    否则同语料会跑出不同合并集（抽取器里刚修过同类确定性 bug）。
    """
    metas: dict[str, dict] = {}
    for f in char_facts:
        if f.get("type") != T_CHARACTER:
            continue
        p = f.get("props") or {}
        metas[f["key"]] = {
            "mentions": p.get("mentions") or 0,
            "speaking": bool(p.get("speaking")),
            "named": bool(p.get("named")),
            "first_segment": f.get("first_segment"),
            "last_segment": f.get("last_segment"),
        }
    keys = sorted(metas)
    key_set = set(keys)
    # 所有已入账名字的首字：用于识别「并列姓名连排」被切出的碎片（见自动档 ② 第 ⑤ 道闸门）
    first_chars = {k[0] for k in keys if k}

    auto: list[dict] = []
    review: list[dict] = []
    consumed: set[str] = set()

    # ── 自动档 ① 粘连/切片残渣：长名以粘连引导字开头，尾部是真名 ──
    for w in keys:
        if len(w) >= 3 and _is_glue_led(w) and w[1:] in key_set:
            auto.append(
                {
                    "alias": w,
                    "canonical": w[1:],
                    "reason": f"粘连引导字「{w[0]}」开头的文本碎片，尾部是已入账真名",
                }
            )
            consumed.add(w)

    # ── 自动档 ② 生成时序归一（只对 generated 生效）──
    # 模型先写全名、后写简称 → 由出现时序定方向，无需猜身份。
    # 闸门有四道，缺一不可（每道都对应一条实测出来的错误合并）：
    #   ① 短名不在泛称表里 —— 拦 `姐儿→凤姐儿`、`元帅→康元帅`
    #   ② 长名已获说话/命名证据 —— 拦 `满面→容满面`（残片从不说话）
    #   ③ 恰好一个长名候选 —— 拦 `宝玉→甄宝玉`（当 贾宝玉 也在账上时属歧义）
    #   ④ 被丢掉的部分 ≤2 字 —— 掉 1 个姓名尾字或 1 个姓氏，不是掉一个短语
    #   ⑤ 长名不是「短名 + 另一个已入账名字的首字」—— 拦并列片语：
    #      实测 `贾母王`(＝贾母＋王夫人)、`袭人麝`(＝袭人＋麝月)、`李纨探`(＝李纨＋探春)、
    #      `尤氏李`、`湘云宝`、`宝钗袭`。中文并列姓名连排无标点，切片必然长这样；
    #      这类碎片带说话证据也照样出现（`袭人麝` 实测 speaking=True），故不能只靠 ②。
    #      只在「丢尾字」方向生效：`薛宝钗` 丢的是姓氏前缀 `薛`，与并列无关。
    if source == "generated":
        for short in keys:
            if short in consumed or short in _GENERIC_REFERENT:
                continue
            cands = [
                long_
                for long_ in keys
                if long_ != short
                and len(long_) > len(short)
                and (long_.startswith(short) or long_.endswith(short))
                and not _is_glue_led(long_)
                and long_ not in consumed
                and len(long_) - len(short) <= 2
                and metas[long_]["first_segment"] is not None
                and metas[short]["first_segment"] is not None
                # 短名出现在后：它是对已建立全名的回指
                and metas[short]["first_segment"] > metas[long_]["first_segment"]
                # ② 长名必须已获说话或显式命名证据 —— 拦 `满面→容满面`（残片不说话）。
                #    刻意**不收**「mentions≥2」：并列切片照样高频出现（`贾母王` 实测 76 次），
                #    用出现次数当「是真角色」的凭据等于把闸门 ② 让开。
                and (metas[long_]["speaking"] or metas[long_]["named"])
                # ⑤ 并列姓名连排切片
                and not (
                    long_.startswith(short)
                    and len(long_) == len(short) + 1
                    and long_[-1] in first_chars
                )
            ]
            if len(cands) == 1:
                auto.append(
                    {
                        "alias": short,
                        "canonical": cands[0],
                        "reason": (
                            f"简称「{short}」首现于第 {metas[short]['first_segment']} 段，"
                            f"晚于全名「{cands[0]}」的第 {metas[cands[0]]['first_segment']} 段"
                        ),
                    }
                )
                consumed.add(short)
            elif len(cands) > 1:
                review.append(
                    {
                        "alias": short,
                        "candidates": sorted(cands),
                        "reason": "多个更长名都可作为规范名，规则无法判定（禁自动裁决）",
                    }
                )

    # ── 待裁决：包含关系但证据不足的，一律只提案 ──
    for short in keys:
        if short in consumed:
            continue
        cands = sorted(
            long_
            for long_ in keys
            if long_ != short
            and len(long_) > len(short)
            and (long_.startswith(short) or long_.endswith(short))
            and not _is_glue_led(long_)
        )
        if cands:
            review.append(
                {
                    "alias": short,
                    "candidates": cands,
                    "reason": (
                        "字符串包含关系不足以判定身份：实测同族规则的错误合并示例有 "
                        "姐儿→凤姐儿（泛称）、元帅→康元帅（官职）、满面→容满面（残片）、"
                        "宝玉→甄宝玉（不同人）；且敬称方向不可判（尤二姐→尤二 / 马道婆→马道）"
                    ),
                }
            )
    return {"auto": auto, "review": review}


def apply_merges(
    db, project_id: int, merges: list[dict], source: str = "generated", *, commit: bool = True
) -> dict:
    """应用合并：账本记别名 + 图谱按规范名改写（重指向边、去重、删被吸收节点）。

    图谱改写是「实体分裂」的真正修复动作：不这么做的话，别名只活在账本里，
    `get_reminders` 仍会把同一人的两个名字当成两个不同的锁定角色注入提示词。

    `commit=False` 时只 flush——供重建原语把整段重建收进一个事务。
    """
    if not merges:
        return {"merged": 0, "nodes_removed": 0, "edges_dropped": 0}

    facts = db.scalars(
        select(Fact).where(
            Fact.project_id == project_id,
            Fact.source == source,
            Fact.type == T_CHARACTER,
        )
    ).all()
    by_key = {f.key: f for f in facts}

    nodes = db.scalars(
        select(KgNode).where(
            KgNode.project_id == project_id,
            KgNode.source == source,
            KgNode.type == T_CHARACTER,
        )
    ).all()
    node_by_name: dict[str, KgNode] = {}
    for n in nodes:
        nm = (n.props or {}).get("name")
        if nm:
            node_by_name.setdefault(nm, n)

    n_merged = n_removed = n_edges_dropped = 0
    for m in merges:
        alias, canon = m["alias"], m["canonical"]
        if alias == canon:
            continue

        cf = by_key.get(canon)
        if cf is not None:
            head = dict(cf.props or {})
            tail = dict((by_key.get(alias).props or {})) if by_key.get(alias) else {}
            for k, v in tail.items():
                if isinstance(v, int) and not isinstance(v, bool):
                    head[k] = (head.get(k) or 0) + v
                elif isinstance(v, bool):
                    head[k] = bool(head.get(k)) or v
                elif k not in head:
                    head[k] = v
            cf.props = head
            al = list(cf.aliases or [])
            if alias not in al:
                al.append(alias)
            # 别名之间不重复挂（alias 自己原先的别名一起搬过来）
            for extra in (by_key.get(alias).aliases or []) if by_key.get(alias) else []:
                if extra not in al and extra != canon:
                    al.append(extra)
            cf.aliases = sorted(al)
            if by_key.get(alias) is not None and by_key[alias] is not cf:
                db.delete(by_key[alias])
            by_key[canon] = cf

        n_merged += 1

        # —— 图谱改写 ——
        loser = node_by_name.pop(alias, None)
        winner = node_by_name.get(canon)
        if loser is None:
            continue
        lp = dict(loser.props or {})
        if winner is not None:
            wp = dict(winner.props or {})
            for k, v in lp.items():
                if isinstance(v, int) and not isinstance(v, bool):
                    wp[k] = (wp.get(k) or 0) + v
                elif isinstance(v, bool):
                    wp[k] = bool(wp.get(k)) or v
                elif k not in wp:
                    wp[k] = v
            winner.props = wp
            for attr, fn in (("first_chapter", min), ("last_chapter", max)):
                old, new = getattr(winner, attr), getattr(loser, attr)
                if new is not None:
                    setattr(winner, attr, new if old is None else fn(old, new))
            winner_id = winner.id
        else:
            loser.props = {**lp, "name": canon}
            winner_id = loser.id
            node_by_name[canon] = loser

        if winner_id == loser.id:
            continue  # 自己没有变（只是改名），无需重指向

        # 被吸收节点的边重指向赢家；重指向后成自环或重复的边丢弃
        edges = db.scalars(
            select(KgEdge).where(
                (KgEdge.from_id == loser.id) | (KgEdge.to_id == loser.id)
            )
        ).all()
        seen = {
            (e.from_id, e.to_id, (e.props or {}).get("relation"))
            for e in db.scalars(
                select(KgEdge).where(
                    (KgEdge.from_id == winner_id) | (KgEdge.to_id == winner_id)
                )
            ).all()
        }
        for e in edges:
            f = winner_id if e.from_id == loser.id else e.from_id
            t = winner_id if e.to_id == loser.id else e.to_id
            if f == t:
                db.delete(e)
                n_edges_dropped += 1
                continue
            sig = (f, t, (e.props or {}).get("relation"))
            if sig in seen:
                db.delete(e)
                n_edges_dropped += 1
                continue
            e.from_id, e.to_id = f, t
            seen.add(sig)
        db.delete(loser)
        n_removed += 1

    # —— 关系事实也要按规范名改写 ——
    # 只改角色事实是不够的：`陆见→秦九` 这条关系会留在账本里，成为第三个名字的
    # 残留引用；且它与 `陆见→秦九歌` 是同一段关系的两种写法，必须并成一条。
    if n_merged:
        fwd = {m["alias"]: m["canonical"] for m in merges if m.get("canonical")}
        rels = [f for f in db.scalars(
            select(Fact).where(
                Fact.project_id == project_id,
                Fact.source == source,
                Fact.type == T_RELATIONSHIP,
            )
        ).all()]
        by_key = {f.key: f for f in rels}
        for f in list(rels):
            props = dict(f.props or {})
            a = fwd.get(props.get("from"), props.get("from"))
            b = fwd.get(props.get("to"), props.get("to"))
            if a == props.get("from") and b == props.get("to"):
                continue
            if a == b:
                db.delete(f)  # 归一后成自环
                continue
            props["from"], props["to"] = a, b
            new_key = f"{a}→{b}"
            if new_key == f.key:
                f.props = props
                continue
            twin = by_key.get(new_key)
            if twin is not None:
                tp = dict(twin.props or {})
                segs = list(tp.get("segments") or [])
                for s in (props.get("segments") or []):
                    if s not in segs:
                        segs.append(s)
                tp["segments"] = sorted(segs)
                twin.props = tp
                db.delete(f)
            else:
                db.delete(f)
                fresh = Fact(
                    project_id=project_id, type=T_RELATIONSHIP, key=new_key,
                    props=props, aliases=[], source=f.source,
                    confidence=f.confidence,
                    first_segment=f.first_segment, last_segment=f.last_segment,
                )
                db.add(fresh)
                by_key[new_key] = fresh

    db.commit() if commit else db.flush()
    return {
        "merged": n_merged,
        "nodes_removed": n_removed,
        "edges_dropped": n_edges_dropped,
    }


def resolve_aliases(
    db, project_id: int, source: str = "generated", *, commit: bool = True
) -> dict:
    """一步到位：读账本 → 规划 → 应用自动档。返回统计与待裁决提案。

    待裁决项**不写库、不改图**，原样返回给调用方展示——自动档之外一律不动，
    这是 Q6 的硬要求（别名词表未就绪前禁用自动裁决）。
    """
    chars = get_facts(db, project_id, type_=T_CHARACTER, source=source)
    plan = plan_alias_merges(chars, source=source)
    stats = apply_merges(db, project_id, plan["auto"], source=source, commit=commit)
    return {
        "auto": plan["auto"],
        "review": plan["review"],
        "review_count": len(plan["review"]),
        **stats,
    }


def canonical_name(db, project_id: int, name: str, source: str = "generated") -> str:
    """把任意书写形态映射到规范名（查不到就原样返回）。"""
    for f in db.scalars(
        select(Fact).where(
            Fact.project_id == project_id,
            Fact.source == source,
            Fact.type == T_CHARACTER,
        )
    ).all():
        if f.key == name:
            return f.key
        if name in (f.aliases or []):
            return f.key
    return name
