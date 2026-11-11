# -*- coding: utf-8 -*-
"""信息载体判据 + 判定侧闸门（batch_worker._reclassify_info_carriers）。"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.kg import info_carrier as ic  # noqa: E402
from app.kg.batch_worker import _reclassify_info_carriers  # noqa: E402


def test_electronic_and_document_are_info():
    assert ic.classify_name("邮件").label == ic.INFO
    assert ic.classify_name("公司邮件").label == ic.INFO
    assert ic.classify_name("策划书").label == ic.INFO
    assert ic.classify_name("报告").label == ic.INFO
    assert ic.classify_name("资料").label == ic.INFO
    assert ic.classify_name("档案").label == ic.INFO


def test_paper_letter_stays_item():
    # 纸质信件是可转交的实物，不能跟「邮件」一起划掉。
    assert ic.classify_name("信件").label == ic.ITEM
    assert ic.classify_name("书信").label == ic.ITEM
    assert ic.classify_name("信").label == ic.ITEM


def test_container_override_beats_carrier_words():
    assert ic.classify_name("文件夹").label == ic.ITEM
    assert ic.classify_name("档案袋").label == ic.ITEM
    assert ic.classify_name("信封").label == ic.ITEM


def test_review_word_file_needs_context():
    # 形态本身两可：不硬改。
    assert ic.classify_name("文件").label == ic.REVIEW
    # 语境明确是成叠的载体内容 → info。
    assert ic.classify_name("文件", context="手中拿着一叠厚厚的文件").label == ic.INFO
    # 语境是实物容器 → item。
    assert ic.classify_name("文件", context="他打开文件夹").label == ic.ITEM


def test_real_items_are_not_hit():
    for name in ("笔", "咖啡", "手机", "电脑", "镇国剑", "黑金长刀", "丹药", "凭证"):
        assert ic.classify_name(name).label == ic.ITEM, name


def _session(tmp_path):
    path = tmp_path / "app.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE kg_entities (id INTEGER PRIMARY KEY, project_id INTEGER, "
        "kind TEXT, canonical_name TEXT, aliases TEXT)"
    )
    con.execute(
        "INSERT INTO kg_entities VALUES (1, 1, 'item', '文件', '[]')")
    con.commit()
    con.close()
    return Session(create_engine(f"sqlite:///{path}"))


def test_gate_skips_existing_item_row(tmp_path):
    """已有 item 行（可能挂着组/持有行）一律不改；只降新名字。"""
    db = _session(tmp_path)
    ents = [
        {"kind": "item", "canonical_name": "文件"},
        {"kind": "item", "canonical_name": "邮件"},
    ]
    changed = _reclassify_info_carriers(
        db, 1, ents, context="手中拿着一叠厚厚的文件，又看到了一个邮件")
    assert changed == 1
    assert ents[0]["kind"] == "item"
    assert ents[1]["kind"] == "info"
    db.close()
