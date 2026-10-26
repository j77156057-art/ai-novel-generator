"""段内自我重复清理。

## 为什么需要

v2.272 五章实测：第 5 段 1402 字里约 700 字是复读——同一句对白、同一段心理描写
原样出现两次，7 处成对重复：

    ×2  “傻小子，别以为爹娘不知道你的心思。”徐骁的声音低沉，带着一丝疲惫…
    ×2  徐龙象的眼神黯淡下来，嘴唇微微颤抖，却什么也没说。他的沉默像一块巨石…

这是长文生成的典型退化：模型在长上下文里丢失「我已经写过这句」的位置感，
把已经写过的段落再写一遍。用户读到的是明显的复制粘贴。

## 为什么在落库前做，而不是靠提示词

提示词里已经写了「不要重复已写过的内容」，模型仍然复读——说明这不是指令能解决的，
必须在产出后作确定性处理。同理也不能靠 LLM 重写（多一次调用、多一次波动）。

## 判据

**只删「几乎逐字相同」的段落**，不做语义改写：

- 归一化后完全相同 → 删（去空白、标点、引号）
- 长度 ≥ 30 字且相似度 ≥ 0.9 → 删（捕获「一句话改两个字」的伪重复）
- 长度 < 30 字的短段落（「嗯。」「他没有回答。」）**不判**——
  短句在正常叙事里重复是合法的（对话应答、节奏顿点）

保留**第一次**出现的段落，删后面的。顺序不变，不改写任何正文。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# 段落分隔：一个或多个换行
_PARA_RE = re.compile(r"\n\s*\n|\n")
# 归一化时剥掉的字符：空白 + 中英文标点 + 引号
_NORM_STRIP_RE = re.compile(r"[\s，。！？；：、“”‘’\"'…—\-—（）()《》〈〉【】\[\]·]+")

# 短段落不参与判重：正常叙事里「嗯。」「他没有说话。」重复是合法的
#
# v2.275：阈值必须用**原始长度**（含标点）判断，不能用归一化后长度。
# 归一化会剥掉全部标点，一句话能缩掉 20%：
#   「老者听到这个消息，脸色变得非常难看。他看着沈砚，问道：“你是谁？”」
#   原文 33 字 → 归一化后 26 字 → 被当成「短句」跳过。
# 而这类 33 字的完整句正是复读循环里最高频的重复单元（实测段2 里同一句 ×6）。
_MIN_PARA_LEN = 30
# 相似度阈值。0.9 能捕获「一句话改两三个字」，又不会误杀同一场景的合理铺陈
_SIM_THRESHOLD = 0.9


def _norm(paragraph: str) -> str:
    return _NORM_STRIP_RE.sub("", paragraph)


def strip_self_repetition(text: str) -> tuple[str, dict]:
    """删掉段内几乎逐字重复的段落，返回 (清理后文本, 统计)。

    统计键：
      removed_paragraphs  删掉的段落数
      removed_chars       删掉的字符数
      total_chars         清理前总字符数
      ratio               删除占比（0~1）
      kept                保留段落数
    """
    if not text:
        return text, {"removed_paragraphs": 0, "removed_chars": 0,
                      "total_chars": 0, "ratio": 0.0, "kept": 0}

    # 用 split 保留分隔符，便于原样拼回（不破坏原有空行结构）
    parts = re.split(r"(\n+)", text)
    paragraphs = parts[0::2]      # 偶数位是正文
    separators = parts[1::2]      # 奇数位是换行符

    kept_norms: list[tuple[str, str]] = []   # (归一化, 原文)
    keep_flags: list[bool] = []
    removed_chars = 0
    removed_count = 0

    for para in paragraphs:
        norm = _norm(para)
        duplicated = False

        # 用原始长度判「是不是短句」（见 _MIN_PARA_LEN 注释：归一化会缩水 20%）
        if len(para.strip()) >= _MIN_PARA_LEN:
            for prev_norm, _prev_raw in kept_norms:
                if norm == prev_norm:
                    duplicated = True
                    break
                # 长度差太大就不用算相似度，省时间
                if abs(len(norm) - len(prev_norm)) > max(len(norm), len(prev_norm)) * 0.3:
                    continue
                if SequenceMatcher(None, norm, prev_norm).ratio() >= _SIM_THRESHOLD:
                    duplicated = True
                    break

        if duplicated:
            keep_flags.append(False)
            removed_chars += len(para)
            removed_count += 1
        else:
            keep_flags.append(True)
            if len(para.strip()) >= _MIN_PARA_LEN:
                kept_norms.append((norm, para))

    if removed_count == 0:
        total = len(text)
        return text, {"removed_paragraphs": 0, "removed_chars": 0,
                      "total_chars": total, "ratio": 0.0,
                      "kept": sum(keep_flags)}

    # 拼回：只保留 keep_flags 为 True 的正文段连同它后面的分隔符
    out: list[str] = []
    for i, para in enumerate(paragraphs):
        if keep_flags[i]:
            out.append(para)
            if i < len(separators):
                out.append(separators[i])
    new_text = "".join(out).rstrip() + ("\n" if text.endswith(("\n", "\r")) else "")

    total = len(text)
    return new_text, {
        "removed_paragraphs": removed_count,
        "removed_chars": removed_chars,
        "total_chars": total,
        "ratio": round(removed_chars / total, 4) if total else 0.0,
        "kept": sum(keep_flags),
    }
