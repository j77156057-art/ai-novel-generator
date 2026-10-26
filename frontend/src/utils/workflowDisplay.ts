import type {
  ActionProposalOut,
  WorkflowPendingProposal,
  WorkflowStep,
} from "@/api/types";

/**
 * 工序链面板的展示逻辑（纯函数，组件与测试共用）。
 *
 * 为什么单独成模块：这个面板只有「显示」，但显示里有判断 —— 两条轨怎么分、
 * 哪一步是当前步、状态词怎么说。让组件里直接写这些判断，就没法离线测；
 * 抽成纯函数后，`node --test` 能直接守住它们（见 tests/workflowDisplay.test.ts）。
 *
 * 两条纪律：
 * 1. **状态词优先用服务端给的** `state_label`（服务端才知道「下一步」是哪个、
 *    「没有待批项」和「已完成」不是一回事）。前端只兜底，不自己发明。
 * 2. **界面不出现内部术语**：界面只说「正文第 3 段」「改动较大」，
 *    不出现 target_kind、risk_level、segment_no 这类字段名。
 */

/** 两条轨的人话标题（内部 id 仍是 prompt / body）。 */
export const WORKFLOW_TRACKS: ReadonlyArray<{ id: string; title: string }> = [
  { id: "prompt", title: "① 准备提示词" },
  { id: "body", title: "② 写正文" },
];

/** 状态 → 兜底状态词（服务端没给 state_label 时才用）。 */
const DEFAULT_STATE_LABELS: Record<string, string> = {
  done: "已完成",
  pending: "待做",
  blocked: "卡住",
  skipped: "不适用",
};

/** 状态 → 样式 class 后缀（颜色只是辅助，信息由文字承载）。 */
const STATE_TONE: Record<string, string> = {
  done: "wf-done",
  pending: "wf-pending",
  blocked: "wf-blocked",
  skipped: "wf-skip",
};

/** 面板上的一道工序（含渲染时要用的派生字段）。 */
export interface WorkflowStepView {
  step: WorkflowStep;
  /** 状态词（人话）。 */
  label: string;
  /** 样式 class 后缀。 */
  tone: string;
  /** 是不是当前该动的那一步。 */
  current: boolean;
}

/** 面板上的一条轨。 */
export interface WorkflowTrackView {
  id: string;
  title: string;
  steps: WorkflowStepView[];
}

/** 一道工序的状态词：服务端优先，前端只兜底。 */
export function stepStateLabel(step: WorkflowStep): string {
  const given = (step.state_label ?? "").trim();
  if (given) return given;
  return DEFAULT_STATE_LABELS[step.state] ?? "未知";
}

/** 一道工序的样式 class 后缀。 */
export function stepStateTone(step: WorkflowStep): string {
  return STATE_TONE[step.state] ?? "wf-skip";
}

/**
 * 把工序按两条轨分组。
 *
 * 为什么分组：九条工序平铺成一排时，用户看不出「先备提示词、再写正文」
 * 这个顺序 —— 第二行开头那个「正文 正文审查 待做」看着就像重复标签。
 * 分成两条轨之后，轨标题本身就是路标。
 */
export function workflowTracks(
  steps: WorkflowStep[],
  currentStep: string | null,
): WorkflowTrackView[] {
  const known = WORKFLOW_TRACKS.map((t) => ({ ...t, steps: [] as WorkflowStepView[] }));
  const byId = new Map(known.map((t) => [t.id, t]));
  const extra: WorkflowTrackView[] = [];
  for (const step of steps ?? []) {
    const view: WorkflowStepView = {
      step,
      label: stepStateLabel(step),
      tone: stepStateTone(step),
      current: !!currentStep && step.id === currentStep,
    };
    const bucket = byId.get(step.track);
    if (bucket) {
      bucket.steps.push(view);
    } else {
      // 出现了没登记的轨：不丢，但单开一轨，避免工序凭空消失。
      let lane = extra.find((t) => t.id === step.track);
      if (!lane) {
        lane = { id: step.track, title: step.track, steps: [] };
        extra.push(lane);
      }
      lane.steps.push(view);
    }
  }
  return [...known, ...extra].filter((t) => t.steps.length > 0);
}

/** 动作提案的种类 → 人话（界面不出现 kind 字符串）。 */
const ACTION_KIND_LABELS: Record<string, string> = {
  start_distill: "启动文风蒸馏",
  plan_outline: "规划剧情大纲",
  extend_plan: "扩展规划",
  invoke_generation: "写正文",
  // —— 闭环四类 ——
  close_acceptance: "整段验收",
  apply_body_repair: "定点修复",
  derived_rebuild: "重建派生数据",
  unlock_generation: "解除生成守卫",
};

/** 动作提案种类的人话说法。 */
export function actionKindLabel(kind: string): string {
  return ACTION_KIND_LABELS[kind] ?? "动作";
}

/** 动作提案的风险等级 → 人话。 */
export function actionRiskLabel(risk: string): string {
  if (risk === "high") return "改动大";
  if (risk === "medium") return "改动中等";
  return "改动小";
}

/** 动作提案的状态 → 人话 + 该显示哪个按钮。 */
export function actionProposalState(
  proposal: ActionProposalOut,
): { label: string; action: "approve" | "apply" | "none" } {
  if (proposal.status === "pending") {
    return { label: "等你批", action: "approve" };
  }
  if (proposal.status === "approved") {
    return { label: "等你执行", action: "apply" };
  }
  if (proposal.status === "applied") {
    return { label: "已执行", action: "none" };
  }
  if (proposal.status === "stale") {
    return { label: "已过期（前提变了，重新提一条）", action: "none" };
  }
  return { label: "已拒绝", action: "none" };
}

/** 待批建议的改动大小 → 样式 class（文字已经写明大小，这里只是补一个色标）。 */
export function pendingProposalTone(proposal: WorkflowPendingProposal): string {
  const level = (proposal.risk_level || "").toLowerCase();
  if (level === "high") return "err";
  if (level === "medium") return "warn";
  return "";
}

/** 文本提案改的是哪一类内容 → 人话（不出现 target_kind 这种内部名）。 */
const PROPOSAL_TARGET_LABELS: Record<string, string> = {
  generation_text: "正文",
  project_plot_bible: "故事设定",
  prompt_override: "系统提示词",
  novel_chunk_text: "参考语料",
};

/** 文本提案改的是哪一类内容 → 人话。 */
export function proposalTargetLabel(targetKind: string): string {
  return PROPOSAL_TARGET_LABELS[targetKind] ?? "内容";
}

/** 文本提案的状态 → 人话。 */
export function proposalStateLabel(status: string): string {
  if (status === "applied") return "已生效";
  if (status === "approved") return "已批准，待写入";
  if (status === "rejected") return "已拒绝";
  if (status === "stale") return "已过期（内容被改过）";
  if (status === "rolled_back") return "已回滚";
  return "等你批";
}
