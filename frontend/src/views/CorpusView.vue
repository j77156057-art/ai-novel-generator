<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "@/api/client";
import type {
  AssistantCleanCorpusResponse,
  ChunkOut,
  NovelOut,
  SearchHit,
  UploadResult,
} from "@/api/types";
import { useAsync } from "@/composables/useAsync";

const { data: novels, loading: novelsLoading, error: novelsError, run: reloadNovels } =
  useAsync(() => api.listNovels());
const uploadState = ref<{ loading: boolean; result: UploadResult | null; error: string | null }>({
  loading: false,
  result: null,
  error: null,
});
const fileInput = ref<HTMLInputElement | null>(null);
/** 混书剔除开关：默认关（只检测不删），与后端 `drop_foreign_blocks=Form(False)` 一致。 */
const dropForeignBlocks = ref(false);

const selected = ref<NovelOut | null>(null);
const chunks = ref<ChunkOut[]>([]);
const chunksLoading = ref(false);
const chunksError = ref<string | null>(null);

const q = ref("");
const hits = ref<SearchHit[] | null>(null);
const searching = ref(false);
const searchError = ref<string | null>(null);

// ---------- 语料清洗（档 3：前端显式入口） ----------
//
// 后端 POST /api/assistant/clean-corpus 的硬约束：规则先行、模型后置，
// **不写 novel_chunks**，只产 pending 提案。所以这里的「二次确认」不是
// 「要不要删」，而是「你把范围说清楚，我再发起一次会落提案、会调模型的扫描」。
//
// 数字来源（与后端常量对齐，不另立口径）：
//   CLEAN_LIMIT_MAX     = MAX_CLEAN_CHUNK_LIMIT（app/routers/assistant_clean.py）
//   CLEAN_PROPOSAL_CAP  = MAX_CLEAN_PROPOSALS_PER_CALL（同上）
const CLEAN_LIMIT_DEFAULT = 20;
const CLEAN_LIMIT_MAX = 200;
const CLEAN_PROPOSAL_CAP = 50;

const cleanNovelId = ref<number | null>(null);
const cleanLimit = ref<number>(CLEAN_LIMIT_DEFAULT);
const cleanDryRun = ref(false);
/** 确认模态：只有当它为 true 时 submitClean 才允许发请求（不可绕过）。 */
const cleanConfirmOpen = ref(false);
const cleanBusy = ref(false);
const cleanError = ref<string | null>(null);
const cleanResult = ref<AssistantCleanCorpusResponse | null>(null);

const cleanNovel = computed(
  () => novels.value?.find((n) => n.id === cleanNovelId.value) ?? null,
);
const cleanLimitSafe = computed(() => {
  const n = Number(cleanLimit.value);
  if (!Number.isFinite(n)) return CLEAN_LIMIT_DEFAULT;
  return Math.min(CLEAN_LIMIT_MAX, Math.max(1, Math.round(n)));
});

onMounted(() => reloadNovels());

function chooseFile() {
  if (!uploadState.value.loading) fileInput.value?.click();
}

async function onFile(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file || uploadState.value.loading) return;
  uploadState.value = { loading: true, result: null, error: null };
  try {
    const r = await api.uploadNovel(file, dropForeignBlocks.value);
    uploadState.value = { loading: false, result: r, error: null };
    await reloadNovels();
  } catch (err) {
    uploadState.value = { loading: false, result: null, error: err instanceof Error ? err.message : String(err) };
  } finally {
    input.value = "";
  }
}

/** 入库状态的人话说法；原始值并列显示，便于与后端 status 对账。 */
function uploadStatusLabel(status: string | null | undefined): string {
  if (status === "imported_review") return "待人工复核 imported_review";
  if (status === "imported") return "已导入 imported";
  return status || "—";
}

async function loadChunks(n: NovelOut) {
  chunksLoading.value = true;
  chunksError.value = null;
  try {
    chunks.value = await api.listChunks(n.id);
  } catch (err) {
    chunks.value = [];
    chunksError.value = err instanceof Error ? err.message : String(err);
  } finally {
    chunksLoading.value = false;
  }
}

async function openChunks(n: NovelOut) {
  selected.value = n;
  cleanNovelId.value = n.id;
  hits.value = null;
  searchError.value = null;
  q.value = "";
  chunks.value = [];
  await loadChunks(n);
}

async function retryChunks() {
  if (selected.value) await loadChunks(selected.value);
}

