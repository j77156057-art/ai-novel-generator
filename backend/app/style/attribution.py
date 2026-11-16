"""对话归属位置：这本书把引语放在说话人**前面**，还是**后面**。

## 这是哪一维

中文小说的对白有两种基本写法：

    前置归属：裴远说道：“内务房不管饭。”      ← 说话人 + 归属动词 + 冒号 + 引语
    后置归属：“内务房不管饭。”裴远低声说。    ← 引语 + 说话人 + 归属动词

「归属动词」还分两派：**道系**（道、说道、问道、低声道…）与**说系**（说）。
参考书用哪一派、归属放前还是放后，都是**这本书的写法**，不是通用规矩。

## 为什么值得单独量它

实测（2026-10-31，盲评**第一次真跑**）：18 题盲评包被评者判成
「人类侧 16 : 我们侧 2」（P=0.0007，FAIL）。逐对排查发现，
**只用一条写法规则就能定位哪边是人类侧**，精确率 100%：

| 规则 | 可判 | 判对 | 精确率 |
|---|---|---|---|
| 「X道：“…”」出现的那侧 = 人类侧 | 10/18 | 10 | 100% |
| 引语后**不**接说话人的那侧 = 人类侧 | 6/18 | 6 | 100% |

同期的题材 / 年代 / 专名 / 脱敏痕迹八道闸门**全绿** ——
它们查的是**内容**，这一维查的是**写法**。

## 为什么之前没查到：尺是坏的

第一版尺的动词集合只有「道 / 说道 / 问道 / 答道 / 笑道 / 叹道 / 低声道…」，
**漏了单独的「说」** ⇒ 我们侧写的「才说：“…”」「想了想，说：“…”」全部漏检
⇒ 得出「我们侧从不写前置归属（0 次）」的**错误结论**。

⇒ 教训钉死：**尺必须先用阳性对照自证**。本模块顶部就有对照用例，
  `tests/test_style_attribution.py` 把它们钉成用例。

## 谁在用

- `app/generation/prompts.py`：从**实际喂给模型的样例**里量出这本书的惯例，
  写成一句可执行口径（`attribution_directive`）。
- `backend/acceptance_lib.py`：第九道闸门用它判「写法上两侧对不对称」。

⚠️ 本模块**只依赖标准库**：盲评 skill 脚本 `leak_scan.py` 要能独立跑，
  不能因为它而拖进 sqlalchemy。
"""
from __future__ import annotations

import re

#: 归属动词。**顺序有意义**：长的一定在前面（「说道」必须先于「说」命中，
#: 「问道」先于「道」，否则会切出「说」/「问」两个字，末字判错）。
#:
#: 判定「道系 / 说系」时看的是**动词的末字**：
#: 「说道 / 问道 / 答道 / 低声道」末字是「道」；「说」末字是「说」；
#: 「笑道 / 叹道」末字是「笑 / 叹」，另算一类。
_VERBS = (
    r"说道|问道|答道|笑道|叹道|低声道|沉声道|冷声道|怒道|喝道|喊道|说|道"
)

#: **前置归属**：说话人（+ 归属动词 + 冒号）在引语**之前**。
#:
#: 前缀 `[^\s，。！？：；、]{0,12}` 是「说话人那一截」——
#: 允许「李牧终于」「他低声」「一名少年」这类修饰，但不许跨过标点
#: （跨标点会把整段叙述吞成说话人）。
_FRONT_RE = re.compile(
    r"[^\s，。！？：；、]{0,12}(?P<verb>" + _VERBS + r")[：:]"
)

#: **真·后置归属**：引语**说完**之后补一个说话人，且这句到此收尾。
#:
#: ⚠️ 为什么尾部必须是「。！？」或句末，**不能是「，」**：
#: 带上「，」就会把叙述里的「说」也吃进来 ——
#: 「…不用说，肯定是他舅舅」「…实在不好意思说，中毒太深」
#: 都被当成后置归属（实测假阳性，参考书全书 31 次里大部分是这类）。
_BACK_RE = re.compile(
    r"[”』][。！？]?[^\s，。！？：；、“”]{0,8}"
    r"(?P<verb>" + _VERBS + r")[。！？]"
)

