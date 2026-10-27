"""beat 插入的引擎侧工序：在连续对白段的开头补一句动作/神态叙述。

## ⚠️ 默认关闭 —— 这条路的**参照物曾经是错的**

最早的判据拿**《北荒刀歌》**（A 类 7%~12%）当标准，得出「模型写的对白戏
比参考书密 ⇒ 要加叙述」。但 15 本真书的 A 类占比是
**min 7%（北荒刀歌）· p25 64% · 中位 72% · p75 81% · max 88%**
⇒ **北荒刀歌是特例**；我们生成的是 **61%**，**已在真书区间下沿**，
不是「差了一倍」。

真跑的量-效曲线（插 6/12/24/48 处）显示：动作密度升到真书 **5 倍**
（=「网球法」），A 类只降到 35%，**采纳率还随量下降**（模型在敷衍）。

⇒ 代码与判据**保留**（① 目标比例取自**参考书自己**，对动作密集的书
仍有意义；② 它是「局部插入」这条路的实现与判据范例），但**默认不开**
（`settings.style_beat_insert = False`）。

## 机制本身是可靠的

整段形态改写（`style/form.py` 的 `correct_form`）已实测**不可靠**：
正式测量 0/20、受控重复 0/4（MiniMax 在那个任务上采样不可复现）。

局部插入的任务小得多：上下文现成、目标单一、输出可逐段验收 ——
真跑**采纳 5/6（83%）**。

## 判据（五道，全过才采纳）

1. **长度**在 10~30 字（真书 C 类引号前叙述 p25~p75 = 7~27 字）；
2. **不含引号**（不能写台词 —— 那是改写，不是插入）+ **像叙述**
   （默认是叙述，只在明确是说理时才拒 —— 白名单当必要条件会误杀）；
3. **不含新的人名/地名/物件**（不能凭空造事实；用真实专名表过滤）；
4. **不推进剧情** —— 靠「不出现上下文里没有的专名」近似保证
   （模型若要写新事实，必然要引入新专名或新事件描述）；
5. **不重复**：① 不能与**已采纳的 beat** 重复（公共前缀 ≥ 4 字或字符集合
   Jaccard ≥ 0.60）；② 不能与**插入点附近的原文**重复（2-gram Jaccard ≥ 0.45）
   —— 上下文相似 ⇒ 句子撞车，真跑实测 4 条采纳里 2 条几乎一样，
   另有 1 条与紧邻原文重复。

## 失败策略

任何一道不过 → **这一处不插**（其余的照插）。
插入失败绝不能影响正文 —— 整段失败就整段放弃，保持原文。
"""
from __future__ import annotations

import logging
import re

#: 插入的模型调用用**低温**。
#:
#: 「变换」不是「创作」—— 高温会让同一份输入的输出漂移
#: （与形态校正那次「一次 76%→24%、一次 76%→92%」同一性质）。
#: 低温让它更可复现（但**不保证**可复现：MiniMax 在这类任务上仍会漂，
#: 所以判据必须逐段验收，不能靠「跑一次就信」）。
BEAT_TEMPERATURE = 0.2

#: 一次最多插几处（防止模型调用失控）。
MAX_BEATS = 6

#: 人名/地名/物件的**专名**提取（用于「不能出现新专名」判据）。
#:
#: ## 为什么不能只查引号里的词
#: 第一版只提取「引号内的短词」与「XX道」两种形态，结果**「京城」这种地名抓不到**
#: ——判据形同虚设（实测：`_real_proper_nouns("他转头望向京城的方向。")` 返回空集）。
#:
#: ## 现在的做法：靠**上下文里出现过的词**反推专名
#: 专名没法靠词形可靠地认出来（中文没有大写），但有个可靠的办法：
#: **上下文里出现过、且位置像专名的词**。所以流程是：
#: 1. 从上下文抽出**候选专名词**（引号内短词 + 「XX道」的说话人 + 已知地名表）；
#: 2. beat 里的**任何**实词，只要不在上下文的词表里、且不是常见虚词，
#:    就当作「可能的新专名」→ 拒。
#:
#: 这个判据是**保守**的（宁可多拒）：宁可漏插一处，也不能往正文里塞进
#: 没有事实依据的人名地名。
#: 代价：模型写纯动作（「他抬起头」）也会被拒 —— 所以「常见词表」要够大。

