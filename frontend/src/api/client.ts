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
  KgBibleApplyResult,
  KgBibleRevisionListItem,
  KgV3Character,
  KgV3ExemptRequest,
  KgV3JudgeFindingsResponse,
  KgV3StyleWarningsResponse,
  KgV3EventsResponse,
  KgV3Relation,
  KgV3Slice,
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
  RoleplayChatResult,
  RoleplayCharactersResponse,
  RoleplayMessage,
  StyleDistillApplyRequest,
  StyleDistillDetail,
  StyleDistillListResponse,
  StyleNarrativeConflictsResponse,
  PersonaCompleteResponse,
  PersonaListResponse,
  PersonaSaveRequest,
  StyleNarrativeRepairResponse,
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

/**
 * 构造一个 JSON 请求的 init。
 *
 * ## 为什么要「body 一起传进来」
 *
 * fetch 对**字符串 body** 不会自动带 `application/json` —— 浏览器会补
 * `Content-Type: text/plain;charset=UTF-8`。FastAPI 收到 `text/plain` 的请求体
 * **不会**把它当 JSON 解析 ⇒ 直接 422
 * （实测：`{"distill_id": 3}` 被拒成「Input should be a valid dictionary」）。
 *
 * 之前 `jsonInit("POST")` 不带 body 时**不设 headers**，调用方再自己补
 * `body: JSON.stringify(...)` ⇒ Content-Type 永远是 text/plain ⇒
 * 「应用到本项目」这个按钮**一直是坏的**（真机点出来才发现，不是测试发现的）。
 *
 * 所以：**body 必须传进 `jsonInit`**，让它决定 headers。
 * 用 `!== undefined` 而不是真值判断：body 可能是 `0` / `""` / `false` / `null`。
 */
