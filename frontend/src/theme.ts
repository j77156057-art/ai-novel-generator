// 主题选择器：拍板设计方向后，改这一个常量即可全站换肤（tokens.css 已备好三套）。
// 可选值："editorial" | "brutalist" | "soft"
export type ThemeName = "editorial" | "brutalist" | "soft";

export const ACTIVE_THEME: ThemeName = "editorial";

export function applyTheme(theme: ThemeName = ACTIVE_THEME) {
  document.documentElement.dataset.theme = theme;
}
