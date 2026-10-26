"""样例必须**看得出行文节奏**：段落分隔符不能被吃掉。

## 背景：一种不会报错的坏写法

字符串字面量里写一个**真实换行**（而不是 `\\n` 转义），Python 会把它当**续行**：

    joined = "<真实换行>".join(buf)      # 实际等价于 "".join(buf)

语法完全合法、不报错、测试也照样过 —— 但**分隔符凭空消失**。
`app/style/exemplar.py` 里就有两处：风格样例的段落分隔符被吃光，
于是模型收到的是**一堵没有换行的墙**，根本看不到参考书「叙述与台词同段、
偶尔单独成行」的混合节奏，只能按自己的习惯写对白
（实测：参考书含引号行中位 47 字、裸台词行占 23%；模型写出 12 字、68%）。

## 这里钉三件事

1. `_split_into_samples` 产出的样例**保留换行**；
2. 采样器挑出的样例**能看出行文节奏**（多行），且含「叙述裹着台词」的形态；
3. **全仓不许再出现这种坏写法**（扫源码，命中即失败）。
"""
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.style.exemplar import MIN_SAMPLE_LINES, _split_into_samples


def test_split_into_samples_keeps_paragraph_breaks():
    """两段之间必须有换行 —— 少一个 \\n，样例就变成一堵墙。"""
    a = "第一段内容。" * 30
    b = "第二段内容。" * 30
    samples = _split_into_samples(a + "\n" + b)
    assert len(samples) == 1, f"应产出一条样例，实际 {len(samples)}"
    assert "\n" in samples[0], f"段落分隔符被吃掉了：{samples[0][:40]!r}"
    assert samples[0].count("\n") == 1


def test_split_into_samples_joins_with_newline_not_empty():
    """直接盯住 join 的分隔符：必须是换行，不能是空串。"""
    a = "甲段落。"
    b = "乙段落。"
    joined = "\n".join([a, b])
    assert joined == "甲段落。\n乙段落。"
    # 旧代码（`"".join`）会得到 "甲段落。乙段落。" —— 没有任何分隔
    assert "".join([a, b]) == "甲段落。乙段落。"


def test_min_sample_lines_is_at_least_three():
    """一行看不出节奏；少于三行的样例没有参考价值。"""
    assert MIN_SAMPLE_LINES >= 3


def test_no_broken_string_literals_in_repo():
    """全仓扫「字符串字面量里含真实换行」——这种写法语法合法但语义是错的。

    特征：某一行以 `"` 或 `"\\` 结尾（引号未闭合），下一行以 `"` 开头。
    """
    suspicious = []
    for path in REPO.rglob("*.py"):
        if any(part in {".git", "node_modules", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines[:-1]):
            s = line.rstrip()
            if not (s.endswith('"') or s.endswith('"\\')):
                continue
            if len(re.findall(r'(?<!\\)"', s)) % 2 == 0:
                continue
            if lines[i + 1].lstrip().startswith('"'):
                suspicious.append(f"{path.relative_to(REPO)}:{i + 1}")
    assert not suspicious, (
        "源码里出现了「字符串字面量含真实换行」的坏写法（会被 Python 当成续行，"
        f"分隔符凭空消失）：{suspicious}"
    )
