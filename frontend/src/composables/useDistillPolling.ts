/**
 * 蒸馏进度的轮询与展示状态。
 *
 * 从 `AssistantWorkspace.vue` 抽出：进度**不靠前端自己算**，每 3 秒拉一次
 * `GET .../notices`（同时返回最近一条任务 + 未消费通知），把未消费通知交给
 * 页面转成助手气泡。后台标签页暂停轮询，回到前台立刻补一轮。
 */
import { computed, ref } from "vue";
import type { DistillJob, WizardNotice } from "@/api/types";

export function useDistillPolling(opts: {
  /** 拉取通知 + 最近一条蒸馏任务（由页面提供，便于复用自己的 api 层）。 */
  fetchNotices: () => Promise<{ distill: DistillJob | null; items: WizardNotice[] }>;
  /** 未消费通知的落地（页面负责渲染气泡与 ack）。 */
  onNotices: (items: WizardNotice[]) => void | Promise<void>;
  /** 页面是否仍在前台（keep-alive 停用/卸载后为 false）。 */
  isAlive: () => boolean;
}) {
  const distillJob = ref<DistillJob | null>(null);
  let timer: ReturnType<typeof setTimeout> | null = null;

  /** 「疑似卡住 / 运行中 / 排队」这三种才需要持续轮询。 */
  const distillActive = computed(() => {
    const h = distillJob.value?.health;
    return h === "queued" || h === "running" || h === "stalled";
  });

  /**
   * 百分比只在**总数探明**时才显示；total=0 绝不拿 0 当分母编一个数。
   * 运行中上限 99%：块提炼完之后还有收尾段，显示 100% 但仍在跑就是在撒谎。
   */
  const distillPercent = computed<number | null>(() => {
    const j = distillJob.value;
    if (!j || !j.progress_total) return null;
    const raw = Math.floor((j.progress_current / j.progress_total) * 100);
    return Math.max(0, Math.min(distillActive.value ? 99 : 100, raw));
  });

  const DISTILL_TAG: Record<string, string> = {
    queued: "排队",
    running: "运行中",
    stalled: "疑似卡住",
    success: "已完成",
    failed: "失败",
  };

  function distillTag(job: DistillJob | null): string {
    if (!job) return "";
    return DISTILL_TAG[job.health ?? job.status] ?? job.status;
  }

  /** 进度条里那句话：优先用后端给的 health_note（带具体分钟数）。 */
  function distillSentence(job: DistillJob | null): string {
    if (!job) return "";
    if (job.health_note) return job.health_note;
    if (job.stage) return job.stage;
    return job.status;
  }

  function schedule(delay = 3000): void {
    if (!opts.isAlive()) return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => void poll(), delay);
  }

  async function poll(): Promise<void> {
    if (!opts.isAlive()) return;
    // 后台标签页不轮询：既省请求，也避免回来时被一串过期回执淹没。
    if (document.hidden) return;
    try {
      const res = await opts.fetchNotices();
      distillJob.value = res.distill;
      await opts.onNotices(res.items);
    } catch {
      /* 网络闪断：保持上一次的进度，下一轮继续 */
    }
    schedule();
  }

  /** 回到前台立刻补一轮（后台期间轮询链已停）。 */
  function onVisibilityChange(): void {
    if (document.hidden || !opts.isAlive()) return;
    schedule(0);
  }

  function stop(): void {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }

  return {
    distillJob,
    distillActive,
    distillPercent,
    distillTag,
    distillSentence,
    schedule,
    poll,
    onVisibilityChange,
    stop,
  };
}
