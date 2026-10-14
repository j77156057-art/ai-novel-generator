"""叙事档案（narrative）：用 LLM 从参考语料提炼 L2/L3 认知层规则，附证据账本。

## 为什么需要它

`style/stylometry.py` 产出的是 L1 表层统计（句长、标点密度、对白占比）——这些数字能
验收「像不像」，但**教不会模型怎么写**。小说的 L2 结构层（开头钩子形态、段落编排模式、
章末收束惯例）和 L3 认知层（人物描写手法、对白习惯、爽点节奏、主角依恋机制、
剧情编排模式）一项都没覆盖。这些维度必须由 LLM 深度阅读后提炼成可注入的规则。

## 与 stylometry 的分工

- `stylometry.py`：纯统计、零 LLM、离线可复现，产出数字档案
- `narrative.py`：LLM 提炼、需要 API key、产出结构化规则文档，落库到 `style_narrative` 列

两者**独立缓存、独立失效**：`style_profile` 的指纹是 `source_ids + unit_chars + version + schema`；
`style_narrative` 的指纹是 `source_ids + narrative_version`。改了提炼逻辑只需把
`narrative_version` +1，不需要碰统计档案的指纹逻辑。

## 证据账本（防幻觉伪规则）

每条规则必须附原文证据。LLM 提炼时容易编造「这个作者其实不这么写」的伪规则，
强制附证据是唯一有效的防幻觉手段。证据段落必须真实存在于语料中，
合并时做删除测试：删掉某条规则后对照原文验证是否成立。

## 不做全文覆盖

L3 深读只覆盖代表性章节（20~30 章），剩下的是重复模式。同时避开早期文风漂移问题——
长篇作者文风是演化的，早期章节的写法不能代表成熟期。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

#: 叙事档案的形状修订号。**任何会改变档案字段的改动都必须把它 +1。**
#:
#: 与 `style/pipeline.py` 的 `PIPELINE_SCHEMA` 同理：指纹只有 `source_ids + narrative_version`，
#: 管不到「档案长什么样」。加字段时老档案不会自动失效，前端拿到的摘要会永远少一块。
NARRATIVE_VERSION = 7

#: 采样比例：从全文章节中采样的百分比。
#: 603 章 × 15% = 90 章，分 4~5 块。平衡覆盖面与 LLM 调用成本。
SAMPLE_RATIO = 0.15

#: 采样的字数下限：短篇/短语料若按比例采样会砍掉过多对白，
#: 导致角色声音维度失效（实测：10 章语料只提得出 1 个角色声音）。
#: 采样结果若低于此字数，则扩容到达到下限；若全量仍不足，则全取。
#: 30 万字 ≈ 100+ 章网文，足够覆盖角色对白与场景多样性。
MIN_SAMPLE_CHARS = 300_000

#: 每块最少章节数（太少提炼不出模式）
MIN_CHAPTERS_PER_CHUNK = 5

#: 场景类型关键词（用于场景均衡采样，确保每种场景都有足够样本）
SCENE_KEYWORDS = {
    "病房/医疗": ["手术室", "病房", "医院", "医生", "护士", "病床", "抢救", "急救"],
    "宴会/社交": ["宴会", "晚宴", "酒会", "宾客", "包厢", "宴席", "酒席"],
    "办公室/商务": ["办公室", "会议室", "总裁", "秘书", "文件", "合同", "签字", "股权"],
    "车内/私密": ["车里", "车内", "驾驶座", "副驾驶", "后座", "车窗"],
    "户外/街道": ["街头", "马路", "巷子", "公园", "咖啡厅", "餐厅"],
    "家中/起居": ["客厅", "卧室", "厨房", "书房", "沙发", "床上"],
}

#: 叙事档案要覆盖的维度（与 writer-novel-style-distiller 的维度设计对齐）
NARRATIVE_DIMENSIONS = [
    "人物描写手法",
    "对白风格",
    "段落编排",
    "场景构建",
    "情绪表达",
    "节奏控制",
    "世界观呈现",
    # —— v3 新增维度 ——
    "剧情编排模式",
    "角色声音词典",
    "伏笔与悬念设计",
    "文笔量化审计",
    # —— v5 新增维度 ——
    "节拍序列",
    "高频场景模板",
    "潜台词模式",
]

#: 角色声音提取：从图谱取高频角色名，单独分析说话风格。
#: 这些角色的「口头禅/自称/敬语/句长」直接影响生成时角色是否「像本人」。
CHARACTER_VOICE_DIMENSION = "角色声音词典"

#: 反向禁忌维度：作者**从不使用**的表达模式。这是防 AI 腔的关键——
#: LLM 更容易遵循具体的「不要用 X」而不是通用的「忌 AI 腔」。
#: 参考 writer-novel-style-distiller 的「反向禁忌清单」设计 [调研结论]。
FORBIDDEN_DIMENSIONS = [
    "禁用转折词",      # 如「就在这时」「突然」
    "禁用情绪直陈",    # 如「心中充满了」「眼中闪过一丝」
    "禁用段尾拔高",    # 如「他知道」「她明白」+ 总结句
    "禁用解释性对白",  # 如对话交代世界观/设定
]


def _load_novel_chapters(db: Session, novel_id: int) -> list[tuple[int, str]]:
    """加载一本语料的全部章节，返回 [(chapter_no, text), ...]，按章节号排序。"""
    from app.models import NovelChunk

    rows = db.scalars(
        select(NovelChunk)
        .where(NovelChunk.novel_id == novel_id)
        .order_by(NovelChunk.idx)
    ).all()
    return [(r.chapter_no or r.idx, r.text or "") for r in rows]


def _sample_chapters(
    chapters: list[tuple[int, str]],
    max_chapters: int = 180,  # v6: 默认 30% × 600 章 ≈ 180
) -> list[tuple[int, str]]:
    """从全部章节中均匀采样代表性章节。

    优先取后半部分（成熟期文风），避免早期文风漂移问题。
    采样量不足时全部返回。
    """
    if len(chapters) <= max_chapters:
        return chapters
    # 取后 2/3（避开早期稚嫩期），再均匀采样
    start = len(chapters) // 3
    pool = chapters[start:]
    if len(pool) <= max_chapters:
        return pool
    step = len(pool) / max_chapters
    picks = sorted({int(i * step) for i in range(max_chapters)})
    return [pool[i] for i in picks]


def _build_extract_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造单块提炼的 system/user prompt。

    关键改进：要求 LLM 输出**抽象规则描述**，不是原文引用。
    旧版 prompt 只说「附原文证据」，LLM 直接把原文句子当规则输出——
    这些原文句子作为「叙事手法」注入提示词，信息量极低。
    """
    system = (
        "你是一位资深中文网文编辑，擅长分析作者的写作风格与叙事手法。"
        "你的任务是从给定文本中提炼**可复用的叙事规则**，而不是总结剧情或复制原文。\n\n"
        "规则要求：\n"
        "1. 每条规则必须是**抽象的写作手法描述**，用你自己的话概括作者「怎么写的」，"
        "**绝对不要直接复制原文句子作为规则**\n"
        "2. 规则描述后面括号里附**原文证据**——引用一句原文（≤20 字）证明这条规则存在\n"
        "3. 规则要**具体可操作**，例如「用连续动作描写紧张感」而不是「文笔好」\n"
        "4. 只提炼原文中**确实反复出现**的模式，不要编造\n"
        "5. 按以下维度分类输出\n\n"
        "**正确的规则示例**：\n"
        "  人物描写手法: [\"用动作和神态暗示情绪，不直接写'害怕/紧张'（证据：指尖不自觉收紧）\", "
        "\"对白后紧跟动作描写，不用'说道'收尾（证据：南宫爵沉声道）\"]\n"
        "**错误的规则示例（不要这样输出）**：\n"
        "  人物描写手法: [\"小钟这话，可是壮着胆子说的。\", \"听言，南宫爵的黑眸顿时涌起了一阵冷厉。\"]\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "人物描写手法": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "对白风格": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "段落编排": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "场景构建": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "情绪表达": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "节奏控制": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "世界观呈现": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "剧情编排模式": ["伏笔如何埋设与回收（证据：原文引用）", "冲突如何升级（证据：原文引用）", ...],\n'
        '  "角色声音词典": ["具体角色名：说话习惯/口头禅/自称（证据：原文引用）", "具体角色名：说话习惯/口头禅/自称（证据：原文引用）", ...],\n'
        '  "伏笔与悬念设计": ["悬念如何设置与揭示（证据：原文引用）", ...],\n'
        '  "文笔量化审计": ["语言控制特点（证据：原文引用）", "信息组织方式（证据：原文引用）", ...]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼叙事规则：\n\n"
        f"{chunk_text}\n\n"
        "请按上述维度输出 JSON。每条规则必须是抽象描述+原文证据，不要复制原文作为规则。"
    )
    return system, user


