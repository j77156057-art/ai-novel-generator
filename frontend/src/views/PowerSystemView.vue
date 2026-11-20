<script setup lang="ts">
defineOptions({ name: "PowerSystemView" });
/**
 * 力量体系页（/projects/:id/power）。
 *
 * 两块内容：
 * 1. 修炼体系 + 档位分层（分层群像）：路人只记档位，主角才逐个记境界；
 * 2. 角色修炼档案：当前境界 / 天赋 / 瓶颈 / 路线 / 代价，挂到角色卡的 `card.修炼`。
 *
 * 纪律：AI 生成只出草稿，不落库；保存是显式动作。
 */
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { api } from "@/api/client";
import ProjectNav from "@/components/ProjectNav.vue";
import type {
  CharacterCardItem,
  CultivationProfile,
  PowerSystemDef,
  PowerTier,
  ProjectOut,
} from "@/api/types";

const route = useRoute();
const projectId = computed(() => Number(route.params.id));

const project = ref<ProjectOut | null>(null);
const loading = ref(true);
const loadError = ref("");
const flash = ref("");

const tab = ref<"system" | "cultivation">("system");

// —— 体系 / 档位 ——
const systems = ref<PowerSystemDef[]>([]);
const tiers = ref<PowerTier[]>([]);
const systemsJson = ref("[]");
const tiersJson = ref("[]");
const note = ref("");
const systemType = ref("");
const generating = ref(false);
const genError = ref("");
const genNote = ref("");
const saving = ref(false);
const saveError = ref("");
const saveNote = ref("");

// —— 角色修炼 ——
const characters = ref<CharacterCardItem[]>([]);
const selectedName = ref("");
const profile = ref({
  当前境界: "", 当前小境界: "", 档位: "", 天赋: "",
  瓶颈: "", 目标境界: "", 代价: "", 来历: "",
});
const routeText = ref("");
const planNote = ref("");
const planning = ref(false);
const cultivationSaving = ref(false);
const cultivationError = ref("");
const cultivationNote = ref("");
const situation = ref("");

function textOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function syncJson(): void {
  systemsJson.value = JSON.stringify(systems.value, null, 2);
  tiersJson.value = JSON.stringify(tiers.value, null, 2);
}

function parseArray<T>(text: string, label: string): T[] {
  let value: unknown;
  try {
    value = JSON.parse(text || "[]");
  } catch (err) {
    throw new Error(`${label} JSON 解析失败：${textOf(err)}`);
  }
  if (!Array.isArray(value)) throw new Error(`${label} 必须是数组。`);
  return value as T[];
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    const [proj, power, cards] = await Promise.all([
      api.getProject(projectId.value),
      api.getPowerSystem(projectId.value),
      api.listCharacterCards(projectId.value),
    ]);
    project.value = proj;
    systems.value = power.power_system?.体系 ?? [];
    tiers.value = power.tiers ?? [];
    characters.value = cards.characters ?? [];
    syncJson();
    if (characters.value.length) {
      selectedName.value = characters.value[0].name;
      onCharChange();
    }
  } catch (err) {
    loadError.value = `加载失败：${textOf(err)}`;
  } finally {
    loading.value = false;
  }
}

async function generateSystem(): Promise<void> {
  generating.value = true;
  genError.value = "";
  genNote.value = "";
  try {
    const res = await api.generatePowerSystem(projectId.value, {
      genre: project.value?.genre ?? "",
      tone: project.value?.style ?? "",
      note: note.value.trim(),
      system_type: systemType.value,
    });
    if (!res.ok || !res.体系?.length) {
      genError.value = res.detail || "AI 没有给出可用的体系，可改要求后重试。";
      return;
    }
    systems.value = res.体系;
    tiers.value = [];
    syncJson();
    genNote.value = "已生成草稿：确认后点「保存体系与分层」才会落库（旧档位分层已清空，请重新生成）。";
  } catch (err) {
    genError.value = `生成失败：${textOf(err)}`;
  } finally {
    generating.value = false;
  }
}

async function generateTiers(): Promise<void> {
  if (!systems.value.length) {
    genError.value = "还没有体系：先「AI 生成体系」或在下方手写一套，再生成分层。";
    return;
  }
  generating.value = true;
  genError.value = "";
  genNote.value = "";
  try {
    const res = await api.generatePowerTiers(projectId.value, note.value.trim(), systems.value);
    if (!res.ok || !res.档位层?.length) {
      genError.value = res.detail || "AI 没有给出可用分层（可重试或改要求）。";
      return;
    }
    tiers.value = res.档位层;
    syncJson();
    genNote.value = "已按页面当前体系生成档位分层草稿：确认后点「保存体系与分层」才会落库。";
  } catch (err) {
    genError.value = `生成分层失败：${textOf(err)}`;
  } finally {
    generating.value = false;
  }
}

