<script setup lang="ts">
defineOptions({ name: "CharacterCardsView" });
/**
 * 角色卡页（/projects/:id/cards）。
 *
 * 一个角色一张静态档案：身份 / 能力（武器·功法·能力·上限）/ 声纹 / 量级 / 关系。
 * 与「角色状态」（位置/伤势/目标）不同，这一层是几十段都不变的设定。
 *
 * 三条纪律（与后端一致）：
 * 1. AI 只建议：补全/批量生成只返回草稿，不落库；
 * 2. 人写的归人：保存后后端标 `来源=user`，自动流程不再覆盖；
 * 3. 声纹要能比对：撞车时明确提示是哪几个锚点相同。
 */
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { api } from "@/api/client";
import ProjectNav from "@/components/ProjectNav.vue";
import type {
  CardAbilityItem,
  CharacterCard,
  CharacterCardItem,
  CharacterCardListResponse,
  ProjectOut,
} from "@/api/types";

const route = useRoute();
const projectId = computed(() => Number(route.params.id));

const project = ref<ProjectOut | null>(null);
const data = ref<CharacterCardListResponse | null>(null);
const loading = ref(true);
const loadError = ref("");
const flash = ref("");

const cards = computed(() => data.value?.characters ?? []);
const collisions = computed(() => data.value?.voice_collisions ?? []);

function textOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// ==================================================================
// 草稿（把嵌套卡摊平成一组输入框，避免深层 v-model）
// ==================================================================

interface CardDraft {
  gender: string;
  age: string;
  appearance: string;
  job: string;
  background: string;
  weapons: string;
  techniques: string;
  abilities: string;
  limits: string;
  selfCall: string;
  callOthers: string;
  sentenceLen: string;
  sentenceEnd: string;
  catchphrases: string;
  banned: string;
  tone: string;
  scale: string;
  scaleAction: string;
  suppression: string;
  relations: string;
}

function blankDraft(): CardDraft {
  return {
    gender: "", age: "", appearance: "", job: "", background: "",
    weapons: "", techniques: "", abilities: "", limits: "",
    selfCall: "", callOthers: "", sentenceLen: "", sentenceEnd: "",
    catchphrases: "", banned: "", tone: "",
    scale: "", scaleAction: "", suppression: "",
    relations: "",
  };
}

function lines(items?: string[]): string {
  return (items ?? []).join("\n");
}

function plainLines(text: string): string[] {
  return text.split("\n").map((s) => s.trim()).filter(Boolean);
}

function itemsToLines(items?: CardAbilityItem[]): string {
  return (items ?? [])
    .map((it) => [it.名称 || "", it.说明 || ""].filter(Boolean).join(" | "))
    .join("\n");
}

function linesToItems(text: string): CardAbilityItem[] {
  const out: CardAbilityItem[] = [];
  for (const line of plainLines(text)) {
    const [name, ...rest] = line.split(/[|｜]/);
    const title = (name || "").trim();
    if (!title) continue;
    out.push({ 名称: title, 说明: rest.join("|").trim() });
  }
  return out;
}

function relationsToLines(rel?: Record<string, string>): string {
  return Object.entries(rel ?? {}).map(([who, text]) => `${who} | ${text}`).join("\n");
}

function linesToRelations(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of plainLines(text)) {
    const [who, ...rest] = line.split(/[|｜]/);
    const name = (who || "").trim();
    if (!name) continue;
    out[name] = rest.join("|").trim();
  }
  return out;
}

function draftFromCard(card: CharacterCard): CardDraft {
  const d = blankDraft();
  const ident = card.身份 ?? {};
  d.gender = ident.性别 ?? "";
  d.age = ident.年龄 ?? "";
  d.appearance = ident.外貌 ?? "";
  d.job = ident.身份职业 ?? "";
  d.background = ident.出身背景 ?? "";
  const ab = card.能力 ?? {};
  d.weapons = itemsToLines(ab.武器);
  d.techniques = itemsToLines(ab.功法);
  d.abilities = itemsToLines(ab.能力);
  d.limits = lines(ab.能力上限);
  const voice = card.声纹 ?? {};
  d.selfCall = voice.自称 ?? "";
  d.callOthers = lines(voice.称呼他人);
  d.sentenceLen = voice.句长 ?? "";
  d.sentenceEnd = voice.句末 ?? "";
  d.catchphrases = lines(voice.口头禅);
  d.banned = lines(voice.禁用词);
  d.tone = voice.语气底色 ?? "";
  const scale = card.量级 ?? {};
  d.scale = scale.量级 ?? "";
  d.scaleAction = scale.出手规模 ?? "";
  d.suppression = scale.压制 ?? "";
  d.relations = relationsToLines(card.关系);
  return d;
}