def _build_forbidden_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造反向禁忌提取的 system/user prompt。

    反向禁忌 = 作者**从不使用**的表达模式。LLM 更容易遵循具体的
    「不要用 X」而不是通用的「忌 AI 腔」。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中提炼**这个作者从不使用的表达模式**"
        "（反向禁忌清单）。这些禁忌将在 AI 续写时注入提示词，防止生成 AI 腔。\n\n"
        "要求：\n"
        "1. 每条禁忌必须是**具体的词句模式**，不是抽象描述\n"
        "2. 只列原文中**确实没有出现**的模式（如果原文中出现了，就不是禁忌）\n"
        "3. 按以下维度分类\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "禁用转折词": ["就在这时", "突然之间"],\n'
        '  "禁用情绪直陈": ["心中充满了", "眼中闪过一丝"],\n'
        '  "禁用段尾拔高": ["他知道，", "她明白，"],\n'
        '  "禁用解释性对白": ["你知道吗？", "我告诉你"]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼这个作者**从不使用**的表达模式：\n\n"
        f"{chunk_text}\n\n"
        "请按上述维度输出 JSON。只列原文中没有出现的模式。"
    )
    return system, user


def _parse_extract_result(raw: str) -> dict:
    """解析 LLM 返回的 JSON，容错处理。"""
    import json
    import re

    # 尝试直接解析
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试提取 JSON 块（LLM 有时会包在 ```json ... ``` 里）
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # 尝试提取 { ... } 块
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass

    return {}


def _validate_evidence(rule: str, chunk_text: str) -> bool:
    """验证规则中的证据是否真实存在于原文中。

    从规则字符串中提取「证据：xxx」部分，检查 xxx 是否在原文中出现。
    如果规则本身就是原文中的句子（LLM 直接引用了原文），也直接验证。

    这是防幻觉的最后一道闸——LLM 编造的伪规则在这里被拦掉。
    """
    import re

    # 尝试提取「证据：xxx」部分
    m = re.search(r"证据[：:]\s*(.+?)[）)]?\s*$", rule)
    if m:
        evidence = m.group(1).strip()
    else:
        # LLM 可能直接引用了原文句子作为规则，没有加「证据：」前缀
        evidence = rule.strip()

    if not evidence or len(evidence) < 4:
        return False
    # 归一化：去空白 + 统一引号（LLM 常用单引号，原文用双引号）
    def _norm(s: str) -> str:
        s = re.sub(r"\s+", "", s)
        s = s.replace("'", "").replace(""", "").replace(""", "")
        s = s.replace("'", "").replace(""", "").replace(""", "")
        return s
    return _norm(evidence) in _norm(chunk_text)


def _is_verbatim_copy(rule: str, chunk_text: str) -> bool:
    """检测规则是否就是原文句子的逐字复制（没有抽象化）。

    LLM 有时不按 prompt 要求输出抽象规则，而是直接复制原文句子。
    这种规则作为「叙事手法」注入提示词，信息量极低。
    检测方法：规则文本（去引号归一化后）是否是原文中的某个完整句子。
    """
    import re

    def _norm(s: str) -> str:
        s = re.sub(r"\s+", "", s)
        s = s.replace("'", "").replace(""", "").replace(""", "")
        s = s.replace("'", "").replace(""", "").replace(""", "")
        return s

    rule_norm = _norm(rule)
    if len(rule_norm) < 8:
        return False
    # 检查规则是否是原文中的某个句子（按句号/问号/感叹号切分）
    sentences = re.split(r"[。！？…]", chunk_text)
    for sent in sentences:
        sent_norm = _norm(sent)
        if sent_norm and rule_norm == sent_norm:
            return True
        # 规则是原文句子的前缀（LLM 截断了句子）
        if len(sent_norm) > len(rule_norm) and sent_norm.startswith(rule_norm):
            return True
    return False


#: 角色声音提取的目标角色数上限。百万字小说有几百个角色，
#: 取高频前 N 个（主角 + 重要配角 + 关键反派）。
#: 清洗后的规范名池（去伪角色/去分词截断）没有名额浪费，可以放宽到 30。
MAX_VOICE_CHARACTERS = 30

#: 主要角色逐个提取的数量上限（前 N 个高频角色单独提取，深度分析）
VOICE_MAIN_CHARACTERS = 8

#: 配角分批提取的数量（每批 N 个角色，1 次 LLM 调用）
VOICE_BATCH_SIZE = 7


def _scene_type(text: str) -> str:
    """识别章节的主要场景类型。返回 SCENE_KEYWORDS 的键，无匹配返回「其他」。"""
    best_type = "其他"
    best_count = 0
    for scene_type, keywords in SCENE_KEYWORDS.items():
        count = sum(text.count(kw) for kw in keywords)
        if count > best_count:
            best_count = count
            best_type = scene_type
    return best_type


