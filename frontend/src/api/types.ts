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

/**
 * 一个文风蒸馏档位（GET /api/projects/narrative-tiers 的单档形状）。
 *
 * 字段与后端 `NarrativeTier.to_public_dict()` 逐一对应。**不要在前端硬编码
 * 三档的文案**：档位定义（采样方式/维度数/角色名归一化/伪角色清洗强度）的
 * 单一事实来源在后端 `app.style.narrative.TIER_REGISTRY`，前端另抄一份必然漂移。
 */
export interface NarrativeTier {
  key: string;
  /** 可读名（完整 / 平衡 / 快速）。 */
  name: string;
  /** 一句话说明，含「什么时候用这一档」。 */
  description: string;
  /** "percentage" → 按 sample_ratio 采样；"fixed_chapters" → 固定 fixed_chapters 章。 */
  sampling_mode: string;
  sample_ratio: number | null;
  fixed_chapters: number | null;
  dimension_count: number;
  dimensions: string[];
  /** 是否启用「语料词频自证」的角色名归一化。 */
  normalize_character_names: boolean;
  /** 伪角色清洗强度："full" / "partial" / "none"。 */
  fake_character_cleaning: string;
  /**
   * 耗时/成本提示，**由后端给**（_COST_HINTS）。
   *
   * 是相对档位（最高/中等/最低）而非编造的分钟数：docs 里只有部分语料被实测计时，
   * 给没测过的档位写具体分钟数 = 拿未验证的数字当承诺。后端新增档位时忘记补
   * `_COST_HINTS` 会响亮失败，所以这个字段总是存在。
   */
  cost_hint: string;
}

export interface NarrativeTiersResponse {
  /** 服务端默认档（当前是完整档）。 */
  default: string;
  /** 按「耗时从重到轻」排序：完整 → 平衡 → 快速。 */
  tiers: NarrativeTier[];
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
  /** 文风蒸馏档位（full / balanced / fast）。空 = 用服务端默认档。 */
  narrative_tier?: string | null;
  /** 事实来源书：本项目按哪本书收窄书级事实（物品组/写法/持有）。null = 清空。 */
  active_novel_id?: number | null;
  /** 续写书：正在从哪本书往后写；null = 原创/清空。读侧优先级高于事实来源书。 */
  continuation_novel_id?: number | null;
}

export interface PlotBible {
  synopsis?: string | null;
  characters?: { name?: string; role?: string; motivation?: string }[];
  relationships?: { a?: string; b?: string; relation?: string }[];
  direction?: string | null;
  conflicts?: string[];
  foreshadowing?: string[];
  note?: string | null;
  [key: string]: unknown;  // 实际是 free-form dict；脚本/runtime 扩展字段自由
}

// ============================================================
// plot_bible 级联协议（kg-cascade-protocol）
// ============================================================

/** POST /projects/{id}/bible/apply 响应：revision + diff + 冲突清单。 */
export interface KgBibleConflict {
  kind: "character_role" | "character_goal" | "relationship_type" | "foreshadow_status";
  kg_key: string;
  kg_value: string;
  bible_field: string;
  bible_value: string;
  severity: "warn";
}

export interface KgBibleDiff {
  added: Record<string, unknown>;
  removed: Record<string, unknown>;
  changed: Record<string, { old: unknown; new: unknown }>;
}

export interface KgBibleApplyResult {
  revision_no: number;
  revision_id: number;
  diff: KgBibleDiff;
  conflicts: KgBibleConflict[];
  conflicts_count: number;
  /** 写入但不会被生成链路读取的键（B2）：unsupported / unknown / shape。 */
  warnings?: KgBibleKeyWarning[];
  warnings_count?: number;
}

export interface KgBibleKeyWarning {
  path: string;
  disposition: "unsupported" | "unknown" | "shape";
  message: string;
}

export interface KgBibleRevisionListItem {
  id: number;
  revision_no: number;
  diff: KgBibleDiff;
  conflicts: KgBibleConflict[];
  conflicts_count: number;
  source: string;
  note: string | null;
  created_at: string | null;
}

/**
 * POST /projects/{id}/bible/characters/rename 响应。
 *
 * 拒绝是业务判定（200 + ok=false），不是请求错误；`receipt` 是给人看的原因，
 * `alternatives` 是替代路径。`scope` 是判定依据（正文逐段精确计数）。
 */
export interface KgCharacterRenameScope {
  source: "generations.text";
  segment_count: number;
  last_segment: number | null;
  hits: { segment_no: number; count: number }[];
  segments: number[];
  total: number;
  only_last_segment: boolean;
  summary_occurrences: number;
  threshold: number;
  allowed: boolean;
}

