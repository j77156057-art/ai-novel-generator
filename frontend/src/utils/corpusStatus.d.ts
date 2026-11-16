/** 语料摄取窗口状态条的类型声明（实现见 corpusStatus.js）。 */

export type CorpusStatusTone = "idle" | "ok" | "warn" | "error";

export interface CorpusWindowSummary {
  ingested?: boolean;
  total?: number;
  failed?: number;
  running?: number;
  pending?: number;
  success?: number;
  failed_reasons?: {
    parse_failed?: number;
    channel_unavailable?: number;
    other?: number;
  };
  suspected_local_fallback?: number;
}

export interface CorpusStatusView {
  tone: CorpusStatusTone;
  text: string;
  detail: string;
  failureCount: number;
}

export function buildCorpusStatus(
  summary: CorpusWindowSummary | null | undefined,
): CorpusStatusView;