def _sample_chapters_v6(
    chapters: list[tuple[int, str]], ratio: float = SAMPLE_RATIO
) -> list[tuple[int, str]]:
    """按百分比采样 + 对话密集优先 + 场景均衡。

    策略：
    1. 按 ratio 计算采样量（603 章 × 30% = 180 章）
    2. 按场景类型分组，每种场景至少采样几章（确保覆盖面）
    3. 剩余配额按对话密度排序填充（对白多的章节优先）
    """
    if not chapters:
        return []
    total_chars = sum(len(t) for _, t in chapters if t)

    # 短篇保护：若全语料字数就不足下限，直接全取（没有砍的空间）。
    if total_chars <= MIN_SAMPLE_CHARS:
        return list(chapters)

    # 目标章节数：按比例取，但保证采样字数不低于下限。
    # 平均章长估算需要多少章才够 MIN_SAMPLE_CHARS。
    avg_ch = total_chars / max(len(chapters), 1)
    ratio_target = int(len(chapters) * ratio)
    chars_target = int(MIN_SAMPLE_CHARS / max(avg_ch, 1))
    target = max(10, ratio_target, chars_target)
    if len(chapters) <= target:
        return list(chapters)

    def dialogue_ratio(text: str) -> float:
        if not text:
            return 0.0
        quote_chars = 0
        for q in ['"', '"', '"', "'", "'", "'"]:
            quote_chars += text.count(q)
        return quote_chars / max(len(text), 1)

    # 按场景类型分组
    scene_groups: dict[str, list[tuple[float, int, str]]] = {}
    for ch_no, text in chapters:
        if not text:
            continue
        st = _scene_type(text)
        scene_groups.setdefault(st, []).append((dialogue_ratio(text), ch_no, text))

    # 每种场景至少取 3 章（对话密度最高的）
    picked: list[tuple[int, str]] = []
    picked_nos: set[int] = set()
    per_scene_min = 3
    for st, items in scene_groups.items():
        items.sort(key=lambda x: -x[0])
        for _, ch_no, text in items[:per_scene_min]:
            if ch_no not in picked_nos:
                picked.append((ch_no, text))
                picked_nos.add(ch_no)

    # 剩余配额：从全部章节按对话密度排序填充
    remaining = target - len(picked)
    if remaining > 0:
        all_scored = sorted(
            [(dialogue_ratio(t), ch_no, t) for ch_no, t in chapters if t and ch_no not in picked_nos],
            key=lambda x: -x[0],
        )
        for _, ch_no, text in all_scored[:remaining]:
            picked.append((ch_no, text))
            picked_nos.add(ch_no)

    # 按章节号排序返回
    return sorted(picked, key=lambda x: x[0])


#: 占位符：LLM 直接照抄 prompt 示例（「角色A」「具体角色名」）
_PLACEHOLDER_RE = re.compile(r"^(角色[A-Za-z0-9]|具体角色名)")

#: 高频中文虚词/动词——图谱会把它们误抽成「角色」节点，混进声音词典
_FAKE_CHARS = frozenset({
    # 言说/动作/情绪（常被当成角色名）
    "相信", "喜欢", "冷声", "冷笑", "冷嗤", "冷怒", "厉声", "沉默",
    "摇头", "点头", "惊讶", "愤怒", "高兴", "难过", "紧张", "放松",
    "微笑", "皱眉", "叹气", "哭泣", "大喊", "低声", "转身", "离开",
    "进入", "出现", "消失", "突然", "忽然", "立刻", "马上",
    "解释", "安慰", "怀孕", "越是", "成天", "强势", "全都", "管她",
    "胡乱", "胡说", "明知", "别瞎", "干嘛", "苍白", "满脸", "索性",
    "清楚", "明白", "明显", "上前", "松开", "后悔", "能有", "时光",
    "包括", "别忘", "月光", "连动", "健康", "知道儿",
    "简直", "听言", "充满", "双手", "上去", "幸福", "呼吸",
    "勾起", "越来", "公司", "成为", "薄唇", "尤其", "家里", "别人", "莫名",
    # 泛指 / 集合 / 家族
    "时间", "任何", "所有人", "大家", "众人", "某人", "无人", "许多人", "南家",
    # 抽象名词 / 连词
    "关系", "安全", "能够", "毕竟",
})


def _is_fake_character(name: str) -> bool:
    """占位符 / 高频虚词 / 非人名判定。"""
    name = (name or "").strip()
    if not name or len(name) < 2:
        return True
    if _PLACEHOLDER_RE.match(name):
        return True
    return name in _FAKE_CHARS


def _resolve_name_artifacts(
    pairs: list[tuple[str, int]], text: str, frac: float = 0.15
) -> dict[str, str | None]:
    """用语料词频的**包含关系**识别分词截断产生的伪角色名。

    不依赖词表，语料自证。对每对 A ⊂ B（A 更短）：
    - A 几乎只作为 B 的子串出现（独立出现 ≤ frac）→ A 是截断，归一到 B
      「宫爵」(9984) ⊂ 「南宫爵」(9982)：独立出现 2 次 → 宫爵 ≡ 南宫爵
      「小落」(12937) ⊂ 「安小落」(12145)：独立 792 次(6%) → 小落 → 安小落
    - B 频次远低于 A（≤ frac × A）→ B 是过度切分（名字+相邻字），丢弃
      「冷月清」(5) ⊃ 「冷月」(603) → 丢弃「冷月清」

    返回 {名字: 归一目标名 or None}；None 表示丢弃。未涉及的短名/长名不是 dict 的 key。
    """
    if not text:
        return {}
    counts = {n: text.count(n) for n, _ in pairs if n}
    names = sorted(counts, key=len, reverse=True)
    resolved: dict[str, str | None] = {}
    for long_name in names:
        n_long = counts[long_name]
        if n_long == 0:
            continue
        # 包含在长名里的短名，按频次降序（主导者优先）
        contained = sorted(
            ((counts[s], s) for s in names
             if len(s) < len(long_name) and s in long_name and counts[s] > 0),
            reverse=True,
        )
        for n_short, short_name in contained:
            # 短名独立出现比例过低 → 截断产物，归一到长名
            if n_short - n_long <= frac * n_short:
                resolved.setdefault(short_name, long_name)
            # 长名几乎只由短名支撑 → 过度切分产物（名字+相邻字），归回短名
            if n_long <= frac * n_short:
                resolved.setdefault(long_name, short_name)
    return resolved


