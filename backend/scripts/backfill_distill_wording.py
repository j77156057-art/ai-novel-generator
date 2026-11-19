"""历史气泡文案回写：「蒸馏」→「文风提炼」。

## 为什么只改展示文本

「蒸馏」是内部工序名（表名 `distill_runs`、接口 `/distill`、前端字段 `distill`），
这些**一个都不能动**——改了会打断已有数据和调用。用户看到的旧气泡是历史快照，
里面写的是已被替换掉的旧叫法；本脚本只把**展示文本**改成新叫法。

## 范围

- `assistant_messages`：只改 `role='assistant'` 的 `text` 与 `payload` 里的字符串
  （用户自己的发言不动）；
- `wizard_notices`：只改 `payload` 里的字符串（投递队列里的回执文案）。

`step` / `field` / `tier` 这类**功能值**是 ASCII（`distill` / `balanced`），
替换词表里没有它们，天然不会被误改。

## 跑法（backend 目录）

    python scripts/backfill_distill_wording.py            # 预演，只打印不落库
    python scripts/backfill_distill_wording.py --apply    # 实际写入

也可用 `--db path/to/other.db` 指向别的库。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parents[1] / "novel_dev.db"

#: 有序替换表：长词在前，避免「文风蒸馏」被换两次。
REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("文风蒸馏", "文风提炼"),
    ("风格蒸馏", "风格提炼"),
    ("重跑蒸馏", "重新提炼"),
    ("继续蒸馏", "继续提炼"),
    ("该蒸馏的", "该提炼的"),
    ("蒸馏任务", "文风提炼任务"),
    ("蒸馏档位", "文风提炼档位"),
    ("蒸馏完成", "文风提炼完成"),
    ("蒸馏被", "文风提炼被"),
    ("蒸馏疑似", "文风提炼疑似"),
    ("蒸馏把", "提炼把"),
    ("蒸馏要", "提炼要"),
    ("蒸馏结果", "文风提炼结果"),
    ("蒸馏", "文风提炼"),
)


def rewrite(text: str) -> str:
    """把一段展示文案里的旧叫法换掉；不含旧叫法时原样返回。"""
    if "蒸馏" not in text:
        return text
    for old, new in REPLACEMENTS:
        text = text.replace(old, new)
    return text


def rewrite_json(value: Any) -> Any:
    """递归改写 JSON 里的所有字符串（键、数字、布尔值原样保留）。"""
    if isinstance(value, str):
        return rewrite(value)
    if isinstance(value, list):
        return [rewrite_json(item) for item in value]
    if isinstance(value, dict):
        return {key: rewrite_json(item) for key, item in value.items()}
    return value


def _dump(payload: Any) -> str:
    """与 SQLAlchemy 的 JSON 序列化口径保持一致（中文不转义）。"""
    return json.dumps(payload, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="实际写入（默认只预演）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 路径")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"数据库不存在：{args.db}")
        return 1

    con = sqlite3.connect(str(args.db))
    con.row_factory = sqlite3.Row
    changed_messages = 0
    changed_notices = 0
    try:
        rows = con.execute(
            "SELECT id, text, payload FROM assistant_messages "
            "WHERE role = 'assistant' "
            "AND (text LIKE '%蒸馏%' OR payload LIKE '%蒸馏%') "
            "ORDER BY id"
        ).fetchall()
        for row in rows:
            new_text = rewrite(row["text"] or "")
            new_payload = None
            if row["payload"] is not None:
                new_payload = _dump(rewrite_json(json.loads(row["payload"])))
            text_changed = new_text != (row["text"] or "")
            payload_changed = new_payload is not None and new_payload != row["payload"]
            if not (text_changed or payload_changed):
                continue
            changed_messages += 1
            print(f"  assistant_messages #{row['id']}: text={text_changed} payload={payload_changed}")
            if args.apply:
                con.execute(
                    "UPDATE assistant_messages SET text = ?, payload = ? WHERE id = ?",
                    (
                        new_text,
                        new_payload if new_payload is not None else row["payload"],
                        row["id"],
                    ),
                )

        notices = con.execute(
            "SELECT id, payload FROM wizard_notices WHERE payload LIKE '%蒸馏%' ORDER BY id"
        ).fetchall()
        for row in notices:
            new_payload = _dump(rewrite_json(json.loads(row["payload"])))
            if new_payload == row["payload"]:
                continue
            changed_notices += 1
            print(f"  wizard_notices #{row['id']}: payload")
            if args.apply:
                con.execute(
                    "UPDATE wizard_notices SET payload = ? WHERE id = ?",
                    (new_payload, row["id"]),
                )

        if args.apply:
            con.commit()
    finally:
        con.close()

    mode = "已写入" if args.apply else "预演（未写入，加 --apply 落地）"
    print(
        f"{mode}：assistant_messages {changed_messages} 条，"
        f"wizard_notices {changed_notices} 条"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
