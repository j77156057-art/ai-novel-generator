"""README 示例自检：确认文档里的 curl 端点真实存在、引用的文件存在。

跑法（backend 目录）：
    python scripts/verify_readme_examples.py

不启动服务、不连数据库：只导入 FastAPI app 并比对路由表。
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.routing import APIRoute

ROOT = Path(__file__).resolve().parents[2]
BACKEND = Path(__file__).resolve().parents[1]
# 直接 `python scripts/...` 时 sys.path[0] 是 scripts/，补上 backend/ 才能导入 app。
sys.path.insert(0, str(BACKEND))
README = ROOT / "README.md"
BASH_BLOCK = re.compile(r"```bash\s*\n(.*?)```", re.S)
URL = re.compile(r"https?://[^\s\"'\\]+")


def normalize(path: str) -> str:
    """把 {pid} / {project_id} 这类路径参数统一成 {} 再比较。"""
    return re.sub(r"\{[^}/]+\}", "{}", path).rstrip("/")


def collect_examples() -> list[tuple[int, str]]:
    """返回 [(README 行号, 规范化路径)]。"""
    text = README.read_text(encoding="utf-8")
    found: list[tuple[int, str]] = []
    for block in BASH_BLOCK.finditer(text):
        line_no = text[: block.start()].count("\n") + 1
        for offset, line in enumerate(block.group(1).splitlines()):
            if "curl" not in line:
                continue
            match = URL.search(line)
            if not match:
                continue
            path = urlsplit(match.group(0)).path
            if not path.startswith("/api"):
                continue
            found.append((line_no + offset, normalize(path)))
    return found


def main() -> int:
    if not README.exists():
        print(f"FAIL 找不到 README：{README}")
        return 1

    app = importlib.import_module("app.main").app
    existing = {
        normalize(route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
    }

    failures: list[str] = []
    examples = collect_examples()
    print(f"README 示例自检：共 {len(examples)} 条 curl")
    for line_no, path in examples:
        ok = path in existing
        print(f"{'PASS' if ok else 'FAIL'}  {path}  (README:{line_no})")
        if not ok:
            failures.append(f"README:{line_no} {path}")

    required_paths = [
        "data/seed",
        "backend/requirements.txt",
        "frontend/package-lock.json",
    ]
    for relative in required_paths:
        ok = (ROOT / relative).exists()
        print(f"{'PASS' if ok else 'FAIL'}  文件存在：{relative}")
        if not ok:
            failures.append(f"缺少 {relative}")

    if failures:
        print(f"\n结果：{len(examples) + len(required_paths) - len(failures)} 通过，"
              f"{len(failures)} 失败")
        for item in failures:
            print("-", item)
        return 1

    print(f"\n结果：全部通过（{len(examples)} 条 curl + {len(required_paths)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