#: 不能延续人名的字符：功能字 / 高频动词字 / 标点。
#: 用于「名字+1字」扩展判定——只有排除这些字之后剩下的高频后继字，
#: 才可能是真实姓名的后半截；否则「安小落」+「的」会被误判成名字。
_NAME_CONT_STOP = frozenset(
    "的了是在和就都也不有我你他她它这那一上下里外说着过要会能得地吗呢吧啊呀哦嗯"
    "把被让给对从向与及或者而但因所以然后于其为之又再还只很更最没无到起来去"
    "们个件事点心天时年月日分秒多少大小老新好，。！？、；：“”‘’（）《》…—~ \t\n\r"
    "0123456789abcdefghijklmnopqrstuvwxyz"
)


def _extend_name_candidates(
    names: list[str], text: str, frac: float = 0.15
) -> dict[str, str]:
    """找回被截断的真实姓名：N 的高频后继构成全名 N+c。

    语料自证，不需要词表：
    - 「莫泽」共 1800 次，其中 1781 次其实是「莫泽修」，独立出现仅 1.1% → 截断
    - 「安小落」共 12145 次，后继多为标点/功能字（安小落，/安小落的），
      独立出现 94% → 不是截断，保持原样

    返回 {短名: 全名}。
    """
    if not text:
        return {}
    from collections import Counter

    result: dict[str, str] = {}
    for name in names:
        total = text.count(name)
        if total < 5:
            continue
        # 统计 name 之后紧跟的字符
        nxt: Counter = Counter()
        start = 0
        while True:
            i = text.find(name, start)
            if i < 0:
                break
            j = i + len(name)
            if j < len(text):
                nxt[text[j]] += 1
            start = i + 1
        best, best_count = None, 0
        for ch, cnt in nxt.items():
            if ch in _NAME_CONT_STOP:
                continue
            if cnt > best_count:
                best, best_count = name + ch, cnt
        if best and (total - best_count) <= frac * total:
            result[name] = best
    return result


def _build_character_name_index(
    db, project, nid: int, corpus_text: str = "", min_mentions: int = 10
) -> tuple[list[tuple[str, int]], dict[str, str]]:
    """构建规范角色名索引，返回 (canonical, alias)。

    - canonical: [(规范名, 提及次数)] 按提及次数降序，已剔除伪角色
    - alias: {简称: 规范名}，分词截断的归一映射（「小落」→「安小落」）

    图谱的 character 节点很脏：513 个里既有「时间/任何/相信」这类高频虚词，
    也有「小落/安小/南宫/宫爵」这类分词截断。不清洗的话，角色声音提取会把
    提名额全浪费在伪角色上，LLM 输出的简称也无法归并到全名。
    """
    try:
        from app.models import KgNode

        rows = db.scalars(
            select(KgNode).where(
                KgNode.project_id == project.id,
                KgNode.type == "character",
                KgNode.source == f"novel:{nid}",
            )
        ).all()
    except Exception:  # noqa: BLE001
        return [], {}

    raw: list[tuple[str, int, bool]] = []
    for r in rows:
        props = r.props or {}
        name = props.get("name")
        if not name or len(name) < 2:
            continue
        raw.append((name, props.get("mentions") or 0, props.get("speaking") is True))

    # 候选：会说话的角色（有对白才能提炼声音）∪ 高提及角色（未被标记但戏份重）
    pool = [
        (n, m) for n, m, speaking in raw
        if (speaking or m >= 200) and m >= min_mentions and not _is_fake_character(n)
    ]

    resolved = _resolve_name_artifacts(pool, corpus_text)
    # 后缀扩展：图谱里只有「莫泽/何萱/南尊/唐甜/郑楚」，语料里其实名是「莫泽修/…」
    extended = _extend_name_candidates([n for n, _ in pool], corpus_text)

    # 合并归一映射。优先级：子串判据 > 扩展判据。
    # 子串判据说「这个短名只由更长名字支撑」是更硬的证据，可判「丢弃」；
    # 已经判丢的（如「夏诺柔」⊂「夏诺」）不能再被扩展成「夏诺柔声」这种假名。
    alias: dict[str, str | None] = dict(resolved)
    for name, full in extended.items():
        if name not in alias:
            alias[name] = full

    # 规范名 = 短名替换为全名，丢弃被判为过度切分的名字；
    # 提及次数取组内最大（全名继承了短名的热度，排序才合理）
    merged: dict[str, int] = {}
    for name, mentions in pool:
        target = alias.get(name, name)
        if target is None:
            continue
        merged[target] = max(merged.get(target, 0), mentions)
    kept = sorted(merged.items(), key=lambda p: -p[1])
    return kept, {k: v for k, v in alias.items() if v}


def _normalize_character_name(
    name: str,
    canonical: list[tuple[str, int]] | None = None,
    alias: dict[str, str] | None = None,
) -> str | None:
    """归一化角色名：过滤占位符/伪角色，并把简称映射到规范全名。

    - 占位符/高频虚词（角色A、具体角色名、相信、时间）→ None
    - 「小落」→「安小落」、「宫爵」→「南宫爵」
      优先用 alias（语料词频自证的截断映射），其次退回子串匹配
    """
    name = (name or "").strip()
    if _is_fake_character(name):
        return None

    # 1) 词频自证的截断映射（最可靠）
    if alias and name in alias:
        return alias[name]

    # 2) 已是规范名 → 原样返回
    if canonical:
        if name in {cn for cn, _ in canonical}:
            return name
        # 3) 退回子串匹配：后缀优先（中文常取后几字作简称），再取最短
        candidates = [
            cn for cn, _ in canonical
            if len(cn) > len(name) and name in cn
        ]
        if candidates:
            suffix = [c for c in candidates if c.endswith(name)]
            return min(suffix or candidates, key=len)
    return name


def _dedup_character_voices(
    voices: list[dict],
    canonical: list[tuple[str, int]] | None = None,
    alias: dict[str, str] | None = None,
) -> list[dict]:
    """角色声音去重 + 角色名归一化。

    处理两类问题：
    1. 占位符/伪角色（角色A、时间、相信）→ 丢弃
    2. 同一角色的不同称呼（安小落/小落、南宫爵/宫爵）→ 合并到规范全名
       （alias 给语料自证的截断映射，canonical 给规范名列表）
    """
    # 第一轮：过滤占位符 + 简称归一化
    valid = []
    for v in voices:
        rule = v.get("rule", "")
        # 提取角色名（格式：「角色名：...」）
        if "：" not in rule:
            continue
        char_name = rule.split("：")[0].strip()
        normalized = _normalize_character_name(char_name, canonical, alias)
        if normalized is None:
            continue
        # 替换归一化后的名字
        v = dict(v)
        v["rule"] = normalized + "：" + rule.split("：", 1)[1]
        valid.append(v)

    # 第二轮：合并同一角色的不同称呼
    # 先按角色名分组（同名保留信息最丰富的）
    by_name: dict[str, dict] = {}
    for v in valid:
        rule = v.get("rule", "")
        char_name = rule.split("：")[0].strip()
        if char_name in by_name:
            if len(rule) > len(by_name[char_name].get("rule", "")):
                by_name[char_name] = v
        else:
            by_name[char_name] = v
    # 再做子串合并：如果 A ⊂ B，合并到 B
    # 按名字长度降序处理，确保长名字先入
    merged: dict[str, dict] = {}
    for char_name in sorted(by_name.keys(), key=len, reverse=True):
        v = by_name[char_name]
        # 检查是否已被更长名字包含
        canonical = char_name
        for existing in merged.keys():
            if char_name != existing and char_name in existing:
                canonical = existing
                break
        if canonical in merged:
            # 保留信息更丰富的
            existing_rule = merged[canonical].get("rule", "")
            new_rule = v.get("rule", "")
            if len(new_rule) > len(existing_rule):
                merged[canonical] = v
        else:
            merged[canonical] = v
    return list(merged.values())