export interface KgCharacterRenameResult {
  ok: boolean;
  decision: "allowed" | "denied";
  old_name: string;
  new_name: string;
  scope: KgCharacterRenameScope;
  receipt: string;
  alternatives: string[];
  bible: KgBibleApplyResult | null;
  entity: {
    updated: boolean;
    entity_id: number | null;
    canonical_name: string;
    aliases: string[];
  } | null;
}

/**
 * 角色编辑三入口（加 / 删 / 改境界）的响应。
 *
 * `receipt` 是人话回执；`bible` 是 apply 的结果（revision + diff + 冲突 + 键告警）。
 * `roster_empty` 为 true 时表示清册已删空（B3 语义：提示词会剔除所有旧图谱角色）。
 */
export interface KgCharacterEditResult {
  ok: boolean;
  action: "add" | "remove" | "set_realm";
  name: string;
  receipt: string;
  roster_size?: number;
  roster_empty?: boolean;
  relationships_removed?: number;
  field?: "境界" | "修炼.当前境界";
  validated?: boolean;
  bible: KgBibleApplyResult;
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
  /** 项目已落库的蒸馏档位；null = 尚未选过（后端按默认档跑）。 */
  narrative_tier: string | null;
  /** 已落库文风档案（蒸馏库「应用」写入的单槽缓存）；null = 尚未蒸馏/应用。 */
  style_narrative?: Record<string, unknown> | null;
  /** 事实来源书（NULL = 未指定，读侧不按书过滤）。 */
  active_novel_id: number | null;
  /** 续写书（NULL = 原创）。 */
  continuation_novel_id: number | null;
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

/**
 * 模型角色：生成（Writer）/ 助手（Assistant）/ 蒸馏（Distiller）三者严格分开，
 * 不共用一份配置。
 *
 * ⚠️ 此处的 distiller 指「文风档案抽取」用的 LLM 角色，与 `NarrativeTier`
 * 的三档「蒸馏档位」（full/balanced/fast，采样/计算档位）不是一回事。
 */
export type LlmRole = "writer" | "assistant" | "distiller";

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
  distiller?: LlmRoleConfig | null;
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
 * 前端「分角色保存」载荷（RoleModelPanel → LlmSettings 视图 → api）。
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
  /**
   * 自定义端点：仅「通用 OpenAI 接口」渠道需要（覆盖该渠道默认 base_url）。
   * 其余渠道留空/null = 用后端该渠道的默认地址。
   */
  base_url?: string | null;
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
// KG 簿记统计（⚠️ 两种形状，别混为一谈）
// ------------------------------------------------------------------
// 与后端 `app.kg.build` 逐字段对齐（手动镜像，改后端时同步此处）。
//
// ⚠️ 两个流式端点的 `done.kg` **不是同一形状**：
//  - `/regenerate-stream` 与 `POST /projects/{id}/build-kg-from-generations`
//    → `rebuild_generated_kg()` → `KgRebuildResult`（rebuilt/complete/…）；
//  - `/generate-stream` → `merge_segment_into_kg()`（经 engine `_merge_segment_into_graph`）
//    → `KgMergeResult`（merged/partial/ledger/aliases/sentinel/…），**没有**
//    rebuilt/complete。
//
// 旧形状 `{characters, plotholes, relationships, chunks}` 已废弃 —— 界面若照旧
// 形状读会渲染出一串 `undefined`。
// ==================================================================

/** 单段 replay 失败的记录（`KgRebuildResult.failed` 数组元素）。 */
export interface KgReplayFailure {
  /** 未完成的段号。 */
  segment_no: number;
  /** 失败原因（后端可能为 null）。 */
  reason?: string | null;
}

/**
 * 「生成后 KG 全量重建」的簿记统计（`rebuild_generated_kg`）。
 *
 * 来源：`POST /build-kg-from-generations`，以及 `/regenerate-stream` 的 `done.kg`。
 *
 * - `rebuilt` 语义 = 操作跑过（即使有失败段也常为 true）；**`rebuilt === false`
 *   只在整体重建抛异常时出现**（引擎兜底分支），此时不带 segments/replayed/failed，
 *   界面必须走「簿记重建失败」文案，不能套「N/M 段成功」那套（会渲染成
 *   `0/0 段成功，段  未完成` 的残句）。
 * - `complete` 语义 = 无失败段（`= failed == []`）—— 这才是「是否簿记完整」。
 *   界面判据用 `complete !== false`（旧后端不返回该键时视为成功，不误报）。
 */
export interface KgRebuildResult {
  rebuilt?: boolean;
  complete?: boolean;
  segments?: number;
  replayed?: number;
  failed?: KgReplayFailure[];
  source?: string;
  /** 仅当整体重建抛异常时出现（引擎兜底分支 `{"rebuilt": false, "reason": ...}`）。 */
  reason?: string | null;
}

/**
 * 「单段并入 KG」的簿记统计（`merge_segment_into_kg`）。
 *
 * 来源：`/generate-stream` 的 `done.kg`。**没有** rebuilt/complete —— 别拿
 * rebuild 那套判据套到它身上（会静默漏报）。两种失败信号：
 * - 图谱步（步骤 1）整体失败 → `merged === false`（附带 `reason`）；
 * - 账本/结算/哨兵步（步骤 2/3/4）失败 → 顶层 `merged` 仍为 true，但 `partial === true`。
 */
export interface KgMergeResult {
  merged?: boolean;
  /** true = 四步只走了一半（步骤 2/3/4 有失败），`merged` 仍可能为 true。 */
  partial?: boolean;
  /** 仅当图谱步整体失败（`merged === false`）时出现。 */
  reason?: string | null;
  project_id?: number;
  segment_no?: number;
  source?: string;
  new_characters?: number;
  updated_characters?: number;
  new_plotholes?: number;
  new_relationships?: number;
  resolved_plotholes?: number;
  resolved_holes?: unknown[];
  /** 账本步统计；失败时为 `{error: "..."}`。 */
  ledger?: Record<string, unknown> | null;
  /** 别名归一步统计；失败时键缺失。 */
  aliases?: Record<string, unknown> | null;
  /** 规则哨兵统计；失败时为 `{error: "...", findings: []}`。 */
  sentinel?: Record<string, unknown> | null;
}

/** 流式 `done.kg` 的两种形状联合（判据函数按字段互斥读取）。 */
export type KgStatus = KgMergeResult | KgRebuildResult;

/**
 * `/generate-stream` 的 `done` 事件负载（`kg` = merge 形状）。
 *
 * `kg` 是纯新增键：既有字段语义一个都没改。挂簿记统计是为了让**流式路径**
 * 也能被观测 —— 此前流式完全不做归档，出了问题是「图谱没建」还是「建了一半」
 * 无从判断。
 */
export interface GenerateStreamDoneMeta {
  segment_no: number;
  chars: number;
  summary: string;
  id: number;
  /** KG 合并统计（merge 形状）；旧后端不返回 → undefined → 不提示（保守，不误报）。 */
  kg?: KgMergeResult;
  /**
   * 叙事归档树（后端 `done` 实际发射的 `summary_tree`，dict）。
   * 旧后端不返回 → undefined → 不提示（保守，不误报）。
   */
  summary_tree?: Record<string, unknown>;
  /**
   * 文风告警（后端 `done` 实际发射的 `style_warning`，dict | None）。
   * 旧后端不返回 / 返回 null → undefined / null → 不提示。
   */
  style_warning?: Record<string, unknown> | null;
  /**
   * 证据重写信息（后端 `done` 实际发射的 `rewrite_info`，dict | None）。
   * 旧后端不返回 / 返回 null → undefined / null → 不提示。
   */
  rewrite_info?: Record<string, unknown> | null;
}

/**
 * `/regenerate-stream` 的 `done` 事件负载（`kg` = rebuild 形状，与
 * `/build-kg-from-generations` 同形）。
 */
export interface RegenerateStreamDoneMeta {
  segment_no: number;
  chars: number;
  summary: string;
  id: number;
  /** KG 全量重建统计（rebuild 形状）；旧后端不返回 → undefined → 不提示。 */
  kg?: KgRebuildResult;
  /**
   * 叙事归档树（后端 `done` 实际发射的 `summary_tree`，dict）。
   * 旧后端不返回 → undefined → 不提示（保守，不误报）。
   * 注意：regen 路径不发射 `style_warning`（未做文风距离计算）；`rewrite_info` 仅作 AI 腔告警信号（命中时 `alert:true`、不重写），见 `engine.py` 的 `regenerate_segment_stream`。
   */
  summary_tree?: Record<string, unknown>;
  /**
   * 零成本 AI 腔告警（后端 `done` 实际发射的 `rewrite_info`，dict | None）。
   * regen 路径只检测 + 告警、不做自动重写：命中时 `alert:true` 且 `rewritten_chars:null`。
   * 旧后端不返回 → undefined → 不提示（保守，不误报）。
   */
  rewrite_info?: Record<string, unknown> | null;
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

// ==================================================================
// 待确认卡片（对话流内的写操作闸门）
// ------------------------------------------------------------------
// agent 需要写操作时不能自己动手，只能在回答里附一张「待确认卡片」，
// 由用户点确认后才真正执行。这组类型是**服务端下发的契约**，前端只
// 渲染 + 原样回传，绝不自己拼请求体 —— 否则前后端各拼一半必然漂移
// （服务端加一个必填字段，前端就会漏传，且漂移表现为运行期 422）。
// ==================================================================

/** 卡片类型：新建提案 / 调动生成。 */
export type AssistantPendingActionKind = "proposal" | "invoke_generation";

/**
 * 一张待确认卡片。
 *
 * 五个字段缺一不可：`title` 给用户看，`detail` 给人读，`endpoint` +
 * `method` + `body` 给机器执行 —— 确认按钮按下时三者原样回传，
 * 前端不参与请求构造。
 */
export interface AssistantPendingAction {
  kind: AssistantPendingActionKind;
  /** 一句话说明要做什么（用户「一眼看出」的唯一依据）。 */
  title: string;
  /** 渲染用详情；字段形状由服务端定义，前端按键值对渲染，未知键也照显。 */
  detail: Record<string, unknown>;
  /** 确认后实际调用的端点（如 "/api/assistant/proposals"）。 */
  endpoint: string;
  /** HTTP 方法（当前只有 "POST"）。 */
  method: string;
  /** 完整请求体，原样回传。 */
  body: Record<string, unknown>;
}

// ---------- 响应体 ----------

export interface AssistantQaResponse {
  answer: string;
  evidence: AssistantEvidence[];
  confidence: number;
  agent_run_id: number;
  tool_results: Record<string, unknown>;
  version_id: number | null;
  /**
   * agent 自主调度诊断 / 日志分析 / 三类审核后产出的结构化 issues。
   * 旧后端不返回这一组字段 → undefined → 界面整段不渲染（不留空态）。
   */
  issues?: AssistantIssue[];
  /** issues 的兼容别名（与 diagnose 同构）。 */
  findings?: AssistantIssue[];
  /** 本次实际执行过的规则名。 */
  rules_evaluated?: string[];
  /** 待用户确认的写操作卡片；旧后端不返回 → [] → 整段不渲染。 */
  pending_actions?: AssistantPendingAction[];
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
// Assistant 运行日志 / 审核 / 调动生成（后端已合入，第 7 步交付 2/3/4）
// ------------------------------------------------------------------
// 与 backend/app/routers/assistant.py（runs/analyze-logs）、
// assistant_audit.py（audit-*）、assistant_generation.py
// （invoke-generation）的 pydantic 模型逐字段对齐。
// ==================================================================

/** GET /api/assistant/runs 列表项（run_history_to_dicts 出参）。 */
export interface AssistantRun {
  id: number;
  project_id: number;
  /** 这些端点都以助手身份调用，固定 "assistant"。 */
  role: string;
  /** running | success | failed（后端 _RUN_STATUSES）。 */
  status: string;
  /** 该次调用实际使用的模型；本地兜底时 "(local_fallback)"。 */
  model: string;
  note: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** 毫秒；未结束的 run 为 null。 */
  duration_ms: number | null;
  request_hash: string | null;
  evidence_count: number;
}

export interface AssistantRunListResponse {
  runs: AssistantRun[];
  count: number;
  by_status: { running: number; success: number; failed: number };
  status_filter: string | null;
  limit: number;
}

/** GET /api/assistant/runs/{run_id}：单条详情 + 该 run 的全部 Evidence。 */
export interface AssistantRunDetailResponse {
  run: AssistantRun;
  evidence: AssistantEvidence[];
}

/** POST /api/assistant/analyze-logs 请求体（只读；scope/version_id 占位）。 */
export interface AssistantAnalyzeLogsRequest {
  project_id: number;
  query?: string | null;
  scope?: string | null;
  version_id?: number | null;
  /** 分析范围过滤；缺省全部（running 的历史也纳入统计口径）。 */
  status?: string | null;
  /** 取数上限（后端合法区间 [1, 500]）。 */
  limit?: number;
}

/** 三类审核的 kind（audit-corpus / audit-kg / audit-generation）。 */
export type AssistantAuditKind = "corpus" | "kg" | "generation";

/** POST /api/assistant/audit-corpus 请求体（只读）。 */
export interface AssistantAuditCorpusRequest {
  project_id: number;
  /** 缺省 = 全库语料；显式列表 = 只审这些（不存在的 id → 404）。 */
  novel_ids?: number[] | null;
  query?: string | null;
  version_id?: number | null;
}

/** POST /api/assistant/audit-kg / audit-generation 请求体（只读）。 */
export interface AssistantAuditRequest {
  project_id: number;
  query?: string | null;
  version_id?: number | null;
}

/**
 * 审核 / 日志分析的统一出参：issues 与 diagnose 的九字段结构同构
 * （findings 是后端保留的兼容别名，同内容），前端一份渲染两处复用。
 */
export interface AssistantAuditResponse {
  answer: string;
  issues: AssistantIssue[];
  findings: AssistantIssue[];
  evidence: AssistantEvidence[];
  confidence: number;
  agent_run_id: number;
  tool_results: Record<string, unknown>;
  rules_evaluated: string[];
  version_id: number | null;
  /** 仅 analyze-logs：本次分析覆盖的历史 run 条数。 */
  runs_analyzed?: number;
}

/** invoke-generation 的两种动作（Literal，传别的值 → 422）。 */
export type AssistantGenerationAction = "generate_next" | "regenerate_segment";

/**
 * POST /api/assistant/invoke-generation 请求体。
 *
 * `instruction` 是产品约束的协议落点：用户显式指令原文，非空——
 * 没有它后端直接 422。「生成」在 API 层因此是一次可追溯的用户动作。
 * `segment_no` 仅 regenerate_segment 必填；generate_next 带了反而 422。
 */
export interface AssistantInvokeGenerationRequest {
  project_id: number;
  action: AssistantGenerationAction;
  segment_no?: number | null;
  /** 缺省用 project.generation_length（与 /api/generate 同口径）。 */
  max_chars?: number | null;
  instruction: string;
}

/** POST /api/assistant/invoke-generation 响应。 */
export interface AssistantInvokeGenerationResponse {
  answer: string;
  agent_run_id: number;
  action: AssistantGenerationAction;
  /** Writer 实际使用的模型（本地兜底时 "(local_fallback)"）。 */
  writer_model: string;
  /** 与 GET /api/generations/{pid} 同形状，可复用渲染。 */
  segment: GenerationOut;
  chars: number;
  /** generate_next 另带；regenerate 只带 summary / summary_tree。 */
  summary?: string;
  kg_update?: Record<string, unknown>;
  summary_tree?: Record<string, unknown>;
  debug?: {
    system_prompt: string;
    user_prompt: string;
    constraints: Record<string, unknown>;
    rag_context: string[];
  };
}

// ---------- 文风蒸馏后台任务（进度条） ----------

/**
 * 蒸馏任务的四种状态。
 *
 * 流转：`pending → running → success | failed`。**没有任何其它组合**——
 * 后台任务要么给结论，要么给失败原因，绝不永远停在 running（那是「进度条
 * 一直转但不报错」，比报错更难排查）。
 */
export type DistillStatus = "pending" | "running" | "success" | "failed";

/**
 * 观测态：把 status + 时间戳派生成用户看得懂的五个结论。
 *
 * 它**不入库**——DB 的状态机仍是 pending → running → success | failed。
 * `stalled` 是「按当前时间算出来」的观感，同一行可能在 running / stalled 之间
 * 来回变（进度又动了就恢复 running），所以不能持久化。
 */
export type DistillHealth = "queued" | "running" | "stalled" | "success" | "failed";

/**
 * 一次文风蒸馏任务的状态快照（GET .../distill/jobs/{job_id} 的响应）。
 *
 * 字段与后端 `app.style.jobs.distill_run_to_dict()` 逐一对应。
 *
 * ## 进度为什么在这里而不在响应体里 Streaming
 *
 * 833 章语料跑完整档实测 42 分钟，蒸馏必须能**离开页面继续跑**：关标签页、
 * 刷新、切步骤都不该丢进度。所以进度落在 DB（`distill_runs` 表），前端轮询，
 * 而不是挂在某条 HTTP 连接的 SSE 上——那条连接一断就什么都没了。
 */
export interface DistillJob {
  id: number;
  project_id: number;
  /** 本次实际使用的档位 key（full/balanced/fast）。 */
  tier: string | null;
  status: DistillStatus;
  /** 人话阶段名，如「语料 2/3：提炼第 5/12 块」，直接显示给用户。 */
  stage: string | null;
  /** 已完成块数。 */
  progress_current: number;
  /** 总块数。**0 表示总数还没探明**，此时必须渲染不确定态，不能拿 0 当分母。 */
  progress_total: number;
  /** 成功后回填的摘要：实际采样章节数 / 规则条数 / 被拒规则数。失败时保持 null。 */
  n_chapters: number | null;
  rule_count: number | null;
  rejected: number | null;
  /** failed 时的原因；其它状态为 null。 */
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  /**
   * 最后一次进度心跳（UTC ISO）。看护判据「多久没有进度更新」的唯一时间源；
   * null = 老任务还没写过心跳，按 started_at / created_at 兜底。
   */
  progress_at?: string | null;
  /**
   * 提交时按语料字数外推的预计耗时（分钟）。**不是实测值**，界面必须标注；
   * null = 算不出来（此时只做「无进度时长」判断）。
   */
  expected_minutes?: number | null;
  /** 已运行分钟数（未结束时按「现在」算）。 */
  elapsed_minutes?: number | null;
  /** 派生观测态；老接口没有这个字段时按 status 兜底。 */
  health?: DistillHealth;
  /**
   * 一句话判断（带具体分钟数，如「已运行 12 分钟，预计约 122 分钟（按字数
   * 外推，非实测）」）。stalled 时说明卡在哪一步、建议等还是重来。
   */
  health_note?: string | null;
}

/** POST /api/projects/{id}/distill 的响应。 */
export interface DistillStartResponse {
  job_id: number;
  status: DistillStatus;
  tier: string | null;
  /**
   * true = 复用了该项目正在跑的同档位任务，没有新起一条。
   *
   * 后端做并发防重：连点两次「开始蒸馏」不该烧两遍 LLM。有了这个标志，
   * 界面可以如实说「已在运行中，已切到那条进度」，而不是假装新开了一个。
   */
  reused: boolean;
  /**
   * true = 本次蒸馏会直接复用「项目已存档案」或「蒸馏库」，不重新调用模型、
   * 秒级返回。前端据此如实提示「已复用缓存、未重新蒸馏」，避免「一秒蒸馏完却没解释」。
   * 仅在不 force 时可能为 true。
   */
  from_cache?: boolean;
  /**
   * 缓存来源：`"project"` = 复用本项目已缓存的文风档案；
   * `"library"` = 复用跨项目蒸馏库里的同语料+同档位条目。force 时为 undefined。
   */
  cache_source?: "project" | "library" | null;
}

/** GET /api/projects/{id}/distill/jobs 的响应（id 降序）。 */
export interface DistillJobListResponse {
  items: DistillJob[];
}

// ---------- 文风蒸馏库（跨项目复用） ----------

/**
 * 蒸馏库条目的两种状态。
 *
 * - `active`：可复用（默认，出现在「可复用」列表）。
 * - `archived`：用户主动归档，不再被复用，但保留供历史查阅。
 */
export type StyleDistillStatus = "active" | "archived";

/**
 * 蒸馏库条目的摘要（GET /api/projects/style-distills 列表项）。
 *
 * 与后端 `_sd_summary()` 逐一对应。**不含** style_narrative 全文与 rules——
 * 那份 JSON 有几万字符，列表场景用不到。
 */
export interface StyleDistillSummary {
  id: number;
  /** 参考语料 novel_id 列表（与项目 reference_novel_ids 同口径）。 */
  source_ids: number[] | null;
  tier: string;
  /** 蒸馏程度的可读名：完整 / 平衡 / 快速（由后端按 tier key 解析）。 */
  tier_name?: string | null;
  tier_signature: string;
  content_hash: string;
  narrative_version: number;
  status: StyleDistillStatus;
  n_chapters: number | null;
  rule_count: number | null;
  rejected: number | null;
  created_by_project_id: number | null;
  created_at: string | null;
  applied_at: string | null;
}

/**
 * 蒸馏库条目详情（GET /api/projects/style-distills/{id}）。
 *
 * 在摘要基础上多了 `style_narrative` 全文——应用/查看时才需要。
 */
export interface StyleDistillDetail extends StyleDistillSummary {
  style_narrative: Record<string, unknown> | null;
}

/** GET /api/projects/style-distills 的响应。 */
export interface StyleDistillListResponse {
  items: StyleDistillSummary[];
}

/** POST /api/projects/{id}/style-distills/apply 的请求体。 */
export interface StyleDistillApplyRequest {
  distill_id: number;
}

// ============================================================
// KG v3.0（9 表读侧 + Judge + 豁免）
// ============================================================

/** GET /kg-v3/slice 返回的切片（generate 注入 prompt 的同款数据）。 */
export interface KgV3Slice {
  recent_entities: { name: string; kind: string; context: string; mention_count: number }[];
  character_states: { character: string; state_type: string; new_value: string }[];
  active_foreshadows: {
    name: string;
    status: string;
    planted_chapter: number | null;
    planned_payoff_chapter: number | null;
  }[];
  known_facts: { character: string; knows: string[] }[];
  recent_events: { summary: string; event_type: string; in_story_date: string | null }[];
}

/** GET /kg-v3/events 的事件与因果链。 */
export interface KgV3EventsResponse {
  events: {
    sequence: number;
    chapter: number | null;
    segment_no: number | null;
    event_type: string;
    summary: string;
    in_story_date: string | null;
  }[];
  links: { from_event_id: number; to_event_id: number; link_type: string }[];
}

/** GET /kg-v3/judge/findings 的单条告警。 */
export interface KgV3JudgeFinding {
  rule: string;
  severity: "err" | "warn";
  segment_no: number | null;
  entity: string;
  message: string;
  evidence: string[];
  exempted?: boolean;
}

/** GET /kg-v3/judge/findings 响应（与哨兵 /alerts 同形状）。 */
export interface KgV3JudgeFindingsResponse {
  items: KgV3JudgeFinding[];
  count: number;
  by_severity: { err: number; warn: number };
  rules: Record<string, { severity: string; desc: string }>;
}

/** GET /kg-v3/characters 的单条角色。 */
export interface KgV3Character {
  id: number;
  name: string;
  mention_count: number;
  has_secrets: boolean;
}

/** GET /kg-v3/relations 的单条关系边。 */
export interface KgV3Relation {
  from: string;
  to: string;
  from_kind: string;
  to_kind: string;
  rel_type: string;
  strength: number;
}

/** POST /kg-v3/exemptions 请求体。 */
export interface KgV3ExemptRequest {
  finding_id: number | null;
  rule: string;
  entity: string;
  note: string;
}

/** v2.1：GET /kg-v3/style-warnings 响应（落库告警历史）。 */
export interface KgV3StyleWarning {
  segment_no: number;
  generated_at: string;
  percentile: number | null;
  mean_abs_z: number | null;
  worst_features: string[];
  warn_message: string | null;
  /** v2.14 方案 B：方案 B「对数粒度 profile」选了哪个 bucket（"short"/"medium"/"long"/"v1"）。 */
  bucket?: string | null;
  /** v2.14：text 长度相对 base_unit_chars 的比值（粒度对齐信号）。 */
  scale_ratio?: number | null;
  /** v2.14：生成段字符数。 */
  chars?: number | null;
}
export interface KgV3StyleWarningsResponse {
  warnings: KgV3StyleWarning[];
  total: number;
  truncated: boolean;
}

// ======================================================================
// 对话式创作引导（/api/assistant/wizard/*）
// ======================================================================

/** 卡片选项值：单选用字符串/数字，确认题用布尔。 */
export type WizardValue = string | number | boolean;

export interface WizardOption {
  value: WizardValue;
  label: string;
  desc?: string;
}

/** 汇总卡片的一行；step 让前端能跳回该步修改。 */
export interface WizardSummaryRow {
  step: string;
  label: string;
  value: string;
}

/**
 * 一步一张卡片。字段随 type 变化，后端只下发当前类型需要的键：
 * single / multi 用 options，number 用 min/max/step/quick，
 * text 用 placeholder/suggestions，confirm 用 options 或 summary。
 */
export interface WizardCard {
  type: "single" | "multi" | "number" | "text" | "confirm";
  field: string;
  options?: WizardOption[];
  default?: unknown;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  quick?: number[];
  placeholder?: string;
  suggestions?: string[];
  /** 多选里的互斥项（例如「不投喂」= 0）。 */
  exclusive_value?: WizardValue;
  empty_hint?: string;
  summary?: WizardSummaryRow[];
  action_label?: string;
}

export interface WizardQuestion {
  step: string;
  title: string;
  why: string;
  /** 这一步的对话正文：在问什么、各选项代价、默认值与推荐。卡片只是快捷入口。 */
  message?: string;
  progress: { index: number; total: number };
  can_back: boolean;
  can_skip: boolean;
  card: WizardCard;
}

export interface WizardSessionView {
  id: number;
  kind: string | null;
  step_key: string;
  status: "active" | "committed" | "aborted";
  answers: Record<string, unknown>;
  result: WizardCommitResult | null;
  can_back: boolean;
}

export interface WizardCommitSegment {
  segment_no: number | null;
  chars: number;
  excerpt: string;
}

export interface WizardCommitResult {
  kind: string;
  stage: "outline" | "ready" | "role" | "done";
  applied: string[];
  message?: string;
  distill?: Record<string, unknown> | null;
  bible?: Record<string, unknown> | null;
  character?: string;
  scene?: string;
  segments?: WizardCommitSegment[];
}

export interface WizardStateResponse {
  session: WizardSessionView | null;
  question: WizardQuestion | null;
  /** 最近一条蒸馏任务（进度 + 看护结论）；没有任务时为 null。 */
  distill?: DistillJob | null;
  project_hint?: {
    id: number;
    title: string;
    genre: string | null;
    style: string | null;
    generation_length: number;
    has_corpus: boolean;
    has_style_archive: boolean;
  };
}

/**
 * 一条持久化的蒸馏通知（`wizard_notices` 表的一行）。
 *
 * 为什么落库：完成回执 / 疑似卡住提醒必须能跨刷新、跨服务重启被再次捡起。
 * 前端轮询 `GET /api/assistant/wizard/{project_id}/notices`，拿到未消费的行就
 * 渲染成助手气泡，然后调 ack 标掉。
 */
export interface WizardNotice {
  id: number;
  /** distill_done（完成回执）/ distill_failed / distill_stalled（疑似卡住）。 */
  kind: "distill_done" | "distill_failed" | "distill_stalled";
  job_id: number;
  payload: WizardNoticePayload;
  created_at: string | null;
}

/** 通知正文（后端已渲染好 `message`，前端不再拼数字）。 */
export interface WizardNoticePayload {
  job_id?: number;
  tier?: string | null;
  tier_name?: string | null;
  status?: string;
  n_chapters?: number | null;
  rule_count?: number | null;
  rejected?: number | null;
  failed_chunks?: number | null;
  n_dimensions?: number | null;
  elapsed_seconds?: number | null;
  elapsed_text?: string | null;
  interrupted?: boolean;
  health?: DistillHealth;
  stage?: string | null;
  progress_current?: number | null;
  progress_total?: number | null;
  expected_minutes?: number | null;
  elapsed_minutes?: number | null;
  /** 已渲染好的人话正文（含「处理 N 章 / N 个维度 / 实际用时 …」）。 */
  message?: string;
  /** 「接着问：要现在开始写吗？」——完成/失败后的下一步问句。 */
  next?: string;
  /** 中断回执专用：还有已完成的块可复用，前端据此显示「继续蒸馏」按钮。 */
  resumable?: boolean;
  /** 中断时已落库的完成块数 / 总数（续跑进度）。 */
  completed_chunks?: number;
  total_chunks?: number;
}

/**
 * 一条对话记录（`assistant_messages` 表的一行）。
 *
 * 为什么要有它：`GET /api/assistant/wizard/{id}` 只恢复「当前停在哪一题」，
 * 之前说过的话全在前端内存里，刷新就没了。进入助手时先用本接口重建历史气泡，
 * 再拼上当前题卡。历史里的题卡一律只读（不能重新提交）。
 */
export interface WizardMessage {
  id: number;
  session_id: number | null;
  role: "user" | "assistant";
  kind:
    | "plain"
    | "choice"
    | "wizard_question"
    | "wizard_action"
    | "wizard_result"
    | "notice"
    | "role_chat";
  text: string;
  payload: {
    step?: string;
    question?: WizardQuestion;
    result?: WizardCommitResult;
    action?: string;
    undo_id?: number | null;
    undoable?: boolean;
    character?: string;
    scene?: string;
    /** kind=notice 时是通知正文，形状同 WizardNoticePayload。 */
    [key: string]: unknown;
  } | null;
  created_at: string | null;
}

export interface WizardMessageListResponse {
  items: WizardMessage[];
  count: number;
}

/** GET .../notices 的响应：未消费通知 + 最近一条任务（一次轮询双用途）。 */
export interface WizardNoticeListResponse {
  items: WizardNotice[];
  distill: DistillJob | null;
}

export interface WizardAnswerRequest {
  step_key: string;
  value?: unknown;
  skip?: boolean;
}
export interface WizardAnswerResponse {
  session: WizardSessionView;
  question: WizardQuestion | null;
  /** 答完这一题的回执（已拼进 question.message 开头，这里单独给一份备查）。 */
  ack?: string;
  done?: boolean;
  result?: WizardCommitResult;
}

/**
 * 一次写操作的审计回执（后端 `wizard_actions._ok` 的 `action` 字段）。
 *
 * 与 `WizardActionAudit` 表一一对应：谁（actor 固定 assistant）、哪一步、
 * 动作、旧值、新值、影响。`old` / `new` 是 JSON 原样，可能是数字、字符串、
 * 对象或 null —— 前端只做展示，不参与解释。
 */
export interface WizardActionEcho {
  id: number | null;
  action: string;
  target: string;
  old: unknown;
  new: unknown;
  impact: string | null;
}

/**
 * 自由输入的结果：作答 / 就地解释（不推进）/ 没听懂 / 回退 / 执行了一次写操作。
 *
 * `mode="action"` 是「助理受控写权限」的落点：不推进引导步骤，只改项目级
 * 配置或本次会话的流程改写，回执带前后值与可撤销 id。
 */
export interface WizardSayResponse {
  mode: "answered" | "explain" | "clarify" | "action";
  message?: string;
  ack?: string;
  /** 解析出的选项文案（收起卡片时显示「已选：3000 字」）。 */
  echo?: string;
  session: WizardSessionView;
  question: WizardQuestion | null;
  done?: boolean;
  result?: WizardCommitResult;
  /** 仅 mode="action"：这次改了什么（旧值 → 新值 + 影响）。 */
  action?: WizardActionEcho | null;
  /** 仅 mode="action" 且可撤销：审计行 id，传给撤销接口。 */
  undo_id?: number | null;
  /** 本次写操作是否可撤销（触发蒸馏这类不可撤销的为 false）。 */
  undoable?: boolean;
}

export interface WizardRunResponse {
  done: boolean;
  result: WizardCommitResult;
  segments: WizardCommitSegment[];
}

export interface WizardRoleChatResponse {
  answer: string;
  character: string;
  scene: string;
}
