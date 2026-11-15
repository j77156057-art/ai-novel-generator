"""逐章推进台账 · 离线回放（只读数据库，不改任何产物）。

用法（仓库根目录）：

    backend\\venv\\Scripts\\python.exe backend\\verify_progress_ledger.py
    backend\\venv\\Scripts\\python.exe backend\\verify_progress_ledger.py --from 23 --to 32
    backend\\venv\\Scripts\\python.exe backend\\verify_progress_ledger.py --project-id 6

产物写到 `reports/2026-11-15-progress-ledger/`：

- `progress-ledger.json`            完整台账（给后续接线 / 二次分析）
- `progress-ledger-chapters.csv`    逐章一行（阵容 / 衔接 / structure 引用）
- `progress-ledger-attributes.csv`  角色 × 属性一行（变化点 / 零变化区间）
- `progress-ledger-report.md`       回放报告（口径与出处 + 回放表 + 实际问题）

本脚本**不写数据库**：连接走 `mode=ro`，不调用任何生成 / 改写函数。
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.coherence import chapter_index  # noqa: E402
from app.generation.progress_ledger import build_progress_ledger  # noqa: E402


def _ro_connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _load_project(conn: sqlite3.Connection, project_id: int) -> dict:
    row = conn.execute(
        "select id, title, plot_bible, outline from projects where id=?",
        (project_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"project {project_id} 不存在")
    bible = json.loads(row[2]) if row[2] else {}
    outline = json.loads(row[3]) if row[3] else []
    return {"id": row[0], "title": row[1], "plot_bible": bible, "outline": outline}


def _load_chapters(
    conn: sqlite3.Connection, project_id: int, lo: int, hi: int
) -> tuple[list[dict], dict[int, dict], dict[int, str]]:
    """按章号取**最后落库**的版本；返回 (章节, structure_check, 章标签)。"""
    rows = conn.execute(
        """
        select id, segment_no, chapter_range, text, quality_json
        from generations
        where project_id=? and text is not null and length(trim(text))>0
        order by id
        """,
        (project_id,),
    ).fetchall()
    chapters: dict[int, dict] = {}
    structures: dict[int, dict] = {}
    labels: dict[int, str] = {}
    for _id, segment_no, label, text, quality in rows:
        ch = chapter_index(label)
        if ch is None:
            ch = int(segment_no or 0)
        if not (lo <= ch <= hi):
            continue
        # 同章多版本：后落库的覆盖前面的。
        chapters[ch] = {"chapter": ch, "text": text}
        labels[ch] = label or f"第{ch}章"
        q = json.loads(quality) if quality else {}
        if isinstance(q.get("structure_check"), dict):
            structures[ch] = q["structure_check"]
    ordered = [chapters[k] for k in sorted(chapters)]
    return ordered, structures, labels


# ─── 报告渲染 ────────────────────────────────────────────────────────────


def _fmt(v: float | None, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def _render_chapter_table(ledger: dict) -> str:
    by_ch = {r["chapter"]: r for r in ledger["cast"]["rows"]}
    link_by_ch = {r["chapter"]: r for r in ledger["linkage"]["rows"]}
    struct_by_ch = {r["chapter"]: r for r in ledger["structure"]["rows"]}
    attr_by_ch: dict[int, dict] = {}
    for row in ledger["attributes"]["rows"]:
        if row["changes"]:
            for ch in row["changes"]:
                bucket = attr_by_ch.setdefault(ch["chapter"], {"realm": 0, "skill": 0, "item": 0})
                bucket[row["kind"]] += 1

    lines = [
        "| 章 | 出场角色 | 窗口内首现 | 与前章重合 | 属性变化(境/技/物) | 前章共享实体 | 共享地点 | 时间跳跃 | 交代 | 上一章问题 | 情节点覆盖 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in ledger["cast"]["rows"]:
        ch = row["chapter"]
        link = link_by_ch.get(ch, {})
        st = struct_by_ch.get(ch, {})
        attr = attr_by_ch.get(ch, {"realm": 0, "skill": 0, "item": 0})
        jump = link.get("time_jump_days") or 0
        if link.get("time_jump_unexplained"):
            jump_cell = f"**{jump:g} 天·未交代**"
        elif link.get("time_jump"):
            jump_cell = f"{jump:g} 天·已交代"
        elif jump:
            jump_cell = f"{jump:g} 天"
        else:
            jump_cell = "—"
        if link.get("time_jump"):
            explained_cell = "是" if link.get("time_explained") else "**否**"
        else:
            explained_cell = "—"
        qs = link.get("questions") or []
        q_cell = (f"{len([q for q in qs if q['mentioned']])}/{len(qs)}"
                  if qs else "—")
        beat = st.get("beat_coverage")
        beat_cell = ("待补算" if st.get("source") == "missing"
                     else _fmt(beat))
        lines.append(
            f"| {ch} | {'、'.join(row['cast']) or '—'} | {'、'.join(row['new_debuts']) or '—'} "
            f"| {_fmt(row['overlap_prev'])} "
            f"| {attr['realm']}/{attr['skill']}/{attr['item']} "
            f"| {'、'.join(link.get('shared_entities') or []) or '—'} "
            f"| {'、'.join(link.get('shared_locations') or []) or '—'} "
            f"| {jump_cell} | {explained_cell} "
            f"| {q_cell} | {beat_cell} |"
        )
    return "\n".join(lines)


def _render_attribute_table(ledger: dict) -> str:
    kind_cn = {"realm": "境界", "skill": "技能", "item": "道具/资源"}
    lines = [
        "| 角色 | 属性 | 观察起点 | 变化点 | 最后一次变化 | 距今章数 | 最长零变化区间 | 状态 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    status_cn = {
        "changed": "有推进",
        "stale": "**长期停滞**",
        "no_evidence_of_change": "有描述但无变化",
        "no_attribute_evidence": "无该类证据",
        "not_in_window": "窗口内未出场",
    }
    for row in ledger["attributes"]["rows"]:
        if row["kind"] == "realm" and row["status"] == "not_in_window":
            continue
        changes = "；".join(
            f"第{c['chapter']}章 " + "/".join(c["values"][:3])
            for c in row["changes"]
        ) or "—"
        span = row["longest_zero_span"]
        start = (f"{row['first_seen']}（出场）"
                 if row["first_seen_basis"] == "cast_debut" and row["first_seen"]
                 else (row["first_seen"] or "—"))
        lines.append(
            f"| {row['character']} | {kind_cn.get(row['kind'], row['kind'])} "
            f"| {start} | {changes} "
            f"| {row['last_change'] or '—'} | {row['chapters_since_last_change'] if row['chapters_since_last_change'] is not None else '—'} "
            f"| {row['longest_zero_run']} 章"
            f"{f'（{span[0]}–{span[1]}）' if span else ''} "
            f"| {status_cn.get(row['status'], row['status'])} |"
        )
    return "\n".join(lines)


def _render_findings(ledger: dict) -> str:
    cast = ledger["cast"]
    attr = ledger["attributes"]
    link = ledger["linkage"]
    out: list[str] = []

    # 阵容
    out.append("### 一、角色阵容：谁一直在转")
    lr = cast["longest_overlap_run"]
    if lr:
        out.append(
            f"- 最长「小圈子」区间：**第 {lr['start']}–{lr['end']} 章**，连续 "
            f"**{lr['chapters']} 章**，成员固定为 **{'、'.join(lr['members'])}**，"
            f"平均相邻重合度 {_fmt(lr['avg_overlap'])}。"
        )
    else:
        out.append("- 没有出现「相邻章高度重合」的连续区间。")
    out.append(
        f"- 主角判定：**{cast['protagonist'] or '—'}**（{cast['protagonist_rule']}）；"
        f"角色卡共 {cast['roster_size']} 人。"
    )
    if lr and lr["chapters"] == ledger["chapter_count"]:
        declared = {c["character"]: c["declared"] for c in cast["debut_checks"]}
        true_new = [n for n in lr["new_debuts"]
                    if declared.get(n) is None or declared[n] >= lr["start"]]
        out.append(
            f"- 这 {ledger['chapter_count']} 章里这个集合**一章没散过**；"
            f"窗口内首现 {len(lr['new_debuts'])} 人，但真正的新角色只有 "
            f"**{len(true_new)} 人**（其余角色卡早已声明在第 1–4 章登场）。"
            f"窗口只有 {ledger['chapter_count']} 章，不能据此断言「几十章」，"
            "但窗口内的角色流动确实是零。")
    debuts = cast["new_debuts_by_chapter"]
    if debuts:
        out.append("- 窗口内首次出现：" + "；".join(
            f"第 {ch} 章 {'、'.join(names)}" for ch, names in debuts.items())
            + "（只代表本窗口内第一次出现，不是全书登场章）。")
    else:
        out.append("- 本窗口没有任何角色首次出现。")
    mismatch = [c for c in cast["debut_checks"]
                if c["status"] == "与角色卡不一致"]
    out.append("- 与角色卡「首次出场章节」的核对：" + "；".join(
        f"{c['character']} 卡上第 {c['declared']} 章 / 窗口内第 {c['observed_in_window']} 章"
        for c in cast["debut_checks"] if c["declared"] is not None) + "。")
    if mismatch:
        out.append("- **对不上**：" + "；".join(
            f"{c['character']}（卡 {c['declared']} / 观察 {c['observed_in_window']}）"
            for c in mismatch) + "。")
    if cast["minor_never_appeared"]:
        out.append("- 角色卡里却始终没在正文露面：**"
                   + "、".join(cast["minor_never_appeared"]) + "**。")
    nov = [(n, v) for n, v in cast["minor_coverage"].items() if v["chapters"]]
    if nov:
        nov.sort(key=lambda kv: kv[1]["coverage"])
        out.append("- 主角之外覆盖最低：" + "；".join(
            f"{n} {v['chapters']}/{ledger['chapter_count']} 章" for n, v in nov[:5]) + "。")

    # 属性
    out.append("\n### 二、属性推进：谁几十章原地不动")
    for kind, cn in (("realm", "境界"), ("skill", "技能"), ("item", "道具/资源")):
        rows = [r for r in attr["rows"] if r["kind"] == kind]
        changed = [r for r in rows if r["changes"]]
        stagnant = [r for r in rows if r["status"] in ("stale", "no_evidence_of_change")]
        no_ev = [r for r in rows if r["status"] == "no_attribute_evidence"]
        out.append(
            f"- **{cn}**：有变化点 {len(changed)} 条；有证据但值未变 {len(stagnant) - sum(1 for r in stagnant if r['status'] == 'stale')} 条；"
            f"长期停滞 {sum(1 for r in rows if r['status'] == 'stale')} 条；完全无该类证据 {len(no_ev)} 条。"
        )
        if changed:
            out.append("  - 有推进：" + "；".join(
                f"{r['character']}（第 {r['last_change']} 章）" for r in changed) + "。")
        if kind == "realm" and not any(
                r["first_seen_basis"] == "first_attribute_evidence" for r in rows):
            out.append(
                f"  - 口径说明：配置里有 {len(attr['realm_systems'])} 套境界阶梯，"
                "但正文里**一次都没出现**任何阶梯词。所以这不是「境界没变」，"
                "而是**这十章根本没写境界**。")
        if kind == "skill" and not any(r["changes"] for r in rows):
            out.append(
                "  - 口径说明：这十章没有任何「学会 / 练成 / 掌握 / 施展」类动词，"
                "所以不是「技能没升级」，而是**正文没有技能层面的动作**。")
        if kind == "item" and not any(r["changes"] for r in rows):
            out.append("  - 口径说明：这十章没有抽到任何改变归属的道具事件。")
        worst = sorted([r for r in rows if r["appearances"]],
                       key=lambda r: (-r["longest_zero_run"], r["character"]))[:4]
        if worst:
            out.append("  - 零变化区间最长：" + "；".join(
                f"{r['character']} {r['longest_zero_run']} 章"
                f"{'（' + str(r['longest_zero_span'][0]) + '–' + str(r['longest_zero_span'][1]) + '）' if r['longest_zero_span'] else ''}"
                for r in worst) + "。")
    if attr["item_states"]:
        out.append("- 关键道具台账：" + "；".join(
            f"{name}（首次第 {v['first_seen']} 章，{len(v['changes'])} 次变化）"
            for name, v in sorted(attr["item_states"].items())) + "。")

    # 衔接
    out.append("\n### 三、章间衔接：哪两章接不上")
    if not link["rows"]:
        out.append("- 窗口不足两章，无法判断。")
    else:
        if link["breaks"]:
            for b in link["breaks"]:
                cues = "、".join(b["head_time_cues"]) or "未写时间词"
                out.append(
                    f"- **第 {b['prev_chapter']} → {b['chapter']} 章**："
                    f"首段跳到「{cues}」（{b['time_jump_days']:g} 天），"
                    f"但既没有过渡词，也没有与前章末段共享角色/地点。")
        else:
            out.append("- **没有发现「时间跳跃未交代」的硬断档**："
                       "所有首段都没有 ≥2 天且无过渡的时间跳跃。")
        if link["dropped_questions"]:
            for d in link["dropped_questions"]:
                out.append(
                    f"- **第 {d['prev_chapter']} → {d['chapter']} 章**："
                    f"上一章留下的问题没有再提 —— "
                    + "；".join(f"「{q}」" for q in d["questions"]) + "。")
        if link["no_time_cue"]:
            out.append(
                f"- 观察项：{len(link['no_time_cue'])}/{len(link['rows'])} 处首段"
                "**没有任何时间词**（第二天 / 当晚 / 三天后…）。没有时间词就没有"
                "「跳了多久」可判，时间线只能靠读者自行接续。")
        if link["anchorless"]:
            pairs = "、".join(f"{a['prev_chapter']}→{a['chapter']}"
                             for a in link["anchorless"])
            out.append(
                f"- 观察项（不判错）：{len(link['anchorless'])}/{len(link['rows'])} 处"
                f"首段与前章末段**既无共享角色也无共享地点**（{pairs}）。"
                "这是镜头切换的正常写法，但没有给读者任何「接着上一幕」的锚点。")
        if not link["breaks"] and not link["dropped_questions"] and not link["anchorless"]:
            out.append("- 没有发现断档或问题丢失。")

    # structure_check 引用（不重算）
    struct_rows = ledger["structure"]["rows"]
    prem = [r for r in struct_rows if (r["premature_chapters_hit"] or 0) > 0]
    if prem:
        out.append(
            f"\n### 四、structure_check 引用：越章材料\n"
            f"- {len(prem)}/{len(struct_rows)} 章命中 `premature`（越章材料），"
            "命中章为 " + "、".join(
                f"第 {r['chapter']} 章（{r['premature_chapters_hit']} 处）"
                for r in prem) + "。这是 `structure_check` 已有的告警，台账只引用。")
    return "\n".join(out)


def _render_structure_block(ledger: dict) -> str:
    rows = ledger["structure"]["rows"]
    total_beats = {r["beat_total"] for r in rows}
    lines = [
        "| 章 | 来源 | 情节点覆盖 | 情节点数 | 越章命中 | 性别不一致 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['chapter']} | {r['source']} | {_fmt(r['beat_coverage'])} "
            f"| {r['beat_total'] if r['beat_total'] is not None else '—'} "
            f"| {r['premature_chapters_hit'] if r['premature_chapters_hit'] is not None else '—'} "
            f"| {r['gender_count'] if r['gender_count'] is not None else '—'} |"
        )
    note = ""
    if total_beats == {1}:
        note = ("\n\n> 本窗口 `outline` 每章只切出 1 个情节点，`beat.coverage` 恒为 1.00，"
                "在本窗口没有区分度；越章材料命中才是这里唯一有波动的 `structure_check` 字段。")
    elif total_beats == {0} or total_beats == {None}:
        note = "\n\n> `outline` 没有可切分的情节点，覆盖率为 0，仅供参考。"
    return "\n".join(lines) + note


def _render_summary(ledger: dict) -> str:
    cast, attr, link = ledger["cast"], ledger["attributes"], ledger["linkage"]
    lr = cast["longest_overlap_run"]
    items = [r for r in attr["rows"] if r["kind"] == "item" and r["changes"]]
    bare = [r for r in attr["rows"]
            if r["kind"] in ("realm", "skill")
            and r["status"] == "no_attribute_evidence"]
    prem = sum(1 for r in ledger["structure"]["rows"]
               if (r["premature_chapters_hit"] or 0) > 0)
    cast_line = "窗口内没有形成固定小圈子"
    if lr:
        declared = {c["character"]: c["declared"] for c in cast["debut_checks"]}
        true_new = [n for n in lr["new_debuts"]
                    if declared.get(n) is None or declared[n] >= lr["start"]]
        cast_line = (
            f"阵容固定为 **{'、'.join(lr['members'])}**（{lr['chapters']} 章内"
            f"{lr['member_count']} 人、真正的新角色 {len(true_new)} 人、"
            f"平均重合 {_fmt(lr['avg_overlap'])}）")
    lines = [
        "## 速览",
        "",
        f"- **阵容**：{cast_line}。",
        f"- **属性**：境界 / 技能完全无词面证据 {len(bare)} 条（不是「没变」，是正文没写）；"
        f"道具/资源有推进 {len(items)} 人。",
        f"- **衔接**：时间跳跃未交代 {len(link['breaks'])} 处；"
        f"首段无共享锚点 {len(link['anchorless'])}/{len(link['rows'])} 处"
        f"（观察项，不判错）；上一章问题未再提 {len(link['dropped_questions'])} 处。",
        f"- **structure_check 引用**：{prem} 章命中越章材料；"
        f"情节点覆盖见下表（本窗口每章仅 1 个情节点，覆盖率无区分度）。",
        "",
    ]
    return "\n".join(lines)


def _render_report(ledger: dict, meta: dict) -> str:
    cast = ledger["cast"]
    attr = ledger["attributes"]
    link = ledger["linkage"]
    struct = ledger["structure"]
    sources = struct["sources"]
    stored_n = sum(1 for v in sources.values() if v == "stored")
    computed_n = sum(1 for v in sources.values() if v == "computed")
    missing_n = sum(1 for v in sources.values() if v == "missing")

    head = f"""# 逐章推进台账 · 第 {ledger['window'][0]}–{ledger['window'][1]} 章离线回放

