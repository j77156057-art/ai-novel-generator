<script setup lang="ts">
import { onMounted, ref, computed } from "vue";
import { useRouter } from "vue-router";
import { api } from "@/api/client";
import type {
  ChunkOut,
  CorpusCleanReport,
  DigestOut,
  DistillJob,
  ForeignBlocksReport,
  NovelOut,
  ProjectOut,
  SearchHit,
} from "@/api/types";
import { useAsync } from "@/composables/useAsync";
import ForeignBlocksNotice from "@/components/ForeignBlocksNotice.vue";
import DistillProgress from "@/components/DistillProgress.vue";

const router = useRouter();
const { data: novels, loading: novelsLoading, error: novelsError, run: reloadNovels } =
  useAsync(() => api.listNovels());

const selected = ref<NovelOut | null>(null);
const chunks = ref<ChunkOut[]>([]);
const chunksLoading = ref(false);
const chunksError = ref<string | null>(null);

const q = ref("");
const hits = ref<SearchHit[] | null>(null);
const searching = ref(false);
const searchError = ref<string | null>(null);

onMounted(() => reloadNovels());

/** 这本语料入库时的体检报告。 */
function reportOf(n: NovelOut | null): CorpusCleanReport | null {
  return n?.clean_report ?? null;
}

/** 「混进了别的书」的检出结论；没检出 → null（整块不渲染，不留空态）。 */
function foreignOf(n: NovelOut | null): ForeignBlocksReport | null {
  const report = reportOf(n);
  const foreign = report?.foreign_blocks;
  if (!foreign || !foreign.detected) return null;
  return foreign;
}

/** 去助理页：文本提交已经统一到那里（agent 会带它走清洗 → 抽检 → 蒸馏 → 审查）。 */
function goAssistant(): void {
  void router.push("/assistant");
}

// ── 全书摘要（BookDigest） ──
//
// 摘要**按书存、跨项目复用**，但**提交挂在项目下**（后台任务表的项目列必填），
// 所以这里要选一个「用哪个项目发起」——产物的归属与项目无关，任务归属有关。
const digest = ref<DigestOut | null>(null);
const digestLoading = ref(false);
const digestErr = ref<string | null>(null);
/** 摘要任务的实时状态（与文风蒸馏同一张任务表，同一套轮询协议）。 */
const digestJob = ref<DistillJob | null>(null);
const digestSubmitting = ref(false);
const digestNotice = ref<string | null>(null);
/** 抽取档位：auto（按书大小与渠道能力自动选）/ whole（整本一次投喂）/ retrieval（切块抽）。 */
const digestMode = ref<"auto" | "whole" | "retrieval">("auto");
/** 压缩比目标（输出 ≤ 输入 / 该值）。 */
const digestRatio = ref(10);
const projects = ref<ProjectOut[]>([]);
const digestProjectId = ref<number | null>(null);
let digestTimer: ReturnType<typeof setInterval> | null = null;

const digestActive = computed(
  () => !!digestJob.value && (digestJob.value.status === "pending" || digestJob.value.status === "running"),
);

/** 摘要的「没有 / 有 / 坏了」三态。空对象会被当成「有摘要但没内容」，那两件事要走的路完全不同。 */
const digestState = computed<"none" | "ready" | "broken">(() => {
  if (!digest.value) return "none";
  return digest.value.text ? "ready" : "broken";
});

/** 四项验收的展示行（顺序按重要性：先看有没有编，再看覆盖，最后看压得狠不狠）。 */
const digestChecks = computed(() => {
  const d = digest.value;
  if (!d) return [];
  const ev = d.evidence_detail;
  return [
    {
      key: "evidence",
      label: "证据留存",
      ok: d.checks.evidence?.ok ?? null,
      value: ev?.dropped_ratio ?? null,
      threshold: ev?.threshold ?? null,
      hint: ev
        ? `保留 ${ev.kept} 条，丢弃 ${ev.dropped} 条${
            ev.reattributed ? `，另有 ${ev.reattributed} 条章号挂错已改正` : ""
          }`
        : "",
    },
    {
      key: "mainline_coverage",
      label: "主线覆盖",
      ok: d.checks.mainline_coverage?.ok ?? null,
      value: d.checks.mainline_coverage?.value ?? null,
      threshold: null,
      hint: "",
    },
    {
      key: "character_coverage",
      label: "角色覆盖",
      ok: d.checks.character_coverage?.ok ?? null,
      value: d.checks.character_coverage?.value ?? null,
      threshold: null,
      hint: "",
    },
    {
      key: "compression",
      label: "压缩比",
      ok: d.checks.compression?.ok ?? null,
      value: d.stats?.ratio ?? null,
      threshold: null,
      hint: `输入 ${(d.stats?.chars_in ?? 0).toLocaleString()} 字 → 输出 ${(
        d.stats?.chars_out ?? 0
      ).toLocaleString()} 字`,
    },
  ];
});

