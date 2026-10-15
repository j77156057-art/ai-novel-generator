<script setup lang="ts">
/**
 * 统一 issues 渲染器（AssistantIssueList）。
 *
 * diagnose / analyze-logs / audit-corpus / audit-kg / audit-generation
 * 五种响应共享同一套九字段 issues 结构（后端 findings 是 issues 的兼容
 * 别名）——界面只写这一份渲染，多个入口复用，杜绝「审核的 issue 长得
 * 和诊断不一样」的第二套实现。
 *
 * 助手页瘦身后只剩问答一个入口，诊断 / 审核 / 日志分析全部由 agent 自主
 * 调度，本组件作为「agent 跑出来的问题」在回答里直接复用。
 *
 * 组件只负责**展示**：issue 是既成事实，不是待批草案，所以没有任何
 * 「起草 / 采纳」按钮 —— 要改什么由 agent 在回答里提出待确认卡片，
 * 由用户点确认，写操作的闸门只有那一个。
 */
import { computed, reactive, watch } from "vue";
import type { AssistantEvidence, AssistantIssue, AssistantSeverity } from "@/api/types";
import { severityMeta, sourceLabel } from "@/utils/assistantDisplay";

const props = defineProps<{
  /**
   * 结论文字。**可选**：调用方若已经在别处渲染了同一段回答（例如助手页的
   * 对话气泡），传空串即可避免同一段话在界面上出现两遍。
   */
  answer?: string;
  issues: AssistantIssue[];
  evidence: AssistantEvidence[];
  confidence: number;
  rulesEvaluated: string[];
  agentRunId: number;
}>();

// 组件内展开态（issue id 唯一）；换一次响应清一次，避免旧展开态串场。
const expanded = reactive<Record<string, boolean>>({});
watch(
  () => props.issues,
  () => {
    for (const k of Object.keys(expanded)) delete expanded[k];
  },
);
function isOpen(key: string): boolean {
  return expanded[key] === true;
}
function toggle(key: string): void {
  expanded[key] = !expanded[key];
}

/** 按 severity 分组（error → warn → info），组内保持后端返回顺序。 */
const issuesBySeverity = computed(() => {
  const buckets: Record<AssistantSeverity, AssistantIssue[]> = {
    error: [],
    warn: [],
    info: [],
  };
  for (const issue of props.issues) {
    const sev: AssistantSeverity =
      issue.severity === "error" || issue.severity === "warn" ? issue.severity : "info";
    buckets[sev].push(issue);
  }
  const order: AssistantSeverity[] = ["error", "warn", "info"];
  return order
    .filter((sev) => buckets[sev].length > 0)
    .map((sev) => ({ severity: sev, items: buckets[sev] }));
});
</script>

