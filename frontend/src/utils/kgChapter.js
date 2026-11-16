/**
 * 书维度状态条与角色章号展示（纯函数，node --test 锁口径）。
 *
 * 两条必须**分开说**的事实：
 * 1. 章号未知：first/last 至少一列为空 —— 不知道角色出现在哪几章；
 * 2. 区间漂移：现行区间与当前 mention 聚合不一致 —— 有值，但按拍板不覆盖，
 *    因为它和「mention 底数整窗重写会丢信息」是同一件事。
 *
 * 合成一句「部分数据异常」会让用户无法判断该看哪一边，也会让漂移被静默。
 */

/** 与后端 `format_chapter_range` 的未知值保持一致。 */
export const CHAPTER_UNKNOWN = "章号未知";

/**
 * @param {import("./kgChapter").BookScope | null | undefined} scope
 * @returns {import("./kgChapter").BookScopeStatusView}
 */
export function buildBookScopeStatus(scope) {
  if (!scope || !scope.entities) {
    return {
      tone: "idle",
      bookLabel: "书维度未读取",
      unknown: 0,
      drift: 0,
      excluded: 0,
      unknownText: "章号未知 —",
      driftText: "区间漂移 —",
      excludedText: "",
      detail: "book-scope 没读到，不等于「没有异常」",
    };
  }

  const entities = scope.entities || {};
  const unknown = entities.chapter_unknown || 0;
  const drift = entities.chapter_range_drift || 0;
  const excluded = entities.other_books_excluded || 0;
  const book = scope.book || {};
  const resolved = book.resolved;
  const bookLabel =
    resolved === null || resolved === undefined
      ? "未指定本书（只读项目级）"
      : `当前书 #${resolved}`;

  const details = [];
  if (excluded > 0) {
    details.push(`别书 ${excluded} 个实体在读侧不显示`);
  }
  if (drift > 0) {
    details.push("漂移不覆盖，明细见 reports/kg-chapter-range-drift-*.csv");
  }

  return {
    tone: drift > 0 || unknown > 0 ? "warn" : "ok",
    bookLabel,
    unknown,
    drift,
    excluded,
    unknownText: `章号未知 ${unknown}`,
    driftText: `区间漂移 ${drift}`,
    excludedText: excluded > 0 ? `别书已排除 ${excluded}` : "",
    detail: details.join(" · "),
  };
}

/**
 * 角色章号标签：空值一律显示「章号未知」，任何路径都不回落成第 1 章。
 *
 * @param {string | null | undefined} chapters
 * @returns {import("./kgChapter").ChapterTag}
 */
export function chapterTag(chapters) {
  const text = (chapters || "").trim() || CHAPTER_UNKNOWN;
  return { text, unknown: text === CHAPTER_UNKNOWN };
}
