/** 书维度状态条与角色章号展示的类型声明（实现见 kgChapter.js）。 */

export type BookScopeTone = "idle" | "ok" | "warn";

export interface BookScopeTable {
  total?: number;
  in_scope?: number;
  project_level_null?: number;
  other_books_excluded?: number;
}

export interface BookScopeEntityTable extends BookScopeTable {
  /** 章号未知（first/last 任一为空）的实体数。 */
  chapter_unknown?: number;
  /** 现行区间与 mention 聚合不一致的实体数（只计数，不覆盖）。 */
  chapter_range_drift?: number;
}

export interface BookScope {
  project_id?: number;
  book?: {
    continuation_novel_id?: number | null;
    active_novel_id?: number | null;
    resolved?: number | null;
    scope?: (number | null)[];
  };
  entities?: BookScopeEntityTable;
  relations?: BookScopeTable;
  foreshadows?: BookScopeTable;
  events?: BookScopeTable;
  mentions?: BookScopeTable;
}

export interface BookScopeStatusView {
  tone: BookScopeTone;
  bookLabel: string;
  unknown: number;
  drift: number;
  excluded: number;
  unknownText: string;
  driftText: string;
  excludedText: string;
  detail: string;
}

export interface ChapterTag {
  text: string;
  unknown: boolean;
}

export const CHAPTER_UNKNOWN: string;

export function buildBookScopeStatus(
  scope: BookScope | null | undefined,
): BookScopeStatusView;

export function chapterTag(chapters: string | null | undefined): ChapterTag;
