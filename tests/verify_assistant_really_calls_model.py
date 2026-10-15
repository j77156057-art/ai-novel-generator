"""验收脚本：证明 Assistant **真的调用了模型**，而不是走本地兜底。

为什么需要它（而不是只看 endpoint 200）：
    缺陷的形态是「契约全绿、语义全错」——`POST /api/assistant/qa` 在无 key 时照样
    返回 HTTP 200、字段齐全，只是 answer 变成了确定性降级文案。前面几轮 QA 验的都是
    写链路 / 权限 / 并发 / 超时，没人验「模型是否真被调用」。所以本脚本把断言打在
    **最底层**：

      场景 2：monkeypatch `urllib.request.urlopen`（HTTP 出口）→ 断言请求真的带着
              legacy 渠道 key 出去了，且 response 原样回流到 answer；
      场景 3：不打任何 patch，用真实网络打 open.bigmodel.cn → 断言拿到**真实模型文本**
              （不是 "Assistant provider 不可用…" 兜底串），且 AgentRun.model
              落库为 "glm-4-flash"（不是 "(local_fallback)"）。

    场景 1 是「修复前形态」的对照快照：五渠道 key 全空 → LocalFallback + 兜底文案。

运行：
    cd backend && python ../tests/verify_assistant_really_calls_model.py
（脚本自己把 backend/ 塞进 sys.path，两个目录都能跑）

诚实约定：
    - 场景 3 依赖真实网络与真实余额，失败时脚本会**明确打印「未拿到真实模型文本」**
      并以 degrade 方式退出（exit code 保持 0，因为「拿不到」不是回归缺陷）；
    - 场景 1/2 是确定性的，必须 PASS，否则 exit code 非 0。
    - **绝不**把降级文案当成模型回答来宣称成功。
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.request

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents.registry import reset_tool_registry  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
import app.llm.provider as llm_provider_mod  # noqa: E402
from app.llm.provider import LocalFallbackLLMProvider, ZhipuChatProvider  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402
from app.models import AgentRun, Project  # noqa: E402
from app.routers import assistant as assistant_router  # noqa: E402

DEGRADED_MARK = "Assistant provider 不可用"  # routers/assistant.py 的降级文案
# LocalFallbackLLMProvider 不会抛错（它的 chat 直接返回确定性模板），所以 router
# 的 except 分支压根不进——「模型没被调用」在用户界面上表现为**本地模板续写文案**，
# 而不是 "Assistant provider 不可用…"。两种文案都必须识别为「非模型回答」。
LOCAL_TEMPLATE_MARK = "【离线兜底·确定性续写】"


def _is_not_model_answer(answer: str) -> bool:
    """判断 answer 是不是「没过模型」的文案（本地模板兜底 / router 降级文案）。"""
    text = answer or ""
    return DEGRADED_MARK in text or LOCAL_TEMPLATE_MARK in text

_RESULTS: list[tuple[str, bool, str]] = []


def _record(scenario: str, ok: bool, detail: str) -> None:
    _RESULTS.append((scenario, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {scenario}: {detail}")


def _reset_all() -> None:
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_tool_registry()


def _read_env_key(env_path: str, name: str) -> str:
    """从 backend/.env 读一个 key（只读 ZHIPU_API_KEY，不回显全文）。"""
    if not os.path.exists(env_path):
        return ""
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith(name + "="):
                return line[len(name) + 1:].strip().strip('"').strip("'")
    return ""


def _seed_db() -> tuple[int, object]:
    """建表 + 造一个项目；返回 (project_id, session)。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    proj = Project(title="调用链验收项目", target_length=3000, mode="quick", status="created")
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj.id, db


def _call_qa(project_id: int, db, query: str = "主角目前的最大困境是什么？") -> dict:
    """直接调 router 函数（等价于 TestClient 打 POST /api/assistant/qa）。"""
    body = assistant_router.AssistantQARequest(project_id=project_id, query=query)
    return assistant_router.assistant_qa(body=body, db=db)


def _run_model_of(run_id: int, db) -> str:
    row = db.get(AgentRun, run_id)
    return row.model if row is not None else "(no run row)"


