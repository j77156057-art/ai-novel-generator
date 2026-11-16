// 极简 fetch 客户端：封装后端全部端点，统一错误解析。
// 走相对路径 /api，由 Vite dev proxy 转发到 FastAPI :8000。

import type {
  ActionProposalCreateRequest,
  ActionProposalListResponse,
  ActionProposalOut,
  AlertsResponse,
  AssistantActionRequest,
  AssistantAnalyzeLogsRequest,
  AssistantApplyResult,
  AssistantApplySelectedResponse,
  AssistantAuditCorpusRequest,
  AssistantAuditRequest,
  AssistantAuditResponse,
  AssistantCleanCorpusRequest,
  AssistantCleanCorpusResponse,
  AssistantDiagnoseRequest,
  AssistantDiagnoseResponse,
  AssistantInvokeGenerationRequest,
  AssistantInvokeGenerationResult,
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
  AssistantRollbackBatchRequest,
  AssistantRollbackBatchResult,
  AssistantRollbackRequest,
  AssistantRollbackResult,
  AssistantRunDetailResponse,
  AssistantRunListResponse,
  AssistantSnapshotGroupListResponse,
  AssistantSnapshotListResponse,
  AssistantVersionDiffResponse,
  AssistantVersionListResponse,
  AttachmentCleaningPreview,
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
  GenerateStreamDoneMeta,
  Graph,
  KgRebuildResult,
  KgBibleApplyResult,
  KgBibleRevisionListItem,
  KgCharacterEditResult,
  KgCharacterRenameResult,
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
  RegenerateStreamDoneMeta,
  Reminders,
  RoleplayCharactersResponse,
  RoleplayChatResult,
  RoleplayMessage,
  StyleDistillApplyRequest,
  StyleDistillDetail,
  StyleDistillListResponse,
  SearchHit,
  UploadResult,
  WizardAnswerRequest,
  WizardAnswerResponse,
  WizardRoleChatResponse,
  WizardRunResponse,
  WizardNoticeListResponse,
  WizardMessageListResponse,
  WizardSayResponse,
  WorkflowState,
  WizardStateResponse,
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

  uploadNovel: (file: File, dropForeignBlocks = false) => {
    const form = new FormData();
    form.append("file", file);
    // 与后端 `Form(False)` 同名同义：默认只检测不删，显式 true 才剔除检出的异书块。
    form.append("drop_foreign_blocks", String(dropForeignBlocks));
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
   * 角色改名：先量正文覆盖，影响前文时返回 ok=false 的回执（不写库）。
   *
   * 允许时改 bible.name + 同步 KG 实体，旧名登记为别名。拒绝也返回 200，
   * 用 `ok` 区分，避免前端把业务判定当成网络错误吞掉原因。
   */
  renameBibleCharacter: (
    projectId: number,
    oldName: string,
    newName: string,
    note?: string | null,
  ) =>
    request<KgCharacterRenameResult>(
      `/projects/${projectId}/bible/characters/rename`,
      jsonInit("POST", { old_name: oldName, new_name: newName, note: note ?? null }),
    ),

  /** 加角色：card 必须过后端封闭键表（未知 / 不支持字段直接 400）。 */
  addBibleCharacter: (
    projectId: number,
    card: Record<string, unknown>,
    note?: string | null,
  ) =>
    request<KgCharacterEditResult>(
      `/projects/${projectId}/bible/characters/add`,
      jsonInit("POST", { card, note: note ?? null }),
    ),

  /** 删角色：删到空时后端保留 characters=[]（空清册语义）。 */
  removeBibleCharacter: (projectId: number, name: string, note?: string | null) =>
    request<KgCharacterEditResult>(
      `/projects/${projectId}/bible/characters/remove`,
      jsonInit("POST", { name, note: note ?? null }),
    ),

  /** 改境界：写 characters[].境界（或 修炼.当前境界），走 power 块。 */
  setBibleCharacterRealm: (
    projectId: number,
    name: string,
    realm: string,
    note?: string | null,
  ) =>
    request<KgCharacterEditResult>(
      `/projects/${projectId}/bible/characters/realm`,
      jsonInit("POST", { name, realm, note: note ?? null }),
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
   * 从断点继续一次被中断的蒸馏。
   *
   * 与 `startDistill` 的差别只有两点：必定复用已完成块（resume=True）、
   * 必定不清空断点（force=False）。**重启后不会自动续跑**——由用户在中断
   * 回执上点「继续蒸馏」触发，避免无人值守烧模型费用。
   */
  resumeDistill: (projectId: number) =>
    request<DistillStartResponse>(
      `/projects/${projectId}/distill/resume`,
      jsonInit("POST", {}),
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

  getKg: (
    projectId: number,
    type: "all" | "character" | "plothole" = "all",
    source?: string | null,
  ) =>
    request<Graph>(
      `/projects/${projectId}/kg?type=${type}${source ? `&source=${encodeURIComponent(source)}` : ""}`,
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
    request<KgRebuildResult>(
      `/projects/${projectId}/build-kg-from-generations`,
      jsonInit("POST"),
    ),

  // ---------- 生成环 ----------
  generatePlan: (projectId: number) =>
    request<GeneratePlan>(
      "/generate-plan",
      jsonInit("POST", { project_id: projectId }),
    ),

  // 流式续写：SSE 逐块回调 onChunk，结束回调 onDone。流结束时 Promise resolve。
  generateStream: (
    projectId: number,
    maxChars: number | null,
    onChunk: (text: string) => void,
    onDone: (meta: GenerateStreamDoneMeta) => void,
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
                  | ({ type: "done" } & GenerateStreamDoneMeta)
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
    onDone: (meta: RegenerateStreamDoneMeta) => void,
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
                  | ({ type: "done" } & RegenerateStreamDoneMeta)
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

  /** 上传文本附件到项目暂存区（只暂存，不写库；投放必须另点确认卡）。 */
  uploadAssistantAttachment: (projectId: number, file: File) => {
    const form = new FormData();
    form.append("project_id", String(projectId));
    form.append("file", file);
    return request<import("@/utils/assistantAttachments").AttachmentMetadata>(
      "/assistant/attachments",
      { method: "POST", body: form },
    );
  },

  /** 投放前的清洗预览（只读）：会删什么、还剩什么没解决。 */
  previewAssistantAttachment: (projectId: number, attachmentId: string) =>
    request<AttachmentCleaningPreview>(
      `/assistant/attachments/${encodeURIComponent(attachmentId)}/preview?project_id=${projectId}`,
    ),

  // 诊断 / 提案 / 版本 / 提示词 / 运行日志 / 三类审核的封装已从前端收起：
  // 能力改由助手按后端动作目录（app/agents/action_catalog.py）发现，读走工具、
  // 写走待确认卡片、破坏性操作只在页面做。后端端点保持不变，契约见 routers。

  // ---------- 档 3：前端显式入口专用（助手不得代执行）----------
  //
  // 语料清洗 / 快照回滚 / 批量应用提案在动作目录里登记为 frontend_only：
  // 助手只能告诉用户「去哪个页面」，真正触发只发生在这里。页面负责
  // 「影响范围说明 → 二次确认 → 可核对回执」三步，actor 固定串标记来源。

  /** 语料清洗：规则先行 + 模型复核，只产 pending 提案，不写 novel_chunks。 */
  cleanCorpus: (body: AssistantCleanCorpusRequest) =>
    request<AssistantCleanCorpusResponse>(
      "/assistant/clean-corpus",
      jsonInit("POST", body),
    ),

  /** 重写快照列表；只有 status="applied" 的旧快照还能回滚。 */
  listSnapshots: (projectId: number, status?: string | null, limit = 50) => {
    const p = new URLSearchParams({
      project_id: String(projectId),
      limit: String(limit),
    });
    if (status) p.set("status", status);
    return request<AssistantSnapshotListResponse>(`/assistant/snapshots?${p}`);
  },

  /** 按一次操作（batch_id）聚合的撤销入口（整组回滚用）。 */
  listSnapshotGroups: (projectId: number, limit = 20) =>
    request<AssistantSnapshotGroupListResponse>(
      `/assistant/snapshots/groups?project_id=${projectId}&limit=${limit}`,
    ),

  /** 单段回滚；不给 snapshot_id 时可只给 segment_no（撤销该段上一次）。 */
  rollbackSnapshot: (body: AssistantRollbackRequest) =>
    request<AssistantRollbackResult>(
      "/assistant/snapshots/rollback",
      jsonInit("POST", body),
    ),

  /** 整组回滚：一个事务恢复所有正文，任一段不自洽则零写入。 */
  rollbackSnapshotGroup: (body: AssistantRollbackBatchRequest) =>
    request<AssistantRollbackBatchResult>(
      "/assistant/snapshots/rollback-group",
      jsonInit("POST", body),
    ),

  /** 提案列表（status 可选：pending / approved / applied / rejected / stale…）。 */
  listProposals: (projectId: number, status?: string | null, limit = 100) => {
    const p = new URLSearchParams({
      project_id: String(projectId),
      limit: String(limit),
    });
    if (status) p.set("status", status);
    return request<AssistantProposalListResponse>(`/assistant/proposals?${p}`);
  },

  /** 批准一条 pending 提案：只改状态，不写任何业务表。 */
  approveProposal: (proposalId: number) =>
    request<AssistantProposalActionResponse>(
      `/assistant/proposals/${proposalId}/approve`,
      jsonInit("POST", { action: "approve" }),
    ),

  /**
   * 应用单条 approved 提案：走单条专用端点（写业务表 + 写 Version）。
   * 409 分两种：目标漂移 detail 是 `{error:"stale", message, hint, ...}`；
   * 状态机拒绝 detail 是字符串（如 pending 直接 apply、重复 apply）。
   */
  applyProposal: (proposalId: number, actor = "ui:workspace") =>
    request<AssistantApplyResult>(
      `/assistant/proposals/${proposalId}/apply`,
      jsonInit("POST", { action: "apply", actor }),
    ),

  /**
   * 受控批量应用：必须显式枚举 id，整批同一事务、同成同败。
   * 任一 stale / blocked → 409，detail 是 `{ok:false, stale[], blocked[], reason}`。
   */
  applySelectedProposals: (proposalIds: number[], actor = "ui:workspace") =>
    request<AssistantApplySelectedResponse>(
      "/assistant/proposals/apply-selected",
      jsonInit("POST", { action: "apply", proposal_ids: proposalIds, actor }),
    ),

  // ---------- 调动生成（后端已合入） ----------

  /**
   * POST /api/assistant/invoke-generation（助手按用户显式指令调动 Writer 生成）。
   * instruction 必填非空；segment_no 仅 regenerate_segment 需要。
   */
  assistantInvokeGeneration: (body: AssistantInvokeGenerationRequest) =>
    request<AssistantInvokeGenerationResult>(
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

  // ---------- 对话式创作引导 ----------

  /** 恢复引导：没有会话时 session=null（前端展示干净的空对话）。 */
  getWizard: (projectId: number) =>
    request<WizardStateResponse>(`/assistant/wizard/${projectId}`),

  /** 开始/恢复：restart=true 会放弃当前会话重新问。 */
  startWizard: (projectId: number, restart = false) =>
    request<WizardStateResponse>(
      "/assistant/wizard/start",
      jsonInit("POST", { project_id: projectId, restart }),
    ),

  /** 提交一步答案；skip=true 表示采用卡片默认值。 */
  answerWizard: (projectId: number, body: WizardAnswerRequest) =>
    request<WizardAnswerResponse>(
      `/assistant/wizard/${projectId}/answer`,
      jsonInit("POST", body),
    ),

  backWizard: (projectId: number) =>
    request<WizardStateResponse>(`/assistant/wizard/${projectId}/back`, jsonInit("POST", {})),

  /** 直接打字：可能被解析成作答，也可能是追问（追问不推进流程）。 */
  sayWizard: (projectId: number, text: string) =>
    request<WizardSayResponse>(
      `/assistant/wizard/${projectId}/say`,
      jsonInit("POST", { text }),
    ),

  /**
   * 撤销一次引导内写操作（档位 / 采样章数 / 字数 / 题材 / 风格 / 大纲 / 流程改写）。
   *
   * 不传 `auditId` = 撤销最近一次可撤销的操作。触发蒸馏不可撤销
   * （后端返回 `undoable:false`），这类操作只能等任务跑完再看结果。
   */
  undoWizard: (projectId: number, auditId?: number | null) =>
    request<WizardSayResponse>(
      `/assistant/wizard/${projectId}/undo`,
      jsonInit("POST", auditId ? { audit_id: auditId } : {}),
    ),

  /**
   * 拉「未消费的蒸馏通知 + 最近一条任务状态」。
   *
   * 一次轮询两用：`distill` 更新卡片里的一句话进度，`items` 是待展示的
   * 完成回执 / 疑似卡住提醒。服务重启后未消费的行仍在库里，所以断线重连、
   * 隔天回来都能补发。
   */
  listWizardNotices: (projectId: number) =>
    request<WizardNoticeListResponse>(`/assistant/wizard/${projectId}/notices`),

  /** 通知已展示给用户，标掉，避免下次进来重复弹。 */
  ackWizardNotice: (projectId: number, noticeId: number) =>
    request<{ ok: boolean; id: number }>(
      `/assistant/wizard/${projectId}/notices/${noticeId}/ack`,
      jsonInit("POST", {}),
    ),

  /**
   * 对话记录（跨退出存活）：进入助手时用它重建历史气泡。
   *
   * `GET /{project_id}` 只给「当前停在哪一题」，本接口给「之前说过什么」。
   * 两者拼起来才是完整对话。
   */
  listWizardMessages: (projectId: number, limit = 200) =>
    request<WizardMessageListResponse>(
      `/assistant/wizard/${projectId}/messages?limit=${encodeURIComponent(String(limit))}`,
    ),

  /** 从汇总卡片跳回已答过的某一步。 */
  gotoWizard: (projectId: number, stepKey: string) =>
    request<WizardStateResponse>(
      `/assistant/wizard/${projectId}/goto`,
      jsonInit("POST", { step_key: stepKey }),
    ),

  commitWizard: (projectId: number) =>
    request<WizardAnswerResponse>(`/assistant/wizard/${projectId}/commit`, jsonInit("POST", {})),

  /** 按已确认的配置开写（复用 generate_loop，串行逐段）。可能跑几分钟。 */
  runWizard: (projectId: number) =>
    request<WizardRunResponse>(`/assistant/wizard/${projectId}/run`, jsonInit("POST", {})),

  /** 角色对话：一次一问，不写正文。 */
  wizardRoleChat: (projectId: number, message: string, history: { role: string; content: string }[]) =>
    request<WizardRoleChatResponse>(
      `/assistant/wizard/${projectId}/role-chat`,
      jsonInit("POST", { message, history }),
    ),

  // ==================================================================
  // 工序链（批 9 后端已落，前端在批 10 接线）
  // ==================================================================
  /** 当前项目的工序链状态（服务端每次重新评估，不是日志）。 */
  getWorkflow: (projectId: number) =>
    request<WorkflowState>(`/projects/${projectId}/workflow`),

  /** 单条提案的完整内容（正文修订要给人看「改哪几句」）。 */
  getProposal: (proposalId: number) =>
    request<AssistantProposalDetailResponse>(`/assistant/proposals/${proposalId}`),

  rejectProposal: (proposalId: number) =>
    request<AssistantProposalActionResponse>(
      `/assistant/proposals/${proposalId}/reject`,
      jsonInit("POST", { action: "reject" }),
    ),

  // —— 动作提案：agent 提「做什么事」，人批了才执行 ——
  listActionProposals: (projectId: number, status?: string | null) =>
    request<ActionProposalListResponse>(
      `/assistant/action-proposals?project_id=${projectId}` +
        (status ? `&status=${encodeURIComponent(status)}` : ""),
    ),

  /** 创建动作提案（卡片确认后原样回传；这一步只记下，不执行）。 */
  createActionProposal: (body: ActionProposalCreateRequest) =>
    request<{ proposal: ActionProposalOut; note: string }>(
      "/assistant/action-proposals",
      jsonInit("POST", body),
    ),

  approveActionProposal: (proposalId: number) =>
    request<{ proposal: ActionProposalOut; next_action: string; note: string }>(
      `/assistant/action-proposals/${proposalId}/approve`,
      jsonInit("POST", { action: "approve" }),
    ),

  /** 执行动作提案：前提漂移会 409（stale），不会带着旧前提硬跑。 */
  applyActionProposal: (proposalId: number) =>
    request<{ proposal: ActionProposalOut; result: Record<string, unknown>; note: string }>(
      `/assistant/action-proposals/${proposalId}/apply`,
      jsonInit("POST", { action: "apply" }),
    ),

  rejectActionProposal: (proposalId: number) =>
    request<{ proposal: ActionProposalOut; note: string }>(
      `/assistant/action-proposals/${proposalId}/reject`,
      jsonInit("POST", { action: "reject" }),
    ),

  // —— 角色对话（第三板块）：产物是对话记录，不进正文主线 ——
  roleplayCharacters: (projectId: number) =>
    request<RoleplayCharactersResponse>(`/projects/${projectId}/roleplay/characters`),

  roleplayChat: (
    projectId: number,
    body: { character: string; question: string; novel_ids?: number[]; k?: number },
  ) =>
    request<RoleplayChatResult>(
      `/projects/${projectId}/roleplay/chat`,
      jsonInit("POST", body),
    ),

  roleplayMessages: (projectId: number, character?: string) =>
    request<{ messages: RoleplayMessage[] }>(
      `/projects/${projectId}/roleplay/messages` +
        (character ? `?character=${encodeURIComponent(character)}` : ""),
    ),
};