function cardFromDraft(d: CardDraft): CharacterCard {
  const card: CharacterCard = {};
  const ident = {
    性别: d.gender.trim(), 年龄: d.age.trim(), 外貌: d.appearance.trim(),
    身份职业: d.job.trim(), 出身背景: d.background.trim(),
  };
  if (Object.values(ident).some(Boolean)) card.身份 = ident;
  const ability = {
    武器: linesToItems(d.weapons), 功法: linesToItems(d.techniques),
    能力: linesToItems(d.abilities), 能力上限: plainLines(d.limits),
  };
  if (ability.武器.length || ability.功法.length || ability.能力.length || ability.能力上限.length) {
    card.能力 = ability;
  }
  const voice = {
    自称: d.selfCall.trim(), 称呼他人: plainLines(d.callOthers),
    句长: d.sentenceLen.trim(), 句末: d.sentenceEnd.trim(),
    口头禅: plainLines(d.catchphrases), 禁用词: plainLines(d.banned),
    语气底色: d.tone.trim(),
  };
  if (voice.自称 || voice.称呼他人.length || voice.句长 || voice.句末
      || voice.口头禅.length || voice.禁用词.length || voice.语气底色) {
    card.声纹 = voice;
  }
  const scale = { 量级: d.scale.trim(), 出手规模: d.scaleAction.trim(), 压制: d.suppression.trim() };
  if (Object.values(scale).some(Boolean)) card.量级 = scale;
  const relations = linesToRelations(d.relations);
  if (Object.keys(relations).length) card.关系 = relations;
  return card;
}

/** 合并 AI 补充：按组覆盖有值的字段，关系按 key 合并。 */
function mergeCard(base: CharacterCard, add: CharacterCard): CharacterCard {
  const out: CharacterCard = JSON.parse(JSON.stringify(base || {}));
  for (const key of ["身份", "能力", "声纹", "量级"] as const) {
    const extra = add[key];
    if (extra && typeof extra === "object" && Object.keys(extra).length) {
      out[key] = { ...(out[key] ?? {}), ...(extra as object) } as never;
    }
  }
  if (add.关系 && Object.keys(add.关系).length) {
    out.关系 = { ...(out.关系 ?? {}), ...add.关系 };
  }
  return out;
}

// ==================================================================
// 编辑器
// ==================================================================

const editingName = ref<string | null>(null);
const isNew = ref(false);
const draft = ref<CardDraft>(blankDraft());
const note = ref("");
const aiBusy = ref(false);
const aiInfo = ref("");
const aiError = ref("");
const saving = ref(false);
const saveError = ref("");

function openEditor(card: CharacterCardItem | null, name = ""): void {
  editingName.value = card ? card.name : name;
  isNew.value = !card;
  draft.value = draftFromCard(card?.card ?? {});
  note.value = "";
  aiInfo.value = "";
  aiError.value = "";
  saveError.value = "";
}

function closeEditor(): void {
  editingName.value = null;
  isNew.value = false;
  aiInfo.value = "";
  aiError.value = "";
  saveError.value = "";
}

function startNew(): void {
  editingName.value = "";
  isNew.value = true;
  draft.value = blankDraft();
  note.value = "";
  aiInfo.value = "";
  aiError.value = "";
  saveError.value = "";
}

