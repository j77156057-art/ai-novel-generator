"""v2.6 诊断性 B：检测白名单外的角色名（只告警，不替换）。

设计动机：
- 模型正常创作时会引入"小角色"（出场几章就消失），这些**应该**被允许
- 项目初期的"核心角色"白名单**不**可能覆盖所有角色——误伤正常创作
- 真正要拦截的是"明显走样"（如 few-shot 风格样例诱导把红楼梦薛姨妈混入现代言情）
- 这种"走样"的根因在 few-shot，**不在**白名单

结论：白名单"诊断性"——只检测+告警，**不替换**。正文保持原样。
对模型生成零影响（告警是生成后的旁路读，不反馈给模型）。

白名单收集：见 collect_allowed_names（项目锁定角色 + 剧情圣经角色）
"""
from __future__ import annotations

import re
from typing import Iterable


def _normalize_name(name: str) -> str:
    """归一化：去空白，保留原字面。

    角色名匹配用字面（不转拼音/不模糊）——避免"林"单独匹配这种误伤。
    """
    return name.strip()


def detect_unlisted_characters(
    text: str,
    allowed_names: Iterable[str],
) -> list[str]:
    """v2.6 诊断性 B：扫正文，**只返回**白名单外的角色名列表——不替换。

    严格"白名单外=可疑"——返回值供 logger / API 诊断用。
    调用方（engine.py）负责：logger.warning + 把列表塞进 return dict。
    正文**始终**原样返回（v2.6 这条只观测，不纠错）。

    启发式：**只在「说话人归属」位置取候选**——`某人道：“…”`、`某人说：`
    或 `“…。”某人说。`。角色名只有出现在这种位置时才被当作"疑似角色"，
    因此不再把正文里任意 2-4 字片段（「被照得睡」「看了一眼」）当人名报。

    为什么收窄（2026-11-23）：旧版扫全篇 2-4 字中文串，第 4 章报了 566 条
    假阳性（全是动作/叙述碎片），告警完全不可读。诊断的目标是「谁在说话、
    但不在白名单里」，说话人归属就是最直接、最可判定的信号。

    代价：只在动作里出现（不说话）的新角色不会被报出来——诊断性功能接受
    这个漏报，换来可读性；真要全量 NER 需引入 jieba。正文**始终**原样返回。

    Args:
        text: LLM 生成的正文
        allowed_names: 白名单（项目锁定角色 + 剧情圣经角色）

    Returns:
        排序去重的"未授权候选角色名"列表（按长度倒序，长名优先）

    设计取舍：
    - 不真删——避免破坏剧情连贯（误伤让正文变难看）
    - 大小写敏感——中文角色名用字面匹配
    - 短名（≤1 字）不过滤（误伤高）
    - allowed_names 为空时返回 []（无"白名单"概念，保守不报）
    """
    allowed_set: set[str] = set()
    for n in allowed_names:
        n = _normalize_name(n)
        if n and len(n) > 1:
            allowed_set.add(n)

    if not allowed_set or not text:
        return []

    # 候选：只取「说话人归属」位置的名字（人名 2-4 字）。
    # 用非贪婪窗口 + 长动词优先，避免把「钱多多笑道」切成「钱多多笑」。
    candidates: set[str] = set()
    for regex in (_SPEECH_BEFORE_RE, _SPEECH_AFTER_RE):
        for m in regex.finditer(text):
            candidates.add(m.group(1))

    # 启发式过滤：候选里"明显不是人名"的扔掉
    # 1. 停用词（人名几乎不会用这些字开头/结尾）
    # 2. 常见词（动词/名词/形容词集中的位置）
    candidates = [c for c in candidates if _looks_like_name(c)]

    # 未授权：白名单外
    unlisted: set[str] = set()
    for name in candidates:
        if name not in allowed_set:
            unlisted.add(name)

    # 按长度倒序（长名先报——更可能是真角色名）
    return sorted(unlisted, key=lambda s: -len(s))


