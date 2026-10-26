"""按用途分开的检索策略。

## 为什么需要分开

一条 `hybrid_search` 打天下不成立，因为三类需求的性质完全不同
（实测见 `docs/retrieval-strategies-design.md`）：

    A. 次要人物档案  → 按人名精确匹配。BM25 强项；
                      向量会把全书最长章节排第一（长文档偏置），反而稀释。
    B. 闲笔/生活质感  → 按语义检索。关键词匹配不够，要「气氛相似」。
    C. 剧情衔接      → 按章节位置取。根本不用检索，位置比相似度可靠。

对应三个函数：`retrieve_for_character` / `retrieve_for_ambience` /
`retrieve_for_continuation`。

## 与蒸馏的分工

蒸馏抽的是「怎么写」（手法、文风、主要人物的模式）；
检索补的是「写什么」（次要人物是谁、路过见过什么、作者那些不推动情节的闲笔）。

两者不重叠，也不能互相替代。
"""
from sqlalchemy import select

from app.models import NovelChunk
from app.ingestion.clean import clean_consumption_text
from app.retrieval.views import chunk_view, prepare_hits

#: 人名检索的结果里，这个角色必须真的出现过。
#: 名字本身几乎没有歧义，所以命中即采用；但为了防止「名字是别词的一部分」
#: 造成误命中，要求 chunk 里该名字至少出现 1 次（BM25 保证）且分数 > 0。
_CHAR_MIN_SCORE = 0.0


def retrieve_for_character(
    db, novel_id: int, character_name: str, k: int = 6,
) -> list[dict]:
    """角色定向检索：这个角色**自己出现过**的原文片段。

    ## 用途

    角色聊天时，被扮演的角色需要记得自己说过什么、经历过什么。
    当前设计（`PROFILE_RULES["roleplay"]["rag"] = False`）完全不检索，
    角色只能靠蒸馏出的「说话方式」表演——但不知道自己二十年前杀过谁。

    ## 为什么只用 BM25

    实测按人名检索（陆守一/楚惊蛰/阿拙）：

        BM25:   881, 881, 777, 881   ← 全部在她出场的章节
        向量:   990, 990, 881, 881   ← 990 是全书最长章（8 万字），偏置

    中文人名的关键词匹配非常准，向量的「语义相近」在这里是噪声：
    它会把任何提到相似概念的长章节排前面，跟这个角色没关系。

    ## 为什么不用 rag 的通用接口

    `retrieve_for_continuation` 拿整段剧情当 query，会返回「剧情相关」的
    章节而不是「这个角色相关」的章节。角色聊天要的是后者。
    """
    from app.retrieval.bm25 import bm25_scores

    name = (character_name or "").strip()
    if not name:
        return []

    chunks = db.scalars(
        select(NovelChunk)
        .where(NovelChunk.novel_id == novel_id)
        .order_by(NovelChunk.chapter_no, NovelChunk.idx)
    ).all()
    if not chunks:
        return []

    # 先用子串筛出「真提到这个名字」的块，再做 BM25 排序。
    # 只靠 BM25 不够：分词可能把「楚惊蛰」切成「楚/惊/蛰」，
    # 于是「宋家」「庭前」「白鹭」都会得分。子串筛是硬约束。
    mentioned = [c for c in chunks if name in (c.text or "")]
    if not mentioned:
        return []

    scores = bm25_scores(name, [c.text or "" for c in mentioned])
    order = sorted(range(len(mentioned)), key=lambda i: -scores[i])

    out: list[dict] = []
    for i in order[:k]:
        out.append({
            "chunk_id": mentioned[i].id,
            "chapter_no": mentioned[i].chapter_no,
            "idx": mentioned[i].idx,
            "score": round(scores[i], 6),
            **chunk_view(mentioned[i]),
            "role": "character",
            "character": name,
        })
    return prepare_hits(out)


