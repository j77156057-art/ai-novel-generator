<script setup lang="ts">
/**
 * 角色对话面板（第三板块）。
 *
 * ## 它是什么
 *
 * 「和书里的角色聊几句」：选一本参考书 → 选一个角色 → 设定**故事进行到第几章**
 * → 提问 → 他用**自己的身份**回答。
 *
 * ## 为什么要有「故事进行到第几章」
 *
 * 角色只能知道**到此刻为止**的事。边界与续写路径**共用同一条**：
 * 没有边界 ⇒ 整本挡掉（fail closed）—— 这不是功能坏了，是防剧透：
 * 否则角色会「记得」还没发生的事。
 *
 * ## 每条证据都带来源标注
 *
 * 「他亲历」= 他自己开过口；「他在场听到」= 别人在说，他在旁边。
 * 模型据此决定能不能当作**自己的记忆**说出口（能演出「这事我是听说的」）。
 */
import { computed, onMounted, ref, watch } from "vue";
import { api } from "@/api/client";
import type {
  NovelOut,
  RoleplayMessage,
  RoleplaySource,
} from "@/api/types";
import type { CutoffInputs } from "@/utils/referenceBoundary";

const props = defineProps<{
  projectId: number;
  /** 项目已选的参考书（决定在哪些书里找角色）。 */
  novelIds: number[];
  /** 各本书的章节上限（用来填「故事进行到第几章」）。 */
  novelChapterMax: Record<number, number>;
  /** 已存的原文边界（novel_id → 章节）。 */
  cutoffs: CutoffInputs;
}>();

const emit = defineEmits<{
  (e: "update:cutoffs", v: CutoffInputs): void;
  (e: "update:character", v: string): void;
}>();

const characters = ref<string[]>([]);
const projectChars = ref<string[]>([]);
const referenceChars = ref<string[]>([]);
const refQuery = ref("");
const showReference = ref(false);
const character = ref("");
const question = ref("");
const sending = ref(false);
const error = ref<string | null>(null);
const messages = ref<RoleplayMessage[]>([]);
const novels = ref<NovelOut[]>([]);

// —— 边界输入：每本书一行，填「故事进行到第几章」——
const cutoffInputs = ref<CutoffInputs>({});

onMounted(async () => {
  cutoffInputs.value = { ...props.cutoffs };
  try {
    const [chars, list] = await Promise.all([
      api.roleplayCharacters(props.projectId),
      api.listNovels(),
    ]);
    characters.value = chars.characters ?? [];
    projectChars.value = chars.project ?? [];
    referenceChars.value = chars.reference ?? [];
    if (chars.current) character.value = chars.current;
    novels.value = list ?? [];
  } catch (e) {
    error.value = `角色清单取不到：${(e as Error).message}`;
  }
});

watch(
  () => props.cutoffs,
  (v) => {
    cutoffInputs.value = { ...v };
  },
);

const filteredReference = computed(() => {
  const q = refQuery.value.trim();
  if (!q) return referenceChars.value;
  return referenceChars.value.filter((c) => c.includes(q));
});

const cutoffIncomplete = computed(() => {
  // 有参考书却没填边界 ⇒ 角色检索会被整本挡掉（fail closed）
  return props.novelIds.length > 0 &&
    props.novelIds.some((id) => !cutoffInputs.value[id]);
});

const canSend = computed(
  () => !!character.value && !!question.value.trim() && !sending.value,
);

function saveCutoff(novelId: number) {
  const next = { ...cutoffInputs.value };
  const v = next[novelId];
  const num = typeof v === "string" ? Number(v) : v;
  if (num === undefined || num === null || !Number.isFinite(num) || num < 1) {
    delete next[novelId];
  } else {
    next[novelId] = num;
  }
  emit("update:cutoffs", next);
}

