"""B 子系统第二刀：当次 prompt / 权威快照的写入与读取。"""
import hashlib
import json
import os
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402
from app.generation.engine import (  # noqa: E402
    _SNAPSHOT_MAX_CHARS,
    _authority_snapshot,
    _commit_segment,
    _prompt_snapshot,
)
from app.models import Generation, Project  # noqa: E402
from app.routers.generation import get_segment_snapshot  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_prompt_snapshot_keeps_text_and_hash():
    raw = _prompt_snapshot("SYS", "USER")
    snap = json.loads(raw)

    assert snap["system"] == "SYS" and snap["user"] == "USER"
    assert snap["chars"] == len("SYS\n\nUSER")
    assert snap["truncated"] is False
    assert snap["sha256"] == hashlib.sha256("SYS\n\nUSER".encode("utf-8")).hexdigest()


def test_prompt_snapshot_truncates_but_hash_covers_full_text():
    user = "段" * (_SNAPSHOT_MAX_CHARS + 10)
    snap = json.loads(_prompt_snapshot("SYS", user))

    assert snap["truncated"] is True
    assert snap["user"] == ""
    assert snap["sha256"] == hashlib.sha256(
        ("SYS\n\n" + user).encode("utf-8")
    ).hexdigest()


def test_authority_snapshot_captures_hashes_and_names():
    proj = Project(
        title="t",
        premise="前提",
        outline=["第1章", "第2章"],
        plot_bible={"synopsis": "梗概"},
        style_narrative={"version": 7, "characters": [{"name": "甲"}, {"name": "乙"}]},
        reference_novel_ids=[11, 12],
    )
    snap = json.loads(
        _authority_snapshot(proj, {"constraints": {"characters": ["甲"]}}, "第1章")
    )

    assert snap["chapter_range"] == "第1章"
    assert snap["plot_bible"] == {"synopsis": "梗概"}
    assert snap["outline_len"] == 2 and len(snap["outline_sha256"]) == 64
    assert snap["premise_sha256"] == hashlib.sha256("前提".encode()).hexdigest()
    assert snap["narrative"]["version"] == 7
    assert snap["narrative"]["character_names"] == ["甲", "乙"]
    assert snap["style_source_ids"] == [11, 12]
    assert snap["characters_in_context"] == ["甲"]
    assert snap["constraint_keys"] == ["characters"]


def test_commit_segment_persists_snapshots(db):
    proj = Project(title="t")
    db.add(proj)
    db.commit()
    prompt = _prompt_snapshot("SYS", "USER")
    authority = _authority_snapshot(proj, {}, None)

    gen, segment_no = _commit_segment(
        db, proj, "正文", "摘要", prompt_json=prompt, authority_json=authority
    )

    assert segment_no == 1
    assert gen.prompt_json == prompt
    assert gen.authority_json == authority


def test_snapshot_endpoint_reads_and_reports_legacy_rows(db):
    proj = Project(title="t")
    db.add(proj)
    db.commit()
    db.add(
        Generation(
            project_id=proj.id,
            segment_no=1,
            text="新段",
            prompt_json=_prompt_snapshot("SYS", "USER"),
            authority_json='{"version": 1}',
        )
    )
    db.add(Generation(project_id=proj.id, segment_no=2, text="旧段"))
    db.commit()

    out = get_segment_snapshot(proj.id, 1, db)
    assert out["prompt"]["system"] == "SYS"
    assert out["authority"] == {"version": 1}
    assert out["note"] is None

    legacy = get_segment_snapshot(proj.id, 2, db)
    assert legacy["prompt"] is None
    assert legacy["note"]

    with pytest.raises(HTTPException) as err:
        get_segment_snapshot(proj.id, 99, db)
    assert err.value.status_code == 404
