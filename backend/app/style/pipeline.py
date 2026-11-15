"""把「参考语料 → 文体档案」这条链路接起来。

这一层做三件事，缺一不可：

1. **拟合前先体检**（`corpus_health`）。盗版语料里的 SEO 水印会把档案拖偏——
   实测斗破那本，水印让自身留出集 p90 从 1.083 虚高到 1.364、跨语料最坏重叠率
   从 7% 涨到 43%。拿被污染的语料拟合，等于给生成结果划了一条错的及格线。
2. **档案要能落库、能失效**。档案整轮生成必须恒定（它进的是静态 system 块，
   一变就击穿提示词缓存），所以拟合一次存下来；但语料选择或粒度变了必须重算，
   因此把 `source_ids` + `unit_chars` + `version` 一起写进档案当指纹。
3. **体检结论随档案一起存**。判据可能出错，所以结论必须能被人复核：
   检出串 + 计数 + 句尾比例 + 原文样本，全部留在档案里，不另存别处。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.corpus_health import find_repeated_watermarks, strip_watermarks
from app.style.stylometry import MEASUREMENT_VERSION, PROFILE_VERSION, fit_profile, fit_profile_v2

#: 每本语料最多留几条水印结论、每条留几个原文样本（防止档案 JSON 膨胀）
MAX_MARK_ROWS = 5
MAX_SAMPLES_PER_MARK = 2

#: 单本档案 JSON 的字节上限。档案要存进 JSON 列、还要在预检响应里下发摘要，
#: 膨胀到几十 KB 会让两边都难受。实测一本 38 万字语料约 6.8KB，余量充足。
PROFILE_MAX_JSON_BYTES = 32_768

#: 档案的**形状**修订号（本模块产出的 JSON 结构，不是 `stylometry.PROFILE_VERSION`
#: 那个统计口径版本）。**任何会改变档案字段的改动都必须把它 +1。**
#:
#: 为什么必须单列：指纹原来只有 source_ids/unit_chars/version，而 version 管的是
#: 特征集，管不到「档案长什么样」。于是给档案加字段时，已经落库的老项目指纹
#: 依然匹配 → 永远沿用缺字段的旧档案，前端拿到的摘要也就永远少一块。
#: 实测踩过：加了 `marks[].final` 之后，预检响应里 `final` 一直是 KeyError。
PIPELINE_SCHEMA = "log_buckets_v1"

#: **规格书**（style spec：可执行的高层规格 + 逐章规格）的版本。
#:
#: 规格书本体 `build_spec` 属台账第 8 批（`app/style/spec.py`）；本次只先把
#: 「规格书是哪一版」记进档案指纹，让「档案与规格书不同版」这件事**可查询**。
#: 等规格书搬进来时，把它挪到 `app/style/spec.py` 并换成真版本号。
SPEC_VERSION = 1

#: `corpus_health[*].marks[]` 的字段清单 —— 档案里存什么、预检响应下发什么，
#: 以这一处为准。预检接口按这份清单逐字段挑，避免 `**m` 展开时被多余字段带出去。
MARK_FIELDS = (
    "text",
    "count",
    "final",
    "final_ratio",
    "para_end_ratio",
    "nested_variants",
    "samples",
)

#: 预检摘要里 `marks[]` 的字段 = 档案字段 + `novel`。
#: 档案按书名分组（`corpus_health` 的键就是书名），条目本身不带书名；
#: 摊平成数组下发时必须补上，否则前端不知道该条来自哪本语料。
DIGEST_MARK_FIELDS = ("novel",) + MARK_FIELDS


def _inspect_one(text: str) -> tuple[str, list[dict]]:
    """对单本语料体检 + 清洗，返回 (清洗后文本, 体检结论)。

    三层清洗：
    1. SEO 水印（find_repeated_watermarks / strip_watermarks）
    2. 完本感言 + 跨书简介（strip_finale_and_cross_novel）
    3. 清洗报告合并返回，供人复核
    """
    from app.corpus_health import strip_finale_and_cross_novel

    hits = find_repeated_watermarks(text)
    rows = []
    for h in hits[:MAX_MARK_ROWS]:
        rows.append({
            "text": h["text"],
            "count": h["count"],
            # `final` 是「紧跟句末标点」的次数，也就是真正会被删掉的次数 ——
            # 没有它就无法复核「删了多少字」这笔账
            "final": h["final"],
            "final_ratio": h["final_ratio"],
            "para_end_ratio": h["para_end_ratio"],
            "nested_variants": h["nested_variants"][:8],
            "samples": h["samples"][:MAX_SAMPLES_PER_MARK],
        })
    cleaned = strip_watermarks(text, hits) if hits else text
    # 完本感言 + 跨书简介清洗（无论有没有水印都要做）
    cleaned, finale_report = strip_finale_and_cross_novel(cleaned)
    if finale_report["hits"]:
        rows.append({
            "text": "完本感言/跨书简介",
            "count": len(finale_report["hits"]),
            "final": finale_report["removed"],
            "final_ratio": 1.0,
            "para_end_ratio": 0.0,
            "nested_variants": [],
            "samples": finale_report["hits"][:MAX_SAMPLES_PER_MARK],
        })
    return cleaned, rows


def build_reference_profile(
    texts: dict[str, str],
    *,
    unit_chars: int = 3000,
    label: str = "",
    clean: bool = True,
) -> dict:
    """在若干本参考语料上炼一份档案（含语料体检记录）。

    **先清洗再拟合**：清洗是逐本的，因为各本污染程度不同。

    `clean=False` 时**仍然体检**，只是不删。这条不能省：如果跳过检测，档案里就
    没有任何污染记录，提示块也就没法如实说「这些数字是脏的」—— 那等于用
    一个看起来很干净的结果去骗人。检测与清洗必须是两件独立的事。
    """
    cleaned: list[str] = []
    health: dict[str, dict] = {}
    for name, t in texts.items():
        stripped, rows = _inspect_one(t)
        cleaned.append(stripped if clean else t)
        health[name] = {
            "chars_in": len(t),
            "chars_out": len(stripped) if clean else len(t),
            "removed": len(t) - len(stripped) if clean else 0,
            "removed_ratio": (
                round((len(t) - len(stripped)) / len(t), 6) if clean and t else 0.0
            ),
            "marks": rows,
        }

    # 方案 B「对数粒度 profile」：fit 3 套 bucket（base/2 + base + base*2）
    profile = fit_profile_v2(cleaned, unit_chars=unit_chars, label=label)
    profile["clean"] = bool(clean)
    profile["corpus_health"] = health
    profile["source_chars"] = sum(len(c) for c in cleaned)
    return profile


def profile_fingerprint(source_ids: list[int], unit_chars: int) -> dict:
    """档案失效判据：语料集合 / 粒度 / 统计口径 / **量尺** / **规格书** / 档案形状。

    两个新增键（2026-11-15，搬自 main）：

    - `measurement`：`stylometry.MEASUREMENT_VERSION` —— 同一段文字被**怎么量**。
      换分母 / 改分句 / 加特征都会让它变，老档案的数值与新测量**不是一把尺**；
    - `spec`：`SPEC_VERSION` —— 档案与规格书不同版时，规格书里的阈值是按另一版
      统计量定的。

    ## ⚠️ 这两键**不会**让老档案自动重拟

    老档案的 fingerprint 里没有这两个键 ⇒ 字典不相等。若照旧走「不等就重拟」，
    加指纹本身就把所有项目档案悄悄重蒸了一遍 —— 那是**用户没同意的写操作**。

    所以 `project_profile` 把失配分成两类（见 `profile_mismatch_kind`）：
    **硬失配**（语料集合 / 粒度 / 结构版本 / schema 变了）照旧重拟；
    **软失配**（只差 measurement / spec 这两把新尺）⇒ **继续用老档案**，
    只把状态标成 `stale`，由用户决定要不要重蒸。
    """
    return {
        "source_ids": sorted(int(i) for i in source_ids),
        "unit_chars": int(unit_chars),
        "version": PROFILE_VERSION,
        "schema": PIPELINE_SCHEMA,
        "measurement": MEASUREMENT_VERSION,
        "spec": SPEC_VERSION,
    }


#: 软失配的键：只差这些 ⇒ 老档案**仍然可用**，只是数值来自旧尺。
SOFT_MISMATCH_KEYS = ("measurement", "spec")


def profile_mismatch_kind(cached_fp, want: dict) -> str:
    """档案指纹与期望值的关系：`match` / `stale`（软失配） / `hard`（必须重拟） / `none`。

    - `match`：逐键相等；
    - `stale`：除了 `measurement` / `spec` 之外全相等（老档案缺这两把新尺）；
    - `hard`：语料集合 / 粒度 / 结构版本 / schema 变了 —— 继续用会算错；
    - `none`：老档案里根本没有 fingerprint（早于指纹机制）。
    """
    if not isinstance(cached_fp, dict):
        return "none"
    if cached_fp == want:
        return "match"
    hard_keys = [k for k in want if k not in SOFT_MISMATCH_KEYS]
    if all(cached_fp.get(k) == want.get(k) for k in hard_keys):
        return "stale"
    return "hard"


def _load_novel_text(db: Session, novel_id: int) -> tuple[str, str]:
    from app.models import NovelChunk, RawNovel

    novel = db.get(RawNovel, novel_id)
    if novel is None:
        raise LookupError(f"参考语料 {novel_id} 不存在")
    chunks = db.scalars(
        select(NovelChunk.text).where(NovelChunk.novel_id == novel_id).order_by(NovelChunk.idx)
    ).all()
    return (novel.title or f"novel:{novel_id}"), "\n".join(chunks)


def project_profile(
    db: Session,
    project,  # noqa: ANN001  (app.models.Project，避免循环引用不做类型标注)
    *,
    unit_chars: int,
    force: bool = False,
) -> dict | None:
    """取项目当前该用的文体档案；没有参考语料则返回 None（自由生成，不注入）。

    已存且指纹一致就直接返回 —— 这是**功能要求**而不是优化：档案进的是静态
    system 块，每次生成都变会击穿提示词缓存。
    """
    ids = [int(i) for i in (project.reference_novel_ids or [])]
    if not ids:
        return None

    want = profile_fingerprint(ids, unit_chars)
    cached = project.style_profile or {}
    if not force and isinstance(cached, dict) and cached:
        kind = profile_mismatch_kind(cached.get("fingerprint"), want)
        # 软失配（只差 measurement / spec）：**沿用老档案，不重拟**。
        # 重拟是写操作（会覆盖 project.style_profile 并 commit），
        # 不能让「加了两把新尺」这件事替用户做决定。状态由
        # `profile_fingerprint_state` 暴露，用户想重蒸时显式 force。
        if cached.get("fingerprint") == want or kind == "stale":
            return cached

    texts: dict[str, str] = {}
    for nid in ids:
        name, text = _load_novel_text(db, nid)
        texts[name] = text

    profile = build_reference_profile(
        texts, unit_chars=unit_chars, label=f"project:{project.id}"
    )
    profile["fingerprint"] = want
    project.style_profile = profile
    db.commit()
    return profile


def profile_fingerprint_state(
    db: Session,
    project,  # noqa: ANN001
    *,
    unit_chars: int,
) -> dict:
    """项目档案与当前「量尺/规格书」的关系（**只读**，用于查询是否需要重蒸）。

    返回：

    - `state`：`match` / `stale`（软失配：老档案来自旧尺，仍可用）/
      `hard`（必须重拟：语料集合、粒度、结构版本变了）/ `none`（无指纹）/
      `absent`（项目还没有档案）；
    - `mismatched`：哪些键对不上（`stale`/`hard` 时非空）；
    - `cached` / `expected`：两边指纹，便于人复核；
    - `needs_recompute`：只有 `hard` / `none` 为 True —— 这两种情况下老档案
      **算错了**；`stale` 不置 True（那是「尺不一样」，不是「算错」）。

    **本函数不写库**：重蒸必须由调用方显式 `project_profile(..., force=True)`。
    """
    ids = [int(i) for i in (project.reference_novel_ids or [])]
    want = profile_fingerprint(ids, unit_chars)
    cached = project.style_profile or {}
    if not isinstance(cached, dict) or not cached:
        return {
            "state": "absent", "mismatched": [], "cached": None,
            "expected": want, "needs_recompute": bool(ids),
        }
    kind = profile_mismatch_kind(cached.get("fingerprint"), want)
    cached_fp = cached.get("fingerprint") if isinstance(cached.get("fingerprint"), dict) else {}
    mismatched = [k for k in want if cached_fp.get(k) != want.get(k)]
    return {
        "state": kind,
        "mismatched": mismatched,
        "cached": cached_fp or None,
        "expected": want,
        "needs_recompute": kind in ("hard", "none"),
    }


def project_profile_safe(
    db: Session,
    project,  # noqa: ANN001
    *,
    unit_chars: int,
    force: bool = False,
) -> dict | None:
    """生成链路上的安全包装。

    文体档案是**增强**，不是生成的前提。语料缺失、库没迁移、拟合失败……
    任何一种情况都只能降级成「不注入」，**绝不能让整次生成失败** ——
    用户点「生成」要的是正文，不是一份档案。
    """
    import logging

    try:
        return project_profile(db, project, unit_chars=unit_chars, force=force)
    except Exception as exc:  # noqa: BLE001 —— 这里就是要兜住一切
        logging.getLogger(__name__).warning(
            "文体档案不可用，本次生成跳过文风档案块：%s: %s", type(exc).__name__, exc
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def health_summary(profile: dict | None) -> str:
    """一行体检摘要，给日志和人看。"""
    if not profile:
        return "无档案"
    health = profile.get("corpus_health") or {}
    removed = sum(v.get("removed", 0) for v in health.values())
    flagged = [
        f"{k}:{v['marks'][0]['text']}×{v['marks'][0]['count']}"
        for k, v in health.items()
        if v.get("marks")
    ]
    src = sum(v.get("chars_in", 0) for v in health.values())
    tail = ("；检出 " + "，".join(flagged)) if flagged else "；未检出注入串"
    return f"{len(health)} 本 / {src} 字，清洗删除 {removed} 字（{removed/max(src,1)*100:.2f}%）{tail}"
