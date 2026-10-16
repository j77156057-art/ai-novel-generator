// 极简 fetch 客户端：封装后端全部端点，统一错误解析。
// 走相对路径 /api，由 Vite dev proxy 转发到 FastAPI :8000。

import type {
  AlertsResponse,
  AssistantActionRequest,
  AssistantAnalyzeLogsRequest,
  AssistantApplyResult,
  AssistantAuditCorpusRequest,
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
  DistillJob,
  DistillJobListResponse,
  DistillStartResponse,
  ExpertQuestions,
  GenerateLoopResult,
  GeneratePlan,
  GenerateResult,
  GenerationOut,
  Graph,
  KgBookkeeping,
  LlmConfig,
  LlmConfigIn,
  LlmTestIn,
  LlmTestResult,
  NovelOut,
  NarrativeTiersResponse,
  PlotBible,
  PremiseProposal,
  ProjectCreate,
  ProjectOut,
  ProjectPatch,
  Reminders,
  StreamDoneMeta,
  StyleDistillApplyRequest,
  StyleDistillDetail,
  StyleDistillListResponse,
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

/**
 * 把服务端下发的 endpoint 归一成 client 内部的 path（去掉 /api 前缀）。
 *
 * 两道安全闸门，缺一不可：
 *   1. 只接受**站内相对路径**。绝对 URL（http://…）或协议相对 URL（//host）
 *      一律拒绝 —— 否则一张待确认卡片就能把请求发到任意外部地址。
 *   2. 只接受 /assistant/ 前缀的端点。写操作只可能落在助手自己的命名空间下，
 *      越界说明服务端（或被注入的模型输出）出了错，宁可 400 也不要盲发。
 */
function assistantEndpointPath(endpoint: string): string {
  const raw = typeof endpoint === "string" ? endpoint.trim() : "";
  if (!raw) throw new ApiError(400, "待确认卡片缺少 endpoint，拒绝盲发请求。");
  if (/^[a-z][a-z0-9+.-]*:/i.test(raw) || raw.startsWith("//")) {
    throw new ApiError(400, `待确认卡片的 endpoint 不是站内相对路径，拒绝发送：${raw}`);
  }
  const path = raw.startsWith("/") ? raw : `/${raw}`;
  const stripped = path.startsWith(`${BASE}/`) ? path.slice(BASE.length) : path;
  if (!stripped.startsWith("/assistant/")) {
    throw new ApiError(400, `待确认卡片的 endpoint 不在 /assistant/ 下，拒绝发送：${raw}`);
  }
  return stripped;
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

  /**
   * 文风蒸馏档位清单（完整 / 平衡 / 快速）。
   *
   * 静态元数据，不碰 DB，所以单独一个端点而不是挂在某个项目下。
   * 选档 UI 全部字段都来自这里 —— 前端不硬编码档位文案，后端加一档就自动出现。
   */
  listNarrativeTiers: () =>
    request<NarrativeTiersResponse>("/projects/narrative-tiers"),

  // ---------- 文风蒸馏（后台任务 + 进度条） ----------

  /**
   * 提交一次文风蒸馏。**立即返回** job_id，蒸馏在服务端后台跑。
   *
   * 不用 `buildNarrative` 之类的同步端点：完整档在 833 章语料上实测 42 分钟，
   * 同步请求必然超时；而且同步端点一断线就没进度了，谈不上「离开页面继续跑」。
   */
  startDistill: (projectId: number, tier?: string | null, force = false) =>
    request<DistillStartResponse>(
      `/projects/${projectId}/distill`,
      jsonInit("POST", { tier: tier ?? null, force }),
    ),

  /**
   * 该项目最近的蒸馏任务（id 降序）。
   *
   * 进工作台时用它恢复：若最近一条还在 pending/running，就接着轮询，
   * 用户于是看到「上次那锅还在熬、熬到第几块」，而不是一片空白。
   */
  listDistillJobs: (projectId: number, limit = 5) =>
    request<DistillJobListResponse>(
      `/projects/${projectId}/distill/jobs?limit=${encodeURIComponent(String(limit))}`,
    ),

  /** 单条蒸馏任务状态（进度条轮询的就是它）。 */
  getDistillJob: (projectId: number, jobId: number) =>
    request<DistillJob>(`/projects/${projectId}/distill/jobs/${jobId}`),

  // ---------- 文风蒸馏库（跨项目复用） ----------

  /**
   * 列出蒸馏库里可复用的文风档案（默认 active）。列表**不含** style_narrative 全文。
   *
   * 复用入口：项目 A 蒸馏完入库，项目 B 在这里看到 A 的档案，点「应用」即可，
   * 省掉 B 自己的 42 分钟 LLM。
   */
  listStyleDistills: (status: "active" | "archived" | "all" = "active", limit = 50) =>
    request<StyleDistillListResponse>(
      `/projects/style-distills?status=${encodeURIComponent(status)}&limit=${encodeURIComponent(String(limit))}`,
    ),

  /** 单条蒸馏库条目详情（含 style_narrative 全文）。 */
  getStyleDistill: (distillId: number) =>
    request<StyleDistillDetail>(`/projects/style-distills/${distillId}`),

  /** 归档一条蒸馏库条目（不再被复用，但保留供查阅）。 */
  archiveStyleDistill: (distillId: number) =>
    request<StyleDistillListResponse["items"][number]>(
      `/projects/style-distills/${distillId}/archive`,
      jsonInit("POST"),
    ),

  /** 把蒸馏库某条目应用到本项目（复制其 style_narrative 进项目缓存）。 */
  applyStyleDistill: (projectId: number, distillId: number) =>
    request<ProjectOut>(`/projects/${projectId}/style-distills/apply`, {
      ...jsonInit("POST"),
      body: JSON.stringify({ distill_id: distillId } satisfies StyleDistillApplyRequest),
    }),

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
    request<KgBookkeeping>(
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
    onDone: (meta: StreamDoneMeta) => void,
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
                  | ({ type: "done" } & StreamDoneMeta)
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
    onDone: (meta: StreamDoneMeta) => void,
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
                  | ({ type: "done" } & StreamDoneMeta)
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

  // ---------- Assistant 运行日志 / 审核 / 调动生成（后端已合入） ----------

  /** GET /api/assistant/runs?project_id=&status=&limit=（助手自己的运行日志，只读）。 */
  assistantRuns: (projectId: number, status?: string | null, limit = 50) =>
    request<AssistantRunListResponse>(
      `/assistant/runs?project_id=${projectId}` +
        (status ? `&status=${encodeURIComponent(status)}` : "") +
        `&limit=${limit}`,
    ),

  /**
   * GET /api/assistant/runs/{run_id}（单条日志详情，含该 run 的全部 Evidence）。
   * projectId 可选：带了后端会做跨项目校验（不匹配 → 404）。
   */
  assistantRunDetail: (runId: number, projectId?: number | null) =>
    request<AssistantRunDetailResponse>(
      `/assistant/runs/${runId}${projectId ? `?project_id=${projectId}` : ""}`,
    ),

  /** POST /api/assistant/analyze-logs（日志分析，只读，统一 issues 结构）。 */
  assistantAnalyzeLogs: (body: AssistantAnalyzeLogsRequest) =>
    request<AssistantAuditResponse>("/assistant/analyze-logs", jsonInit("POST", body)),

  /** POST /api/assistant/audit-corpus（参考语料审核，只读，统一 issues）。 */
  assistantAuditCorpus: (body: AssistantAuditCorpusRequest) =>
    request<AssistantAuditResponse>("/assistant/audit-corpus", jsonInit("POST", body)),

  /** POST /api/assistant/audit-kg（知识图谱审核，只读，统一 issues）。 */
  assistantAuditKg: (body: AssistantAuditRequest) =>
    request<AssistantAuditResponse>("/assistant/audit-kg", jsonInit("POST", body)),

  /** POST /api/assistant/audit-generation（生成质量审核，只读，统一 issues）。 */
  assistantAuditGeneration: (body: AssistantAuditRequest) =>
    request<AssistantAuditResponse>("/assistant/audit-generation", jsonInit("POST", body)),

  /**
   * POST /api/assistant/invoke-generation（助手按用户显式指令调动 Writer 生成）。
   * instruction 必填非空；segment_no 仅 regenerate_segment 需要。
   */
  assistantInvokeGeneration: (body: AssistantInvokeGenerationRequest) =>
    request<AssistantInvokeGenerationResponse>(
      "/assistant/invoke-generation",
      jsonInit("POST", body),
    ),

  /**
   * 执行一张待确认卡片。
   *
   * **前端不构造任何请求体**：endpoint / method / body 三者全部由服务端在
   * `pending_actions[]` 里下发，这里原样回传。理由是前后端各拼一半必然
   * 漂移 —— 服务端加一个必填字段而前端没跟上，表现为运行期 422，且很难
   * 定位是「契约改了」还是「前端拼错了」。
   *
   * 端点合法性由 `assistantEndpointPath` 把关（站内相对路径 + /assistant/ 前缀）。
   */
  assistantRunPendingAction: (
    endpoint: string,
    method: string,
    body: Record<string, unknown>,
  ) => request<Record<string, unknown>>(
    assistantEndpointPath(endpoint),
    jsonInit(method.toUpperCase(), body),
  ),
};