#: **插话式归属**：引语中间插一个说话人 —— `“…，”他道，“…”`。
#:
#: 为什么单列：它长得像后置，实则是**第三种**写法。
#: 实测参考书《道途魔典》全书几乎没有，我们侧很常见 ⇒ 同样是非对称线索，
#: 但混进「后置」里会把尺的口径搅浑。
#:
#: ⚠️ 语序：中文是 `“不认识，**”**他道，**“**古字…` ——
#: 逗号在引号**里面**，收尾引号在逗号**外面**。所以这里是「收尾引号 + 说话人
#: + 逗号 + 开引号」，不是「引号 + 逗号」。
_MID_RE = re.compile(
    r"[”』][^\s，。！？：；、“”]{0,8}"
    r"(?P<verb>" + _VERBS + r")[，,][“「『]"
)

#: 量「每百字」时用的分母下限（短文本别炸）。
_MIN_CHARS = 1

#: 判定「这本书有明确的前置惯例」门槛（每百字）。
#:
#: ⚠️ 门槛是**量出来的**（10 本真书）：
#: 拿**实际喂给模型的那 3 条样例**去量，样例密度与整本密度的关系是：
#:
#: | 参考书 | 整本 前置/百字 | 样例 前置/百字 | 样例命中数 |
#: |---|---|---|---|
#: | 《道途魔典》 | 0.993 | 0.982 | 36 |
#: | 《北荒刀歌》 | 0.400 | 0.698 | 24 |
#: | 《山河旧录》 | 0.436 | 0.686 | 28 |
#: | 《回声年代》 | 0.162 | **0.080** | **3** |
#: | 《两千年旧账》 | 0.067 | 0.109 | 4 |
#: | 《透视手记》 | 0.000 | 0.000 | 0 |
#: | 《戏神不在》 | 0.018 | 0.000 | 0 |
#:
#: ⇒ 第一版门槛定 0.10，**把《回声年代》挡在外面了**（样例 0.080 < 0.10）
#: ⇒ 都市臂**根本没拿到口径**，产出仍是「后置 7 次 / 前置 0 次」（实测）。
#:
#: ⇒ 改成 **命中 ≥ 3 次 且 密度 ≥ 0.05/百字**：
#: 10 本书里，明显有前置惯例的**全部触发**，没有的**全部不触发**，
#: 混合/后置的那两本按各自那一支走。
#:
#: 为什么还要「命中 ≥ 3 次」这一条：光看密度，短样例里偶发 1~2 次也会过线；
#: 命中数是**绝对证据**，密度是**比率**，两个都要。
FRONT_PER100_MIN = 0.05
FRONT_MIN_HITS = 3

#: 判定「前置明显占优」的比例门槛：前置密度至少要是（后置+插话）的这么多倍。
FRONT_OVER_BACK_RATIO = 2.0

#: 判定「这本书有明确的后置惯例」门槛（每百字 / 命中数）—— 与前置同一把尺。
BACK_PER100_MIN = 0.05
BACK_MIN_HITS = 3


def _norm(text: str) -> str:
    """去掉空白与换行：中文正文的统计不该被排版影响。"""
    return re.sub(r"\s+", "", text or "")


def attribution_profile(text: str) -> dict:
    """量一段文本的「对话归属位置」画像。

    返回：
        front / back / mid   前置 / 真后置 / **插话式**归属的**次数**
        front_dao         前置里动词末字是「道」的次数
        front_shuo        前置里动词末字是「说」的次数
        front_per100      前置密度（每百字）
        back_per100       后置密度（每百字）
        mid_per100        插话式密度（每百字）

    ⚠️ 三种形态**必须分开**：混在一起会把「叙述里的说」也算成归属
    （假阳性），也会把「插话式」当成「引语说完再补说话人」（不是一回事）。
    """
    t = _norm(text)
    n = len(t) or _MIN_CHARS
    fronts = _FRONT_RE.findall(t)
    backs = _BACK_RE.findall(t)
    mids = _MID_RE.findall(t)
    dao = sum(1 for v in fronts if v.endswith("道"))
    shuo = sum(1 for v in fronts if v.endswith("说"))
    return {
        "front": len(fronts),
        "back": len(backs),
        "mid": len(mids),
        "front_dao": dao,
        "front_shuo": shuo,
        "front_per100": 100.0 * len(fronts) / n,
        "back_per100": 100.0 * len(backs) / n,
        "mid_per100": 100.0 * len(mids) / n,
        "dao_per100": 100.0 * dao / n,
        "shuo_per100": 100.0 * shuo / n,
        "chars": n,
    }


