"""专名表的加载与缓存——按小说隔离，不跨书复用。

## 为什么要单独一层

`app/style/entities.py` 只有算法（PMI 召回、残片剔除、脱敏），
`app/style/entity_judge.py` 负责构建（含 LLM 判定）。这里负责**取用**：

- 什么时候该有表（原创模式需要，续写/角色聊天不需要）
- 表存哪里（按 novel_id 缓存，A 书的人名不能进 B 书的表）
- 什么时候失效（语料变了就要重建）

## 缓存位置

`raw_novels.clean_report` 是个 JSON 列，已经被清洗报告占用。专名表另存
`raw_novels` 旁边不现实（表结构固定），所以落磁盘：

    <数据目录>/entities/novel_{novel_id}.json

带 `source_size` 与 `source_chunks` 做失效判断：语料重新上传后 chunk 数或
总字数变化，就重建。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_CACHE_DIR_ENV = "ENTITY_CACHE_DIR"


def _cache_dir() -> Path:
    """专名表缓存目录。可用环境变量覆盖（测试/多实例部署）。"""
    d = os.environ.get(_CACHE_DIR_ENV)
    if d:
        p = Path(d)
    else:
        # 落盘位置：项目内的 .cache/entities（跟仓库走，换机器不用重算；
        # 之前落在用户主目录下的本机缓存目录，别人 clone 下来对不上）。
        p = Path(os.environ.get("ENTITY_CACHE_DIR") or (Path(__file__).resolve().parents[3] / ".cache" / "entities"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def table_path(novel_id: int) -> Path:
    return _cache_dir() / f"novel_{novel_id}.json"


def verdict_cache_path(novel_id: int) -> Path:
    """逐词判定缓存：建表被中断后可续跑，不用重烧已判过的批次。"""
    return _cache_dir() / f"verdicts_novel_{novel_id}.json"


def entity_entries(table: dict[str, Any]) -> list[str]:
    """专名表里**实际有的**专名（跨类别合并）。

    ## 为什么不能只看「表是不是空字典」

    表里除了实体列表还有元数据（version / audited / source_chunks / novel_id…），
    所以一张**实体全空**的表 `bool(table)` 仍为真。而 `Redactor` 对空表是**空操作**
    （没有 pattern → 原文原样返回）—— 于是「以为脱敏了」，实际把带专名的原文
    直接送进提示词。判定必须看实体本身。
    """
    if not isinstance(table, dict):
        return []
    from app.style.entities import PLACEHOLDER_PREFIX

    out: list[str] = []
    for layer in PLACEHOLDER_PREFIX:
        for name in table.get(layer) or []:
            if isinstance(name, str) and name.strip():
                out.append(name)
    return out


def load_table(novel_id: int) -> dict[str, Any]:
    """读专名表。不存在或损坏返回 {}。"""
    p = table_path(novel_id)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 表损坏不该阻断生成
        return {}


def save_table(novel_id: int, table: dict[str, Any]) -> Path:
    p = table_path(novel_id)
    p.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def is_stale(table: dict[str, Any], *, chunks: int, chars: int) -> bool:
    """表是否该重建。

    语料重新上传后 chunk 数或总字数会变；两个都记，任一变化即失效。

    另外：**实体全空的表一律重建**（除非已标记 `judge_failed`）。旧版本写下的空表
    意味着「LLM 判定失败被吞掉」，它带着新鲜的 source_chunks/source_chars 指纹，
    会被当成有效表一直复用 → 脱敏永远是空操作 → 参考书专名直接进提示词。
    """
    if not table:
        return True
    if not entity_entries(table) and not table.get("judge_failed"):
        return True
    if table.get("source_chunks") != chunks:
        return True
    prev = table.get("source_chars")
    if isinstance(prev, int) and chars:
        # 允许 2% 抖动（清洗规则微调会改字数，不该触发全量重建）
        if abs(prev - chars) / max(chars, 1) > 0.02:
            return True
    return False


def _read_corpus(db, novel_id: int) -> tuple[str, int, int]:
    """读整本语料正文，返回 (text, chunks, chars)。

    **必须同时兼容两种 db**：
      - sqlite3.Connection（脚本 / probe）
      - SQLAlchemy Session（生成路径）

    SQLAlchemy 2 要求原生 SQL 显式包 `text()`，否则报
    "Textual SQL expression ... should be explicitly declared as text(...)"。
    这个差异收敛在此处，调用方不必关心。
    """
    sql = "SELECT text FROM novel_chunks WHERE novel_id = ? ORDER BY chapter_no, idx"

    def _rows_via_session() -> list[str] | None:
        try:
            from sqlalchemy import text as sql_text
            from sqlalchemy.orm import Session as OrmSession
        except ImportError:
            return None
        if not isinstance(db, OrmSession):
            return None
        res = db.execute(
            sql_text("SELECT text FROM novel_chunks WHERE novel_id = :nid "
                     "ORDER BY chapter_no, idx"),
            {"nid": novel_id},
        ).fetchall()
        return [r[0] for r in res]

    rows = _rows_via_session()
    if rows is None:
        rows = [r[0] for r in db.execute(sql, (novel_id,)).fetchall()]

    text = "\n".join(t or "" for t in rows)
    return text, len(rows), len(text)


def ensure_table(
    db,
    novel_id: int,
    *,
    provider=None,
    log=None,
) -> dict[str, Any]:
    """按需取专名表：有且不陈旧就复用，否则构建并落盘。

    构建只发生在原创模式第一次生成时，之后零成本。
    """
    from app.style.entity_judge import build_entity_table

    def _log(m: str) -> None:
        if log:
            log(m)

    text, chunks, chars = _read_corpus(db, novel_id)
    if not text:
        return {}

    cached = load_table(novel_id)
    if not is_stale(cached, chunks=chunks, chars=chars):
        _log(f"复用专名表：{table_path(novel_id)}（{chunks} chunk / {chars} 字）")
        return cached

    _log(f"构建专名表：{chunks} chunk / {chars} 字（PMI 召回 + LLM 判定）")
    if provider is None:
        from app.llm.provider import get_llm_provider
        provider = get_llm_provider()

    from app.style.entities import PLACEHOLDER_PREFIX

    table = build_entity_table(text, provider, log=_log,
                               cache_path=verdict_cache_path(novel_id))
    table["source_chunks"] = chunks
    table["source_chars"] = chars
    table["novel_id"] = novel_id
    if not entity_entries(table):
        # 判定失败（LLM 不可用 / 候选全被判为泛称）——标记为**不可用**，
        # 落盘是为了不重复烧钱，但调用方必须据此关闭「原文进提示词」这条路。
        table["judge_failed"] = True
        _log(
            "专名表构建后仍为空（判定失败或候选全被判为泛称）——"
            "样例脱敏将关闭，参考原文不会进入提示词"
        )
    save_table(novel_id, table)
    _log(
        "专名表就绪：" + "、".join(
            f"{k} {len(table.get(k) or [])}" for k in PLACEHOLDER_PREFIX
        )
    )
    return table