async function send() {
  if (!canSend.value) return;
  sending.value = true;
  error.value = null;
  const q = question.value.trim();
  try {
    const res = await api.roleplayChat(props.projectId, {
      character: character.value,
      question: q,
      novel_ids: props.novelIds,
    });
    messages.value = [
      ...messages.value,
      {
        id: res.id,
        character: res.character,
        question: q,
        answer: res.answer,
        sources: res.sources ?? [],
        novel_ids: props.novelIds,
        created_at: new Date().toISOString(),
      },
    ];
    question.value = "";
    emit("update:character", res.character);
  } catch (e) {
    error.value = `没能问出答案：${(e as Error).message}`;
  } finally {
    sending.value = false;
  }
}

function originTag(s: RoleplaySource): string {
  return s.origin === "亲历" ? "他亲历" : "他在场听到";
}
</script>

<template>
  <section class="card roleplay">
    <div class="card-title">角色对话</div>
    <p class="muted" style="font-size: var(--fs-sm); margin-bottom: var(--gap)">
      和书里的角色聊几句。他只知道自己<strong>经历过</strong>的事 ——
      检索到的每段原文都标了来源（他亲历 / 他在场听到），
      他会据此决定哪些能当作自己的记忆说出口。不知道的事他会直说不知道。
    </p>

    <!-- ① 选角色 -->
    <div class="rp-block">
      <div class="card-title" style="font-size: var(--fs-md)">扮演哪个角色</div>
      <div v-if="!characters.length" class="empty">
        这个项目还没有可选角色 —— 先在上面选一本参考书，或先生成几段正文。
      </div>
      <template v-else>
        <div v-if="projectChars.length">
          <div class="rp-group">这个项目里的角色</div>
          <div class="rp-chars">
            <button
              v-for="c in projectChars"
              :key="c"
              type="button"
              class="rp-char"
              :class="{ on: character === c }"
              :aria-pressed="character === c"
              @click="character = c; emit('update:character', c)"
            >
              {{ c }}
            </button>
          </div>
        </div>
        <div v-if="referenceChars.length" class="rp-ref">
          <button type="button" class="rp-ref-toggle" @click="showReference = !showReference">
            {{ showReference ? "收起" : "展开" }}参考书里的角色（{{ referenceChars.length }}）
          </button>
          <div v-if="showReference" class="rp-ref-body">
            <input
              v-model="refQuery"
              class="input rp-search"
              type="search"
              placeholder="搜一个角色名…"
            />
            <div v-if="!filteredReference.length" class="empty">没有匹配的角色。</div>
            <div v-else class="rp-chars">
              <button
                v-for="c in filteredReference"
                :key="c"
                type="button"
                class="rp-char"
                :class="{ on: character === c }"
                :aria-pressed="character === c"
                @click="character = c; emit('update:character', c)"
              >
                {{ c }}
              </button>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- ② 故事进行到第几章（与续写同一条边界） -->
    <div v-if="novelIds.length" class="rp-block">
      <div class="card-title" style="font-size: var(--fs-md)">
        故事进行到第几章<span class="hint">角色只能知道到这一章为止的事</span>
      </div>
      <div v-for="nid in novelIds" :key="nid" class="rp-cut">
        <span class="rp-cut-name">
          《{{ novels.find((n) => n.id === nid)?.title || "无题" }}》
        </span>
        <label class="cut-label" :for="`rp-cut-${nid}`">故事进行到第</label>
        <input
          :id="`rp-cut-${nid}`"
          class="input cut-input"
          type="number"
          min="1"
          :max="novelChapterMax[nid] || undefined"
          :value="cutoffInputs[nid] ?? ''"
          @input="cutoffInputs[nid] = (($event.target as HTMLInputElement).value || null); saveCutoff(nid)"
        />
        <span class="cut-label">章</span>
        <span class="faint">全书 {{ novelChapterMax[nid] || 0 }} 章</span>
      </div>
      <p v-if="cutoffIncomplete" class="muted rp-note">
        没有边界就等于整本不可见 —— 他会「什么都不知道」。填上章节即可。
      </p>
    </div>

    <!-- ③ 问答 -->
    <div class="rp-block">
      <div class="card-title" style="font-size: var(--fs-md)">问点什么</div>
      <div v-if="!messages.length" class="empty">
        还没有对话。问一件只有他知道的事 —— 比如「你陪着他长大的，他小时候什么样？」
      </div>
      <div v-for="m in messages" :key="m.id" class="rp-msg">
        <div class="rp-q">问：{{ m.question }}</div>
        <div class="rp-a">答：{{ m.answer }}</div>
        <details v-if="m.sources.length" class="rp-src">
          <summary>他依据的原文（{{ m.sources.length }} 段）</summary>
          <div v-for="(s, i) in m.sources" :key="i" class="rp-src-item">
            <span class="rp-tag" :class="s.origin === '亲历' ? 'ok' : 'muted'">
              {{ originTag(s) }}
            </span>
            <span class="faint">{{ s.basis }} · 第 {{ s.chapter_no ?? "?" }} 章</span>
            <p class="rp-src-text">{{ s.text }}</p>
          </div>
        </details>
      </div>
      <div class="row" style="margin-top: var(--gap)">
        <input
          v-model="question"
          class="input rp-input"
          type="text"
          placeholder="问他一件事…"
          :disabled="!character || sending"
          @keyup.enter="send"
        />
        <button class="btn primary" :disabled="!canSend" @click="send">
          {{ sending ? "在问…" : "问他" }}
        </button>
      </div>
      <p v-if="error" class="notice err">{{ error }}</p>
    </div>
  </section>