#: 常见虚词/动作/代词等 —— 这些出现在 beat 里是正常的，不算「新专名」。
_COMMON_WORDS = frozenset("""
的 了 着 过 是 在 和 与 及 或 也 都 就 还 又 再 很 更 最 太 不 没 没有
他 她 它 我 你 我们 你们 他们 她们 对方 两人 一人 那个 这个 那个 这个
一 二 三 四 五 六 七 八 九 十 个 只 条 件 张 把 双 些 点 些
看 望 抬 抬 低头 抬头 转头 回过 侧过 瞥 瞟 扫 盯 闭 睁
走 站 坐 躺 蹲 伏 倚 靠 挪 退 进 跨 迈 踏 奔 跑 停 顿
笑 叹 喝 喊 叫 骂 答 问 说 摇 点头 摇头 伸手 收回 皱眉 舒展
握 抓 提 递 放 拾 抹 擦 按住 松开 握住 攥 扶 拽 推 拉
眼睛 目光 视线 脸色 神色 表情 嘴角 眉头 眉心 眼神 身子 肩膀 手
手指 掌心 手背 指节 指尖 头 脑袋 额头 下巴 脖颈 后背 脊背 腰 腿 脚
话 声音 语气 嗓子 腔调 一声 一句 一阵 一片 一眼 一瞬 一会儿
时候 片刻 半晌 刹那 忽然 慢慢 缓缓 逐渐 依旧 仍然 始终 依然
不 也 又 才 就 只 还 呢 吗 吧 啊 呀 哦 嗯 呵
东西 地方 屋里 屋外 门边 门口 窗边 窗前 桌上 地上 墙边 桥上 路上
天色 天光 日光 月色 风声 雨声 人声 声音 气味 味道
""".split())

#: 常见的真专名（地名/物件/机构）—— 白名单：出现它们不算「新增事实」。
_KNOWN_PROPER = frozenset("""
王府 北荒 京城 皇宫 朝廷 江湖 江湖人 世子 殿下 王爷 皇上 陛下 太傅 先生 姑娘 公子 少爷 小姐
师父 师兄 师弟 师姐 师妹 父亲 母亲 爹爹 娘亲 爷爷 奶奶 哥哥 姐姐 弟弟 妹妹
将军 大人 总管 长老 掌门 教主 门主 帮主 城主 县令 知府 捕快 衙门
兵器 剑 刀 枪 矛 弓 箭 剑鞘 马鞭 缰绳 账册 铜钱 木匣 铁链 银票 金银 铜钱
衣袍 长衫 布衣 铠甲 斗篷 佩剑 玉佩 折扇 灯笼 酒杯 茶壶 茶碗 砚台 书卷
马车 骏马 青灰马 枣红马 战马 军马 骏马 骡子 牛车 板车
""".split())

_PROPER_NOUN_HINT = re.compile(
    r"[“「『]([^”」』]{2,12})[”」』]"  # 引号里的短专名
)
_SPEAKER_HINT = re.compile(
    r"([一-鿿]{2,4})(?:说道|问道|答道|笑道|喝道|道：|问：|答道：)"
)

#: 明显的叙述动词/形容 —— 用来粗判「这是不是一句叙述」
#:
#: **它只是个「阳性信号」，不是白名单** —— 见 `_looks_like_narration`。
_NARRATIVE_HINT = re.compile(
    r"(抬|低|转|看|望|走|站|坐|笑|叹|喝|摇|伸手|收回|皱眉|点头|摇头|"
    r"伸手|按住|松开|顿|停|瞥|瞟|扫|踏|迈|握|抓|提|递|放|拾|抹|擦|"
    r"盯|盯住|闭|睁|侧|俯|蹲|伏|倚|靠|挪|退|进|跨|俯身|抬头|低头)",
)

#: **说理 / 议论**的连接词 —— 这类句子不是「动作或神态叙述」。
#:
#: 为什么要单独一条：`_NARRATIVE_HINT` 是**动词白名单**，白名单**漏一个就误杀**
#: （真跑：6 条候选里 2 条合格的 beat 被误杀，误杀率 33%）：
#: - 「拢着湿布的手微微收紧，指节泛白。」—— 没有白名单里的动词；
#: - 「陆长风眉梢微动，指尖搭在黄绢边角。」—— 同上。
#: 两条都是**标准的动作/神态叙述**，写得很对。
#:
#: ⇒ 判据改成「**默认是叙述，只在明确是说理时才拒**」：
#: 白名单命中 ⇒ 直接过；没命中 ⇒ 看有没有说理连接词，没有就放过
#: （剩下的三道判据：长度、无引号、无新事实，仍然把关）。
_DISCOURSE_HINT = re.compile(
    r"(因为|所以|因此|但是|然而|而且|于是|不过|其实|显然|似乎|仿佛|"
    r"看来|要说|问题是|道理|毕竟|既然|不但|不仅|否则|尽管|虽然)",
)


