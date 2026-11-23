/**
 * 助手本机对话历史的类型与纯函数。
 *
 * 从 `AssistantWorkspace.vue` 抽出：这些逻辑只跟 localStorage 形态、
 * 会话标题/预览格式有关，不持有组件状态。留在 SFC 里的只有「什么时候存、
 * 什么时候恢复、切换会话」这类需要 turns/wizard 状态的编排。
 */
import type {
  AssistantEvidence,
  AssistantPendingAction,
  AssistantThinking,
  AssistantWebSearchMeta,
  AssistantWebSource,
} from "@/api/types";

/** 最近 30 轮（一问一答算一轮）。 */
export const HISTORY_ROUNDS = 30;
export const HISTORY_MAX_TURNS = HISTORY_ROUNDS * 2;
/** 每个项目最多保留多少个本机会话（超出丢最旧的）。 */
export const MAX_SESSIONS = 30;

export interface StoredTurn {
  role: "user" | "assistant";
  /** 用户原话。 */
  text: string;
  /** 助手回答（引导/通知类消息的正文也落到这里）。 */
  answer: string;
  error: string | null;
  confidence: number;
  agentRunId: number;
  attachments?: { filename: string }[];
  /** 系统追加的过程消息（生成后追问）：恢复后仍让计划卡跳过它。 */
  synthetic?: boolean;
  evidence?: AssistantEvidence[];
  /** 本轮读取步骤：刷新后仍能看到「读了什么」，不再只剩证据反推。 */
  steps?: { name: string; meta?: string; detail?: string }[];
  /** 推理留痕（字数 / 耗时），刷新后保留「思考了 N 秒」。 */
  thinking?: AssistantThinking | null;
  /** 联网检索元信息（含清洗掉的低质条数），刷新后仍能解释来源数量。 */
  webSearch?: AssistantWebSearchMeta | null;
  /** 联网来源：刷新后「联网来源」小窗口仍能看到，不再只剩一句带 [1] 的回答。 */
  webSources?: AssistantWebSource[];
  /**
   * 写作卡片（只存已完成/失败的生成卡）。
   *
   * 不存 pending / running：刷新后那些动作已经不在跑，恢复成可点按钮
   * 只会制造一个必然失败或重复生成的假按钮。卡片只保留「点开看正文」
   * 这一件事，正文本身仍在 generations 表里。
   */
  cards?: StoredCard[];
}

/** 刷新后仍保留的写作卡片（无执行轨迹、无实时进度）。 */
export interface StoredCard {
  action: AssistantPendingAction;
  state: "confirmed" | "failed";
  notice: string;
  segmentNo?: number | null;
  preview?: string;
}

/** 一条本机会话：标题 + 时间 + 已落盘的问答轮次。 */
export interface ChatSession {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  turns: StoredTurn[];
  /** 本会话最后停留的滚动位置（像素）；恢复会话时回到这里而不是强行滚底。 */
  scrollTop?: number;
}

export function historyKeyFor(projectId: number | string): string {
  return `assistant:chat:v1:${projectId}`;
}

export function sessionsKeyFor(projectId: number | string): string {
  return `assistant:sessions:v1:${projectId}`;
}

export function activeSessionKeyFor(projectId: number | string): string {
  return `assistant:active-session:v1:${projectId}`;
}

export function newSessionId(): string {
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

/** 会话标题取第一句用户原话（不做模型总结，避免标题与内容漂移）。 */
export function sessionTitleFromTurns(list: StoredTurn[]): string {
  const first = list.find((t) => t.role === "user" && t.text.trim());
  if (!first) return "新对话";
  const text = first.text.trim().replace(/\s+/g, " ");
  return text.length > 18 ? text.slice(0, 18) + "…" : text;
}

export function sessionPreview(session: ChatSession): string {
  const last = [...session.turns]
    .reverse()
    .find((t) => t.role === "assistant" && (t.answer || "").trim());
  if (!last) return "还没有消息";
  const text = last.answer
    .replace(/[#>*`|_-]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > 36 ? text.slice(0, 36) + "…" : text;
}

export function sessionTimeLabel(session: ChatSession): string {
  const diff = Date.now() - session.updatedAt;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  const d = new Date(session.updatedAt);
  if (d.toDateString() === new Date().toDateString()) {
    return `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
}

/** 证据只留开头几段：足够认出「当时引了什么」，又不至于撑爆 5MB 配额。 */
export function trimEvidence(list: AssistantEvidence[]): AssistantEvidence[] {
  return list.slice(0, 6).map((ev) => ({
    ...ev,
    snippet: (ev.snippet ?? "").slice(0, 400),
  }));
}

export function readStoredSessions(projectId: number | string): ChatSession[] {
  try {
    const raw = localStorage.getItem(sessionsKeyFor(projectId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (s): s is ChatSession =>
        !!s &&
        typeof s === "object" &&
        typeof (s as ChatSession).id === "string" &&
        Array.isArray((s as ChatSession).turns),
    );
  } catch {
    return [];
  }
}

/** 旧版单会话记录 → 一条会话（迁移后仍读旧键，不做破坏性删除）。 */
export function migrateLegacyHistory(
  projectId: number | string,
): ChatSession | null {
  try {
    const raw = localStorage.getItem(historyKeyFor(projectId));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    const turns = parsed.filter(
      (t): t is StoredTurn => !!t && typeof t === "object",
    );
    if (!turns.length) return null;
    const now = Date.now();
    return {
      id: newSessionId(),
      title: sessionTitleFromTurns(turns),
      createdAt: now,
      updatedAt: now,
      turns,
    };
  } catch {
    return null;
  }
}
