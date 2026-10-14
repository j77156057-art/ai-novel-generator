"""pytest 配置：把 backend/ 加入 sys.path，让 `from app.xxx import` 在测试里可用。"""
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