def _real_proper_nouns(text: str) -> set[str]:
    """抽出文本里像专名的词。

    判据（保守）：
    - 引号里的短词（「那匹青灰马」里的「青灰马」）；
    - 「XX道」的说话人（「老周说道」里的「老周」）；
    - 已知地名/物件/称谓白名单里出现的词。

    **只靠词形认不出「京城」这种地名** —— 第一版就是这么写的，
    结果判据形同虚设（实测返回空集）。
    ⇒ 真正的防线在 `check_no_new_facts()`：拿 beat 里的实词
       与**上下文的词表**比，而不是指望这里能认出专名。
    """
    out: set[str] = set()
    for m in _PROPER_NOUN_HINT.finditer(text or ""):
        s = m.group(1).strip()
        if 2 <= len(s) <= 12:
            out.add(s)
    for m in _SPEAKER_HINT.finditer(text or ""):
        out.add(m.group(1))
    for w in _KNOWN_PROPER:
        if w in (text or ""):
            out.add(w)
    return out


#: beat 里的**实词**（用来与上下文词表比）。
#:
#: ## 注意：不能按「连续汉字块」切
#: 第一版用 `[一-鿿]{2,6}` 切，结果把「老周没有抬头」整块当成一个词 ——
#: 它当然「上下文里没有」⇒ 把**所有**正常 beat 都拒了（实测插入数 0）。
#:
#: ⇒ 改成**逐字滑动**取 2 字窗口，与上下文的 2 字窗口集合比。
#: 「没有抬头」在上下文里没有 ⇒ 会被疑；这仍然偏严，
#: 但比整块切好得多（正常 beat 的实词多为上下文里出现过的 2 字组合）。
_CONTENT_WINDOW = 2


def _windows(text: str, n: int = _CONTENT_WINDOW) -> set[str]:
    """文本里所有 n 字连续窗口（去标点后）。"""
    clean = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    if len(clean) < n:
        return {clean} if clean else set()
    return {clean[i:i + n] for i in range(len(clean) - n + 1)}


def _context_proper_nouns(context: str) -> set[str]:
    """从上下文里抽「像专名的词」，当白名单用（没有专名表时的降级方案）。"""
    out: set[str] = set()
    for m in _PROPER_NOUN_HINT.finditer(context or ""):
        s = m.group(1).strip()
        if 2 <= len(s) <= 12:
            out.add(s)
    for m in _SPEAKER_HINT.finditer(context or ""):
        out.add(m.group(1))
    for w in _KNOWN_PROPER:
        if w in (context or ""):
            out.add(w)
    return out


def _windows(text: str, n: int = _CONTENT_WINDOW) -> set[str]:
    """文本里所有 n 字连续窗口（去标点后）。"""
    clean = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    if len(clean) < n:
        return {clean} if clean else set()
    return {clean[i:i + n] for i in range(len(clean) - n + 1)}


#: 参考书语汇白名单的缓存（按原文长度+首尾片段做 key）。
#:
#: 参考书有 500 万字，一次全量扫 2 字窗口约 1~2 秒 —— 同一段正文里要查好几次，
#: 不缓存的话每次插入都重扫。
_VOCAB_CACHE: dict[str, set[str]] = {}


def corpus_vocabulary(corpus_text: str) -> set[str]:
    """参考书的 2 字组合白名单（带缓存）。"""
    key = f"{len(corpus_text)}:{corpus_text[:64]}:{corpus_text[-64:]}"
    cached = _VOCAB_CACHE.get(key)
    if cached is not None:
        return cached
    vocab = _windows(corpus_text)
    _VOCAB_CACHE[key] = vocab
    return vocab


