// 与后端 Pydantic schemas 对齐的 TS 类型（手动镜像，改后端时同步此处）。

export interface NovelOut {
  id: number;
  title: string | null;
  author: string | null;
  platform: string | null;
  genre: string | null;
  status: string | null;
}

export interface ChunkOut {
  id: number;
  novel_id: number;
  chapter_no: number | null;
  idx: number;
  text: string;
}

export interface UploadResult {
  novel_id: number;
  title: string | null;
  chapters: number;
  chunks: number;
}

export interface SearchHit {
  chunk_id: number;
  chapter_no: number | null;
  idx: number;
  score: number;
  bm25: number;
  vector: number;
  text: string;
}

export interface ProjectCreate {
  title: string;
  target_length?: number;
  mode?: "quick" | "expert";
  style_dims?: Record<string, unknown> | null;
  outline?: string[] | string | null;
  genre?: string | null;
  style?: string | null;
  premise?: string | null;
  reference_novel_ids?: number[] | null;
  generation_length?: number;
}

export interface ProjectPatch {
  title?: string;
  target_length?: number;
  mode?: "quick" | "expert";
  genre?: string | null;
  style?: string | null;
  premise?: string | null;
  reference_novel_ids?: number[] | null;
  generation_length?: number;
  plot_bible?: Record<string, unknown> | null;
  status?: string | null;
}

export interface PlotBible {
  synopsis?: string | null;
  characters?: { name?: string; role?: string; motivation?: string }[];
  relationships?: { a?: string; b?: string; relation?: string }[];
  direction?: string | null;
  conflicts?: string[];
  foreshadowing?: string[];
  note?: string | null;
}

export interface ProjectOut {
  id: number;
  title: string;
  target_length: number;
  mode: string;
  status: string | null;
  style_dims: Record<string, unknown> | null;
  outline: string[] | string | null;
  created_at: string | null;
  // 向导字段
  genre: string | null;
  style: string | null;
  premise: string | null;
  reference_novel_ids: number[] | null;
  plot_bible: PlotBible | null;
  generation_length: number;
  segment_count: number;
}

export interface KgNode {
  id: number;
  type: string;
  props: Record<string, unknown> | null;
  first_chapter: number | null;
  last_chapter: number | null;
}

export interface KgEdge {
  from: number;
  to: number;
  type: string | null;
  props: Record<string, unknown> | null;
}

export interface Graph {
  nodes: KgNode[];
  edges: KgEdge[];
}

export interface CharacterRelation {
  name: string;
  relation: string | null;
  chapters: number[];
}

export interface CharacterDetail {
  name: string;
  mentions: number | null;
  first_chapter: number | null;
  last_chapter: number | null;
  appearances: number[];
  relations: CharacterRelation[];
}

export interface Reminders {
  unresolved_plotholes: { question: string | null; first_chapter: number | null }[];
  character_constraints: {
    name: string | null;
    mentions: number | null;
    first_chapter: number | null;
    last_chapter: number | null;
  }[];
}

export interface GenerationOut {
  id: number;
  project_id: number;
  segment_no: number;
  chapter_range: string | null;
  text: string | null;
  summary: unknown;
}

export interface PremiseProposal {
  proposals: string[];
  note?: string | null;
}

export interface ExpertQuestions {
  questions: string[];
  note?: string | null;
}

// ---------- 模型设置（LLM 渠道） ----------

/** 模型角色：生成（Writer）与助手（Assistant）严格分开，不共用一份配置。 */
export type LlmRole = "writer" | "assistant";

export interface LlmProviderMeta {
  label: string;
  key_hint: string;
  free_note: string;
  default_models: string[];
}

export interface LlmKeyStatus {
  has: boolean;
  masked: string | null;
}

export interface LlmConfig {
  provider: string;
  model: string;
  keys: Record<string, LlmKeyStatus>;
  providers: Record<string, LlmProviderMeta>;
  effective: {
    provider: string;
    model: string;
    label: string;
    is_fallback: boolean;
  };
  /**
   * 分角色配置（新后端字段）。
   *
   * writer 控制正文生成，assistant 控制问答/诊断/审核——两者严格分开。
   * 旧后端不返回这两个字段：前端据此把界面标成「后端未就绪」并回显
   * 单模型生效值，绝不伪造一份看起来能用的双模型配置。
   */
  writer?: LlmRoleConfig | null;
  assistant?: LlmRoleConfig | null;
}