async function aiComplete(): Promise<void> {
  const name = (editingName.value || "").trim();
  if (!name) {
    aiError.value = "先填角色名，AI 才有对象可补全。";
    return;
  }
  aiBusy.value = true;
  aiError.value = "";
  aiInfo.value = "";
  try {
    const res = await api.generateCharacterCard(
      projectId.value, name, cardFromDraft(draft.value), note.value.trim(),
    );
    if (!res.ok) {
      aiError.value = "AI 没有给出可用结果，可换一句补充要求再试。";
      return;
    }
    draft.value = draftFromCard(mergeCard(cardFromDraft(draft.value), res.补充));
    aiInfo.value = res.说明 || "已补全，检查无误后点「保存角色卡」。";
  } catch (err) {
    aiError.value = `AI 补全失败：${textOf(err)}`;
  } finally {
    aiBusy.value = false;
  }
}

async function saveCard(): Promise<void> {
  const name = (editingName.value || "").trim();
  if (!name) {
    saveError.value = "角色名不能为空。";
    return;
  }
  saving.value = true;
  saveError.value = "";
  try {
    await api.saveCharacterCard(projectId.value, name, cardFromDraft(draft.value));
    flash.value = `已保存「${name}」的角色卡（来源=user，自动流程不再覆盖）。`;
    closeEditor();
    await load();
  } catch (err) {
    saveError.value = `保存失败：${textOf(err)}`;
  } finally {
    saving.value = false;
  }
}

// ==================================================================
// 批量新建
// ==================================================================

const batchOpen = ref(false);
const wantsText = ref("");
const batchNote = ref("");
const batchBusy = ref(false);
const batchError = ref("");
const batchInfo = ref("");
const batchDrafts = ref<{ name: string; card: CharacterCard; picked: boolean }[]>([]);
const batchSaving = ref(false);

async function runBatch(): Promise<void> {
  const wants = plainLines(wantsText.value);
  if (!wants.length) {
    batchError.value = "至少写一个角色名（每行一个，可写「名字：一句话设定」）。";
    return;
  }
  batchBusy.value = true;
  batchError.value = "";
  batchInfo.value = "";
  try {
    const res = await api.generateCharacterCardsBatch(projectId.value, wants, batchNote.value.trim());
    if (!res.ok) {
      batchError.value = "AI 没有产出可用角色，可改写要求后重试。";
      return;
    }
    batchDrafts.value = Object.entries(res.角色).map(([name, card]) => ({
      name, card, picked: true,
    }));
    const clash = res.冲突?.length ? `；声纹冲突 ${res.冲突.length} 处` : "";
    batchInfo.value = `已生成 ${batchDrafts.value.length} 个草稿（第 ${res.轮次} 轮${clash}）。确认后再保存，未勾选的不落库。`;
  } catch (err) {
    batchError.value = `批量生成失败：${textOf(err)}`;
  } finally {
    batchBusy.value = false;
  }
}

async function saveBatch(): Promise<void> {
  const picked = batchDrafts.value.filter((d) => d.picked);
  if (!picked.length) return;
  batchSaving.value = true;
  batchError.value = "";
  try {
    for (const item of picked) {
      await api.saveCharacterCard(projectId.value, item.name, item.card);
    }
    flash.value = `已保存 ${picked.length} 个角色卡。`;
    batchDrafts.value = [];
    batchOpen.value = false;
    wantsText.value = "";
    await load();
  } catch (err) {
    batchError.value = `保存失败：${textOf(err)}`;
  } finally {
    batchSaving.value = false;
  }
}

// ==================================================================
// 展示辅助
// ==================================================================

function hasCard(card: CharacterCard): boolean {
  return Object.keys(card ?? {}).length > 0;
}

function identityLine(card: CharacterCard): string {
  const v = card.身份 ?? {};
  return [v.性别, v.年龄, v.身份职业, v.外貌, v.出身背景].filter(Boolean).join(" · ");
}

function abilityLine(card: CharacterCard): string {
  const a = card.能力 ?? {};
  const parts = [
    a.武器?.length ? `武器 ${a.武器.length}` : "",
    a.功法?.length ? `功法 ${a.功法.length}` : "",
    a.能力?.length ? `能力 ${a.能力.length}` : "",
    a.能力上限?.length ? `上限 ${a.能力上限.length}` : "",
  ];
  return parts.filter(Boolean).join(" · ");
}

