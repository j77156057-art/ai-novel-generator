<script setup lang="ts">
/**
 * 文风蒸馏进度条。
 *
 * ## 为什么这个组件存在
 *
 * 蒸馏是分钟级长任务（833 章语料跑完整档实测 42 分钟）。之前的界面点完按钮
 * 什么反馈都没有就直接跳下一步，用户无法判断「到底开始蒸馏了没有」。
 * 这里把**后台任务的实时状态**显式画出来：阶段文字 + 百分比 + 明确的状态结论。
 *
 * ## 双编码（不靠颜色单打独斗）
 *
 * 每个状态都同时给出**文字**和**色标**：「排队中 / 运行中 / 已完成 / 失败」
 * 这行字始终在场。色弱、单色打印、截图转灰度时结论照样读得出来 ——
 * 颜色只做冗余强化，不独自承载语义。
 *
 * ## total 为 0 时的不确定态
 *
 * 后端在语料加载采样之前**探不明**一共有多少块，此时 `progress_total === 0`。
 * 这里渲染成流动条纹而不是「0 / 0 = 0%」——后者会让用户以为卡死了。
 */
import { computed } from "vue";
import type { DistillJob, DistillStatus } from "@/api/types";

const props = withDefaults(
  defineProps<{
    /** 任务状态快照（父组件轮询更新同一个对象）。 */
    job: DistillJob;
    /** 当前档位的可读名（"完整 / 平衡 / 快速"）；拿不到就退化成档位 key。 */
    tierName?: string | null;
    /** 紧凑模式：只画状态条 + 进度条，不画成功后摘要（跨步骤常驻的那个横幅用）。 */
    compact?: boolean;
  }>(),
  { tierName: null, compact: false },
);

/** 每个状态的「文字 + 色标」。**两者必须同时出现**，见文件头。 */
const STATUS_META: Record<DistillStatus, { label: string; tag: string }> = {
  pending: { label: "排队中", tag: "info" },
  running: { label: "运行中", tag: "accent" },
  success: { label: "已完成", tag: "ok" },
  failed: { label: "失败", tag: "err" },
};

const meta = computed(
  () => STATUS_META[props.job.status] ?? { label: props.job.status, tag: "warn" },
);

const active = computed(
  () => props.job.status === "pending" || props.job.status === "running",
);

/** true = 总数还没探明，走不确定态（不显示百分比）。 */
const indeterminate = computed(() => !props.job.progress_total);

/**
 * 百分比（0~100）。
 *
 * 刻意 **clamp 到 100**：后端对剩余未探明语料是按「已探明语料的平均块数」
 * 估算总数的，估算偶尔会略小于实际完成数，直接除会得到 103% 这种越界值。
 */
const percent = computed(() => {
  const total = props.job.progress_total;
  if (!total) return 0;
  const raw = (props.job.progress_current / total) * 100;
  return Math.max(0, Math.min(100, Math.round(raw)));
});

/** 「第 5 / 12 块」——不确定态下只给已完成数，不编一个分母。 */
const counterText = computed(() => {
  if (indeterminate.value) return `已完成 ${props.job.progress_current} 块`;
  return `${props.job.progress_current} / ${props.job.progress_total} 块`;
});

/** 阶段文字兜底：后端还没来得及写 stage 时也得有一句人话。 */
const stageText = computed(() => props.job.stage || "正在等待服务端上报进度…");

/** 已经跑了的时长足迹。没有 finished_at 时显示 started_at，都没有就留空。 */
const timeHint = computed(() => {
  const t = props.job.finished_at || props.job.started_at;
  if (!t) return "";
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${props.job.finished_at ? "结束" : "开始"}于 ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
});

/** 成功后的摘要（每一项都可能为 null；为 null 的项不显示，不显示"0"蒙混）。 */
const summary = computed(() => {
  const items: string[] = [];
  if (props.job.n_chapters != null) items.push(`采样 ${props.job.n_chapters} 章`);
  if (props.job.rule_count != null) items.push(`提炼 ${props.job.rule_count} 条规则`);
  if (props.job.rejected != null) items.push(`拒伪规则 ${props.job.rejected} 条`);
  return items;
});
</script>