export interface LlmConfigIn {
  provider: string;
  api_key?: string | null;
  model?: string | null;
  models?: string[] | null;
  /**
   * 分角色保存：带 role 时只保存该角色配置，不带动走旧逻辑（全局单模型）。
   * 旧后端（StrictRequest extra=forbid）会以 422 拒绝未知字段。
   */
  role?: LlmRole;
  base_url?: string | null;
  max_tokens?: number | null;
  timeout?: number | null;
}

/** POST /api/settings/llm/test 请求体（role 可选，只测该角色）。 */
export interface LlmTestIn {
  provider: string;
  api_key?: string | null;
  model?: string | null;
  role?: LlmRole;
}

/**
 * 前端「分角色保存」载荷（RoleModelPanel → ModelSettingsDialog → api）。
 *
 * 按角色只带该角色接受的字段（后端按角色 fail-loud，多带 = 400）：
 *   - writer：带 models（限流切换链）；**不带** max_tokens / timeout
 *     （Writer 的 chat/stream timeout 由生成基线固定，后端不提供改写入口，
 *     GET 的 writer 对象仍如实展示单例真实值）；
 *   - assistant：带 max_tokens / timeout；**不带** models（助手是单模型
 *     配置，没有限流切换链）。
 */
export interface LlmRoleSavePayload {
  role: LlmRole;
  provider: string;
  /** 空 = 不改变该角色已存 key（脱敏输入框的约定）。 */
  api_key: string | null;
  model: string | null;
  /** 仅 writer：候选模型链（限流切换用）。 */
  models?: string[] | null;
  /** 仅 assistant：助手模型的 max_tokens / timeout。 */
  max_tokens?: number | null;
  timeout?: number | null;
}

/** 前端「分角色测试连接」载荷（只测指定角色这一个配置）。 */
export interface LlmRoleTestPayload {
  role: LlmRole;
  provider: string;
  api_key: string | null;
  model: string | null;
}

export interface LlmTestResult {
  ok: boolean;
  model?: string;
  latency_ms?: number;
  reply?: string;
  error?: string;
}

/**
 * GET /api/settings/llm 中单个角色的当前配置（新后端 writer / assistant 字段）。
 *
 * 界面读它展示「实际生效值」；is_fallback=true 时必须文字+色标双编码
 * （「已降级 · 本地兜底」+ warn 色），不能只靠颜色区分。
 */
export interface LlmRoleConfig {
  provider: string;
  model: string;
  /** 脱敏 key（如 "95ef…"）；null = 该角色尚未保存 key。 */
  api_key_masked: string | null;
  has_key: boolean;
  base_url: string;
  max_tokens: number | null;
  timeout: number | null;
  is_fallback: boolean;
  label: string;
}

export interface GeneratePlan {
  reminders: Reminders;
  constraints: { characters: string[]; plotholes: string[] };
  /** 段级摘要链（扁平、无损归档）。**不是**真正下发的那份前情——见 prior_blocks。 */
  prior_summaries: string[];
  /** 真正注入 user_prompt 的有界前情块：[主线] / [卷 k｜段 a-b] / [近情·段 n]。 */
  prior_blocks: string[];
  /** 前情块的构成与覆盖证明（uncovered 恒空 = 无信息空洞；degraded = 退化为全量）。 */
  prior_report: {
    n_segments: number;
    /** 已封满的卷数（L2 存在的前提是整卷的 L1 齐备）。 */
    sealed_volumes: number;
    /** 近情逐段窗口 [起, 止]；null = 还没有任何段。 */
    window: [number, number] | null;
    /** 本次真的渲染进去的卷号。 */
    l2_rendered: number[];
    /** 因预算被丢掉的卷号（丢的前提是已被主线骨架粗覆盖，不会留洞）。 */
    l2_dropped: number[];
    /** 覆盖自证：**恒为空**。非空 = 有洞 = 模块承诺被破坏，必须报红。 */
    uncovered: number[];
    /** 已退化为「全量逐段拼接」（粗层缺失时的兜底，吃预算但不丢信息）。 */
    degraded: boolean;
    /** 超预算（为保覆盖而保留必留层所致，不是 bug 但要看得见）。 */
    over_budget: boolean;
    chars: number;
    budget_chars: number;
    /** 摘要文本丢失（= 占位符）的段数。 */
    missing_l1: number;
    /** 本次是否渲染了主线骨架（只覆盖 ≤1 卷时故意不渲染，避免纯开销）。 */
    spine: boolean;
    /** degraded 时逐段拼了多少段。 */
    flat_segments: number;
  };
  rag_context: string[];
  system_prompt: string;
  user_prompt: string;
  /**
   * 本次下发的文风档案摘要（来自参考语料实测）。
   *
   * 刻意不是完整档案：预检要回答的是「下发的是哪份档案、它可不可信」，
   * 而不是复现统计（完整档案有 20 个特征的分布，约 7KB）。
   */
  style_profile: {
    available: boolean;
    /** available=false 时说明为什么没注入。 */
    reason?: string;
    label?: string;
    /** 拟合粒度。必须与生成长度一致，否则统计粒度不可比。 */
    unit_chars?: number;
    n_units?: number;
    n_train?: number;
    n_holdout?: number;
    /** 拟合前是否做过水印清洗。false 且 marks 非空 = 这些数字是脏的。 */
    cleaned?: boolean;
    source_chars?: number;
    removed_chars?: number;
    /** 一行体检摘要，给人看的。 */
    health_line?: string;
    /** 检出的注入串（判据可能出错，所以带原文样本供人复核）。 */
    marks?: {
      novel: string;
      text: string;
      count: number;
      final: number;
      final_ratio: number;
      samples: string[];
    }[];
    /** 参考语料**人写的**留出单元里，距离 p90。生成段的分位以它为参照。 */
    holdout_p90?: number;
    /** 真正拼进 system 提示的那段文本（确定性，整轮不变）。 */
    text: string;
  };
  /**
   * 规则哨兵的预检摘要：哪几条硬冲突规则**当前真的会响**。
   *
   * 必须显式标注未生效的规则 —— 否则界面看上去像全都在工作，
   * 用户会把「没报错」读成「没问题」，而实际是那条规则根本没参与判定。
   */
  sentinel: SentinelDigest;
}

