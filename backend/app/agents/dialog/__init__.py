# -*- coding: utf-8 -*-
"""对话助手「重写」的段级快照（撤销 / 审计）。

包内只放两个模块：

- :mod:`app.agents.dialog.snapshot` —— 快照写入原语（pending → applied/failed）；
- :mod:`app.agents.dialog.rollback` —— 回滚（单段 / 整批）与派生重建编排。
"""
