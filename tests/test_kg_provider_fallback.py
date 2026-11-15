"""跨渠道 provider fallback（仅 KG 抽取链）的验收用例。

只包 KG 抽取：`writer` / `assistant` / `distiller` 不接（工厂不经过本包装器）。
`fallback_providers` 为空时 `get_kg_extract_provider()` 返回原 provider，
路径与改造前逐字节一致——所以「空配置」必须显式断言。

四条验收（对应任务清单）：
1. 主渠道超时 → 副渠道成功，`note.llm` 记账完整（channel_used / fallback_used /
   channels[] / timing 带 provider），且切换打一条 warning；
2. 400 / 401 / 403 / 404 不切换：副渠道一次都不调，原异常直接抛出；
3. 全渠道失败：抛错信息带完整链、打一条 error，批抽落 failed 且可 requeue；
4. 空配置下返回原 provider；未知渠道名 / 缺 key 记 channel_skipped_no_key。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: E402,F401
from app.models import AgentRun, Generation, Project  # noqa: E402

ROLE = "kg_batch_extract"


class _FakeChannel:
    """最小 provider：chat 可命中/抛错，成功时留下 last_call_stats。"""

    def __init__(self, name: str, *, exc: Exception | None = None) -> None:
        self.channel_name = name
        self.model = f"{name}-model"
        self.last_model = None
        self.last_call_stats = None
        self.total_timeout = 300
        self.first_byte_timeout = 120
        self.calls = 0
        self._exc = exc

    def chat(self, system, user, constraints=None):  # noqa: ANN001, ANN201
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        self.last_model = self.model
        self.last_call_stats = {
            "provider": type(self).__name__,
            "model": self.model,
            "first_byte_ms": 1,
            "total_ms": 2,
            "bytes": 3,
        }
        return "ok"


def _wrap(*channels, chain_timeout=300, skipped=None):
    from app.llm.provider import FallbackChannelProvider

    return FallbackChannelProvider(
        list(channels), chain_timeout=chain_timeout, skipped=skipped)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://fake", code, f"http {code}", {}, None)


# ---------------------------------------------------------------------------
# 1. 主渠道超时 → 副渠道成功
# ---------------------------------------------------------------------------

def test_primary_timeout_falls_back_to_secondary(caplog):
    from app.kg.batch_worker import _llm_budget_note

    primary = _FakeChannel("minimax", exc=TimeoutError("首字节超时"))
    secondary = _FakeChannel("zhipu")
    provider = _wrap(("minimax", primary), ("zhipu", secondary))

    with caplog.at_level("WARNING"):
        assert provider.chat("sys", "user") == "ok"

    assert primary.calls == 1, "主渠道必须真的被尝试过"
    assert secondary.calls == 1, "超时后必须切到副渠道"

    stats = provider.last_channel_stats
    assert stats["channel_used"] == "zhipu"
    assert stats["fallback_used"] is True
    assert [c["provider"] for c in stats["channels"]] == ["minimax", "zhipu"]
    assert stats["channels"][0]["outcome"] == "timeout"
    assert stats["channels"][1]["outcome"] == "success"

    note = _llm_budget_note(provider)
    assert note["channel_used"] == "zhipu"
    assert note["fallback_used"] is True
    assert [c["provider"] for c in note["channels"]] == ["minimax", "zhipu"]
    # timing 必须带 provider（否则「跑在哪个渠道」还是查不到）
    assert note["timing"]["provider"] == "_FakeChannel"

    switched = [r for r in caplog.records if "渠道降级" in r.getMessage()]
    assert len(switched) == 1, "每次切换恰好一条 warning"


# ---------------------------------------------------------------------------
# 2. 配置类错误不切换（400 / 401 / 403 / 404）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_config_errors_do_not_switch(code):
    primary = _FakeChannel("minimax", exc=_http_error(code))
    secondary = _FakeChannel("zhipu")
    provider = _wrap(("minimax", primary), ("zhipu", secondary))

    with pytest.raises(urllib.error.HTTPError) as ei:
        provider.chat("sys", "user")

    assert ei.value.code == code, "原异常必须原样暴露，不能被包成 RuntimeError"
    assert secondary.calls == 0, f"{code} 是配置问题，不该换渠道掩盖"
    stats = provider.last_channel_stats
    assert stats["channel_used"] is None
    assert stats["fallback_used"] is False
    assert [c["outcome"] for c in stats["channels"]] == [f"http_{code}"]


@pytest.mark.parametrize("code", [429, 500, 502, 503, 529])
def test_transient_http_errors_do_switch(code):
    primary = _FakeChannel("minimax", exc=_http_error(code))
    secondary = _FakeChannel("zhipu")
    provider = _wrap(("minimax", primary), ("zhipu", secondary))

    assert provider.chat("sys", "user") == "ok"
    assert secondary.calls == 1
    assert provider.last_channel_stats["channel_used"] == "zhipu"


# ---------------------------------------------------------------------------
# 3. 全渠道失败：完整链 + 可 requeue
# ---------------------------------------------------------------------------

def test_all_channels_fail_raise_with_full_chain(caplog):
    primary = _FakeChannel("minimax", exc=TimeoutError("超时"))
    secondary = _FakeChannel(
        "zhipu", exc=urllib.error.URLError("connection refused"))
    provider = _wrap(("minimax", primary), ("zhipu", secondary))

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError) as ei:
            provider.chat("sys", "user")

    message = str(ei.value)
    assert "minimax=timeout" in message
    assert "zhipu=transport" in message

    stats = provider.last_channel_stats
    assert stats["channel_used"] is None
    assert stats["fallback_used"] is False
    assert [c["outcome"] for c in stats["channels"]] == ["timeout", "transport"]

    errors = [r for r in caplog.records if "全部渠道失败" in r.getMessage()]
    assert len(errors) == 1, "全败恰好一条 error"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """文件库 + 一个项目/一段正文：批抽与 requeue 都从库里读。"""
    db_path = tmp_path / "kg_fallback.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True,
                           connect_args={"timeout": 1.0})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)

    import app.db as app_db

    monkeypatch.setattr(app_db, "SessionLocal", Session)
    with Session() as s:
        s.add(Project(id=1, title="测试"))
        s.add(Generation(project_id=1, segment_no=1, text="他推开门，走了出去。",
                         summary=None))
        row = AgentRun(
            project_id=1, role=ROLE, status="pending",
            request_hash=hashlib.sha256(b"1:1").hexdigest(),
            note=json.dumps({"segment_no": 1}, ensure_ascii=False),
        )
        s.add(row)
        s.commit()
        row_id = row.id
    try:
        yield {"Session": Session, "row_id": row_id}
    finally:
        engine.dispose()


def test_batch_marks_failed_and_is_requeueable(env, monkeypatch):
    from app.kg import batch_worker as bw

    primary = _FakeChannel("minimax", exc=TimeoutError("超时"))
    secondary = _FakeChannel(
        "zhipu", exc=urllib.error.URLError("connection reset"))
    provider = _wrap(("minimax", primary), ("zhipu", secondary))

    monkeypatch.setattr(bw, "_get_provider", lambda: provider)
    bw._queue.put(bw._QueueItem(project_id=1, segment_no=1, text="他推开门，走了出去。",
                                run_id=env["row_id"]))
    bw._run_batches(1)

    with env["Session"]() as s:
        row = s.get(AgentRun, env["row_id"])
        note = json.loads(row.note or "{}")
        assert row.status == "failed", "全渠道失败仍要落 failed，不能永远 running"
        assert note["error_kind"] == "retry_exhausted"
        assert note["abandoned"] is True
        llm = note["llm"]
        assert llm["channel_used"] is None
        assert llm["fallback_used"] is False
        assert [c["provider"] for c in llm["channels"]] == ["minimax", "zhipu"]
        assert [c["outcome"] for c in llm["channels"]] == ["timeout", "transport"]

    with env["Session"]() as s:
        result = bw.requeue_failed(s, 1, [env["row_id"]])
    assert result["requeued"] == 1, result
    # 清掉重捞进内存队列的段，避免污染同进程的后续用例
    bw._queue.take_batch(1)


def test_worker_status_counts_fallback_rows(env):
    from app.kg import batch_worker as bw

    with env["Session"]() as s:
        # 第一条：切到副渠道成功；第二条：主渠道直接成功。
        for seg, fallback_used in ((201, True), (202, False)):
            s.add(AgentRun(
                project_id=1, role=ROLE, status="success",
                request_hash=f"h-{seg}",
                note=json.dumps({
                    "segment_no": seg,
                    "llm": {"channel_used": "zhipu" if fallback_used else "minimax",
                            "fallback_used": fallback_used, "channels": []},
                }, ensure_ascii=False),
            ))
        s.commit()
        summary = bw.worker_status(s, 1)["summary"]

    assert summary["fallback"] == 1
    assert summary["success"] >= 2


# ---------------------------------------------------------------------------
# 4. 空配置 / 跳过渠道
# ---------------------------------------------------------------------------

@pytest.fixture()
def kg_settings(monkeypatch):
    from app.config import settings
    from app.llm.provider import reset_kg_extract_provider_cache

    names = (
        "llm_provider", "minimax_api_key", "minimax_model",
        "zhipu_api_key", "kg_extract_provider", "kg_extract_models",
        "kg_extract_fallback_providers", "kg_extract_fallback_chain_timeout",
    )
    snapshot = {name: getattr(settings, name) for name in names}
    reset_kg_extract_provider_cache()
    yield settings
    for name, value in snapshot.items():
        setattr(settings, name, value)
    reset_kg_extract_provider_cache()


def _setup_primary(settings) -> None:
    settings.llm_provider = "minimax"
    settings.minimax_api_key = "test-minimax-key"
    settings.minimax_model = "MiniMax-M3.1-Flash"
    settings.kg_extract_provider = ""
    settings.kg_extract_models = ""


def test_empty_fallback_returns_original_provider(kg_settings):
    from app.llm.provider import (
        FallbackChannelProvider,
        MiniMaxChatProvider,
        get_kg_extract_provider,
    )

    _setup_primary(kg_settings)
    kg_settings.kg_extract_fallback_providers = ""

    provider = get_kg_extract_provider()
    assert isinstance(provider, MiniMaxChatProvider)
    assert not isinstance(provider, FallbackChannelProvider), (
        "空配置必须返回原 provider，路径与改造前逐字节一致")


def test_unknown_and_missing_key_are_skipped(kg_settings):
    from app.llm.provider import FallbackChannelProvider, get_kg_extract_provider

    _setup_primary(kg_settings)
    kg_settings.zhipu_api_key = ""  # 缺 key
    kg_settings.kg_extract_fallback_providers = "zhipu,not-a-channel"

    provider = get_kg_extract_provider()
    assert isinstance(provider, FallbackChannelProvider), (
        "被跳过的渠道也要留痕，不能静默消失")
    assert [name for name, _ in provider.channels] == ["minimax"]
    outcomes = {item["provider"]: item["outcome"] for item in provider.skipped}
    assert outcomes == {
        "zhipu": "channel_skipped_no_key",
        "not-a-channel": "channel_skipped_no_key",
    }
    assert {item["detail"] for item in provider.skipped} == {
        "no_api_key", "unknown_channel"}


def test_chain_timeout_default_is_300():
    from app import config as cfg
    from app.config import Settings

    assert cfg._KG_EXTRACT_DEFAULTS.get("fallback_providers") == ""
    assert cfg._KG_EXTRACT_DEFAULTS.get("fallback_chain_timeout") == 300
    assert Settings(_env_file=None).kg_extract_fallback_chain_timeout == 300
