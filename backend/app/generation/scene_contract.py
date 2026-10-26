"""Scene-level generation contract, audits, local repair and L2 trimming."""
from __future__ import annotations
import re
from typing import Any

def build_scene_contract(*, scene_type: str | None, current_goal: str | None, previous_scene: str | None, allowed_characters: list[str] | None, outline: str | None = None) -> str:
    scene_type = scene_type or "未判定场景"; current_goal = current_goal or outline or "推进当前冲突并留下未解决状态"; previous_scene = previous_scene or "无"
    chars = "、".join(dict.fromkeys(str(x).strip() for x in (allowed_characters or []) if str(x).strip())) or "当前已锁定角色"
    return ("【本节场景契约】\n" f"当前场景：{scene_type}\n" f"本节目标：{current_goal}\n" f"上一场景：{previous_scene}\n" f"可用角色：{chars}\n" "本节只推进一个主要状态变化；新人物、新地点、新谜面都要有原文或大纲依据。\n" "对白服务于行动和关系变化，不要连续用问答替代叙述；结尾保留未解决状态。\n")

_REVEAL_RE = re.compile(r"(?:忽然|突然|意外|不知何时|原来|竟然|却见|多出|出现|传来|不见|来历|身份|秘密|是谁|什么人|没有人|没人)")


def audit_scene_text(text: str, budgets: dict[str, float] | None = None) -> dict[str, Any]:
    """Audit one scene using explicitly supplied reference-calibrated budgets."""
    budgets = budgets or {}
    text = text or ""
    chars = max(len(text), 1)
    quoted = re.findall(r"[“\"]([^”\"]*)[”\"]", text)
    dialogue_chars = sum(len(x) for x in quoted)
    question_count = text.count("？") + text.count("?")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    # Count reveal transitions, not named props. Deduplicate multiple cue words
    # within one paragraph so one reveal is one event regardless of wording.
    mystery_hits = sum(1 for p in paragraphs if _REVEAL_RE.search(p))
    dialogue_density = dialogue_chars / chars
    question_per_1k = question_count * 1000 / chars
    metrics = {"dialogue_density": round(dialogue_density, 4),
               "question_per_1k": round(question_per_1k, 3),
               "mystery_stack": mystery_hits}
    violations: list[str] = []
    if "dialogue_density" in budgets and dialogue_density > float(budgets["dialogue_density"]):
        violations.append("dialogue_density")
    if "question_per_1k" in budgets and question_per_1k > float(budgets["question_per_1k"]):
        violations.append("question_per_1k")
    if "mystery_stack" in budgets and mystery_hits > int(budgets["mystery_stack"]):
        violations.append("mystery_stack")
    spans = []
    if any(key in violations for key in ("dialogue_density", "question_per_1k")) and paragraphs:
        scores = [len(re.findall(r"[“\"]", p)) + (2 if _REVEAL_RE.search(p) else 0) for p in paragraphs]
        end = max(range(len(scores)), key=lambda i: scores[i])
        spans.append({"start": max(0, end - 2), "end": min(len(paragraphs), end + 3),
                      "reason": ",".join(violations)})
    spans = [{**span, "start": max(1, span["start"]), "end": min(len(paragraphs) - 1, span["end"])} for span in spans]
    spans = [span for span in spans if span["start"] < span["end"]]
    return {"metrics": metrics, "violations": violations, "repair_spans": spans, "calibrated": bool(budgets), "budgets": budgets}


def derive_scene_budgets(reference_texts: list[str], target_chars: int = 3000) -> dict[str, float]:
    """Use retrieved same-scene samples to derive permissive p90 budgets.

    With fewer than two usable samples, return no gates: an uncalibrated audit
    must not assert that a genre or scene is wrong.
    """
    samples = []
    for text in reference_texts:
        if len(text or "") < 250:
            continue
        metric = audit_scene_text(text)["metrics"]
        metric["mystery_stack"] *= target_chars / len(text)
        samples.append(metric)
    if len(samples) < 2:
        return {}
    def p90(key: str) -> float:
        values = sorted(float(x[key]) for x in samples)
        return values[min(len(values) - 1, int(.9 * (len(values) - 1)))]
    return {
        "dialogue_density": min(.9, max(.2, p90("dialogue_density") * 1.35 + .08)),
        "question_per_1k": max(4.0, p90("question_per_1k") * 1.5 + 2),
        "mystery_stack": max(3, int(p90("mystery_stack") * 1.5 + 2)),
    }

def build_local_repair_prompt(text: str, audit: dict, *, immutable_facts: list[str] | None = None, preserve_ending: str = "") -> tuple[str,str]:
    system="你是中文网文局部修复编辑。只返回替换段落，不要解释。\n只修复指定问题，不重写全文；保留人物、事实、事件顺序和原文结尾。\n不得增加新人物、新地点、新谜面，不得把叙述改成总结。\n只返回替换段落"
    user=(f"问题：{audit.get('violations') or []}\n"
          f"当前指标：{audit.get('metrics') or {}}\n"
          "修复动作：对白过密时把连续问答合并为叙述、动作或内心判断；问句过密时保留必要的一问，其余改为陈述；悬疑提示词仅供诊断，不得为降低指标删除事实。\n"
          f"不可改变事实：{'；'.join(immutable_facts or []) or '无'}\n"
          f"外部结尾仅供边界参考，不得复制到替换内容：{preserve_ending or '保留原结尾'}\n"
          f"原文：\n{text}\n")
    return system,user

def apply_repaired_spans(text: str, repairs: list[dict], spans: list[dict]) -> str:
    parts=re.split(r"(\n\s*\n)",text); nonempty=[i for i,p in enumerate(parts) if p.strip()]
    for repair,span in sorted(zip(repairs,spans),key=lambda x:x[1]["start"],reverse=True):
        if repair.get("start", span["start"]) != span["start"] or repair.get("end", span["end"]) != span["end"]:
            return text
        ids=nonempty[span["start"]:span["end"]]
        if ids and repair.get("text"): parts[ids[0]:ids[-1]+1]=[repair["text"]]; nonempty=[i for i,p in enumerate(parts) if p.strip()]
    return "".join(parts)

def select_l2_profiles(entries: list[dict], *, active_names: list[str], roleplay_name: str | None = None, max_profiles: int = 3) -> list[dict]:
    wanted={str(x).strip() for x in active_names if str(x).strip()}; wanted={roleplay_name.strip()} if roleplay_name else wanted; selected=[]
    for entry in entries:
        if not isinstance(entry,dict) or entry.get("name") not in wanted: continue
        item = dict(entry)
        relations = item.get("关系网") or {}
        if roleplay_name:
            item["关系网"] = dict(list(relations.items())[:3])
        else:
            item["关系网"] = {k: v for k, v in relations.items() if k in wanted or k in set(active_names)}
        selected.append(item)
        if len(selected)>=max_profiles: break
    return selected