/** 单条哨兵规则的状态。 */
export interface SentinelRule {
  severity: "err" | "warn";
  desc: string;
  /** false = 当前不会响（原因见 note），不要当成「检查过了没问题」。 */
  active: boolean;
  note: string;
}

export interface SentinelDigest {
  rules: Record<string, SentinelRule>;
  /** 本书生效的境界体系套数（多套之间不可比）。 */
  realm_systems: number;
  realm_words: number;
  /** 是否取自 plot_bible.realm_order。 */
  realm_from_project: boolean;
  realm_default_words: number;
  note: string;
}

/** 一条历史告警（哨兵落库的，按段号倒序取回）。 */
export interface SentinelAlert {
  rule: string;
  severity: "err" | "warn";
  segment_no: number | null;
  /** 冲突指向的实体（角色名 / 物品名）。与生成响应里的 findings 同形状。 */
  entity: string;
  message: string;
  /** 本段的证据位置。 */
  spans: { segment_no: number; span: number[]; text: string }[];
  /** 更早段的反证 —— 告警的说服力全在这里。 */
  evidence: { segment_no: number; span: number[]; text: string; kind: string }[];
}

export interface AlertsResponse {
  items: SentinelAlert[];
  count: number;
  by_severity: { err: number; warn: number };
  rules: Record<string, SentinelRule>;
}

export interface GenerationDebug {
  system_prompt: string;
  user_prompt: string;
  constraints: { characters: string[]; plotholes: string[] };
  rag_context: string[];
}

export interface GenerateResult extends GenerationOut {
  debug: GenerationDebug;
}

export interface GenerateLoopResult {
  segments: (GenerationOut & { summary: unknown })[];
  last_system_prompt: string;
  constraints: { characters: string[]; plotholes: string[] };
}

// ==================================================================
// Assistant（助手面板）
// ------------------------------------------------------------------
// 与 `backend/app/routers/assistant.py` + `assistant_proposals.py` 的
// pydantic 模型逐字段对齐（手动镜像，改后端时同步此处）。
//
// 两条来自后端的硬约束，前端必须自己守住（否则只会拿到一个 422）：
//   1. type ↔ target_kind 一一对应（TYPE_TO_TARGET_KIND）：
//      text_edit → generation_text
//      prompt_edit → prompt_override
//      fact_resolution → project_plot_bible
//   2. scope 里的 base_content_hash / target_snapshot / diff / impact
//      是服务端独占键（SERVER_COMPUTED_SCOPE_KEYS），前端构造请求体时
//      一律不要带这四个键。
// ==================================================================

export type AssistantProposalType = "text_edit" | "prompt_edit" | "fact_resolution";

export type AssistantTargetKind =
  | "generation_text"
  | "project_plot_bible"
  | "prompt_override";

export type AssistantProposalStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "applied"
  | "stale"
  | "rolled_back";

export type AssistantRiskLevel = "low" | "medium" | "high";

/** 诊断 issue 的 severity（后端 ALLOWED_SEVERITIES）。 */
export type AssistantSeverity = "info" | "warn" | "error";