/** 百分比显示（闸门的 value 是 0~1 的比例）。 */
function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

/** 三态标签：true=通过 / false=不通过 / null=这一项不适用（根本没跑）。 */
function checkLabel(ok: boolean | null | undefined): { text: string; tag: string } {
  if (ok === true) return { text: "通过", tag: "ok" };
  if (ok === false) return { text: "不通过", tag: "err" };
  return { text: "不适用", tag: "info" };
}

/** 丢弃原因的人话说明（界面上要能回答「为什么丢了这么多」）。 */
const DROP_REASON_TEXT: Record<string, string> = {
  quote_not_found: "引文在原文里找不到（模型改写了句子）",
  quote_far: "引文在书里，但离它声称的章太远",
  quote_too_short: "引文过短，无法定位",
  no_evidence: "没有给出证据",
  no_quote: "证据里没有引文",
  no_chapter_no: "证据里没有章号",
};

async function loadDigest(novelId: number): Promise<void> {
  digestLoading.value = true;
  digestErr.value = null;
  try {
    digest.value = await api.getDigest(novelId);
  } catch (err) {
    // 404 = 这本书还没有摘要（该去跑一次）；其它错误要如实说，不能都当成「没有」。
    const status = (err as { status?: number })?.status;
    digest.value = null;
    if (status && status !== 404) {
      digestErr.value = err instanceof Error ? err.message : String(err);
    }
  } finally {
    digestLoading.value = false;
  }
}

function stopDigestPolling(): void {
  if (digestTimer !== null) {
    clearInterval(digestTimer);
    digestTimer = null;
  }
}

async function pollDigest(projectId: number, jobId: number): Promise<void> {
  try {
    const job = await api.getDistillJob(projectId, jobId);
    digestJob.value = job;
    if (job.status !== "pending" && job.status !== "running") {
      stopDigestPolling();
      if (job.status === "success" && selected.value) {
        await loadDigest(selected.value.id);
      }
    }
  } catch (err) {
    // 轮询失败不打断主流程：下一 tick 会再试，结论由服务端任务状态兜底。
    console.warn("摘要进度轮询失败，下个周期重试：", err);
  }
}

async function startDigest(): Promise<void> {
  if (!selected.value || !digestProjectId.value) return;
  digestSubmitting.value = true;
  digestErr.value = null;
  digestNotice.value = null;
  try {
    const r = await api.startDigest(digestProjectId.value, {
      novel_id: selected.value.id,
      mode: digestMode.value,
      target_ratio: digestRatio.value,
      // 已经有摘要时再点按钮，意思就是「重算一版」⇒ 覆盖旧产物。
      // 没有摘要时传 false：后端会按 (正文版本, 档位, 压缩比) 判定能否复用，
      // 参数没变就直接复用旧产物，不白花一次模型调用。
      force: digestState.value !== "none",
    });
    digestNotice.value = r.reused
      ? "这本书已有摘要任务在运行，已切到那条任务的进度。"
      : "已提交，摘要在后台生成。";
    stopDigestPolling();
    digestTimer = setInterval(() => {
      void pollDigest(digestProjectId.value!, r.job_id);
    }, 1500);
    void pollDigest(digestProjectId.value, r.job_id);
  } catch (err) {
    digestErr.value = err instanceof Error ? err.message : String(err);
  } finally {
    digestSubmitting.value = false;
  }
}