# ====================================================================
# 场景 1：修复前的形态（五渠道 key 全空）→ LocalFallback + 降级文案
# ====================================================================


def _old_get_provider_config(self, role: str) -> dict[str, object]:  # noqa: ANN001
    """修复前**原样抄回来**的 assistant 分支实现（对照组用）。

    与修复前的代码一致：直接返回 self.assistant_api_key，没有任何渠道回退。
    """
    if role == "writer":
        return {
            "provider": settings.writer_provider,
            "api_key": settings.writer_api_key,
            "base_url": settings.writer_base_url,
            "model": settings.writer_model,
            "max_tokens": settings.writer_max_tokens,
            "timeout": settings.writer_timeout,
            "_fallback_to_legacy": not bool(settings.writer_provider),
        }
    if role == "assistant":
        return {
            "provider": settings.assistant_provider,
            "api_key": settings.assistant_api_key,      # ← 恒为空的根因
            "base_url": settings.assistant_base_url,
            "model": settings.assistant_model,
            "max_tokens": settings.assistant_max_tokens,
            "timeout": settings.assistant_timeout,
            "_fallback_to_legacy": False,
        }
    raise ValueError(f"未知 role: {role!r}（应为 'writer' 或 'assistant'）")


def scenario_1_pre_fix_behavior(project_id: int, db, real_key: str) -> None:
    """场景 1（对照组）：**同一份配置下**把修复前的实现装回去，复现恒定降级。

    配置态就是 providers.yaml 文档化的默认态：assistant.api_key 留空、渠道 key 配好。
    """
    print("\n[场景 1] 同一配置 + 修复前的 get_provider_config —— 复现恒定降级")
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = ""          # ← yaml 的默认态
    settings.assistant_base_url = ""
    settings.assistant_model = "glm-4-flash"
    settings.zhipu_api_key = real_key
    _reset_all()

    original = settings.__class__.get_provider_config
    settings.__class__.get_provider_config = _old_get_provider_config  # type: ignore[assignment]
    try:
        provider = llm_provider_mod.build_assistant_provider()
        old_cfg = dict(settings.get_provider_config("assistant"))
        resp = _call_qa(project_id, db)
        old_answer = (resp.get("answer") or "")
        old_model_field = _run_model_of(resp["agent_run_id"], db)
    finally:
        settings.__class__.get_provider_config = original  # type: ignore[assignment]

    _record("修复前 assistant cfg.api_key 为空", old_cfg.get("api_key") == "",
            repr(old_cfg.get("api_key")))
    _record("修复前 provider = LocalFallback（缺陷复现）",
            isinstance(provider, LocalFallbackLLMProvider), type(provider).__name__)
    _record("修复前 answer 是本地模板文案（非模型回答）",
            _is_not_model_answer(old_answer), repr(old_answer[:40]))
    _record("修复前 AgentRun.model = (local_fallback)",
            old_model_field == "(local_fallback)", old_model_field)

    # —— 修复后：同一配置立刻拿到真实渠道 provider（与修复前形成直接对照）——
    _reset_all()
    fixed = llm_provider_mod.build_assistant_provider()
    _record("同一配置下修复后 = ZhipuChatProvider",
            isinstance(fixed, ZhipuChatProvider), type(fixed).__name__)


def scenario_1b_all_keys_empty(project_id: int, db) -> None:
    """场景 1b：五渠道 key 全空时（新装机器）仍安全降级，不报错。"""
    print("\n[场景 1b] 五渠道 key 全空 —— 仍降级 LocalFallback（不崩、不报错）")
    for f in ("assistant_api_key", "zhipu_api_key", "openrouter_api_key",
              "siliconflow_api_key", "minimax_api_key", "dashscope_api_key"):
        setattr(settings, f, "")
    settings.assistant_provider = "zhipu"
    settings.assistant_model = "glm-4-flash"
    _reset_all()

    provider = llm_provider_mod.build_assistant_provider()
    resp = _call_qa(project_id, db)

    _record("provider = LocalFallback", isinstance(provider, LocalFallbackLLMProvider),
            type(provider).__name__)
    _record("端点仍返回 200 结构（answer 非异常）", isinstance(resp.get("answer"), str), "str")


