// 主题选择器：全站共享。拍板设计方向后，改 ACTIVE_THEME 即可换默认；
// tokens.css 已备好 editorial / brutalist / soft / ink 四套。
// 用户手动切换（暗色工作台 ↔ 纸色）会写入 localStorage，下次进入沿用。
import { readonly, ref } from "vue";

export type ThemeName = "editorial" | "brutalist" | "soft" | "ink";

export const ACTIVE_THEME: ThemeName = "editorial";

const STORAGE_KEY = "app:theme";
const VALID: ThemeName[] = ["editorial", "brutalist", "soft", "ink"];

const current = ref<ThemeName>(ACTIVE_THEME);

/** 读用户上次的选择；没有/非法时回落到默认主题。 */
export function readTheme(): ThemeName {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && (VALID as string[]).includes(saved)) return saved as ThemeName;
    // 兼容上一版「对话页局部暗色」的偏好：用户当时选了 ink，就迁移成全站 ink。
    const legacy = localStorage.getItem("assistant:surface");
    if (legacy === "ink") return "ink";
  } catch {
    /* 隐私模式 / 本地存储不可用：用默认主题 */
  }
  return ACTIVE_THEME;
}

/** 应用主题（默认读取用户上次选择）。main.ts 在挂载前调用一次。 */
export function applyTheme(theme: ThemeName = readTheme()): void {
  current.value = theme;
  document.documentElement.dataset.theme = theme;
}

/** 切换并持久化主题。 */
export function setTheme(theme: ThemeName): void {
  applyTheme(theme);
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* 写不进去只影响下次默认值 */
  }
}

/** 全站唯一的暗色开关：在「纸色」与「暗色工作台」之间切换。 */
export function toggleInkTheme(): ThemeName {
  const next: ThemeName = current.value === "ink" ? "editorial" : "ink";
  setTheme(next);
  return next;
}

/** 组件里只读订阅当前主题（切换由 toggleInkTheme / setTheme 完成）。 */
export function useAppTheme() {
  return { theme: readonly(current), setTheme, toggleInkTheme };
}
