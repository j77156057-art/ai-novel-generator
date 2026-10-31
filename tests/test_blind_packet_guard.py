"""盲评包的四道闸门（2026-10-30 重写）。

==============================================================================
起因：主人一句话点破了第一版盲评包
==============================================================================

「这段语料取自一部很出名的武侠，光是看到主角叫啥我就知道是不是人写的了」

⇒ 第一版盲评包有两个硬伤：
   1. 人类样本的人物名直接暴露出处（评者一眼就能认出是哪部书）；
   2. 题材跨代（一侧古风武侠、一侧现代都市）—— 就算名字全涂掉，
      评者不看内容也能分出哪边是哪边。

⇒ 修法不是「换个样本」，是**让工具自己发现这种包**：
   - 两侧**同等脱敏**（同一套召回 + 判定 + 等长占位符）；
   - 生成时做**题材/年代自检**（整包级别），跨代就把 `blind` 置 false；
   - 生成时做**脱敏痕迹密度自检**，密度差得出来也置 false；
   - 判定脚本看到 `blind=false` ⇒ **拒绝出统计结论**（退出码 2）。

## 为什么自检要「整包级别」

实测（一次性探测脚本，跑完即弃）：逐 400 字样本算年代标记，
只检出 2/6；把整包 2400 字拼起来算，检出 100%。
单样本里标记词出现零几次，分位数噪声压不住信号。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from acceptance_lib import (  # noqa: E402
    blind_verdict,
    density_leak,
    era_mismatch,
    era_profile,
    mask_density,
    mask_names,
    mask_side,
    normalize_for_blind,
    recall_proper_nouns,
    shape_candidates,
)


# --------------------------------------------------------------------------- #
# 假 provider：只判「我认识的这几个词是人名」，其余一律 null
# --------------------------------------------------------------------------- #

class _StubJudge:
    """按词判定专名的假 provider（只用于测流程，不测判定质量）。

    它要应付**三种**提示词：

    - 逐词判定（system 里含「标注员」）：回「每个词一个 JSON 对象」
    - 题面级复查（system 里含「校对」）：回「残留专名的 JSON 数组」
    - 题材判定（system 里含「题材分类员」）：回「每个片段一个题材」
    """

    def __init__(self, names: set[str], residual: list[str] | None = None,
                 fail_words: set[str] | None = None,
                 genre_rules: dict[str, str] | None = None):
        self.names = names
        self.residual = list(residual or [])
        self.fail_words = set(fail_words or set())
        #: 题材判定规则：{正文里的关键词: 题材名}；命中第一个就返回它
        self.genre_rules = genre_rules or {}
        self.calls = 0

    def _genre(self, text: str) -> str:
        for kw, g in self.genre_rules.items():
            if kw in text:
                return g
        return "其他（以上都不是，或看不出来）"

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        if "题材分类员" in system:      # 题材判定（输入是 JSON 数组）
            arr = []
            m = re.search(r"\[.*\]", user, re.S)
            for it in json.loads(m.group(0)) if m else []:
                arr.append({"id": it.get("id"),
                            "genre": self._genre(it.get("text") or "")})
            return json.dumps(arr, ensure_ascii=False)
        if "校对" in system:          # 题面级复查
            # 报告**当前文本里还在的**第一个候选名 —— 模拟「每轮都揪出新名字」。
            # （不能无条件报队首：名字被遮掉后就不在文本里了，报了也会被
            #   「只认正文里真有的」那一关滤掉 ⇒ 模拟不出「补不干净」。）
            for n in self.residual:
                if n in user:
                    return json.dumps([n], ensure_ascii=False)
            return json.dumps([], ensure_ascii=False)
        words = [w.strip() for w in user.split("：", 1)[-1].split("、") if w.strip()]
        arr = []
        for w in words:
            if w in self.fail_words:  # 模拟「这批判不出来」
                continue
            arr.append({"w": w, "t": "character" if w in self.names else None})
        return json.dumps(arr, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 1. 脱敏：等长、长名优先
# --------------------------------------------------------------------------- #

def test_脱敏是等长替换():
    text = "赵长风借着阳光看着棋子散发出的一圈圈光晕。"
    out, names = mask_names(text, ["赵长风"])
    assert "赵长风" not in out
    assert "赵长风" in names
    assert len(out) == len(text), "占位符必须等长 —— 否则句长指标失真"


def test_脱敏长名优先不会留下半个名字():
    """「赵长安」先替掉，就不会再剩下孤零零的「凤年」。"""
    text = "赵长安看了赵长一眼。"
    out, _ = mask_names(text, ["赵长安", "赵长", "凤年"])
    assert "徐" not in out
    assert "年" not in out
    assert out.count("＃") == 5, out     # 赵长安(3) + 赵长(2)


def test_占位符不带编号():
    """带编号会泄露「这一侧有多少个不同的专名」⇒ 编号个数本身是线索。"""
    out, _ = mask_names("林夜与苏晚站在程砚面前。", ["林夜", "苏晚", "程砚"])
    assert "＃1" not in out and "＃2" not in out
    assert out.count("＃") == 6


def test_切串残渣削成核心再遮():
    """「上苍梧山」是把前面的虚词切进来了 —— 遮核心，不能把「上」也吃掉，
    但也**不能整个丢掉**（丢掉就等于把真名留在题面里）。"""
    out, hit = mask_names("从上苍梧山下来。", ["上苍梧山"])
    assert hit == ["苍梧山"], hit
    assert out == "从上＃＃＃下来。", out


def test_切串残渣不会被召回():
    """召回阶段就不该给出「上苍梧山」这种候选。"""
    cands = shape_candidates("从上苍梧山下来，一路向北。")
    assert "上苍梧山" not in cands


# --------------------------------------------------------------------------- #
# 2. 召回：片段级必须能召回（PMI 在片段级召回 0 个）
# --------------------------------------------------------------------------- #

def test_片段级形状召回能拿到人名():
    """阳性对照：尺必须会响。"""
    cands = shape_candidates("赵长安站在门口，林夜从巷子里走出来。")
    assert "赵长安" in cands
    assert "林夜" in cands


def test_片段级召回能拿到只出现一次的名字():
    """PMI 按绝对频次召回，在 400 字片段上召回 0 个 ⇒ 必须靠形状召回兜底。"""
    snippet = "他推开门，苏晚站在玄关，手里还拎着那把伞。"
    source = snippet * 30          # 整本里高频
    rare = "苏晚站在玄关，窗外有人按了门铃。"   # 只在片段里出现一次
    got = recall_proper_nouns([rare], rare)
    assert "苏晚" in got, f"片段级召回漏了：{got}"


def test_召回只保留真的出现在片段里的候选():
    snippet = "苏晚站在玄关，手里拎着那把伞。"
    source = "林夜走了很远。苏晚不在。程砚也在。" * 40
    got = recall_proper_nouns([snippet], source)
    assert "苏晚" in got
    assert "程砚" not in got, "片段里没有的词不该送去判定"


# --------------------------------------------------------------------------- #
# 3. 两侧同等脱敏
# --------------------------------------------------------------------------- #

def test_两侧用同一套流程脱敏后长度都守恒():
    human = "赵长风和刘师兄在湖畔说话，师姐递来一枚铜钥匙。"
    ours = "林夜把苏晚给的铜钥匙攥在手里，陆见微站在柜台那头看着他。"
    names = {"赵长风", "刘师兄", "师姐", "林夜", "苏晚", "陆见微"}
    p = _StubJudge(names)
    h_out, h_hit = mask_side([human], human * 20, p)
    o_out, o_hit = mask_side([ours], ours * 20, p)
    assert h_hit and o_hit, f"两侧都要遮到东西：{h_hit} / {o_hit}"
    assert len(h_out[0]) == len(human)
    assert len(o_out[0]) == len(ours)
    assert "＃" in h_out[0] and "＃" in o_out[0]


def test_专名没判出来就不遮_不误伤普通词():
    """判定为 null 的词不该被替换 —— 遮了会把正常词汇毁掉。"""
    text = "王爷公子在路边说话，师妹递来一把伞。"
    p = _StubJudge(set())          # 什么都判不出来
    out, hit = mask_side([text], text * 20, p)
    assert out[0] == text
    assert hit == []


# --------------------------------------------------------------------------- #
# 4. 自检：题材/年代 + 痕迹密度
# --------------------------------------------------------------------------- #

def test_题材跨代能被检出():
    old = "师兄师姐在宗门里练功，王爷身边的内力真气翻涌，江湖上传言四起。"
    new = "他掏出手机叫了外卖，监控和卷宗都调出来，地铁末班车上他还在刷视频。"
    bad, prof = era_mismatch(new, old)
    assert bad, f"跨代没被检出：{prof}"
    assert prof["dominant"]["A"] == "现代", prof["dominant"]
    assert prof["dominant"]["B"] == "古风", prof["dominant"]
    assert prof["shares"]["A"]["古风"] < prof["shares"]["B"]["古风"]


def test_同题材不被误判():
    a = "他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。"
    b = "她把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。"
    bad, _ = era_mismatch(a, b)
    assert not bad


def test_年代自检要在整包级别才算():
    """逐样本只检出 2/6，整包检出 100% —— 判据必须在整包上算。"""
    ours = ("他掏出手机叫了外卖，监控和卷宗都调出来。" * 8 +
            "地铁末班车上他还在刷视频，路灯把积水照成一片。" * 8)
    human = ("师兄师姐在宗门里练功，王爷身边的内力真气翻涌。" * 8 +
             "江湖上传言四起，镖局的镖师在山道上快马加鞭。" * 8)
    # 逐样本（每 400 字）：信号被噪声压住
    per_pair = 0
    for i in range(0, 800, 400):
        bad, _ = era_mismatch(ours[i:i + 400], human[i:i + 400])
        per_pair += 1 if bad else 0
    # 整包：稳定检出
    bad_all, _ = era_mismatch(ours, human)
    assert bad_all, "整包级别必须能检出跨代"
    assert per_pair <= 2, "逐样本本来就不稳（这条是记录实测口径，不是判据）"


def test_脱敏痕迹密度差太多判为泄露():
    assert density_leak(0.2, 3.0) is True
    assert density_leak(1.0, 1.1) is False
    assert density_leak(0.0, 0.0) is False, "两侧都没专名时没有信息量"


def test_盲评判定_跨代不是盲评():
    ours = "他掏出手机叫了外卖，监控和卷宗都调出来，地铁末班车上他还在刷视频。" * 6
    human = "师兄师姐在宗门里练功，王爷身边的内力真气翻涌，江湖上传言四起。" * 6
    v = blind_verdict(ours, human, 1.0, 1.0)
    assert v["blind"] is False
    assert any("题材" in r for r in v["reasons"]), v["reasons"]


def test_盲评判定_没有人类语料不是盲评():
    v = blind_verdict("他掏出手机叫了外卖。", "   ", 0.0, 0.0)
    assert v["blind"] is False
    assert v["reasons"]


def test_盲评判定_同题材同痕迹是盲评():
    a = ("他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。"
         "监控和卷宗都调出来，地铁末班车已经走了。" * 6)
    b = ("她把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。"
         "监控和卷宗都调出来，地铁末班车已经走了。" * 6)
    v = blind_verdict(a, b, 1.2, 1.3)
    assert v["blind"] is True, v["reasons"]


def test_占位符密度能算出来():
    masked, _ = mask_names("林夜与苏晚站在程砚面前。", ["林夜", "苏晚", "程砚"])
    d = mask_density(masked)
    assert d > 0
    assert mask_density("没有任何专名的一段话。") == 0


def test_版式归一去掉缩进与章节标记():
    """盗版站文本带段首缩进和章节标题行 —— 那都是**非对称特征**。"""
    raw = ("    第74章 报复\n"
           "    他把烟一丢，站了起来。\n"
           "\n"
           "    “事情搞定了？”\n")
    out = normalize_for_blind(raw)
    assert "第74章" not in out
    assert "报复" not in out
    assert not out.startswith(" ")
    assert "    " not in out
    assert "他把烟一丢" in out


def test_版式归一不会误删正文里的章节字样():
    out = normalize_for_blind("他说第74章那段写得不错，后来就没再提。")
    assert "第74章" in out, "正文里提到章号不该被当章节标记删掉"


# --------------------------------------------------------------------------- #
# 5. 端到端闸门：不是盲评包 ⇒ 判定脚本拒绝出统计结论（退出码 2）
# --------------------------------------------------------------------------- #

def _rows(n: int = 12) -> list[dict]:
    body = ("林夜把苏晚给的铜钥匙攥在手里，陆见微站在柜台那头看着他。"
            "他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。"
            "监控和卷宗都调出来，地铁末班车上他还在刷视频。")
    return [{"seg": i + 1, "text": body * 3, "error": None} for i in range(n)]


def test_混采时轮转取书而不是随机抽(tmp_path):
    """多本混采要**轮转**：随机抽可能 5 题来自同一本，
    评者认出一本就等于认出大半。"""
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_blind_packet

    corpus = [
        ("甲书", "他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。" * 40),
        ("乙书", "她把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。" * 40),
    ]
    out_dir = tmp_path
    info = build_blind_packet(_rows(), corpus, out_dir, provider=_StubJudge(set()))
    assert info["emitted"] is True
    key = json.loads((out_dir / "blind_key.json").read_text(encoding="utf-8"))
    srcs = [it["human_source"] for it in key["items"]]
    assert set(srcs) == {"甲书", "乙书"}, srcs
    # 6 题 / 2 本 ⇒ 各 3 题
    assert srcs.count("甲书") == srcs.count("乙书") == 3, srcs


def test_脱敏没跑成就不发题面(tmp_path):
    """脱敏失败 ⇒ 题面里留着没脱敏的原文（专名直接暴露出处）⇒ 干脆别发。"""
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_blind_packet

    corpus = [("现代都市", "他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。" * 40)]
    info = build_blind_packet(_rows(), corpus, tmp_path, provider=None)
    assert info["emitted"] is False
    assert info["blind"] is False
    assert not (tmp_path / "blind_samples.md").exists(), "题面不该发出去"
    assert not (tmp_path / "blind_sheet.md").exists()
    # 密封文件仍写出，事后能追溯
    key = json.loads((tmp_path / "blind_key.json").read_text(encoding="utf-8"))
    assert key["blind"] is False
    assert any("脱敏" in r for r in key["blind_reasons"]), key["blind_reasons"]


def test_跨代语料出的包被标成不是盲评且判定脚本拒绝出结论(tmp_path):
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_blind_packet

    corpus = [("古风长篇", ("师兄师姐在宗门里练功，王爷身边的内力真气翻涌。"
                            "江湖上传言四起，镖局的镖师在山道上快马加鞭。") * 40)]
    out_dir = tmp_path
    info = build_blind_packet(_rows(), corpus, out_dir,
                              provider=_StubJudge({"林夜", "苏晚", "陆见微"}))
    assert info, "包应该生成得出来"
    assert info["blind"] is False, f"跨代必须判成不是盲评：{info}"
    assert info["reasons"]

    key = json.loads((out_dir / "blind_key.json").read_text(encoding="utf-8"))
    assert key["blind"] is False
    assert key["blind_reasons"]

    # 题面顶部必须写明「不是盲评包」—— 评者有权知道自己在评什么
    sheet = (out_dir / "blind_samples.md").read_text(encoding="utf-8")
    assert "不是盲评包" in sheet

    # 判定脚本：拒绝出统计结论，退出码 2
    rec = (out_dir / "blind_sheet.md")
    rec.write_text("| sample | 更像人写 | 主线更清 | 有 AI 腔 | 猜 AI | 备注 |\n"
                   "|---|---|---|---|---|---|\n"
                   "| s01 | A | A | A | A | |\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(BACKEND / "verify_blind_eval.py"),
         "--sheet", str(rec), "--key", str(out_dir / "blind_key.json")],
        capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 2, f"应当拒绝出结论，实际退出码 {proc.returncode}\n{proc.stdout}"
    assert "不出统计结论" in proc.stdout


# --------------------------------------------------------------------------- #
# 6. 题数可调：n = 评者人数 × 题数，所以「多请人」和「多出题」是两条等效的路
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 7. 第六道闸门：判定失败不许当成「不是专名」；题面里不许残留专名
# --------------------------------------------------------------------------- #

def test_判定批次失败不许被记成不是专名(tmp_path):
    """回归：批次失败（回包被截断/解析不了）时，旧实现把整批记成 null，
    而 null 的含义是「不是专名」⇒ 真名留在题面里（实测 2026-10-30）。"""
    from app.style.entity_judge import UNJUDGED, judge_proper_nouns

    class _Failing:
        def chat(self, system, user):
            return "截断了，没有 JSON"     # 解析不了 ⇒ 整批没判出来

    cache = tmp_path / "names.json"
    got = judge_proper_nouns(["赵长安"], _Failing(), cache_path=cache)
    assert got["赵长安"] == UNJUDGED, got
    # **没判出来的词不许进缓存**（下次还会重判）；更不许被写成 null
    raw = cache.read_text(encoding="utf-8")
    assert "赵长安" not in raw, raw


def test_旧格式判定缓存要作废(tmp_path):
    """旧缓存把「批次失败」记成了 null —— 继续用它 = 继续用一份漏过名的判定。"""
    from app.style.entity_judge import judge_proper_nouns

    cache = tmp_path / "legacy.json"
    cache.write_text(json.dumps({"赵长安": None}, ensure_ascii=False), encoding="utf-8")

    class _Ok:
        def chat(self, system, user):
            words = [w.strip() for w in user.split("：", 1)[-1].split("、") if w.strip()]
            return json.dumps([{"w": w, "t": "character"} for w in words], ensure_ascii=False)

    got = judge_proper_nouns(["赵长安"], _Ok(), cache_path=cache)
    assert got["赵长安"] == "character", got


def test_没判出来的词会让整包作废(tmp_path):
    """判定不出来的词可能是真名 ⇒ 宁可整包不发，也不能带名字出去。"""
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_blind_packet

    corpus = [("现代都市", "他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。" * 40)]
    p = _StubJudge({"林夜", "苏晚", "陆见微"}, fail_words={"陆见微"})
    info = build_blind_packet(_rows(), corpus, tmp_path, provider=p)
    assert info["emitted"] is False, info
    assert info["blind"] is False
    assert not (tmp_path / "blind_samples.md").exists(), "题面不该发出去"


def test_题面里残留的专名会被补遮(tmp_path):
    """第六道闸门：逐词判定漏掉的，题面复查要能抓住并补遮。"""
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_blind_packet

    corpus = [("现代都市", "他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。" * 40)]
    # 逐词判定只认出「林夜」；题面复查额外认出「苏晚」（它漏了）
    p = _StubJudge({"林夜"}, residual=["苏晚"])
    out_dir = tmp_path
    info = build_blind_packet(_rows(), corpus, out_dir, provider=p)
    assert info["emitted"] is True, info
    key = json.loads((out_dir / "blind_key.json").read_text(encoding="utf-8"))
    assert "苏晚" in key["residual_names"]["repaired"], key["residual_names"]
    sheet = (out_dir / "blind_samples.md").read_text(encoding="utf-8")
    assert "苏晚" not in sheet, "残留的专名必须被补遮掉"


def test_残留专名补不干净就不发题面(tmp_path):
    """每一轮复查都报新名字 ⇒ 补不干净 ⇒ 整包作废（fail closed）。

    ⚠️ **逐题复查**（2026-10-31 改）后，一次出包会调「题面级复查」**每题一次**。
    stub 每次都报「当前文本里还在的」第一个候选名 ⇒ 补了一轮又冒出一个，
    轮数用完仍有残留 ⇒ 最后一轮仍有残留 ⇒ 整包作废、题面不发出去。

    （轮数是 `RESIDUAL_ROUNDS`，本测试不写死它 ——
      文本里的名字够多（18 个）⇒ 任何轮数都补不干净 ⇒ 语义与轮数无关。）
    """
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_blind_packet

    # 正文里放够多的名字：每个被补遮掉，下一轮就冒出下一个
    many = ["赵长安", "钱小七", "孙明月", "李听澜", "周砚秋", "吴子期",
            "郑云舟", "王怀瑾", "冯清和", "陈砚之", "褚明玉", "卫南风",
            "蒋寒星", "沈青崖", "韩暮辞", "杨九思", "朱见山", "秦未央"]
    line = "、".join(many) + "站在路灯下，雨还在下，他把手机揣回兜里，出了便利店。"
    corpus = [("现代都市", line * 40)]
    p = _StubJudge({"林夜"}, residual=list(many))
    out_dir = tmp_path
    info = build_blind_packet(_rows(), corpus, out_dir, provider=p)
    assert info["emitted"] is False, info
    assert info["blind"] is False
    assert not (out_dir / "blind_samples.md").exists(), "补不干净就不该发题面"
    assert any("残留" in r for r in info["reasons"]), info["reasons"]


def test_默认取样位置保持首2中2尾2():
    """默认 6 题的取样口径**不能被改掉** —— 漂移与退化都发生在长跑的首尾。"""
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import sample_positions

    assert sample_positions(30) == [1, 3, 15, 16, 29, 30]


def test_题数加到18时取样要铺满整条长跑():
    """题多了必须**分散取**：否则十几道题落在同一段区间，
    评者其实在评同一批文本，样本量是假的。"""
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import sample_positions

    pos = sample_positions(30, 18)
    assert len(pos) == 18, pos
    assert pos[0] == 1 and pos[-1] == 30, pos          # 首尾必留
    assert len(set(pos)) == 18, pos                    # 不许重复
    # 铺开：后半程也要有题（不是全挤在前半）
    assert sum(1 for p in pos if p > 15) >= 8, pos
    # 相邻取样点不能太密（否则等于同一段取好几次）
    assert max(b - a for a, b in zip(pos, pos[1:])) <= 3, pos


def test_出18题的包_记录表与题数一致(tmp_path):
    """记录表上写的题数必须跟实际题数走，写死 6 的话评者会按错数字数。"""
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_blind_packet

    corpus = [("现代都市", "他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。" * 40)]
    out_dir = tmp_path
    info = build_blind_packet(_rows(30), corpus, out_dir,
                              provider=_StubJudge(set()), n_snips=18)
    assert info["emitted"] is True
    assert info["n"] == 18

    key = json.loads((out_dir / "blind_key.json").read_text(encoding="utf-8"))
    assert len(key["items"]) == 18, len(key["items"])

    sheet = (out_dir / "blind_sheet.md").read_text(encoding="utf-8")
    assert "一共 **18 题**" in sheet, sheet
    assert "× 6" not in sheet, sheet
    assert sheet.count("| A / B | A / B | |") == 18, sheet

    # 题面里的题数也要对得上
    samples = (out_dir / "blind_samples.md").read_text(encoding="utf-8")
    assert samples.count("**A**") == 18, samples.count("**A**")


def test_记录表不给评者留填不了的合计栏(tmp_path):
    """⚠️ 记录表上**不能**有「选我们那段次数 k」这种栏。

    三个理由（2026-10-31 改表时想到的）：

    1. **评者填不了** —— A/B 与「哪边是我们的」是**逐题随机**的（映射在密封文件里），
       评者没有也不该有办法从汇总数反推 k ⇒ 这一栏只会让人瞎填一个数。
    2. **k 不用人填** —— `verify_blind_eval.py` 直接从密封文件算 k 与 n。
    3. **它引入了「汇总方视角」的措辞** —— 「选**我们**那段」暗示了
       「有人=我们的产出、有人=不是」。
       ⚠️ 这条**不是新增的实质泄露**：题面开头已经说了
       「一段来自本项目的产出，一段来自人类作品」
       ⇒ 评者本来就知道有一侧是我们产的；这里只是多余的框架提示。
       真问题在前两条，这条是顺手一起清掉。

    ⇒ 表上只留评者填得到的东西：题号 + 两列判断 + 备注。
    """
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_blind_packet

    corpus = [("现代都市", "他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。" * 40)]
    out_dir = tmp_path
    build_blind_packet(_rows(12), corpus, out_dir, provider=_StubJudge(set()))
    sheet = (out_dir / "blind_sheet.md").read_text(encoding="utf-8")
    assert "合计" not in sheet, sheet
    assert "k = ____" not in sheet, sheet
    assert "评者人数" not in sheet, sheet
    # 整张表上不该出现「我们」——那是汇总方的视角，评者没有这个视角
    # （题面里说了「一段来自本项目的产出」，所以这不是防泄露，是防多余框架）
    assert "我们" not in sheet, sheet
    # 题数要写在表头（评者要知道一共多少题），但不能算 n
    assert "判定 n" not in sheet, sheet
    assert "一共 **" in sheet, sheet
    # 写出来的题数要和表里的行数一致
    import re as _re
    m = _re.search(r"一共 \*\*(\d+) 题\*\*", sheet)
    assert m, sheet
    n_rows = len([ln for ln in sheet.splitlines()
                  if ln.startswith("| s") and "A / B" in ln])
    assert int(m.group(1)) == n_rows, (m.group(1), n_rows)


def test_记录表只留两列_另外两列在二选一下是废话(tmp_path):
    """⚠️ 「有 AI 腔」「猜 AI」这两列**信息量为零**。

    记录表要求**每列二选一** ⇒ 「猜 AI」与「更像人写」**必然取反**，
    「有 AI 腔」同理 ⇒ 问了三遍同一件事，还让评者多填 36 次。

    主人当场点破：「猜了哪个更像人写，另外那个不就是 AI 了么」。

    ⇒ 记录表只留两列：「更像人写」（判定用）+「主线更清」（**独立维度**：
      叙事清晰度，不是「像不像人写的」）。
    """
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_blind_packet

    corpus = [("现代都市", "他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。" * 40)]
    out_dir = tmp_path
    build_blind_packet(_rows(12), corpus, out_dir, provider=_StubJudge(set()))
    sheet = (out_dir / "blind_sheet.md").read_text(encoding="utf-8")
    header = [ln for ln in sheet.splitlines() if ln.startswith("| sample")][0]
    assert "更像这本书写出来的" in header and "主线更清" in header, header
    assert "有 AI 腔" not in sheet, sheet
    assert "猜 AI" not in sheet, sheet
    # 备注列留着 —— 评者认出出处时写在那一列，判定脚本会单独拎出来
    assert "备注" in header, header


def test_判定脚本按表头取列而不是按位置(tmp_path):
    """⚠️ 列一改，按位置取列就全错位（2026-10-31 删列时踩过）。

    ⇒ 解析必须**按表头名**；表头顺序换了也要对。
    """
    sys.path.insert(0, str(BACKEND))
    from verify_blind_eval import parse_sheet

    p = tmp_path / "sheet.md"
    p.write_text("| sample | 主线更清 | 更好看 | 备注 |\n"
                 "|---|---|---|---|\n"
                 "| s01 | B | A | 认出出处 |\n", encoding="utf-8")
    rows = parse_sheet(p)
    assert len(rows) == 1, rows
    # 规范列名是「更好看」；旧列名「更像人写」映射到同一列
    assert rows[0]["更像这本书写出来的"] == "A", rows
    assert rows[0]["主线更清"] == "B", rows
    assert rows[0]["备注"] == "认出出处", rows


def test_判定脚本用第二列做交叉分析(tmp_path):
    """第二列不能是**摆设** —— 判定脚本要拿它出交叉表。

    「主线更清」与「更像人写」**指向不同侧**的那些题 = 诊断信息：
    我们的产出是「主线清楚但不像人写」（语言层的问题），
    还是「像人写但主线乱」（结构层的问题）。
    """
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_blind_packet

    corpus = [("现代都市", "他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。" * 40)]
    out_dir = tmp_path
    build_blind_packet(_rows(12), corpus, out_dir, provider=_StubJudge(set()))
    key = json.loads((out_dir / "blind_key.json").read_text(encoding="utf-8"))
    ids = [it["sample_id"] for it in key["items"]]

    # 人写列全选人类侧那一列；主线更清列全选我们那一列 ⇒ 全部「分裂」
    # （密封文件里 slot_A/slot_B 记的是「这一侧是 ours 还是 human」）
    def _letter(it, want):
        """want='ours'/'human' ⇒ 该侧在题面里的字母（A/B）。"""
        if it["slot_A"] == want:
            return "A"
        return "B"

    human_side = {it["sample_id"]: _letter(it, "human") for it in key["items"]}
    ours_side = {it["sample_id"]: _letter(it, "ours") for it in key["items"]}
    lines = ["| sample | 更像人写 | 主线更清 | 备注 |", "|---|---|---|---|"]
    for sid in ids:
        lines.append(f"| {sid} | {human_side[sid]} | {ours_side[sid]} | |")
    rec = out_dir / "blind_sheet.md"
    rec.write_text("\n".join(lines) + "\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(BACKEND / "verify_blind_eval.py"),
         "--sheet", str(rec), "--key", str(out_dir / "blind_key.json")],
        capture_output=True, text=True, encoding="utf-8")
    # 人写列全选人类侧 ⇒ k=0 ⇒ 判定为「可被识别为 AI」⇒ 退出码 1（这是**正确**行为）
    assert proc.returncode == 1, f"退出码 {proc.returncode}\n{proc.stdout}"
    assert "两列交叉" in proc.stdout, proc.stdout
    assert f"指向不同侧**的题 = {len(ids)}/{len(ids)}" in proc.stdout, proc.stdout


# --------------------------------------------------------------------------- #
# 题面级复查的**假阳性**（2026-10-31 实测：18 题包卡在假阳性上，题面发不出去）
# --------------------------------------------------------------------------- #

class _ResidualStub:
    """只会回答「题面里还剩哪些专名」的假 provider。"""

    def __init__(self, reported):
        self.reported = list(reported)
        self.calls = 0

    def chat(self, system, user):  # noqa: ARG002
        self.calls += 1
        return json.dumps(self.reported, ensure_ascii=False)


def test_占位符不被当成残留专名():
    """模型把已经遮好的「＃＃＃」报回来 —— 那不是专名，遮它只会越遮越长。"""
    sys.path.insert(0, str(BACKEND))
    from acceptance_lib import residual_proper_nouns

    text = "他＃＃＃站在门口，雨还在下。"
    p = _ResidualStub(["＃＃＃", "＃＃"])
    out = residual_proper_nouns(text, p)
    assert out == [], out


def test_带杂质字符的名字照样遮掉():
    """「灶|房」：竖线是抓取残留。

    ⚠️ 这种**不能丢** —— 丢掉就等于让竖线原样留在题面上，
    评者一看就知道这文本被处理过（还会以为是机器那边被处理了 ⇒ 反而偏向另一侧）。
    遮掉反而干净：等长占位符会把竖线一起吃掉。
    """
    sys.path.insert(0, str(BACKEND))
    from acceptance_lib import residual_proper_nouns

    text = "灶|房里没有人，门是虚掩着。"
    p = _ResidualStub(["灶|房"])
    assert residual_proper_nouns(text, p) == ["灶|房"]


def test_杂质字符加超长串被丢掉():
    """模型偶尔会把一整句话抄回来（长度 > 6 且带杂质字符）—— 那不是名字。

    ⚠️ 这条也是回归护栏：过滤器的长度判断一旦写错（比如少了常量定义），
    这里就会抛 NameError，整包以「复查失败」作废 —— 一次改动毁掉一整轮出包。
    """
    sys.path.insert(0, str(BACKEND))
    from acceptance_lib import residual_proper_nouns

    text = "他说了一句很长的话，长到不像一个名字，里面还有竖线|。"
    p = _ResidualStub(["很长很长很长的一句话|"])
    assert residual_proper_nouns(text, p) == []


def test_两字切串残渣被丢掉():
    """《道途魔典》「他带柳入林而入」里的「带柳」是跨词残渣，不是人名。"""
    sys.path.insert(0, str(BACKEND))
    from acceptance_lib import residual_proper_nouns

    text = "他带柳入林而入，只见有个少年站在那里。"
    p = _ResidualStub(["带柳"])
    assert residual_proper_nouns(text, p) == []


def test_三字真地名不被当成残渣丢掉():
    """⚠️ 反向护栏：「藏经阁」的「藏」也在动词表里 —— 若按动词首字一刀切，
    真地名会被当成残渣丢掉 ⇒ **漏遮** ⇒ 真名暴露出处。"""
    sys.path.insert(0, str(BACKEND))
    from acceptance_lib import residual_proper_nouns

    text = "他推开藏经阁的门，里面全是灰。"
    p = _ResidualStub(["藏经阁"])
    assert residual_proper_nouns(text, p) == ["藏经阁"]


def test_安全闸门不缓存否定结论(tmp_path):
    """⚠️ 安全闸门**不能缓存否定结论**。

    踩过的坑（2026-10-31）：模型是非确定性的 —— 同一段文字问两次，
    答案可能不同（实测 s06 的「麻子」第一次没报、第二次报了）。
    一旦「这段是干净的」被缓存下来，之后每次重建都白捡这个**假阴性**：
    题面上留着真名，却以为已经查过了。
    """
    sys.path.insert(0, str(BACKEND))
    from acceptance_lib import residual_proper_nouns

    text = "他带柳入林而入，只见有个少年站在那里。"
    cache_path = tmp_path / "resid.json"
    p = _ResidualStub([])
    residual_proper_nouns(text, p, cache_path=cache_path)
    residual_proper_nouns(text, p, cache_path=cache_path)
    assert p.calls == 2, "同一段文字问两次 ⇒ 必须真的问两次，不能吃缓存"
    assert not cache_path.exists(), "安全闸门不该写判定缓存"