- 项目：#{meta['project_id']}《{meta['title']}》
- 台账版本：`{ledger['version']}`；共 {ledger['chapter_count']} 章
- 性质：**只判不拦**。本报告不修改 `passed`、不写入 `violations`、不改正文、不重生成。
- 数据来源：`generations` 表（只读）+ `projects.plot_bible` / `projects.outline`。
- `structure_check` 引用：落库 {stored_n} 章，补算 {computed_n} 章，缺 {missing_n} 章。

""" + _render_summary(ledger) + f"""
## 一、三类指标的定义与出处

### 1. 角色阵容（cast）

| 指标 | 定义 | 出处 |
|---|---|---|
| 出场集合 | 本章正文中出现的角色卡姓名/别名集合；只认名单，不猜路人 | `plot_bible.characters` → `coherence.build_roster`（姓名+别名归一） |
| 新登场章 | 该角色**在本窗口内**第一次出现的章号；同时与角色卡 `首次出场章节` 核对 | `plot_bible.characters` + `coherence.build_roster`；窗口外的登场不在此判断 |
| 相邻章重合度 | `|A∩B| / |A∪B|`（Jaccard） | 本模块 `_build_cast` |
| 小圈子区间 | 相邻重合度 ≥ `{cast['thresholds']['overlap_high']}` 且出场人数 ≤ `{cast['thresholds']['core_max']}` 的连续区间 | `progress_ledger_cast_overlap_high` / `_core_max` |
| 阵容停滞 | 小圈子区间长度 ≥ `{cast['thresholds']['stagnant_chapters']}` 章 | `progress_ledger_cast_stagnant_chapters` |
| 主角 | 角色卡 `role` 含「主角」优先，否则全窗口提及最多 | `_protagonist`；不写死名字 |
| 配角覆盖 | 主角之外每个角色的出场章数 / 窗口章数 | 本模块 `minor_coverage` |

