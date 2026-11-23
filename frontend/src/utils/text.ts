/** 轻量文本解析助手。 */

/**
 * 逗号分隔的模型链 → 去空白、去空项的数组。
 *
 * 设置页的「候选模型链」在两个面板里都要解析（KG 抽取 / 角色模型），
 * 口径必须一致：中文逗号也算分隔符，空串不产出条目。
 */
export function splitCsv(text: string): string[] {
  return text
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean);
}