def _build_beat_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造节拍序列提取的 prompt。

    提炼「一场典型冲突的 5~7 步节拍」——从套路名到具体写法。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中提炼**一场典型冲突/高潮的节拍序列**。\n\n"
        "不是总结剧情，而是提炼**作者怎么写一场冲突的结构**。例如：\n"
        "- 铺垫：用什么细节暗示冲突即将爆发\n"
        "- 升温：如何逐步加压（对话变短/动作变急/环境变压抑）\n"
        "- 爆发：冲突顶点如何处理（谁说最后一句话/什么动作打断）\n"
        "- 余波：爆发后如何收束（沉默/转身离去/一句冷话）\n"
        "- 新钩子：如何引出下一场冲突\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "节拍序列": [\n'
        '    {"步骤": "铺垫", "写法": "具体怎么写（证据：原文引用）"},\n'
        '    {"步骤": "升温", "写法": "具体怎么写（证据：原文引用）"},\n'
        '    {"步骤": "爆发", "写法": "具体怎么写（证据：原文引用）"},\n'
        '    {"步骤": "余波", "写法": "具体怎么写（证据：原文引用）"},\n'
        '    {"步骤": "新钩子", "写法": "具体怎么写（证据：原文引用）"}\n'
        '  ]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼一场典型冲突的节拍序列：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。每步必须附原文证据。"
    )
    return system, user


def _build_scene_template_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造高频场景模板提取的 prompt。

    识别反复出现的场景类型，提炼环境细节、人物站位、典型动作。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中提炼**高频场景的写作模板**。\n\n"
        "识别文中反复出现的场景类型（如病房/宴会/办公室对峙/车内独处/手术室门口），"
        "每个场景提炼：\n"
        "1. 环境细节（光线/声音/气味/温度）\n"
        "2. 人物站位（谁站着/谁坐着/距离多远）\n"
        "3. 典型动作链（进入→对峙→动作→离场）\n"
        "4. 情绪基调（压抑/紧张/暧昧/冰冷）\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "场景模板": [\n'
        '    {"场景": "病房", "环境": "...", "站位": "...", "动作链": "...", "基调": "..."},\n'
        '    {"场景": "宴会", "环境": "...", "站位": "...", "动作链": "...", "基调": "..."}\n'
        '  ]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼高频场景模板：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。每个场景必须附原文中的具体细节。"
    )
    return system, user


def _build_subtext_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造潜台词模式提取的 prompt。

    提炼「角色如何用反话/转移话题/沉默表达真实意图」。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中提炼**对白潜台词的写法**。\n\n"
        "分析角色如何用间接方式表达真实意图：\n"
        "1. 反话/正话反说（说「没事」其实有事）\n"
        "2. 转移话题（用问另一个问题回避）\n"
        "3. 沉默/停顿（不说话比说话更有力）\n"
        "4. 借题发挥（借他人之事说自己）\n"
        "5. 命令式关心（用命令语气表达担心）\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "潜台词模式": [\n'
        '    {"模式": "反话正说", "写法": "具体怎么写（证据：原文引用）"},\n'
        '    {"模式": "沉默施压", "写法": "具体怎么写（证据：原文引用）"}\n'
        '  ]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼对白潜台词模式：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。每个模式必须附原文证据。"
    )
    return system, user


def _build_character_voice_prompt(
    chunk_text: str, character_names: list[str], chunk_label: str
) -> tuple[str, str]:
    """构造角色声音提取的 prompt。

    针对已知的高频角色名，分析每个角色的说话风格 + 关系动态。
    """
    names_str = "、".join(character_names)
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中分析**特定角色的说话风格**。\n\n"
        "对每个角色，分析：\n"
        "1. 自称/称呼习惯（如「我」「本座」「老子」「人家」）\n"
        "2. 口头禅/高频用语（列出 2-3 个具体词句）\n"
        "3. 句长偏好（短句干脆/长句绵密/混合）\n"
        "4. 语气特征（命令式/试探式/撒娇式/冷淡式/嘲讽式...）\n"
        "5. 对白节奏（抢话/停顿/欲言又止/一口气说完）\n"
        "6. **关系动态**：对不同人说话方式的变化（如「对A温柔，对B冷淡」）\n"
        "7. **代表性台词**：摘录 1-2 句最能体现该角色声音的对白原文\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        f'  "{character_names[0]}": {{\n'
        '    "自称": "...",\n'
        '    "口头禅": ["词1", "词2"],\n'
        '    "句长": "短句/长句/混合",\n'
        '    "语气": "...",\n'
        '    "节奏": "...",\n'
        '    "关系动态": "对X如何，对Y如何",\n'
        '    "代表台词": ["台词1", "台词2"]\n'
        '  },\n'
        f'  "{character_names[1] if len(character_names) > 1 else "其他角色"}": {{...}}\n'
        '}\n\n'
        "**重要**：JSON 的 key 必须是上面列出的真实角色名，不要用「角色A」「角色B」等占位符。"
    )
    user = (
        f"以下是《{chunk_label}》的片段，请分析以下角色的说话风格：{names_str}\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。只分析列出的角色，不要分析其他角色。"
        "每个角色必须包含关系动态和代表台词。"
    )
    return system, user


def _find_character_chapters(
    chapters: list[tuple[int, str]], char_name: str, max_chapters: int = 8
) -> list[tuple[int, str]]:
    """从全文章节里找出该角色出场最多的章节。

    不是用统一的采样章节，而是针对每个角色检索——
    配角可能只在特定章节大量出场，统一采样会漏掉。
    """
    scored = []
    for ch_no, text in chapters:
        count = text.count(char_name)
        if count >= 2:  # 至少出现 2 次（配角跨章累计出场多，单章可能只有 2 次）
            scored.append((count, ch_no, text))
    scored.sort(key=lambda x: -x[0])
    return [(ch_no, text) for _, ch_no, text in scored[:max_chapters]]


