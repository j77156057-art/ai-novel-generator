<script setup lang="ts">
import type { DistillJob } from "@/api/types";

const props = defineProps<{
  /** 最近一条蒸馏任务；父组件用 distillActive 控制是否渲染。 */
  job: DistillJob | null;
  /** 状态标签文字（排队 / 运行中 / 疑似卡住 …）。 */
  tag: string;
  /** 进度条下方那句话（后端 health_note 优先）。 */
  sentence: string;
  /** 0–99 的百分比；总数未探明时为 null（不显示进度条）。 */
  percent: number | null;
}>();
</script>

<template>
  <div class="wz-distill" :class="{ stalled: props.job?.health === 'stalled' }">
    <div class="wz-distill-head">
      <span class="tag" :class="props.job?.health === 'stalled' ? 'warn' : 'accent'">
        {{ props.tag }}
      </span>
      <span class="wz-distill-stage">{{ props.job?.stage || "提炼中" }}</span>
      <span v-if="props.percent !== null" class="mono wz-distill-pct">
        {{ props.percent }}%
      </span>
    </div>
    <div v-if="props.percent !== null" class="wz-distill-bar">
      <span :style="{ width: props.percent + '%' }" />
    </div>
    <p class="faint wz-distill-note">{{ props.sentence }}</p>
  </div>
</template>

<style scoped>
/* 蒸馏进度：一句话 + 百分比，不占大屏 */
.wz-distill {
  margin: var(--gap-sm) 0;
  padding: 9px 11px;
  border: var(--border-w) solid var(--border-strong);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
}
.wz-distill.stalled {
  border-left-color: var(--warn, #c98a1b);
}
.wz-distill-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.wz-distill-stage {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-sm);
  color: var(--fg);
}
.wz-distill-pct {
  flex: none;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--fg);
}
.wz-distill-bar {
  margin-top: 6px;
  height: 4px;
  border-radius: 2px;
  background: var(--border-strong);
  overflow: hidden;
}
.wz-distill-bar > span {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 0.4s ease;
}
.wz-distill-note {
  margin: 6px 0 0;
  font-size: var(--fs-xs);
  line-height: 1.6;
}
</style>
