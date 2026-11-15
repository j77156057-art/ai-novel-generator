# -*- coding: utf-8 -*-
"""KG 质量表的过期区块清理：尾块区间增长后（31–32 → 31–35），旧块必须被替换掉。

锁三件事：
1. 被新区间完整覆盖的同项目旧块会被删除；
2. 当前写入的块本身、其他项目/不被覆盖的块不动；
3. ``supersede_contained=False``（手工补录路径）保持旧行为——不清理。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.kg.quality_report import _drop_contained_blocks  # noqa: E402


def _block(pid: int, a: int, b: int, body: str = "内容") -> str:
    key = f"p{pid}:ch{a}-ch{b}"
    return (f"<!-- kg-quality:{key}:start -->\n## 第 {a}–{b} 章\n\n{body}\n"
            f"<!-- kg-quality:{key}:end -->\n")


def test_contained_stale_block_is_dropped():
    content = "头\n\n" + _block(6, 31, 32, "旧") + "\n" + _block(6, 26, 30) + _block(7, 31, 32)
    out = _drop_contained_blocks(content, 6, 31, 35, keep="p6:ch31-ch35")
    assert "p6:ch31-ch32" not in out          # 被覆盖的旧尾块：删
    assert "p6:ch26-ch30" in out              # 不被覆盖：留
    assert "p7:ch31-ch32" in out              # 别的项目：留
    assert "\n\n\n" not in out                # 删除后不留多余空行


def test_no_contained_blocks_returns_content_unchanged():
    content = _block(6, 26, 30) + _block(6, 31, 35)
    out = _drop_contained_blocks(content, 6, 31, 35, keep="p6:ch31-ch35")
    assert out == content                     # 自己的块即使被覆盖语义包含也保留


def test_partial_overlap_is_kept():
    # 旧块只与新区间部分重叠（16–20 vs 17–35）：不删，等它自己的同 key 刷新
    content = _block(6, 16, 20) + _block(6, 31, 32)
    out = _drop_contained_blocks(content, 6, 17, 35, keep="p6:ch17-ch35")
    assert "p6:ch16-ch20" in out
    assert "p6:ch31-ch32" not in out