def retrieve_for_ambience(
    db, novel_id: int, query: str, k: int = 3,
) -> list[dict]:
    """闲笔/生活质感检索：气氛相似的片段，不要求关键词命中。

    ## 用途

    作者的闲笔（吃饭、走路、闲聊、看景）不推动情节，因此**永远进不了蒸馏产物**
    ——蒸馏抽的是模式，而闲笔各不重复。但它们撑起小说的生活质地，
    续写时缺了就发干。

    这类需求关键词匹配不了：query 写「几个人围着桌子吃饭闲聊」，
    原文可能写「老掌柜让小灶多炒了两个菜，几个人就着酒说话」——
    一个字都不重合。

    ## 用加权融合

    实测这个 query：

        BM25:      571, 731, 412, 294
        向量:      990, 412, 353, 290
        加权 0.75: 412, 571, 731, 966   ← 966 是真正的酒楼说书段

    语义路把 966 拉了进来，关键词路保证了 412/571 这类字面接近的段落。
    """
    from app.retrieval.search import weighted_search

    q = (query or "").strip()
    if not q:
        return []
    hits = weighted_search(db, novel_id, q, k=k)
    for h in hits:
        h["role"] = "ambience"
    return hits


def retrieve_for_continuation(
    db, novel_id: int, chapter_hint: int | None, query: str | None = None,
    k_pos: int = 3, k_sem: int = 2,
) -> list[dict]:
    """续写：**位置检索为主**，语义检索补闲笔。

    ## 为什么位置优先

    续写最需要的是「紧接着当前剧情位置的前文」——这个需求不需要相似度，
    直接按章节号取就最准。实测第 300 章附近有 26 块可选，全部是真正相邻的内容，
    比任何检索都可靠。

    旧实现只用 `hybrid_search` 拿整段剧情当 query，结果命中第 554/545/529 章
    这类无关位置（分数 0.023-0.027，无区分度）。

    ## 位置块与语义块要用不同标记进 prompt

    位置块是「前文，必须衔接」；语义块是「同类场景，仅供气氛参考」。
    混在一起模型分不清哪个是事实、哪个是参考，会把参考语料的剧情写进本书。

    返回的每条带 `role`：`"position"` / `"ambience"`。
    """
    out: list[dict] = []

    # —— 位置检索：章节号附近 ——
    if chapter_hint is not None:
        lo, hi = max(1, chapter_hint - 1), chapter_hint
        near = db.scalars(
            select(NovelChunk)
            .where(
                NovelChunk.novel_id == novel_id,
                NovelChunk.chapter_no >= lo,
                NovelChunk.chapter_no <= hi,
            )
            .order_by(NovelChunk.chapter_no.desc(), NovelChunk.idx.desc())
        ).all()
        for c in reversed(near[:k_pos]):
            out.append({
                "chunk_id": c.id,
                "chapter_no": c.chapter_no,
                "idx": c.idx,
                "score": 1.0,
                **chunk_view(c),
                "role": "position",
            })

    # —— 语义补齐：同类场景的写法参考 ——
    if query:
        seen = {h["chunk_id"] for h in out}
        for h in retrieve_for_ambience(db, novel_id, query, k=k_sem):
            if h["chunk_id"] not in seen:
                out.append(h)

    return prepare_hits(out)