### 2. 属性推进（attributes）

| 属性 | 变化点定义 | 出处 |
|---|---|---|
| 境界 | 角色在同一境界体系里的章末值与上一已知值不同；跨体系不互比 | 阶梯来自 `plot_bible.realm_order`；每档 rank 由表内顺序决定 |
| 技能 | 「习得类动词 + 技能名」（学会/练成/掌握/习得/悟出/领悟/精通/入门）；`施展/使出` 只记使用，不算变化 | 本模块 `_SKILL_ACQUIRE_RE`；动词是通用汉语，不是作品词 |
| 道具/资源 | 「获得 / 失去 / 转手」改变归属；`掏出/摸出` 不算；关键道具首次出现另记 | 本模块 `_ITEM_*_RE` + `plot_bible.derived_item_names` |
| 属性归属 | 同句唯一角色 → 归他；多角色取最近，同距不猜；同句无名字时，整段唯一角色才归 | `_nearest_character`（与「同场景唯一实体」一致，宁可漏不猜） |
| 距今天数 | 窗口末章 − 最后一次变化章 | `_build_attributes` |
| 零变化区间 | 从首次证据章起，连续没有变化点的章数（含首尾） | `_zero_run` |
| 停滞 | 距今天数 ≥ `{attr['thresholds']['stale_chapters']}` 章 | `progress_ledger_attr_stale_chapters` |

