"""系统提示词体检：把 writer 真正吃进去的那份静态提示词**组装一次**，逐条查。

## 为什么要有这道体检

「蒸馏跑完了」≠「writer 吃到了文风」。中间隔着好几道会**静默失败**的门：

- 档案存着，但渲染进提示词时是空的（维度对不上 / 内容被过滤）→ 蒸馏白做；
- 叙事档案里的「禁用词」自己也写在档案正文里 → 「不许出现 X」与档案自相矛盾；
- 原创模式却把参考书的**角色声音词典**带进来 → 模型照着参考书角色造人；
- 覆写块超长 → 挤掉正文额度，也击穿提示词缓存；
- 静态提示词整体过长 → 同样挤正文额度。

这些都不会报错，只会让「文风蒸馏白做了」或「正文开始像参考书」。
所以要有一道**只读**的体检：组装 → 查 → 出报告（九字段 issue 结构，与其它审核同构）。

## 与已有审核的分工

- `audit_narrative` 审**档案 JSON 本身**（规则太短、维度膨胀、剧情泄漏）；
- 本模块审**组装后的提示词**（注入是否生效、长度、禁词矛盾、声音词典泄漏、覆写越界）。
两者互补，不重复。
"""

from __future__ import annotations

from typing import Any

#: 静态系统提示词的字符预算（超了会挤正文额度，也击穿提示词缓存的收益）。
SYSTEM_PROMPT_BUDGET = 12000

#: 单条提示词覆写块的字符上限（与 prompt_overrides 的落库上限同口径）。
OVERRIDE_TEXT_LIMIT = 4000


def _issue(taken: set[str], *, issue_id: str, issue_type: str, severity: str,
           title: str, claim: str, suggestion: str, rule: str,
           evidence: tuple = ()) -> Any:
    from app.agents.diagnosis import DiagnosticIssue, make_issue_id
    return DiagnosticIssue(
        id=make_issue_id(rule, issue_id, taken),
        type=issue_type,
        severity=severity,
        title=title,
        claim=claim,
        suggestion=suggestion,
        rule=rule,
        evidence=evidence,
    )


