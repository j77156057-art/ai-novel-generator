import type {
  AssistantPendingAction,
  AssistantReactBlock,
  AssistantSeverity,
} from "@/api/types";

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
  if (action.kind === "body_repair") {
    return {label: "中风险 · 生成局部修订候选", tag: "warn",
      hint: "确认后调用 Writer 与独立 Assistant，写入待审批提案和审计记录；不应用正文，复验未通过不可生效。"};
  }
  if (action.kind === "ingest_attachment") {
    return {
      label: "中风险 · 新增参考语料",
      tag: "warn",
      hint: "确认后清洗附件、抽样复核并新增参考语料；未知残留保留待复查。",
    };
  }
  if (action.kind === "action_proposal") {
    // 动作提案：agent 提「做什么事」。批准与执行是**两次**点击，
    // 确认这一步只是把提案**记下来**，那个动作还没发生。
    return {
      label: "待批准的动作",
      tag: "warn",
      hint: "确认后记下这条动作提案；还要再点一次「批准 → 执行」，那个动作才会真的发生。",
    };
  }
  if (action.kind === "proposal") {
    // 改写提案：卡上带一份**草稿**，用户改或原样确认。
    // 落库的是用户确认的那一版 —— 服务端不替用户写正文。
    return {
      label: "待确认的改写",
      tag: "warn",
      hint: "下面是助手的草稿，可以直接改；确认后存成待批提案，批准并应用后才会生效。",
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

/**
 * 一轮 ReAct 的「本轮结论」标记。
 *
 * ## 为什么前端要分三类
 *
 * 后端给的 `degraded_reason` 带前缀，三类前缀的**含义完全不同**，不能共用文案：
 *
 * - `unsupported_claims:*` —— **答案无据**：模型跑完了，但答案里有话没有证据支撑。
 *   这是「内容」问题，不是「机器」问题 → 说「未经核对」。
 * - `stagnated:*` —— **没收敛**：模型在同一个地方反复打转，被要求换思路仍不换。
 *   → 说「未能收敛」。
 * - 其它（`provider_stub` / `model_error` / `timeout` / `invalid_decision` …）——
 *   **模型侧故障** → 说「本轮未完成」。
 *
 * 把「答案无据」说成「模型故障」会把用户的注意力引到错的地方（去查 key、配额），
 * 而真正要做的是「别信这几句话」。
 *
 * 返回 `null` = 本轮没降级，不显示任何标记（不制造噪音）。
 */
export interface ReactNotice {
  /** 一句话结论（人话）。 */
  label: string;
  /** 标签 class 后缀（颜色只是辅助，信息由文字承载）。 */
  tag: string;
  /** 展开后给用户看的解释。 */
  detail: string;
  /** 命中「无据断言」的具体条目（人话短语）。 */
  claims: string[];
}

export function reactNotice(react: AssistantReactBlock | null | undefined): ReactNotice | null {
  if (!react || react.degraded !== true) return null;
  const reason = String(react.degraded_reason ?? "").trim();
  const claims = (react.unsupported_claims ?? [])
    .map((c) => String(c?.label ?? c?.phrase ?? "").trim())
    .filter(Boolean);

  if (reason.startsWith("unsupported_claims")) {
    return {
      label: "本轮未经核对",
      tag: "err",
      detail: claims.length
        ? `下面这段回答里有说法没有证据支撑，不是模型坏了 —— 引用到的地方请自己核：${claims.join("；")}`
        : "下面这段回答里有说法没有证据支撑，请自行核对后再采信。",
      claims,
    };
  }
  if (reason.startsWith("stagnated")) {
    return {
      label: "本轮未能收敛",
      tag: "warn",
      detail: "助手在同一个地方反复打转，这一轮没走完 —— 换个问法或先看它卡在哪一步。",
      claims,
    };
  }
  // 剥掉机器前缀（`provider_stub：` 这类），只留人话部分。
  const human = reason.includes("：") ? reason.split("：").slice(1).join("：") : reason;
  return {
    label: "本轮未完成",
    tag: "warn",
    detail: human || "这一轮没有正常跑完，下面这段内容请谨慎参考。",
    claims,
  };
}

/** 卡片类型的人话说法；原始 kind 并列显示，便于对账。 */
export function bodyRepairVerificationText(res: Record<string, unknown>): string {
  const acceptance = res.verification_acceptance as Record<string, unknown> | undefined;
  if (acceptance?.local_repair_verification === "accepted") {
    const missing = Array.isArray(acceptance.uncovered) && acceptance.uncovered.includes("missing_saved_contract");
    return `指定矛盾的局部修复已验收；整段计划一致性未知；${missing ? "缺少原始契约，应用仍阻塞；" : "其他语义仍待审核；"}正文未应用`;
  }
  return res.semantic_accepted === true ? "独立复验通过，仍待审批；正文未应用" : "局部修复尚未验收，整段计划一致性未知；正文未应用";
}

export function pendingActionKindLabel(kind: string): string {
  if (kind === "choice") return "选一个";
  if (kind === "body_repair") return "局部修订候选";
  if (kind === "ingest_attachment") return "附件投放";
  if (kind === "proposal") return "提案";
  if (kind === "invoke_generation") return "调动生成";
  if (kind === "action_proposal") return "动作提案";
  return "待确认动作";
}

/**
 * issue.type 的人话说法；原始值在界面上始终并列显示。
 *
 * 界面不出现内部术语：`plothole` / `character_state` 这种 key 单独摆在界面上
 * 用户读不出来（它只对写代码的人有意义）。所以给一个人话标签，原始 key
 * 仍然并列显示 —— 排查时要能对上后端的值。
 */
export function issueTypeLabel(type: string): string {
  const map: Record<string, string> = {
    plothole: "伏笔",
    fact_quality: "事实质量",
    identity: "人物身份",
    character_state: "人物状态",
    style: "文风",
  };
  return map[type] ?? "其它";
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
