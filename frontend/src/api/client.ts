// 极简 fetch 客户端：封装后端全部端点，统一错误解析。
// 走相对路径 /api，由 Vite dev proxy 转发到 FastAPI :8000。

import type {
  AlertsResponse,
  AssistantActionRequest,
  AssistantAnalyzeLogsRequest,
  AssistantApplyResult,
  AssistantAuditRequest,
  AssistantAuditResponse,
  AssistantDiagnoseRequest,
  AssistantDiagnoseResponse,
  AssistantInvokeGenerationRequest,
  AssistantInvokeGenerationResponse,
  AssistantProposalActionResponse,
  AssistantProposalCreate,
  AssistantProposalCreateResponse,
  AssistantProposalDetailResponse,
  AssistantProposalListResponse,
  AssistantProposalStatus,
  AssistantPromptVersionsResponse,
  AssistantPromptsActiveResponse,
  AssistantQaRequest,
  AssistantQaResponse,
  AssistantRestoreRequest,
  AssistantRestoreResult,
  AssistantRunDetailResponse,
  AssistantRunListResponse,
  AssistantVersionDiffResponse,
  AssistantVersionListResponse,
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
  LlmTestIn,
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
  /**
   * 后端 FastAPI 的原始 `detail`。
   *
   * 多数端点是字符串；但漂移 409 返回的是结构化 dict
   * （`{error:"stale", message, expected_hash, current_hash, hint}`），
   * 界面要把它和「状态机拒绝」的 409 区分开，所以这里保留原始对象，
   * 不退化成 "[object Object]"。
   */
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail === undefined ? message : detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, init);
  const text = await res.text();
  const data = text ? safeJson(text) : null;
  if (!res.ok) {
    const raw =
      data && typeof data === "object" && "detail" in data
        ? (data as { detail: unknown }).detail
        : undefined;
    const detail = raw !== undefined ? detailText(raw) : text || `HTTP ${res.status}`;
    throw new ApiError(res.status, detail, raw === undefined ? text : raw);
  }
  return data as T;
}

/**
 * 把 FastAPI 的 detail 转成可读文本。
 *
 * 三种形状都要处理：字符串（多数端点）、带 message 的对象（漂移 409 的
 * `{error:"stale", message, ...}`，取 message）、pydantic 422 的对象数组
 * （`[{loc, msg}, ...]`，逐条拼 "body.role: Extra inputs are not permitted"）——
 * 直接 String() 数组只会得到 "[object Object]"，等于把后端的拒绝理由丢了。
 */