async function savePower(): Promise<void> {
  saving.value = true;
  saveError.value = "";
  saveNote.value = "";
  try {
    const parsedSystems = parseArray<PowerSystemDef>(systemsJson.value, "体系");
    const parsedTiers = parseArray<PowerTier>(tiersJson.value, "档位分层");
    await api.savePowerSystem(projectId.value, parsedSystems, parsedTiers);
    systems.value = parsedSystems;
    tiers.value = parsedTiers;
    saveNote.value = "已保存。生成正文时会按这套体系与档位写境界。";
    flash.value = saveNote.value;
  } catch (err) {
    saveError.value = `保存失败：${textOf(err)}`;
  } finally {
    saving.value = false;
  }
}

function onCharChange(): void {
  const item = characters.value.find((c) => c.name === selectedName.value);
  const p = (item?.card?.修炼 ?? {}) as CultivationProfile;
  profile.value = {
    当前境界: p.当前境界 ?? "",
    当前小境界: p.当前小境界 ?? "",
    档位: p.档位 ?? "",
    天赋: p.天赋 ?? "",
    瓶颈: p.瓶颈 ?? "",
    目标境界: p.目标境界 ?? "",
    代价: p.代价 ?? "",
    来历: p.来历 ?? "",
  };
  routeText.value = (p.路线 ?? []).join("\n");
  situation.value = "";
  cultivationError.value = "";
  cultivationNote.value = "";
}

async function planRoute(): Promise<void> {
  if (!selectedName.value) return;
  planning.value = true;
  cultivationError.value = "";
  cultivationNote.value = "";
  try {
    const draft: CultivationProfile = {
      ...profile.value,
      路线: routeText.value.split("\n").map((s) => s.trim()).filter(Boolean),
    };
    const res = await api.planCultivation(
      projectId.value,
      selectedName.value,
      planNote.value.trim(),
      draft,
      systems.value,
    );
    if (!res.ok || !res.路线) {
      cultivationError.value = res.detail || "AI 没有给出可用路线。";
      return;
    }
    const r = res.路线;
    profile.value = {
      当前境界: r.当前境界 ?? profile.value.当前境界,
      当前小境界: r.当前小境界 ?? profile.value.当前小境界,
      档位: r.档位 ?? profile.value.档位,
      天赋: r.天赋 ?? profile.value.天赋,
      瓶颈: r.瓶颈 ?? profile.value.瓶颈,
      目标境界: r.目标境界 ?? profile.value.目标境界,
      代价: r.代价 ?? profile.value.代价,
      来历: r.来历 ?? profile.value.来历,
    };
    if (r.路线?.length) routeText.value = r.路线.join("\n");
    situation.value = res.处境 ?? "";
    cultivationNote.value = "路线是草稿（起点与目标由 AI 建议）：确认后点「保存修炼档案」。";
  } catch (err) {
    cultivationError.value = `规划失败：${textOf(err)}`;
  } finally {
    planning.value = false;
  }
}