# 停用词表：人名几乎不会用这些字开头
# 来自实际项目观察：薛姨妈/林悠然/顾辰逸/王夫人/李老板——常见姓氏+称谓
# "咖啡""林夫人"等是噪声
_NAME_STOPWORDS_HEAD = set(
    "的了一是就在有我你他她它我们你"
    "什么怎么哪里那个这个那些这些"
    "可以能够应该必须会要"
    "公司部门经理工作项目业务"
    "因为所以但是虽然然后接着"
    "咖啡花园住宅公寓办公室"
)
# 常见称谓后缀（出现就认定为称谓，不是人名主体）
# 注意：不要包含 "夫"/"人"/"女" 等——它们是常见人名用字（"王夫人""林夫人""男人"）。
# 包含 "夫" 会误伤"王夫人"等真实角色名。
_NAME_STOPWORDS_TAIL = set(
    "先生女士老师同学朋友"
    "总编经理"
)

# 单字常见姓氏（人名可能以这些字开头，但不是"完整人名"）
_COMMON_SURNAMES = set("李王张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾萧田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤")
# 单字常见人名用字
_COMMON_GIVEN_CHARS = set("伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超秀英霞刚平桂英建华俊宇浩然子轩")

#: 说话人归属（名字在引语前）：`某人道：“…”` / `某人说:`。
#: 非贪婪 2-4 字 + 长动词优先，避免把「钱多多笑道」切成「钱多多笑」。
_SPEECH_BEFORE_RE = re.compile(
    r"([\u4e00-\u9fff]{2,4}?)"
    r"(?:冷笑道|低声说|沉声道|开口道|接着说|问道|答道|笑道|说道|道|说|问|答|喊)"
    r"[：:]?\s*[“\"「『]"
)
#: 引语在前、归属在后：`“…”某人说。`
_SPEECH_AFTER_RE = re.compile(
    r"[”\"」』]\s*([\u4e00-\u9fff]{2,4}?)"
    r"(?:冷笑道|低声说|沉声道|问道|答道|笑道|说道|道|说|问|答)"
)


def _looks_like_name(s: str) -> bool:
    """启发式：候选字符串 s 是否"可能是人名"。

    接受：
    - 含 2-3 字的中文组合
    - 不在停用词头/尾里
    - 不是"明显"的普通名词（无停用词头）

    拒绝：
    - 头是停用词（"的了"开头 = 助词，不可能是名字）
    - 尾是称谓（"先生"结尾 = 称谓，不是名字主体）
    - 全部是单字姓氏组成（"李王" = 列举姓氏）

    注意：这只是启发式，必然有假阳性（接受"咖啡馆见"）+ 假阴性（漏掉"老狐狸"）。
    终极方案是 NER。
    """
    if not s or len(s) < 2:
        return False
    head = s[0]
    tail = s[-1]
    if head in _NAME_STOPWORDS_HEAD:
        return False
    if tail in _NAME_STOPWORDS_TAIL:
        return False
    # 全部是常见姓氏（"李王张" = 列举，不算人名）
    if all(c in _COMMON_SURNAMES for c in s):
        return False
    return True


def collect_allowed_names(
    ctx_reminders: dict | None = None,
    plot_bible: dict | None = None,
    node_prompt: str | None = None,
) -> list[str]:
    """v2.6 诊断性 B：收集所有"允许"的角色名（白名单源）。

    三个来源：
    1. ctx_reminders["characters"] —— 项目锁定角色（build_context 返回的）
    2. plot_bible["characters"] —— 剧情圣经里的角色（多字典形态）
    3. node_prompt 字符串里出现的名字（粗略，从 entry/exit 文字扫描）

    返回去重后的名字列表（保持原顺序）。
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(name: str) -> None:
        n = _normalize_name(name)
        if n and n not in seen and len(n) > 1:
            seen.add(n)
            out.append(n)

    # 来源 1：ctx_reminders["characters"]
    if ctx_reminders:
        chars = ctx_reminders.get("characters") or []
        if isinstance(chars, list):
            for c in chars:
                if isinstance(c, str):
                    _add(c)
                elif isinstance(c, dict):
                    # 形态 {name, mentions, ...} 或 {canonical, alias, ...}
                    _add(c.get("name") or c.get("canonical") or "")

    # 来源 2：plot_bible["characters"]（多种形态：list[str] / list[dict]）
    if plot_bible:
        chars = plot_bible.get("characters") or []
        if isinstance(chars, list):
            for c in chars:
                if isinstance(c, str):
                    _add(c)
                elif isinstance(c, dict):
                    _add(c.get("name") or "")

    return out