async function openChunks(n: NovelOut) {
  selected.value = n;
  hits.value = null;
  q.value = "";
  chunksLoading.value = true;
  chunksError.value = null;
  searchError.value = null;
  digestJob.value = null;
  digestNotice.value = null;
  void loadDigest(n.id);
  try {
    chunks.value = await api.listChunks(n.id);
  } catch (err) {
    chunks.value = [];
    chunksError.value = err instanceof Error ? err.message : String(err);
  } finally {
    chunksLoading.value = false;
  }
}

async function runSearch() {
  if (!selected.value || !q.value.trim()) return;
  searching.value = true;
  searchError.value = null;
  try {
    hits.value = await api.searchNovel(selected.value.id, q.value.trim());
  } catch (err) {
    hits.value = null;
    searchError.value = err instanceof Error ? err.message : String(err);
  } finally {
    searching.value = false;
  }
}

// 提交需要项目归属（任务表的项目列必填）。项目列表只加载一次。
onMounted(async () => {
  await reloadNovels();
  try {
    projects.value = await api.listProjects();
    if (projects.value.length) digestProjectId.value = projects.value[0].id;
  } catch {
    // 拿不到项目列表就提交不了：表单里会显示「需要先有一个项目」。
  }
});
</script>

<template>
  <header class="page-head corpus-head">
    <div>
      <div class="eyebrow">01 · REFERENCE LIBRARY</div>
      <h1>语料库</h1>
      <p>让参考书止于参考：入库先体检，检索可追溯，全书摘要保留原文证据。</p>
    </div>
    <button class="btn primary" type="button" @click="goAssistant">通过助手投放语料 <span aria-hidden="true">→</span></button>
  </header>

  <div class="corpus-overview">
    <section class="library-browser" aria-labelledby="corpus-list-title">
      <div class="browser-head">
        <div><span class="browser-kicker">已入库</span><h2 id="corpus-list-title">参考书架</h2></div>
        <span class="book-count">{{ novels?.length ?? 0 }} 本</span>
      </div>
      <div v-if="novelsLoading" class="empty"><span class="spinner" /> 正在读取语料…</div>
      <div v-else-if="novelsError" class="notice err" role="alert">
        {{ novelsError }}<button class="link-btn" type="button" @click="reloadNovels">重试</button>
      </div>
      <div v-else-if="!novels?.length" class="corpus-empty">
        <span aria-hidden="true">00</span><strong>书架还是空的</strong><p>从助手附件开始，完成清洗、抽检、蒸馏与审查后再入库。</p>
      </div>
      <div v-else class="book-list">
        <button
          v-for="n in novels"
          :key="n.id"
          class="book-row"
          :class="{ active: selected?.id === n.id }"
          type="button"
          @click="openChunks(n)"
        >
          <span class="book-spine" aria-hidden="true"></span>
          <span class="book-copy"><strong>{{ n.title || "（无题）" }}</strong><small>{{ n.platform || "未标记平台" }} · #{{ n.id }}</small></span>
          <span class="book-enter" aria-hidden="true">↗</span>
        </button>
      </div>
    </section>

    <aside class="ingest-guide">
      <span class="guide-index">INGEST / 01</span>
      <h2>不是上传，<br />是建立可靠参考。</h2>
      <p>文本统一经助手附件进入完整工序，避免空分片、断句与混书内容直接污染参考库。</p>
      <ol>
        <li><span>01</span>清洗文本</li><li><span>02</span>抽检与体检</li>
        <li><span>03</span>提炼风格</li><li><span>04</span>审查后入库</li>
      </ol>
      <small>支持 .txt / .md · 由助手附件链路暂存与交付</small>
    </aside>
  </div>

  <section v-if="foreignOf(selected)" class="card">
    <div class="card-title">入库体检 <span class="hint">《{{ selected?.title }}》</span></div>
    <div class="notice warn" style="margin-top: 8px">
      {{ foreignOf(selected)!.dropped ? "入库时已剔除这些内容。" : "这些内容仍在库里。" }}
    </div>
    <ForeignBlocksNotice :report="foreignOf(selected)!" />
  </section>

  <template v-if="selected">
    <section class="card">
      <div class="card-title">
        全书摘要 ·《{{ selected.title }}》
        <span class="hint">
          {{ digestLoading ? "读取中" : digestState === "ready" ? "已生成" : digestState === "broken" ? "内容为空" : "尚未生成" }}
        </span>
      </div>

      <div class="notice info" style="margin-top: 4px">
        摘要把一整本书压成时间线、角色生平、情境反应三块，<strong>每条都挂原文证据</strong>：
        引文必须逐字出现在书里；章号挂错的会在邻近章节里改正，
        查无此句的条目才丢弃，并留下丢弃原因。产物按书保存，跨项目复用；
        续写时它可以替代整本进入上下文。
      </div>

      <div v-if="digestErr" class="notice err" style="margin-top: 10px">{{ digestErr }}</div>

      <!-- 任务进度：摘要要走模型、读整本书，是分钟级任务，必须有可见进度 -->
      <div v-if="digestJob" style="margin-top: 12px">
        <DistillProgress :job="digestJob" title="全书摘要" />
      </div>
      <div v-if="digestNotice" class="notice" style="margin-top: 10px">{{ digestNotice }}</div>

      <!-- 已有摘要：结论 + 验收 + 三块内容 -->
      <template v-if="digestState === 'ready' && digest">
        <div class="row" style="margin-top: 14px; gap: 6px; flex-wrap: wrap">
          <span class="tag" :class="digest.verify_passed ? 'ok' : 'err'">
            {{ digest.verify_passed ? "验收通过" : "验收未通过" }}
          </span>
          <span class="tag info">{{ digest.mode === "whole" ? "整本一次投喂" : digest.mode === "retrieval" ? "切块抽取后合并" : digest.mode }}</span>
          <span class="faint" style="font-size: var(--fs-xs)">{{ digest.mode_reason }}</span>
        </div>

        <div class="list" style="margin-top: 12px">
          <div v-for="c in digestChecks" :key="c.key" class="list-item" style="align-items: flex-start">
            <div style="width: 92px; flex: none">
              <span class="tag" :class="checkLabel(c.ok).tag">{{ checkLabel(c.ok).text }}</span>
            </div>
            <div style="flex: 1">
              <div class="row tight">
                <strong>{{ c.label }}</strong>
                <span class="mono" style="font-size: var(--fs-xs)">{{ pct(c.value) }}</span>
              </div>
              <div v-if="c.hint" class="faint" style="font-size: var(--fs-xs)">{{ c.hint }}</div>
            </div>
          </div>
        </div>

        <div v-if="digest.not_applicable?.length" class="notice warn" style="margin-top: 10px">
          有 {{ digest.not_applicable.length }} 项验收**不适用**（这一项没有候选、根本没跑），不等于通过。
        </div>

        <!-- 丢弃原因：界面要能回答「为什么丢了这么多」 -->
        <details v-if="digest.evidence_detail?.drop_reasons && Object.keys(digest.evidence_detail.drop_reasons).length" style="margin-top: 10px">
          <summary class="faint" style="cursor: pointer; font-size: var(--fs-xs)">
            证据丢弃原因明细
          </summary>
          <div class="list" style="margin-top: 8px">
            <div
              v-for="(n, reason) in digest.evidence_detail.drop_reasons"
              :key="reason"
              class="list-item"
            >
              <span class="mono" style="width: 48px; flex: none">{{ n }} 条</span>
              <span style="flex: 1">{{ DROP_REASON_TEXT[reason] ?? reason }}</span>
            </div>
          </div>
        </details>

        <!-- 三块结构化内容 -->
        <details style="margin-top: 12px" open>
          <summary style="cursor: pointer; font-weight: 600">
            故事时间线
            <span class="hint" style="margin-left: 8px">{{ digest.payload?.timeline?.length ?? 0 }} 条</span>
          </summary>
          <div class="list" style="margin-top: 8px">
            <div v-for="(t, i) in digest.payload?.timeline ?? []" :key="i" class="list-item" style="align-items: flex-start">
              <span class="tag" style="flex: none">第 {{ t.chapter_no ?? "?" }} 章</span>
              <div style="flex: 1">
                <div>{{ t.in_story_time ? t.in_story_time + "｜" : "" }}{{ t.event }}</div>
                <div v-if="t.actors?.length" class="faint" style="font-size: var(--fs-xs)">
                  {{ t.actors.join("、") }}
                </div>
                <div v-if="t.evidence?.length" class="faint mono" style="font-size: var(--fs-xs)">
                  引文：{{ t.evidence[0].quote }}
                </div>
              </div>
            </div>
          </div>
        </details>

        <details style="margin-top: 10px">
          <summary style="cursor: pointer; font-weight: 600">
            主要角色
            <span class="hint" style="margin-left: 8px">{{ digest.payload?.characters?.length ?? 0 }} 位</span>
          </summary>
          <div class="list" style="margin-top: 8px">
            <div v-for="(c, i) in digest.payload?.characters ?? []" :key="i" class="list-item" style="align-items: flex-start">
              <strong style="width: 110px; flex: none">{{ c.name }}</strong>
              <div style="flex: 1">
                <div>{{ c.role }}{{ c.arc ? "：" + c.arc : "" }}</div>
                <div v-if="c.relations?.length" class="faint" style="font-size: var(--fs-xs)">
                  关系：{{ c.relations.map((r) => `${r.name}（${r.kind ?? ""}）`).join("、") }}
                </div>
                <div v-if="c.voice?.habits?.length" class="faint" style="font-size: var(--fs-xs)">
                  说话习惯：{{ c.voice.habits.join("、") }}
                </div>
              </div>
            </div>
          </div>
        </details>

        <details style="margin-top: 10px">
          <summary style="cursor: pointer; font-weight: 600">
            情境反应
            <span class="hint" style="margin-left: 8px">{{ digest.payload?.reactions?.length ?? 0 }} 条</span>
          </summary>
          <div class="list" style="margin-top: 8px">
            <div v-for="(r, i) in digest.payload?.reactions ?? []" :key="i" class="list-item" style="align-items: flex-start">
              <strong style="width: 110px; flex: none">{{ r.character }}</strong>
              <div style="flex: 1">
                <div>{{ r.trigger }}</div>
                <div class="faint" style="font-size: var(--fs-xs)">
                  会说：{{ r.says }}；会做：{{ r.does }}
                </div>
                <div v-if="r.evidence?.length" class="faint mono" style="font-size: var(--fs-xs)">
                  引文：{{ r.evidence[0].quote }}
                </div>
              </div>
            </div>
          </div>
        </details>
      </template>

      <div v-if="digestState === 'broken'" class="notice err" style="margin-top: 12px">
        摘要内容为空，可能是产物损坏。可用下面的「重新生成全书摘要」重跑一次。
      </div>

      <!-- 提交表单：**始终可提交**。
           浏览器核对时发现：表单原先挂在「尚无摘要」分支下，摘要一旦生成就再也点不到，
           而「内容为空 ⇒ 勾选强制重新生成」那条提示指向的复选框压根不渲染 ⇒ 死路。
           现在已有摘要时按钮变成「重新生成全书摘要」，后端按 force 覆盖旧产物。 -->
      <div style="margin-top: 12px">
        <div class="row" style="gap: 10px; flex-wrap: wrap; align-items: flex-end">
          <div class="field" style="width: 200px">
            <label>发起项目</label>
            <select class="input" v-model.number="digestProjectId">
              <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.title }}</option>
            </select>
          </div>
          <div class="field" style="width: 160px">
            <label>抽取档位</label>
            <select class="input" v-model="digestMode">
              <option value="auto">自动</option>
              <option value="whole">整本一次投喂</option>
              <option value="retrieval">切块抽取后合并</option>
            </select>
          </div>
          <div class="field" style="width: 130px">
            <label>压缩比目标</label>
            <input class="input" type="number" min="1" max="100" v-model.number="digestRatio" />
          </div>
          <button
            class="btn primary"
            :disabled="digestSubmitting || digestActive || !digestProjectId || !projects.length"
            @click="startDigest"
          >
            {{ digestSubmitting ? "提交中…" : digestState === "none" ? "生成全书摘要" : "重新生成全书摘要" }}
          </button>
        </div>
        <div v-if="!projects.length" class="notice warn" style="margin-top: 10px">
          还没有任何项目。摘要任务需要归属到一个项目，请先在「创作」里新建一个。
        </div>
        <div class="faint" style="margin-top: 10px; font-size: var(--fs-xs); line-height: 1.7">
          「自动」档由后端按书的大小与渠道能力决定：渠道声明过单次能读多少字、
          且这本书在它能吃下的量级内 ⇒ 整本一次投喂（模型看得到全局）；
          渠道没声明过 ⇒ 保守退回切块抽取。选哪一种、为什么，生成后都会写在结果里。
          换了档位或压缩比目标再跑，会按新参数重算。
        </div>
      </div>
    </section>

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

      <div v-if="searchError" class="notice err" role="alert" style="margin-bottom: 16px">{{ searchError }}</div>
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
      <div v-else-if="chunksError" class="notice err" role="alert">
        {{ chunksError }}<button class="link-btn" type="button" @click="selected && openChunks(selected)">重试</button>
      </div>
      <div v-else-if="!chunks.length" class="empty">这本书暂时没有可显示的分片。</div>
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

