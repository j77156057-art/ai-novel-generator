"""模型可自主调用的「外部素材检索」工具。

阶段边界
--------
本工具**只在初稿阶段**暴露给模型（`TOOL_STAGE = "draft"`）；重写轮不提供，
避免同一章重复联网计费。模型自己决定要不要调用、调用几次、查什么，
本模块不预生成查询词，也不对题材做任何假设。

边界与降级
----------
- 每章调用次数、单次超时、本章总预算、返回条数、上下文字数全部来自
  `BriefSettings`，调用点不写死。
- 任何异常（未配置后端/超时/结构异常/预算耗尽）都返回空串并落轨迹，
  **不抛出**，因此不会阻断正文生成。
- 开关关闭时 `call()` 立即返回空串：不联网、不读缓存、不写轨迹。

留痕
----
默认写 `retrieval_logs`（复用 `app.retrieval.trace.record_trace`）：
`agent_kind="system"`、`tool_name="search_external_material"`、`query`、
`returned_chars`、`latency_ms`、`status`、`budget_remaining`。
是否被采用由调用方在段末调用 `mark_used()` 回填到 `soft_flags.brief_used`。

素材库（两阶段）
----------------
检索结果**先进素材库**（`library.MaterialLibrary`），再以「候选清单」交给模型：
候选只含 id、入库天数、来源查询与标题，不含正文；模型调用
`select_external_material` 指定 id 后，被选中的条目才渲染进 prompt。
本章没用上的条目留在库里，后续章节可作为候选再次被选中。
注入条数（`injected`）在选中时记录，正文实际采纳条数（`adopted`）由调用方在
拿到正文后调用 `note_chapter_text()` 用词面重合粗判。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .backend import (
    FreeSearchBackend,
    JSONSearchBackend,
    SearchBackend,
    SearchError,
    SearchResult,
)
from .cache import BriefCache
from .library import MaterialEntry, MaterialLibrary, overlap_ratio
from .render import HEADER, clean_text, filter_results, render_block
from .settings import BriefSettings

TOOL_NAME = "search_external_material"
SELECT_TOOL_NAME = "select_external_material"
TOOL_STAGE = "draft"

LogSink = Callable[[dict[str, Any]], None]

#: 候选清单里摘要截断长度。只给标题分不出广告/聚合页，给一小段即可判断，不占正文。
CANDIDATE_DIGEST_CHARS = 40

#: 预算用尽时的工具返回文本。
#:
#: 不能返回空串：模型会把「空结果」理解成「这次查询没查到」，继续换关键词重试，
#: 把剩余轮次全耗在注定被拒的检索上（真实链路实测 5 轮全部如此）。
#: 这段文本只进工具消息，不会进正文——只有 `select` 的返回才拼进 prompt。
BUDGET_EXHAUSTED_TEXT = (
    "检索预算已用尽（本章检索次数或总时长已到上限），不能再检索。"
    f"请直接从已有候选清单中用 {SELECT_TOOL_NAME} 选择本章要用的条目；"
    "若一条都用不上，直接回复「无」。"
)


@dataclass
class _ChapterState:
    session_id: str
    project_id: int | None = None
    novel_id: int | None = None
    segment_no: int | None = None
    turn_no: int = 1
    started_at: float = 0.0
    #: 已消耗的**检索**耗时（秒）。预算只算检索本身，不算模型决策时间。
    spent_s: float = 0.0
    calls: int = 0
    cache_hits: int = 0
    errors: int = 0
    empty: int = 0
    returned_chars: int = 0
    queries: list[str] = field(default_factory=list)
    # 本章候选/注入/采纳：只记量，不参与生成判定。
    candidates: int = 0
    select_calls: int = 0
    injected: int = 0
    adopted: int = 0
    injected_entries: list[MaterialEntry] = field(default_factory=list)


class BriefTool:
    def __init__(
        self,
        settings: BriefSettings | None = None,
        *,
        backend: SearchBackend | None = None,
        cache: BriefCache | None = None,
        library: MaterialLibrary | None = None,
        db: Any | None = None,
        log_sink: LogSink | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or BriefSettings.from_env()
        self._backend = backend or _build_backend(self.settings)
        self._cache = cache or BriefCache(
            self.settings.cache_dir,
            self.settings.brief_version,
            similarity_threshold=self.settings.cache_similarity,
            scan_limit=self.settings.cache_scan_limit,
            ttl_s=self.settings.cache_ttl_s,
        )
        self._library = library or MaterialLibrary(
            _default_library_dir(self.settings),
            ttl_s=self.settings.library_ttl_s,
            max_items=self.settings.library_max_items,
        )
        self._db = db
        self._log_sink = log_sink or (self._default_log_sink(db) if db is not None else None)
        self._clock = clock
        self._sleep = sleep
        self._state: _ChapterState | None = None

    # ------------------------------------------------------------------ 工具定义
    def spec(self) -> dict[str, Any]:
        """OpenAI 风格 function schema。只在初稿阶段传入 tools 列表。"""
        return {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": (
                    "当你判断本章缺少可写的具体细节（环境、器物、职业流程、专业信息等）时调用，"
                    "检索结果会先存入素材库，并返回可选候选（含 id 与入库时间）。"
                    "需要正文素材时，再用 select_external_material 选择具体条目。"
                    "不需要补充素材时不要调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "要查的具体问题或关键词，由你根据本章需要自行决定",
                        },
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": self.settings.max_results,
                            "description": "希望返回的片段条数，可省略",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def select_spec(self) -> dict[str, Any]:
        """第二阶段：从候选清单里挑条目注入正文。"""
        return {
            "type": "function",
            "function": {
                "name": SELECT_TOOL_NAME,
                "description": (
                    "从 search_external_material 返回的候选清单中选择本章要用的素材，"
                    "被选中的条目才会作为参考资料注入。不需要素材时不要调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": max(1, self.settings.library_candidates),
                            "description": "候选列表方括号内的 id，按需要顺序排列",
                        }
                    },
                    "required": ["ids"],
                },
            },
        }

    # ------------------------------------------------------------------ 章节生命周期
    def start_chapter(
        self,
        *,
        session_id: str,
        project_id: int | None = None,
        novel_id: int | None = None,
        segment_no: int | None = None,
        turn_no: int = 1,
    ) -> None:
        now = self._clock()
        self._state = _ChapterState(
            session_id=session_id,
            project_id=project_id,
            novel_id=novel_id,
            segment_no=segment_no,
            turn_no=turn_no,
            started_at=now,
            spent_s=0.0,
        )

    def chapter_stats(self) -> dict[str, Any]:
        st = self._state
        if st is None:
            return {
                "calls": 0,
                "cache_hits": 0,
                "errors": 0,
                "empty": 0,
                "returned_chars": 0,
                "queries": [],
                "candidates": 0,
                "select_calls": 0,
                "injected": 0,
                "adopted": 0,
            }
        return {
            "calls": st.calls,
            "cache_hits": st.cache_hits,
            "errors": st.errors,
            "empty": st.empty,
            "returned_chars": st.returned_chars,
            "queries": list(st.queries),
            "candidates": st.candidates,
            "select_calls": st.select_calls,
            "injected": st.injected,
            "adopted": st.adopted,
        }

    # ------------------------------------------------------------------ 调用入口
    def dispatch(self, arguments: dict[str, Any], *, name: str | None = None) -> str:
        """供工具分发器调用；未知工具名返回空串，不抛错。"""
        if name == SELECT_TOOL_NAME:
            if not isinstance(arguments, dict):
                return self.settings.empty_text
            return self.select(arguments.get("ids") or [])
        if name is not None and name != TOOL_NAME:
            return self.settings.empty_text
        if not isinstance(arguments, dict):
            return self.settings.empty_text
        return self.call(arguments.get("query", ""), max_results=arguments.get("max_results"))

    def call(self, query: str, max_results: int | None = None) -> str:
        if not self.settings.enabled:
            return self.settings.empty_text
        try:
            return self._call(query, max_results)
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须降级
            if self._state is not None:
                self._state.errors += 1
            self._log(
                query=str(query or "")[: self.settings.max_query_chars],
                status="error",
                returned_chars=0,
                latency_ms=0,
                extra={"error": repr(exc)},
            )
            return self.settings.empty_text

    def select(self, ids: Iterable[str] | str) -> str:
        """模型选定要用的素材：只把选中的条目渲染进 prompt，并标记已使用。

        未知 id（模型编的）直接跳过；一条都没命中时返回空串，不影响生成。
        选中的条目可以来自本章检索，也可以来自库里早先入库、一直没用上的条目。
        """
        if not self.settings.enabled:
            return self.settings.empty_text
        try:
            st = self._ensure_state()
            wanted = _as_ids(ids)
            st.select_calls += 1
            picked = self._library.select(wanted)
            results = [SearchResult(title=e.title, snippet=e.snippet) for e in picked]
            block = render_block(results, max_chars=self.settings.max_block_chars) if picked else ""
            st.injected += len(picked)
            st.injected_entries.extend(picked)
            self._log(
                query=",".join(wanted)[: self.settings.max_query_chars],
                status="ok" if block else "empty",
                returned_chars=len(block),
                latency_ms=0,
                extra={
                    "selected": len(picked),
                    "requested": len(wanted),
                    "injected": len(picked),
                    "ids": [e.id for e in picked],
                },
            )
            return block
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须降级
            if self._state is not None:
                self._state.errors += 1
            self._log(query="", status="error", returned_chars=0, latency_ms=0, extra={"error": repr(exc)})
            return self.settings.empty_text

    def note_chapter_text(self, text: str) -> dict[str, int]:
        """段末调用：词面重合粗判「注入的素材有几条真被正文用上」。

        只统计本章 `select` 注入过的条目；重合阈值见 `adopt_overlap`。
        结果落一条观测记录，同时写进 `chapter_stats()`。
        """
        st = self._state
        if st is None or not st.injected_entries:
            return {"injected": st.injected if st else 0, "adopted": 0}
        body = str(text or "")
        threshold = max(0.0, self.settings.adopt_overlap)
        adopted = [
            e
            for e in st.injected_entries
            if overlap_ratio(e.snippet or e.title, body) >= threshold
        ]
        st.adopted = len(adopted)
        self._log(
            query="",
            status="ok",
            returned_chars=0,
            latency_ms=0,
            extra={
                "injected": st.injected,
                "adopted": st.adopted,
                "adopt_ratio": round(st.adopted / st.injected, 3) if st.injected else 0.0,
                "adopted_ids": [e.id for e in adopted],
            },
        )
        return {"injected": st.injected, "adopted": st.adopted}

    # ------------------------------------------------------------------ 内部实现
    def _call(self, query: str, max_results: int | None) -> str:
        st = self._ensure_state()
        q = " ".join(str(query or "").split())[: self.settings.max_query_chars]
        if not q:
            st.empty += 1
            self._log(query="", status="empty", returned_chars=0, latency_ms=0)
            return self.settings.empty_text

        if st.calls >= max(0, self.settings.max_calls_per_chapter):
            self._log(query=q, status="budget_exhausted", returned_chars=0, latency_ms=0)
            return BUDGET_EXHAUSTED_TEXT

        # 预算只统计检索耗时：模型决策一轮可能十几秒，若按墙钟从 start_chapter
        # 起算，慢模型会把整章预算吃光，导致真实检索一次都跑不成（2026-11-15 实测
        # 模型首轮 12.7s，8 次检索全部 budget_exhausted）。
        if st.spent_s >= max(0.0, self.settings.chapter_budget_s):
            self._log(query=q, status="budget_exhausted", returned_chars=0, latency_ms=0)
            return BUDGET_EXHAUSTED_TEXT

        limit = self._resolve_limit(max_results)
        st.calls += 1
        st.queries.append(q)

        started = self._clock()
        cached = self._cache.get(q, limit)
        if cached is not None:
            st.cache_hits += 1
            kept, stats = filter_results(
                _as_results(cached.results),
                min_snippet_chars=self.settings.min_snippet_chars,
                query=q,
                coverage_min=self.settings.query_coverage_min,
            )
            block, lib = self._absorb(st, q, kept)
            st.returned_chars += len(block)
            latency = self._clock() - started
            st.spent_s += latency
            self._log(
                query=q,
                status="ok" if block else "empty",
                returned_chars=len(block),
                latency_ms=int(latency * 1000),
                extra={
                    "cached": True,
                    "similarity": round(cached.similarity, 4),
                    "filter": stats.to_dict(),
                    "library": lib,
                },
            )
            return block

        results, error = self._search_with_retry(q, limit)
        latency = int((self._clock() - started) * 1000)
        if error is not None:
            st.errors += 1
            # 被反爬拦下单独记 blocked，与普通失败分开统计；两者都降级为空素材。
            self._log(
                query=q,
                status="blocked" if error.blocked else "error",
                returned_chars=0,
                latency_ms=latency,
                extra={"error": str(error)[:200], "blocked": bool(error.blocked)},
            )
            return self.settings.empty_text
        if not results:
            st.empty += 1
            self._log(query=q, status="empty", returned_chars=0, latency_ms=latency)
            return self.settings.empty_text

        # 缓存存原始结果：过滤规则以后调整时，旧缓存仍可重新筛，不必整体失效。
        self._cache.put(q, limit, [r.__dict__ for r in results])
        kept, stats = filter_results(
            results,
            min_snippet_chars=self.settings.min_snippet_chars,
            query=q,
            coverage_min=self.settings.query_coverage_min,
        )
        block, lib = self._absorb(st, q, kept)
        st.returned_chars += len(block)
        self._log(
            query=q,
            status="ok" if block else "empty",
            returned_chars=len(block),
            latency_ms=latency,
            extra={"cached": False, "filter": stats.to_dict(), "library": lib},
        )
        return block

    def _absorb(
        self, st: _ChapterState, query: str, kept: list[Any]
    ) -> tuple[str, dict[str, Any]]:
        """过滤后的条目先入库，再把库里的可用条目作为候选清单返回。

        候选只列 id / 入库天数 / 来源查询 / 标题，不含摘要正文：
        模型选定后由 `select()` 注入，本章没用上的继续留在库里。
        """
        cleaned = [
            SearchResult(
                title=clean_text(getattr(r, "title", "") or ""),
                snippet=clean_text(getattr(r, "snippet", "") or ""),
            )
            for r in kept
        ]
        added = self._library.add(query, cleaned)
        cands = self._library.candidates(self.settings.library_candidates)
        st.candidates = len(cands)
        block = _render_candidates(cands, max_chars=self.settings.max_block_chars)
        info = {
            "added": len(added),
            "candidates": len(cands),
            "size": self._library.size(),
        }
        return block, info

    def _search_with_retry(self, query: str, limit: int) -> tuple[list[Any] | None, SearchError | None]:
        """返回 (结果, 错误)。结果为空列表表示检索成功但无结果。"""
        st = self._ensure_state()
        last_error: SearchError | None = None
        results: list[Any] | None = None
        for attempt in range(max(0, self.settings.retries_per_call) + 1):
            # 预算只累计检索本身（含退避），模型决策时间不计入。
            if st.spent_s >= max(0.0, self.settings.chapter_budget_s):
                break
            attempt_started = self._clock()
            try:
                results = self._backend.search(query, limit)
                st.spent_s += self._clock() - attempt_started
                return results, None
            except SearchError as exc:
                st.spent_s += self._clock() - attempt_started
                last_error = exc
                # 被反爬拦下不重试：硬闯只会更糟，降级是设计的一部分。
                if exc.blocked or not exc.retryable or attempt >= self.settings.retries_per_call:
                    break
                wait = min(
                    self.settings.backoff_base_s * (2**attempt),
                    self.settings.backoff_cap_s,
                )
                if wait > 0:
                    self._sleep(wait)
                    st.spent_s += wait
            except Exception as exc:  # noqa: BLE001 - 非 SearchError 也视为失败
                st.spent_s += self._clock() - attempt_started
                last_error = SearchError(repr(exc), retryable=False)
                break
        return None, last_error

    def _resolve_limit(self, requested: int | None) -> int:
        cap = max(1, self.settings.max_results)
        try:
            value = int(requested) if requested is not None else cap
        except (TypeError, ValueError):
            value = cap
        return max(1, min(value, cap))

    def _ensure_state(self) -> _ChapterState:
        if self._state is None:
            self.start_chapter(session_id=f"brief-tool-{int(time.time())}")
        assert self._state is not None
        return self._state

    # ------------------------------------------------------------------ 留痕
    def _log(
        self,
        *,
        query: str,
        status: str,
        returned_chars: int,
        latency_ms: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self._log_sink is None:
            return
        st = self._state
        payload: dict[str, Any] = {
            "session_id": st.session_id if st else "brief-tool",
            "tool_name": TOOL_NAME,
            "agent_kind": "system",
            "turn_no": st.turn_no if st else 1,
            "segment_no": st.segment_no if st else None,
            "project_id": st.project_id if st else None,
            "novel_id": st.novel_id if st else None,
            "query": query,
            "returned_chars": returned_chars,
            "latency_ms": latency_ms,
            "status": status,
            "budget_remaining": {
                "calls": max(
                    0,
                    self.settings.max_calls_per_chapter - (st.calls if st else 0),
                ),
                "seconds": round(
                    max(0.0, self.settings.chapter_budget_s - (st.spent_s if st else 0.0)),
                    2,
                ),
            },
        }
        if extra:
            payload["extra"] = extra
        try:
            self._log_sink(payload)
        except Exception:  # noqa: BLE001 - 留痕失败不得影响生成
            pass

    def _default_log_sink(self, db: Any) -> LogSink:
        def sink(payload: dict[str, Any]) -> None:
            from app.models import RetrievalLog
            from app.retrieval.trace import record_trace

            extra = payload.pop("extra", None) or {}
            row = record_trace(
                db,
                session_id=payload["session_id"],
                tool_name=payload["tool_name"],
                agent_kind=payload["agent_kind"],
                turn_no=payload["turn_no"],
                segment_no=payload["segment_no"],
                project_id=payload["project_id"],
                novel_id=payload["novel_id"],
                query=payload["query"],
                returned_chars=payload["returned_chars"],
                latency_ms=payload["latency_ms"],
                status=payload["status"],
                budget_remaining=payload["budget_remaining"],
            )
            flags: dict[str, Any] = {}
            if "cached" in extra:
                flags["brief_cached"] = bool(extra["cached"])
            if "similarity" in extra:
                # 近义命中时把 Jaccard 相似度落进软档，便于事后判断缓存是否「并错了」。
                flags["brief_similarity"] = extra["similarity"]
            if "filter" in extra:
                # 过滤计数：原始/保留/丢弃条数与各原因分布，能看出筛了多少、为什么筛。
                flags["brief_filter"] = extra["filter"]
            if "library" in extra:
                # 候选条数：这次检索后库里交给模型的可选条目有几条。
                flags["brief_candidates"] = int(extra["library"].get("candidates") or 0)
            if "injected" in extra:
                # 本次实际注入正文的素材条数（select 阶段）。
                flags["brief_injected"] = int(extra["injected"])
            if "adopted" in extra:
                # 正文里词面重合达到阈值的条数，与 injected 一起看才知道素材有没有帮上忙。
                flags["brief_adopted"] = int(extra["adopted"])
            if "adopt_ratio" in extra:
                flags["brief_adopt_ratio"] = extra["adopt_ratio"]
            if "error" in extra:
                flags["brief_error"] = str(extra["error"])[:200]
            if flags and isinstance(row, RetrievalLog):
                row.soft_flags = {**(row.soft_flags or {}), **flags}
                try:
                    # record_trace 已提交过行本身；软档是之后补的，需要再提交一次才落库。
                    self._db.commit()
                except Exception:  # noqa: BLE001 - 留痕失败不得影响生成
                    self._db.rollback()

        return sink

    def mark_used(self, used: bool = True) -> int:
        """段末回填「这一章的素材是否被采用」，写入 soft_flags.brief_used。"""
        st = self._state
        if st is None or self._db is None:
            return 0
        try:
            from sqlalchemy import select

            from app.models import RetrievalLog

            rows = (
                self._db.execute(
                    select(RetrievalLog).where(
                        RetrievalLog.session_id == st.session_id,
                        RetrievalLog.tool_name == TOOL_NAME,
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.soft_flags = {**(row.soft_flags or {}), "brief_used": used}
            return len(rows)
        except Exception:  # noqa: BLE001
            return 0


def _default_library_dir(settings: BriefSettings) -> str:
    """素材库目录：显式配置优先，否则与缓存同级的 `brief_library`。"""
    if settings.library_dir:
        return settings.library_dir
    return str(Path(settings.cache_dir).parent / "brief_library")


def _render_candidates(entries: list[MaterialEntry], *, max_chars: int) -> str:
    """候选清单：id + 入库时间 + 来源查询 + 标题 + 摘要截断。

    只给标题时模型分不出广告/聚合页，只能按标题猜内容；因此附一小段摘要（40 字内）
    作为判断依据，但正文仍要等 `select()` 才注入。
    入库时间必须写在每一条上——跨章复用时模型要知道这是几天前抓的，
    几十天前的条目可能已经过期，得由它自己决定还要不要用。
    """
    if not entries:
        return ""
    lines = [
        HEADER,
        f"（以下为素材库候选，仅列概要；要用的条目请调用 {SELECT_TOOL_NAME}，"
        "用方括号内的 id 指定。）",
    ]
    used = sum(len(line) + 1 for line in lines)
    for entry in entries:
        title = clean_text(entry.title) or "（无标题）"
        snippet = clean_text(entry.snippet)
        digest = snippet[:CANDIDATE_DIGEST_CHARS]
        if len(snippet) > CANDIDATE_DIGEST_CHARS:
            digest += "…"
        tail = f"｜摘要：{digest}" if digest else ""
        line = f"[{entry.id}] {entry.age_days} 天前入库 · 来源查询「{entry.query}」· {title}{tail}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


def _as_ids(raw: Any) -> list[str]:
    """把模型传来的 ids 统一成字符串列表；单字符串也接受。"""
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _build_backend(settings: BriefSettings) -> SearchBackend:
    """按 `backend` 配置选后端；调用方无感。

    - json：付费供应商，用 `search_url` + key；
    - free：百度结果页优先、DuckDuckGo 回退，无需 key；
    - auto：配了 `search_url` 就走 JSON，没配就走免费（默认值即此分支）。
    - 免费路径上 `headless=True` 时换成无头浏览器实现，协议与调用方不变。
    """
    kind = (settings.backend or "auto").strip().lower()
    if kind == "json" or (kind == "auto" and settings.search_url):
        return JSONSearchBackend(
            settings.search_url,
            settings.search_api_key,
            timeout_s=settings.timeout_s,
        )
    if settings.headless:
        from .browser import BrowserSearchBackend

        return BrowserSearchBackend(
            timeout_s=settings.timeout_s,
            min_interval_s=settings.min_interval_s,
            user_agent=settings.user_agent,
            allow_fallback=settings.allow_fallback,
        )
    return FreeSearchBackend(
        timeout_s=settings.timeout_s,
        min_interval_s=settings.min_interval_s,
        user_agent=settings.user_agent,
        allow_fallback=settings.allow_fallback,
    )


def _as_results(raw: list[dict[str, Any]]) -> list[Any]:
    from .backend import SearchResult

    out = []
    for item in raw:
        if isinstance(item, dict):
            out.append(
                SearchResult(
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("snippet") or ""),
                    url=str(item.get("url") or ""),
                )
            )
    return out
