# -*- coding: utf-8 -*-
"""检查 :8000 上跑的进程是否旧于当前代码（可直接进发布流程）。

退出码：
  0 = 进程不旧于代码（``started_at >= build_time``）
  1 = 陈旧（代码比进程新，需要重启）
  2 = 连不上 / 返回体缺字段（无法判定，按失败处理）

用法：
  python backend/check_healthz.py
  python backend/check_healthz.py --url http://127.0.0.1:8000/healthz
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def fetch(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查后端进程是否旧于当前代码")
    parser.add_argument("--url", default="http://127.0.0.1:8000/healthz")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    try:
        payload = fetch(args.url, args.timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"FAIL 无法读取 {args.url}: {exc}")
        return 2

    try:
        started = float(payload["started_at_epoch"])
        build = float(payload["build_time_epoch"])
    except (KeyError, TypeError, ValueError):
        print("FAIL /healthz 返回体缺少 started_at_epoch / build_time_epoch")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    print(f"pid={payload.get('pid')} git={payload.get('git_sha')} dirty={payload.get('dirty')}")
    print(f"started_at={payload.get('started_at')}")
    print(f"build_time={payload.get('build_time')} ({payload.get('build_time_file')})")

    lag = build - started
    if lag > 0:
        print(f"STALE 代码比进程新 {lag:.3f}s，需要重启")
        return 1
    print(f"OK 进程不旧于代码（余量 {-lag:.3f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