async function runSearch() {
  const novel = selected.value;
  const query = q.value.trim();
  if (!novel || !query || searching.value) return;
  searching.value = true;
  searchError.value = null;
  hits.value = null;
  try {
    hits.value = await api.searchNovel(novel.id, query);
  } catch (err) {
    searchError.value = err instanceof Error ? err.message : String(err);
  } finally {
    searching.value = false;
  }
}

/** 打开二次确认。真正的请求只在 submitClean 里发，这里不发任何请求。 */
function openCleanConfirm() {
  if (!cleanNovelId.value || cleanBusy.value) return;
  cleanError.value = null;
  cleanConfirmOpen.value = true;
}

function closeCleanConfirm() {
  if (cleanBusy.value) return;
  cleanConfirmOpen.value = false;
}

async function submitClean() {
  // 防绕过：没经过确认模态、或没选语料，一律不发请求。
  if (!cleanConfirmOpen.value || !cleanNovelId.value || cleanBusy.value) return;
  cleanBusy.value = true;
  cleanError.value = null;
  try {
    const r = await api.cleanCorpus({
      novel_id: cleanNovelId.value,
      limit: cleanLimitSafe.value,
      dry_run: cleanDryRun.value,
    });
    cleanResult.value = r;
    cleanConfirmOpen.value = false;
  } catch (err) {
    cleanError.value = err instanceof Error ? err.message : String(err);
  } finally {
    cleanBusy.value = false;
  }
}

/** 结果里的判据计数按次数降序，便于一眼看出哪条命中最多。 */
const cleanRuleCounts = computed(() => {
  const src = cleanResult.value?.tool_results.findings_by_rule ?? {};
  return Object.entries(src).sort((a, b) => b[1] - a[1]);
});
</script>