# ====================================================================
# 场景 2：monkeypatch HTTP 出口 —— 证明请求真的带着 legacy key 出去了
# ====================================================================


def scenario_2_urlopen_recorded(project_id: int, db, real_key: str) -> None:
    print("\n[场景 2] assistant_api_key 留空 + 借 zhipu 渠道 key —— 拦截 HTTP 出口取证")
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = ""          # ← providers.yaml 的默认态
    settings.assistant_base_url = ""
    settings.assistant_model = "glm-4-flash"
    settings.zhipu_api_key = real_key
    _reset_all()

    captured: dict[str, object] = {}

    class _FakeResp(io.BytesIO):
        """最小 OpenAI 兼容响应体：urlopen 的调用方只用到 read() / 上下文管理。"""

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def read(self, *args):  # noqa: D102
            return json.dumps({
                "choices": [{"message": {"content": "【模型侧真实回包-桩】主角的困境是身份暴露。"}}]
            }).encode("utf-8")

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["called"] = True
        captured["url"] = getattr(req, "full_url", str(req))
        captured["auth"] = req.get_header("Authorization")
        captured["timeout"] = timeout
        body = json.loads((req.data or b"{}").decode("utf-8"))
        captured["payload_model"] = body.get("model")
        captured["max_tokens"] = body.get("max_tokens")
        captured["user"] = (body.get("messages") or [{}])[-1].get("content", "")
        return _FakeResp(b"")

    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        provider = llm_provider_mod.build_assistant_provider()
        resp = _call_qa(project_id, db)
    finally:
        urllib.request.urlopen = original_urlopen  # type: ignore[assignment]

    _record("provider 类型 = ZhipuChatProvider（非兜底）",
            isinstance(provider, ZhipuChatProvider), type(provider).__name__)
    _record("urlopen 真的被调用了（= 模型请求出网）",
            bool(captured.get("called")), f"url={captured.get('url')}")
    _record("Authorization 带的是 legacy 渠道 key",
            captured.get("auth") == f"Bearer {real_key}", "Bearer ***" + real_key[-4:])
    _record("请求体 model = glm-4-flash",
            captured.get("payload_model") == "glm-4-flash", str(captured.get("payload_model")))
    _record("Assistant max_tokens=1024 / timeout=60 生效",
            captured.get("max_tokens") == 1024 and captured.get("timeout") == 60,
            f"max_tokens={captured.get('max_tokens')} timeout={captured.get('timeout')}")
    _record("answer = 模型回包内容（非降级文案）",
            resp.get("answer") == "【模型侧真实回包-桩】主角的困境是身份暴露。",
            repr((resp.get("answer") or "")[:40]))
    _record("AgentRun.model = glm-4-flash",
            _run_model_of(resp["agent_run_id"], db) == "glm-4-flash",
            _run_model_of(resp["agent_run_id"], db))
    _record("evidence 已拼进发给模型的 user 文本",
            "evidence:" in str(captured.get("user")), "含 evidence 段")


# ====================================================================
# 场景 3：真实网络 —— 拿到真实模型文本才算「助手真的在调模型」
# ====================================================================


def scenario_3_real_network(project_id: int, db, real_key: str) -> bool:
    print("\n[场景 3] 不打任何 patch，真实调用 open.bigmodel.cn")
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = ""
    settings.assistant_base_url = ""
    settings.assistant_model = "glm-4-flash"
    settings.zhipu_api_key = real_key
    settings.assistant_timeout = 60
    _reset_all()

    try:
        provider = llm_provider_mod.build_assistant_provider()
    except Exception as exc:  # noqa: BLE001
        _record("构造 ZhipuChatProvider", False, f"{type(exc).__name__}: {exc}")
        return False

    is_zhipu = isinstance(provider, ZhipuChatProvider)
    _record("provider 类型 = ZhipuChatProvider", is_zhipu, type(provider).__name__)

    try:
        resp = _call_qa(project_id, db, query="用一句话说明这个项目当前最该关注的点。")
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] 真网调用异常（本机出网受限？）：{type(exc).__name__}: {exc}")
        print("         说明：本轮**没有**拿到真实模型文本，只证明到调用链（见场景 2）。")
        return False

    answer = (resp.get("answer") or "").strip()
    model_field = _run_model_of(resp["agent_run_id"], db)
    got_real_model_text = (
        bool(answer)
        and not _is_not_model_answer(answer)
        and model_field == "glm-4-flash"
    )

    _record("AgentRun.model = glm-4-flash（非 local_fallback）",
            model_field == "glm-4-flash", model_field)
    _record("answer 既不是降级文案也不是本地模板",
            not _is_not_model_answer(answer), repr(answer[:60]))
    _record("拿到真实模型文本", got_real_model_text,
            f"len(answer)={len(answer)} 首 120 字：{answer[:120]}")

    if not got_real_model_text:
        print("         说明：本轮**没有**拿到真实模型文本，只证明到调用链（见场景 2）。")
    return got_real_model_text