async function saveCultivation(): Promise<void> {
  if (!selectedName.value) return;
  cultivationSaving.value = true;
  cultivationError.value = "";
  cultivationNote.value = "";
  try {
    const payload: CultivationProfile = {
      当前境界: profile.value.当前境界.trim(),
      当前小境界: profile.value.当前小境界.trim(),
      档位: profile.value.档位.trim(),
      天赋: profile.value.天赋.trim(),
      瓶颈: profile.value.瓶颈.trim(),
      目标境界: profile.value.目标境界.trim(),
      代价: profile.value.代价.trim(),
      来历: profile.value.来历.trim(),
      路线: routeText.value.split("\n").map((s) => s.trim()).filter(Boolean),
    };
    await api.saveCultivation(projectId.value, selectedName.value, payload);
    cultivationNote.value = `已保存「${selectedName.value}」的修炼档案。`;
    flash.value = cultivationNote.value;
    const cards = await api.listCharacterCards(projectId.value);
    characters.value = cards.characters ?? [];
  } catch (err) {
    cultivationError.value = `保存失败：${textOf(err)}`;
  } finally {
    cultivationSaving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section class="power-page">
    <ProjectNav :project-id="projectId" />
    <header class="page-head">
      <div>
        <p class="faint kicker">写作素材</p>
        <h1>力量体系</h1>
        <p class="faint">
          自定义境界阶梯 + 档位分层（路人只记档位）；角色修炼档案记录当前境界、
          天赋、瓶颈与路线。AI 只出草稿，确认后保存。
        </p>
      </div>
      <router-link class="btn" :to="`/projects/${projectId}`">返回工作台</router-link>
    </header>

    <div v-if="flash" class="notice ok">
      {{ flash }}
      <button class="link-btn" type="button" @click="flash = ''">关闭</button>
    </div>

    <div v-if="loading" class="card faint">加载体系与角色…</div>
    <div v-else-if="loadError" class="notice err" role="alert">
      {{ loadError }}
      <button class="link-btn" type="button" @click="load">重试</button>
    </div>

    <template v-else>
      <nav class="tabs">
        <button type="button" class="tab" :class="{ on: tab === 'system' }" @click="tab = 'system'">
          体系与分层
        </button>
        <button type="button" class="tab" :class="{ on: tab === 'cultivation' }" @click="tab = 'cultivation'">
          角色修炼
        </button>
      </nav>

      <!-- ========== 体系与分层 ========== -->
      <template v-if="tab === 'system'">
        <div class="card">
          <div class="row-actions">
            <label class="field grow">
              <span>体系类型（可选）</span>
              <select v-model="systemType" class="input">
                <option value="">让 AI 自己定</option>
                <option value="细分型">细分型</option>
                <option value="序列型">序列型</option>
                <option value="粗放型">粗放型</option>
              </select>
            </label>
            <label class="field grow">
              <span>额外要求（可选）</span>
              <input v-model="note" class="input" placeholder="例如：低武世界；境界别超过九个" />
            </label>
          </div>
          <div class="actions">
            <button class="btn" type="button" :disabled="generating" @click="generateSystem">
              {{ generating ? "生成中…" : "AI 生成体系" }}
            </button>
            <button class="btn" type="button" :disabled="generating" @click="generateTiers">
              生成档位分层
            </button>
            <button class="btn primary" type="button" :disabled="saving" @click="savePower">
              {{ saving ? "保存中…" : "保存体系与分层" }}
            </button>
          </div>
          <div v-if="genError" class="notice err">{{ genError }}</div>
          <div v-if="genNote" class="notice info">{{ genNote }}</div>
          <div v-if="saveError" class="notice err">{{ saveError }}</div>
          <div v-if="saveNote" class="notice info">{{ saveNote }}</div>
        </div>

        <div class="card">
          <div class="list-head">
            <h2>修炼体系</h2>
            <span class="faint">{{ systems.length }} 套</span>
          </div>
          <div v-if="!systems.length" class="empty">还没有体系。点上方「AI 生成体系」，或在下方高级编辑里手写。</div>
          <div v-for="(s, i) in systems" :key="`sys-${i}`" class="system">
            <div class="system-head">
              <b>{{ s.名称 }}</b>
              <span v-if="s.类型" class="tag">{{ s.类型 }}</span>
            </div>
            <p v-if="s.说明" class="faint">{{ s.说明 }}</p>
            <ol class="realms">
              <li v-for="(r, ri) in s.境界 ?? []" :key="`r-${ri}`">
                <b>{{ r.名称 }}</b>
                <span v-if="r.档位" class="tag">{{ r.档位 }}</span>
                <span v-if="r.说明" class="faint"> {{ r.说明 }}</span>
                <div v-if="r.小境界?.length" class="faint stages">
                  {{ r.小境界.map((x) => x.名称).filter(Boolean).join(" → ") }}
                </div>
              </li>
            </ol>
          </div>
          <details class="advanced">
            <summary>高级编辑（体系 JSON）</summary>
            <textarea v-model="systemsJson" class="input mono" rows="10" spellcheck="false" />
          </details>
        </div>

        <div class="card">
          <div class="list-head">
            <h2>档位分层</h2>
            <span class="faint">{{ tiers.length }} 层</span>
          </div>
          <p class="faint hint">分层群像：路人只记档位（如「筑基期散修」），主角与主要配角才逐个记境界。</p>
          <div v-if="!tiers.length" class="empty">还没有档位分层。保存体系后点「生成档位分层」。</div>
          <ul v-else class="tiers">
            <li v-for="(t, i) in tiers" :key="`t-${i}`">
              <b>{{ t.名称 }}</b>
              <span v-if="t.档位" class="tag">{{ t.档位 }}</span>
              <span v-if="t.人数" class="faint"> · {{ t.人数 }}</span>
              <span v-if="t.特征" class="faint"> · {{ t.特征 }}</span>
            </li>
          </ul>
          <details class="advanced">
            <summary>高级编辑（档位 JSON）</summary>
            <textarea v-model="tiersJson" class="input mono" rows="8" spellcheck="false" />
          </details>
        </div>
      </template>

      <!-- ========== 角色修炼 ========== -->
      <template v-else>
        <div class="card">
          <div class="row-actions">
            <label class="field grow">
              <span>角色</span>
              <select v-model="selectedName" class="input" @change="onCharChange">
                <option v-for="c in characters" :key="c.name" :value="c.name">{{ c.name }}</option>
              </select>
            </label>
            <label class="field grow">
              <span>规划要求（可选）</span>
              <input v-model="planNote" class="input" placeholder="例如：三年内到金丹；中途掉一次境" />
            </label>
          </div>
          <p v-if="!characters.length" class="notice warn">
            还没有角色。先去「角色卡」页建角色。
            <router-link class="link-btn" :to="`/projects/${projectId}/cards`">去角色卡</router-link>
          </p>
          <div class="actions">
            <button class="btn" type="button" :disabled="planning || !selectedName" @click="planRoute">
              {{ planning ? "规划中…" : "AI 规划路线" }}
            </button>
            <button class="btn primary" type="button" :disabled="cultivationSaving || !selectedName" @click="saveCultivation">
              {{ cultivationSaving ? "保存中…" : "保存修炼档案" }}
            </button>
          </div>
          <div v-if="cultivationError" class="notice err">{{ cultivationError }}</div>
          <div v-if="cultivationNote" class="notice info">{{ cultivationNote }}</div>
          <div v-if="situation" class="notice info">处境：{{ situation }}</div>
        </div>

        <div class="card" v-if="selectedName">
          <div class="grid2">
            <label class="field"><span>当前境界</span><input v-model="profile.当前境界" class="input" /></label>
            <label class="field"><span>当前小境界</span><input v-model="profile.当前小境界" class="input" /></label>
            <label class="field"><span>档位</span><input v-model="profile.档位" class="input" /></label>
            <label class="field"><span>目标境界</span><input v-model="profile.目标境界" class="input" /></label>
            <label class="field"><span>天赋</span><input v-model="profile.天赋" class="input" /></label>
            <label class="field"><span>瓶颈</span><input v-model="profile.瓶颈" class="input" /></label>
            <label class="field"><span>代价</span><input v-model="profile.代价" class="input" placeholder="例如：燃烧寿命" /></label>
            <label class="field"><span>来历</span><input v-model="profile.来历" class="input" /></label>
          </div>
          <label class="field route">
            <span>路线（每行一步）</span>
            <textarea v-model="routeText" class="input" rows="5" />
          </label>
        </div>
      </template>
    </template>
  </section>
</template>

<style scoped>
.power-page { display: flex; flex-direction: column; gap: var(--gap); }
.page-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--gap); flex-wrap: wrap;
}
.page-head h1 { margin: 2px 0 4px; font-family: var(--font-display); font-size: var(--fs-xl); }
.kicker { margin: 0; font-size: var(--fs-2xs); letter-spacing: 0.08em; }
.tabs { display: flex; gap: 6px; }
.tab {
  padding: 6px 12px; border: 1px solid var(--border); border-radius: var(--radius-md);
  background: transparent; color: var(--fg-muted); cursor: pointer;
}
.tab.on { background: var(--accent-soft); border-color: var(--accent-line); color: var(--fg); }
.row-actions { display: flex; gap: var(--gap); flex-wrap: wrap; align-items: flex-end; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-xs); }
.grow { flex: 1; min-width: 220px; }
.actions { display: flex; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
.list-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.list-head h2 { margin: 0; }
.empty { padding: 12px 0; color: var(--fg-faint); font-size: var(--fs-sm); }
.system { padding: 8px 0; border-top: 1px solid var(--border); }
.system:first-of-type { border-top: 0; }
.system-head { display: flex; align-items: center; gap: 8px; margin-bottom: 2px; }
.realms { margin: 4px 0 0; padding-left: 20px; font-size: var(--fs-xs); line-height: 1.8; }
.realms .tag { margin: 0 4px; }
.stages { margin-top: 2px; }
.tiers { list-style: none; margin: 0; padding: 0; font-size: var(--fs-sm); line-height: 1.9; }
.advanced { margin-top: 10px; }
.advanced summary { cursor: pointer; font-size: var(--fs-xs); color: var(--fg-muted); }
.advanced textarea { margin-top: 6px; }
.mono { font-family: var(--font-mono); font-size: var(--fs-2xs); }
.hint { margin: 0 0 6px; font-size: var(--fs-2xs); }
.grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--gap); }
.route { margin-top: var(--gap); }
@media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; } }
</style>