### 3. 章间衔接（linkage）

| 指标 | 定义 | 出处 |
|---|---|---|
| 首段 / 末段 | 本章前 {ledger['config']['head_paragraphs']} 段 vs 上一章后 {ledger['config']['tail_paragraphs']} 段 | `coherence.split_paragraphs` |
| 共享实体 | 两段的角色卡姓名交集 | `build_roster` |
| 共享地点 | 两段的「通用地点后缀词」交集（院/站/区/楼/巷…） | `_LOC_RE`，通用构词，不是场景词表 |
| 时间线索 | 第二天/三天后/一周后/傍晚… 解析为天数 | `_time_cues` + `coherence.parse_relative` |
| 时间跳跃 | 首段时间线索向前 ≥ `{link['thresholds']['time_jump_min_days']}` 天 | `progress_ledger_time_jump_min_days` |
| 已交代 | 首段有过渡词（过去/这期间/一直/直到…）**或**与前章末段共享角色/地点 | `_TIME_BRIDGE`；规则，不是阈值 |
| 上一章的问题 | 上一章末 {ledger['config']['question_tail_paragraphs']} 段中的疑问句，最多 {ledger['config']['question_top']} 条 | `_open_questions` |
| 再次提及 | 该问句的二元组在本章正文覆盖 ≥ `{link['thresholds']['question_overlap']}` 且命中 ≥2 个 | 与 `structure_check` 的情节点覆盖同口径（0.34） |
| 无共享锚点 | 首段与前章末段既无共享角色也无共享地点；**只作观察，不判错** | 本模块；镜头切换的正常写法 |