function detailText(raw: unknown): string {
  if (typeof raw === "string") return raw;
  if (Array.isArray(raw)) {
    return raw
      .map((item) => {
        if (item && typeof item === "object") {
          const loc = (item as { loc?: unknown }).loc;
          const msg = (item as { msg?: unknown }).msg;
          const where = Array.isArray(loc) ? loc.join(".") : "";
          const what = typeof msg === "string" ? msg : JSON.stringify(item);
          return where ? `${where}: ${what}` : what;
        }
        return String(item);
      })
      .join("；");
  }
  if (raw && typeof raw === "object" && "message" in raw) {
    const m = (raw as { message: unknown }).message;
    if (typeof m === "string" && m) return m;
  }
  return String(raw);
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

  /** role 可选：带 role 时只测该角色这一个配置（生成 / 助手分开测）。 */
  testLlmConfig: (body: LlmTestIn) =>
    request<LlmTestResult>("/settings/llm/test", jsonInit("POST", body)),

  // ---------- Assistant（助手面板，共 13 个端点）----------
  // 请求体全部是后端 StrictRequest（extra="forbid"）：字段名写错 / 多传一个
  // 字段就是 422，所以这里不拼多余字段，也不构造 scope 的服务端独占键
  // （base_content_hash / target_snapshot / diff / impact）。

  /** 1. 问答：只读检索 + Assistant 模型回答，返回 evidence 与 confidence。 */
  assistantQa: (body: AssistantQaRequest) =>
    request<AssistantQaResponse>("/assistant/qa", jsonInit("POST", body)),

  /** 2. 诊断：确定性规则先行、模型后置，返回结构化 issues。 */
  assistantDiagnose: (body: AssistantDiagnoseRequest) =>
    request<AssistantDiagnoseResponse>("/assistant/diagnose", jsonInit("POST", body)),

  /** 3. 创建提案（停在 pending，业务表零写入）。after_text 必须由用户填写。 */
  createProposal: (body: AssistantProposalCreate) =>
    request<AssistantProposalCreateResponse>(
      "/assistant/proposals",
      jsonInit("POST", body),
    ),

  /** 4. 提案列表（可按 status 过滤）。 */
  listProposals: (projectId: number, status?: AssistantProposalStatus | null, limit = 100) =>
    request<AssistantProposalListResponse>(
      `/assistant/proposals?project_id=${projectId}` +
        (status ? `&status=${encodeURIComponent(status)}` : "") +
        `&limit=${limit}`,
    ),

  /** 5. 提案详情（含 diff 与目标基线指纹）。 */
  getProposal: (proposalId: number) =>
    request<AssistantProposalDetailResponse>(`/assistant/proposals/${proposalId}`),

  /** 6. 批准：pending → approved。只改状态，不写业务表。 */
  approveProposal: (proposalId: number, body: AssistantActionRequest) =>
    request<AssistantProposalActionResponse>(
      `/assistant/proposals/${proposalId}/approve`,
      jsonInit("POST", body),
    ),

  /** 7. 拒绝：pending → rejected（终态）。 */
  rejectProposal: (proposalId: number, body: AssistantActionRequest) =>
    request<AssistantProposalActionResponse>(
      `/assistant/proposals/${proposalId}/reject`,
      jsonInit("POST", body),
    ),

  /** 8. 应用：approved → applied，唯一写业务表的入口。漂移时返回 409(stale)。 */
  applyProposal: (proposalId: number, body: AssistantActionRequest) =>
    request<AssistantApplyResult>(
      `/assistant/proposals/${proposalId}/apply`,
      jsonInit("POST", body),
    ),

  /** 9. 版本历史（含回滚产生的版本）。 */
  listVersions: (projectId: number, kind?: string | null, limit = 100) =>
    request<AssistantVersionListResponse>(
      `/assistant/versions?project_id=${projectId}` +
        (kind ? `&kind=${encodeURIComponent(kind)}` : "") +
        `&limit=${limit}`,
    ),

  /** 10. 两个版本的内容 diff（from_id → to_id）。 */
  diffVersions: (fromId: number, toId: number) =>
    request<AssistantVersionDiffResponse>(
      `/assistant/versions/diff?from_id=${fromId}&to_id=${toId}`,
    ),

  /** 11. 回滚到某个版本；回滚本身也生成一条新 Version（历史不丢）。 */
  restoreVersion: (versionId: number, body: AssistantRestoreRequest) =>
    request<AssistantRestoreResult>(
      `/assistant/versions/${versionId}/restore`,
      jsonInit("POST", body),
    ),

  /** 12. 当前生效提示词来源（runtime_override / code_default）。 */
  promptsActive: (projectId: number) =>
    request<AssistantPromptsActiveResponse>(
      `/assistant/prompts/active?project_id=${projectId}`,
    ),

  /** 13. 提示词覆写操作历史。 */
  promptVersions: (projectId: number) =>
    request<AssistantPromptVersionsResponse>(
      `/assistant/prompts/versions?project_id=${projectId}`,
    ),

  // ---------- Assistant 运行日志 / 审核 / 调动生成 ----------
  // 后端并行开发中，尚未就绪：这些方法按契约接好（类型见 types.ts 末段），
  // 但 UI 当前不调用——端点合入前不发送请求，也不用假数据冒充响应。

  /** GET /api/assistant/runs?project_id=&status=&limit=（助手自己的运行日志）。 */
  assistantRuns: (projectId: number, status?: string | null, limit = 50) =>
    request<AssistantRunListResponse>(
      `/assistant/runs?project_id=${projectId}` +
        (status ? `&status=${encodeURIComponent(status)}` : "") +
        `&limit=${limit}`,
    ),

  /** GET /api/assistant/runs/{run_id}（单条日志详情，含 evidence）。 */
  assistantRunDetail: (runId: number) =>
    request<AssistantRunDetailResponse>(`/assistant/runs/${runId}`),

  /** POST /api/assistant/analyze-logs（日志分析，返回统一 issues 结构）。 */
  assistantAnalyzeLogs: (body: AssistantAnalyzeLogsRequest) =>
    request<AssistantAuditResponse>("/assistant/analyze-logs", jsonInit("POST", body)),

  /** POST /api/assistant/audit-corpus（参考语料审核，统一 issues）。 */
  assistantAuditCorpus: (body: AssistantAuditRequest) =>
    request<AssistantAuditResponse>("/assistant/audit-corpus", jsonInit("POST", body)),

  /** POST /api/assistant/audit-kg（知识图谱审核，统一 issues）。 */
  assistantAuditKg: (body: AssistantAuditRequest) =>
    request<AssistantAuditResponse>("/assistant/audit-kg", jsonInit("POST", body)),

  /** POST /api/assistant/audit-generation（生成质量审核，统一 issues）。 */
  assistantAuditGeneration: (body: AssistantAuditRequest) =>
    request<AssistantAuditResponse>("/assistant/audit-generation", jsonInit("POST", body)),

  /** POST /api/assistant/invoke-generation（助手按用户显式指令调动 Writer 生成）。 */
  assistantInvokeGeneration: (body: AssistantInvokeGenerationRequest) =>
    request<AssistantInvokeGenerationResponse>(
      "/assistant/invoke-generation",
      jsonInit("POST", body),
    ),
};
