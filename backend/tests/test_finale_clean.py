"""完本感言/跨书简介清洗测试：test_finale_clean.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.corpus_health import (
    _find_finale_start,
    strip_finale_and_cross_novel,
)

FAILED = []
PASSED = []


def check(name, cond):
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(name)
        print(f"  FAIL: {name}")


def main():
    # ── 1. 完本感言清洗 ──
    # 前置足够长的正文，确保「大结局」落在文末 20% 区域
    filler = "南宫爵握着安小落的手，两人相视一笑，眼底满是温柔。\n\n" * 50
    text_with_finale = (
        filler
        + "从此以后，他们过上了幸福的生活。\n\n"
        + "——大结局——\n\n"
        + "各位可爱的亲亲们，半城无比痛心的向你们宣布《豪门宠婚》到这里就彻底完结了。\n"
        + "不管喜不喜欢能够一路陪伴到这里的都是真爱哦！\n"
        + "半城的新书《萌宝来袭》已经在香网连载，内容简介：五年前，莫名的被同父异母的妹妹替嫁。\n"
    )
    cleaned, report = strip_finale_and_cross_novel(text_with_finale)
    check("完本感言被删除", "大结局" not in cleaned)
    check("作者话被删除", "亲亲们" not in cleaned)
    check("新书预告被删除", "萌宝来袭" not in cleaned)
    check("正文保留", "南宫爵握着安小落的手" in cleaned)
    check("清洗报告含删除字数", report["removed"] > 0)
    check("清洗报告含命中", len(report["hits"]) > 0)

    # ── 2. 跨书简介清洗 ──
    text_with_cross = (
        "安小落走进房间，看见南宫爵正在等她。\n\n"
        "文中男主：简一集团执行总裁 雷厉风行杀伐果断，英俊高冷。\n"
        "文中女主：唐诗 唐家无人问津的长女，后成为简太太。\n"
        "这是一本集甜宠虐为一体的豪门小说哦。\n\n"
        "她走过去，轻轻抱住了他。\n"
    )
    cleaned2, report2 = strip_finale_and_cross_novel(text_with_cross)
    check("跨书简介男主被删除", "文中男主" not in cleaned2)
    check("跨书简介女主被删除", "文中女主" not in cleaned2)
    check("跨书简介标语被删除", "集甜宠虐为一体" not in cleaned2)
    check("跨书清洗后正文保留", "安小落走进房间" in cleaned2)
    check("跨书清洗后正文保留2", "轻轻抱住了他" in cleaned2)

    # ── 3. 正常文本不受影响 ──
    normal_text = (
        "他抬起头，看着远处的山口。\n\n"
        "风从那里吹过来，带着一点凉意。\n\n"
        "她把斗篷裹紧了些，没有说话。\n"
    )
    cleaned3, report3 = strip_finale_and_cross_novel(normal_text)
    check("正常文本不变", cleaned3 == normal_text)
    check("正常文本无删除", report3["removed"] == 0)

    # ── 4. _find_finale_start ──
    # 文末 20% 区域内有大结局 → 找到位置
    long_text = "正文内容。" * 100 + "\n\n——大结局——\n\n作者的话..."
    pos = _find_finale_start(long_text)
    check("找到完本位置", pos is not None and pos > len(long_text) * 0.5)

    # 没有完本标志 → None
    no_finale = "纯正文内容。" * 100
    check("无完本标志返回 None", _find_finale_start(no_finale) is None)

    # ── 5. 多种完本格式 ──
    for marker in ["大结局", "全文完", "完结", "全书完"]:
        t = "正文。" * 100 + f"\n\n——{marker}——\n\n作者的话"
        c, r = strip_finale_and_cross_novel(t)
        check(f"完本标志「{marker}」被识别", marker not in c)

    print(f"\n结果：{len(PASSED)}/{len(PASSED) + len(FAILED)} 通过")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