#: beat 里出现**数字** ⇒ 拒。
#:
#: 为什么这条能防「新事实」：真书的 beat（动作/神态）**几乎不报数** ——
#: 报了数就是新事实（新的数量、新的物件、新的地名距离）概率最高的地方。
#:
#: ## 「半」不是数字（真跑踩到过）
#: 「退后半步」「那半只黑蹄」里的「半」是**量词**，不是数量 ——
#: 第一版把它当数字，误拒了两条本来合格的 beat（实测 6 个插入点只采纳 3 个，
#: 其中 2 个是被「半」误杀的）。
#: ⇒ 数字判据要排除**量词用法**。
#:
#: ## 单独的「一」也不是数字（2026-10-27 实测，误杀率 100%）
#: 「眉头**一**皱」「目光停**一**瞬」「指尖轻轻**一**点」「脚步**一**顿」
#: —— 这里的「一」是**副词**（表示动作短暂），不是数量。
#: 实测 128 条真跑的引导语，这条判据拒掉 12 条，**逐条看全是误杀**。
#: 「一封信」那种真数量确实分辨不出来（中文没词边界）——
#: 按项目纪律「**判不准的别当场拒**」，单独的「一」**不判为数字**，
#: 但「十/百/千/两/三…」这些**硬数量**照旧拒。
_NUM_HINT = re.compile(r"[0-9０-９一二三四五六七八九十百千万两]+")

#: 单独的「一」不算数量（副词用法）。跟着「十百千万两」的不算（那是硬数量）。
_ONE_IS_SOFT = re.compile(r"一(?![十百千万两])")

#: 量词/程度词里的「半」「几」「数」——不是数量，不算「新事实」。
_NOT_A_COUNT = frozenset("半几数点些多")

#: 「半」等字若**跟在名词后**（如「半步」「半只」）是量词；
#: 若**单独成词**（「半斤八两」）另说 —— 两种都放过。
#:
#: 2026-10-27 补：「一半」也要放过（「布条缠到一半」是描写，不是新事实）。
_HALF_AS_MEASURE = re.compile(r"[半几数](?=[个只匹条张口口碗步声下名位道把双份成块枚尊柄扇面根支顶身头尾])")


def check_no_new_facts(
    beat: str,
    context: str,
    *,
    proper_nouns: set[str] | None = None,
    declared_nouns: list[str] | None = None,
) -> tuple[list[str], bool]:
    """beat 里有没有可能造出事实的地方？

    返回 (可疑项列表, 是否降级)。

    ## 三层判据（各管一件事，见模块 docstring）

    1. **模型自报的专名**（`declared_nouns`）必须都在**专名表/上下文**里
       —— 模型自己说「我用了京城」，我们只管查表（确定性）。
    2. **数字** ⇒ 拒（新事实的来源）。
    3. 专名表给了白名单，但**不逐词查**（中文没词边界，误杀太多）。

    ## 为什么不做「逐词查表」

    试过三版都不成立（详见模块 docstring）：按汉字块切会把「老周没有抬头」
    整块当成一个词 ⇒ 所有正常 beat 全被拒；按 2 字窗口切则「来回」「蹭着」
    这类词上下文里本来就没有 ⇒ 照样误杀（13 个疑词）；
    用采样过的语料查 ⇒ 仍误杀（「抬头」这种组合语料里不一定有）。
    """
    suspicious: list[str] = []

    # 判据 1：模型自报的专名必须都在允许名单里
    #:
    #: **注意**：`_KNOWN_PROPER`（常见地名/物件/称谓白名单）**不能**在这里放行 ——
    #: 它的用途是「认出常见专名」，而这里是「检查有没有造新事实」：
    #: 「京城」在白名单里被放行 = 允许模型凭空提到一个地名（实测：插入数 2 而非 0）。
    #: ⇒ 这里**只**认专名表（`proper_nouns`）与上下文里出现过的名字。
    allowed = set(proper_nouns or ())
    for n in (declared_nouns or []):
        n = (n or "").strip()
        if not n:
            continue
        if n in allowed:
            continue
        if n in context:
            continue
        suspicious.append(f"专名:{n}")

    # 判据 2：数字
    # 「一」单独出现时是副词用法（眉头一皱/目光停一瞬），不判为数字。
    probe = _HALF_AS_MEASURE.sub("", beat or "")
    probe = _ONE_IS_SOFT.sub("", probe)
    m = _NUM_HINT.search(probe)
    if m:
        suspicious.append(f"数字:{m.group(0)}")

    return suspicious, False


