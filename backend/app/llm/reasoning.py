"""剥离模型吐出的推理块（`<think>…</think>`）。

## 为什么需要

实测事故（2026-10-14）：`style-distillation` 分支跑完整规模时，生成的第 3 段
**1662 字里有 826 字（49%）是模型的思考过程**，正文以这个开头：

    <think>
    好的，我现在需要处理用户提供的这个改写任务。首先，我得仔细阅读用户的要求…

也就是模型把「我要先想一遍」的过程原样写进了小说正文。同一批次里另外
两段是干净的 → **间歇性发生**，且与提示词内容无关（三臂对照 12 段零复现，
详见 docs 记录）。唯一那次泄漏对应的调用耗时 384 秒（远超 120s 超时），
怀疑与 provider 降级/重试有关，但未证实。

## 为什么在 provider 层剥

这是模型输出进入系统的**唯一收口**：
- `chat()` / `summarize()` / `summarize_rollup()` → 都走 `_complete()`
- `stream_chat()` → 生成与改写两条流式路径都走它

在别处剥会漏掉其中一条；而且叙事提炼那侧还要 `json.loads` 解析模型输出，
多一个 think 块就直接解析失败。

**这是确定性防护，不依赖根因**：根因还没查实，但"落库的东西不该含推理
过程"这条不变量无论何时都成立。
"""
from __future__ import annotations

import re

_OPEN = "<think>"
_CLOSE = "</think>"

_CLOSED_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_reasoning(text: str) -> str:
    """剥掉完整文本里的推理块（用于非流式路径）。"""
    if not text:
        return text
    if _OPEN not in text and _CLOSE not in text:
        return text
    out = _CLOSED_BLOCK.sub("", text)
    # 流被截断导致的未闭合块：从 <think> 到文末都是推理内容
    i = out.find(_OPEN)
    if i >= 0:
        out = out[:i]
    return out.strip()


def _partial_open_len(buf: str) -> int:
    """buf 末尾有多长的一段是 `<think>` 的前缀（可能是被 token 边界切断的开标签）。"""
    for n in range(min(len(buf), len(_OPEN) - 1), 0, -1):
        if _OPEN.startswith(buf[-n:]):
            return n
    return 0


class ReasoningFilter:
    """流式过滤器：逐 token 喂入，吐出「确定不含推理」的部分。

    不能逐块简单替换——开标签可能被 token 边界切成 `"<thi"` + `"nk>"`，
    那样第一块就会漏出去。这里对「可能是开标签前缀」的尾巴**持有不发**，
    等下一个 token 到达再判断，因此不会误吐、也不会把推理内容吐出去。

    无推理块时**零缓冲**（`_partial_open_len` 返回 0 的常见情况），
    不影响「首字即显」的低延迟承诺。
    """

    __slots__ = ("_buf", "_in_think", "_saw", "_skip_nl")

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False
        self._saw = False
        self._skip_nl = False

    def feed(self, piece: str) -> str:
        """喂入一个 delta，返回可以安全下发的文本（可能为空串）。"""
        if not piece:
            return ""
        if self._skip_nl:
            # 刚闭合一个推理块：吞掉开头的换行（模型习惯把</think>\n 当分隔符）。
            # 只吃\r\n，不吃空格/缩进 —— 缩进可能是正文排版的一部分。
            stripped = piece.lstrip("\r\n")
            if not stripped:
                return ""          # 还是纯换行，继续吞
            self._skip_nl = False
            piece = stripped
        buf = self._buf + piece
        out: list[str] = []
        while True:
            if self._in_think:
                i = buf.find(_CLOSE)
                if i < 0:
                    self._buf = buf  # 还在推理中，继续攒（丢弃）
                    return ""
                buf = buf[i + len(_CLOSE):]
                # 闭标签后紧跟的换行要跳过，但不能在这里做 lstrip：
                # 那个换行往往作为**独立的一次 feed** 到达的，当时不在
                # 缓冲区里，lstrip 看不到它。所以改成留一个状态，在 feed 入口持续跳。
                self._skip_nl = True
                self._in_think = False
                continue
            i = buf.find(_OPEN)
            if i >= 0:
                out.append(buf[:i])  # 推理之前的正文，可以下发
                buf = buf[i + len(_OPEN):]
                self._in_think = True
                self._saw = True
                continue
            # 没有完整开标签：把可能是开标签前缀的尾巴留住，其余下发
            keep = _partial_open_len(buf)
            out.append(buf[: len(buf) - keep])
            self._buf = buf[len(buf) - keep:]
            return "".join(out)

    def flush(self) -> str:
        """流结束时收尾。

        未闭合的推理块（被截断）**整段丢弃**——它一定是思考过程，
        不是正文。已经持有的非推理尾巴正常下发。
        """
        rest = "" if (self._in_think or self._skip_nl) else self._buf
        self._buf = ""
        self._in_think = False
        self._skip_nl = False
        return rest

    @property
    def saw_reasoning(self) -> bool:
        """本流是否**曾经**出现过推理块（粘性标志，flush 之后仍可查）。"""
        return self._saw