def run_prompt_audit(db: Any, project: Any, *, profile: str = 'original'
                     ) -> tuple[Any, dict[str, Any]]:
    """组装一次 writer 的静态系统提示词，逐条查。

    返回 `(DiagnosticReport, 概要统计)` —— 与其它审核同构（九字段 issue）。
    """
    from app.agents.diagnosis import DiagnosticReport, IssueEvidence
    from app.generation.profiles import normalize_profile, rules_for
    from app.generation.prompts import _narrative_block, build_system_prompt

    issues: list[Any] = []
    taken: set[str] = set()
    stats: dict[str, Any] = {
        'profile': str(profile),
        'has_narrative': False,
        'narrative_injected': False,
        'system_prompt_chars': 0,
        'overrides': [],
    }

    narrative = getattr(project, 'style_narrative', None)
    if isinstance(narrative, dict) and narrative:
        stats['has_narrative'] = True

    # —— 组装：提示词块（与引擎同一口径）——
    rules = rules_for(normalize_profile(profile))
    narrative_block = _narrative_block(
        narrative if isinstance(narrative, dict) else None,
        include_voice=bool(rules.get('include_voice', True)),
    )
    stats['narrative_injected'] = bool(narrative_block)

    reminders: dict[str, Any] = {}
    try:
        from app.kg.queries import get_reminders
        reminders = get_reminders(db, project.id, source='generated') or {}
    except Exception:  # noqa: BLE001 - 取不到提醒不算体检失败
        reminders = {}

    style_dims: dict[str, Any] = {}
    if project.genre or project.style:
        style_dims = {'类型': project.genre, '风格平台': project.style}
    style_profile = None
    try:
        from app.generation.engine import _style_profile
        style_profile = _style_profile(db, project)
    except Exception:  # noqa: BLE001
        style_profile = None

    # 体检的是**真正下发的那份提示词** —— 运行时覆写已经接进生成路径了，
    # 体检不带上覆写就会漏判「覆写把提示词撑太长」这类问题。
    try:
        from app.agents.prompt_overrides import resolve_overrides_for_generation
        _ov = resolve_overrides_for_generation(db, project.id)
    except Exception:  # noqa: BLE001 - 取不到覆写不算体检失败
        _ov = {}

    system = build_system_prompt(
        project.title or '', reminders, style_dims or None, project.plot_bible,
        style_profile, narrative if isinstance(narrative, dict) else None,
        profile=profile, character_names=None,
        override_style=_ov.get("style_suffix", ""),
        override_constraint=_ov.get("constraint_note", ""),
    )
    stats['system_prompt_chars'] = len(system)

    # —— 查 1：蒸馏产物在，但没进提示词 → 蒸馏白做 ——
    if stats['has_narrative'] and not narrative_block:
        issues.append(_issue(
            taken, issue_id='narrative', issue_type='prompt_effectiveness',
            severity='error',
            title='文风蒸馏产物没有进到系统提示词里',
            claim='项目里有叙事档案（蒸馏产物），但渲染成提示词块后是空的 —— '
                  'writer 这次生成吃到的还是默认文风，蒸馏等于白做。',
            suggestion='查档案的维度键是否与当前方法维度清单一致（app.style.layers.method_rules），'
                       '或重跑一次蒸馏（换档位/换语料）。',
            rule='prompt_narrative_not_injected',
            evidence=(IssueEvidence(kind='novel', source=f'project:{project.id}',
                                    ref_id=f'project:{project.id}',
                                    snippet='style_narrative 非空但叙事提示块为空', score=0.8),),
        ))

    # —— 查 2：原创模式的**渲染块**里带着参考书角色的说话方式 ——
    # 判据是**渲染结果**，不是档案内容。
    #
    # 为什么：档案里留着【角色声音词典】本身是**对的** —— 续写模式写的就是
    # 原书角色，角色聊天模式也要它。真正要防的是「**原创模式**的提示词里
    # 出现了它」。原创模式的 `include_voice=False`（见 profiles.PROFILE_RULES），
    # 渲染块里本来就没有这个维度。
    #
    # 老写法查的是「档案里有没有这个维度」→ 任何含真书档案的项目在原创模式
    # 都被判成泄漏，连带被生成闸门挡住（实测：《北荒刀歌》真书档案必误报）。
    # 它给的建议「或修 profiles.PROFILE_RULES['original']['include_voice']」
    # 更是让人去修一件**早就修好**的事 —— 那就是判据查错了对象的信号。
    if isinstance(narrative, dict) and narrative and normalize_profile(profile) == 'original':
        try:
            from app.generation.prompts import _narrative_block as _block
            plain = _block(narrative, include_voice=False)
            # 渲染块与「不含声音词典」的版本不同 → 声音词典真被注进去了
            leaked = bool(narrative_block) and narrative_block != plain
        except Exception:  # noqa: BLE001
            leaked = False
    else:
        leaked = False
    if leaked:
        issues.append(_issue(
            taken, issue_id='voice_leak', issue_type='prompt_effectiveness',
            severity='error',
            title='原创模式把参考书的角色声音词典带进了提示词',
            claim='原创模式的提示词块里含有【角色声音词典】（参考书角色怎么说话）—— '
                  '模型会照着参考书角色的自称习惯造人。',
            suggestion='查 profiles.PROFILE_RULES["original"]["include_voice"]：'
                       '它必须是 False（原创模式只给方法，不给参考书角色）。',
            rule='prompt_voice_leak_in_original',
            evidence=(IssueEvidence(kind='novel', source=f'project:{project.id}',
                                    ref_id=f'project:{project.id}',
                                    snippet='原创模式的渲染块里含有【角色声音词典】', score=0.8),),
        ))

    # —— 查 3：禁用词与**规则正文**自相矛盾（档案说不许出现 X，规则正文就在写 X）——
    # 比的对象是「规则正文」，不是整个渲染块。
    #
    # 为什么：渲染块**本身含禁用清单**
    # （`【禁用转折词】禁止使用：然而、但是、不过…`），拿整块去比等于
    # **禁用清单和自己匹配** —— 只要档案有禁用清单就恒真（实测：真书档案必误报）。
    # 真正要抓的是：规则正文在教模型用某个词，而清单同时禁这个词。
    if isinstance(narrative, dict) and narrative:
        from app.style.narrative import FORBIDDEN_SECTION_MARKER
        rules_text = narrative_block.split(FORBIDDEN_SECTION_MARKER, 1)[0]
        forbidden = (narrative.get('forbidden') or {})
        hits: list[str] = []
        for words in forbidden.values():
            for word in (words or []):
                if word and str(word) in rules_text:
                    hits.append(str(word))
        if hits:
            issues.append(_issue(
                taken, issue_id='forbidden_conflict', issue_type='prompt_consistency',
                severity='error',
                title='档案的禁用词与档案正文自相矛盾',
                claim=f'档案把这些词列为禁用（不得出现在正文里），但档案正文自己就写着它们：'
                      f'{"、".join(sorted(set(hits))[:5])}。',
                suggestion='从档案规则里删掉这些词，或从禁用清单里去掉（两者不能同时说）。',
                rule='prompt_forbidden_self_conflict',
                evidence=(IssueEvidence(kind='novel', source=f'project:{project.id}',
                                        ref_id=f'project:{project.id}',
                                        snippet='禁用词：' + '、'.join(sorted(set(hits))[:5]),
                                        score=0.7),),
            ))

    # —— 查 4：静态提示词过长（挤正文额度、也击穿缓存收益）——
    if len(system) > SYSTEM_PROMPT_BUDGET:
        issues.append(_issue(
            taken, issue_id='too_long', issue_type='prompt_budget',
            severity='warn',
            title=f'静态系统提示词过长（{len(system)} 字符，预算 {SYSTEM_PROMPT_BUDGET}）',
            claim='静态提示词太长会挤掉正文额度，也击穿提示词缓存的收益（每段都要重发）。',
            suggestion='精简档案规则条数（默认每维 5 条，觉得太长就调小）'
                       '或换更低的蒸馏档位。',
            rule='prompt_system_too_long',
            evidence=(IssueEvidence(kind='novel', source=f'project:{project.id}',
                                    ref_id=f'project:{project.id}',
                                    snippet=f'系统提示词 {len(system)} 字符', score=0.5),),
        ))

    # —— 查 5：提示词覆写块越界 / 生效来源不明 ——
    try:
        from app.agents.prompt_overrides import PROMPT_KEYS, resolve_prompt_block
        for key in PROMPT_KEYS:
            text, source = resolve_prompt_block(db, project.id, key)
            stats['overrides'].append({'prompt_key': key, 'source': source,
                                       'chars': len(text or '')})
            if len(text or '') > OVERRIDE_TEXT_LIMIT:
                issues.append(_issue(
                    taken, issue_id=f'override_{key}', issue_type='prompt_budget',
                    severity='error',
                    title=f'提示词覆写块「{key}」超长（{len(text)} 字符）',
                    claim='运行期覆写块超过上限，容易变成注入超长提示（也会挤正文额度）。',
                    suggestion='把这段覆写拆短或删掉；覆写只能追加，不能替换代码默认块。',
                    rule='prompt_override_overlong',
                    evidence=(IssueEvidence(kind='novel', source=f'project:{project.id}',
                                            ref_id=f'project:{project.id}',
                                            snippet=f'{key}: {len(text)} 字符', score=0.7),),
                ))
    except Exception:  # noqa: BLE001 - 取不到覆写状态不算体检失败
        pass

    report = DiagnosticReport(
        issues=issues,
        extra_evidence=[],
        tool_results=stats,
        rules_evaluated=[
            'prompt_narrative_not_injected',
            'prompt_voice_leak_in_original',
            'prompt_forbidden_self_conflict',
            'prompt_system_too_long',
            'prompt_override_overlong',
        ],
    )
    return report, stats
