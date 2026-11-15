"""KG 抽取专用调用预算 + 渠道解耦（防回归 + 首字节阶段生效）。

守这些事：
1. KG 抽取路径拿到的是**新预算**（总 300s / 首字节 120s），不是 writer 的 960s；
2. **默认 writer 路径一字不变**：总预算仍是 settings.writer_total_timeout（960s）、
   首字节不设额外上限（None）——正文生成首字实测 100–305s，不能被误杀；
3. 首字节阶段的 socket/urlopen 超时在抽取路径确实取 min(剩余预算, 120)；
4. `kg_extract.provider` 默认空 = 跟随主渠道（与改造前完全一致）；
   设为 zhipu = 抽取拿到智谱 provider、模型链与库里 `llm.zhipu_models` 一致，
   写正文仍是 MiniMax；未知值抛错（绝不静默回退主渠道）。

跑法：python tests/test_kg_extract_timeout.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label, got, want):
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILED.append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")


def check_true(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILED.append(label)
    print(f"{'PASS' if cond else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")


class _FakeSock:
    def __init__(self, log: list[float]) -> None:
        self._log = log

    def settimeout(self, value: float) -> None:
        self._log.append(value)


class _FakeRaw:
    def __init__(self, log: list[float]) -> None:
        self._sock = _FakeSock(log)


class _FakeFP:
    def __init__(self, log: list[float]) -> None:
        self.raw = _FakeRaw(log)


class _FakeResp:
    """够 `complete_message` / `_read_body` 用的最小响应：一次返回完 body。"""

    def __init__(self, body: bytes, sock_log: list[float]) -> None:
        self._body = body
        self._sent = False
        self.fp = _FakeFP(sock_log)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read1(self, n: int) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._body


def _install_fake_urlopen(sock_log: list[float], call_log: list[float]):
    """替换 urllib.request.urlopen，记录 timeout kwarg 与首字节前的 settimeout。"""
    body = json.dumps({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        call_log.append(timeout)
        return _FakeResp(body, sock_log)

    return fake_urlopen


class _FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """只服务 `load_persisted_llm_config` 的 `db.execute(select(...))`。"""

    def __init__(self, rows) -> None:
        self._rows = rows

    def execute(self, *_args, **_kwargs):
        return _FakeResult(self._rows)


def _setup_minimax(kg_provider: str = ""):
    """把渠道固定成 minimax + 假 key，保证两条路径构造同一渠道、可比较模型链。

    `kg_provider` 用于覆盖 `kg_extract.provider`（默认空 = 跟随主渠道）。
    """
    from app.config import settings
    from app.llm.provider import reset_provider_cache

    settings.llm_provider = "minimax"
    settings.minimax_api_key = "test-key"
    settings.minimax_model = "MiniMax-M3.1-Flash"
    settings.kg_extract_provider = kg_provider
    reset_provider_cache()


def test_defaults():
    print("\n=== 1. 配置默认值（providers.yaml / settings） ===")
    sys.stdout.flush()
    from app import config as cfg
    from app.config import settings

    check("yaml kg_extract.total_timeout", cfg._KG_EXTRACT_DEFAULTS.get("total_timeout"), 300)
    check("yaml kg_extract.first_byte_timeout", cfg._KG_EXTRACT_DEFAULTS.get("first_byte_timeout"), 120)
    check("yaml kg_extract.provider（默认空 = 跟随主渠道）",
          cfg._KG_EXTRACT_DEFAULTS.get("provider"), "")
    check("settings.kg_extract_total_timeout", settings.kg_extract_total_timeout, 300)
    check("settings.kg_extract_first_byte_timeout", settings.kg_extract_first_byte_timeout, 120)
    # 默认值断言不能读全局 settings：本机 .env 可能显式设了 KG_EXTRACT_PROVIDER
    # （当前部署就是 zhipu）。这里构造一个「不读 .env、显式传空」的实例来证明默认语义。
    check("默认空（不读 .env）", cfg.Settings(_env_file=None, kg_extract_provider="").kg_extract_provider, "")
    # writer 默认总预算原值：防有人「顺手」把它改小
    check("settings.writer_total_timeout", settings.writer_total_timeout, 960)


def test_writer_unchanged():
    print("\n=== 2. 默认 writer 路径：预算一字不变（防回归） ===")
    sys.stdout.flush()
    from app.config import settings
    from app.llm.provider import get_llm_provider

    _setup_minimax()
    w = get_llm_provider()
    check("writer.total_timeout == settings 原值", w.total_timeout, settings.writer_total_timeout)
    check("writer.total_timeout", w.total_timeout, 960)
    check("writer.first_byte_timeout（不设额外上限）", w.first_byte_timeout, None)


def test_kg_budget():
    print("\n=== 3. KG 抽取路径拿到新预算 ===")
    sys.stdout.flush()
    from app.config import settings
    from app.llm.provider import get_kg_extract_provider, get_llm_provider

    _setup_minimax()
    w = get_llm_provider()
    k = get_kg_extract_provider()
    check("kg.total_timeout", k.total_timeout, settings.kg_extract_total_timeout)
    check("kg.total_timeout 值", k.total_timeout, 300)
    check("kg.first_byte_timeout", k.first_byte_timeout, 120)
    check_true("kg 与 writer 是不同实例", k is not w)
    check("kg 模型链与 writer 一致", k._model_chain(), w._model_chain())
    check_true("kg 单例复用", get_kg_extract_provider() is k)


def test_first_byte_applied():
    print("\n=== 4. 首字节阶段超时在抽取路径生效 ===")
    sys.stdout.flush()
    from app.llm.provider import get_kg_extract_provider, get_llm_provider

    _setup_minimax()
    k = get_kg_extract_provider()
    w = get_llm_provider()
    msgs = [{"role": "user", "content": "hi"}]

    orig = urllib.request.urlopen
    try:
        # KG：urlopen 用 min(剩余总预算, 120)；首字节前 settimeout 同样被压到 120
        kg_calls: list[float] = []
        kg_socks: list[float] = []
        urllib.request.urlopen = _install_fake_urlopen(kg_socks, kg_calls)
        k.complete_message(msgs)
        check("kg urlopen timeout", kg_calls[0], 120.0)
        check_true("kg 首字节 settimeout <= 120",
                   0 < kg_socks[0] <= 120.0, detail=f"got={kg_socks[0]}")

        # Writer：urlopen 用剩余总预算（≈960），首字节不额外压缩
        w_calls: list[float] = []
        w_socks: list[float] = []
        urllib.request.urlopen = _install_fake_urlopen(w_socks, w_calls)
        w.complete_message(msgs)
        check_true("writer urlopen timeout ≈960（未变）",
                   w_calls[0] is not None and 900 < w_calls[0] <= 960,
                   detail=f"got={w_calls[0]}")
        check_true("writer 首字节 settimeout ≈960（未变）",
                   900 < w_socks[0] <= 960, detail=f"got={w_socks[0]}")
    finally:
        urllib.request.urlopen = orig


def test_reset_both():
    print("\n=== 5. reset_provider_cache 同时清掉两个单例 ===")
    sys.stdout.flush()
    from app.llm.provider import (
        get_kg_extract_provider,
        get_llm_provider,
        reset_provider_cache,
    )

    _setup_minimax()
    kg1 = get_kg_extract_provider()
    w1 = get_llm_provider()
    reset_provider_cache()
    kg2 = get_kg_extract_provider()
    w2 = get_llm_provider()
    check_true("reset 后 kg 重建", kg1 is not kg2)
    check_true("reset 后 writer 重建", w1 is not w2)


def test_kg_channel_default():
    print("\n=== 6. kg_extract.provider 默认空：与改造前完全一致 ===")
    sys.stdout.flush()
    from app.llm.provider import get_kg_extract_provider, get_llm_provider

    _setup_minimax("")
    w = get_llm_provider()
    k = get_kg_extract_provider()
    check_true("默认空：kg 与 writer 同渠道类型", type(k) is type(w),
               detail=f"{type(k).__name__} / {type(w).__name__}")
    check("默认空：kg 模型链 == writer 模型链", k._model_chain(), w._model_chain())
    check("默认空：kg api_key == writer", k.api_key, w.api_key)
    check("默认空：kg base_url == writer", k.base_url, w.base_url)
    check("默认空：kg 总预算 300", k.total_timeout, 300)
    check("默认空：kg 首字节 120", k.first_byte_timeout, 120)
    check("默认空：writer 总预算仍 960", w.total_timeout, 960)
    check("默认空：writer 首字节仍 None", w.first_byte_timeout, None)


def test_kg_channel_zhipu():
    print("\n=== 7. kg_extract.provider=zhipu：智谱渠道 + 库里模型链 ===")
    sys.stdout.flush()
    from app.config import settings
    from app.llm.provider import (
        MiniMaxChatProvider,
        ZhipuChatProvider,
        get_kg_extract_provider,
        get_llm_provider,
    )

    settings.zhipu_api_key = "test-zhipu-key"
    # 与库里 `llm.zhipu_models` 同值：启动加载后 settings 就是这个串
    settings.zhipu_models = "glm-4-flash,glm-z1-flash"
    _setup_minimax("zhipu")
    k = get_kg_extract_provider()
    check_true("kg 是智谱 provider", isinstance(k, ZhipuChatProvider),
               detail=type(k).__name__)
    check("kg 模型链 == 库里 llm.zhipu_models", k._model_chain(),
          ["glm-4-flash", "glm-z1-flash"])
    check("kg 首选模型（链首）", k.model, "glm-4-flash")
    check("kg 用 zhipu key", k.api_key, "test-zhipu-key")
    check("kg 用 zhipu base_url", k.base_url, settings.zhipu_base_url.rstrip("/"))
    check("kg 预算 total_timeout 仍 300", k.total_timeout, 300)
    check("kg 预算 first_byte_timeout 仍 120", k.first_byte_timeout, 120)

    w = get_llm_provider()
    check_true("写正文仍是 MiniMax", isinstance(w, MiniMaxChatProvider),
               detail=type(w).__name__)
    check("writer 模型未变", w.model, "MiniMax-M3.1-Flash")
    check("writer 总预算未变", w.total_timeout, 960)
    check("writer 首字节未变", w.first_byte_timeout, None)


def test_kg_channel_unknown():
    print("\n=== 8. kg_extract.provider 未知值：明确报错、不回退 ===")
    sys.stdout.flush()
    from pydantic import ValidationError

    from app.config import Settings, settings
    from app.llm.provider import (
        MiniMaxChatProvider,
        get_kg_extract_provider,
        get_llm_provider,
    )

    _setup_minimax("zhiphu")
    try:
        get_kg_extract_provider()
    except ValueError as exc:
        check_true("工厂抛 ValueError", True, detail=str(exc)[:80])
    else:
        check_true("工厂抛 ValueError", False, detail="没有抛错，可能静默回退了主渠道")

    w = get_llm_provider()
    check_true("报错不影响写正文渠道", isinstance(w, MiniMaxChatProvider),
               detail=type(w).__name__)

    try:
        Settings(kg_extract_provider="zhiphu")
    except ValidationError as exc:
        check_true("Settings 构造期拒绝未知值",
                   "kg_extract_provider" in str(exc))
    else:
        check_true("Settings 构造期拒绝未知值", False, detail="未知值被放行")

    # 合法值（大小写不敏感）仍可正常构造
    s = Settings(kg_extract_provider="ZHIPU")
    check("合法值 zhipu 保留原值", s.kg_extract_provider, "ZHIPU")


def test_load_persisted_zhipu_models():
    print("\n=== 9. 启动加载：库里 llm.zhipu_models 落进 settings ===")
    sys.stdout.flush()
    from app.config import settings
    from app.llm.provider import get_kg_extract_provider, reset_provider_cache
    from app.routers.settings import load_persisted_llm_config

    snapshot = dict(vars(settings))
    try:
        rows = [
            SimpleNamespace(key="llm.provider", value="minimax"),
            SimpleNamespace(key="llm.zhipu_models", value="glm-4-flash,glm-z1-flash"),
        ]
        load_persisted_llm_config(_FakeSession(rows))
        check("settings.zhipu_models 来自库", settings.zhipu_models,
              "glm-4-flash,glm-z1-flash")
        check("主渠道仍是 minimax（正文路径不变）", settings.llm_provider, "minimax")

        settings.zhipu_api_key = "test-zhipu-key"
        settings.kg_extract_provider = "zhipu"
        reset_provider_cache()
        k = get_kg_extract_provider()
        check("抽取链确实接上库里的智谱链", k._model_chain(),
              ["glm-4-flash", "glm-z1-flash"])
    finally:
        for name in [n for n in vars(settings) if n not in snapshot]:
            delattr(settings, name)
        vars(settings).update(snapshot)
        reset_provider_cache()


if __name__ == "__main__":
    test_defaults()
    test_writer_unchanged()
    test_kg_budget()
    test_first_byte_applied()
    test_reset_both()
    test_kg_channel_default()
    test_kg_channel_zhipu()
    test_kg_channel_unknown()
    test_load_persisted_zhipu_models()
    print(f"\n===== 通过 {PASS} 条，失败 {FAIL} 条 =====")
    if FAILED:
        print("失败项：")
        for name in FAILED:
            print("  -", name)
    sys.exit(0 if FAIL == 0 else 1)
