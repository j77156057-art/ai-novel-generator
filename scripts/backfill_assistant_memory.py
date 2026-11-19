"""把历史对话回填成长期记忆（一次性运维脚本）。

背景：长期记忆是这一版才加上的，加之前的对话里其实已经说过不少稳定偏好
（人称、称呼、禁忌），但它们只躺在 `assistant_messages` 里，从未被抽取过。
本脚本按「用户问 → 助手答」配对重跑一遍抽取，把结果写进 `assistant_memories`。

## 用法（在仓库根执行即可，脚本会切到 backend/ 读 .env）

    backend\\venv\\Scripts\\python.exe scripts\\backfill_assistant_memory.py --project 6 --dry-run
    backend\\venv\\Scripts\\python.exe scripts\\backfill_assistant_memory.py --project 6

    --project   项目 id（必填；不设默认值，避免手滑写到别的书上）
    --limit     最多看最近多少轮问答（默认 200）
    --all       不按「只抽陈述句」的闸门过滤，每一轮都交给模型判断
    --dry-run   只打印会写什么，不落库

## 为什么不复用 `auto_extract`

`auto_extract` 只回「新增了哪些」，脚本还要区分「重复」与「抽不出」，
所以要自己拿 `extract_candidates` + `remember` 的结果做统计。抽取提示词、
解析与去重仍然共用同一份实现，不存在两套口径。

## 安全边界

- 只读 `assistant_messages`，只写 `assistant_memories`；
- 重复内容由 `remember()` 幂等跳过，条数到上限就停；
- `--dry-run` 完全不写库。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

# 让 `from app.xxx import` 可用，并切到 backend/（.env 与默认 sqlite 路径都按它解析）。
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

try:  # Windows 控制台按 UTF-8 输出，避免中文变问号
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import select  # noqa: E402

from app import assistant_memory as am  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import AssistantMessage, Project  # noqa: E402
from app.routers import settings as settings_router  # noqa: E402


def load_pairs(db, project_id: int, limit: int) -> list[tuple[str, str]]:
    """把 plain 消息按「用户问 → 助手答」配对（丢弃没有回答的提问）。"""
    rows = db.scalars(
        select(AssistantMessage)
        .where(
            AssistantMessage.project_id == project_id,
            AssistantMessage.kind == "plain",
            AssistantMessage.role.in_(("user", "assistant")),
        )
        .order_by(AssistantMessage.id)
    ).all()
    pairs: list[tuple[str, str]] = []
    pending: str | None = None
    for row in rows:
        if row.role == "user":
            pending = row.text or ""
            continue
        if pending is None:
            continue
        answer = row.text or ""
        if answer.strip():
            pairs.append((pending, answer))
        pending = None
    return pairs[-limit:] if limit > 0 else pairs


def main() -> int:
    parser = argparse.ArgumentParser(description="把历史对话回填成助理长期记忆")
    parser.add_argument("--project", type=int, required=True, help="项目 id")
    parser.add_argument("--limit", type=int, default=200, help="最多看最近多少轮（默认 200）")
    parser.add_argument(
        "--all",
        action="store_true",
        help="不按陈述句闸门过滤，每轮都交给模型判断（更慢，但可能多捞到设定）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        project = db.get(Project, args.project)
        if project is None:
            print(f"[错误] 项目 {args.project} 不存在")
            return 2

        # 与后端进程同源：读库里持久化的模型配置（backend/.env 提供渠道 key）。
        settings_router.load_persisted_llm_config(db)
        from app.llm.provider import LocalFallbackLLMProvider
        from app.routers.assistant_shared import _ensure_assistant_provider

        provider = _ensure_assistant_provider()
        if isinstance(provider, LocalFallbackLLMProvider):
            print("[错误] 助手模型不可用（当前是本地兜底），无法抽取。请先在「模型设置」里配好。")
            return 2

        pairs = load_pairs(db, args.project, args.limit)
        mode = "预览（不写库）" if args.dry_run else "写入"
        print(
            f"项目 {args.project}《{project.title or '未命名'}》｜"
            f"最近 {len(pairs)} 轮问答｜模式：{mode}｜"
            f"抽取范围：{'全部轮次' if args.all else '仅陈述句'}"
        )
        print(f"模型：{getattr(provider, 'model', '') or type(provider).__name__}")
        print("-" * 60)

        candidates: list[str] = []
        empty = 0
        skipped_gate = 0

        for index, (query, answer) in enumerate(pairs, 1):
            if not args.all and not am.should_extract(query):
                skipped_gate += 1
                continue
            items = am.extract_candidates(
                db, args.project, query=query, answer=answer, provider=provider, force=args.all
            )
            if not items:
                empty += 1
                print(f"[{index}] {query[:28]}… → 无可记内容")
                continue
            for item in items:
                print(f"[{index}] {query[:28]}… → 候选：{item}")
                candidates.append(item)

        print("-" * 60)
        # 回填最容易出的问题是「同义不同写」：这本书用第一人称 / 写作口径：第一人称 /
        # 写作使用第一人称。字符串去重挡不住，必须让模型做一次语义合并。
        existing_rows = am.list_memories(db, args.project)
        existing = [row.text for row in existing_rows]
        remaining = max(0, am.MAX_MEMORIES - len(existing_rows))
        merged = (
            am.merge_candidates(existing, candidates, provider, limit=remaining)
            if remaining
            else existing
        )
        existing_set = {am.normalize_text(text) for text in existing}
        new_items = [text for text in merged if am.normalize_text(text) not in existing_set]
        print(
            f"候选 {len(candidates)} 条 → 合并后清单 {len(merged)} 条，"
            f"其中新增 {len(new_items)} 条："
        )
        for item in new_items:
            print(f"  + {item}")
        if not candidates:
            print("  （没有候选：这段历史里没有可抽取的问答）")
        elif not new_items:
            print("  （没有新增：候选与已有记忆表达的是同一件事）")

        if args.dry_run:
            print("-" * 60)
            print(
                f"预览完成：抽不出 {empty} 轮，跳过（非陈述句）{skipped_gate} 轮。"
                "去掉 --dry-run 才会真正写入。"
            )
            return 0

        added: list[str] = []
        for item in new_items:
            row, created, reason = am.remember(db, args.project, item, source="auto")
            if created and row is not None:
                added.append(row.text)
            elif reason == "full":
                print("[!] 记忆已满，后续条目不再写入")
                break

        rows = am.list_memories(db, args.project)
        print("-" * 60)
        print(
            f"完成：新增 {len(added)} 条，抽不出 {empty} 轮，"
            f"跳过（非陈述句）{skipped_gate} 轮。"
        )
        print(f"当前记忆共 {len(rows)} 条：")
        for row in rows:
            print(f"  {row.id}. [{row.source}] {row.text}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
