// 极简 fetch 客户端：封装后端全部端点，统一错误解析。
// 走相对路径 /api，由 Vite dev proxy 转发到 FastAPI :8000。

import type {
  AlertsResponse,
  CharacterDetail,
  ChunkOut,
  ExpertQuestions,
  GenerateLoopResult,
  GeneratePlan,
  GenerateResult,
  GenerationOut,
  Graph,
  LlmConfig,
  LlmConfigIn,
  LlmTestResult,
  NovelOut,
  PlotBible,
  PremiseProposal,
  ProjectCreate,
  ProjectOut,
  ProjectPatch,
  Reminders,
  SearchHit,
  UploadResult,
} from "./types";

const BASE = "/api";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, init);
  const text = await res.text();
  const data = text ? safeJson(text) : null;
  if (!res.ok) {
    const detail =
      (data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : text) || `HTTP ${res.status}`;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  };
}

// ---------- 语料 ----------
export const api = {
  listNovels: () => request<NovelOut[]>("/novels"),

  uploadNovel: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<UploadResult>("/novels/upload", { method: "POST", body: form });
  },

  listChunks: (novelId: number) =>
    request<ChunkOut[]>(`/novels/${novelId}/chunks`),

  searchNovel: (novelId: number, q: string, k = 6) =>
    request<SearchHit[]>(
      `/novels/${novelId}/search?q=${encodeURIComponent(q)}&k=${k}`,
    ),

  // ---------- 项目 / 图谱 ----------
  listProjects: () => request<ProjectOut[]>("/projects"),

  getProject: (projectId: number) =>
    request<ProjectOut>(`/projects/${projectId}`),

  createProject: (body: ProjectCreate) =>
    request<ProjectOut>("/projects", jsonInit("POST", body)),

  patchProject: (projectId: number, body: ProjectPatch) =>
    request<ProjectOut>(`/projects/${projectId}`, jsonInit("PATCH", body)),

  buildKg: (projectId: number, novelId?: number | null) =>
    request<{ characters: number; plotholes: number; relationships: number }>(
      `/projects/${projectId}/build-kg${novelId ? `?novel_id=${novelId}` : ""}`,
      jsonInit("POST"),
    ),

  getKg: (
    projectId: number,
    type: "all" | "character" | "plothole" = "all",
    source?: string | null,
  ) =>
    request<Graph>(
      `/projects/${projectId}/kg?type=${type}${source ? `&source=${encodeURIComponent(source)}` : ""}`,
    ),

  getCharacter: (projectId: number, name: string) =>
    request<CharacterDetail>(
      `/projects/${projectId}/kg/character/${encodeURIComponent(name)}`,
    ),

  // source 与 getKg 保持对称：不传＝本次创作自己的图谱（生成默认），
  // 传 novel:{id} 才能看参考语料那一侧的提醒
  getReminders: (projectId: number, source?: string | null) =>
    request<Reminders>(
      `/projects/${projectId}/reminders${source ? `?source=${encodeURIComponent(source)}` : ""}`,
    ),

  // ---------- 向导（选语料 → 类型/风格 → 内容 → 构思 → 生成） ----------
  proposePremise: (projectId: number) =>
    request<PremiseProposal>(
      `/projects/${projectId}/propose-premise`,
      jsonInit("POST"),
    ),

  expertQuestions: (projectId: number) =>
    request<ExpertQuestions>(
      `/projects/${projectId}/expert-questions`,
      jsonInit("POST"),
    ),

  buildBible: (projectId: number, answers?: Record<string, string> | null) =>
    request<PlotBible>(
      `/projects/${projectId}/build-bible`,
      jsonInit("POST", answers ? { answers } : {}),
    ),

  saveCharacters: (projectId: number, body: {
    characters: { name: string; role: string; motivation?: string | null }[];
    relationships?: { a: string; b: string; relation: string }[];
  }) => request<{ characters: PlotBible["characters"]; relationships: PlotBible["relationships"] }>(
    `/projects/${projectId}/characters`, jsonInit("POST", body),
  ),

  confirmUpcoming: (projectId: number, confirmed: { name: string; role?: string; motivation?: string }[]) =>
    request<{ confirmed_upcoming: typeof confirmed }>(
      `/projects/${projectId}/upcoming-characters`, jsonInit("POST", { confirmed }),
    ),

  buildKgFromGenerations: (projectId: number) =>
    request<{ characters: number; plotholes: number; relationships: number; chunks: number }>(
      `/projects/${projectId}/build-kg-from-generations`,
      jsonInit("POST"),
    ),

  // ---------- 生成环 ----------
  generatePlan: (projectId: number) =>
    request<GeneratePlan>(
      "/generate-plan",
      jsonInit("POST", { project_id: projectId }),
    ),

  generate: (projectId: number, maxChars?: number | null) =>
    request<GenerateResult>(
      "/generate",
      jsonInit("POST", {
        project_id: projectId,
        max_chars: maxChars ?? null,
      }),
    ),

  // 流式续写：SSE 逐块回调 onChunk，结束回调 onDone。流结束时 Promise resolve。
  generateStream: (
    projectId: number,
    maxChars: number | null,
    onChunk: (text: string) => void,
    onDone: (meta: {
      segment_no: number;
      chars: number;
      summary: string;
      id: number;
    }) => void,
  ): Promise<void> =>
    new Promise<void>((resolve, reject) => {
      fetch(`${BASE}/generate-stream`, jsonInit("POST", {
        project_id: projectId,
        max_chars: maxChars ?? null,
      })).then(async (res) => {
        if (!res.ok) {
          reject(new ApiError(res.status, `生成失败 HTTP ${res.status}`));
          return;
        }
        const reader = res.body!.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        const pump = (): Promise<void> =>
          reader.read().then(({ done, value }) => {
            if (done) {
              resolve();
              return;
            }
            buf += decoder.decode(value, { stream: true });
            let idx: number;
            while ((idx = buf.indexOf("\n\n")) !== -1) {
              const frame = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              const line = frame.split("\n").find((l) => l.startsWith("data:"));
              if (!line) continue;
              const payload = line.slice(5).trim();
              try {
                const ev = JSON.parse(payload) as
                  | { type: "chunk"; text: string }
                  | {
                      type: "done";
                      segment_no: number;
                      chars: number;
                      summary: string;
                      id: number;
                    }
                  | { type: "error"; message: string };
                if (ev.type === "chunk") onChunk(ev.text);
                else if (ev.type === "done") onDone(ev);
                else if (ev.type === "error") reject(new ApiError(500, ev.message));
              } catch {
                /* 跳过不完整帧 */
              }
            }
            return pump();
          });
        pump().catch((e) => reject(e));
      }).catch((e) => reject(e));
    }),

  // 重生成单段：与 generateStream 同协议，额外带 segment_no
  regenerateStream: (
    projectId: number,
    segmentNo: number,
    maxChars: number | null,
    onChunk: (text: string) => void,
    onDone: (meta: {
      segment_no: number;
      chars: number;
      summary: string;
      id: number;
    }) => void,
  ): Promise<void> =>
    new Promise<void>((resolve, reject) => {
      fetch(`${BASE}/regenerate-stream`, jsonInit("POST", {
        project_id: projectId,
        segment_no: segmentNo,
        max_chars: maxChars ?? null,
      })).then(async (res) => {
        if (!res.ok) {
          reject(new ApiError(res.status, `重生成失败 HTTP ${res.status}`));
          return;
        }
        const reader = res.body!.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        const pump = (): Promise<void> =>
          reader.read().then(({ done, value }) => {
            if (done) {
              resolve();
              return;
            }
            buf += decoder.decode(value, { stream: true });
            let idx: number;
            while ((idx = buf.indexOf("\n\n")) !== -1) {
              const frame = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              const line = frame.split("\n").find((l) => l.startsWith("data:"));
              if (!line) continue;
              const payload = line.slice(5).trim();
              try {
                const ev = JSON.parse(payload) as
                  | { type: "chunk"; text: string }
                  | {
                      type: "done";
                      segment_no: number;
                      chars: number;
                      summary: string;
                      id: number;
                    }
                  | { type: "error"; message: string };
                if (ev.type === "chunk") onChunk(ev.text);
                else if (ev.type === "done") onDone(ev);
                else if (ev.type === "error") reject(new ApiError(500, ev.message));
              } catch {
                /* 跳过不完整帧 */
              }
            }
            return pump();
          });
        pump().catch((e) => reject(e));
      }).catch((e) => reject(e));
    }),

  generateLoop: (projectId: number, segments: number, maxChars?: number) =>
    request<GenerateLoopResult>(
      "/generate-loop",
      jsonInit("POST", {
        project_id: projectId,
        segments,
        max_chars: maxChars ?? null,
      }),
    ),

  listGenerations: (projectId: number) =>
    request<GenerationOut[]>(`/generations/${projectId}`),

  /** 规则哨兵的历史告警（落库的，按段号倒序）。只读，不触发重新判定。 */
  listAlerts: (projectId: number) =>
    request<AlertsResponse>(`/alerts/${projectId}`),

  // 导出为 txt：拿二进制流并触发浏览器下载
  exportTxt: async (projectId: number, filename: string) => {
    const res = await fetch(`${BASE}/projects/${projectId}/export-txt`);
    if (!res.ok) throw new ApiError(res.status, `导出失败 HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename.endsWith(".txt") ? filename : `${filename}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },

  // ---------- 模型设置（渠道 / key / 模型，持久化 + 热切换） ----------
  getLlmConfig: () => request<LlmConfig>("/settings/llm"),

  saveLlmConfig: (body: LlmConfigIn) =>
    request<LlmConfig>("/settings/llm", jsonInit("PUT", body)),

  testLlmConfig: (body: { provider: string; api_key?: string | null; model?: string | null }) =>
    request<LlmTestResult>("/settings/llm/test", jsonInit("POST", body)),
};