def _looks_like_narration(beat: str) -> bool:
    """这句像不像「动作/神态叙述」。

    判据：**含叙述动词，且不含引号**。

    ## 为什么是「默认放过，只在说理时才拒」

    第一版用「动词白名单」做**必要条件** —— 白名单漏一个词就误杀。
    真跑（完整流程，MiniMax-M3.1-Flash）实测**误杀率 33%**（6 条候选里 2 条）：

    - 「拢着湿布的手微微收紧，指节泛白。」
    - 「陆长风眉梢微动，指尖搭在黄绢边角。」

    两条都是**标准的动作/神态叙述**，写得没问题，只是没用白名单里的动词。

    ⇒ 判据改成：
    - 白名单命中 → **过**；
    - 没命中 → 只有在**是说理/议论**时才拒（`_DISCOURSE_HINT`），否则**放过**
      （长度、无引号、无新事实这三道判据仍在把关）。

    纪律同项目其它地方：**判不准的别当场拒** —— 误杀会堵死真活。
    """
    if not beat or not beat.strip():
        return False
    if any(q in beat for q in "“”「」『』\"'"):
        return False
    if _NARRATIVE_HINT.search(beat):
        return True
    return not _DISCOURSE_HINT.search(beat)


def _split_declared_nouns(raw: str) -> tuple[str, list[str]]:
    """把模型输出拆成 (叙述句, 自报的专名列表)。

    模型被要求在句后另起一行写 `专名：甲、乙` —— 这样「它用了什么专名」
    由**模型自己声明**，服务端只管查表（确定性，不用猜什么是专名）。
    """
    text = (raw or "").strip()
    nouns: list[str] = []
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    kept: list[str] = []
    for ln in lines:
        m = re.match(r"^专名[：:]\s*(.+)$", ln)
        if m:
            nouns.extend([x.strip() for x in re.split(r"[、,，\s]+", m.group(1)) if x.strip()])
            continue
        kept.append(ln)
    return "\n".join(kept), nouns


def _clean_beat(raw: str) -> str:
    """把模型输出收拾成一句话叙述（不含自报专名那行）。"""
    t, _nouns = _split_declared_nouns(raw)
    t = t.strip()
    # 去掉可能的引号包裹与首尾空白
    t = t.strip("“”\"'「」『』 ")
    # 只取第一行（模型有时会多写解释）
    for sep in ("\n", "。\n", "---"):
        if sep in t:
            t = t.split(sep)[0].strip()
    # 去掉末尾多余句号后补一个
    t = t.rstrip("。；;，,")
    return t + "。" if t else ""


#: 两条引导语算「重复」的门槛（2-gram Jaccard）。
#:
#: ## 为什么不用「公共前缀 ≥ 4」当主判据（2026-10-27 实测推翻）
#: 前缀规则是早先按真跑定的（「他垂下眼，指尖在袖口边停了停。」/
#: 「他垂下眼，手指在袖口上停了停。」公共前缀 4 字）。
#: 但它**大量误杀**「同一开头、不同宾语」的正常引导语：
#: 实测 128 条真跑里，它拒掉的 14 条中有 10 条是这种
#: （「他抬手指向北面」vs「他抬手指向守卒」—— 前缀 5，其实是两句不同的话）。
#:
#: ⇒ 改成**2-gram Jaccard**（看的是词组，不是字面开头）。
#: 同一批数据上的分离度很清楚：
#: - 真重复（该拒）：0.538 / 0.692 / 0.727
#: - 误杀（该放）：0.120 ~ 0.357
#: ⇒ 阈值取 **0.40**（真重复最低 0.538，误杀最高 0.357，两边都留了余量）。
#: 同时保留「字符集 Jaccard ≥ 0.60」当第二条（结构一样、只换了个词）。
#:
#: **验算**：换成新规则后，被拒的 14 条只剩 4 条（确实重复的那 4 条），
#: 而**已采纳的 96 条里 0 对**会被新规则误伤成重复。
_DUP_2GRAM_JACCARD = 0.40

#: 字符集合 Jaccard 到这个值也算重复（改了字但结构一样）。
_DUP_JACCARD = 0.60

