<script setup lang="ts">
/**
 * 章纲写入/编辑独立页（/projects/:id/outline）。
 *
 * 三条界面纪律：
 * 1. **逐章状态可见**：已确认 / 缺失 / 超出末章 / 正文已写但无章纲，各占一个标签；
 * 2. **写入必须人工确认**：单章保存与草稿应用都要勾选确认，AI 起草只出候选；
 * 3. **不许静默**：校验错误带命中词与原文位置，checks / skipped_checks 原样列出。
 */
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError, api } from "@/api/client";
import type {
  ChapterOutlineDraftResponse,
  ChapterOutlineEntry,
  ChapterOutlineIssue,
  ChapterOutlineListResponse,
  ChapterOutlineValidation,
  ChapterOutlineWriteResponse,
  ProjectOut,
} from "@/api/types";

const route = useRoute();
const router = useRouter();
const projectId = computed(() => Number(route.params.id));

const project = ref<ProjectOut | null>(null);
const data = ref<ChapterOutlineListResponse | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);

// 区间输入用字符串：空串 = 不自定上界，交给后端按 max(章纲长度, 已写章数) 决定。
const fromInput = ref("1");
const toInput = ref("");

type Flash = { kind: "ok" | "warn" | "err"; text: string };
const flash = ref<Flash | null>(null);
/** 最近一次保存/起草/写失败的校验回执；有它就一定展示，不折叠。 */
const report = ref<ChapterOutlineValidation | null>(null);

// —— 单章编辑 ——
const editing = ref<{ chapter: number; text: string; confirm: boolean } | null>(null);
const saving = ref(false);

// —— AI 起草（只出候选） ——
const draftFromInput = ref("");
const draftToInput = ref("");
const direction = ref("");
const drafting = ref(false);
const draft = ref<ChapterOutlineDraftResponse | null>(null);
const draftEntries = ref<{ chapter: number; entry: string }[]>([]);
const draftConfirm = ref(false);
const resetTimeline = ref(false);
const applying = ref(false);

const counts = computed(() => {
  const entries = data.value?.entries ?? [];
  let confirmed = 0;
  let missing = 0;
  let writtenMissing = 0;
  let beyond = 0;
  for (const entry of entries) {
    if (entry.status === "confirmed") confirmed += 1;
    else if (entry.status === "beyond_end") beyond += 1;
    else if (entry.written) writtenMissing += 1;
    else missing += 1;
  }
  return { confirmed, missing, writtenMissing, beyond };
});

/** 展示哪份校验回执：写失败/写成功的优先，其次是草稿的预校验。 */
const shownReport = computed<ChapterOutlineValidation | null>(
  () => report.value ?? draft.value?.validation ?? null,
);

function parseChapter(raw: string): number | null {
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) && n >= 1 ? n : null;
}

function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409) return `未写入：${err.message}`;
    if (err.status === 422 && extractValidation(err)) {
      return "校验未通过：整批都没有写入，逐条原因见下方校验回执。";
    }
    if (err.status === 503) return "未配置可用的模型渠道，无法起草。";
    if (
      err.status === 502 &&
      err.detail &&
      typeof err.detail === "object" &&
      (err.detail as { error?: string }).error === "draft_failed"
    ) {
      return "起草失败：模型连续几次返回都不合格式，未写库。稍后可重试或换方向。";
    }
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

function extractValidation(err: unknown): ChapterOutlineValidation | null {
  if (!(err instanceof ApiError)) return null;
  const detail = err.detail;
  if (!detail || typeof detail !== "object") return null;
  const candidate = (detail as { report?: ChapterOutlineValidation }).report;
  if (candidate && Array.isArray(candidate.errors)) return candidate;
  return null;
}

function fmtChapters(list: number[]): string {
  const nums = [...list].sort((a, b) => a - b);
  const parts: string[] = [];
  let start = nums[0];
  let prev = nums[0];
  const flush = () => {
    parts.push(start === prev ? String(start) : `${start}–${prev}`);
  };
  for (const n of nums.slice(1)) {
    if (n === prev + 1) {
      prev = n;
      continue;
    }
    flush();
    start = n;
    prev = n;
  }
  flush();
  return parts.join("、");
}