/** Version.kind（后端 VERSION_KINDS）。 */
export type AssistantVersionKind = "text" | "prompt" | "bible" | "mixed";

/** 提案动作。approve / reject / apply 共用同一个请求体形状。 */
export type AssistantProposalAction = "approve" | "reject" | "apply";

/** 可覆写的提示词块键（后端 PromptKey）。 */
export type AssistantPromptKey = "style_suffix" | "constraint_note" | "directive_suffix";

/** 一条证据。evidence.id 只在 /qa、/diagnose 的顶层 evidence 里有；
 *  issue.evidence[]（IssueEvidence.to_dict）不带 id。 */
export interface AssistantEvidence {
  id?: number | null;
  kind: string;
  source: string;
  ref_id: string;
  snippet: string;
  score: number;
}

/** 一条结构化诊断结论（后端 DiagnosticIssue.to_dict，九个字段）。 */
export interface AssistantIssue {
  id: string;
  type: string;
  severity: AssistantSeverity;
  title: string;
  claim: string;
  suggestion: string;
  rule: string;
  evidence: AssistantEvidence[];
}

// ---------- 请求体 ----------

/** POST /api/assistant/qa —— query 必填，scope 可选。 */
export interface AssistantQaRequest {
  project_id: number;
  query: string;
  scope?: string | null;
  version_id?: number | null;
}

/** POST /api/assistant/diagnose —— scope 必填，query 可选。 */
export interface AssistantDiagnoseRequest {
  project_id: number;
  scope: string;
  query?: string | null;
  version_id?: number | null;
}

/**
 * POST /api/assistant/proposals —— 四项必填：
 * project_id / type / target_kind / target_id / after_text。
 *
 * `after_text` 是「提案只能由用户显式指定改什么」的协议落点：
 * 前端**不许**替用户自动生成或预填它，只能由人在输入框里敲。
 */
export interface AssistantProposalCreate {
  project_id: number;
  type: AssistantProposalType;
  target_kind: AssistantTargetKind;
  target_id: string;
  after_text: string;
  reason?: string;
  /** 影响范围；禁止出现服务端独占键。 */
  scope?: Record<string, unknown> | null;
  evidence?: AssistantEvidence[] | null;
  /** 关联诊断 issue 的 id（只贡献 reason / evidence 素材，不决定改哪儿）。 */
  from_issue?: string | null;
  risk_level?: AssistantRiskLevel | null;
  run_id?: number | null;
  created_by?: string | null;
}

/** approve / reject / apply 的请求体（action 必填且必须与路径一致）。 */
export interface AssistantActionRequest {
  action: AssistantProposalAction;
  actor?: string | null;
  note?: string | null;
}

/** POST /api/assistant/versions/{id}/restore 的请求体。 */
export interface AssistantRestoreRequest {
  action: "restore";
  actor?: string | null;
  note?: string | null;
}

// ---------- 响应体 ----------

export interface AssistantQaResponse {
  answer: string;
  evidence: AssistantEvidence[];
  confidence: number;
  agent_run_id: number;
  tool_results: Record<string, unknown>;
  version_id: number | null;
}

export interface AssistantDiagnoseResponse extends AssistantQaResponse {
  issues: AssistantIssue[];
  /** issues 的兼容别名（后端同名同内容）。 */
  findings: AssistantIssue[];
  rules_evaluated: string[];
}

export interface AssistantProposal {
  id: number;
  run_id: number;
  project_id: number;
  type: AssistantProposalType;
  target_kind: AssistantTargetKind;
  target_table: string;
  target_id: string;
  patch: Record<string, unknown> | null;
  status: AssistantProposalStatus;
  risk_level: AssistantRiskLevel;
  reason: string | null;
  before_text: string | null;
  after_text: string | null;
  diff: string;
  impact: string;
  base_content_hash: string | null;
  evidence: AssistantEvidence[];
  version_id: number | null;
  created_by: string | null;
  created_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  applied_at: string | null;
}

export interface AssistantProposalCreateResponse {
  proposal: AssistantProposal;
  next_action: string;
  note: string;
}

export interface AssistantProposalActionResponse {
  proposal: AssistantProposal;
  next_action?: string;
  note?: string;
}

export interface AssistantProposalListResponse {
  project_id: number;
  status: string | null;
  count: number;
  proposals: AssistantProposal[];
}

export interface AssistantProposalDetailResponse {
  proposal: AssistantProposal;
}

/** POST /api/assistant/proposals/{id}/apply 的成功出参。 */
export interface AssistantApplyResult {
  proposal_id: number;
  version_id: number;
  kind: string;
  content_hash: string;
  target_kind: string;
  target_id: string;
  status: string;
  applied_at: string | null;
  proposal: AssistantProposal;
}

