"""test_name_audit.py —— 角色名 LLM 审计（v2.265）

## 设计

审计**只标记不删除**——它本身会误判（雪中「红薯」是丫鬟名却被标为
普通名词），所以结果只写进 `voice_name_audit` 字段供人工复核。

## 关键测试点

1. 真角色不被标（徐骁 / 姜泥 —— 早期问法把它们标成可疑，是核心 bug）
2. 虚词被标（后来 / 南北 / 老人）
3. **只标记不删除**：调用方不据此修改规则列表
4. 调用失败不阻断（返回空结果）
5. LLM 编造的词被过滤（只接受清单里的）

用 fake provider，不真调 LLM。
"""
import sys

sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.style.narrative import _audit_character_names  # noqa: E402


class FakeProvider:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []

    def chat(self, system, user):
        self.calls.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def voices_of(names):
    return [{"rule": f"{n}：自称「我」语气平淡"} for n in names]


REAL_NAMES = ["徐凤年", "王仙芝", "徐骁", "姜泥", "李淳罡", "褚禄山",
              "曹长卿", "邓太阿", "宋岩", "曹嵬", "冯岭", "黄青"]


#: 能通过廉价预过滤的词（不像姓+名、不是具体化称谓）——这些才会送 LLM。
#: 姓+名的词（徐骁/姜泥/轩辕）在预过滤阶段就被跳过了，不参与审计。
AUDIT_CANDIDATES = ["后来", "南北", "老人", "洛阳", "青鸟", "红薯"]


def test_a_marks_detected():
    """A: 正常情况——模型标出的词原样返回。"""
    p = FakeProvider('{"not_person": ["后来", "南北"]}')
    res = _audit_character_names(p, voices_of(AUDIT_CANDIDATES))
    assert res["suspicious"] == ["后来", "南北"], res
    assert res["checked"] == len(AUDIT_CANDIDATES)
    assert res["skipped"] is False


def test_b_no_false_positive_on_real_names():
    """B: 真角色名不进可疑清单（只要模型没乱标）。"""
    p = FakeProvider('{"not_person": []}')
    res = _audit_character_names(p, voices_of(REAL_NAMES))
    assert res["suspicious"] == [], res


def test_c_filters_hallucinated():
    """C: 模型编造的词被过滤掉（只接受清单里存在的）。"""
    p = FakeProvider('{"not_person": ["后来", "不存在的词", "张三"]}')
    res = _audit_character_names(p, voices_of(AUDIT_CANDIDATES))
    assert res["suspicious"] == ["后来"], f"编造词没被过滤: {res}"


def test_d_llm_failure_not_blocking():
    """D: LLM 调用失败时返回空结果，不抛异常（不阻断蒸馏）。"""
    p = FakeProvider(RuntimeError("网络超时"))
    res = _audit_character_names(p, voices_of(AUDIT_CANDIDATES))
    assert res["suspicious"] == []
    assert res["checked"] > 0


def test_e_bad_json_not_blocking():
    """E: 模型返回非 JSON 时不崩，返回空结果。"""
    p = FakeProvider("我觉得这些名字都挺好的，没有可疑的。")
    res = _audit_character_names(p, voices_of(REAL_NAMES))
    assert res["suspicious"] == []


def test_f_too_few_names_skipped():
    """F: 名字太少（< 3）时跳过审计，省一次 LLM 调用。"""
    p = FakeProvider('{"not_person": []}')
    res = _audit_character_names(p, voices_of(["徐凤年", "王仙芝"]))
    assert res["skipped"] is True
    assert not p.calls, "名字太少不该调 LLM"


def test_g_max_names_truncation():
    """G: 名字超过 max_names 时截断并标记 skipped。"""
    many = [f"角色{i}" for i in range(100)]
    p = FakeProvider('{"not_person": []}')
    res = _audit_character_names(p, voices_of(many), max_names=80)
    assert res["checked"] == 80
    assert res["skipped"] is True


def test_i_surname_names_skip_audit():
    """I: 姓+名的词在预过滤阶段就跳过，不浪费 LLM 调用。

    这是 v2.265 的关键优化：glm-4-flash 对「徐骁」「姜泥」判断不稳定
    （同一输入两次，一次保留一次标可疑），把明显是人名的送进去只会
    制造噪声。
    """
    p = FakeProvider('{"not_person": []}')
    names = ["徐凤年", "徐骁", "姜泥", "王仙芝", "李淳罡"]
    res = _audit_character_names(p, voices_of(names))
    assert not p.calls, f"姓+名的词不该送审，实际调了 {len(p.calls)} 次"
    assert res["checked"] == 0


def test_j_compound_surname_skip_audit():
    """J: 复姓（轩辕/赫连/澹台）也跳过审计。

    这些在雪中悍刀行里是真角色。早期只收单字姓，导致它们被送审且被
    glm-4-flash 误标成「普通名词」——加复姓白名单后修复。
    """
    p = FakeProvider('{"not_person": []}')
    names = ["轩辕青锋", "赫连武威", "澹台平静", "公孙杨", "慕容"]
    res = _audit_character_names(p, voices_of(names))
    assert not p.calls, f"复姓不该送审，实际调了 {len(p.calls)} 次"
    assert res["checked"] == 0


def test_k_specific_title_skip_audit():
    """K: 具体化称谓（老和尚/洪姨）跳过审计。"""
    p = FakeProvider('{"not_person": []}')
    res = _audit_character_names(p, voices_of(["老和尚", "老夫子", "洪姨", "铁匠"]))
    assert not p.calls


def test_h_only_marks_never_deletes():
    """H: 审计不改动传入的 voices 列表（只标记不删除的契约）。"""
    names = REAL_NAMES + ["后来"]
    voices = voices_of(names)
    original = [v["rule"] for v in voices]
    p = FakeProvider('{"not_person": ["后来", "徐骁"]}')
    _audit_character_names(p, voices)
    assert [v["rule"] for v in voices] == original, "审计不能改动规则列表"


if __name__ == "__main__":
    tests = [
        ("A 标记生效", test_a_marks_detected),
        ("B 真角色不误标", test_b_no_false_positive_on_real_names),
        ("C 编造词被过滤", test_c_filters_hallucinated),
        ("D LLM 失败不阻断", test_d_llm_failure_not_blocking),
        ("E 坏 JSON 不崩", test_e_bad_json_not_blocking),
        ("F 名字太少跳过", test_f_too_few_names_skipped),
        ("G 超量截断", test_g_max_names_truncation),
        ("H 只标记不删除", test_h_only_marks_never_deletes),
        ("I 姓+名跳过审计", test_i_surname_names_skip_audit),
        ("J 复姓跳过审计", test_j_compound_surname_skip_audit),
        ("K 具体化称谓跳过", test_k_specific_title_skip_audit),
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