<template>
  <header class="page-head">
    <div class="eyebrow">01 · Corpus</div>
    <h1>语料库</h1>
    <p>上传 UTF-8 小说 .txt → 自动分章切分入库。选中一本可查看分片，并用 M2 混合检索（BM25 × 向量）召回相关片段。</p>
  </header>

  <section class="card">
    <div class="card-title">上传语料 <span class="hint">.txt · UTF-8</span></div>
    <div class="row upload-row">
      <input
        ref="fileInput"
        type="file"
        accept=".txt,text/plain"
        hidden
        :disabled="uploadState.loading"
        @change="onFile"
      />
      <label class="upload-drop">
        <input type="checkbox" v-model="dropForeignBlocks" :disabled="uploadState.loading" />
        剔除检出的异书块（drop_foreign_blocks）
      </label>
      <button
        type="button"
        class="btn primary"
        :disabled="uploadState.loading"
        @click="chooseFile"
      >
        {{ uploadState.loading ? "正在上传…" : "选择 .txt 上传" }}
      </button>
      <span v-if="uploadState.loading" class="muted" role="status">
        <span class="spinner" /> 正在上传并解析…
      </span>
    </div>
    <p class="faint upload-hint">
      默认只检测、不删除；不勾选时若检出混入的其他书块，语料仍入库并标记为
      <span class="mono">imported_review</span> 待人工复核。
    </p>
    <div v-if="uploadState.result" class="notice ok upload-notice" role="status">
      已入库：{{ uploadState.result.title }} · {{ uploadState.result.chapters }} 章 / {{ uploadState.result.chunks }} 分片（novel_id={{ uploadState.result.novel_id }}）
      <span
        class="tag"
        :class="uploadState.result.status === 'imported_review' ? 'warn' : 'ok'"
      >
        {{ uploadStatusLabel(uploadState.result.status) }}
      </span>
    </div>
    <div
      v-if="uploadState.result?.status === 'imported_review'"
      class="notice warn upload-notice"
      role="alert"
    >
      检出疑似混入的其他书块，本次未剔除（开关未勾选），已按待复核状态入库。
      如需剔除，请重新上传并勾选「剔除检出的异书块」。
    </div>
    <div v-if="uploadState.error" class="notice err upload-notice" role="alert">
      {{ uploadState.error }}
    </div>
  </section>

  <section class="card">
    <div class="card-title">已入库语料 <span class="hint">{{ novels?.length ?? 0 }} 本</span></div>
    <div
      v-if="novelsLoading"
      class="skeleton-stack novel-skeleton"
      role="status"
      aria-label="正在加载语料列表"
      aria-busy="true"
    >
      <div class="skeleton-line" aria-hidden="true"></div>
      <div class="skeleton-line" aria-hidden="true"></div>
      <div class="skeleton-line" aria-hidden="true"></div>
    </div>
    <div v-else-if="novelsError" class="notice err error-with-action" role="alert">
      <span class="error-message">{{ novelsError }}</span>
      <button type="button" class="btn" @click="reloadNovels">重新加载</button>
    </div>
    <div v-else-if="!novels?.length" class="empty">还没有语料，先上传一本。</div>
    <div v-else class="list">
      <button
        v-for="n in novels"
        :key="n.id"
        type="button"
        class="list-item clickable novel-item"
        :class="{ active: selected?.id === n.id }"
        @click="openChunks(n)"
      >
        <div class="novel-copy">
          <strong class="novel-title" :title="n.title || '（无题）'">{{ n.title || "（无题）" }}</strong>
          <span class="novel-id faint mono">#{{ n.id }}</span>
        </div>
        <div class="row tight novel-meta">
          <span class="tag">{{ n.platform || "—" }}</span>
          <span v-if="n.status === 'imported_review'" class="tag warn">待复核 imported_review</span>
          <span class="faint novel-action">查看分片</span>
        </div>
      </button>
    </div>
  </section>

  <!-- ===== 语料清洗（档 3：前端显式入口，助手不代执行） ===== -->
  <section class="card clean-card">
    <div class="card-title">
      语料清洗 <span class="hint">规则先行 · 模型复核</span>
    </div>
    <p class="hint clean-intro">
      八条确定性判据先扫一遍；只有命中「可模型复核」判据的分片才送模型。
      <strong>这一步不会直接改语料正文</strong>：命中的修正会落成待批准提案，
      要生效需再到项目页「待应用提案」批准并应用。
    </p>

    <div class="row clean-form">
      <div class="field clean-novel-field">
        <label for="clean-novel">清洗哪本语料</label>
        <select id="clean-novel" class="input" v-model.number="cleanNovelId" :disabled="cleanBusy">
          <option :value="null" disabled>请选择语料</option>
          <option v-for="n in novels" :key="n.id" :value="n.id">
            {{ n.title || "（无题）" }} · #{{ n.id }}
          </option>
        </select>
      </div>
      <div class="field clean-limit-field">
        <label for="clean-limit">单次扫描分片数</label>
        <input
          id="clean-limit"
          class="input"
          type="number"
          :min="1"
          :max="CLEAN_LIMIT_MAX"
          step="1"
          v-model.number="cleanLimit"
          :disabled="cleanBusy"
        />
        <span class="faint clean-limit-hint">1–{{ CLEAN_LIMIT_MAX }}，默认 {{ CLEAN_LIMIT_DEFAULT }}</span>
      </div>
      <label class="clean-dry">
        <input type="checkbox" v-model="cleanDryRun" :disabled="cleanBusy" />
        只检测、不创建提案（dry_run）
      </label>
      <button
        type="button"
        class="btn primary"
        :disabled="!cleanNovelId || cleanBusy"
        @click="openCleanConfirm"
      >
        扫描并复核
      </button>
    </div>
    <div v-if="!novels?.length" class="empty">先上传一本语料，再清洗。</div>

    <div v-if="cleanError" class="notice err clean-result" role="alert">{{ cleanError }}</div>

    <!-- 执行后回执：每一项都能与后端响应逐一核对 -->
    <div v-if="cleanResult" class="clean-result">
      <div class="clean-result-head">
        <strong>{{ cleanResult.dry_run ? "检测完成（未创建提案）" : "扫描并复核完成" }}</strong>
        <span class="tag mono">扫描 {{ cleanResult.scope.chunks_scanned }} 片</span>
        <span class="tag" :class="cleanResult.proposal_count ? 'accent' : ''">
          提案 {{ cleanResult.proposal_count }} 条
        </span>
        <span class="tag" :class="cleanResult.model_participated ? 'ok' : 'warn'">
          {{ cleanResult.model_participated ? "模型已参与复核" : "模型未参与" }}
        </span>
        <span class="tag info mono">run #{{ cleanResult.agent_run_id }}</span>
      </div>

      <div class="notice" :class="cleanResult.dry_run ? 'warn' : 'ok'">
        {{ cleanResult.answer }}
      </div>

      <div class="clean-facts">
        <span class="faint">范围</span>
        <span class="mono">
          novel_id={{ cleanResult.scope.novel_id ?? "—" }} · limit={{ cleanResult.scope.limit }} ·
          chunks_scanned={{ cleanResult.scope.chunks_scanned }}
        </span>
        <span class="faint">模型</span>
        <span class="mono">{{ cleanResult.model_note || "（无备注）" }}</span>
      </div>

      <div v-if="cleanRuleCounts.length" class="clean-block">
        <div class="faint clean-label">确定性判据命中（chunks_scanned 内）</div>
        <div class="row tight clean-tags">
          <span v-for="[rule, n] in cleanRuleCounts" :key="rule" class="tag mono">
            {{ rule }} × {{ n }}
          </span>
        </div>
      </div>

      <div v-if="cleanResult.proposals.length" class="clean-block">
        <div class="faint clean-label">
          本次落成的 pending 提案（要生效需批准 → 应用）
        </div>
        <div class="row tight clean-tags">
          <span v-for="p in cleanResult.proposals" :key="p.id" class="tag accent mono">
            #{{ p.id }} · chunk {{ p.target_id }}
          </span>
        </div>
      </div>

      <div v-if="cleanResult.suggested_edits.length" class="clean-block">
        <div class="faint clean-label">
          建议修正 {{ cleanResult.suggested_edits.length }} 处
        </div>
        <div class="list clean-edits">
          <div v-for="e in cleanResult.suggested_edits" :key="e.chunk_id + ':' + e.before" class="list-item edit-item">
            <div class="row tight edit-meta">
              <span class="tag mono">chunk {{ e.chunk_id }}</span>
              <span class="tag info">{{ e.source === "model" ? "模型复核" : "确定性修正" }}</span>
              <span class="tag mono">conf {{ e.confidence.toFixed(2) }}</span>
              <span class="tag" :class="e.proposed ? 'ok' : 'warn'">
                {{ e.proposed ? "已落提案" : e.skip_reason ? "跳过：" + e.skip_reason : "仅建议" }}
              </span>
            </div>
            <div class="edit-diff mono">
              <span class="edit-before">{{ e.before }}</span>
              <span class="faint"> → </span>
              <span class="edit-after">{{ e.after }}</span>
            </div>
            <div class="faint edit-reason">{{ e.reason }}</div>
          </div>
        </div>
      </div>

      <div v-if="cleanResult.model_rejected.length" class="notice warn">
        模型输出被护栏丢弃 {{ cleanResult.model_rejected.length }} 条：
        <span v-for="r in cleanResult.model_rejected" :key="r.chunk_id" class="mono clean-reject">
          chunk {{ r.chunk_id }}（{{ r.reason }}）
        </span>
      </div>
    </div>
  </section>

  <template v-if="selected">
    <section class="card">
      <div class="card-title">
        <span class="selection-title">分片 · 《{{ selected.title || "（无题）" }}》</span>
        <span class="hint">
          {{ chunksLoading ? "加载中" : chunksError ? "加载失败" : chunks.length + " 片" }}
        </span>
      </div>
      <div class="row search-row">
        <div class="field search-field">
          <label for="corpus-search">混合检索（M2）</label>
          <input
            id="corpus-search"
            v-model="q"
            class="input"
            placeholder="输入关键词或语义描述"
            @keyup.enter="runSearch"
          />
        </div>
        <button type="button" class="btn primary" :disabled="searching || !q.trim()" @click="runSearch">
          {{ searching ? "检索中…" : "检索" }}
        </button>
      </div>

      <div v-if="searching" class="empty search-state" role="status" aria-live="polite">
        <span class="spinner" /> 正在检索…
      </div>
      <div v-else-if="searchError" class="notice err error-with-action search-state" role="alert">
        <span class="error-message">{{ searchError }}</span>
        <button type="button" class="btn" @click="runSearch">重试检索</button>
      </div>
      <div v-else-if="hits" class="search-results">
        <div class="card-title section-heading">
          检索命中 <span class="hint">RRF 融合 · {{ hits.length }} 条</span>
        </div>
        <div v-if="!hits.length" class="empty">未检索到相关片段，请调整关键词后重试。</div>
        <div v-else class="list">
          <div v-for="h in hits" :key="h.chunk_id" class="list-item hit-item">
            <div class="hit-content">
              <div class="row tight hit-meta">
                <span class="tag info">第 {{ h.chapter_no ?? "?" }} 章</span>
                <span class="tag">idx {{ h.idx }}</span>
                <span class="tag accent mono">score {{ h.score.toFixed(4) }}</span>
                <span class="faint mono hit-score">bm25 {{ h.bm25.toFixed(3) }} · vec {{ h.vector.toFixed(3) }}</span>
              </div>
              <div class="prose hit-text">{{ h.text }}</div>
            </div>
          </div>
        </div>
      </div>

      <div
        v-if="chunksLoading"
        class="skeleton-stack chunk-skeleton"
        role="status"
        aria-label="正在加载分片"
        aria-busy="true"
      >
        <div class="skeleton-line" aria-hidden="true"></div>
        <div class="skeleton-line" aria-hidden="true"></div>
        <div class="skeleton-line" aria-hidden="true"></div>
      </div>
      <div v-else-if="chunksError" class="notice err error-with-action" role="alert">
        <span class="error-message">{{ chunksError }}</span>
        <button type="button" class="btn" @click="retryChunks">重新加载分片</button>
      </div>
      <div v-else-if="!chunks.length" class="empty">该语料暂无分片。</div>
      <div v-else class="list">
        <div v-for="c in chunks" :key="c.id" class="list-item chunk-item">
          <div class="row tight chunk-meta">
            <span class="tag">第 {{ c.chapter_no ?? "?" }} 章</span>
            <span class="faint mono chunk-index">#{{ c.idx }}</span>
          </div>
          <div class="prose chunk-text">{{ c.text }}</div>
        </div>
      </div>
    </section>
  </template>

  <!-- ===== 二次确认（唯一的请求发起点；关掉它就没有别的路径能触发清洗） ===== -->
  <div v-if="cleanConfirmOpen" class="clean-modal-mask" @click.self="closeCleanConfirm">
    <div class="clean-modal" role="dialog" aria-modal="true" aria-labelledby="clean-confirm-title">
      <div id="clean-confirm-title" class="card-title">确认扫描并复核语料</div>
      <p class="clean-confirm-lead">
        即将对《{{ cleanNovel?.title || "（无题）" }}》
        <span class="mono">(novel_id={{ cleanNovelId }})</span> 发起清洗。
      </p>

      <ul class="clean-scope">
        <li>
          <b>扫描范围</b>：最多 {{ cleanLimitSafe }} 个分片（按 idx 升序），
          本次只读这些分片。
        </li>
        <li>
          <b>数据影响</b>：<span class="err-text">不写 novel_chunks，不改任何正文</span>；
          最多创建 {{ Math.min(cleanLimitSafe, CLEAN_PROPOSAL_CAP) }} 条
          <span class="mono">pending</span> 提案（每个命中分片最多 1 条）。
          提案要生效，必须再到项目页「待应用提案」批准后应用。
        </li>
        <li>
          <b>模型调用</b>：八条确定性判据先跑；只有命中可复核判据的分片才送模型。
          模型不可用会降级为纯规则结果，并在回执里标注「模型未参与」。
        </li>
        <li>
          <b>留痕</b>：本次会写一条 AgentRun 审计记录（回执里的 run id）。
        </li>
        <li v-if="cleanDryRun" class="dry-line">
          已勾选 <b>dry_run</b>：本次只检测与复核，<b>不会创建任何提案</b>
          （proposal_count 将为 0）。
        </li>
      </ul>

      <div v-if="cleanError" class="notice err" style="margin-top: 12px">{{ cleanError }}</div>

      <div class="row modal-actions">
        <button type="button" class="btn ghost" :disabled="cleanBusy" @click="closeCleanConfirm">
          取消
        </button>
        <button type="button" class="btn primary" :disabled="cleanBusy" @click="submitClean">
          {{ cleanBusy ? "扫描中…" : `确认扫描 ${cleanLimitSafe} 片` }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.upload-row {
  align-items: center;
}
.upload-drop {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-sm);
}
.upload-hint {
  margin-top: 8px;
  font-size: var(--fs-xs);
}
.upload-notice {
  margin-top: 16px;
}
.notice {
  overflow-wrap: anywhere;
}
.error-with-action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--gap-md);
}
.error-message {
  flex: 1 1 240px;
  min-width: 0;
  overflow-wrap: anywhere;
}
.novel-skeleton .skeleton-line {
  width: 100%;
  height: 44px;
}
.novel-item {
  appearance: none;
  width: 100%;
  min-width: 0;
  flex-wrap: wrap;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
}
.novel-item:last-child {
  border-bottom: 0;
}
.novel-item:hover {
  background: var(--bg-sunken);
}
.novel-item.active {
  background: var(--accent-soft);
}
.novel-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.novel-copy {
  flex: 1;
  min-width: 0;
}
.novel-title {
  overflow-wrap: anywhere;
}
.novel-id {
  margin-left: 10px;
  white-space: nowrap;
}
.novel-meta {
  flex: none;
  max-width: 100%;
}
.novel-meta .tag {
  max-width: 100%;
  overflow-wrap: anywhere;
}
.novel-action {
  font-size: var(--fs-xs);
  white-space: nowrap;
}
.selection-title {
  min-width: 0;
  overflow-wrap: anywhere;
}
.card-title .hint {
  flex: none;
}
.search-row {
  margin-bottom: 16px;
}
.search-field {
  flex: 1;
  min-width: min(260px, 100%);
}
.search-state,
.search-results {
  margin-bottom: 16px;
}
.section-heading {
  font-size: var(--fs-md);
}
.hit-item {
  align-items: flex-start;
}
.hit-content {
  flex: 1;
  width: 100%;
  min-width: 0;
}
.hit-meta {
  align-items: center;
  min-width: 0;
  margin-bottom: 6px;
}
.hit-score {
  min-width: 0;
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.hit-text,
.chunk-text {
  min-width: 0;
  max-width: 100%;
  overflow: auto;
  overflow-wrap: anywhere;
  word-break: break-word;
  font-size: var(--fs-sm);
}
.hit-text {
  max-height: 120px;
}
.chunk-skeleton .skeleton-line {
  width: 100%;
  height: 64px;
}
.chunk-item {
  align-items: flex-start;
  flex-wrap: wrap;
  min-width: 0;
}
.chunk-meta {
  flex: 0 0 120px;
  align-items: center;
  max-width: 100%;
}
.chunk-index {
  font-size: var(--fs-xs);
}
.chunk-text {
  flex: 1 1 280px;
  max-height: 90px;
}

/* ---- 语料清洗 ---- */
.clean-intro {
  margin-bottom: 12px;
  line-height: 1.7;
}
.clean-form {
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--gap-md);
}
.clean-novel-field {
  flex: 1 1 240px;
  min-width: min(220px, 100%);
}
.clean-limit-field {
  flex: 0 0 150px;
}
.clean-limit-hint {
  font-size: var(--fs-xs);
}
.clean-dry {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-sm);
  white-space: nowrap;
}
.clean-result {
  margin-top: 16px;
}
.clean-result-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.clean-facts {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 12px;
  margin: 10px 0;
  font-size: var(--fs-xs);
}
.clean-block {
  margin-top: 12px;
}
.clean-label {
  font-size: var(--fs-xs);
  margin-bottom: 6px;
}
.clean-tags {
  flex-wrap: wrap;
  gap: 6px;
}
.clean-edits {
  margin-top: 6px;
}
.edit-item {
  align-items: flex-start;
  flex-direction: column;
  gap: 4px;
}
.edit-meta {
  flex-wrap: wrap;
  gap: 6px;
}
.edit-diff {
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.edit-before {
  text-decoration: line-through;
  color: var(--err, #b3261e);
}
.edit-after {
  color: var(--ok, #1e7d32);
}
.edit-reason {
  font-size: var(--fs-xs);
}
.clean-reject {
  margin-left: 8px;
}

/* ---- 二次确认模态 ---- */
.clean-modal-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(20, 20, 24, 0.55);
}
.clean-modal {
  width: 100%;
  max-width: 560px;
  max-height: 86vh;
  overflow: auto;
  padding: 20px;
  border: 1px solid var(--border);
  border-radius: var(--radius, 10px);
  background: var(--bg, #fff);
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
}
.clean-confirm-lead {
  margin: 8px 0 12px;
}
.clean-scope {
  margin: 0;
  padding-left: 20px;
  line-height: 1.75;
  font-size: var(--fs-sm);
}
.clean-scope li + li {
  margin-top: 6px;
}
.clean-scope .err-text {
  color: var(--err, #b3261e);
  font-weight: 700;
}
.clean-scope .dry-line {
  color: var(--warn, #a15c00);
}
.modal-actions {
  margin-top: var(--gap);
  justify-content: flex-end;
}
</style>