def attribution_convention(text: str) -> dict:
    """这段文本（一般是参考书原文）用的**是哪种归属惯例**。

    返回 `convention`：
        "front_dao"  以「X道：“…”」为主
        "front_shuo" 以「X说：“…”」为主
        "back"       以「“…。”X说」为主
        "mixed"      两种都有，分不出主次
        "none"       基本没有归属（引语是光秃秃的）

    ⚠️ 门槛是**量的**（密度 + 占比），不是数的 —— 门槛见模块顶部。
    """
    p = attribution_profile(text)
    front = p["front_per100"]
    back = p["back_per100"] + p["mid_per100"]
    front_ok = (front >= FRONT_PER100_MIN and p["front"] >= FRONT_MIN_HITS)
    back_ok = (back >= BACK_PER100_MIN
               and (p["back"] + p["mid"]) >= BACK_MIN_HITS)
    if front_ok and front >= back * FRONT_OVER_BACK_RATIO:
        if p["dao_per100"] >= p["shuo_per100"]:
            convention = "front_dao"
        else:
            convention = "front_shuo"
    elif back_ok and back >= front * FRONT_OVER_BACK_RATIO:
        convention = "back"
    elif front_ok or back_ok:
        convention = "mixed"
    else:
        convention = "none"
    p["convention"] = convention
    return p


def _pick_example_line(texts: list[str], convention: str) -> str:
    """从样例里挑**一行**符合该惯例的原文，给模型当正确实例。

    为什么给实例不给数字：`app/style/form.py` 的实测结论 ——
    「给『形态的正确实例』比给统计数字有用：模型能照着抄形态，抄不了比率」。

    ## 下限为什么是 8 字（不是 12）

    归属句天然就短：「秦昭道：“放肆。”」只有 10 个字。
    下限定 12 会把**最标准**的实例挡在外面（实测踩过：口径整条不触发）。
    """
    if convention == "front_dao":
        pat = _FRONT_RE
    elif convention == "front_shuo":
        pat = re.compile(r"[^\s，。！？：；、]{0,12}说[：:]")
    elif convention == "back":
        pat = _BACK_RE
    else:
        return ""
    for t in texts or []:
        for line in (t or "").split("\n"):
            line = line.strip()
            if 8 <= len(line) <= 60 and pat.search(line) and "“" in line:
                return line
    return ""


#: 口径文案。**每条都要把形态写死**（写成 `某人道：“原话”` 的样子），
#: 后面再跟一个从样例里摘出来的真实实例。
_DIRECTIVE_TMPL = {
    "front_dao": (
        "这本书的对话引语写在说话人**后面**，习惯用「道」字收尾，"
        "写成 `某人道：“原话”` 的样子；引语说完**不要**再补一句「某某说」，"
        "也**不要**在引语中间插「他道，」这种夹注。"
    ),
    "front_shuo": (
        "这本书的对话引语写在说话人**后面**，写成 `某人说：“原话”` 的样子；"
        "引语说完**不要**再补一句「某某说」，"
        "也**不要**在引语中间插「他道，」这种夹注。"
    ),
    "back": (
        "这本书的对话引语写在说话人**前面**（`“原话。”某某说。`），"
        "引语说完再补说话人。"
    ),
}


def attribution_directive(texts: list[str]) -> str:
    """把参考书的归属惯例写成一句可执行的口径；**没有明显惯例就返回空串**。

    `texts` = **实际喂给模型的那几条样例**（可以已被脱敏成等长占位符 ——
    脱敏换掉的是名字，不影响「说话人 + 动词 + 冒号 + 引语」这个形态）。

    ## 为什么要显式写口径（而不是只给原文样例）

    实测：仙侠臂那 3 条样例里**已经有 29 次**「X道：“…”」，
    模型照样只写出 0.06 次/百字（**差 16 倍**）⇒ **光有样例教不会**。
    同一批模型对「句长」也只认样例不认数字，对「纯台词段占比」则**只认数字不认样例**。

    ⇒ 归属位置属于「**形态**」这一类：**样例 + 一句显式口径**一起给。
    """
    joined = "\n".join(t or "" for t in (texts or []))
    if not joined.strip():
        return ""
    conv = attribution_convention(joined)
    tmpl = _DIRECTIVE_TMPL.get(conv.get("convention", ""))
    if not tmpl:
        return ""
    body = tmpl
    example = _pick_example_line(list(texts or []), conv["convention"])
    if example:
        body += "\n例如：" + example
    return body
