<script setup lang="ts">
import { computed, ref } from "vue";
import type { WizardCommitResult } from "@/api/types";

interface WizardCharacter {
  name: string;
  role: string;
  card: Record<string, unknown> | null;
}

const props = defineProps<{
  result: WizardCommitResult;
  characters: WizardCharacter[];
  /** 正在批量/单张生成卡片的 key（"all" / "missing" / 角色名）。 */
  busyName: string | null;
  cardSaving: boolean;
  cardDeleting: boolean;
  characterRemoving: boolean;
  cardEditError: string | null;
}>();

const emit = defineEmits<{
  (e: "regenerate", payload: { names: string[]; mode: string }): void;
  (e: "edit", character: WizardCharacter): void;
  (e: "delete-card", name: string): void;
  (e: "remove-character", name: string): void;
}>();

/** 删除/移除的二次确认是纯本地 UI：拆出来后由本组件自己管。 */
const cardDeleteName = ref<string | null>(null);
const characterRemoveName = ref<string | null>(null);

const cardCount = computed(() => props.characters.filter((c) => !!c.card).length);
const missingNames = computed(() =>
  props.characters.filter((c) => !c.card).map((c) => c.name),
);

/** 涉及该角色的关系条数：移除确认里先说清会连带清理多少条。 */
function relationshipCount(name: string): number {
  const bible = props.result.bible as Record<string, unknown> | null | undefined;
  const rels = bible && Array.isArray(bible.relationships) ? bible.relationships : [];
  return rels.filter((r) => {
    if (!r || typeof r !== "object") return false;
    const rel = r as Record<string, unknown>;
    return rel.a === name || rel.b === name;
  }).length;
}

function asText(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

function cardIdentityLine(card: Record<string, unknown>): string {
  const ident = card["身份"];
  if (!ident || typeof ident !== "object") return "";
  const obj = ident as Record<string, unknown>;
  return ["性别", "年龄", "身份职业"]
    .map((key) => asText(obj[key]))
    .filter(Boolean)
    .join(" · ");
}

function cardVoiceLine(card: Record<string, unknown>): string {
  const voice = card["声纹"];
  if (!voice || typeof voice !== "object") return "";
  const v = voice as Record<string, unknown>;
  const tics = Array.isArray(v["口头禅"])
    ? v["口头禅"].map(asText).filter(Boolean).slice(0, 2)
    : [];
  const parts = [
    v["自称"] ? `自称「${asText(v["自称"])}」` : "",
    v["句长"] ? `句长${asText(v["句长"])}` : "",
    asText(v["句末"]),
    tics.length ? `口头禅：${tics.map((x) => `「${x}」`).join("")}` : "",
  ];
  return parts.filter(Boolean).join(" · ");
}

function confirmDelete(name: string): void {
  emit("delete-card", name);
  cardDeleteName.value = null;
}

function confirmRemove(name: string): void {
  emit("remove-character", name);
  characterRemoveName.value = null;
}
</script>

<template>
  <div class="wz-cards">
    <div class="row tight">
      <span class="tag mono">角色卡 {{ cardCount }} 张</span>
      <span v-if="props.result.character_cards?.collisions" class="tag">
        声纹撞车 {{ props.result.character_cards.collisions }}
      </span>
      <button
        v-if="missingNames.length"
        class="link-btn"
        :disabled="!!props.busyName"
        @click="emit('regenerate', { names: missingNames, mode: 'missing' })"
      >
        {{ props.busyName === "missing"
          ? "补齐中…"
          : `补齐缺失（${missingNames.length}）` }}
      </button>
      <button
        class="link-btn"
        :disabled="!!props.busyName"
        @click="emit('regenerate', { names: [], mode: 'all' })"
      >
        {{ props.busyName === "all" ? "生成中…" : "重生成全部" }}
      </button>
    </div>

    <ul class="wz-card-list">
      <li
        v-for="c in props.characters"
        :key="c.name"
        class="wz-card-item"
      >
        <div class="wz-card-head">
          <strong>{{ c.name }}</strong>
          <span class="tag" :class="{ mono: !!c.card }">
            {{ c.card ? "已生成" : "未生成" }}
          </span>
          <span v-if="c.card && c.card['来源'] === 'user'" class="tag">手写</span>
          <div class="wz-card-actions">
            <button
              v-if="c.card"
              class="link-btn"
              :disabled="props.cardSaving"
              @click="emit('edit', c)"
            >
              编辑
            </button>
            <button
              v-if="c.card"
              class="link-btn wz-danger"
              :disabled="props.cardDeleting"
              @click="cardDeleteName = c.name"
            >
              删除卡
            </button>
            <button
              class="link-btn wz-danger"
              :disabled="props.characterRemoving"
              title="从剧情圣经整体移除，不只是删卡"
              @click="characterRemoveName = c.name"
            >
              移除角色
            </button>
            <button
              class="link-btn"
              :disabled="!!props.busyName || props.cardSaving"
              @click="emit('regenerate', { names: [c.name], mode: c.name })"
            >
              {{ props.busyName === c.name
                ? "生成中…"
                : c.card ? "重生成" : "生成" }}
            </button>
          </div>
        </div>
        <div v-if="c.card" class="wz-card-meta">{{ cardIdentityLine(c.card) }}</div>
        <div v-if="c.card" class="wz-card-meta">{{ cardVoiceLine(c.card) }}</div>

        <div v-if="cardDeleteName === c.name" class="wz-card-confirm">
          <span>只删除角色卡，角色仍保留在剧情圣经里。</span>
          <button
            class="btn ghost sm wz-danger"
            :disabled="props.cardDeleting"
            @click="confirmDelete(c.name)"
          >
            {{ props.cardDeleting ? "删除中…" : "确认删除" }}
          </button>
          <button class="btn ghost sm" @click="cardDeleteName = null">取消</button>
        </div>

        <div v-if="characterRemoveName === c.name" class="wz-card-confirm">
          <span>
            会从剧情圣经整体移除「{{ c.name }}」<template
              v-if="relationshipCount(c.name)"
            >，并清理 {{ relationshipCount(c.name) }} 条相关关系</template>。
          </span>
          <button
            class="btn ghost sm wz-danger"
            :disabled="props.characterRemoving"
            @click="confirmRemove(c.name)"
          >
            {{ props.characterRemoving ? "移除中…" : "确认移除" }}
          </button>
          <button class="btn ghost sm" @click="characterRemoveName = null">取消</button>
          <span v-if="props.cardEditError" class="notice err">{{ props.cardEditError }}</span>
          <span class="wz-card-confirm-hint">
            正文与已抽好的图谱保留，只是以后写文不再把 TA 当锁定角色；此操作不可撤销。
          </span>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.wz-cards {
  margin-top: var(--gap-sm);
  display: flex;
  flex-direction: column;
  gap: var(--gap-sm);
}
.wz-card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.wz-card-item {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  background: var(--bg-sunken);
}
.wz-card-head {
  display: flex;
  align-items: center;
  gap: var(--gap-sm);
}
.wz-card-head strong {
  font-size: var(--fs-sm);
}
.wz-card-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--gap-sm);
}
.wz-card-meta {
  margin-top: 4px;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
  line-height: 1.5;
}
.wz-danger {
  color: var(--danger);
}
.wz-card-confirm {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: var(--gap-sm);
  flex-wrap: wrap;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}
/* 确认块的第二行说明：整行铺开，不和按钮挤在一行。 */
.wz-card-confirm-hint {
  flex-basis: 100%;
  line-height: 1.5;
}
</style>
