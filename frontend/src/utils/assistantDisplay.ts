import type { AssistantPendingAction, AssistantSeverity } from "@/api/types";

/**
 * 助手界面的展示映射（纯函数，页面与子组件共用，避免两处实现漂移）。
 * 全部走「文字 + 标签样式」双编码：不靠颜色单打独斗。
 */

/** severity 同时给文字标签 + 标签 class，双编码。 */
export function severityMeta(s: string): { label: string; tag: string } {
  const map: Record<AssistantSeverity, { label: string; tag: string }> = {
    error: { label: "错误 error", tag: "err" },
    warn: { label: "警告 warn", tag: "warn" },
    info: { label: "提示 info", tag: "info" },
  };
  return map[s as AssistantSeverity] ?? { label: s, tag: "" };
}

/**
 * 待确认卡片的危险等级。
 *
 * 为什么单独一套而不用 severity：severity 说的是「问题有多严重」，
 * 这里说的是「按下确认会发生什么」—— 用户在决定要不要点那个按钮，
 * 判据是后果，不是问题的严重程度。
 *
 * 两档（都带文字，不靠颜色单独承载信息）：
 *   - 高（err）：会**顶掉**已有内容 —— regenerate_segment 重写某段
 *   - 中（warn）：只**新增**，已有段落不受影响 —— generate_next 续写、
 *     以及各类提案（提案本身不改正文，真正写入还要再过审批与生效）
 *
 * 判据优先级：`detail.destructive`（服务端显式标注，权威）→ 退回从
 * `body.action` 推断（覆盖式生成）。之所以两条都留：detail 是给界面看的
 * 标注，body 是实际要发的请求体；万一服务端只填了后者，卡片仍能正确
 * 分出「覆盖」与「新增」，不会因为少一个标注就把危险动作显示成安全。
 */
export function pendingActionRiskMeta(
  action: AssistantPendingAction,
): { label: string; tag: string; hint: string } {
  const detail = action.detail ?? {};
  const body = action.body ?? {};
  // 服务端已标注时以它为准；它只在「覆盖 / 不覆盖」上有权威性，
  // 但前提是别和服务端自己能算出来的矛盾 —— 所以下面仍保留推断作为兜底。
  const destructive = detail.destructive === true || body.action === "regenerate_segment";
  if (destructive) {
    const seg = body.segment_no ?? detail.segment_no;
    return {
      label: `高风险 · 覆盖已有段落${seg != null ? ` 第 ${seg} 段` : ""}`,
      tag: "err",
      hint:
        typeof detail.risk_hint === "string" && detail.risk_hint
          ? detail.risk_hint
          : "确认后已有正文会被顶掉（旧内容进版本历史，可用版本回滚兜底）。",
    };
  }
  return {
    label: "中风险 · 追加新段落",
    tag: "warn",
    hint:
      typeof detail.risk_hint === "string" && detail.risk_hint
        ? detail.risk_hint
        : "确认后 Writer 会新写一段并入库，已有段落不受影响。",
  };
}

/** 卡片类型的人话说法；原始 kind 并列显示，便于对账。 */
export function pendingActionKindLabel(kind: string): string {
  if (kind === "proposal") return "提案";
  if (kind === "invoke_generation") return "调动生成";
  return "待确认动作";
}

/** evidence.source 的人话说法；原始值在界面上始终并列显示。 */
export function sourceLabel(source: string): string {
  const s = (source || "").toLowerCase();
  if (s === "generation") return "本次生成正文";
  if (s === "novel" || s.startsWith("novel:")) return "参考语料";
  if (s === "kg") return "知识图谱 / 提醒";
  if (s === "fact") return "事实账本";
  if (s === "run") return "助手运行日志";
  if (s === "generated") return "助手调动生成的正文";
  return "其它来源";
}

/** 后端 ISO8601 → 展示用「YYYY-MM-DD HH:MM:SS」（只做 T→空格，不做时区换算）。 */
export function fmtTime(value: string | null | undefined): string {
  if (!value) return "—";
  return value.replace("T", " ").slice(0, 19);
}

/** 长哈希掐头去尾；null → —。 */
export function shortHash(hash: string | null | undefined): string {
  if (!hash) return "—";
  return hash.length > 20 ? `${hash.slice(0, 12)}…${hash.slice(-4)}` : hash;
}