<style scoped>
.corpus-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; padding-top: 8px; }
.corpus-overview { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(280px, .75fr); gap: 18px; margin-bottom: 22px; }
.library-browser { min-width: 0; padding: 22px 24px; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 10px; }
.browser-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
.browser-kicker, .guide-index { color: var(--accent-ink); font: 10px/1.4 var(--font-mono); letter-spacing: .12em; }
.browser-head h2 { margin-top: 4px; font: 21px/1.3 var(--font-serif); }
.book-count { color: var(--fg-faint); font-size: var(--fs-xs); }
.book-list { display: flex; flex-direction: column; max-height: 390px; overflow-y: auto; }
.book-row { display: grid; grid-template-columns: 4px minmax(0, 1fr) auto; gap: 14px; align-items: center; padding: 15px 8px; text-align: left; color: var(--fg); background: none; border: 0; border-bottom: 1px solid var(--border); cursor: pointer; }
.book-row:last-child { border-bottom: 0; }
.book-row:hover, .book-row.active { background: var(--bg); }
.book-spine { width: 3px; height: 32px; background: var(--border-strong); border-radius: 2px; }
.book-row.active .book-spine { background: var(--accent); }
.book-copy { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.book-copy strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font: 16px/1.4 var(--font-serif); }
.book-copy small { color: var(--fg-faint); font-size: 10px; }
.book-enter { color: var(--accent-ink); opacity: 0; }
.book-row:hover .book-enter, .book-row.active .book-enter { opacity: 1; }
.ingest-guide { padding: 24px; color: #ece8de; background: #2b2c27; border-radius: 10px; }
.ingest-guide .guide-index { color: #d8836f; }
.ingest-guide h2 { margin-top: 12px; font: 23px/1.45 var(--font-serif); }
.ingest-guide > p { margin: 12px 0 18px; color: #aaa99f; font-size: 12px; line-height: 1.8; }
.ingest-guide ol { display: grid; grid-template-columns: 1fr 1fr; gap: 9px 14px; margin: 0; padding: 16px 0; list-style: none; border-top: 1px solid #484940; border-bottom: 1px solid #484940; }
.ingest-guide li { display: flex; align-items: center; gap: 7px; color: #d0cfc5; font-size: 11px; }
.ingest-guide li span { color: #818278; font: 9px/1 var(--font-mono); }
.ingest-guide small { display: block; margin-top: 14px; color: #85867c; font-size: 10px; line-height: 1.7; }
.corpus-empty { min-height: 220px; display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--fg-muted); text-align: center; }
.corpus-empty > span { color: var(--border-strong); font: 34px/1 var(--font-serif); }
.corpus-empty strong { margin-top: 10px; }
.corpus-empty p { max-width: 36em; margin: 6px 0 0; color: var(--fg-faint); font-size: var(--fs-sm); }
.link-btn { margin-left: 10px; padding: 0; color: inherit; background: none; border: 0; text-decoration: underline; cursor: pointer; }
@media (max-width: 980px) { .corpus-overview { grid-template-columns: 1fr; } }
@media (max-width: 640px) {
  .corpus-head { align-items: flex-start; flex-direction: column; }
  .library-browser, .ingest-guide { padding: 18px 16px; }
  .book-copy strong { white-space: normal; }
}
</style>