def render_retrieval_blocks(hits: list[dict]) -> list[tuple[str, str]]:
    """把检索结果分块渲染成 [(块标题, 正文), ...]，供 prompt 拼装。

    ## 为什么按 role 分块

    旧实现把所有 RAG 结果拼成一段纯文本，模型看不出哪块是「紧接着的前文」、
    哪块是「风格参考」。结果它会把参考语料的剧情当成已发生的事实写进本书
    ——这正是「原创模式检索会漏剧情」的同一个坑。

    分块后每块自带语义标签：

        位置块：「紧接着当前剧情位置的前文（必须与之衔接）」
        语义块：「参考语料里同类场景的写法（仅供气氛参考，不要复用其情节）」
        角色块：「这个角色在原文里的片段（用于把握其言行与经历）」
    """
    order = {"position": 0, "ambience": 1, "character": 2}
    groups: dict[str, list[str]] = {}
    for h in sorted(prepare_hits(hits), key=lambda x: order.get(x.get("role", ""), 9)):
        role = h.get("role") or "ambience"
        text = clean_consumption_text(h.get("text") or "")[0].strip()
        if not text:
            continue
        ch = h.get("chapter_no")
        label = f"（第 {ch} 章）" if ch is not None else ""
        groups.setdefault(role, []).append(f"{label}\n{text}")

    titles = {
        "position": "【前文衔接（紧接着当前剧情位置，必须与之自然衔接）】",
        "ambience": "【参考语料同类场景（学其气氛与写法，严禁复用其中情节与人名）】",
        "character": "【角色原文片段（用于把握该角色的言行、经历与关系）】",
    }
    return [(titles.get(r, "【检索上下文】"), "\n\n".join(v)) for r, v in groups.items()]

def retrieve_for_character_auto(
    db, project, novel_ids: list[int] | None, character_name: str, k: int = 6,
) -> list[dict]:
    """角色检索：自动判断这个角色该查哪个库。

    ## 为什么需要分流

    角色有两种来源，史料在完全不同的地方：

    A. **参考语料里的角色**（陆长风、裴白）
       原话和经历在 `novel_chunks`（参考书原文）。实测陆长风出现在 3494 个
       chunk、裴白 532 个。

    B. **用户原创角色**（沈砚、周郎中）
       参考书里**根本没有这个人**——实测 0 个 chunk。他的言行只在
       用户自己写的 `generations` 里（实测沈砚出现在 5 段正文中）。

    只查参考语料会让原创角色的检索永远返回空，角色聊天拿不到任何史料，
    开口全是空话——这正是修这个功能要解决的问题。

    ## 判断方式

    在参考语料里数一下这个名字出现几次：

        出现 > 0  → 参考语料角色，查 novel_chunks
        出现 == 0 → 原创角色，查 generations

    不需要 LLM，一次 count 查询。同名冲突的概率极低（原创角色名撞参考书
    角色名时，查参考语料反而更准——那本来就该是同一人）。

    ## 返回

    与 `retrieve_for_character` 同构，`role="character"`，
    额外带 `source="reference" | "generated"`，便于 prompt 标注。
    """
    from sqlalchemy import func, select

    from app.models import Generation, NovelChunk

    name = (character_name or "").strip()
    if not name:
        return []

    # 1) 参考语料里有没有这个人
    n_ref = 0
    for nid in (novel_ids or []):
        n_ref += db.scalar(
            select(func.count()).select_from(NovelChunk)
            .where(NovelChunk.novel_id == nid, NovelChunk.text.contains(name))
        ) or 0

    if n_ref > 0:
        out: list[dict] = []
        for nid in (novel_ids or []):
            for h in retrieve_for_character(db, nid, name, k=k):
                h["source"] = "reference"
                out.append(h)
            if len(out) >= k:
                break
        return out[:k]

    # 2) 原创角色：查用户自己写的段落
    gens = db.scalars(
        select(Generation)
        .where(Generation.project_id == project.id)
        .order_by(Generation.segment_no.desc())
    ).all()
    out = []
    for g in gens:
        text = g.text or ""
        if name not in text:
            continue
        # 只截这个名字附近的窗口，不要把整段 3000 字都塞进去
        i = text.find(name)
        lo = max(0, i - 300)
        hi = min(len(text), i + 500)
        out.append({
            "chunk_id": None,
            "chapter_no": None,
            "segment_no": g.segment_no,
            "idx": None,
            "score": 1.0,
            "text": text[lo:hi],
            "role": "character",
            "character": name,
            "source": "generated",
        })
        if len(out) >= k:
            break
    return out
