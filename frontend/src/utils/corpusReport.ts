/**
 * 界面共用的小工具：章标题去重。
 *
 * ## 为什么需要
 *
 * 盗版站的 txt 常把同一章的标题**连贴两遍**（同一行出现两次）。
 * 语料体检报告里的 `chapter_titles` 是如实记录，于是会出现
 * 「第1047章 符合路子、第1047章 符合路子」这样的重复。
 *
 * 章数（`chapter_count`）保持原样 —— 那是真实的；只把**标题**去重，
 * 因为界面上摆两条一模一样的标题，看起来像故障而不是事实。
 */

/** 按原顺序去重（保留首次出现的位置）。 */
export function distinctChapterTitles(titles: string[] | null | undefined): string[] {
  if (!titles?.length) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const t of titles) {
    if (seen.has(t)) continue;
    seen.add(t);
    out.push(t);
  }
  return out;
}
