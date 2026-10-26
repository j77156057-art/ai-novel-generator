"""助理 agent 的**工序链**：交稿 → 清洗 → 抽检 → 蒸馏 → 审查 → 修复 → 收敛。

设计见 `docs/agent-pipeline-design.md`。三条纪律：

1. **读工序直接复用既有只读能力**（清洗复查、三类审核、验收状态、待批提案），
   工序链**不实现第二套业务逻辑** —— 它只回答「现在到哪一步、该做什么」。
2. **写工序不在这里执行**：工序只判定「该做什么」与「做完没有」；
   写动作一律走 proposal 审批闸门（`app.agents.proposals`）。
3. **收敛判据是确定性的**（查库 + 跑只读规则），**不让模型自己说「差不多了」**。

## 每一步的状态是「重新评估」出来的

`steps` 列里存的是**推进日志**（谁在什么时候做了哪一步、结论是什么），
但「这一步现在算不算做完」是每次调用 `evaluate()` 时**重新查库**判出来的。
理由：正文改过、蒸馏重跑过、提案批了 —— 日志不会自己更新，
只有重新评估才不会出现「日志说做完了、其实没做完」。

状态取值：

    done     判据满足
    pending  还没做（下一步该做它）
    blocked  有必须先处理的问题（error 级 issue / 待批提案 / 验收未通过）
    skipped  这一步对这个项目不适用（例如项目没有正文）

## 状态词（`state_label`）由服务端给

界面只**显示**状态，不自己猜：`state_label` 是这一步的人话说法
（「已完成」/「下一步」/「等你批」/「没有待批项」…）。为什么不让前端映射：

- 「修复待批」这一步没有待批项时，它**不是**「已完成」——它只是**没东西可批**。
  这两个含义在中文里差得很远，映射表写死在两处就会漂。
- 「下一步」只有服务端知道（它是第一个还没做的工序），前端拿不到这个信息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

#: 工序 id → 判据函数。判据只允许**读**库与跑只读规则。
_CRITERIA_ORDER = (
    'corpus_ready',
    'clean_check',
    'distill',
    'review_narrative',
    'review_prompt',
    'write_body',
    'review_body',
    'repair',
    'derived_rebuild',
    'unlock',
    'converged',
)


@dataclass(frozen=True)
class Step:
    """一道工序的定义。

    `read_tools` 是这一步 agent 该调的**只读**工具（工具箱里已注册的）；
    `write_proposal` 是这一步若要写库，产出哪一类**提案**（写操作走审批闸门，
    不在这里执行）；`note` 是给人看的说明（前端直接显示）。
    """

    id: str
    title: str
    read_tools: tuple[str, ...] = ()
    write_proposal: str | None = None
    track: str = 'body'          # 哪条轨：'prompt'（提示词）/ 'body'（正文）
    dispatch: str = ''            # 派给谁做（人话：哪个 agent / 哪道工序）
    note: str = ''


STEPS: tuple[Step, ...] = (
    # ——— 提示词轨：从干净语料到 writer 真能吃到的系统提示词 ———
    Step('corpus_ready', '语料已就位',
         ('read_attachment', 'audit_chunks'), None,
         track='prompt', dispatch='作者投放语料',
         note='确认参考语料已经投放并切分完成。'),
    Step('clean_check', '清洗抽检',
         ('check_ingest_clean', 'audit_chunks'), 'ingest_clean',
         track='prompt', dispatch='清洗 agent（错字/漏字/水印）',
         note='清洗后的抽样复查：错字、漏字、广告水印、切分破损。'),
    Step('distill', '文风蒸馏',
         ('audit_corpus',), 'start_distill',
         track='prompt', dispatch='蒸馏 agent（抽取文风档案）',
         note='从干净语料里提炼文体档案（完整/平衡/快速三档）。'),
    Step('review_narrative', '档案审查',
         ('audit_narrative',), 'start_distill',
         track='prompt', dispatch='审查 agent（审档案本身）',
         note='审蒸馏产物本身：维度齐不齐、规则有没有证据。'),
    Step('review_prompt', '提示词体检',
         ('audit_prompt',), 'prompt_override',
         track='prompt', dispatch='提示词审核 agent（审组装后的系统提示词）',
         note='审 writer 真正吃进去的那份提示词：档案有没有真的注入、'
              '有没有把参考书的角色声音词典漏进原创模式、禁用词自不自相矛盾、'
              '提示词是不是太长。'),
    # ——— 正文轨：从 writer 写出来的正文到可以放行 ———
    Step('write_body', '写正文',
         ('get_plot_timeline',), None,
         track='body', dispatch='Writer agent（按你选的档位与章数写）',
         note='按权威与文风档案写正文；写完要过正文审查才能放行。'),
    Step('review_body', '正文审查',
         ('run_diagnostic_task',), 'close_acceptance',
         track='body', dispatch='审查 agent（封闭清单审计 + 整段验收）',
         note='正文与保存权威是否一致（封闭清单审计 + 整段验收）。'),
    Step('repair', '修复待批',
         ('get_plot_timeline',), 'apply_body_repair',
         track='body', dispatch='修复 agent（Writer 候选 + 独立复验）',
         note='要改的地方会生成提案；等人批准并应用后，这一步才算做完。'),
    Step('derived_rebuild', '派生重建',
         ('get_plot_timeline',), 'derived_rebuild',
         track='body', dispatch='派生重建（摘要 / 完成度 / 图谱 / 事实）',
         note='正文改过之后，摘要、完成度、图谱和事实要按新正文重算。'),
    Step('unlock', '解锁放行',
         (), 'unlock_generation',
         track='body', dispatch='生成守卫',
         note='派生数据重建完、整段验收也过了，才解除守卫放行继续生成。'),
    Step('converged', '收敛',
         (), None,
         track='body', dispatch='—',
         note='所有判据都满足，这一轮做完了。'),
)

STEP_BY_ID: dict[str, Step] = {s.id: s for s in STEPS}

#: 每道工序能提哪几类**动作提案**（`app.agents.action_proposals.ACTION_KINDS`）。
#:
#: 与 `Step.write_proposal` 的分工：
#: - `write_proposal` = 这一步若要**改文字**，产出哪一类文本提案；
#: - `write_actions`  = 这一步若要**做动作**（启动蒸馏 / 规划大纲 / 扩展规划 /
#:   调动生成），能提哪几类动作提案。
#:
#: 为什么要这张表：agent 要能自己把「这一步缺什么」变成**可批准的提案**，
#: 但**不能自己发明动作类型** —— 动作种类、端点、前提算法都由服务端说了算
#: （同「选项由决策点注册表给出」是同一条纪律）。
STEP_WRITE_KINDS: dict[str, tuple[str, ...]] = {
    'distill': ('start_distill',),
    'review_narrative': ('start_distill',),
    'write_body': ('plan_outline', 'extend_plan', 'invoke_generation'),
    # —— 闭环四类：验收 / 修复 / 重建 / 解锁 ——
    #
    # 「正文审查」这一步能提**两个**动作：跑整段验收、提一个定点修复。
    # 为什么修复也能在这里提：审出问题后的正当回应有两个（先改 / 先验），
    # 而「修复待批」那一步说的是「**已经**有东西等着批」—— 审出问题时
    # 那一步还是「没有待批项」，agent 若只能站在那一步才许提修复，
    # 就被卡在「明明查出了问题却提不了修复」（实测：真书项目 8 就是这样）。
    'review_body': ('close_acceptance', 'apply_body_repair'),
    'repair': ('apply_body_repair',),
    'derived_rebuild': ('derived_rebuild',),
    'unlock': ('unlock_generation',),
}

#: 状态词缺省值。判据给了更贴切的说法时用判据的（例如「没有待批项」）。
_DEFAULT_STATE_LABELS: dict[str, str] = {
    'done': '已完成',
    'pending': '待做',
    'blocked': '卡住',
    'skipped': '不适用',
}

#: 提案目标的人话说法。界面不出现 `target_kind` / `target_table` 这种内部名 ——
#: 用户要判断的是「这条要改哪儿」，不是「改的是哪张表的哪一列」。
#: 两个键都列是因为既有行的 `target_kind` 里存的还是旧表名（`generations`…）。
_TARGET_LABELS: dict[str, str] = {
    'generation_text': '正文',
    'generations': '正文',
    'project_plot_bible': '故事设定',
    'projects': '故事设定',
    'prompt_override': '系统提示词',
    'prompt_overrides': '系统提示词',
    'novel_chunk_text': '参考语料',
    'novel_chunks': '参考语料',
}

#: 改动大小的人话说法（`risk_level` 的界面翻译）。
_RISK_LABELS: dict[str, str] = {
    'low': '改动小',
    'medium': '改动中等',
    'high': '改动较大',
}


def _target_kind(row: Any) -> str:
    """这条提案改的是哪类内容（小写）。

    `target_kind` 是新字段（值是内容类别），`target_table` 是旧字段镜像
    （值是表名）。既有行可能两个值不一样，取哪个都可能不准 ——
    所以这里把两者都当成「键」去查 `_TARGET_LABELS`，谁认得用谁。
    """
    kind = str(row.target_kind or '').strip().lower()
    if kind in _TARGET_LABELS:
        return kind
    table = str(row.target_table or '').strip().lower()
    if table in _TARGET_LABELS:
        return table
    return kind or table


def _pending_proposals(db: Any, project: Any) -> list[dict[str, Any]]:
    """当前**等着人处理**的写回建议（只读清单，人话）。

    范围是 `pending`（等人批）**加上** `approved`（批了、等人写进正文）。
    只查 pending 会有个流程坑：用户点完「批准」，提案状态变成 approved，
    于是它从清单里消失 —— 用户再也点不到「应用」，这件事就卡死了。

    为什么在这里查、而不是让前端自己再调一次提案列表接口：
    工序链要回答的是「现在卡在哪、还差什么」，待处理建议正是它的一环。
    两处各查一遍会出现「面板说没有、列表说有」的漂移 —— 而工序链是
    agent 判断下一步的唯一依据，漂移会让它给出错误的派发建议。

    这里是**只读**的：列出待处理项不等于批准，批准仍然走既有的审批闸门
    （`POST /api/assistant/proposals/{id}/approve` → 再 `apply`）。
    """
    from app.models import EditProposal, Generation
    rows = list(db.scalars(
        select(EditProposal).where(
            EditProposal.project_id == project.id,
            EditProposal.status.in_(('pending', 'approved')))
        .order_by(EditProposal.id.desc())).all())
    if not rows:
        return []
    # 正文类提案要显示「第几段」：批量查一次段号，避免 N+1。
    gen_ids: list[int] = []
    for row in rows:
        raw = row.target_id
        if raw is None or not str(raw).isdigit():
            continue
        if _target_kind(row) in ('generation_text', 'generations'):
            gen_ids.append(int(raw))
    seg_of: dict[int, int] = {}
    if gen_ids:
        for gid, seg in db.execute(
                select(Generation.id, Generation.segment_no)
                .where(Generation.id.in_(gen_ids))).all():
            seg_of[int(gid)] = int(seg)
    out: list[dict[str, Any]] = []
    for row in rows:
        kind = _target_kind(row)
        label = _TARGET_LABELS.get(kind, '其它内容')
        if kind in ('generation_text', 'generations'):
            raw = row.target_id
            seg = seg_of.get(int(raw)) if raw is not None and str(raw).isdigit() else None
            if seg is not None:
                label = f'正文第 {seg} 段'
        out.append({
            'id': row.id,
            'target': label,
            'reason': (row.reason or '').strip(),
            'risk': _RISK_LABELS.get(str(row.risk_level or 'low').lower(), '改动小'),
            'risk_level': row.risk_level or 'low',
            'status': row.status,
            'created_at': row.created_at.isoformat() if row.created_at else None,
        })
    return out



# ----------------------------------------------------------------------
# 判据（确定性：查库 + 跑既有只读规则）
# ----------------------------------------------------------------------

def _has_story_seed(project: Any) -> bool:
    """项目有没有「故事种子」（premise / outline / plot_bible 至少一个非空）。

    为什么要分这个：语料侧工序对**自由生成**的项目是不适用（没语料就没有
    文风可提炼），但对**什么都还没交**的项目是「还没交稿」——
    两者都叫「没有参考语料」，语义完全相反，判成同一个状态就会把
    「一个字都没写」的项目说成「已收敛」。
    """
    return bool((project.premise or '').strip()
                or (project.outline or [])
                or (project.plot_bible or {}))


def _project_corpus_ids(project: Any) -> list[int]:
    ids = project.reference_novel_ids or []
    if isinstance(ids, (str, int)):
        ids = [ids]
    out: list[int] = []
    for value in ids:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def _corpus_ready(db: Any, project: Any, ctx: dict[str, Any] | None = None) -> tuple[str, ...]:
    from app.models import NovelChunk
    ids = _project_corpus_ids(project)
    if not ids:
        if _has_story_seed(project):
            return 'skipped', '自由生成（没选参考语料）→ 语料侧工序不适用。'
        return 'pending', ('还没交稿：把小说文本交给助理 agent，'
                          '它会带它走清洗、抽检、蒸馏、审查。')
    chunks = db.scalar(
        select(func.count(NovelChunk.id)).where(NovelChunk.novel_id.in_(ids)))
    if not chunks:
        return 'pending', '参考语料还没投放或切分（一个分片都没有）。'
    return 'done', f'参考语料已切分 {int(chunks)} 片。'


def _clean_check(db: Any, project: Any, ctx: dict[str, Any] | None = None) -> tuple[str, ...]:
    ids = _project_corpus_ids(project)
    if not ids:
        if _has_story_seed(project):
            return 'skipped', '自由生成 → 没有语料要清洗。'
        return 'pending', '等语料到位（先把文本交给助理 agent）。'
    from app.agents.audit import run_corpus_audit
    report, _stats = run_corpus_audit(db, novel_ids=ids)
    errors = [i for i in report.issues if getattr(i, 'severity', None) == 'error']
    if errors:
        return 'blocked', (f'语料有 {len(errors)} 个必须先处理的问题'
                           f'（第一件：{getattr(errors[0], "title", "")}）。')
    notes = len(report.issues)
    return 'done', f'抽检通过（{notes} 条提示不阻塞）。'


def _active_archive_for(db: Any, source_ids: list[int]) -> Any | None:
    """蒸馏库里有没有这份语料的**可复用**档案（active 态）。

    匹配键用 `source_ids_hash`（仓库里蒸馏库复用查找的**同一个**归一化口径，
    见 `app.style.narrative._source_ids_hash`）—— 自己另算一套哈希会在
    「语料顺序不同」时对不上，档案明明在却找不到。
    """
    from app.models import StyleDistill
    from app.style.narrative import _source_ids_hash
    return db.scalar(
        select(StyleDistill).where(
            StyleDistill.source_ids_hash == _source_ids_hash(source_ids),
            StyleDistill.status == 'active')
        .order_by(StyleDistill.id.desc()))


def _distill(db: Any, project: Any, ctx: dict[str, Any] | None = None) -> tuple[str, ...]:
    """文风蒸馏：这道工序的**产物**是「文风档案」，判据就看产物在不在位。

    产物有两条来路，只认其中一条就会把链条说成自相矛盾：

    1. 项目上已生效的 `style_narrative`（自己跑蒸馏得到，或从蒸馏库**应用**得到）；
    2. 蒸馏库里有一份同一份语料的 active 档案（还没应用到本项目）。

    只看 `distill_runs` 的老写法会把第 1 条里的「应用来的」误判成
    「还没跑过文风蒸馏」，而同一时刻「档案审查」却显示 done（档案确实在）——
    面板上就出现「蒸馏 待做」紧挨着「档案审查 已完成」。
    实测：《北荒刀歌》真书档案的两个项目正是这样（distill_runs=0，档案在位）。
    """
    if not _project_corpus_ids(project):
        if _has_story_seed(project):
            # 自由生成：没有参考语料就没有文风可提炼 —— 这步「不适用」，
            # 不是「下一步」（否则选择框会问「蒸馏用哪一档」，而根本无从蒸馏）。
            return 'skipped', '自由生成（没选参考语料）→ 没有语料可提炼文风，跳过蒸馏。'
        return 'pending', '等语料到位后提炼文风。'
    from app.models import DistillRun
    runs = list(db.scalars(
        select(DistillRun).where(DistillRun.project_id == project.id)
        .order_by(DistillRun.id.desc())).all())
    profile = getattr(project, 'style_narrative', None)
    if profile:
        if runs and runs[0].status == 'success':
            return 'done', f'最近一次蒸馏成功（{runs[0].rule_count or 0} 条规则）。'
        return 'done', '文风档案已就位（从蒸馏库应用）。'
    archive = _active_archive_for(db, _project_corpus_ids(project))
    if archive is not None:
        return ('pending',
                f'蒸馏库里已有这份语料的文风档案'
                f'（{archive.rule_count or 0} 条规则），应用到本项目后这一步才算完。')
    if not runs:
        return 'pending', '还没跑过文风蒸馏。'
    last = runs[0]
    if last.status == 'success':
        return 'done', f'最近一次蒸馏成功（{last.rule_count or 0} 条规则）。'
    if last.status in ('pending', 'running'):
        return 'pending', f'蒸馏还在跑（{last.stage or last.status}）。'
    return 'blocked', f'最近一次蒸馏失败：{str(last.error or "")[:120]}'


def _review_narrative(db: Any, project: Any, ctx: dict[str, Any] | None = None) -> tuple[str, ...]:
    profile = getattr(project, 'style_narrative', None)
    if not profile:
        if not _project_corpus_ids(project):
            if _has_story_seed(project):
                return 'skipped', '自由生成 → 没有文风档案可审。'
            return 'pending', '等蒸馏产物。'
        return 'pending', '还没有蒸馏产物可审。'
    from app.agents.audit_narrative import run_narrative_audit
    issues, _stats = run_narrative_audit(profile)
    errors = [i for i in issues if i.get('severity') == 'error']
    if errors:
        return 'blocked', (f'蒸馏产物有 {len(errors)} 个必须先处理的问题'
                           f'（第一件：{errors[0].get("title", "")}）。')
    return 'done', f'蒸馏产物审查通过（{len(issues)} 条提示不阻塞）。'


def _review_prompt(db: Any, project: Any, ctx: dict[str, Any] | None = None) -> tuple[str, ...]:
    """提示词体检：组装一次 writer 的静态提示词，逐条查。

    查的是「蒸馏产物有没有真进到提示词里」这一类**静默失败** ——
    它们不会报错，只会让文风蒸馏白做、或让正文开始像参考书。
    """
    if not _project_corpus_ids(project):
        if _has_story_seed(project):
            return 'skipped', '自由生成 → 没有文风档案，这步不适用。'
        return 'pending', '等文风档案就位。'
    from app.agents.prompt_audit import run_prompt_audit
    report, stats = run_prompt_audit(db, project)
    errors = [i for i in report.issues if getattr(i, 'severity', None) == 'error']
    if errors:
        return 'blocked', (f'提示词体检有 {len(errors)} 个必须先处理的问题'
                           f'（第一件：{getattr(errors[0], "title", "")}）。')
    if not stats.get('has_narrative'):
        return 'pending', '还没有文风档案可体检（先跑蒸馏）。'
    return 'done', f'提示词体检通过（{len(report.issues)} 条提示不阻塞）。'


def _segment_contracts(project: Any) -> dict[str, Any]:
    """项目的**冻结段契约**（与正文审查同源）。

    口径必须与 `app.agents.body_consistency.read_body` 一致：
    `project.plot_timeline['planning']['segment_contracts'][段号]`。
    自己另算一套就会与审计判据对不上 —— 那正是「查错对象」的坑。
    """
    planning = (getattr(project, 'plot_timeline', None) or {}).get('planning') or {}
    contracts = planning.get('segment_contracts') or {}
    return contracts if isinstance(contracts, dict) else {}


def _has_outline(project: Any) -> bool:
    """项目有没有剧情大纲（段契约的来源）。

    大纲是**契约的来源**：没有大纲，引擎不规划时间线
    （`engine._timeline_state_for` 第一行 `if not project.outline: return None`），
    就没有冻结契约，正文审查也就没有判据基准。
    """
    outline = getattr(project, 'outline', None)
    if isinstance(outline, str):
        return bool(outline.strip())
    if isinstance(outline, (list, tuple)):
        return any(str(item).strip() for item in outline)
    return False


def _write_body(db: Any, project: Any, ctx: dict[str, Any] | None = None) -> tuple[str, ...]:
    """正文轨的第一步：写正文。

    判据不只是「有没有正文」——**写了但没有段契约的正文过不了正文审查**：
    封闭清单审计要拿每段的冻结契约当判据基准，没有契约就是
    `missing_saved_authority`、零候选、覆盖不完整，整段验收直接
    `no_pending_acceptance`（实测：真书全流程第一次跑，审计 4 次尝试
    零模型调用就挂在这一步）。

    所以要先问「有没有段契约的来源」（剧情大纲），再问「有没有正文」——
    否则链上会把人引向「去写正文」，而写完才发现过不了审查。
    """
    from app.models import Generation
    rows = list(db.scalars(
        select(Generation).where(Generation.project_id == project.id)
        .order_by(Generation.segment_no, Generation.id)).all())
    contracts = _segment_contracts(project)
    missing = [r.segment_no for r in rows if not contracts.get(str(r.segment_no))]
    if not rows:
        if not _has_outline(project):
            return ('blocked',
                    '还没有剧情大纲 —— 写出来的正文过不了正文审查'
                    '（正文审查要拿每段的段契约当判据基准）。',
                    '先规划大纲')
        return 'pending', '还没有正文 —— 按你定的章数与字数写。'
    if missing:
        return ('blocked',
                f'已写出 {len(rows)} 段，但第 '
                f'{"、".join(str(n) for n in missing)} 段没有对应的段契约'
                ' —— 正文审查过不去（先补规划）。',
                '先补段契约')
    return 'done', f'已写出 {len(rows)} 段。'


def _review_body(db: Any, project: Any, ctx: dict[str, Any] | None = None) -> tuple[str, ...]:
    from app.models import Generation
    rows = list(db.scalars(
        select(Generation).where(Generation.project_id == project.id)
        .order_by(Generation.segment_no, Generation.id)).all())
    if not rows:
        return 'pending', '还没有正文可审。'
    # 有正文但缺段契约：审计会直接报 missing_saved_authority（零候选、覆盖不完整），
    # 整段验收连请求都不会发 —— 说清这个，比笼统的「还没做整段验收」有用。
    contracts = _segment_contracts(project)
    missing = [r.segment_no for r in rows if not contracts.get(str(r.segment_no))]
    if missing:
        return ('blocked',
                f'第 {"、".join(str(n) for n in missing)} 段没有对应的段契约 —— '
                '正文审查没有判据基准（封闭清单审计会判成 missing_saved_authority）。',
                '先补段契约')
    timeline = project.plot_timeline or {}
    pending = timeline.get('pending_acceptance') or {}
    if not pending:
        return 'pending', '还没做整段验收（要先跑闭环审计）。'
    if pending.get('state') == 'accepted':
        coverage = pending.get('plan_coverage') or {}
        blocking = coverage.get('blocking') or []
        if blocking:
            first = blocking[0] or {}
            return 'blocked', (f'正文与规划有 {len(blocking)} 处冲突'
                               f'（{first.get("title") or first.get("node_id")}）。')
        # —— 第三块：文字质量 ——
        # 越界由封闭清单审、状态一致由整段验收审；这里审**文字本身**。
        # error 级（标点事故这类规则判得死的）挡住；warn 级（外来词这类
        # 判不准的）只报不挡 —— 误报会堵死真活。
        from app.agents.text_quality import run_text_quality_audit
        tq_report, _tq_stats = run_text_quality_audit(db, project)
        tq_errors = [i for i in tq_report.issues
                     if getattr(i, 'severity', None) == 'error']
        tq_warns = [i for i in tq_report.issues
                    if getattr(i, 'severity', None) == 'warn']
        if tq_errors:
            return ('blocked',
                    f'正文有 {len(tq_errors)} 处必须改的标点事故'
                    f'（第一件：{getattr(tq_errors[0], "title", "")}）'
                    '—— 改完再审。',
                    '先改标点')
        if tq_warns:
            return ('done',
                    f'整段验收已通过，规划覆盖率没有缺口'
                    f'（另有 {len(tq_warns)} 条文字提示不阻塞）。')
        return 'done', '整段验收已通过，规划覆盖率没有缺口。'
    unresolved = pending.get('unresolved') or []
    if unresolved:
        return 'blocked', f'整段验收还差 {len(unresolved)} 项（第一项：{unresolved[0]}）。'
    return 'pending', '整段验收还没通过。'


def _repair(db: Any, project: Any, ctx: dict[str, Any] | None = None) -> tuple[str, ...]:
    """修复待批：有没有等着人处理的写回建议。

    状态词要特别小心：**没有待批项 ≠ 这步「已完成」**。
    前者说明「没东西要批」，后者说明「该做的都做完了」——
    混成一个词，用户会以为正文已经改过。

    另外两种「等着」要分开说：等人批 / 批了等人写进正文 ——
    下一步要做的事不一样，一句话混着说会让人不知道该点哪个按钮。
    """
    items = list((ctx or {}).get('open_proposals') or [])
    to_approve = [i for i in items if i.get('status') == 'pending']
    to_apply = [i for i in items if i.get('status') == 'approved']
    if to_approve:
        return ('blocked',
                f'有 {len(to_approve)} 条写回建议等着批准'
                '（批准并应用后，这一步才算做完）。',
                '等你批')
    if to_apply:
        return ('blocked',
                f'有 {len(to_apply)} 条改动批了还没写进正文'
                '（应用之后，这一步才算做完）。',
                '等你应用')
    return ('done', '没有待批的写回建议。', '没有待批项')


def _derived_rebuild(db: Any, project: Any, ctx: dict[str, Any] | None = None) -> tuple[str, ...]:
    """派生重建：有没有要按**当前正文**重算的派生数据。

    正文没被改过 → 没有要重建的 → 这步**不适用**（不是「待做」）。
    说成「待做」会让人以为还有活没干完，实际上是不存在这一步。
    """
    timeline = project.plot_timeline if isinstance(project.plot_timeline, dict) else {}
    state = timeline.get('derived_state') or {}
    rebuild = timeline.get('derived_rebuild') or {}
    pending = bool(timeline.get('body_revision_requires_review')) \
        or state.get('kg') == 'pending_rebuild' \
        or state.get('facts') == 'pending_rebuild'
    if not pending:
        return 'skipped', '正文没改过，没有要重建的派生数据。'
    if (state.get('kg') == 'rebuilt' and state.get('facts') == 'rebuilt'
            and rebuild.get('status') == 'needs_review'):
        return 'done', '摘要 / 完成度 / 图谱 / 事实已按当前正文重建。'
    return 'pending', '摘要 / 完成度 / 图谱 / 事实要按新正文重算。'


def _unlock(db: Any, project: Any, ctx: dict[str, Any] | None = None) -> tuple[str, ...]:
    """解锁：生成守卫解了没有。

    守卫只在**正文被改过**时才有 —— 正文没改过就不存在要解的锁，
    这步同样**不适用**。
    """
    timeline = project.plot_timeline if isinstance(project.plot_timeline, dict) else {}
    if not timeline.get('body_revision_requires_review'):
        return 'skipped', '没有待解除的生成守卫。'
    state = timeline.get('derived_state') or {}
    if state.get('unlocked_at'):
        return 'done', '生成守卫已解除，可以继续生成。'
    return 'pending', '重建与验收都过了，可以解除守卫放行生成。'


def _converged(db: Any, project: Any, ctx: dict[str, Any] | None = None) -> tuple[str, ...]:
    # 收敛 = 前面的工序都满足（这一步在 evaluate 里按顺序判定，不自己查库）。
    return 'pending', '前面的工序还没全部满足。'


_CRITERIA = {
    'corpus_ready': _corpus_ready,
    'clean_check': _clean_check,
    'distill': _distill,
    'review_narrative': _review_narrative,
    'review_prompt': _review_prompt,
    'write_body': _write_body,
    'review_body': _review_body,
    'repair': _repair,
    'derived_rebuild': _derived_rebuild,
    'unlock': _unlock,
    'converged': _converged,
}


# ----------------------------------------------------------------------
# 评估
# ----------------------------------------------------------------------

def evaluate(db: Any, project: Any) -> dict[str, Any]:
    """重新评估这个项目的工序状态。

    返回：
        {'status': 'active'|'done'|'blocked',
         'current_step': str|None,
         'steps': [{'id','title','state','state_label','reason','read_tools',
                    'write_proposal','note'}],
         'gates': [...],
         'summary': '人话一句话',
         'open_proposals': [{'id','target','reason','risk','status','created_at'}]}
    """
    # 等着人处理的写回建议先查一次：既给「修复待批」判据用，也直接返回给界面
    # （面板要能看见「在等什么」，而不是只看到一个「卡住了」）。
    ctx: dict[str, Any] = {'open_proposals': _pending_proposals(db, project)}
    steps: list[dict[str, Any]] = []
    first_open: str | None = None
    for step_id in _CRITERIA_ORDER:
        step = STEP_BY_ID[step_id]
        result = _CRITERIA[step_id](db, project, ctx)
        state, reason = result[0], result[1]
        label = result[2] if len(result) > 2 else None
        if step_id == 'converged':
            # 收敛判据是「前面都满足」，不自己查库。
            blocked = [s for s in steps if s['state'] == 'blocked']
            pending = [s for s in steps if s['state'] == 'pending']
            if blocked:
                state, reason = 'blocked', (f'卡在「{blocked[0]["title"]}」：'
                                           f'{blocked[0]["reason"]}')
                # 状态词说「还差几步」而不是「卡住」：这一步的卡点是**前面**那道工序，
                # 说成「卡住」会让人以为收敛本身坏了。
                label = f'还差 {len(blocked) + len(pending)} 步'
            elif pending:
                state, reason = 'pending', (f'还没到「{pending[0]["title"]}」'
                                            f'（{pending[0]["reason"]}）')
                label = f'还差 {len(pending)} 步'
            else:
                state, reason = 'done', '这一轮全部做完了。'
                label = '已收敛'
        steps.append({
            'id': step.id, 'title': step.title, 'state': state, 'reason': reason,
            'state_label': label,
            'read_tools': list(step.read_tools), 'write_proposal': step.write_proposal,
            # 这一步能提哪几类**动作提案**（服务端说了算，agent 不自己发明）。
            'write_actions': list(STEP_WRITE_KINDS.get(step.id, ())),
            'track': step.track, 'dispatch': step.dispatch, 'note': step.note,
        })
        if first_open is None and state in ('pending', 'blocked'):
            first_open = step.id

    gates = [s for s in steps if s['state'] == 'blocked']
    done = all(s['state'] in ('done', 'skipped') for s in steps)
    if done:
        status, current = 'done', None
        summary = '这一轮做完了：语料、蒸馏、审查都过了，没有待批的修改。'
    else:
        # 当前步 = 链上**第一个还没做完**的工序（按顺序）。
        #
        # 为什么要「第一个」而不是「第一个被卡住的」：被卡住的那步可能在**后面**
        # （「写正文」缺段契约就是这种），而前面还有没做的工序（蒸馏）。
        # 说「卡在写正文」会让人以为先解决它 —— 其实先做蒸馏才对，
        # 链会把人引到错误的动作上（同「判据说错话，派发建议也会错」）。
        current = first_open
        nxt = next((s for s in steps if s['id'] == current), None)
        status = 'blocked' if gates else 'active'
        if nxt is not None and nxt['state'] == 'blocked':
            summary = f'卡在「{nxt["title"]}」：{nxt["reason"]}'
        else:
            summary = (f'下一步：{nxt["title"]} —— {nxt["reason"]}'
                       if nxt else '进行中')
            others = [g['title'] for g in gates if g['id'] != current]
            if others:
                summary += f'（另有 {len(others)} 处待处理：{"、".join(others)}）'
    needs_decision = None
    if current:
        step = next((s for s in steps if s['id'] == current), None)
        if step is not None and step['state'] == 'pending':
            from app.agents.decision_points import decision_for_step
            point = decision_for_step(current)
            if point is not None:
                needs_decision = {'key': point.key, 'title': point.title,
                                  'question': point.question, 'step_id': point.step_id}
    # 状态词补齐：判据没给就用默认；**当前**这一步的「待做」改成「下一步」——
    # 同样是没做，但「下一步」告诉用户该动哪一下，后面排队的步骤不该抢戏。
    for s in steps:
        if not s.get('state_label'):
            s['state_label'] = _DEFAULT_STATE_LABELS.get(s['state'], '未知')
        if s['id'] == current and s['state'] == 'pending':
            s['state_label'] = '下一步'
    return {'status': status, 'current_step': current, 'steps': steps,
            'gates': [g['id'] for g in gates], 'summary': summary,
            'needs_decision': needs_decision,
            'open_proposals': list(ctx['open_proposals'])}


def get_or_create_run(db: Any, project: Any) -> Any:
    """取这个项目的工序链行（没有就建一条空的）。"""
    from app.models import WorkflowRun
    run = db.scalar(
        select(WorkflowRun).where(WorkflowRun.project_id == project.id)
        .order_by(WorkflowRun.id.desc()))
    if run is None:
        from app.models import _utcnow
        run = WorkflowRun(project_id=project.id, status='active', current_step=None,
                          round=1, steps=[], gates=[],
                          created_at=_utcnow(), updated_at=_utcnow())
        db.add(run)
        db.commit()
        db.refresh(run)
    return run


def record(db: Any, project: Any, *, step_id: str, action: str, detail: str = '',
           proposal_id: int | None = None) -> Any:
    """记一笔工序推进（谁在什么时候做了哪一步；提了哪条提案）。"""
    from app.models import _utcnow
    run = get_or_create_run(db, project)
    steps = list(run.steps or [])
    steps.append({'step_id': step_id, 'action': action, 'detail': detail,
                  'proposal_id': proposal_id, 'at': _utcnow().isoformat()})
    run.steps = steps
    gates = list(run.gates or [])
    if proposal_id is not None and str(proposal_id) not in [str(g) for g in gates]:
        gates.append(proposal_id)
    run.gates = gates
    run.updated_at = _utcnow()
    db.commit()
    db.refresh(run)
    return run


def workflow_decision_card(db: Any, project: Any,
                            query: str = '') -> dict[str, Any] | None:
    """工序链当前需要**用户决断**时，给一张选择框卡片（没有则 None）。

    什么时候需要用户决断：工序链的**下一步**正等着开始，而那一步有个岔路口
    系统不能替作者决定（蒸馏用哪一档、一次生成几章…）。
    决策点与选项来自注册表（服务端说了算），模型不参与编选项。
    """
    from app.agents.decision_points import build_choice_action, decision_for_step
    state = evaluate(db, project)
    current = state.get('current_step')
    if not current:
        return None
    steps = {s['id']: s for s in state['steps']}
    step = steps.get(current)
    if step is None or step['state'] != 'pending':
        # 只在「正等着开始」时给选项；已经卡住（blocked）时给的是原因，不是选项。
        return None
    from app.agents.decision_points import decision_for_step
    point = decision_for_step(current, query)
    if point is None:
        return None
    return build_choice_action(point, project_id=int(project.id))


def generation_block(db: Any, project: Any) -> dict[str, Any] | None:
    """生成闸门：工序链没过就别生成（避免「越过流程生成」）。

    只在**有具体问题**时拦：

    - 没有剧情大纲 → 写出来的正文过不了审查（详见下面）；
    - 有待批的写回建议 → 正文要改完才能继续生成；
    - 提示词体检有 error 级问题（文风档案没真进到提示词里）→ 生成出来也是白写。

    自由生成（没选参考语料）、没有文风档案的项目一律不拦 ——
    闸门拦的是「流程没走完」，不是「项目没资料」。

    ## 为什么「没有大纲」也要拦

    大纲是**段契约的来源**（`engine._timeline_state_for` 第一行
    `if not project.outline: return None`）：没有大纲 → 没有时间线 →
    没有冻结契约 → 封闭清单审计没有判据基准（`missing_saved_authority`、
    零候选、覆盖不完整）→ 整段验收直接 `no_pending_acceptance`。
    **写出来的正文过不了审查**（实测：真书全流程第一次跑就卡在这里）。

    所以这条闸门拦的不是「用户没准备」，而是「这一步做出来的东西没人能审」。

    **有出路**：助理能起草一份大纲并提出来（动作提案 `plan_outline`），
    人点确认后写入项目，再回来生成 —— 不是死路。
    """
    from app.models import EditProposal
    if not _has_outline(project) and _has_story_seed(project):
        return {
            'status': 'workflow_needs_outline',
            'message': ('还没有剧情大纲 —— 写出来的正文过不了正文审查'
                        '（正文审查要拿每段的段契约当判据基准）。'
                        '跟助理说一声就能让它起草一份，你确认后就能写正文了。'),
        }
    pending = list(db.scalars(
        select(EditProposal).where(
            EditProposal.project_id == project.id,
            EditProposal.status == 'pending')).all())
    if pending:
        return {
            'status': 'workflow_pending_repair',
            'message': (f'有 {len(pending)} 条修改建议还没批 —— '
                        '正文要改完才能继续生成（别绕过流程）。'),
            'pending_proposals': len(pending),
        }
    if project.reference_novel_ids:
        from app.agents.prompt_audit import run_prompt_audit
        report, _stats = run_prompt_audit(db, project)
        errors = [i for i in report.issues if getattr(i, 'severity', None) == 'error']
        if errors:
            return {
                'status': 'workflow_prompt_not_ready',
                'message': (f'文风档案还没过关（{len(errors)} 个问题，第一件：'
                            f'{getattr(errors[0], "title", "")}）—— 先处理它再生成。'),
                'prompt_errors': len(errors),
            }
    return None


def state_to_dict(db: Any, project: Any, run: Any = None) -> dict[str, Any]:
    """工序状态 → API/工具用的 dict（人话 + 机器字段）。"""
    evaluation = evaluate(db, project)
    payload = dict(evaluation)
    payload['project_id'] = int(project.id)
    payload['round'] = int(getattr(run, 'round', 1) or 1) if run is not None else 1
    payload['updated_at'] = (
        getattr(run, 'updated_at', None).isoformat()
        if run is not None and getattr(run, 'updated_at', None) else None)
    return payload
