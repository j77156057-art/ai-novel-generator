<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "@/api/client";
import type { ChunkOut, NovelOut, SearchHit, UploadResult } from "@/api/types";
import { useAsync } from "@/composables/useAsync";

const { data: novels, loading: novelsLoading, error: novelsError, run: reloadNovels } =
  useAsync(() => api.listNovels());
const uploadState = ref<{ loading: boolean; result: UploadResult | null; error: string | null }>({
  loading: false,
  result: null,
  error: null,
});
const fileInput = ref<HTMLInputElement | null>(null);

const selected = ref<NovelOut | null>(null);
const chunks = ref<ChunkOut[]>([]);
const chunksLoading = ref(false);
const chunksError = ref<string | null>(null);

const q = ref("");
const hits = ref<SearchHit[] | null>(null);
const searching = ref(false);
const searchError = ref<string | null>(null);

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
    const r = await api.uploadNovel(file);
    uploadState.value = { loading: false, result: r, error: null };
    await reloadNovels();
  } catch (err) {
    uploadState.value = { loading: false, result: null, error: err instanceof Error ? err.message : String(err) };
  } finally {
    input.value = "";
  }
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
    <div v-if="uploadState.result" class="notice ok upload-notice" role="status">
      已入库：{{ uploadState.result.title }} · {{ uploadState.result.chapters }} 章 / {{ uploadState.result.chunks }} 分片（novel_id={{ uploadState.result.novel_id }}）
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
          <span class="faint novel-action">查看分片</span>
        </div>
      </button>
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
</template>

<style scoped>
.upload-row {
  align-items: center;
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
</style>
