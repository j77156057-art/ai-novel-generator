<script setup lang="ts">
/**
 * 「混进了别的书」的体检结论块 —— 投放卡与语料页共用。
 *
 * ## 为什么抽成组件
 *
 * 这块有两处渲染（投放时在待确认卡上、入库后在语料页），两处的**结论内容**
 * 必须一致：混了几段、独占词是哪些、占了多大比例。
 * 抽出来之后，渲染层的约定可以**被组件测试直接断言**，而不是靠两处模板
 * 各写各的、指望它们碰巧一样。
 *
 * ## 界面纪律
 *
 * - **只报不删是默认**：勾选框默认不勾。要删得人主动点 ——
 *   删的是整章正文，判据万一错了丢的是真内容。
 * - **勾选框只在检出时出现**：没检出时界面上不该有一段
 *   「你要不要剔除别的书」的噪音（由父组件的 v-if 保证）。
 * - **文字 + 色标双编码**：结论用 `notice warn`，独占词用 `tag warn`，
 *   不靠颜色单独承载信息。
 */
import { computed } from "vue";
import type { ForeignBlocksReport } from "@/api/types";
import { distinctChapterTitles } from "@/utils/corpusReport";

const props = defineProps<{
  /** 体检结论（父组件保证这里已判定为「检出了」）。 */
  report: ForeignBlocksReport;
  /** 是否是「投放前」场景：投放时要给勾选框，语料页（已入库）不给。 */
  selectable?: boolean;
  /** 剔除勾选状态（v-model）。 */
  modelValue?: boolean;
}>();

const emit = defineEmits<{ (e: "update:modelValue", v: boolean): void }>();

/** 一句话结论：混进了几段、约多少字、占多大比例。 */
const summary = computed(() => {
  const r = props.report;
  const pct = ((r.ratio ?? 0) * 100).toFixed(1);
  return `这份 txt 里混进了 ${r.block_count} 段别的书（约 ${r.chars.toLocaleString()} 字，占 ${pct}%）`;
});

function onToggle(e: Event): void {
  emit("update:modelValue", (e.target as HTMLInputElement).checked);
}
</script>

<template>
  <div class="foreign-box">
    <div class="blk-head">混进了别的书</div>
    <div class="notice warn">{{ summary }}</div>

    <ul class="foreign-blocks">
      <li v-for="(b, i) in report.blocks ?? []" :key="i">
        <span class="mono faint">{{ b.chapter_count }} 章</span>
        <span v-if="distinctChapterTitles(b.chapter_titles).length" class="faint">
          （{{ distinctChapterTitles(b.chapter_titles).slice(0, 3).join("、") }}{{ distinctChapterTitles(b.chapter_titles).length > 3 ? " 等" : "" }}）
        </span>
        <span v-if="b.terms?.length" class="tag warn">
          独占词：{{ b.terms.slice(0, 5).join("、") }}
        </span>
        <div v-if="b.sample" class="prose foreign-sample">{{ b.sample }}</div>
      </li>
    </ul>

    <label v-if="selectable" class="foreign-check">
      <input type="checkbox" :checked="modelValue === true" @change="onToggle" />
      <span>
        剔除这几段别的书再入库
        <span class="faint">
          （会删掉约 {{ report.chars.toLocaleString() }} 字；不勾则照常入库，体检结论会留在这本语料的报告里）
        </span>
      </span>
    </label>
  </div>
</template>

<style scoped>
/* 小标题：与其它块级标题同一套约定（各组件 scoped 内各自定义一份）。 */
.blk-head {
  color: var(--fg-faint);
  font-weight: 700;
  font-size: var(--fs-xs);
}
.foreign-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border: var(--border-w) solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg-2, var(--bg));
}
.foreign-blocks {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: var(--fs-xs);
  line-height: 1.7;
}
.foreign-blocks li {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
}
.foreign-sample {
  flex-basis: 100%;
  max-height: 5.5em;
  overflow: auto;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}
.foreign-check {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: var(--fs-sm);
  line-height: 1.6;
  cursor: pointer;
}
.foreign-check input {
  margin-top: 0.25em;
}
</style>