</template>

<style scoped>
.rp-block {
  margin-bottom: var(--gap-lg);
  padding-top: var(--gap);
  border-top: 1px solid var(--border);
}
.rp-block:first-of-type {
  border-top: none;
}
.rp-chars {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.rp-char {
  padding: 6px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  background: var(--bg-elev);
  color: var(--fg);
  cursor: pointer;
  font-size: var(--fs-sm);
}
.rp-char.on {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent-ink);
  font-weight: 600;
}
.rp-cut {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: var(--fs-sm);
}
.rp-cut-name {
  min-width: 8em;
  color: var(--fg-muted);
}
.rp-note {
  font-size: var(--fs-sm);
  margin-top: 4px;
}
.rp-msg {
  padding: 10px 12px;
  margin-bottom: 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-elev);
}
.rp-q {
  font-size: var(--fs-sm);
  color: var(--fg-muted);
}
.rp-a {
  margin-top: 4px;
  white-space: pre-wrap;
}
.rp-src {
  margin-top: 8px;
  font-size: var(--fs-sm);
}
.rp-src-item {
  padding: 6px 0;
  border-top: 1px dashed var(--border);
}
.rp-src-text {
  margin: 4px 0 0;
  color: var(--fg-muted);
  white-space: pre-wrap;
  font-size: var(--fs-xs);
}
.rp-tag {
  display: inline-block;
  padding: 1px 6px;
  margin-right: 6px;
  border-radius: 3px;
  font-size: var(--fs-xs);
}
.rp-tag.ok {
  background: var(--ok-soft);
  color: var(--ok);
}
.rp-tag.muted {
  background: var(--bg-sunken);
  color: var(--fg-muted);
}
.rp-input {
  flex: 1;
}
.rp-group {
  font-size: var(--fs-xs);
  color: var(--fg-faint);
  margin-bottom: 6px;
}
.rp-ref {
  margin-top: 10px;
}
.rp-ref-toggle {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
  background: none;
  border: none;
  padding: 2px 0;
  cursor: pointer;
  text-decoration: underline;
}
.rp-ref-body {
  margin-top: 8px;
}
.rp-search {
  max-width: 260px;
  margin-bottom: 8px;
}
</style>