async function load() {
  loading.value = true;
  loadError.value = null;
  try {
    const from = parseChapter(fromInput.value) ?? 1;
    const trimmed = toInput.value.trim();
    const to = trimmed ? parseChapter(trimmed) : null;
    if (trimmed && to == null) {
      throw new Error("结束章必须是 >= 1 的整数（留空表示到末章）。");
    }
    const [proj, res] = await Promise.all([
      project.value ? Promise.resolve(project.value) : api.getProject(projectId.value),
      api.listChapterOutline(projectId.value, from, to),
    ]);
    project.value = proj;
    data.value = res;
    if (res.entries.length) {
      toInput.value = String(res.entries[res.entries.length - 1].chapter);
      fromInput.value = String(res.entries[0].chapter);
    }
    if (!draftFromInput.value) {
      // 起草默认从第一处缺失开始；没有缺失就从末章之后接。
      const first = res.first_missing_chapter ?? res.outline_len + 1;
      draftFromInput.value = String(first);
      draftToInput.value = String(first + 2);
    }
  } catch (err) {
    loadError.value = describeError(err);
  } finally {
    loading.value = false;
  }
}

function statusLabel(entry: ChapterOutlineEntry): string {
  if (entry.status === "confirmed") return "已确认";
  if (entry.status === "beyond_end") return "超出末章";
  return entry.written ? "正文已写·无章纲" : "缺失";
}

function statusClass(entry: ChapterOutlineEntry): string {
  if (entry.status === "confirmed") return "ok";
  if (entry.status === "beyond_end") return "info";
  return entry.written ? "err" : "warn";
}

function checkClass(status: string): string {
  if (status === "checked") return "ok";
  if (status === "skipped") return "warn";
  return "err";
}

function checkLabel(status: string): string {
  if (status === "checked") return "已检查";
  if (status === "skipped") return "已跳过";
  return "未通过";
}

function issueExtra(item: ChapterOutlineIssue): string {
  const parts: string[] = [];
  if (item.term) parts.push(`命中词「${item.term}」`);
  if (item.position) {
    parts.push(`位置 ${item.position.start}–${item.position.end}：「${item.position.snippet}」`);
  }
  return parts.join("；");
}

function writeSummary(res: ChapterOutlineWriteResponse, what: string): string {
  const parts: string[] = [what];
  parts.push(res.changed.length ? `改动第 ${fmtChapters(res.changed)} 章` : "内容未变");
  if (res.auto_filled_missing.length) {
    parts.push(`自动补空第 ${fmtChapters(res.auto_filled_missing)} 章（已显式回执）`);
  }
  const impact = res.timeline_impact;
  if (impact.reset_timeline) {
    parts.push("时间线已清空，下次生成会重新规划");
  } else {
    if (impact.chapter_ranges_recomputed) parts.push("节点章区间已按新长度重算");
    if (impact.node_content_stale) parts.push("节点内容仍来自旧章纲（node_content_stale，未重建）");
  }
  return `${parts.join("；")}。`;
}

function afterWrite(res: ChapterOutlineWriteResponse, what: string) {
  report.value = res.validation;
  const stale = res.timeline_impact.node_content_stale && !res.timeline_impact.reset_timeline;
  flash.value = { kind: stale ? "warn" : "ok", text: writeSummary(res, what) };
  editing.value = null;
  void load();
}

function startEdit(entry: ChapterOutlineEntry) {
  editing.value = { chapter: entry.chapter, text: entry.entry ?? "", confirm: false };
  report.value = null;
}