def _build_voice_rule(char_name: str, voice: dict) -> str | None:
    """从 LLM 返回的 voice dict 构建规则字符串。"""
    parts = [f"{char_name}："]
    if voice.get("自称") and voice["自称"] != "无":
        parts.append(f"自称「{voice['自称']}」")
    if voice.get("口头禅"):
        habits = voice["口头禅"]
        if isinstance(habits, list):
            valid = [h for h in habits if h and h != "无"]
            if valid:
                parts.append(f"口头禅「{'、'.join(str(h) for h in valid[:3])}」")
        elif habits != "无":
            parts.append(f"口头禅「{habits}」")
    if voice.get("句长") and voice["句长"] != "无":
        parts.append(f"句长{voice['句长']}")
    if voice.get("语气") and voice["语气"] != "无":
        parts.append(f"语气{voice['语气']}")
    if voice.get("节奏") and voice["节奏"] != "无":
        parts.append(f"节奏{voice['节奏']}")
    if voice.get("关系动态") and voice["关系动态"] != "无":
        parts.append(f"关系动态：{voice['关系动态']}")
    if voice.get("代表台词"):
        lines = voice["代表台词"]
        if isinstance(lines, list):
            valid = [l for l in lines if l and l != "无"]
            if valid:
                parts.append(f"代表台词：「{'」「'.join(str(l)[:25] for l in valid[:2])}」")
    if len(parts) <= 1:
        return None
    return " ".join(parts)


def _find_voice_for_char(parsed: dict, char_name: str) -> dict | None:
    """从 LLM 返回的 dict 里找角色声音。

    LLM 可能用简称（「宫爵」而非「南宫爵」）作为 key。
    先精确匹配，再模糊匹配（char_name 包含 key 或 key 包含 char_name）。
    """
    voice = parsed.get(char_name)
    if isinstance(voice, dict):
        return voice
    # 模糊匹配
    for key, val in parsed.items():
        if not isinstance(val, dict):
            continue
        if char_name in key or key in char_name:
            return val
    return None


def _extract_character_voices(
    provider, db, project, nid: int, chapters: list[tuple[int, str]],
    char_names: list[str] | None = None,
) -> list[dict]:
    """混合方案：主要角色逐个提取，配角分批提取。

    - 前 VOICE_MAIN_CHARACTERS 个高频角色：逐个提取（专属章节，深度分析）
    - 剩余角色：分批提取（每批 VOICE_BATCH_SIZE 个，混合章节）
    - 总共约 8 + 3 = 11 次调用

    char_names 由 _build_character_name_index 清洗后给出（已剔除伪角色与
    分词截断的简称）——直接用图谱原始顺序会把提名额浪费在「时间/任何/冷声」
    这类伪角色上，且提取出的简称无法归并到全名。
    """
    if char_names is None:
        char_names, _ = _build_character_name_index(db, project, nid)
        char_names = [n for n, _ in char_names]
    char_names = [
        n for n in char_names if n and len(n) >= 2
    ][:MAX_VOICE_CHARACTERS]

    if not char_names or not chapters:
        return []

    results: list[dict] = []
    main_chars = char_names[:VOICE_MAIN_CHARACTERS]
    side_chars = char_names[VOICE_MAIN_CHARACTERS:]

    # —— 主要角色：逐个提取 ——
    for char_name in main_chars:
        char_chapters = _find_character_chapters(chapters, char_name)
        if not char_chapters:
            continue
        voice_text = "\n\n".join(text for _, text in char_chapters)[:6000]
        if not voice_text:
            continue
        try:
            system, user = _build_character_voice_prompt(
                voice_text, [char_name], f"语料{nid}"
            )
            raw = provider.chat(system, user)
            parsed = _parse_extract_result(raw)
            if not isinstance(parsed, dict):
                continue
            voice = _find_voice_for_char(parsed, char_name)
            if not isinstance(voice, dict):
                continue
            rule = _build_voice_rule(char_name, voice)
            if rule:
                results.append({
                    "rule": rule,
                    "novel_id": nid,
                    "chapter_no": char_chapters[0][0],
                })
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "角色声音提取失败 %s: %s", char_name, exc
            )

    # —— 配角：分批提取 ——
    for i in range(0, len(side_chars), VOICE_BATCH_SIZE):
        batch = side_chars[i : i + VOICE_BATCH_SIZE]
        # 收集这批角色的出场章节
        batch_texts = []
        for char_name in batch:
            char_chapters = _find_character_chapters(chapters, char_name, max_chapters=3)
            if char_chapters:
                batch_texts.extend(text for _, text in char_chapters)
        if not batch_texts:
            continue
        voice_text = "\n\n".join(batch_texts)[:12000]
        try:
            system, user = _build_character_voice_prompt(
                voice_text, batch, f"语料{nid}"
            )
            raw = provider.chat(system, user)
            parsed = _parse_extract_result(raw)
            if not isinstance(parsed, dict):
                continue
            for char_name in batch:
                voice = _find_voice_for_char(parsed, char_name)
                if not isinstance(voice, dict):
                    continue
                rule = _build_voice_rule(char_name, voice)
                if rule:
                    results.append({
                        "rule": rule,
                        "novel_id": nid,
                        "chapter_no": 0,
                    })
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "配角声音批次提取失败 batch=%s: %s", batch, exc
            )

    return results


