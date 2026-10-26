<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "@/api/client";
import type { ChunkOut, NovelOut, SearchHit } from "@/api/types";
import { useAsync } from "@/composables/useAsync";

const router = useRouter();
const { data: novels, loading: novelsLoading, error: novelsError, run: reloadNovels } =
  useAsync(() => api.listNovels());

const selected = ref<NovelOut | null>(null);
const chunks = ref<ChunkOut[]>([]);
const chunksLoading = ref(false);

const q = ref("");
const hits = ref<SearchHit[] | null>(null);
const searching = ref(false);

onMounted(() => reloadNovels());

/** 去助理页：文本提交已经统一到那里（agent 会带它走清洗 → 抽检 → 蒸馏 → 审查）。 */
function goAssistant(): void {
  void router.push("/assistant");
}

async function openChunks(n: NovelOut) {
  selected.value = n;
  hits.value = null;
  q.value = "";
  chunksLoading.value = true;
  try {
    chunks.value = await api.listChunks(n.id);
  } catch (err) {
    chunks.value = [];
    alert(err instanceof Error ? err.message : String(err));
  } finally {
    chunksLoading.value = false;
  }
}

async function runSearch() {
  if (!selected.value || !q.value.trim()) return;
  searching.value = true;
  try {
    hits.value = await api.searchNovel(selected.value.id, q.value.trim());
  } catch (err) {
    alert(err instanceof Error ? err.message : String(err));
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
    <div class="card-title">投放语料 <span class="hint">.txt · UTF-8</span></div>
    <div class="notice info" style="margin-top: 4px">
      文本提交已统一到助理 agent。直接上传会绕过整条流程（清洗、抽检、蒸馏、审查）——
      实测那类语料里有大量空/超短分片、切分停在半句话中间，文风也就不可信了。
      请到助理页上传附件，agent 会带它走：清洗 → 抽检 → 蒸馏 → 审查。
    </div>
    <div class="row" style="margin-top: 12px">
      <button class="btn primary" @click="goAssistant">去助理页投放</button>
    </div>
  </section>

  <section class="card">
    <div class="card-title">已入库语料 <span class="hint">{{ novels?.length ?? 0 }} 本</span></div>
    <div v-if="novelsLoading" class="empty"><span class="spinner" /> 加载中…</div>
    <div v-else-if="novelsError" class="notice err">{{ novelsError }}</div>
    <div v-else-if="!novels?.length" class="empty">还没有语料，先上传一本。</div>
    <div v-else class="list">
      <div
        v-for="n in novels"
        :key="n.id"
        class="list-item clickable"
        :class="{ active: selected?.id === n.id }"
        @click="openChunks(n)"
      >
        <div>
          <strong>{{ n.title || "（无题）" }}</strong>
          <span class="faint mono" style="margin-left: 10px">#{{ n.id }}</span>
        </div>
        <div class="row tight">
          <span class="tag">{{ n.platform || "—" }}</span>
          <span class="faint" style="font-size: var(--fs-xs)">查看分片 →</span>
        </div>
      </div>
    </div>
  </section>

  <template v-if="selected">
    <section class="card">
      <div class="card-title">
        分片 · 《{{ selected.title }}》
        <span class="hint">{{ chunksLoading ? "加载中" : chunks.length + " 片" }}</span>
      </div>
      <div class="row" style="margin-bottom: 16px">
        <div class="field" style="flex: 1">
          <label>混合检索（M2）</label>
          <input class="input" v-model="q" placeholder="输入关键词或语义，如：林夜 那道光" @keyup.enter="runSearch" />
        </div>
        <button class="btn primary" :disabled="searching || !q.trim()" @click="runSearch">
          {{ searching ? "检索中…" : "检索" }}
        </button>
      </div>

      <div v-if="hits" style="margin-bottom: 16px">
        <div class="card-title" style="font-size: var(--fs-md)">检索命中 <span class="hint">RRF 融合 Top {{ hits.length }}</span></div>
        <div class="list">
          <div v-for="h in hits" :key="h.chunk_id" class="list-item" style="align-items: flex-start">
            <div style="flex: 1">
              <div class="row tight" style="margin-bottom: 6px">
                <span class="tag info">第 {{ h.chapter_no ?? "?" }} 章</span>
                <span class="tag">idx {{ h.idx }}</span>
                <span class="tag accent mono">score {{ h.score.toFixed(4) }}</span>
                <span class="faint mono" style="font-size: var(--fs-xs)">bm25 {{ h.bm25.toFixed(3) }} · vec {{ h.vector.toFixed(3) }}</span>
              </div>
              <div class="prose" style="font-size: var(--fs-sm); max-height: 120px; overflow: auto">{{ h.text }}</div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="chunksLoading" class="empty"><span class="spinner" /> 载入分片…</div>
      <div v-else class="list">
        <div v-for="c in chunks" :key="c.id" class="list-item" style="align-items: flex-start">
          <div class="row tight" style="width: 120px; flex: none">
            <span class="tag">第 {{ c.chapter_no ?? "?" }} 章</span>
            <span class="faint mono" style="font-size: var(--fs-xs)">#{{ c.idx }}</span>
          </div>
          <div class="prose" style="font-size: var(--fs-sm); flex: 1; max-height: 90px; overflow: auto">{{ c.text }}</div>
        </div>
      </div>
    </section>
  </template>
</template>