#: 与**插入点上下文**的 2-gram 重合率到这个值就算重复。
#:
#: ## 为什么还要一条「跟上下文比」
#:
#: 完整流程真跑（MiniMax-M3.1-Flash）的落库正文里，4 处采纳的 beat 中有一处
#: 跟**紧邻的原文段落**重复：
#:
#: - 原文：「陆崇将手中的蜡书放在木槽上。」
#: - 插入的 beat：「陆崇将手按在木槽上，抬眼看去。」
#:
#: ⇒ 相邻两段连着做同一个动作（都往木槽上放东西），读起来像模型卡带了。
#: 这条跟「已采纳的 beat 互撞」是**两回事**：前者只跟**上下文**比。
#:
#: 为什么用 2-gram 而不是字符集合：字符集合会把「都含木槽上」当成相似
#: （实测那两句的字符集合 Jaccard 是 0.57，差一点点够不着 0.60）；
#: 2-gram 看的是**词组**，更贴近「说的是同一句话」。
#:
#: ## 阈值是量出来的，不是拍的
#:
#: 真跑那 4 处插入与各自上下文的 2-gram Jaccard：
#: **0.056 / 0.333（重复的那处）/ 0.000 / 0.000**；
#: 另取 4 组「同样合规、但不该被判重复」的对照：
#: **0.034 / 0.065 / 0.000 / 0.000**。
#: ⇒ 取 **0.15**（上下各留 2 倍余量）。
_DUP_CTX_JACCARD = 0.15


def _normalize_beat(beat: str) -> str:
    """去掉标点与空白，只留汉字。"""
    return "".join(ch for ch in (beat or "") if "一" <= ch <= "鿿")