### 4. 不重复造的三项

情节点覆盖、越章材料、性别一致性**全部引用 `structure_check`**（`beat.coverage` /
`beat.missing`、`premature.chapters_hit`、`gender.count`），本模块只搬运字段，
不重算；落库没有的章才用同一函数、同一默认值补算，并在 `structure.source` 标注。

> 阈值说明：上表所有数值都标了「待验证」。它们没有人工标注样本校准，
> 是观察口径的起点，不是从数据推导出来的判罚线；本模块也不判罚。
"""
    body = "\n\n## 二、回放表\n\n" + _render_chapter_table(ledger)
    body += "\n\n### 回放表之外的 structure_check 引用\n\n" + _render_structure_block(ledger)
    body += "\n\n## 三、角色 × 属性台账\n\n" + _render_attribute_table(ledger)
    body += "\n\n## 四、这十章看出来的实际问题\n\n" + _render_findings(ledger)
    body += "\n\n## 五、待验证与已知局限\n\n"
    body += "- 阈值未经人工标注校准，只用于观察，不参与 `passed` 判定。\n"
    body += "- 属性/地点/时间都是**词面证据**：抽不到不等于设定上没变，只说明正文没写出来。\n"
    body += "- 技能抽取只认「习得类动词 + 宾语」，`大成/小成/突破到某层` 这类写法覆盖不全。\n"
    body += "- 道具去重按 `(物件, 变化类型)` 只记第一次，重复「失去→再得到」会少算。\n"
    body += "- 上一章问题按疑问句抽，反问/心声/无问号悬念会漏；覆盖率 0.34 与情节点同口径，未单独校准。\n"
    body += f"- 待验证项：{('、'.join(ledger['pending_verification']))}。\n"
    return head + body


def main() -> int:
    ap = argparse.ArgumentParser(description="逐章推进台账离线回放")
    ap.add_argument("--db", default=str(ROOT.parent / "backend" / "novel_dev.db"))
    ap.add_argument("--project-id", type=int, default=6)
    ap.add_argument("--from", dest="lo", type=int, default=23)
    ap.add_argument("--to", dest="hi", type=int, default=32)
    ap.add_argument(
        "--out-dir",
        default=str(ROOT.parent / "reports" / "2026-11-15-progress-ledger"),
    )
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"数据库不存在：{db}")

    conn = _ro_connect(db)
    try:
        project = _load_project(conn, args.project_id)
        chapters, structures, labels = _load_chapters(
            conn, args.project_id, args.lo, args.hi)
    finally:
        conn.close()

    if not chapters:
        raise SystemExit(f"第 {args.lo}–{args.hi} 章没有可用正文")

    ledger = build_progress_ledger(
        chapters,
        plot_bible=project["plot_bible"],
        outline=project["outline"],
        structure_checks=structures,
    )
    ledger["chapter_labels"] = {str(k): v for k, v in labels.items()}

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "progress-ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")

    # 逐章 CSV
    link_by_ch = {r["chapter"]: r for r in ledger["linkage"]["rows"]}
    struct_by_ch = {r["chapter"]: r for r in ledger["structure"]["rows"]}
    with (out_dir / "progress-ledger-chapters.csv").open(
            "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["章", "标签", "出场角色", "窗口内首现", "与前章重合", "出场人数",
                    "主角在场", "配角在场", "共享实体", "共享地点", "有无共享锚点",
                    "时间跳跃天数", "时间已交代", "上一章问题数", "问题再提数",
                    "情节点覆盖", "越章命中章数", "性别不一致", "structure来源"])
        for row in ledger["cast"]["rows"]:
            ch = row["chapter"]
            link = link_by_ch.get(ch, {})
            st = struct_by_ch.get(ch, {})
            qs = link.get("questions") or []
            w.writerow([
                ch, labels.get(ch, ""), "/".join(row["cast"]),
                "/".join(row["new_debuts"]), row["overlap_prev"], row["cast_size"],
                "是" if row["protagonist_present"] else "否",
                "/".join(row["minor_present"]),
                "/".join(link.get("shared_entities") or []),
                "/".join(link.get("shared_locations") or []),
                "有" if link.get("shared_anchor") else "无",
                link.get("time_jump_days"),
                ("" if not link.get("time_jump")
                 else ("是" if link.get("time_explained") else "否")),
                len(qs), sum(1 for q in qs if q["mentioned"]),
                st.get("beat_coverage"), st.get("premature_chapters_hit"),
                st.get("gender_count"), st.get("source"),
            ])

    # 角色 × 属性 CSV
    with (out_dir / "progress-ledger-attributes.csv").open(
            "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["角色", "属性", "首次证据章", "首次依据", "出场章数",
                    "变化次数", "变化点", "最后一次变化", "距今章数",
                    "最长零变化章数", "区间起", "区间止", "状态"])
        for row in ledger["attributes"]["rows"]:
            span = row["longest_zero_span"] or [None, None]
            w.writerow([
                row["character"], row["kind"], row["first_seen"],
                row["first_seen_basis"], row["appearances"], row["change_count"],
                "；".join(f"第{c['chapter']}章:" + "/".join(c["values"])
                          for c in row["changes"]),
                row["last_change"], row["chapters_since_last_change"],
                row["longest_zero_run"], span[0], span[1], row["status"],
            ])

    meta = {"project_id": project["id"], "title": project["title"]}
    (out_dir / "progress-ledger-report.md").write_text(
        _render_report(ledger, meta), encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "window": ledger["window"],
        "chapters": ledger["chapter_count"],
        "flags": [f["code"] for f in ledger["flags"]],
        "out_dir": str(out_dir),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