export interface AssistantVersion {
  id: number;
  project_id: number;
  proposal_id: number | null;
  source_proposal_id: number | null;
  kind: AssistantVersionKind;
  content_hash: string | null;
  is_restore: boolean;
  target_kind: string | null;
  target_id: string | null;
  before_hash: string | null;
  after_hash: string | null;
  reason: string;
  created_by: string | null;
  created_at: string | null;
}

export interface AssistantVersionListResponse {
  project_id: number;
  kind: string | null;
  count: number;
  versions: AssistantVersion[];
}

export interface AssistantVersionDiffResponse {
  from_version: AssistantVersion;
  to_version: AssistantVersion;
  diff: string;
}

export interface AssistantRestoreResult {
  restored_from_version_id: number;
  version_id: number;
  kind: string;
  content_hash: string;
  target_kind: string;
  target_id: string;
  proposal_id: number | null;
  proposal_status: string | null;
  is_restore: boolean;
  restored_from?: AssistantVersion;
  proposal?: AssistantProposal;
}

export interface AssistantPromptActiveBlock {
  prompt_key: string;
  source: "runtime_override" | "code_default" | string;
  text: string;
  override_id: number | null;
  source_proposal_id: number | null;
  created_at: string | null;
}

export interface AssistantPromptsActiveResponse {
  project_id: number;
  prompt_keys: string[];
  blocks: AssistantPromptActiveBlock[];
  has_override: boolean;
  note: string;
}

export interface AssistantPromptOverrideRow {
  id: number;
  prompt_key: string;
  active: boolean;
  text: string;
  source_proposal_id: number | null;
  created_by: string | null;
  created_at: string | null;
}

export interface AssistantPromptVersionsResponse {
  project_id: number;
  prompt_keys: string[];
  count: number;
  overrides: AssistantPromptOverrideRow[];
}

// ==================================================================
// Assistant 运行日志 / 审核 / 调动生成（后端并行开发中，尚未就绪）
// ------------------------------------------------------------------
// 以下类型是**契约镜像**：另一位工程师正在并行实现这些端点，字段形状
// 以后端实现为准。前端只接好了 api client 方法与类型（见 client.ts），
// UI 不接入、不发送请求、不展示模拟数据——端点就绪后直接接线。
// ==================================================================

/** GET /api/assistant/runs 的一条助手运行日志。 */
export interface AssistantRun {
  id: number;
  project_id: number;
  /** 触发能力：qa / diagnose / analyze-logs / audit-corpus / audit-kg / audit-generation … */
  kind: string;
  /** 后端枚举未定，前端按字符串展示并兜底，不假设取值集合。 */
  status: string;
  query?: string | null;
  scope?: string | null;
  summary?: string | null;
  error?: string | null;
  issue_count?: number | null;
  created_at: string | null;
  finished_at?: string | null;
}

export interface AssistantRunListResponse {
  project_id: number;
  count: number;
  runs: AssistantRun[];
}

/** GET /api/assistant/runs/{run_id}：单条日志详情（含 evidence / issues）。 */
export interface AssistantRunDetailResponse {
  run: AssistantRun;
  evidence?: AssistantEvidence[];
  issues?: AssistantIssue[];
  answer?: string | null;
}

/** 三类审核 + 日志分析的统一 issues 出参（与 diagnose 的 issues 同结构）。 */
export type AssistantAuditKind = "corpus" | "kg" | "generation";

export interface AssistantAuditResponse {
  project_id: number;
  kind: string;
  issues: AssistantIssue[];
  evidence?: AssistantEvidence[];
  answer?: string | null;
  confidence?: number | null;
  agent_run_id?: number | null;
  note?: string | null;
}

/** POST /api/assistant/analyze-logs 请求体。 */
export interface AssistantAnalyzeLogsRequest {
  project_id: number;
  limit?: number | null;
  status?: string | null;
}

/** POST /api/assistant/audit-{corpus,kg,generation} 请求体。 */
export interface AssistantAuditRequest {
  project_id: number;
}

/**
 * POST /api/assistant/invoke-generation 请求体：
 * 助手按用户**显式指令**调动 Writer 生成（指令必须由人填写）。
 */
export interface AssistantInvokeGenerationRequest {
  project_id: number;
  instruction: string;
  max_chars?: number | null;
}

export interface AssistantInvokeGenerationResponse {
  project_id: number;
  instruction: string;
  generation?: GenerationOut | null;
  note?: string | null;
  agent_run_id?: number | null;
}