def _build_drift_prompt(chunks_info: list[tuple[str, str]], chunk_label: str) -> tuple[str, str]:
    """构造文风漂移对比的 prompt。

    传入多个时间段的文本片段，让 LLM 对比文风变化。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是对比**同一作者在不同时期的文风差异**。\n\n"
        "对比维度：\n"
        "1. 句长节奏变化（早期 vs 后期）\n"
        "2. 对白密度变化\n"
        "3. 描写细腻度变化\n"
        "4. 叙事结构复杂度变化\n"
        "5. 用词偏好变化\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "漂移总结": "整体趋势描述",\n'
        '  "早期特点": ["特点1", "特点2"],\n'
        '  "后期特点": ["特点1", "特点2"],\n'
        '  "最大变化": "最显著的变化维度"\n'
        '}\n'
    )
    parts = []
    for period_name, text in chunks_info:
        parts.append(f"【{period_name}】\n{text[:2000]}")
    user = (
        f"以下是《{chunk_label}》在不同时期的片段：\n\n"
        + "\n\n---\n\n".join(parts)
        + "\n\n请按上述维度对比输出 JSON。"
    )
    return system, user


def extract_narrative_profile(
    db: Session,
    project,
    novel_ids: list[int],
    provider=None,
    max_chapters: int = 180,  # v6: 默认 30% × 600 章 ≈ 180
) -> dict:
    """从参考语料提炼叙事档案。

    返回结构（可直接 JSON 落库到 `Project.style_narrative`）：
    {
      "version": 1,
      "source_ids": [...],
      "narrative_version": 1,
      "n_chapters": 30,
      "rules": {
        "人物描写手法": [
          {"rule": "规则文本（证据：原文句子）", "novel_id": 1, "chapter_no": 5},
          ...
        ],
        ...
      },
      "rejected": 3,  # 因证据不通过被拒的规则数
    }
    """
    from app.llm.provider import get_llm_provider

    if provider is None:
        provider = get_llm_provider()

    all_rules: dict[str, list[dict]] = {
        dim: [] for dim in NARRATIVE_DIMENSIONS + ["文风漂移分析"]
    }
    all_forbidden: dict[str, list[str]] = {dim: [] for dim in FORBIDDEN_DIMENSIONS}
    rejected = 0
    total_chapters = 0

    for nid in novel_ids:
        chapters = _load_novel_chapters(db, nid)
        if not chapters:
            continue

        # v6：按百分比采样 + 对话密集优先 + 场景均衡
        sampled = _sample_chapters_v6(chapters, ratio=SAMPLE_RATIO)
        total_chapters += len(sampled)

        # 分块：每块约 20 章（180 章 / 3 块 = 60 章/块，但限制块大小防止 prompt 过长）
        chunk_size = max(MIN_CHAPTERS_PER_CHUNK, min(len(sampled) // 3, 20))
        for i in range(0, len(sampled), chunk_size):
            chunk = sampled[i : i + chunk_size]
            chunk_text = "\n\n".join(text for _, text in chunk if text)
            if not chunk_text:
                continue

            chapter_range = f"{chunk[0][0]}-{chunk[-1][0]}"
            system, user = _build_extract_prompt(chunk_text, f"语料{nid} 第{chapter_range}章")

            try:
                raw = provider.chat(system, user)
            except Exception as exc:  # noqa: BLE001 —— 单块失败不阻断其余块
                logging.getLogger(__name__).warning(
                    "叙事提炼失败 novel_id=%s chunk=%s: %s", nid, chapter_range, exc
                )
                continue

            parsed = _parse_extract_result(raw)
            if not isinstance(parsed, dict):
                continue

            for dim in NARRATIVE_DIMENSIONS:
                items = parsed.get(dim)
                if not isinstance(items, list):
                    continue
                for rule in items:
                    if not isinstance(rule, str) or not rule.strip():
                        continue
                    # 证据验证：伪规则在这里被拦掉
                    if not _validate_evidence(rule, chunk_text):
                        rejected += 1
                        continue
                    # 逐字复制检测：LLM 直接复制原文句子而非抽象规则，信息量极低
                    if _is_verbatim_copy(rule, chunk_text):
                        rejected += 1
                        continue
                    all_rules[dim].append({
                        "rule": rule.strip(),
                        "novel_id": nid,
                        "chapter_no": chunk[0][0],
                    })

            # —— 反向禁忌提取（同块，追加一次 LLM 调用）——
            # 只对第一块提取（成本考虑），禁忌模式在全书范围内通用
            if i == 0:
                f_system, f_user = _build_forbidden_prompt(chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章")
                try:
                    f_raw = provider.chat(f_system, f_user)
                    f_parsed = _parse_extract_result(f_raw)
                    if isinstance(f_parsed, dict):
                        for dim in FORBIDDEN_DIMENSIONS:
                            items = f_parsed.get(dim)
                            if not isinstance(items, list):
                                continue
                            for item in items:
                                if not isinstance(item, str) or not item.strip():
                                    continue
                                # 反向禁忌不需要证据验证——它是「不存在」的模式
                                all_forbidden[dim].append(item.strip())
                except Exception as exc:  # noqa: BLE001
                    logging.getLogger(__name__).warning(
                        "反向禁忌提取失败 novel_id=%s: %s", nid, exc
                    )

            # —— v6：节拍序列提取（每块都提取，提炼不同类型的冲突节拍）——
            try:
                b_system, b_user = _build_beat_prompt(chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章")
                b_raw = provider.chat(b_system, b_user)
                b_parsed = _parse_extract_result(b_raw)
                if isinstance(b_parsed, dict):
                    beats = b_parsed.get("节拍序列") or []
                    for beat in beats:
                        if isinstance(beat, dict) and beat.get("步骤") and beat.get("写法"):
                            rule = f"{beat['步骤']}：{beat['写法']}"
                            if not _is_verbatim_copy(rule, chunk_text):
                                all_rules["节拍序列"].append({
                                    "rule": rule,
                                    "novel_id": nid,
                                    "chapter_no": chunk[0][0],
                                })
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning("节拍序列提取失败: %s", exc)

            # —— v5：高频场景模板提取（只对第一块）——
            try:
                s_system, s_user = _build_scene_template_prompt(chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章")
                s_raw = provider.chat(s_system, s_user)
                s_parsed = _parse_extract_result(s_raw)
                if isinstance(s_parsed, dict):
                    scenes = s_parsed.get("场景模板") or []
                    for scene in scenes:
                        if isinstance(scene, dict) and scene.get("场景"):
                            rule = (
                                f"{scene['场景']}：环境={scene.get('环境','')} "
                                f"站位={scene.get('站位','')} "
                                f"动作链={scene.get('动作链','')} "
                                f"基调={scene.get('基调','')}"
                            )
                            all_rules["高频场景模板"].append({
                                "rule": rule,
                                "novel_id": nid,
                                "chapter_no": chunk[0][0],
                            })
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning("场景模板提取失败: %s", exc)

            # —— v5：潜台词模式提取（只对第一块）——
            try:
                st_system, st_user = _build_subtext_prompt(chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章")
                st_raw = provider.chat(st_system, st_user)
                st_parsed = _parse_extract_result(st_raw)
                if isinstance(st_parsed, dict):
                    patterns = st_parsed.get("潜台词模式") or []
                    for pat in patterns:
                        if isinstance(pat, dict) and pat.get("模式") and pat.get("写法"):
                            rule = f"{pat['模式']}：{pat['写法']}"
                            if not _is_verbatim_copy(rule, chunk_text):
                                all_rules["潜台词模式"].append({
                                    "rule": rule,
                                    "novel_id": nid,
                                    "chapter_no": chunk[0][0],
                                })
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning("潜台词提取失败: %s", exc)

        # —— v5：角色声音提取（针对性：每个角色检索出场最多的章节）——
        try:
            # v6.6：从图谱取规范角色名 + 语料自证的简称映射。
            # 图谱的 character 节点含大量高频虚词（时间/任何/相信）与分词截断
            # （小落/安小/宫爵/南宫），必须用语料词频清洗后才能当作规范名。
            corpus_text = "\n".join(t for _, t in chapters if t)
            canonical, alias = _build_character_name_index(
                db, project, nid, corpus_text
            )
            voice_results = _extract_character_voices(
                provider, db, project, nid, chapters,
                char_names=[n for n, _ in canonical],
            )
            # 去重 + 角色名归一化（小落→安小落、宫爵→南宫爵、过滤角色A/时间）
            voice_results = _dedup_character_voices(voice_results, canonical, alias)
            all_rules[CHARACTER_VOICE_DIMENSION].extend(voice_results)
            # 对整个角色声音词典做最终去重：批量路径（NARRATIVE_DIMENSIONS 循环）
            # 也会产出「小落：...」这类简称记录，必须在这里再归一化一次才能合并
            all_rules[CHARACTER_VOICE_DIMENSION] = _dedup_character_voices(
                all_rules[CHARACTER_VOICE_DIMENSION], canonical, alias
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "角色声音提取失败 novel_id=%s: %s", nid, exc
            )

        # —— v3：文风漂移分析（第三轮，对比早晚期）——
        try:
            if len(chapters) >= 20:
                early = chapters[:5]
                late = chapters[-5:]
                early_text = "\n\n".join(t for _, t in early if t)[:2000]
                late_text = "\n\n".join(t for _, t in late if t)[:2000]
                drift_system, drift_user = _build_drift_prompt(
                    [("早期（前5章）", early_text), ("晚期（后5章）", late_text)],
                    f"语料{nid}",
                )
                drift_raw = provider.chat(drift_system, drift_user)
                drift_parsed = _parse_extract_result(drift_raw)
                if isinstance(drift_parsed, dict):
                    # 容错：key 名可能不一致（LLM 输出格式不稳定）
                    summary = (
                        drift_parsed.get("漂移总结")
                        or drift_parsed.get("整体趋势")
                        or drift_parsed.get("总结")
                        or drift_parsed.get("变化总结")
                    )
                    if summary:
                        early_feats = (
                            drift_parsed.get("早期特点")
                            or drift_parsed.get("早期")
                            or []
                        )
                        late_feats = (
                            drift_parsed.get("后期特点")
                            or drift_parsed.get("晚期")
                            or []
                        )
                        max_change = (
                            drift_parsed.get("最大变化")
                            or drift_parsed.get("最显著的变化")
                            or ""
                        )
                        if isinstance(early_feats, list):
                            early_str = "、".join(str(x) for x in early_feats[:5])
                        else:
                            early_str = str(early_feats)[:200]
                        if isinstance(late_feats, list):
                            late_str = "、".join(str(x) for x in late_feats[:5])
                        else:
                            late_str = str(late_feats)[:200]
                        all_rules["文风漂移分析"] = [{
                            "rule": (
                                f"整体趋势：{summary}；"
                                f"早期：{early_str}；"
                                f"晚期：{late_str}；"
                                f"最大变化：{max_change}"
                            ),
                            "novel_id": nid,
                            "chapter_no": 0,
                    }]
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "文风漂移分析失败 novel_id=%s: %s", nid, exc
            )

    return {
        "version": NARRATIVE_VERSION,
        "source_ids": sorted(int(i) for i in novel_ids),
        "narrative_version": NARRATIVE_VERSION,
        "n_chapters": total_chapters,
        "rules": all_rules,
        "forbidden": all_forbidden,
        "rejected": rejected,
    }


def narrative_fingerprint(source_ids: list[int]) -> dict:
    """叙事档案的失效判据：语料集合 + 提炼版本号。"""
    return {
        "source_ids": sorted(int(i) for i in source_ids),
        "narrative_version": NARRATIVE_VERSION,
    }


def project_narrative(
    db: Session,
    project,
    *,
    force: bool = False,
    max_chapters: int = 180,  # v6: 默认 30% × 600 章 ≈ 180
) -> dict | None:
    """取项目当前该用的叙事档案；没有参考语料则返回 None。

    已存且指纹一致就直接返回——与 `style_profile` 同理，叙事档案进的是静态 system 块，
    每次生成都变会击穿提示词缓存。
    """
    ids = [int(i) for i in (project.reference_novel_ids or [])]
    if not ids:
        return None

    want = narrative_fingerprint(ids)
    cached = project.style_narrative or {}
    if not force and isinstance(cached, dict) and cached.get("fingerprint") == want:
        return cached

    profile = extract_narrative_profile(db, project, ids, max_chapters=max_chapters)
    profile["fingerprint"] = want
    project.style_narrative = profile
    db.commit()
    return profile


def project_narrative_safe(
    db: Session,
    project,
    *,
    force: bool = False,
    max_chapters: int = 180,  # v6: 默认 30% × 600 章 ≈ 180
) -> dict | None:
    """生成链路上的安全包装。与 `style_profile_safe` 同理：失败只能降级为「不注入」，
    绝不能让整次生成失败。
    """
    try:
        return project_narrative(db, project, force=force, max_chapters=max_chapters)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "叙事档案不可用，本次生成跳过叙事档案块：%s: %s", type(exc).__name__, exc
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def describe_narrative(profile: dict | None, max_rules_per_dim: int = 3) -> str:
    """把叙事档案翻成一段确定性文字，用于 system prompt 的静态【叙事档案】块。

    只取每个维度的前 max_rules_per_dim 条规则，防止 system prompt 膨胀。
    确定性意味着：同一份档案永远产出同一段文本，不随段号变化。
    """
    if not profile:
        return ""
    rules = profile.get("rules") or {}
    forbidden = profile.get("forbidden") or {}
    if not rules and not forbidden:
        return ""

    lines: list[str] = []
    all_dims = NARRATIVE_DIMENSIONS + ["文风漂移分析"]
    for dim in all_dims:
        items = rules.get(dim) or []
        if not items:
            continue
        # 角色声音词典：每个角色一条，不截断（角色多才有效）
        limit = 20 if dim == CHARACTER_VOICE_DIMENSION else max_rules_per_dim
        lines.append(f"【{dim}】")
        for item in items[:limit]:
            if isinstance(item, dict):
                lines.append(f"  · {item.get('rule', '')}")
            else:
                lines.append(f"  · {item}")

    # 反向禁忌：具体的「不要用 X」比通用的「忌 AI 腔」更有效
    forbidden_lines: list[str] = []
    for dim in FORBIDDEN_DIMENSIONS:
        items = forbidden.get(dim) or []
        if not items:
            continue
        # 去重保序
        seen = set()
        unique = []
        for x in items:
            if x not in seen:
                seen.add(x)
                unique.append(x)
        forbidden_lines.append(f"【{dim}】禁止使用：{'、'.join(unique[:8])}")

    if not lines and not forbidden_lines:
        return ""
    parts = []
    if lines:
        parts.append("【叙事档案：从参考语料提炼，生成时请遵循以下叙事手法】\n" + "\n".join(lines))
    if forbidden_lines:
        parts.append("【反向禁忌：参考作者从不使用以下表达，严禁出现在生成文本中】\n" + "\n".join(forbidden_lines))
    return "\n".join(parts)