async function saveEdit() {
  const target = editing.value;
  if (!target || !data.value) return;
  saving.value = true;
  report.value = null;
  try {
    const res = await api.putChapterOutline(projectId.value, target.chapter, {
      entry: target.text,
      expected_fingerprint: data.value.source_fingerprint,
      confirm: true,
    });
    afterWrite(res, `已保存第 ${target.chapter} 章`);
  } catch (err) {
    report.value = extractValidation(err);
    flash.value = { kind: "err", text: describeError(err) };
  } finally {
    saving.value = false;
  }
}

async function markMissing(entry: ChapterOutlineEntry) {
  if (!data.value) return;
  const ok = window.confirm(
    `把第 ${entry.chapter} 章标记为缺失？\n\n章纲会写成空串（界面显示为缺失），正文不受影响。`,
  );
  if (!ok) return;
  report.value = null;
  try {
    const res = await api.deleteChapterOutline(
      projectId.value,
      entry.chapter,
      data.value.source_fingerprint,
    );
    afterWrite(res, `第 ${entry.chapter} 章已标记缺失`);
  } catch (err) {
    report.value = extractValidation(err);
    flash.value = { kind: "err", text: describeError(err) };
  }
}

async function generateDraft() {
  const from = parseChapter(draftFromInput.value);
  const to = parseChapter(draftToInput.value);
  if (from == null || to == null || to < from) {
    flash.value = { kind: "err", text: "起草区间不合法：结束章必须 >= 起始章。" };
    return;
  }
  drafting.value = true;
  draft.value = null;
  draftEntries.value = [];
  draftConfirm.value = false;
  report.value = null;
  try {
    const res = await api.draftChapterOutline(projectId.value, {
      from_chapter: from,
      to_chapter: to,
      direction: direction.value.trim() || null,
    });
    draft.value = res;
    draftEntries.value = res.entries.map((item) => ({
      chapter: item.chapter,
      entry: item.entry ?? "",
    }));
    flash.value = {
      kind: res.validation.ok ? "ok" : "warn",
      text: `草稿已生成（第 ${res.from_chapter}–${res.to_chapter} 章），尚未写入；核对后再应用。`,
    };
  } catch (err) {
    flash.value = { kind: "err", text: describeError(err) };
  } finally {
    drafting.value = false;
  }
}

async function applyDraft() {
  const current = draft.value;
  if (!current) return;
  if (draftEntries.value.some((item) => !item.entry.trim())) {
    flash.value = { kind: "err", text: "有空白条目：要缺省请在应用后单独标记缺失。" };
    return;
  }
  applying.value = true;
  report.value = null;
  try {
    const res = await api.applyChapterOutline(projectId.value, {
      from_chapter: current.from_chapter,
      to_chapter: current.to_chapter,
      entries: draftEntries.value.map((item) => ({
        chapter: item.chapter,
        entry: item.entry.trim(),
      })),
      expected_fingerprint: current.source_fingerprint,
      confirm: true,
      reset_timeline: resetTimeline.value,
    });
    flash.value = {
      kind: res.timeline_impact.node_content_stale && !resetTimeline.value ? "warn" : "ok",
      text: writeSummary(res, `已应用第 ${current.from_chapter}–${current.to_chapter} 章草稿`),
    };
    report.value = res.validation;
    draft.value = null;
    draftEntries.value = [];
    draftConfirm.value = false;
    resetTimeline.value = false;
    void load();
  } catch (err) {
    report.value = extractValidation(err);
    flash.value = { kind: "err", text: describeError(err) };
  } finally {
    applying.value = false;
  }
}

onMounted(load);
</script>

