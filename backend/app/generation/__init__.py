"""M4 分段生成环：提示词组装 + 续写 + 段级总结 + 入库。

对外入口见 engine.generate_next_segment / generate_loop / build_context。
"""

from app.generation.engine import (
    build_context,
    generate_loop,
    generate_next_segment,
)
from app.generation.prompts import build_system_prompt, build_user_prompt

__all__ = [
    "build_context",
    "build_system_prompt",
    "build_user_prompt",
    "generate_loop",
    "generate_next_segment",
]