function jsonInit(method: string, body?: unknown): RequestInit {
  const hasBody = body !== undefined;
  return {
    method,
    headers: hasBody ? { "Content-Type": "application/json" } : undefined,
    body: hasBody ? JSON.stringify(body) : undefined,
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
   * plot_bible 级联协议新路径：写 revision + 算 diff + 扫 KG 冲突 + 覆盖。
   * 返回的 conflicts 在前端 toast 一次性展开——不需要新 tab。
   */
  applyBible: (projectId: number, bible: Record<string, unknown>, note?: string | null) =>
    request<KgBibleApplyResult>(
      `/projects/${projectId}/bible/apply`,
      jsonInit("POST", { bible, note: note ?? null }),
    ),

  /** plot_bible 改写历史（按 revision_no 倒序，新的在前）。 */
  listBibleRevisions: (projectId: number, limit = 20) =>
    request<{ revisions: KgBibleRevisionListItem[] }>(
      `/projects/${projectId}/bible/revisions?limit=${limit}`,
    ),

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

  /**
   * 工序链状态：助理 agent 编排的全流程走到哪一步、卡在哪儿。
   *
   * 这是服务端**重新评估**出来的（查库 + 只读规则），不是日志里的一句话 ——
   * 正文改过、蒸馏重跑过、提案批了，状态都会跟着变。
   */
  getWorkflow: (projectId: number) =>
    request<import("@/api/types").WorkflowState>(`/projects/${projectId}/workflow`),

  /**
   * 记一笔工序推进（派发留痕）。只写工序链自己的账，不动业务数据。
   */
  recordWorkflowStep: (
    projectId: number,
    body: { step_id: string; action: string; detail?: string; proposal_id?: number },
  ) =>
    request<import("@/api/types").WorkflowState>(
      `/projects/${projectId}/workflow/log`,
      jsonInit("POST", body),
    ),

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
    request<ProjectOut>(
      `/projects/${projectId}/style-distills/apply`,
      jsonInit("POST", { distill_id: distillId } satisfies StyleDistillApplyRequest),
    ),

  /**
   * 查文风档案里的禁用词冲突（只读，不调模型）。
   *
   * 体检说「档案的禁用词与档案正文自相矛盾」时，生成会被挡住（409）。
   * 这个端点把那几处列出来，界面上就能直接修 —— 不用重蒸馏（≈42 分钟）。
   */
  listStyleNarrativeConflicts: (projectId: number) =>
    request<StyleNarrativeConflictsResponse>(
      `/projects/${projectId}/style-narrative/conflicts`,
    ),

  /**
   * 修档案里的禁用词冲突（**人点的**：agent 只能提卡、不能落库）。
   *
   * `preview=true` 只返回「将要改什么」，不落库；
   * `preview=false` 落库并重跑体检，结果一起返回（当场自证修好了）。
   */
  repairStyleNarrative: (
    projectId: number,
    body: { word: string; mode: "remove_word" | "drop_rule" | "unban"; preview?: boolean },
  ) =>
    request<StyleNarrativeRepairResponse>(
      `/projects/${projectId}/style-narrative/repair`,
      jsonInit("POST", body),
    ),

  /**
   * 列出角色的个性档案（用户自己写的那份也在里面，标 `source=user`）。
   */
  listPersonas: (projectId: number) =>
    request<PersonaListResponse>(`/projects/${projectId}/style-narrative/personas`),

  /**
   * 保存**用户写的**角色个性（落库后标 `来源=user`，不会被反推覆盖）。
   */
  savePersona: (projectId: number, body: PersonaSaveRequest) =>
    request<{ ok: boolean; name: string; persona: Record<string, unknown> }>(
      `/projects/${projectId}/style-narrative/personas`,
      jsonInit("POST", body),
    ),

  /**
   * AI 帮着完善角色个性 —— **只返回建议，不落库**（要不要采纳由人点）。
   */
  completePersona: (projectId: number, body: { name: string; persona?: Record<string, unknown>; note?: string }) =>
    request<PersonaCompleteResponse>(
      `/projects/${projectId}/style-narrative/personas/complete`,
      jsonInit("POST", body),
    ),

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
  //
  // ⚠️ chunk 的**到达时机**变了（2026-10-27）：后端现在「先生成 + 过段长闸门，
  // 再分块吐出」⇒ 首字延迟从秒级变成整段生成的时长。
  // 换来的是：界面落库的段长与同步接口一致（闸门下限 = 目标 × 80%）。
  generateStream: (
    projectId: number,
    maxChars: number | null,
    onChunk: (text: string) => void,
    onDone: (meta: {
      segment_no: number;
      chars: number;
      summary: string;
      id: number;
      /** 实际汉字数（与 length_report.cjk 相同） */
      cjk?: number;
      /** 段长闸门的报告：目标 / 下限 / 实测 / 是否达标 */
      length_report?: {
        target_cjk?: number;
        floor_cjk?: number;
        cjk?: number;
        attempts?: number;
        below_target?: boolean;
        below_floor?: boolean;
      };
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
                      /** 实际汉字数（与 length_report.cjk 相同） */
                      cjk?: number;
                      /** 段长闸门的报告：目标 / 下限 / 实测 / 是否达标 */
                      length_report?: {
                        target_cjk?: number;
                        floor_cjk?: number;
                        cjk?: number;
                        attempts?: number;
                        below_target?: boolean;
                        below_floor?: boolean;
                      };
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
                      /** 实际汉字数（与 length_report.cjk 相同） */
                      cjk?: number;
                      /** 段长闸门的报告：目标 / 下限 / 实测 / 是否达标 */
                      length_report?: {
                        target_cjk?: number;
                        floor_cjk?: number;
                        cjk?: number;
                        attempts?: number;
                        below_target?: boolean;
                        below_floor?: boolean;
                      };
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
  uploadAssistantAttachment: (projectId: number, file: File) => {
    const form = new FormData();
    form.append("project_id", String(projectId));
    form.append("file", file);
    return request<import("@/utils/assistantAttachments").AttachmentMetadata>("/assistant/attachments", { method: "POST", body: form });
  },

  getAssistantAttachment: (projectId: number, attachmentId: string) =>
    request<import("@/utils/assistantAttachments").AttachmentMetadata>(`/assistant/attachments/${encodeURIComponent(attachmentId)}?project_id=${projectId}`),

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

  // —— 动作提案：agent 提「做什么事」，人批了才执行 ——
  /** 动作提案列表（可按 status 过滤）。 */
  listActionProposals: (projectId: number, status?: string | null) =>
    request<import("@/api/types").ActionProposalListResponse>(
      `/assistant/action-proposals?project_id=${projectId}` +
        (status ? `&status=${encodeURIComponent(status)}` : ""),
    ),

  /** 创建动作提案（停在 pending，动作还没执行）。 */
  createActionProposal: (
    body: import("@/api/types").ActionProposalCreateRequest,
  ) =>
    request<{
      proposal: import("@/api/types").ActionProposalOut;
      next_action: string;
      note: string;
    }>("/assistant/action-proposals", jsonInit("POST", body)),

  /** 批准动作提案：pending → approved。只改状态，不执行动作。 */
  approveActionProposal: (proposalId: number, body: AssistantActionRequest) =>
    request<{
      proposal: import("@/api/types").ActionProposalOut;
      next_action: string;
      note: string;
    }>(`/assistant/action-proposals/${proposalId}/approve`, jsonInit("POST", body)),

  /** 执行动作提案：approved → applied（唯一会动业务表的一步）。 */
  applyActionProposal: (proposalId: number, body: AssistantActionRequest) =>
    request<{
      proposal: import("@/api/types").ActionProposalOut;
      result: Record<string, unknown>;
      note: string | null;
    }>(`/assistant/action-proposals/${proposalId}/apply`, jsonInit("POST", body)),

  /** 拒绝动作提案：pending → rejected（终态）。 */
  rejectActionProposal: (proposalId: number, body: AssistantActionRequest) =>
    request<{
      proposal: import("@/api/types").ActionProposalOut;
      note: string;
    }>(`/assistant/action-proposals/${proposalId}/reject`, jsonInit("POST", body)),

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

  // ---------- KG v3.0（9 表读侧 + Judge + 豁免）----------

  /** 按当前剧情点取 KG 切片（generate 注入 prompt 的同款数据）。 */
  kgV3Slice: (
    projectId: number,
    segmentNo: number,
    opts: { window?: number; maxEntities?: number; characters?: string | null } = {},
  ) => {
    const p = new URLSearchParams({ segment_no: String(segmentNo) });
    if (opts.window != null) p.set("window", String(opts.window));
    if (opts.maxEntities != null) p.set("max_entities", String(opts.maxEntities));
    if (opts.characters) p.set("characters", opts.characters);
    return request<KgV3Slice>(`/projects/${projectId}/kg-v3/slice?${p}`);
  },

  /** 渲染成 prompt 文本（看 generate 实际注入什么）。参数与 kgV3Slice 对齐。 */
  kgV3SlicePrompt: (
    projectId: number,
    segmentNo: number,
    opts: { window?: number; maxEntities?: number; characters?: string | null } = {},
  ) => {
    const p = new URLSearchParams({ segment_no: String(segmentNo) });
    if (opts.window != null) p.set("window", String(opts.window));
    if (opts.maxEntities != null) p.set("max_entities", String(opts.maxEntities));
    if (opts.characters) p.set("characters", opts.characters);
    return request<{ text: string }>(`/projects/${projectId}/kg-v3/slice/prompt?${p}`);
  },

  /** 关系演变曲线（strength 历史）。 */
  kgV3RelationCurve: (projectId: number, fromName: string, toName: string) =>
    request<{ from: string; to: string; curve: { rel_type: string; chapter: number | null; strength: number; note: string | null }[] }>(
      `/projects/${projectId}/kg-v3/relations/curve?from=${encodeURIComponent(fromName)}&to=${encodeURIComponent(toName)}`,
    ),

  /** 人物情报追踪：character 此刻是否知道 fact。 */
  kgV3Knows: (projectId: number, character: string, fact: string) =>
    request<{ character: string; fact: string; knows: boolean }>(
      `/projects/${projectId}/kg-v3/knowledge?character=${encodeURIComponent(character)}&fact=${encodeURIComponent(fact)}`,
    ),

  /** 节奏分析：最近 N 段每实体的 mention 分布。 */
  kgV3Rhythm: (projectId: number, window = 10) =>
    request<{ segments: number[]; top_entities: { name: string; kind: string; mentions: number }[] }>(
      `/projects/${projectId}/kg-v3/rhythm?window=${window}`,
    ),

  /** 事件列表（含因果链），sequence 升序。 */
  kgV3Events: (projectId: number, limit = 200, eventType?: string | null) => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (eventType) p.set("event_type", eventType);
    return request<KgV3EventsResponse>(`/projects/${projectId}/kg-v3/events?${p}`);
  },

  /** 作者豁免：认定某告警是故意的，Judge 以后不再报。 */
  kgV3Exempt: (projectId: number, body: KgV3ExemptRequest) =>
    request<{ exempted: boolean; rule: string; entity: string }>(
      `/projects/${projectId}/kg-v3/exemptions`,
      jsonInit("POST", body),
    ),

  /** 豁免清单。 */
  kgV3ListExemptions: (projectId: number) =>
    request<{ exemptions: { rule: string | null; entity: string | null; note: string | null }[] }>(
      `/projects/${projectId}/kg-v3/exemptions`,
    ),

  /** 撤销豁免：Judge 恢复报告该告警。 */
  kgV3RevokeExemption: (projectId: number, rule: string, entity: string) =>
    request<{ revoked: number; rule: string; entity: string }>(
      `/projects/${projectId}/kg-v3/exemptions?rule=${encodeURIComponent(rule)}&entity=${encodeURIComponent(entity)}`,
      { method: "DELETE" },
    ),

  /** Judge 告警历史（落库的，按段倒序，含 exempted 标记与规则清单）。 */
  kgV3JudgeFindings: (projectId: number) =>
    request<KgV3JudgeFindingsResponse>(`/projects/${projectId}/kg-v3/judge/findings`),

  /** v2.1：文风告警历史（engine 落库 style_narrative.style_warnings，按段倒序）。 */
  kgV3StyleWarnings: (projectId: number) =>
    request<KgV3StyleWarningsResponse>(`/projects/${projectId}/kg-v3/style-warnings`),

  /** 项目角色清单（mention 降序）。 */
  kgV3Characters: (projectId: number, limit = 200) =>
    request<{ characters: KgV3Character[] }>(
      `/projects/${projectId}/kg-v3/characters?limit=${limit}`,
    ),

  /** 关系清单（strength 降序，含两端实体名）。 */
  kgV3Relations: (projectId: number, limit = 300) =>
    request<{ relations: KgV3Relation[] }>(
      `/projects/${projectId}/kg-v3/relations?limit=${limit}`,
    ),

  /** 批抽 worker 状态：线程存活/队列积压/汇总计数/最近行。 */
  kgV3WorkerStatus: (projectId: number) =>
    request<{
      worker_alive: boolean;
      pending: number;
      summary: { success: number; failed: number; running: number; stuck: number; empty: number };
      recent_runs: {
        id: number;
        segment_no: number | null;
        status: string;
        stuck: boolean;
        empty: boolean;
        duration_ms: number | null;
        batch_stats: Record<string, unknown> | null;
        error: string | null;
        started_at: string | null;
      }[];
    }>(`/projects/${projectId}/kg-v3/worker/status`),

  /** 手动重捞失败的段（按 run id 列表）。 */
  kgV3Requeue: (projectId: number, runIds: number[]) =>
    request<{ requeued: number; missing: number[] }>(
      `/projects/${projectId}/kg-v3/worker/requeue`,
      jsonInit("POST", { run_ids: runIds }),
    ),

  // ---------- 角色对话（第三板块） ----------
  /** 可扮演的角色清单（参考书角色 + 图谱角色 + 剧情圣经角色）。 */
  roleplayCharacters: (projectId: number) =>
    request<RoleplayCharactersResponse>(`/projects/${projectId}/roleplay/characters`),

  /** 问一句，答一句。产物是对话记录，不进正文主线。 */
  roleplayChat: (
    projectId: number,
    body: { character: string; question: string; novel_ids?: number[]; k?: number },
  ) =>
    request<RoleplayChatResult>(
      `/projects/${projectId}/roleplay/chat`,
      jsonInit("POST", body),
    ),

  /** 对话记录。 */
  roleplayMessages: (projectId: number, character?: string) =>
    request<{ messages: RoleplayMessage[] }>(
      `/projects/${projectId}/roleplay/messages` +
        (character ? `?character=${encodeURIComponent(character)}` : ""),
    ),
};
