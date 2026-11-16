/**
 * 语料摄取窗口状态 → 顶部状态条文案（纯函数，便于单测锁口径）。
 *
 * 两条铁律（测试钉死）：
 * 1. failed > 0 时必须渲染「N 个窗口失败」，**绝不**退化成「无数据/无实体」；
 * 2. 窗口表为空（从未摄取）时说的是「尚未摄取」，不是「没有实体」。
 *
 * @param {import("./corpusStatus").CorpusWindowSummary | null} summary
 * @returns {import("./corpusStatus").CorpusStatusView}
 */
export function buildCorpusStatus(summary) {
  if (!summary || summary.ingested !== true || !(summary.total > 0)) {
    return {
      tone: "idle",
      text: "尚未摄取参考语料（窗口表为空）",
      detail: "从未跑过语料摄取，不代表这本书没有实体",
      failureCount: 0,
    };
  }

  const total = summary.total || 0;
  const failed = summary.failed || 0;
  const running = summary.running || 0;
  const pending = summary.pending || 0;
  const success = summary.success || 0;
  const reasons = summary.failed_reasons || {};

  if (failed > 0) {
    const parts = [];
    const parseFailed = reasons.parse_failed || 0;
    if (parseFailed > 0) {
      parts.push(
        (summary.suspected_local_fallback || 0) > 0
          ? `解析失败 ${parseFailed}（疑似渠道不可用）`
          : `解析失败 ${parseFailed}`,
      );
    }
    if ((reasons.channel_unavailable || 0) > 0) {
      parts.push(`渠道不可用 ${reasons.channel_unavailable}`);
    }
    if ((reasons.other || 0) > 0) {
      parts.push(`其他错误 ${reasons.other}`);
    }
    return {
      tone: "error",
      text: `${failed} 个窗口失败 · ${parts.join(" / ") || "原因未记录"}`,
      detail: `共 ${total} 个窗口，成功 ${success} 个；原始错误见下方失败清单`,
      failureCount: failed,
    };
  }

  if (running > 0) {
    return {
      tone: "warn",
      text: `${running} 个窗口摄取中 · 共 ${total} 个窗口`,
      detail: "",
      failureCount: 0,
    };
  }

  if (success === 0) {
    return {
      tone: "warn",
      text: `尚无窗口完成 · ${pending} 个待跑（共 ${total} 个窗口）`,
      detail: "",
      failureCount: 0,
    };
  }

  return {
    tone: "ok",
    text: `${success}/${total} 个窗口已完成${pending > 0 ? ` · ${pending} 个待跑` : ""}`,
    detail: "",
    failureCount: 0,
  };
}
