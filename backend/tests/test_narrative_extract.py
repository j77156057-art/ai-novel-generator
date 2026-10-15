"""P2 叙事档案提炼：test_narrative_extract.py

覆盖：
- 采样策略（均匀采样、避开早期章节）
- 证据验证（伪规则被拦掉）
- JSON 解析容错（```json 包裹、裸 {...} 块）
- 指纹缓存（source_ids + narrative_version）
- 无参考语料时返回 None
- describe_narrative 输出确定性、每维度限量
- 安全包装不抛异常
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.style.narrative import (
    NARRATIVE_DIMENSIONS,
    NARRATIVE_VERSION,
    _parse_extract_result,
    _sample_chapters,
    _validate_evidence,
    describe_narrative,
    narrative_fingerprint,
    project_narrative_safe,
)

FAILED = []
PASSED = []


def check(name, cond):
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(name)
        print(f"  FAIL: {name}")


class FakeProvider:
    """模拟 LLM provider，返回预设的 JSON 结果。"""

    def __init__(self, responses=None):
        self.responses = responses or []
        self.call_count = 0

    def chat(self, system, user):
        self.call_count += 1
        if self.responses:
            return self.responses[min(self.call_count - 1, len(self.responses) - 1)]
        return "{}"


def main():
    # ── 1. _sample_chapters：均匀采样 + 避开早期 ──
    chapters = [(i, f"第{i}章内容") for i in range(1, 101)]  # 100 章
    sampled = _sample_chapters(chapters, max_chapters=30)
    check("采样数量不超过上限", len(sampled) <= 30)
    check("避开早期章节", all(c[0] > 33 for c in sampled))  # 取后 2/3
    check("采样结果非空", len(sampled) > 0)

    # 少量章节全部返回
    small = [(i, f"第{i}章") for i in range(1, 11)]
    check("少量章节全部返回", _sample_chapters(small, max_chapters=30) == small)

    # ── 2. _validate_evidence：证据验证 ──
    chunk = "他抬起头，看着远处的山口，风从那里吹过来，带着一点凉意。她把斗篷裹紧了些，没有说话。"
    # 证据在原文中
    check("证据在原文中通过", _validate_evidence("对白简短（证据：她没有回答，只是握紧了手中的匕首）", chunk) or
          _validate_evidence("外貌描写从动作入手（证据：她把斗篷裹紧了些，没有说话）", chunk))
    # 证据不在原文中
    check("证据不在原文中被拒", not _validate_evidence("角色总是微笑（证据：他露出了灿烂的笑容）", chunk))
    # 无证据标记但规则本身是原文句子——通过
    check("原文引用直接验证通过", _validate_evidence("她把斗篷裹紧了些，没有说话", chunk))
    # 无证据标记且规则不是原文句子——被拒
    check("编造规则被拒", not _validate_evidence("这是一个完全编造的规则，原文中不存在", chunk))
    # 证据太短被拒
    check("证据太短被拒", not _validate_evidence("规则（证据：他）", chunk))

    # ── 3. _parse_extract_result：JSON 解析容错 ──
    # 直接 JSON
    r1 = _parse_extract_result('{"人物描写手法": ["规则1"]}')
    check("直接 JSON 解析", r1.get("人物描写手法") == ["规则1"])

    # ```json 包裹
    r2 = _parse_extract_result('```json\n{"对白风格": ["规则2"]}\n```')
    check("```json 包裹解析", r2.get("对白风格") == ["规则2"])

    # 裸 {...} 块（前面有文字）
    r3 = _parse_extract_result('以下是分析结果：\n{"段落编排": ["规则3"]}\n以上。')
    check("裸 {...} 块解析", r3.get("段落编排") == ["规则3"])

    # 无法解析
    r4 = _parse_extract_result("完全不是 JSON")
    check("无法解析返回空 dict", r4 == {})

    # ── 4. narrative_fingerprint ──
    fp1 = narrative_fingerprint([1, 2])
    fp2 = narrative_fingerprint([2, 1])  # 顺序不同应归一
    check("指纹 source_ids 排序归一", fp1 == fp2)
    check("指纹含 narrative_version", fp1.get("narrative_version") == NARRATIVE_VERSION)

    # ── 5. describe_narrative ──
    profile = {
        "rules": {
            "人物描写手法": [
                {"rule": "规则A（证据：原文A）"},
                {"rule": "规则B（证据：原文B）"},
                {"rule": "规则C（证据：原文C）"},
                {"rule": "规则D（证据：原文D）"},
            ],
            "对白风格": [
                {"rule": "规则E（证据：原文E）"},
            ],
            "节奏控制": [],  # 空维度应跳过
        },
    }
    desc = describe_narrative(profile, max_rules_per_dim=3)
    check("describe 非空", bool(desc))
    check("describe 含人物描写手法", "人物描写手法" in desc)
    check("describe 含对白风格", "对白风格" in desc)
    check("describe 跳过空维度", "节奏控制" not in desc)
    check("describe 每维度限量", desc.count("规则") <= 4)  # 3 + 1
    # 确定性：同一档案两次调用逐字节相同
    check("describe 确定性", describe_narrative(profile) == describe_narrative(profile))
    # 无档案返回空
    check("无档案返回空", describe_narrative(None) == "")
    check("空 rules 返回空", describe_narrative({"rules": {}}) == "")

    # ── 6. project_narrative_safe：无参考语料返回 None ──
    class FakeProject:
        def __init__(self, ids):
            self.reference_novel_ids = ids
            self.style_narrative = None

    class FakeDB:
        def commit(self): pass
        def rollback(self): pass

    proj = FakeProject([])
    check("无参考语料返回 None", project_narrative_safe(FakeDB(), proj) is None)

    # 有缓存时直接返回
    cached = {"fingerprint": narrative_fingerprint([1]), "rules": {}}
    proj2 = FakeProject([1])
    proj2.style_narrative = cached
    result = project_narrative_safe(FakeDB(), proj2)
    check("有缓存直接返回", result == cached)

    # ── 7. 维度清单 ──
    check("维度清单非空", len(NARRATIVE_DIMENSIONS) == 14)
    check("维度含人物描写手法", "人物描写手法" in NARRATIVE_DIMENSIONS)
    check("维度含对白风格", "对白风格" in NARRATIVE_DIMENSIONS)

    # ── 8. 反向禁忌 ──
    from app.style.narrative import FORBIDDEN_DIMENSIONS
    check("反向禁忌维度非空", len(FORBIDDEN_DIMENSIONS) == 4)
    check("反向禁忌含禁用转折词", "禁用转折词" in FORBIDDEN_DIMENSIONS)

    # describe_narrative 含反向禁忌
    profile_with_forbidden = {
        "rules": {
            "人物描写手法": [{"rule": "规则A（证据：原文A）"}],
        },
        "forbidden": {
            "禁用转折词": ["就在这时", "突然"],
            "禁用情绪直陈": ["心中充满了", "眼中闪过一丝"],
        },
    }
    desc2 = describe_narrative(profile_with_forbidden)
    check("describe 含反向禁忌", "反向禁忌" in desc2)
    check("describe 含禁用转折词", "就在这时" in desc2)
    check("describe 含禁用情绪直陈", "心中充满了" in desc2)

    # 只有 forbidden 没有 rules 也能渲染
    profile_only_forbidden = {
        "rules": {},
        "forbidden": {"禁用转折词": ["就在这时"]},
    }
    desc3 = describe_narrative(profile_only_forbidden)
    check("只有 forbidden 也能渲染", "反向禁忌" in desc3 and "就在这时" in desc3)

    print(f"\n结果：{len(PASSED)}/{len(PASSED) + len(FAILED)} 通过")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