<template>
  <div class="distill" :class="{ 'is-active': active, 'is-compact': compact }">
    <div class="distill-head">
      <span class="tag" :class="meta.tag">
        <!-- 文字状态在前：色弱/灰度下靠这行字读结论 -->
        <span v-if="active" class="spinner" aria-hidden="true"></span>
        {{ meta.label }}
      </span>
      <span class="distill-title">文风蒸馏</span>
      <span v-if="tierName" class="distill-tier">档位：{{ tierName }}</span>
      <span v-if="timeHint" class="faint distill-time">{{ timeHint }}</span>
    </div>

    <div
      class="distill-bar"
      role="progressbar"
      :aria-valuemin="0"
      :aria-valuemax="indeterminate ? undefined : 100"
      :aria-valuenow="indeterminate ? undefined : percent"
      :aria-label="`文风蒸馏：${stageText}`"
    >
      <div
        v-if="!indeterminate"
        class="distill-fill"
        :class="`fill-${meta.tag}`"
        :style="{ width: percent + '%' }"
      ></div>
      <div v-else class="distill-fill indeterminate" :class="`fill-${meta.tag}`"></div>
    </div>

    <div class="distill-foot">
      <span class="distill-stage">{{ stageText }}</span>
      <span class="faint mono distill-count">{{ counterText }}</span>
      <span v-if="!indeterminate" class="distill-pct">{{ percent }}%</span>
    </div>

    <!-- 失败必须给出原因：进度条转半天然后什么都不说，是最难自查的一种失败 -->
    <div v-if="job.status === 'failed'" class="notice err distill-error">
      <strong>蒸馏失败</strong>
      <div class="distill-error-body">{{ job.error || "（服务端未返回原因）" }}</div>
      <div class="faint">
        常见原因：未配置蒸馏用的模型密钥、模型返回超时、参考语料为空。修好后重新点「开始蒸馏」即可。
      </div>
    </div>

    <div v-else-if="job.status === 'success'" class="distill-summary">
      <span class="tag ok">已写入叙事档案</span>
      <!-- 用本组件自己的 .distill-fact：`.tier-fact` 是 ProjectWorkspace 的
           scoped 样式，跨组件不会生效，抄过来会得到一个没样式的裸 span。 -->
      <span v-for="s in summary" :key="s" class="distill-fact">{{ s }}</span>
    </div>

    <div v-else-if="job.status === 'pending'" class="faint distill-tip">
      任务已提交，正在等待后台线程接手。可以放心继续下一步 —— 蒸馏在后台跑，进度不会丢。
    </div>
  </div>
</template>

<style scoped>
.distill {
  padding: 12px 14px;
  border: var(--border-w) solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.distill.is-active {
  border-color: var(--accent-line);
  background: var(--accent-soft);
}
.distill.is-compact {
  padding: 10px 12px;
  gap: 6px;
}
.distill-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.distill-title {
  font-size: var(--fs-sm);
  font-weight: 700;
}
.distill-tier,
.distill-time {
  font-size: var(--fs-xs);
}
.distill-bar {
  height: 8px;
  border-radius: var(--radius-full);
  background: var(--bg-elev);
  border: var(--border-w) solid var(--border);
  overflow: hidden;
}
.distill-fill {
  height: 100%;
  width: 0;
  border-radius: var(--radius-full);
  transition: width 0.4s ease;
}
/* 四种状态的填充色 —— 只是冗余强化，语义由旁边的文字标签承担 */
.distill-fill.fill-info {
  background: var(--info);
}
.distill-fill.fill-accent {
  background: var(--accent);
}
.distill-fill.fill-ok {
  background: var(--ok);
}
.distill-fill.fill-err {
  background: var(--danger);
}
/* 不确定态：流动条纹表示「在动，但总数还没探明」 */
.distill-fill.indeterminate {
  width: 40%;
  animation: distill-slide 1.2s ease-in-out infinite;
}
@keyframes distill-slide {
  0% {
    margin-left: -40%;
  }
  100% {
    margin-left: 100%;
  }
}
@media (prefers-reduced-motion: reduce) {
  .distill-fill.indeterminate {
    animation: none;
    width: 100%;
    opacity: 0.45;
  }
  .distill-fill {
    transition: none;
  }
}
.distill-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: var(--fs-xs);
}
.distill-stage {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--fg-muted);
}
.distill-pct {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.distill-error {
  margin-top: 2px;
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.distill-error-body {
  margin: 4px 0;
  white-space: pre-wrap;
  word-break: break-word;
}
.distill-summary {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.distill-fact {
  font-size: var(--fs-xs);
  color: var(--fg-muted);
  background: var(--bg-elev);
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-full);
  padding: 1px 8px;
}
.distill-tip {
  font-size: var(--fs-xs);
  line-height: 1.6;
}
/* 小转圈只做「在动」的暗示，状态文字始终在旁边 → 不靠动画传达语义 */
.spinner {
  display: inline-block;
  width: 8px;
  height: 8px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: distill-spin 0.8s linear infinite;
}
@keyframes distill-spin {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .spinner {
    animation: none;
  }
}
</style>