function voiceLine(card: CharacterCard): string {
  const v = card.声纹 ?? {};
  return [
    v.自称 ? `自称「${v.自称}」` : "",
    v.句长 ? `句长 ${v.句长}` : "",
    v.句末 ? `句末 ${v.句末}` : "",
    v.口头禅?.length ? `口头禅 ${v.口头禅.join("、")}` : "",
  ].filter(Boolean).join(" · ");
}

function relationLine(card: CharacterCard): string {
  return Object.entries(card.关系 ?? {})
    .map(([who, text]) => `${who}：${text}`)
    .join("；");
}

function itemText(item: CardAbilityItem): string {
  return [item.名称, item.说明].filter(Boolean).join(" — ");
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    const [proj, res] = await Promise.all([
      api.getProject(projectId.value),
      api.listCharacterCards(projectId.value),
    ]);
    project.value = proj;
    data.value = res;
  } catch (err) {
    loadError.value = `加载失败：${textOf(err)}`;
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section class="cards-page">
    <ProjectNav :project-id="projectId" />
    <header class="page-head">
      <div>
        <p class="faint kicker">写作素材</p>
        <h1>角色卡</h1>
        <p class="faint">
          一个角色一张静态档案：身份、武器/功法/能力、声纹（怎么说话）、量级与关系。
          AI 只给草稿，确认后才会保存。
        </p>
      </div>
      <div class="head-actions">
        <button class="btn" type="button" @click="batchOpen = !batchOpen">
          {{ batchOpen ? "收起批量新建" : "批量新建角色" }}
        </button>
        <button class="btn primary" type="button" @click="startNew">新建角色</button>
        <router-link class="btn" :to="`/projects/${projectId}`">返回工作台</router-link>
      </div>
    </header>

    <div v-if="flash" class="notice ok">
      {{ flash }}
      <button class="link-btn" type="button" @click="flash = ''">关闭</button>
    </div>

    <div v-if="loading" class="card faint">加载角色卡…</div>
    <div v-else-if="loadError" class="notice err" role="alert">
      {{ loadError }}
      <button class="link-btn" type="button" @click="load">重试</button>
    </div>

    <template v-else>
      <div v-if="collisions.length" class="notice warn">
        <b>声纹撞车：</b>
        <span v-for="(c, i) in collisions" :key="i">
          {{ c.人物.join(" / ") }} 在「{{ c.相同.join("、") }}」上相同{{ i < collisions.length - 1 ? "；" : "。" }}
        </span>
        <span v-if="collisions[0]?.建议" class="faint"> 建议：{{ collisions[0].建议 }}</span>
      </div>

      <!-- 批量新建 -->
      <div v-if="batchOpen" class="card batch">
        <h2>批量新建角色</h2>
        <div class="field">
          <span>要生成的角色（每行一个，可写「名字：一句话设定」）</span>
          <textarea v-model="wantsText" class="input" rows="4" placeholder="林照：沉默的剑客，左手使刀&#10;苏晚：嘴硬心软的医师" />
        </div>
        <div class="field">
          <span>额外要求（可选）</span>
          <input v-model="batchNote" class="input" placeholder="例如：整体偏江湖气；不要重复的姓氏" />
        </div>
        <div class="actions">
          <button class="btn primary" type="button" :disabled="batchBusy" @click="runBatch">
            {{ batchBusy ? "生成中…" : "生成草稿" }}
          </button>
          <span class="faint">生成结果不会自动保存。</span>
        </div>
        <div v-if="batchError" class="notice err">{{ batchError }}</div>
        <div v-if="batchInfo" class="notice info">{{ batchInfo }}</div>

        <div v-if="batchDrafts.length" class="batch-list">
          <label v-for="item in batchDrafts" :key="item.name" class="batch-item">
            <input v-model="item.picked" type="checkbox" />
            <div>
              <b>{{ item.name }}</b>
              <p v-if="identityLine(item.card)" class="faint">{{ identityLine(item.card) }}</p>
              <p v-if="abilityLine(item.card)" class="faint">{{ abilityLine(item.card) }}</p>
              <p v-if="voiceLine(item.card)" class="faint">{{ voiceLine(item.card) }}</p>
            </div>
          </label>
          <button class="btn primary" type="button" :disabled="batchSaving" @click="saveBatch">
            {{ batchSaving ? "保存中…" : `保存选中的 ${batchDrafts.filter((d) => d.picked).length} 个` }}
          </button>
        </div>
      </div>

      <!-- 编辑器 -->
      <div v-if="editingName !== null" class="card editor">
        <div class="editor-head">
          <h2>{{ isNew ? "新建角色" : `编辑「${editingName}」` }}</h2>
          <button class="link-btn" type="button" @click="closeEditor">收起</button>
        </div>
        <div class="field">
          <span>角色名</span>
          <input v-model="editingName" class="input" :disabled="!isNew" />
        </div>

        <h3>AI 补全</h3>
        <div class="field">
          <span>补充要求（可选）</span>
          <input v-model="note" class="input" placeholder="例如：补武器和口头禅；能力上限写清楚" />
        </div>
        <div class="actions">
          <button class="btn" type="button" :disabled="aiBusy" @click="aiComplete">
            {{ aiBusy ? "补全中…" : "AI 补全（先预览）" }}
          </button>
          <span class="faint">只补进下方草稿，点保存才落库。</span>
        </div>
        <div v-if="aiError" class="notice err">{{ aiError }}</div>
        <div v-if="aiInfo" class="notice info">{{ aiInfo }}</div>

        <h3>身份</h3>
        <div class="grid2">
          <label class="field"><span>性别</span><input v-model="draft.gender" class="input" /></label>
          <label class="field"><span>年龄</span><input v-model="draft.age" class="input" /></label>
          <label class="field wide"><span>外貌</span><input v-model="draft.appearance" class="input" /></label>
          <label class="field"><span>身份职业</span><input v-model="draft.job" class="input" /></label>
          <label class="field"><span>出身背景</span><input v-model="draft.background" class="input" /></label>
        </div>

        <h3>能力</h3>
        <p class="faint hint">每行一条：名称 | 说明</p>
        <div class="grid2">
          <label class="field"><span>武器</span><textarea v-model="draft.weapons" class="input" rows="2" /></label>
          <label class="field"><span>功法 / 技艺</span><textarea v-model="draft.techniques" class="input" rows="2" /></label>
          <label class="field"><span>能力 / 天赋</span><textarea v-model="draft.abilities" class="input" rows="2" /></label>
          <label class="field"><span>能力上限（每行一条）</span><textarea v-model="draft.limits" class="input" rows="2" /></label>
        </div>

        <h3>声纹（怎么说话）</h3>
        <div class="grid2">
          <label class="field"><span>自称</span><input v-model="draft.selfCall" class="input" placeholder="我 / 在下 / 本座" /></label>
          <label class="field">
            <span>句长</span>
            <select v-model="draft.sentenceLen" class="input">
              <option value="">未定</option>
              <option value="极短">极短（≤4 字）</option>
              <option value="短">短（5–8 字）</option>
              <option value="中">中（9–16 字）</option>
              <option value="长">长（≥17 字）</option>
            </select>
          </label>
          <label class="field"><span>称呼他人（每行一条）</span><textarea v-model="draft.callOthers" class="input" rows="2" /></label>
          <label class="field"><span>句末习惯</span><input v-model="draft.sentenceEnd" class="input" placeholder="直陈收尾 / 常用「呢」「吧」" /></label>
          <label class="field"><span>口头禅（每行一条）</span><textarea v-model="draft.catchphrases" class="input" rows="2" /></label>
          <label class="field"><span>禁用词（每行一条）</span><textarea v-model="draft.banned" class="input" rows="2" /></label>
          <label class="field wide"><span>语气底色</span><input v-model="draft.tone" class="input" placeholder="冷硬 / 戏谑 / 温吞" /></label>
        </div>

        <h3>量级</h3>
        <div class="grid3">
          <label class="field"><span>量级</span><input v-model="draft.scale" class="input" /></label>
          <label class="field"><span>出手规模</span><input v-model="draft.scaleAction" class="input" /></label>
          <label class="field"><span>压制</span><input v-model="draft.suppression" class="input" /></label>
        </div>

        <h3>关系</h3>
        <p class="faint hint">每行一条：对象 | 称呼与态度（含边界）</p>
        <textarea v-model="draft.relations" class="input" rows="3" />

        <div class="actions">
          <button class="btn primary" type="button" :disabled="saving" @click="saveCard">
            {{ saving ? "保存中…" : "保存角色卡" }}
          </button>
          <button class="btn" type="button" :disabled="saving" @click="closeEditor">取消</button>
        </div>
        <div v-if="saveError" class="notice err">{{ saveError }}</div>
      </div>

      <!-- 列表 -->
      <div v-if="!cards.length" class="card empty">
        还没有角色卡。点右上「新建角色」，或在工作台建立剧情圣经后回来补全。
      </div>
      <div v-else class="card-grid">
        <article v-for="c in cards" :key="c.name" class="tpl-card">
          <div class="tpl-card-head">
            <div>
              <b>{{ c.name }}</b>
              <span v-if="c.role" class="tag">{{ c.role }}</span>
              <span v-if="c.card_source" class="tag" :class="c.card_source === 'user' ? 'ok' : ''">
                {{ c.card_source === "user" ? "手写" : c.card_source }}
              </span>
            </div>
            <div class="tpl-card-actions">
              <button class="link-btn" type="button" @click="openEditor(c)">编辑</button>
            </div>
          </div>
          <p v-if="c.motivation" class="faint">{{ c.motivation }}</p>
          <template v-if="hasCard(c.card)">
            <p v-if="identityLine(c.card)" class="line"><span class="k">身份</span>{{ identityLine(c.card) }}</p>
            <p v-if="abilityLine(c.card)" class="line"><span class="k">能力</span>{{ abilityLine(c.card) }}</p>
            <div v-if="c.card.能力?.武器?.length || c.card.能力?.功法?.length || c.card.能力?.能力?.length" class="items">
              <p v-for="(it, i) in [...(c.card.能力?.武器 ?? []), ...(c.card.能力?.功法 ?? []), ...(c.card.能力?.能力 ?? [])]" :key="`ab-${i}`" class="faint">
                · {{ itemText(it) }}
              </p>
            </div>
            <p v-if="voiceLine(c.card)" class="line"><span class="k">声纹</span>{{ voiceLine(c.card) }}</p>
            <p v-if="relationLine(c.card)" class="line"><span class="k">关系</span>{{ relationLine(c.card) }}</p>
          </template>
          <p v-else class="faint">还没有卡内容，点「编辑」或让 AI 补全。</p>
        </article>
      </div>
    </template>
  </section>
</template>

<style scoped>
.cards-page { display: flex; flex-direction: column; gap: var(--gap); }
.page-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--gap); flex-wrap: wrap;
}
.page-head h1 { margin: 2px 0 4px; font-family: var(--font-display); font-size: var(--fs-xl); }
.kicker { margin: 0; font-size: var(--fs-2xs); letter-spacing: 0.08em; }
.head-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-xs); }
.grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--gap); }
.grid3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--gap); }
.wide { grid-column: 1 / -1; }
.actions { display: flex; align-items: center; gap: 10px; margin: 10px 0; flex-wrap: wrap; }
.hint { margin: 0 0 4px; font-size: var(--fs-2xs); }
.editor h3 { margin: 14px 0 6px; font-size: var(--fs-sm); }
.editor-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.batch-list { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
.batch-item {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 8px; border: 1px solid var(--border); border-radius: var(--radius-md);
}
.batch-item p { margin: 2px 0; font-size: var(--fs-2xs); }
.empty { padding: 16px; color: var(--fg-faint); font-size: var(--fs-sm); }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: var(--gap); }
.tpl-card { border: 1px solid var(--border); border-radius: var(--radius-md); padding: 12px 14px; }
.tpl-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
.tpl-card-head .tag { margin-left: 6px; }
.tpl-card-actions { display: flex; gap: 6px; }
.line { margin: 4px 0; font-size: var(--fs-xs); line-height: 1.7; }
.line .k { display: inline-block; min-width: 34px; color: var(--fg-faint); }
.items { margin: 2px 0 4px 8px; }
.items p { margin: 1px 0; font-size: var(--fs-2xs); }
.tag.ok { color: var(--ok); border-color: var(--ok); }
@media (max-width: 900px) {
  .grid2, .grid3 { grid-template-columns: 1fr; }
}
</style>