<template>
  <header class="page-head">
    <div class="eyebrow">02 · 项目</div>
    <h1>
      章纲
      <span v-if="project" class="hint">{{ project.title }} · #{{ project.id }}</span>
    </h1>
    <p>
      逐章可见：已确认、缺失、超出末章、正文已写但无章纲。写入必须勾选确认；校验未通过时整批不落库，
      错误会带命中词与原文位置。
    </p>
    <div class="head-actions">
      <button type="button" class="btn ghost sm" @click="router.push(`/projects/${projectId}`)">
        返回工作台
      </button>
    </div>
  </header>

  <p v-if="flash" class="flash" :class="flash.kind" role="status" aria-live="polite">
    {{ flash.text }}
  </p>

  <div v-if="loading && !data" class="card state-panel" role="status" aria-live="polite">
    <p class="state-description">正在加载章纲…</p>
  </div>

  <div v-else-if="loadError" class="card state-panel" role="alert">
    <h2 class="state-title">章纲加载失败</h2>
    <p class="state-description">{{ loadError }}</p>
    <div class="state-actions">
      <button type="button" class="btn primary" @click="load">重新加载</button>
    </div>
  </div>

  <template v-else-if="data">
    <section class="card">
      <div class="card-title">区间查看</div>
      <div class="range-row">
        <div class="field">
          <label for="outline-from">起始章</label>
          <input id="outline-from" v-model="fromInput" class="input" type="number" min="1" />
        </div>
        <div class="field">
          <label for="outline-to">结束章（留空 = 到末章）</label>
          <input id="outline-to" v-model="toInput" class="input" type="number" min="1" />
        </div>
        <button type="button" class="btn" :disabled="loading" @click="load">
          {{ loading ? "加载中…" : "加载" }}
        </button>
      </div>
      <p class="hint">
        章纲共 {{ data.outline_len }} 章；正文已写到第 {{ data.written_chapters }} 章。
        设定指纹 <span class="mono">{{ data.source_fingerprint.slice(0, 12) }}…</span>
      </p>
    </section>

    <section class="card">
      <div class="card-title">
        区间状态
        <span class="hint">{{ data.entries.length }} 章</span>
      </div>
      <div class="stat-row">
        <span class="tag ok">已确认 {{ counts.confirmed }}</span>
        <span class="tag warn">缺失 {{ counts.missing }}</span>
        <span class="tag err">正文已写·无章纲 {{ counts.writtenMissing }}</span>
        <span class="tag info">超出末章 {{ counts.beyond }}</span>
      </div>
      <p v-if="data.missing_chapters.length" class="note err">
        无章纲的章：第 {{ fmtChapters(data.missing_chapters) }} 章（逐章显式列出，不省略）。
        <template v-if="data.first_missing_chapter">
          第一章缺纲是第 {{ data.first_missing_chapter }} 章。
        </template>
      </p>
      <p v-else class="note ok">区间内每一章都有章纲。</p>

      <div class="checks">
        <div class="sub-title">区间校验</div>
        <ul>
          <li v-for="check in data.checks" :key="`${check.rule}-${check.status}`">
            <span class="tag" :class="checkClass(check.status)">{{ checkLabel(check.status) }}</span>
            <span class="mono">{{ check.rule }}</span>
            <span class="faint">{{ check.detail || check.reason || "—" }}</span>
          </li>
        </ul>
        <p v-if="data.skipped_checks.length" class="note warn">
          被跳过的检查：{{ data.skipped_checks.join("；") }}
        </p>
        <ul v-if="data.warnings.length" class="issue-list">
          <li v-for="(item, index) in data.warnings" :key="`lw-${index}`">
            <span class="tag warn">告警 · {{ item.code }}</span>
            <span>{{ item.message }}</span>
            <span v-if="issueExtra(item)" class="mono faint">{{ issueExtra(item) }}</span>
          </li>
        </ul>
      </div>
    </section>

    <section class="card">
      <div class="card-title">逐章章纲</div>
      <div v-if="!data.entries.length" class="empty">这个区间没有章节。</div>
      <div v-for="entry in data.entries" :key="entry.chapter" class="chapter-row">
        <div class="chapter-head">
          <strong>第 {{ entry.chapter }} 章</strong>
          <span class="tag" :class="statusClass(entry)">{{ statusLabel(entry) }}</span>
          <span v-if="entry.written" class="tag info">正文已写</span>
          <span v-if="entry.node_id" class="faint mono">
            节点 {{ entry.node_id }}
            <template v-if="entry.node_range">
              · 第 {{ entry.node_range[0] }}–{{ entry.node_range[1] }} 章
            </template>
          </span>
          <span class="row-spacer"></span>
          <button type="button" class="btn ghost sm" @click="startEdit(entry)">编辑</button>
          <button
            type="button"
            class="btn ghost sm"
            :disabled="!entry.entry"
            @click="markMissing(entry)"
          >
            标记缺失
          </button>
        </div>
        <p v-if="entry.entry" class="chapter-text">{{ entry.entry }}</p>
        <p v-else class="chapter-text absent">
          {{ statusLabel(entry) }}：第 {{ entry.chapter }} 章没有章纲文本。
        </p>

        <div v-if="editing && editing.chapter === entry.chapter" class="editor">
          <textarea v-model="editing.text" class="input" rows="4"></textarea>
          <p class="hint">
            空文本不能保存；要缺省请用「标记缺失」。保存时会重新校验章号、时间窗与权威物件。
          </p>
          <label class="check-line">
            <input v-model="editing.confirm" type="checkbox" />
            我已核对，确认写入第 {{ entry.chapter }} 章
          </label>
          <div class="actions">
            <button
              type="button"
              class="btn primary sm"
              :disabled="saving || !editing.text.trim() || !editing.confirm"
              @click="saveEdit"
            >
              {{ saving ? "保存中…" : "保存" }}
            </button>
            <button type="button" class="btn ghost sm" @click="editing = null">取消</button>
          </div>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-title">AI 起草（草稿态，必须人工确认）</div>
      <div class="range-row">
        <div class="field">
          <label for="draft-from">起始章</label>
          <input id="draft-from" v-model="draftFromInput" class="input" type="number" min="1" />
        </div>
        <div class="field">
          <label for="draft-to">结束章</label>
          <input id="draft-to" v-model="draftToInput" class="input" type="number" min="1" />
        </div>
        <div class="field grow">
          <label for="draft-direction">方向（可选）</label>
          <input
            id="draft-direction"
            v-model="direction"
            class="input"
            placeholder="例如：收束支线、把冲突推到下一卷"
          />
        </div>
        <button type="button" class="btn" :disabled="drafting" @click="generateDraft">
          {{ drafting ? "起草中…" : "生成草稿" }}
        </button>
      </div>
      <p class="hint">一次最多 20 章。起草只返回候选并预校验，不写库；应用时会重新校验。</p>

      <div v-if="draft" class="draft">
        <p class="note">
          候选：第 {{ draft.from_chapter }}–{{ draft.to_chapter }} 章，共
          {{ draftEntries.length }} 条。可直接改字。
        </p>
        <div v-for="(item, index) in draftEntries" :key="item.chapter" class="draft-item">
          <span class="tag accent">第 {{ item.chapter }} 章</span>
          <textarea v-model="draftEntries[index].entry" class="input" rows="2"></textarea>
        </div>
        <label class="check-line">
          <input v-model="draftConfirm" type="checkbox" />
          我已逐条核对，确认应用这份草稿
        </label>
        <label class="check-line">
          <input v-model="resetTimeline" type="checkbox" />
          同时清空时间线（会丢已完成进度，下次生成重新规划）
        </label>
        <div class="actions">
          <button
            type="button"
            class="btn primary sm"
            :disabled="
              applying || !draftConfirm || draftEntries.some((item) => !item.entry.trim())
            "
            @click="applyDraft"
          >
            {{ applying ? "应用中…" : "应用草稿" }}
          </button>
          <button type="button" class="btn ghost sm" @click="draft = null">丢弃草稿</button>
        </div>
      </div>
    </section>

    <section v-if="shownReport" class="card" :class="{ 'invalid-report': !shownReport.ok }">
      <div class="card-title">校验回执</div>
      <p v-if="shownReport.ok" class="note ok">校验通过。</p>
      <p v-else class="note err">
        校验未通过：{{ shownReport.errors.length }} 条错误，没有任何改动被写入。
      </p>
      <ul v-if="shownReport.errors.length" class="issue-list">
        <li v-for="(item, index) in shownReport.errors" :key="`e-${index}`">
          <span class="tag err">错误 · {{ item.code }}</span>
          <span>{{ item.message }}</span>
          <span v-if="issueExtra(item)" class="mono faint">{{ issueExtra(item) }}</span>
        </li>
      </ul>
      <ul v-if="shownReport.warnings.length" class="issue-list">
        <li v-for="(item, index) in shownReport.warnings" :key="`w-${index}`">
          <span class="tag warn">告警 · {{ item.code }}</span>
          <span>{{ item.message }}</span>
          <span v-if="issueExtra(item)" class="mono faint">{{ issueExtra(item) }}</span>
        </li>
      </ul>
      <div class="checks">
        <div class="sub-title">检查项</div>
        <ul>
          <li v-for="check in shownReport.checks" :key="`${check.rule}-${check.status}`">
            <span class="tag" :class="checkClass(check.status)">{{ checkLabel(check.status) }}</span>
            <span class="mono">{{ check.rule }}</span>
            <span class="faint">{{ check.detail || check.reason || "—" }}</span>
          </li>
        </ul>
        <p v-if="shownReport.skipped_checks.length" class="note warn">
          被跳过的检查：{{ shownReport.skipped_checks.join("；") }}
        </p>
      </div>
    </section>
  </template>
