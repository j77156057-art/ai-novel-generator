"""canon 词表来源开关 —— **全仓库唯一的 canon 读取点**。

配置：``KG_CANON_SOURCE=legacy|cleaned|mixed``（默认 ``legacy``）。

- ``legacy``：仓库里原来那套 canon —— 从主库已有节点筛出来的约 300 条 v2 词表，
  实际读取的是原著层 sqlite 的 ``new_nodes`` / ``new_edges``。
  **它不是抽取链路的版本号**：抽取链路另有 v2（规则式 ``extractor``）与
  v3（LLM ``extractor_v3``），两条并行，与本开关无关。
- ``cleaned``：清洗后的 63 条 → ``data/canon_layers.json``（实体层 + 称呼层）。
- ``mixed``：分域判定，**不合成一份名单**——人物域用 cleaned 实体层；
  势力/地点域继续用 legacy 的 ``faction`` 节点，但必须过结构化后缀门
  （``FACTION_SUFFIXES`` / ``LOCATION_SUFFIXES``），否则整份 legacy 放回来
  仍是高噪声。两侧各自过滤后再并集；``power_system`` / ``item`` 整体退出。

为什么集中在唯一一处：抽取链路上只要有一处自行判断来源，就会出现
「一部分用 legacy、一部分用 cleaned」的混合口径，事后无法回答
「这批是用哪份词表抽的」。因此所有读取都必须经过本模块。

降级纪律：``cleaned`` 文件缺失 / 损坏 / 为空时，**明确 WARN 并降级 legacy**；
``mixed`` 在 cleaned 不可用时同样降级 legacy，在原著层不可用时降级 cleaned；
绝不返回空表——空表会让所有实体被判成 ``mech_only``，比直接报错更难查。

称呼层的语义：命中「许郎」归一为「许七安」，原文词保留在实体 aliases 与
mention 上下文里，关系不会拆成两个人。纯称谓（大哥/老爷/师父/夫人/皇帝/
银锣/道长）一律不入称呼层，只走共指消解。唯一例外是同时满足
「唯一原文直证 + 全库唯一归属」的纯封号（本批：长公主 → 怀庆），
判据与证据留在词表的 ``title_exception`` 字段里，可复查。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import sqlite3
from collections import defaultdict

SOURCE_LEGACY = "legacy"
SOURCE_CLEANED = "cleaned"
SOURCE_MIXED = "mixed"
VALID_SOURCES = (SOURCE_LEGACY, SOURCE_CLEANED, SOURCE_MIXED)

#: 混合源·势力域的结构化后缀门。只放行「名字本身像势力」的 legacy faction 节点。
FACTION_SUFFIXES = (
    "监", "院", "司", "门", "教", "党", "宗", "卫", "寺", "部", "军", "阁",
    "楼", "府", "会", "衙", "堂", "帮", "派", "局", "营", "盟", "殿", "社",
    "馆", "署", "台", "族", "国", "朝",
)
#: 混合源·地点域的结构化后缀门（legacy 把这些混在 faction kind 里，没有独立 location）。
LOCATION_SUFFIXES = (
    "城", "州", "宫", "观", "庵", "山", "关", "场", "庙", "斋", "居", "庄",
    "园", "驿", "站", "铺", "坊", "市", "街", "巷", "镇", "村", "湖", "江",
    "河", "岭", "谷", "渡", "桥", "塔", "陵", "墓", "店", "房", "屋",
)
#: 明确不是实体名的抽象词/碎片（结构后缀挡不住的少量噪声）。
NON_ENTITY_NAMES = frozenset({
    "官场", "后宫", "党派", "卫司", "行气法门", "书院", "衙门",
})
_BAD_NAME = re.compile(r"[，。！？、；：“”「」『』（）()\s0-9A-Za-z]")

#: 两层词表产物（实体层 + 称呼层）。测试/部署可用 KG_CANON_LAYERS 覆盖。
LAYERS_PATH = pathlib.Path(__file__).resolve().parent / "data" / "canon_layers.json"

_log = logging.getLogger(__name__)

#: JSON 词表缓存：(path, mtime_ns, size) → layers / alias index
_cache: dict = {"key": None, "layers": None, "index": None, "hash": ""}
#: 原著层词表 hash 缓存（只记内容，不记文件名）
_legacy_hash_cache: dict = {}


class CanonSourceError(RuntimeError):
    """cleaned 词表不可用且调用方要求 fail-fast 时抛出（默认不抛，走降级）。"""


def layers_path() -> pathlib.Path:
    raw = os.getenv("KG_CANON_LAYERS", "").strip()
    return pathlib.Path(raw) if raw else LAYERS_PATH


def source() -> str:
    """实际请求的来源（配置值；非法值按 legacy 处理并记 warn）。"""
    from app.config import settings

    raw = str(getattr(settings, "kg_canon_source", SOURCE_LEGACY) or "").strip().lower()
    if raw in VALID_SOURCES:
        return raw
    _log.warning("KG_CANON_SOURCE=%r 不是合法取值（legacy|cleaned|mixed），按 legacy 处理", raw)
    return SOURCE_LEGACY


def reset_cache() -> None:
    """清空缓存（测试与热更新用）。"""
    _cache.update({"key": None, "layers": None, "index": None, "hash": ""})
    _legacy_hash_cache.clear()


# ---------------------------------------------------------------------------
# 两层词表（cleaned）
# ---------------------------------------------------------------------------
def load_layers(path: str | pathlib.Path | None = None) -> dict | None:
    """读两层词表；失败返回 ``None``。

    调用方**必须**把 ``None`` 当「来源不可用」处理并降级 legacy，
    不能当成「空词表」——这是本模块最重要的约定。
    """
    p = pathlib.Path(path) if path else layers_path()
    try:
        stat = p.stat()
        key = (str(p), stat.st_mtime_ns, stat.st_size)
        if _cache["key"] == key and _cache["layers"] is not None:
            return _cache["layers"]
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        entities = data.get("entities") if isinstance(data, dict) else None
        if not entities:
            raise ValueError("entities 缺失或为空")
        if not isinstance(data.get("aliases"), list):
            raise ValueError("aliases 缺失")
        _cache.update({
            "key": key,
            "layers": data,
            "index": _build_index(data),
            "hash": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        })
        return data
    except Exception as exc:  # noqa: BLE001 —— 读取失败必须降级，不能外抛打断抽取
        _log.warning(
            "cleaned canon 词表读取失败（%s）：%s；本批降级 legacy，绝不按空表处理",
            p, exc,
        )
        return None


def _build_index(layers: dict) -> dict[str, dict]:
    """form → 称呼层行；``in_layer=false`` 的（纯封号等）不参与匹配。"""
    index: dict[str, dict] = {}
    for row in layers.get("aliases") or []:
        if row.get("in_layer") is False:
            continue
        form = str(row.get("form") or "").strip()
        if form and form not in index:
            index[form] = row
    return index


def _index() -> dict[str, dict]:
    """当前生效的称呼层索引；legacy 模式返回空 dict（行为不变）。"""
    if source() not in (SOURCE_CLEANED, SOURCE_MIXED):
        return {}
    if _cache["index"] is not None:
        return _cache["index"]
    layers = load_layers()
    return _cache["index"] if layers is not None and _cache["index"] is not None else {}


def content_hash(path: str | pathlib.Path | None = None) -> str:
    """两层词表内容 sha256（前 16 位）；读不到返回空串。"""
    p = pathlib.Path(path) if path else layers_path()
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 原著层（legacy）
# ---------------------------------------------------------------------------
def load_legacy(path: str | pathlib.Path) -> tuple[list[dict], list[dict]]:
    """只读打开原著层；只取 active 节点与 resolved 边。"""
    from app.kg.generated_extract import parse_aliases, parse_attrs

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        nodes = [
            {
                "name": row["canonical_name"],
                "kind": row["kind"],
                "aliases": parse_aliases(row["aliases"]),
                "attrs": parse_attrs(row["attrs"]),
                "mentions": row["mentions"] or 0,
                "last_chapter": row["last_chapter"],
            }
            for row in con.execute(
                """SELECT canonical_name, kind, aliases, attrs, mentions, last_chapter
                     FROM new_nodes WHERE review_status = 'active'"""
            )
        ]
        edges = [
            {
                "from": row["from_name"],
                "to": row["to_name"],
                "rel": row["rel_type"],
                "strength": float(row["strength"] or 0.5),
                "chapters": row["chapters"],
            }
            for row in con.execute(
                """SELECT from_name, to_name, rel_type, strength, chapters
                     FROM new_edges WHERE status = 'resolved'"""
            )
        ]
    finally:
        con.close()
    return nodes, edges


def legacy_hash(path: str | pathlib.Path | None) -> str:
    """原著层词表内容 hash（canonical_name 全集）；只记文件名对不上账。"""
    if not path:
        return ""
    p = pathlib.Path(path)
    try:
        stat = p.stat()
        key = (str(p), stat.st_mtime_ns, stat.st_size)
        if key in _legacy_hash_cache:
            return _legacy_hash_cache[key]
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            names = [row[0] for row in con.execute(
                "SELECT canonical_name FROM new_nodes WHERE review_status = 'active'"
            )]
        finally:
            con.close()
        digest = hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()[:16]
        _legacy_hash_cache[key] = digest
        return digest
    except Exception:  # noqa: BLE001 —— hash 只用于记账，读不到不影响主流程
        return ""


def load(path: str | pathlib.Path, *, source_override: str | None = None
         ) -> tuple[list[dict], list[dict]]:
    """唯一读取入口：按开关返回 canon 节点与边。

    - legacy：直接读原著层 sqlite（行为与改造前完全一致）
    - cleaned：实体层 + 称呼层；边从原著层过滤（两端都要落在实体层），
      这样人物之间的关系还能保留，势力/地点节点则随 cleaned 口径一起退出
    - mixed：人物域 cleaned + 势力/地点域 legacy（结构化后缀过滤），
      两侧各自过滤后并集；边要求两端都在并集里
    - cleaned 词表不可用：WARN + 降级 legacy，**绝不返回空表**
    """
    src = (source_override or source()).lower()
    if src == SOURCE_MIXED:
        return _load_mixed(path)
    if src != SOURCE_CLEANED:
        return load_legacy(path)
    layers = load_layers()
    if layers is None:
        _log.warning("KG_CANON_SOURCE=cleaned 但词表不可用 → 本批改用 legacy 口径")
        return load_legacy(path)
    nodes = nodes_from_layers(layers)
    edges = _filtered_edges(path, {n["name"] for n in nodes})
    return nodes, edges


def _load_mixed(path: str | pathlib.Path | None) -> tuple[list[dict], list[dict]]:
    """混合源：人物域 cleaned，势力/地点域 legacy（后缀门过滤）。"""
    layers = load_layers()
    if layers is None:
        _log.warning("KG_CANON_SOURCE=mixed 但 cleaned 词表不可用 → 本批改用 legacy 口径")
        return load_legacy(path)
    try:
        legacy_nodes, legacy_edges = load_legacy(path)
    except Exception as exc:  # noqa: BLE001 —— 人物域仍可用，降级为 cleaned 并留痕
        _log.warning("mixed 模式下原著层读取失败（%s）：%s；本批降级 cleaned（仅人物域）",
                     path, exc)
        return nodes_from_layers(layers), []
    if not legacy_nodes:
        _log.warning("mixed 模式下原著层为空（%s）→ 本批降级 cleaned（仅人物域）", path)
        return nodes_from_layers(layers), []
    nodes, _stats = mixed_nodes(layers, legacy_nodes)
    names = {n["name"] for n in nodes}
    return nodes, _edges_from(legacy_edges, names)


def _nonchar_domain(node: dict) -> str | None:
    """legacy 非人物节点 → ``faction`` / ``location``；不达标返回 None。

    只认 ``kind='faction'``（原著层没有独立 location kind，地点混在里面），
    且必须过结构化后缀门。无后缀的高频核心势力（mentions>=50）作为
    补充判据放行；本批只命中「打更人」1 条。
    """
    name = str(node.get("name") or "").strip()
    if node.get("kind") != "faction":
        return None
    if not 2 <= len(name) <= 8 or _BAD_NAME.search(name):
        return None
    if name in NON_ENTITY_NAMES or name.endswith("官场"):
        return None
    if name.endswith(FACTION_SUFFIXES) or name.endswith(LOCATION_SUFFIXES):
        return "location" if name.endswith(LOCATION_SUFFIXES) else "faction"
    if (node.get("mentions") or 0) >= 50:
        return "faction"
    return None


def mixed_nodes(layers: dict, legacy_nodes: list[dict]
                ) -> tuple[list[dict], dict]:
    """两侧各自过滤后并集；返回 (nodes, 分域统计)。"""
    char_nodes = nodes_from_layers(layers)
    seen = {n["name"] for n in char_nodes}
    stats = {"character": len(char_nodes), "faction": 0, "location": 0,
             "filtered_out": 0}
    kept: list[dict] = []
    for node in legacy_nodes:
        domain = _nonchar_domain(node)
        if domain is None or node["name"] in seen:
            stats["filtered_out"] += 1
            continue
        seen.add(node["name"])
        stats[domain] += 1
        kept.append({**node, "domain": domain, "source": "legacy"})
    return char_nodes + kept, stats


def nodes_from_layers(layers: dict) -> list[dict]:
    """实体层 → 与原著层同构的 node 列表（aliases 来自称呼层）。"""
    forms: dict[str, list[str]] = defaultdict(list)
    for row in layers.get("aliases") or []:
        if row.get("in_layer") is False:
            continue
        entity = row.get("entity")
        form = row.get("form")
        if entity and form:
            forms[entity].append(form)
    nodes = []
    for ent in layers.get("entities") or []:
        name = ent["name"]
        aliases = [f for f in dict.fromkeys(forms.get(name, [])) if f != name]
        node = {
            "name": name,
            "kind": ent.get("kind") or "character",
            "aliases": aliases,
            "attrs": ent.get("attrs") or {},
            "mentions": ent.get("mentions") or 0,
            "last_chapter": ent.get("last_chapter"),
            "domain": "character",
            "source": "cleaned",
        }
        # 原著层本来就没有这个人物：attrs 缺失是结构性的，不是抽取漏了
        if ent.get("no_attrs"):
            node["no_attrs"] = True
            node["no_attrs_reason"] = ent.get("no_attrs_reason") or "not_in_canon_layer"
        nodes.append(node)
    return nodes


def _filtered_edges(path: str | pathlib.Path | None,
                    names: set[str]) -> list[dict]:
    """cleaned 口径下的边：原著层边按实体层过滤，端点先过称呼层归一。"""
    if not path:
        return []
    try:
        _, edges = load_legacy(path)
    except Exception as exc:  # noqa: BLE001 —— 实体仍可用，边降级为空并留痕
        _log.warning("cleaned 模式下原著层边读取失败（%s）：%s；本批只给实体不给边",
                     path, exc)
        return []
    return _edges_from(edges, names)


def _edges_from(edges: list[dict], names: set[str]) -> list[dict]:
    """两端都要落在生效词表里；端点先过称呼层归一，重复取最大强度。"""
    out: dict[tuple[str, str, str], dict] = {}
    for edge in edges:
        a = normalize(edge["from"])
        b = normalize(edge["to"])
        if a not in names or b not in names:
            continue
        key = (a, edge["rel"], b)
        if key not in out or edge["strength"] > out[key]["strength"]:
            out[key] = {**edge, "from": a, "to": b}
    return list(out.values())


# ---------------------------------------------------------------------------
# 称呼层归一
# ---------------------------------------------------------------------------
def _constraint_ok(row: dict, context: str, offset: int) -> bool:
    """带上下文约束的写法是否成立（白莲 vs 白莲花 / 平远伯 vs 平远伯府）。"""
    constraint = row.get("constraint")
    if not constraint:
        return True
    if not context or offset < 0:
        # 没有上下文就无法证明成立：保守地不归一（原文词照旧）
        return False
    form = str(row.get("form") or "")
    after = context[offset + len(form): offset + len(form) + 4]
    for bad in constraint.get("block_follow") or []:
        if after.startswith(bad):
            return False
    need = constraint.get("require_any") or []
    if need:
        window = int(constraint.get("window") or 6)
        around = context[max(0, offset - window): offset + len(form) + window]
        if not any(word in around for word in need):
            return False
    return True


def normalize(form: str, context: str = "", offset: int = -1) -> str:
    """称呼写法 → 实体规范名；未命中或约束不成立时原样返回。"""
    if not form:
        return form
    row = _index().get(form)
    if row is None:
        return form
    if not _constraint_ok(row, context, offset):
        return form
    return row.get("entity") or form


def entity_forms(name: str) -> list[str]:
    """实体的全部写法（规范名 + 称呼层别名）；legacy 模式只有规范名。"""
    forms = [name]
    if not name:
        return forms
    for form, row in _index().items():
        if row.get("entity") == name and form not in forms:
            forms.append(form)
    return forms


def count_forms(text: str, forms: list[str]) -> int:
    """最长优先计数，且对带约束的写法逐个校验上下文。

    与 ``generated_extract._count_forms`` 的差别只有一个：带 constraint 的
    写法（如「白莲」）在「白莲花」里不会被计数。legacy 模式下索引为空，
    行为与原来完全一致。
    """
    cleaned = sorted({str(f) for f in forms if f}, key=len, reverse=True)
    if not cleaned or not text:
        return 0
    pattern = re.compile("|".join(re.escape(f) for f in cleaned))
    index = _index()
    total = 0
    for match in pattern.finditer(text):
        row = index.get(match.group(0))
        if row is not None and not _constraint_ok(row, text, match.start()):
            continue
        total += 1
    return total


def text_mention(text: str, name: str) -> str | None:
    """该实体在本段文本里命中的第一个写法；没命中返回 None。"""
    for form in entity_forms(name):
        if count_forms(text, [form]) > 0:
            return form
    return None


#: 抽取结果里所有「指向实体名」的字段
_REF_KEYS = ("from", "to", "subject", "object", "location", "character", "knows")


def normalize_extraction(result: dict) -> dict:
    """把 v3 抽取结果的称呼写法折到实体规范名（原地修改并返回）。

    原文词保留：实体原有写法进 aliases，mention 的 context_window 仍是原文。
    关系两端一并归一，避免「许郎 —兄弟→ 许七安」这种同一人被拆成两个节点。
    """
    if source() not in (SOURCE_CLEANED, SOURCE_MIXED) or not _index():
        return result
    for ent in result.get("entities") or []:
        raw = ent.get("canonical_name")
        if not raw:
            continue
        canonical = normalize(raw)
        if canonical == raw:
            continue
        aliases = [a for a in (ent.get("aliases") or []) if a != raw]
        ent["aliases"] = [raw] + aliases
        ent["canonical_name"] = canonical
    for key in ("relations", "events", "state_changes", "entity_knowledge"):
        for row in result.get(key) or []:
            if not isinstance(row, dict):
                continue
            for field in _REF_KEYS:
                value = row.get(field)
                if isinstance(value, str) and value:
                    row[field] = normalize(value)
    return result


# ---------------------------------------------------------------------------
# 记账：这一批到底用了哪份词表
# ---------------------------------------------------------------------------
def describe(path: str | pathlib.Path | None = None) -> dict:
    """当前生效来源 + 词表 hash + 规模，用于日志与批级 note。"""
    requested = source()
    if requested in (SOURCE_CLEANED, SOURCE_MIXED):
        layers = load_layers()
        if layers is None:
            return {
                "requested": requested, "effective": SOURCE_LEGACY, "degraded": True,
                "file": layers_path().name, "sha256": "",
                "entities": 0, "aliases": 0,
            }
        rows = [r for r in layers.get("aliases") or [] if r.get("in_layer") is not False]
        info = {
            "requested": requested, "effective": requested, "degraded": False,
            "file": layers_path().name, "sha256": content_hash(),
            "entities": len(layers.get("entities") or []), "aliases": len(rows),
        }
        if requested != SOURCE_MIXED:
            return info
        if not path:
            info.update({"effective": SOURCE_CLEANED, "degraded": True})
            return info
        try:
            legacy_nodes, _ = load_legacy(path)
        except Exception:  # noqa: BLE001 —— 记账不打断主流程
            legacy_nodes = []
        if not legacy_nodes:
            info.update({"effective": SOURCE_CLEANED, "degraded": True})
            return info
        _, stats = mixed_nodes(layers, legacy_nodes)
        info.update({
            "file": f"{layers_path().name}+new_nodes",
            "sha256": f"{content_hash()}+{legacy_hash(path)}",
            "domains": stats,
        })
        return info
    return {
        "requested": requested, "effective": SOURCE_LEGACY, "degraded": False,
        "file": pathlib.Path(path).name if path else "canon_db(new_nodes/new_edges)",
        "sha256": legacy_hash(path), "entities": 0, "aliases": 0,
    }


def log_effective(logger=None, *, path: str | pathlib.Path | None = None,
                  prefix: str = "") -> dict:
    """打一行「本批生效的 canon 源 + hash」；返回同一份元数据。"""
    info = describe(path)
    line = (
        f"{prefix}canon source={info['effective']} wordlist={info['file']} "
        f"sha256={info['sha256'] or '-'} entities={info['entities']} "
        f"aliases={info['aliases']}"
    )
    domains = info.get("domains")
    if domains:
        line += (
            f" domains=character:{domains['character']},faction:{domains['faction']},"
            f"location:{domains['location']},filtered:{domains['filtered_out']}"
        )
    if info["degraded"]:
        line += f"（请求 {info['requested']}，已降级 {info['effective']}）"
    (logger or _log).info(line)
    return info
