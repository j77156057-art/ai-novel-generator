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

实测（`D:\\Temp\\probe_traces.py`）：逐 400 字样本算年代标记，
只检出 2/6；把整包 2400 字拼起来算，检出 100%。
单样本里标记词出现零几次，分位数噪声压不住信号。
"""
from __future__ import annotations

import json
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
    recall_proper_nouns,
    shape_candidates,
)


# --------------------------------------------------------------------------- #
# 假 provider：只判「我认识的这几个词是人名」，其余一律 null
# --------------------------------------------------------------------------- #

class _StubJudge:
    """按词判定专名的假 provider（只用于测流程，不测判定质量）。"""

    def __init__(self, names: set[str]):
        self.names = names
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        words = [w for w in user.split("：", 1)[-1].split("、") if w]
        arr = [{"w": w, "t": "character" if w in self.names else None} for w in words]
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


# --------------------------------------------------------------------------- #
# 5. 端到端闸门：不是盲评包 ⇒ 判定脚本拒绝出统计结论（退出码 2）
# --------------------------------------------------------------------------- #

def _rows(n: int = 12) -> list[dict]:
    body = ("林夜把苏晚给的铜钥匙攥在手里，陆见微站在柜台那头看着他。"
            "他把手机揣回兜里，出了便利店，雨还在下，路灯把积水照成一片。"
            "监控和卷宗都调出来，地铁末班车上他还在刷视频。")
    return [{"seg": i + 1, "text": body * 3, "error": None} for i in range(n)]


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
