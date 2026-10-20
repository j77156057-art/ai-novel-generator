"""test_voice_char_name_filter.py —— 角色声音条目污染防护

数据来源：docs/v236_xuezhong_8k.json（雪中悍刀行 990 章真链路）
那次的 29 条角色声音里混进 18 条垃圾，本测试锁住修复后的行为。

三类污染：
1. 规则文本被当角色名（LLM 把另一维度的规则塞进角色声音数组）
2. 称谓/地名/泛称（江湖/公子/师父/皇帝/家伙/许多/高手…）
3. 同一份声音内容被复制给多个人名（LLM 长 JSON 输出的复制粘贴错误）
"""
import json
import pathlib
import sys

sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.style.narrative import (  # noqa: E402
    _VOICE_STOP_WORDS,
    _dedup_character_voices,
    _is_valid_voice_char_name,
)

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "docs" / "v236_xuezhong_8k.json"


def test_a_rule_text_as_char_name():
    """A: 规则文本不能被当角色名。

    雪中悍刀行真链路第 0 条：
    '通过角色的语言和行动，展现其个性特点（证据：徐凤年目不斜视...）'
    LLM 把「人物描写手法」维度的规则塞进了「角色声音词典」。
    """
    bad = "通过角色的语言和行动，展现其个性特点（证据"
    assert not _is_valid_voice_char_name(bad), f"{bad!r} 不该通过"
    # 长度超 4 字就拦得住
    assert len(bad) > 4


def test_b_titles_and_place_names():
    """B: 称谓/地名/泛称全部拦掉（这 15 个来自真链路实测）。"""
    garbage = [
        "江湖", "公子", "师父", "皇帝", "家伙", "许多", "高手",
        "广陵", "春秋", "武当", "王爷", "白衣", "西楚", "可惜", "后者",
    ]
    survivors = [g for g in garbage if _is_valid_voice_char_name(g)]
    assert not survivors, f"这些不该通过：{survivors}"


def test_c_real_names_pass():
    """C: 真实角色名必须通过（不能过度过滤）。"""
    real = ["徐凤年", "王仙芝", "徐北枳", "曹长卿", "陈芝豹",
            "邓太阿", "褚禄山", "李淳罡", "顾剑棠", "徐骁", "姜泥"]
    failed = [r for r in real if not _is_valid_voice_char_name(r)]
    assert not failed, f"真实角色名被误杀：{failed}"


def test_d_all_unmentioned_dropped():
    """D: 整条全是「未提及」的空壳规则要被丢弃。

    雪中悍刀行的姜泥/许多/高手都是这种：LLM 没提取到任何内容，
    填满占位符。它对生成零信息量，却占掉角色声音名额。
    """
    voices = [
        {"rule": "姜泥： 自称「未提及」 口头禅「未提及」 句长混合 语气未提及 "
                 "节奏未提及 关系动态：未提及具体关系动态 代表台词：「未提及」"},
        {"rule": "徐凤年： 自称「我」 口头禅「小家伙」 句长短句为主 语气轻佻 "
                 "节奏抢话 关系动态：对萧炎亲近 代表台词：「三十年河东」"},
    ]
    out = _dedup_character_voices(voices, canonical=[], alias={}, clean_fake=True)
    names = [v["rule"].split("：")[0] for v in out]
    assert "姜泥" not in names, f"全「未提及」条目应被丢弃，实际保留：{names}"
    assert "徐凤年" in names, "有实质内容的条目应保留"


def test_e_copy_paste_duplicate_removed():
    """E: 同一份声音内容被复制给多个人名时只保留一条。

    雪中悍刀行实测：褚禄山 / 李淳罡 / 武当 / 西楚 / 可惜 五条正文完全相同，
    内容都是褚禄山的台词。留着会让生成时把褚禄山的说话习惯安到别人身上。
    """
    body = ("自称「我」 口头禅「这玩意儿有啥看头、嘿嘿桀桀」 句长混合 "
            "语气自嘲，轻松 节奏停顿 关系动态：对徐凤年敌对 "
            "代表台词：「我褚禄山有洁癖，每天都要换一身华贵衣衫，喜豪奢」")
    voices = [
        {"rule": f"褚禄山： {body}"},
        {"rule": f"李淳罡： {body}"},
        {"rule": f"顾剑棠： {body}"},
    ]
    out = _dedup_character_voices(voices, canonical=[], alias={}, clean_fake=True)
    assert len(out) == 1, f"复制粘贴组应只留 1 条，实际 {len(out)}: {[v['rule'][:12] for v in out]}"
    assert out[0]["rule"].startswith("褚禄山"), "应保留第一条（LLM 原始顺序）"


def test_f_short_bodies_not_deduped():
    """F: 短正文撞车是正常的，不能被去重误杀。

    「语气冷淡」这种短语在两个角色身上出现很正常，
    去重只针对长正文（>= 40 字）的复制粘贴。
    """
    voices = [
        {"rule": "张三： 语气冷淡，句长短句 节奏停顿，关系动态：对李四疏远，代表台词：「嗯」"},
        {"rule": "李四： 语气冷淡，句长短句 节奏停顿，关系动态：对张三疏远，代表台词：「哦」"},
    ]
    out = _dedup_character_voices(voices, canonical=[], alias={}, clean_fake=True)
    assert len(out) == 2, f"短正文不该被去重，实际 {len(out)}"


def test_g_real_fixture_regression():
    """G: 用 v2.236 真链路数据回归——29 条进，垃圾清零。

    这是最重要的一条：不是合成数据，是雪中悍刀行 990 章真实产出。
    """
    if not FIXTURE.exists():
        print(f"  (跳过：fixture 不存在 {FIXTURE})")
        return
    narr = json.loads(FIXTURE.read_text(encoding="utf-8"))
    voices = narr.get("rules", {}).get("角色声音词典", [])
    if not voices:
        print("  (跳过：fixture 无角色声音)")
        return

    out = _dedup_character_voices(voices, canonical=[], alias={}, clean_fake=True)
    names = [v["rule"].split("：")[0].strip() for v in out]

    garbage = ["江湖", "公子", "师父", "皇帝", "家伙", "许多", "高手",
               "广陵", "春秋", "武当", "王爷", "白衣", "西楚", "可惜", "后者"]
    still = [g for g in garbage if g in names]
    assert not still, f"真链路数据过滤后仍残留垃圾：{still}"

    # 真角色至少保住核心 8 个
    core = ["徐凤年", "王仙芝", "徐北枳", "曹长卿", "陈芝豹", "邓太阿", "褚禄山", "徐骁"]
    kept = [c for c in core if c in names]
    assert len(kept) >= 7, f"核心角色只保住 {len(kept)}/8: {kept}"

    print(f"  真链路回归：{len(voices)} 条 → {len(out)} 条，核心角色 {len(kept)}/8")


if __name__ == "__main__":
    tests = [
        ("A 规则文本不当角色名", test_a_rule_text_as_char_name),
        ("B 称谓/地名/泛称拦截", test_b_titles_and_place_names),
        ("C 真实角色名通过", test_c_real_names_pass),
        ("D 全未提及条目丢弃", test_d_all_unmentioned_dropped),
        ("E 复制粘贴去重", test_e_copy_paste_duplicate_removed),
        ("F 短正文不去重", test_f_short_bodies_not_deduped),
        ("G 真链路数据回归", test_g_real_fixture_regression),
    ]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
    print(f"\n{passed}/{len(tests)} pass")
    sys.exit(0 if passed == len(tests) else 1)
