"""风格样例采样：从参考语料里挑几段原文，作为 few-shot 样例注入 user prompt。

## 为什么需要它

文风档案（`style/stylometry.py`）给模型的是**目标数字**（句长、对白占比、标点密度），
数字能验收「像不像」，但**教不会模型怎么写**——「句子怎么呼吸、段落怎么接、
什么时候突然用一个短句」这类东西只能靠原文 in-context 学习。

调研结论（写作蒸馏器 writing-dna-skill / 小说风格蒸馏器 writer-novel-style-distiller）：
每次生成前必须挑几篇与本次写作体裁、题材最接近的原文通读，原文用于校准
「分层产物描述不出来的句子的呼吸感与断句节奏」。

## 与 RAG 的分工

RAG（`hybrid_search`）按查询词检索**与当前剧情相关**的片段，目标是「找素材」。
本模块按结构位置均匀采样**与当前 project 体裁接近**的段落，目标是「学写法」。
两者目标不同，**绝不共用同一路检索**，且必须**排除 RAG 已命中的 chunk**——
同一段原文同时作为「相关剧情」和「风格样例」出现两次，模型会困惑。

## 采样策略

- **体裁接近优先**：优先从 genre 与 project.genre 匹配的语料里采样
- **成熟期优先**：只在**后半程**的章节里取 —— 早期章节文风还没稳定，
  拿开篇代表全书是错的（与叙事档案的选规则口径一致）
- **样例要够长**：一段 200~600 字的样例看不出「句子怎么呼吸」，
  实测（真书 + 真模型）600 字以内的样例几乎不改变生成文本的句法
- **均匀分布**：在成熟期范围内均匀分布，覆盖不同节奏
- **段落边界完整**：每个样例是完整的自然段（以换行切），不截断句子
- **不进 system prompt**：样例每次生成现采样，内容随段号变化，进 system 会击穿缓存
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion.clean import clean_consumption_text, strip_site_marker_lines

#: **章标题行**（整行就是「第 N 章」「番外第十章」这类）。
#:
#: 为什么不当样例：它是章节结构标记，不是正文行。摆给模型看，模型可能学会
#: 在正文中间冒出一行章节标题。判据要求**整行**只有标题（两端都有边界），
#: 所以正文里出现的「第 N 章」字样不会被误伤。
_CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:第[零〇一二三四五六七八九十百千万两\d]{1,8}章"
    r"|番外[^\s\x0d\n]{0,12}章"
    r"|楔子|序章|后记|尾声|终章)\s*$"
)

#: 每本语料最多取几个样例（总量 = min(per_novel × 语料数, max_total)）
PER_NOVEL = 3
#: 样例总量上限（避免挤占生成空间）
MAX_TOTAL = 3
#: 单个样例的**地板**：短于此直接丢（撑不起段落编排分析）
MIN_SAMPLE_CHARS = 200
#: 单个样例的**目标长度**：够长才看得出「句子怎么呼吸」。
#: 候选里够长的样例足够时只挑这些；不够时（短语料）照样给短的 —— 宁可短也别没有。
TARGET_SAMPLE_CHARS = 700
#: 单个样例最多字符数（太长会挤占生成空间，与 unit_chars 对齐的量级）
MAX_SAMPLE_CHARS = 1400
#: 样例至少要有几行，才**看得出行文节奏**。
#:
#: 为什么单行不够：真书的行有两种 —— 长叙述行（可达 1000+ 字）与短对白行
#: （中位 37 字）。只取**一行**长叙述，样例就是「一堵墙」：模型看不到这本书
#: 「叙述与台词同段、偶尔单独成行」的混合节奏，只能按自己的习惯写对白
#: （实测：参考书含引号行中位 47 字、裸台词行占 23%；模型写出的是
#: 12 字、68% —— 一行一句「“别动。”」）。
#:
#: 取**连续几行**（>= 3 行）才能把这个节奏原样摆给模型看。
MIN_SAMPLE_LINES = 3


def _paragraphs(text: str) -> list[str]:
    """按换行切自然段，丢弃空白段。"""
    return [p.strip() for p in (text or "").split("\n") if p.strip()]


def _split_into_samples(text: str) -> list[str]:
    """把一段 chunk 文本切成若干「段落级样例」。

    每个样例 = 连续 1~2 个自然段拼接，长度落在
    [MIN_SAMPLE_CHARS, MAX_SAMPLE_CHARS] 区间内。
    过短的段落单独丢弃（撑不起段落编排分析）。

    ## 先摘掉站点/公告行

    盗版站会把 SEO 词**插进句子中间**（`立即隐相最新章节去别院拿来三根黄瓜`）、
    把广告整行塞进章节（`请记住本书首发域名：…`）。这种行原词已被替换、
    修不回来，而它正要被当成**「这本书的写法」**摆给模型看 —— 模型会把
    站点词当普通词学。实测（真书）：清洗模块能删掉 87% 的站点垃圾，
    剩下 182 行全是这种句子中段污染的残句。

    所以这里**整行不取用**（不是修字）：这些行对「学写法」没有价值。
    入库语料本身不动 —— 那是另一回事（见 `ingestion/clean.py` 的留痕约定）。
    """
    text, _dropped = strip_site_marker_lines(text)
    paras = [p for p in _paragraphs(text) if not _CHAPTER_HEADING_RE.match(p)]
    samples: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paras:
        if size and size + len(p) + 1 > MAX_SAMPLE_CHARS:
            joined = "\n".join(buf)
            if len(joined) >= MIN_SAMPLE_CHARS:
                samples.append(joined)
            buf, size = [], 0
        buf.append(p)
        size += len(p) + 1
    if buf:
        joined = "\n".join(buf)
        if len(joined) >= MIN_SAMPLE_CHARS:
            samples.append(joined)
    return samples


def _genre_match(project_genre: str | None, novel_genre: str | None) -> bool:
    """粗粒度体裁匹配：完全相等或互相包含即算匹配。

    不做同义词归一（仙侠/修真/玄幻的等价关系需要词表），只做字面匹配——
    匹配不上就退化为均匀采样，不阻断。
    """
    if not project_genre or not novel_genre:
        return False
    a, b = str(project_genre).strip(), str(novel_genre).strip()
    return a == b or a in b or b in a


def _pick_evenly(items: list, n: int) -> list:
    """从列表里均匀取 n 个（首尾必取，中间均匀分布）。"""
    if not items or n <= 0:
        return []
    if len(items) <= n:
        return list(items)
    # 均匀取 n 个：步长 = (len-1)/(n-1)，取整后去重保序
    step = (len(items) - 1) / max(n - 1, 1)
    picks = sorted({round(i * step) for i in range(n)})
    return [items[i] for i in picks[:n]]


def _mature_pool(pool: list[tuple]) -> list[tuple]:
    """只保留**成熟期**的样例候选（有样例的章节取后一半）。

    为什么：早期章节文风还没稳定，拿开篇代表全书是错的。
    与叙事档案注入的选规则口径一致（见 `narrative._select_dim_rules`）。

    只有一两章的语料（样本本身就少）不过滤 —— 过滤会把候选清空，
    那就等于「没有样例」，比「样例来自早期章节」更糟。
    """
    if len(pool) < 4:
        return pool
    chapters = sorted({p[0] for p in pool})
    if len(chapters) < 2:
        return pool
    half = len(chapters) // 2
    mature = set(chapters[half:])
    kept = [p for p in pool if p[0] in mature]
    return kept or pool


def sample_style_exemplars(
    db: Session,
    project,
    novel_ids: list[int],
    exclude_texts: set[str] | None = None,
    exclude_chunk_ids: set[int] | None = None,
    per_novel: int = PER_NOVEL,
    max_total: int = MAX_TOTAL,
    visibility=None,
) -> list[dict]:
    """从参考语料采样风格样例。

    返回 [{novel_id, novel_title, genre, text, chunk_idx}, ...]，按 novel_id 排序。
    失败（语料不存在/无段落/全被排除）返回空列表，**不抛异常**——样例是增强，
    不是生成的前提，绝不能让整次生成失败。
    """
    from app.models import NovelChunk, RawNovel

    exclude_texts = exclude_texts or set()
    exclude_chunk_ids = exclude_chunk_ids or set()
    project_genre = getattr(project, "genre", None)
    results: list[dict] = []

    # 体裁接近的语料排前面（用户可能选多本，优先取最像的）
    novels = []
    for nid in novel_ids:
        novel = db.get(RawNovel, nid)
        if novel is None:
            continue
        match = _genre_match(project_genre, getattr(novel, "genre", None))
        novels.append((match, nid, novel))
    novels.sort(key=lambda t: (not t[0], t[1]))  # 匹配的在前，同优先级按 id

    # 每本均匀取 per_novel 段，总量不超过 max_total
    budget = max_total
    for _match, nid, novel in novels:
        if budget <= 0:
            break
        try:
            rows = db.scalars(
                select(NovelChunk)
                .where(NovelChunk.novel_id == nid)
                .order_by(NovelChunk.chapter_no, NovelChunk.idx, NovelChunk.id)
            ).all()
        except Exception:  # noqa: BLE001 —— 单本失败不阻断其余语料
            logging.getLogger(__name__).warning("风格样例采样失败 novel_id=%s", nid)
            continue

        # 收集所有 chunk 里的段落级样例（排除已被 RAG 命中的）
        pool: list[tuple[int, int, int, str, dict]] = []
        for row in rows:
            if visibility is not None and visibility.reason(nid, row.chapter_no):
                # 归并记账（不是逐条）：真书 990 章时这一行会命中几千次，
                # 逐条记进诊断会把预检响应撑到几百 KB。见 VisibilityPolicy.exclude。
                visibility.exclude(
                    "exemplar_pool",
                    visibility.reason(nid, row.chapter_no),
                    novel_id=nid,
                    chapter_no=row.chapter_no,
                    chunk_id=row.id,
                )
                continue
            text, report = clean_consumption_text(row.text or "")
            text = text.strip()
            if not text:
                continue
            for s in _split_into_samples(text):
                if row.id in exclude_chunk_ids or s in exclude_texts:
                    continue
                pool.append((row.chapter_no or 0, row.idx or 0, row.id, s, report))

        if not pool:
            continue

        take = min(per_novel, budget)
        # 章节口径先定（成熟期），长度口径后定：成熟期里够长的样例够多就只挑这些，
        # 不够（短语料）就照样给短的 —— 宁可短也别一个样例都没有。
        mature = _mature_pool(pool)
        long_pool = [p for p in mature if len(p[3]) >= TARGET_SAMPLE_CHARS]
        pool_to_pick = long_pool if len(long_pool) >= take else mature
        # 再筛一层「看得出行文节奏」的（连续多行）：单行长叙述段是「一堵墙」，
        # 教不了模型这本书的对白形态（见 MIN_SAMPLE_LINES 的说明）。
        # 多行的不够就退回原池 —— 宁可形态不理想，也别没有样例。
        rhythm = [p for p in pool_to_pick if p[3].count("\n") + 1 >= MIN_SAMPLE_LINES]
        if len(rhythm) >= take:
            pool_to_pick = rhythm
        picked = _pick_evenly(pool_to_pick, take)
        for chapter_no, idx, chunk_id, s, report in picked:
            results.append({
                "novel_id": nid,
                "novel_title": novel.title or f"novel:{nid}",
                "genre": getattr(novel, "genre", None),
                "text": s,
                "chunk_idx": idx, "idx": idx,
                "source": "reference", "clean_report": report,
                "sources": [{"novel_id": nid, "chunk_id": chunk_id, "chapter_no": chapter_no,
                             "idx": idx, "source": "reference", "clean_report": report}],
                "chunk_id": chunk_id,
                "chapter_no": chapter_no,
            })
        budget -= len(picked)

    from app.retrieval.views import prepare_hits
    return prepare_hits(results)


def filter_single_character(
    exemplars: list[dict], character: str,
) -> list[dict]:
    """只留**被扮演角色**说过的话（角色聊天的写法样板）。

    ## 为什么需要这一步

    角色聊天要的是「**这个人**怎么说话」，不是「这本书怎么写」。
    之前的实现里这条规则**从来没有生效**：`ctx_profile` 按
    `ex.get("character_filtered")` 过滤，而这个标记**上游从来没设过**
    ⇒ 角色聊天的写法样板恒为空。

    ## 取法（机械、可复算）

    保留「**他参与的行**」：
    - 他是说话人（`老周道：“…”`）；
    - 或行里提到他（有人叫他、或他的话被引）。

    再带上**紧邻的上一行**做上下文（不然他的台词会失去来龙去脉）。

    ## 兜底

    筛完太短（< `MIN_SAMPLE_CHARS`）就**保留原样**并在诊断里标明 ——
    宁可给一段不够纯的样板，也别让角色一个写法样板都没有。
    """
    name = (character or "").strip()
    if not name:
        return []
    out: list[dict] = []
    for ex in exemplars or []:
        text = str(ex.get("text") or "")
        if not text:
            continue
        lines = text.split("\n")
        keep: list[str] = []
        for i, line in enumerate(lines):
            if name not in line:
                continue
            if i > 0 and lines[i - 1].strip() and (not keep or keep[-1] != lines[i - 1]):
                keep.append(lines[i - 1])
            keep.append(line)
        picked = "\n".join(keep).strip()
        if len(picked) < MIN_SAMPLE_CHARS:
            # 筛不出足够长度 ⇒ 留原文，但标明「未按角色过滤」
            new_ex = dict(ex)
            new_ex["character_filtered"] = False
            out.append(new_ex)
            continue
        new_ex = dict(ex)
        new_ex["text"] = picked
        new_ex["character_filtered"] = True
        out.append(new_ex)
    return out


def exemplars_from_character_hits(
    hits: list[dict], character: str, *, max_blocks: int = 3,
) -> list[dict]:
    """从**角色检索命中的块**里取他的台词，做成写法样板。

    ## 为什么不用「随机采样 + 按名字筛」

    实测（《北荒刀歌》）：随机采样挑出的 3 块样例里**一块都没有他** ——
    中文小说里角色常被**别称/特征代称**（「缺胳膊的老马夫」「马夫」），
    名字一次都不出现 ⇒ 按名字筛行必然筛空。

    ⇒ 正确顺序是**反过来**：先按角色检索定位「他出场/说过的块」，
    再从这些块里取他的台词做样板。检索那一步已经有子串硬约束
    （`retrieve_for_character`：名字出现过才命中），所以定位是准的。

    ## 长度下限 60 字

    这份样例的职责是「**这个角色怎么说话**」（腔调、句长、自称），
    不是「这本书怎么写」。两三行（约 60 字）就够看出腔调；
    低于此就丢掉这一块，别拿一两句充数。
    """
    name = (character or "").strip()
    if not name:
        return []
    out: list[dict] = []
    for h in hits or []:
        text = str(h.get("text") or "")
        if not text:
            continue
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        keep = [ln for ln in lines if name in ln]
        if not keep:
            continue
        picked = "\n".join(keep)
        if len(picked) < 60:
            continue
        out.append({
            "novel_id": h.get("novel_id"),
            "chunk_id": h.get("chunk_id"),
            "chapter_no": h.get("chapter_no"),
            "idx": h.get("idx"),
            "text": picked,
            "character_filtered": True,
            "source": "reference",
            "role": "character",
        })
        if len(out) >= max_blocks:
            break
    return out
