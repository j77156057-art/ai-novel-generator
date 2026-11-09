"""时代越界检查器（新增只读检测，不接入既有抽取链路）。

判定顺序（命中即停）：
  gate1 memory → gate2 simile → gate3 hypothetical → gate4 figurative
  → treat_as_unknown → 可操作性兜底
  → 非原生技术再按 origin / introduced_by 分「硬禁 / 需授权 / 豁免」

世界观配置全部来自 data/world_profile.json，不在代码里写死。
"""
from __future__ import annotations

import bisect
import json
import re
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parent / "data" / "world_profile.json"

_SENT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
_CLAUSE_SEP = re.compile(r"[，,、：:]")


class EraGuard:
    def __init__(
        self,
        profile_path: str | Path | None = None,
        character_names: list[str] | None = None,
        profile: dict[str, Any] | None = None,
    ) -> None:
        if profile is not None:
            # 项目级基线覆盖（由 era_check 从项目设定解析后传入）。
            # 传 dict 而不是临时文件：避免并发写盘、也避免把配置落到仓库里。
            self.profile = profile
        else:
            self.profile = json.loads(
                Path(profile_path or DATA_FILE).read_text(encoding="utf-8")
            )
        self.rules = self.profile["gate_rules"]
        # baseline 通过 allowed_tech_levels 决定哪些档位算「本世界合规」，不在代码里写死古代。
        self.allowed_levels = set(self.profile.get("allowed_tech_levels", ["原生"]))
        default_native_level = self.profile.get("native_family_tech_level", "原生")

        # term -> {level, family, allowed, origin}
        self.lexicon: dict[str, dict[str, Any]] = {}
        for fam in self.profile["native_tech_families"]:
            level = fam.get("tech_level", default_native_level)
            for term in fam["terms"]:
                self.lexicon[term] = {
                    "level": level,
                    "family": fam["family"],
                    "allowed": level in self.allowed_levels,
                    "origin": fam.get("origin", "crafted"),
                }
        for level, table in self.profile["tech_lexicon"].items():
            for term, family in table.items():
                if term in self.lexicon:
                    raise ValueError(f"词表重复：{term}")
                self.lexicon[term] = {
                    "level": level,
                    "family": family,
                    "allowed": level in self.allowed_levels,
                    "origin": None,
                }
        self.unknown_terms = set(self.profile.get("treat_as_unknown", []))
        for term in self.unknown_terms:
            if term not in self.lexicon:
                self.lexicon[term] = {
                    "level": "未知",
                    "family": "未知",
                    "allowed": False,
                    "origin": None,
                }
        self.period_mismatch = self.profile.get("period_mismatch", {"enabled": False})
        self.blocklist = {
            k: v for k, v in self.profile.get("context_blocklist", {}).items() if not k.startswith("_")
        }
        self.figurative = self.profile.get("figurative_patterns", {})
        self.channels = [c for c in self.profile["exception_channels"]]
        self.channel_markers = self.profile.get("exception_markers", {})
        self.carriers = {
            k: set(v)
            for k, v in self.profile.get("exception_carriers", {}).items()
            if not k.startswith("_") and isinstance(v, list)
        }

        self._by_first: dict[str, list[str]] = {}
        for term in sorted(self.lexicon, key=len, reverse=True):
            self._by_first.setdefault(term[0], []).append(term)

        self.characters = sorted({n for n in (character_names or []) if len(n) >= 2}, key=len, reverse=True)

    # ------------------------------------------------------------------ #
    # 文本定位
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sentences(text: str) -> list[tuple[int, int, str]]:
        out = []
        for m in _SENT_RE.finditer(text):
            if m.group().strip():
                out.append((m.start(), m.end(), m.group()))
        return out

    @staticmethod
    def _clause_bounds(sent: str, rel: int) -> tuple[int, int]:
        start = 0
        for m in _CLAUSE_SEP.finditer(sent):
            if m.end() <= rel:
                start = m.end()
            elif m.start() >= rel:
                return start, m.start()
        return start, len(sent)

    @staticmethod
    def _first_in(text: str, markers: list[str]) -> tuple[str, int] | None:
        best = None
        for marker in markers:
            idx = text.rfind(marker)
            if idx >= 0 and (best is None or idx > best[1]):
                best = (marker, idx)
        return best

    def _iter_mentions(self, text: str):
        i, n = 0, len(text)
        while i < n:
            for term in self._by_first.get(text[i], ()):
                if not text.startswith(term, i):
                    continue
                end = i + len(term)
                if self._blocked(text, i, end, term):
                    i = end
                    break
                yield i, end, term
                i = end
                break
            else:
                i += 1

    def _blocked(self, text: str, start: int, end: int, term: str) -> bool:
        for phrase in self.blocklist.get(term, []):
            lo = max(0, start - len(phrase))
            p = text.rfind(phrase, lo, end + len(phrase))
            if p >= 0 and p <= start < p + len(phrase):
                return True
        return False

    # ------------------------------------------------------------------ #
    # 人物 / 通道
    # ------------------------------------------------------------------ #
    def _nearest_character(self, text: str) -> str | None:
        best = None
        for name in self.characters:
            idx = text.rfind(name)
            if idx >= 0 and (best is None or idx > best[1]):
                best = (name, idx)
        return best[0] if best else None

    def _owner(self, clause: str, rel: int, term: str) -> str | None:
        prefix = clause[:rel]
        best = None
        for name in self.characters:
            idx = prefix.rfind(name)
            if idx < 0:
                continue
            tail = prefix[idx + len(name): idx + len(name) + 1]
            if tail == "的" and rel - (idx + len(name)) <= 8:
                if best is None or idx > best[1]:
                    best = (name, idx)
        return best[0] if best else None

    def _channel_in_sentence(self, sent: str) -> str | None:
        for channel in self.channels:
            for marker in self.channel_markers.get(channel, []):
                if marker in sent:
                    return channel
        return None

    @staticmethod
    def _origin_of(channel: str | None) -> str | None:
        return {"穿越者": "imported", "系统": "system", "异界": "imported", "传承": "crafted"}.get(channel)

    # ------------------------------------------------------------------ #
    # 单条 mention 判定
    # ------------------------------------------------------------------ #
    def judge_mention(self, text: str, start: int, end: int, term: str, sents) -> dict[str, Any]:
        info = self.lexicon[term]
        sent_i = bisect.bisect_right([s[0] for s in sents], start) - 1
        st, en, sent = sents[max(0, sent_i)]
        rel = start - st
        cst, cen = self._clause_bounds(sent, rel)
        clause = sent[cst:cen]
        crel = rel - cst
        pre_clause = clause[:crel]
        pre_sent = sent[:rel]
        around = clause[max(0, crel - 24): crel + len(term) + 16]

        action = self._first_in(around, self.rules["action_verbs"])
        state = self._first_in(around, self.rules["state_cues"])
        presence = self._first_in(around, self.rules["presence_cues"])
        projectile = self._first_in(around, self.rules["projectile_cues"])
        operable = bool(action or state or presence or projectile)

        trace: dict[str, Any] = {
            "memory": None,
            "simile": None,
            "hypothetical": None,
            "figurative": None,
            "unknown": None,
            "action": action[0] if action else None,
            "state": state[0] if state else None,
            "presence": presence[0] if presence else None,
            "projectile": projectile[0] if projectile else None,
        }

        # gate1 memory：强标记全句生效；「记得/心想」这类弱标记只在同分句生效，
        # 避免「他只记得来路，账册上却没写」把后一分句的真实器物一并豁免。
        mem = self._first_in(sent[: rel + len(term) + 2], self.rules["memory_markers"])
        if mem is None:
            mem = self._first_in(pre_clause, self.rules.get("memory_weak_markers", []))
        if mem:
            trace["memory"] = mem[0]
            return self._row(text, start, end, term, info, sent, "memory", "not_judged",
                             "gate1_memory", trace)

        # gate2 simile：喻体不需要存在；动作与比喻同时出现时动作优先
        sim = None
        for marker in sorted(self.rules["simile_markers"], key=len, reverse=True):
            idx = pre_clause.rfind(marker)
            if idx >= 0 and crel - (idx + len(marker)) <= 18:
                sim = (marker, idx)
                break
        if sim is None and re.search(r"比[^，。；]{0,10}" + re.escape(term), clause):
            sim = ("比…", clause.find("比"))
        if sim:
            trace["simile"] = sim[0]
            if action:
                trace["action_priority"] = True
            else:
                return self._row(text, start, end, term, info, sent, "simile", "not_judged",
                                 "gate2_simile", trace)

        # gate3 hypothetical / negation / existence-question
        cond = self._first_in(pre_clause, self.rules["hypothetical_markers"])
        neg = self._first_in(pre_sent[-10:], self.rules["negation_markers"])
        if cond or neg:
            trace["hypothetical"] = (cond or neg)[0]
            return self._row(text, start, end, term, info, sent, "hypothetical", "not_judged",
                             "gate3_hypothetical", trace)

        # gate4 figurative / 词义借用
        patterns = [p for p in self.figurative.get(term, []) if not p.startswith("_")]
        hit = next((p for p in patterns if p.replace("{term}", term) in sent), None)
        if hit:
            trace["figurative"] = hit
            return self._row(text, start, end, term, info, sent, "figurative", "not_judged",
                             "gate4_figurative", trace)

        # 模糊项：不自动判实体；同句出现例外通道标记才升级
        if term in self.unknown_terms:
            channel = self._channel_in_sentence(sent)
            if not channel:
                trace["unknown"] = True
                return self._row(text, start, end, term, info, sent, "non_entity", "not_judged",
                                 "treat_as_unknown", trace, unknown_scope=True)
            return self._entity_row(text, start, end, term, info, sent, rel, clause, channel, trace)

        # 可操作性兜底：能被使用/传递/损毁/摆放才算实体
        if not operable:
            return self._row(text, start, end, term, info, sent, "non_entity", "not_judged",
                             "operability", trace)

        return self._entity_row(text, start, end, term, info, sent, rel, clause, None, trace)

    def _entity_row(self, text, start, end, term, info, sent, rel, clause, forced_channel, trace):
        owner = self._owner(clause, rel, term)
        introducer = self._nearest_character(sent[:rel]) or None
        channel = forced_channel or self._channel_in_sentence(sent)
        if channel is None and introducer:
            for ch, names in self.carriers.items():
                if introducer in names:
                    channel = ch
                    break

        if info["allowed"]:
            return self._row(text, start, end, term, info, sent, "entity", "native",
                             "channel" if forced_channel else "operability", trace,
                             owner=owner, origin=info["origin"] or "native", introduced_by=None)

        # 时代错位：baseline 不允许的「前现代」器物。属于本世界历史器物，
        # 有收藏/文具/装饰等正当化语境时走豁免，否则记「需授权（period_mismatch）」。
        if info["level"] == "前现代" and self.period_mismatch.get("enabled"):
            cue = self._first_in(sent, self.period_mismatch.get("legit_cues", []))
            if cue:
                trace["period_legit"] = cue[0]
                return self._row(text, start, end, term, info, sent, "entity", "exempt",
                                 "period_legit", trace, owner=owner, origin=info["origin"],
                                 introduced_by=None, exempt=True,
                                 exempt_channel=self.period_mismatch.get("legit_channel", "period_legit"),
                                 violation="needs_authorization")
            return self._row(text, start, end, term, info, sent, "entity", "violation",
                             "period_mismatch", trace, owner=owner, origin=info["origin"],
                             introduced_by=None, violation="needs_authorization",
                             violation_note="period_mismatch")

        origin = self._origin_of(channel)
        if channel and introducer:
            return self._row(text, start, end, term, info, sent, "entity", "exempt",
                             "exception_channel", trace, owner=owner, origin=origin,
                             introduced_by=introducer, exempt=True, exempt_channel=channel,
                             violation="needs_authorization")
        if channel and not introducer:
            return self._row(text, start, end, term, info, sent, "entity", "violation",
                             "exception_channel", trace, owner=owner, origin=origin,
                             introduced_by=None, violation="hard_ban",
                             violation_note="missing_introducer")
        return self._row(text, start, end, term, info, sent, "entity", "violation",
                         "operability", trace, owner=owner, origin=None,
                         introduced_by=introducer, violation="hard_ban",
                         violation_note="unregistered_origin" if introducer else "missing_introducer")

    def _row(self, text, start, end, term, info, sent, reason, decision, gate, trace, *,
             owner=None, origin=None, introduced_by=None, exempt=False, exempt_channel=None,
             violation="none", violation_note=None, unknown_scope=False):
        line = text.count("\n", 0, start) + 1
        item = {
            "name": term,
            "type": info["family"],
            "owner": owner,
            "tech_level": info["level"],
            "origin": origin,
            "introduced_by": (
                {"character": introduced_by, "chapter": None} if introduced_by else None
            ),
        }
        return {
            "line": line,
            "char_start": start,
            "char_end": end,
            "term": term,
            "tech_level": info["level"],
            "tech_family": info["family"],
            "sentence": sent.strip(),
            "is_entity": reason == "entity",
            "judgement_reason": reason,
            "decision": decision,
            "gate": gate,
            "baseline": self.profile["baseline"],
            "baseline_allowed": bool(info["allowed"]),
            "unknown_scope": unknown_scope,
            "exempt": exempt,
            "exempt_channel": exempt_channel,
            "violation": violation,
            "violation_note": violation_note,
            "item": item,
            "rule_trace": trace,
        }

    # ------------------------------------------------------------------ #
    # 对外扫描
    # ------------------------------------------------------------------ #
    def scan_text(self, text: str, chapter: int, corpus: str, source: str) -> list[dict[str, Any]]:
        sents = self._sentences(text)
        rows = []
        for seq, (start, end, term) in enumerate(self._iter_mentions(text), 1):
            row = self.judge_mention(text, start, end, term, sents)
            row.update({
                "mention_id": f"{corpus}-ch{chapter:03d}-{seq:04d}",
                "corpus": corpus,
                "chapter": chapter,
                "source": source,
            })
            if row["item"]["introduced_by"]:
                row["item"]["introduced_by"]["chapter"] = chapter
            row["introduced_by"] = row["item"]["introduced_by"]
            rows.append(row)
        return rows
