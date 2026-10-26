"""E 任务验收：抽取器 3 字名 / 噪声护栏回归。

8 个 fixture 全部 PASS = 任务完成。失败回退：把 fail 的 fixture + 当前 3 字段
值/反例交给收口人判断，不要硬塞。
"""
import json
import pathlib
import re
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.kg.extractor import RuleBasedExtractor  # noqa: E402

FIX = pathlib.Path(__file__).resolve().parent / "tests" / "fixtures" / "extractor_v2_cases.json"


def _extract_chars(text: str) -> list[str]:
    """用固定分句 chunk 验证抽取器，不依赖 ORM/数据库。

    入图门槛要求角色跨两个 chapter 出现；fixture 将句子映射为递增的
    chapter_no，避免把测试结果绑定到数据库切片长度或本地 ORM 环境。
    """
    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?；;])", text) if s.strip()]
    chunks = [
        SimpleNamespace(chapter_no=i + 1, text=sentence)
        for i, sentence in enumerate(sentences)
    ]
    result = RuleBasedExtractor().extract(chunks)
    return list(result.characters)


def run() -> int:
    cases = json.loads(FIX.read_text(encoding="utf-8"))
    fail = []
    for i, c in enumerate(cases):
        chars = _extract_chars(c["text"])
        miss = [n for n in c["expect_characters"] if n not in chars]
        noise = [n for n in c["expect_no_noise"] if n in chars]
        ok = (not miss) and (not noise)
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] case {i}: {c['name']}")
        if not ok:
            print(f"        抽到: {chars}")
            if miss:
                print(f"        漏:   {miss}")
            if noise:
                print(f"        噪声: {noise}")
            fail.append(c["name"])
    print("=" * 60)
    if fail:
        print(f"FAIL  {len(fail)}/{len(cases)}: {fail}")
        return 1
    print(f"OK    {len(cases)}/{len(cases)} fixture 全过")
    return 0


if __name__ == "__main__":
    sys.exit(run())