<template>
  <div class="issues-out">
    <div class="row tight" style="align-items: center">
      <span class="tag info mono">AgentRun #{{ agentRunId }}</span>
      <span class="tag mono">命中 {{ issues.length }} 个问题</span>
      <span class="tag mono">置信度 {{ confidence.toFixed(2) }}</span>
    </div>

    <!-- 结论只在调用方没自己渲染时出现，避免同一段话显示两遍 -->
    <div v-if="answer" class="blk">
      <div class="blk-head">结论</div>
      <div class="prose snippet" :class="{ expanded: isOpen('issue-answer') }">
        {{ answer }}
      </div>
      <button class="link-btn" @click="toggle('issue-answer')">
        {{ isOpen("issue-answer") ? "收起" : "展开全文" }}
      </button>
    </div>

    <div v-if="rulesEvaluated.length" class="rules-line">
      <span class="blk-head">已执行规则：</span>
      <span v-for="r in rulesEvaluated" :key="r" class="tag mono">{{ r }}</span>
    </div>

    <div v-if="!issuesBySeverity.length" class="empty">
      未命中任何已知问题模式。
      <span class="faint">（规则全部已执行；这不等同于「绝对没问题」）</span>
    </div>

    <div v-else class="sev-groups">
      <div v-for="g in issuesBySeverity" :key="g.severity" class="sev-group">
        <div class="sev-head">
          <span class="tag" :class="severityMeta(g.severity).tag">
            {{ severityMeta(g.severity).label }}
          </span>
          <span class="faint" style="font-size: var(--fs-xs)">{{ g.items.length }} 条</span>
        </div>
        <div v-for="issue in g.items" :key="issue.id" class="issue">
          <div class="row tight" style="align-items: center">
            <span class="tag" :class="severityMeta(issue.severity).tag">
              {{ severityMeta(issue.severity).label }}
            </span>
            <strong>{{ issue.title }}</strong>
            <span class="tag mono">rule={{ issue.rule }}</span>
            <span class="tag mono">{{ issue.type }}</span>
          </div>

          <div class="blk">
            <span class="blk-head">判定 claim（库里是什么）</span>
            <div
              class="prose snippet"
              :class="{ expanded: isOpen(`issue-claim-${issue.id}`) }"
            >{{ issue.claim }}</div>
            <button class="link-btn" @click="toggle(`issue-claim-${issue.id}`)">
              {{ isOpen(`issue-claim-${issue.id}`) ? "收起" : "展开" }}
            </button>
          </div>

          <div class="blk">
            <span class="blk-head">建议 suggestion（人该怎么处理）</span>
            <div
              class="prose snippet"
              :class="{ expanded: isOpen(`issue-sug-${issue.id}`) }"
            >{{ issue.suggestion }}</div>
            <button class="link-btn" @click="toggle(`issue-sug-${issue.id}`)">
              {{ isOpen(`issue-sug-${issue.id}`) ? "收起" : "展开" }}
            </button>
          </div>

          <div class="blk">
            <span class="blk-head">证据原文（{{ issue.evidence.length }} 条）</span>
            <div v-if="!issue.evidence.length" class="faint">该 issue 未附证据。</div>
            <div v-else class="ev-list">
              <div v-for="(ev, i) in issue.evidence" :key="`is-ev-${issue.id}-${i}`" class="ev">
                <div class="row tight" style="align-items: center">
                  <span class="tag accent">{{ sourceLabel(ev.source) }}</span>
                  <span class="tag mono">source={{ ev.source }}</span>
                  <span class="tag mono">kind={{ ev.kind }}</span>
                  <span class="tag mono">score={{ Number(ev.score).toFixed(3) }}</span>
                </div>
                <div
                  class="prose snippet"
                  :class="{ expanded: isOpen(`is-ev-${issue.id}-${i}`) }"
                >{{ ev.snippet }}</div>
                <button class="link-btn" @click="toggle(`is-ev-${issue.id}-${i}`)">
                  {{ isOpen(`is-ev-${issue.id}-${i}`) ? "收起原文" : "展开原文" }}
                </button>
              </div>
            </div>
          </div>

          <div class="faint mono" style="font-size: var(--fs-xs)">
            issue.id = {{ issue.id }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.issues-out {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
}
.blk {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.blk-head {
  color: var(--fg-faint);
  font-weight: 700;
  font-size: var(--fs-xs);
}
.link-btn {
  font: inherit;
  font-size: var(--fs-sm);
  font-weight: 600;
  border: none;
  background: none;
  color: var(--accent-ink);
  cursor: pointer;
  padding: 2px 6px;
  align-self: flex-start;
}
.link-btn:hover {
  text-decoration: underline;
}
.ev-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.ev {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
}
/* 长文本默认限高可展开：既不让面板被撑爆，也不把内容憋在小框里。 */
.snippet {
  max-height: 7.5em;
  overflow: hidden;
}
.snippet.expanded {
  max-height: none;
}
.rules-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.sev-groups {
  display: flex;
  flex-direction: column;
  gap: var(--gap-lg);
}
.sev-group {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.sev-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.issue {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  border: var(--border-w) solid var(--border);
  border-left: 3px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
}
</style>
