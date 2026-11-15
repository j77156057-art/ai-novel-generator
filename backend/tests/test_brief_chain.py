"""素材询问链路（引擎接线）的端到端测试。

用可控的假后端 + 脚本化 provider 跑通整条链路，回答四个问题：
1. 模型有没有自主发起检索（工具是否真的给了、调用是否被接受）；
2. 候选清单有没有回到模型手里；
3. 模型点名了哪几条（注入几条）；
4. 注入的条目里，正文实际用上了几条（词面重合粗判）。

真实外部源（百度/DDG）在本机被拦，这里不依赖网络；假后端只用于测试，
不进默认路径，也不改开关默认值。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.generation.engine import (
    _brief_backend_ready,
    _brief_visibility,
    _gather_brief_materials,
)
from app.retrieval.brief.backend import SearchResult
from app.retrieval.brief.cache import BriefCache
from app.retrieval.brief.settings import BriefSettings
from app.retrieval.brief.tool import SELECT_TOOL_NAME, TOOL_NAME, BriefTool

MATERIALS = [
    SearchResult(
        title="某流程说明",
        snippet="办理前需要先登记，窗口会核对材料清单，票据要留存备查。",
    ),
    SearchResult(
        title="器物规格",
        snippet="登记窗口常见规格有三种，尺寸按用途区分，长期存放要避光。",
    ),
]


class FakeSearchBackend:
    """只替换网络层：记录调用并按固定结果返回。"""

    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.calls: list[str] = []

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        self.calls.append(query)
        return list(self.results[:max_results])


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


class ScriptedProvider:
    """按脚本返回 assistant 消息；脚本项可以是 callable(messages)。"""

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []

    def complete_message(self, messages, tools=None, tool_choice=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools})
        reply = self.replies.pop(0)
        return reply(messages) if callable(reply) else reply


def _settings(tmp_path: Path) -> BriefSettings:
    return BriefSettings(
        enabled=True,
        brief_version="chain-v1",
        cache_dir=str(tmp_path / "cache"),
        max_calls_per_chapter=3,
        timeout_s=0.5,
        chapter_budget_s=5.0,
        max_results=3,
        max_query_chars=40,
        max_block_chars=600,
    )


def _tool(tmp_path: Path, backend: FakeSearchBackend, logs: list[dict]) -> BriefTool:
    tool = BriefTool(
        _settings(tmp_path),
        backend=backend,
        cache=BriefCache(
            str(tmp_path / "cache"), "chain-v1", similarity_threshold=0.9, scan_limit=50
        ),
        log_sink=logs.append,
        sleep=lambda _: None,
    )
    tool.start_chapter(session_id="s-chain", project_id=1, segment_no=1)
    return tool


def _ids_in(text: str) -> list[str]:
    return re.findall(r"\[(m[0-9a-f]{12})\]", text)


def _candidate_text(messages) -> str:
    """取最近一条带 id 的工具结果（候选清单）。"""
    for message in reversed(messages):
        content = str(message.get("content") or "")
        if message.get("role") == "tool" and _ids_in(content):
            return content
    return ""


def test_chain_search_then_select_then_adopt(tmp_path: Path) -> None:
    backend = FakeSearchBackend(MATERIALS)
    logs: list[dict] = []
    tool = _tool(tmp_path, backend, logs)
    provider = ScriptedProvider([
        # 1) 模型自主发起检索
        {"content": "", "tool_calls": [
            _call("c1", TOOL_NAME, {"query": "登记窗口 材料清单 流程"})
        ]},
        # 2) 看到候选后点名第一条
        lambda messages: {
            "content": "",
            "tool_calls": [
                _call("c2", SELECT_TOOL_NAME, {"ids": _ids_in(_candidate_text(messages))[:1]})
            ],
        },
        # 3) 素材到手，结束素材轮
        {"content": "已选好。", "tool_calls": []},
    ])

    blocks = _gather_brief_materials(
        provider, tool, system="你是写作者", user="写一段登记窗口的场景", constraints=None
    )

    # 工具真的给了模型（可见性链路）：两个阶段的 schema 都在 tools 里
    names = [t["function"]["name"] for t in provider.requests[0]["tools"]]
    assert names == [TOOL_NAME, SELECT_TOOL_NAME]
    # 模型发起的检索被接受并联网一次
    assert backend.calls == ["登记窗口 材料清单 流程"]
    # 候选清单回到了模型手里：第二轮的上下文里有 id、入库时间、摘要截断
    second_context = provider.requests[1]["messages"][-1]["content"]
    assert "天前入库" in second_context and "｜摘要：" in second_context
    # 点名的条目才进 prompt，且只注入一条
    assert len(blocks) == 1 and "办理前需要先登记" in blocks[0]
    assert tool.chapter_stats()["injected"] == 1
    assert tool.chapter_stats()["candidates"] == 2

    # 正文采纳：只有真正写进去的那条算采纳
    stats = tool.note_chapter_text("他先到登记窗口，把票据和材料清单一并递了进去。")
    assert stats == {"injected": 1, "adopted": 1}
    assert logs[-1]["extra"]["adopt_ratio"] == 1.0

    # 落库的条目：选中的标记使用，未选中的留着给后续章节
    assert tool._library.stats() == {"total": 2, "unused": 1, "used": 1}


def test_chain_model_declines_has_no_side_effects(tmp_path: Path) -> None:
    backend = FakeSearchBackend(MATERIALS)
    logs: list[dict] = []
    tool = _tool(tmp_path, backend, logs)
    provider = ScriptedProvider([{"content": "无", "tool_calls": []}])

    assert _gather_brief_materials(provider, tool, system="s", user="u", constraints=None) == []
    assert backend.calls == []
    assert logs == []
    assert not (tmp_path / "brief_library").exists()


def test_chain_backfills_selection_when_model_stops_after_search(tmp_path: Path) -> None:
    """模型查完直接回复「无」时，补一轮只做选择，避免退化成「只查不选」。"""
    backend = FakeSearchBackend(MATERIALS)
    logs: list[dict] = []
    tool = _tool(tmp_path, backend, logs)
    provider = ScriptedProvider([
        {"content": "", "tool_calls": [
            _call("c1", TOOL_NAME, {"query": "登记窗口 材料清单 流程"})
        ]},
        {"content": "无", "tool_calls": []},
        # 补选轮：只给 select 工具
        lambda messages: {
            "content": "",
            "tool_calls": [
                _call("c2", SELECT_TOOL_NAME, {"ids": _ids_in(_candidate_text(messages))[:1]})
            ],
        },
    ])

    blocks = _gather_brief_materials(provider, tool, system="s", user="u", constraints=None)

    assert len(blocks) == 1
    assert tool.chapter_stats()["injected"] == 1
    # 补选轮只暴露选择工具，不再暴露检索工具
    backfill_tools = provider.requests[-1]["tools"]
    assert [t["function"]["name"] for t in backfill_tools] == [SELECT_TOOL_NAME]


def test_chain_selection_unknown_id_degrades(tmp_path: Path) -> None:
    backend = FakeSearchBackend(MATERIALS)
    logs: list[dict] = []
    tool = _tool(tmp_path, backend, logs)
    provider = ScriptedProvider([
        {"content": "", "tool_calls": [
            _call("c1", TOOL_NAME, {"query": "登记窗口 材料清单 流程"})
        ]},
        {"content": "", "tool_calls": [
            _call("c2", SELECT_TOOL_NAME, {"ids": ["m-does-not-exist"]})
        ]},
        {"content": "无", "tool_calls": []},
    ])

    blocks = _gather_brief_materials(provider, tool, system="s", user="u", constraints=None)
    assert blocks == []
    assert tool.chapter_stats()["injected"] == 0
    # 模型编的 id 不会让库丢条目
    assert tool._library.stats()["total"] == 2


def test_backend_ready_without_search_url() -> None:
    """免费/无头后端没有 search_url 也必须可见；JSON 后端才要求地址。"""
    assert _brief_backend_ready(BriefSettings(enabled=True, backend="free"))[0] is True
    assert _brief_backend_ready(BriefSettings(enabled=True, backend="auto"))[0] is True
    ready, reason = _brief_backend_ready(BriefSettings(enabled=True, backend="json"))
    assert ready is False and reason == "no_search_url"
    assert _brief_backend_ready(
        BriefSettings(enabled=True, backend="json", search_url="http://127.0.0.1:1/x")
    )[0] is True
    # 三态留痕：开关关 = disabled；开了但无后端 = no_backend；可用 = visible
    assert _brief_visibility(BriefSettings(enabled=False)) == "disabled"
    assert _brief_visibility(BriefSettings(enabled=True, backend="json")) == "no_backend"
    assert _brief_visibility(BriefSettings(enabled=True, backend="free")) == "visible"