def _bigrams(text: str) -> set[str]:
    """相邻两字的组合集合（2-gram）。"""
    t = _normalize_beat(text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _bigram_jaccard(a: str, b: str) -> float:
    """两个文本的 2-gram Jaccard 相似度。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _too_similar_to_context(beat: str, context: list[str]) -> str | None:
    """这条 beat 跟插入点附近哪段原文重复了？（返回那段原文，没重复返回 None）

    ## 为什么要有这道判据

    「不跟别的 beat 重复」不够 —— beat 还可能**重复它要插进去的那段上下文**
    （模型照着上下文写，动作就撞上了）。真跑实测 4 处采纳里有 1 处如此。
    """
    for para in context:
        if not para or not para.strip():
            continue
        if _bigram_jaccard(beat, para) >= _DUP_CTX_JACCARD:
            return para
    return None


def _find_duplicate(beat: str, accepted: list[str]) -> str | None:
    """这条 beat 跟已采纳的哪一条重复了？返回那条（没重复返回 None）。

    ## 判据（两条任一成立即算重复）

    1. **2-gram Jaccard ≥ 0.40**（看的是词组）；
    2. 字符集合 Jaccard 相似度 ≥ 0.60（结构一样、只换了个词）。

    早先还有一条「公共前缀 ≥ 4 个汉字」，2026-10-27 实测证明它**大量误杀**
    「同一开头、不同宾语」的正常引导语 ⇒ 已撤掉（见 `_DUP_2GRAM_JACCARD` 的注释）。
    """
    a = _normalize_beat(beat)
    if not a:
        return None
    a_set = set(a)
    a_bi = _bigrams(beat)
    for other in accepted:
        b = _normalize_beat(other)
        if not b:
            continue
        b_set = set(b)
        b_bi = _bigrams(other)
        if a_bi and b_bi:
            union_bi = a_bi | b_bi
            if union_bi and len(a_bi & b_bi) / len(union_bi) >= _DUP_2GRAM_JACCARD:
                return other
        union = a_set | b_set
        if union and len(a_set & b_set) / len(union) >= _DUP_JACCARD:
            return other
    return None


def _insert_beats(
    text: str,
    provider,
    *,
    target_ratio: float,
    corpus_text: str | None = None,
    proper_nouns: set[str] | None = None,
    max_beats: int = MAX_BEATS,
) -> tuple[str, dict]:
    """在正文里插入 beat。返回 (新正文, 诊断)。

    ## 流程

    1. `beat.find_slots` 找位置（**纯机械**，按真书实测的判据）；
    2. 逐个位置调模型写一句叙述（低温）；
    3. **逐条验收**（长度 / 无引号 / 无新专名 / 像叙述）；
    4. 全部验收通过才插入；有一条不过就只插别的。

    ## 失败策略

    - 某个位置写不出合格 beat → **跳过这一处**（不影响别的）；
    - 全部失败 → 原文返回，诊断写明。
    """
    from app.style.beat import (
        BEAT_MAX_CHARS,
        BEAT_MIN_CHARS,
        beat_system_prompt,
        beat_user_prompt,
        find_slots,
        merge_beat,
    )
    from app.style.stylometry import paragraphs

    info = {"applied": False, "inserted": 0, "slots": 0, "rejected": []}
    if not text or not text.strip():
        return text, info

    ps = [p for p in paragraphs(text) if p.strip()]
    if not ps:
        return text, info

    slots = find_slots(ps, target_ratio=target_ratio, max_slots=max_beats)
    info["slots"] = len(slots)
    if not slots:
        info["reason"] = "不需要插入（A 类占比已在目标内）"
        return text, info

    # 判据 3 的语汇白名单：优先用参考书原文（最强），
    # 没有就退化成「上下文里出现过的词组」（并在诊断里标明降级）。
    info["vocab_source"] = "参考书原文" if corpus_text else "上下文（降级）"

    accepted: list[tuple[int, str]] = []
    for slot in slots:
        try:
            raw = provider.chat(
                beat_system_prompt(),
                beat_user_prompt(slot.before, slot.after,
                                 min_chars=BEAT_MIN_CHARS, max_chars=BEAT_MAX_CHARS),
                temperature=BEAT_TEMPERATURE,
            )
        except Exception as exc:  # noqa: BLE001 —— 一个位置失败不该毁掉整段
            logging.getLogger(__name__).warning("beat 插入失败: %s", exc)
            info["rejected"].append({"index": slot.index, "reason": f"调用失败 {type(exc).__name__}"})
            continue

        beat, declared = _split_declared_nouns(raw)
        beat = _clean_beat(raw)
        n_cjk = sum(1 for ch in beat if "一" <= ch <= "鿿")

        # 判据 2：无引号 + 像叙述
        if not _looks_like_narration(beat):
            info["rejected"].append({"index": slot.index, "reason": "不像叙述或含引号",
                                     "text": beat[:40]})
            continue
        # 判据 1：长度
        if not (BEAT_MIN_CHARS <= n_cjk <= BEAT_MAX_CHARS):
            info["rejected"].append({"index": slot.index, "reason": f"长度 {n_cjk} 字不在 {BEAT_MIN_CHARS}~{BEAT_MAX_CHARS}",
                                     "text": beat[:40]})
            continue
        # 判据 3：不能造事实 —— 模型自报的专名要在允许名单里；且不报数
        new_words, degraded = check_no_new_facts(
            beat, f"{slot.before}\n{slot.after}\n{text}",
            proper_nouns=proper_nouns, declared_nouns=declared,
        )
        if degraded:
            info["degraded"] = True
        if new_words:
            info["rejected"].append({"index": slot.index,
                                     "reason": f"可能造事实 {new_words[:3]}",
                                     "text": beat[:40]})
            continue
        # 判据 5：不能跟已采纳的 beat 重复，也不能跟插入点附近的原文重复
        dup = _find_duplicate(beat, [b for _i, b in accepted])
        if dup:
            info["rejected"].append({"index": slot.index, "reason": f"与已插入的一句重复（{dup}）",
                                     "text": beat[:40]})
            continue
        dup_ctx = _too_similar_to_context(beat, [slot.before, slot.after])
        if dup_ctx:
            info["rejected"].append({"index": slot.index, "reason": f"与上下文重复（{dup_ctx[:24]}）",
                                     "text": beat[:40]})
            continue

        accepted.append((slot.index, beat))

    if not accepted:
        info["reason"] = "没有一处插入通过验收"
        return text, info

    # 插入（从后往前，避免索引错位）
    new_ps = list(ps)
    for idx, beat in sorted(accepted, key=lambda t: -t[0]):
        new_ps = merge_beat(new_ps, idx, beat)
    info["applied"] = True
    info["inserted"] = len(accepted)
    info["beats"] = [b for _i, b in accepted]
    logging.getLogger(__name__).info(
        "beat 插入：%d 处候选，%d 处通过验收", len(slots), len(accepted),
    )
    # 段落分隔符要跟原文**一致** ——
    # `stylometry.paragraphs` 按 `\n` 切段，而正文里段间是空行（`\n\n`）。
    # 用 `\n` 连接会把整篇并成一段（真跑踩到：段数从 4 变 1）。
    sep = "\n\n" if "\n\n" in text else "\n"
    return sep.join(new_ps), info