# ====================================================================
# 场景 4：Writer 对照组 —— 同一环境下 Writer 完全未被本次修复扰动
# ====================================================================


def scenario_4_writer_untouched(real_key: str) -> None:
    print("\n[场景 4] Writer 对照组：Assistant 借 key 不得改写 Writer 单例")
    settings.llm_provider = "zhipu"
    settings.zhipu_api_key = real_key
    settings.zhipu_model = "glm-4-flash"
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = ""
    _reset_all()

    writer = llm_provider_mod.get_llm_provider()
    assistant = llm_provider_mod.build_assistant_provider()

    if hasattr(writer, "timeout"):
        _record("Writer timeout = 120", writer.timeout == 120, str(writer.timeout))
        _record("Writer stream_timeout = 180", writer.stream_timeout == 180,
                str(writer.stream_timeout))
    else:
        _record("Writer 单例未被降级", False, type(writer).__name__)
    if hasattr(assistant, "timeout"):
        _record("Assistant timeout = 60（与 Writer 独立）",
                assistant.timeout == 60, str(assistant.timeout))
        _record("Assistant stream_timeout = 60", assistant.stream_timeout == 60,
                str(assistant.stream_timeout))
    _record("双轨实例不是同一对象", writer is not assistant,
            f"writer={type(writer).__name__} assistant={type(assistant).__name__}")
    _record("get_llm_provider() 仍是单例", llm_provider_mod.get_llm_provider() is writer, "id 一致")


def main() -> int:
    print("=" * 78)
    print("Assistant 调用链验收：模型到底有没有被真的调到？")
    print("=" * 78)

    real_key = _read_env_key(os.path.join(_BACKEND, ".env"), "ZHIPU_API_KEY")
    has_key = bool(real_key)
    print(f"backend/.env 里的 ZHIPU_API_KEY：{'已读到（长度 %d）' % len(real_key) if has_key else '未找到'}")

    project_id, db = _seed_db()
    try:
        fake_key = real_key or "fake-key-for-offline-trace"
        scenario_1_pre_fix_behavior(project_id, db, fake_key)
        scenario_1b_all_keys_empty(project_id, db)
        scenario_2_urlopen_recorded(project_id, db, fake_key)
        real_ok = scenario_3_real_network(project_id, db, real_key) if has_key else False
        if not has_key:
            print("\n[场景 3] SKIP：本机 backend/.env 没有 ZHIPU_API_KEY，无法做真实网络取证。")
        scenario_4_writer_untouched(real_key or "fake-key-for-offline-trace")
    finally:
        db.close()

    mandatory = [r for r in _RESULTS if not r[0].startswith("拿到真实模型文本")]
    failed = [r for r in mandatory if not r[1]]

    print("\n" + "=" * 78)
    print(f"确定性断言：{len(mandatory) - len(failed)}/{len(mandatory)} PASS")
    for name, ok, detail in failed:
        print(f"  FAIL  {name}: {detail}")
    if real_ok:
        print("真实网络：已拿到真实模型文本（GLM-4-Flash 实际回包，非降级文案）。")
    else:
        print("真实网络：**未**拿到真实模型文本 —— 结论只到「调用链真的走到模型请求」这一层，")
        print("           不要把场景 2 的桩回包或任何降级文案说成模型回答。")
    print("=" * 78)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
