"""测试桩共享基类：给「只有同步 chat()」的假 provider 补 async_chat()。

## 为什么需要

蒸馏链路自 v2.227 起把各块 prompt 拍平，统一走 `_gather_chat` →
`provider.async_chat(...)`（真实 provider 由 `app.llm.provider.LLMProvider`
基类把同步 `chat()` 包一层，所以生产侧无感）。

测试桩是独立小类、**不继承** provider 基类，因此必须自己补这个入口。少了它，
并发调度一律 AttributeError → `_gather_chat` 把它收成 Exception → 上层判「所有块
都拿不到可用结果」→ 抛出「蒸馏失败：N 个语料块全部调用模型失败」。

后果比报错更糟：一部分断言会**以假乱真地通过**——比如「全都失败时必须抛
ValueError」那条，AttributeError 也让它抛了 ValueError，测试看起来是绿的，
但真正在验的东西没被验到。

## 为什么直接同步委托、不用线程池

桩是纯内存函数，没有 IO，套 executor 只会引入线程调度带来的**非确定性**，
让「第 N 次调用」这类计数断言变脆。直接 `return self.chat(...)` 既保序又保真。
"""


class AsyncChatStub:
    """混入后即具备 async_chat()，委托给子类的同步 chat()。"""

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        raise NotImplementedError  # pragma: no cover - 由子类实现

    async def async_chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        return self.chat(system, user, constraints=constraints)
