"""文风档案的「方法维度」名称表。

`generation.profiles`（提示词按维度注入）与 `style.layers`（四层档案的
method 层）用的是同一组维度名，此前两份常量逐字重复。放在这里避免改动
一侧忘另一侧；两个模块各自用自己习惯的名字导入。
"""
from __future__ import annotations

METHOD_DIMS: tuple[str, ...] = (
    "人物描写手法",
    "对白风格",
    "段落编排",
    "场景构建",
    "情绪表达",
    "节奏控制",
    "世界观呈现",
    "剧情编排模式",
    "伏笔与悬念设计",
    "节拍序列",
    "高频场景模板",
    "潜台词模式",
    "语序编排",
)

#: `style.layers` 里的叫法：method 层包含哪些维度键。
METHOD_KEYS = METHOD_DIMS