</template>

<style scoped>
.head-actions {
  display: flex;
  gap: var(--gap-sm);
  margin-top: var(--gap-sm);
}
.flash {
  margin-bottom: var(--gap-md);
  padding: 10px 14px;
  border-radius: var(--radius-md);
  border: var(--border-w) solid var(--border);
  background: var(--bg-elev);
  font-size: var(--fs-sm);
  overflow-wrap: anywhere;
}
.flash.ok {
  background: var(--ok-soft);
  color: var(--ok);
  border-color: transparent;
}
.flash.warn {
  background: var(--warn-soft);
  color: var(--warn);
  border-color: transparent;
}
.flash.err {
  background: var(--danger-soft);
  color: var(--danger);
  border-color: transparent;
}
.range-row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--gap-sm);
  margin-bottom: var(--gap-sm);
}
.range-row .field {
  min-width: 140px;
}
.range-row .field.grow {
  flex: 1 1 220px;
}
.stat-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--gap-sm);
  margin-bottom: var(--gap-sm);
}
.sub-title {
  margin: var(--gap-md) 0 var(--gap-sm);
  font-size: var(--fs-sm);
  font-weight: 650;
  color: var(--fg);
}
.chapter-row {
  padding: 12px 0;
  border-top: var(--border-w) solid var(--border);
}
.chapter-row:first-of-type {
  border-top: none;
}
.chapter-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--gap-sm);
}
.row-spacer {
  flex: 1;
}
.chapter-text {
  margin-top: 6px;
  line-height: 1.6;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.chapter-text.absent {
  color: var(--fg-muted);
  font-style: italic;
}
.editor,
.draft-item {
  margin-top: var(--gap-sm);
}
.editor textarea,
.draft-item textarea {
  width: 100%;
  resize: vertical;
  line-height: 1.6;
}
.draft-item {
  display: flex;
  gap: var(--gap-sm);
  align-items: flex-start;
}
.draft-item .tag {
  flex: none;
  margin-top: 8px;
}
.draft-item textarea {
  flex: 1;
}
.check-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  font-size: var(--fs-sm);
  color: var(--fg-muted);
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--gap-sm);
  margin-top: var(--gap-sm);
}
.note {
  margin-top: var(--gap-sm);
  font-size: var(--fs-sm);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.note.ok {
  color: var(--ok);
}
.note.warn {
  color: var(--warn);
}
.note.err {
  color: var(--danger);
}
.issue-list,
.checks ul {
  list-style: none;
  margin: var(--gap-sm) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.issue-list li,
.checks li {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  font-size: var(--fs-sm);
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.invalid-report {
  border-color: var(--danger);
}
</style>
