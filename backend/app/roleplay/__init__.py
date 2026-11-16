"""角色对话：证据检索、来源标注、对话编排。

与「写正文」的两条路径是分开的：
- `original` / `continuation` 走 `generation.engine`，产物是正文段、落库；
- 这里的**角色对话**产物是对话记录，不进 `generations`。
"""
