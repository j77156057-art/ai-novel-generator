// 与后端 Pydantic schemas 对齐的 TS 类型（手动镜像，改后端时同步此处）。

/** 入库时的语料清洗报告（GET /api/novels 的 clean_report）。 */
export interface CorpusCleanReport {
  /** 洗掉的注入水印串（每条含文本与出现次数）。 */
  marks?: Array<{ text: string; count: number }>;
  /** 剔除总字数。 */
  removed_chars?: number;
  /** 清洗前后字数。 */
  chars_in?: number;
  chars_out?: number;
  /** 清洗是否改动了内容。 */
  cleaned?: boolean;
  /** 「混进来的别的书」的体检结论（盗版站的 txt 常把多本书拼在一起）。 */
  foreign_blocks?: ForeignBlocksReport;
  /** 其它体检维度（原文缺失等），按后端实际写入的键展示。 */
  [key: string]: unknown;
}

/**
 * 「混进了别的书」的检出结论。
 *
 * 命中的是**人物聚集度 + 排他性**（出现集中、且别处见不到的人物名），
 * 不是风格离群 —— 风格离群会把作者自己的章节也判成外来。
 */
export interface ForeignBlocksReport {
  /** 是否检出。 */
  detected: boolean;
  /** 检出块数。 */
  block_count: number;
  /** 涉及字数。 */
  chars: number;
  /** 占比（0~1）。 */
  ratio: number;
  /** 是否已剔除（入库时显式开启过剔除才会为 true）。 */
  dropped?: boolean;
  /** 实际剔除字数。 */
  removed_chars?: number;
  /** 逐块明细。 */
  blocks?: Array<{
    /** 该块的起止字符位置。 */
    start: number;
    end: number;
    /** 该块字数。 */
    chars: number;
    /** 该块占全书比例（0~1）。 */
    ratio: number;
    /** 该块含几章。 */
    chapter_count: number;
    /** 该块的章标题。 */
    chapter_titles?: string[];
    /** 该块独占的人物名（判定依据）。 */
    terms?: string[];
    /** 独占词集中度（0~1）。 */
    exclusive_ratio?: number;
    /** 正文样本（用于人工确认这是不是另一本书）。 */
    sample?: string;
  }>;
}

export interface NovelOut {
  id: number;
  title: string | null;
  author: string | null;
  platform: string | null;
  genre: string | null;
  status: string | null;
  /**
   * 这本语料最大的章节号（0 = 还没切出章节）。
   * 界面上的「故事进行到第几章」输入框靠它定上界 —— 没有它只能等后端 400。
   */
  max_chapter_no?: number;
  /**
   * 入库时的语料清洗报告（含「混进了别的书」的检出结论）。
   * 用户要能看到「洗掉了什么」「里面混了什么」。
   */
  clean_report?: CorpusCleanReport | null;
}

/**
 * 一本参考书的原文边界：故事进行到第 `chapter` 章（含该章）。
 *
 * 边界是**用户对「这本书我读到哪了」的回答**：模型只会读到该章为止，
 * 后面的章节不进入提示词（避免剧透，也避免把后面的剧情带偏）。
 */
export interface ReferenceCutoff {
  novel_id: number;
  chapter: number;
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
  /** imported / imported_review（后者 = 检出混书但未剔除，待人工确认）。 */
  status: string | null;
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
  /** 原文边界：每本参考书「故事进行到第几章」。null = 清空边界。 */
  reference_cutoffs?: ReferenceCutoff[] | null;
}

/** 剧情圣经里的一条分档伏笔（老项目仍是纯字符串）。 */
export interface BibleForeshadow {
  name?: string;
  title?: string;
  tier?: "main" | "arc" | "chapter";
  truth?: string;
  hints?: string[];
  reveal_chapter?: number | null;
  planned_chapter?: number | null;
  reveal_span?: number | null;
  total_chapters?: number | null;
  reveal_ratio?: number | null;
  reveal_span_ratio?: number | null;
  status?: string;
}

export interface PlotBible {
  synopsis?: string | null;
  characters?: { name?: string; role?: string; motivation?: string }[];
  relationships?: { a?: string; b?: string; relation?: string }[];
  direction?: string | null;
  conflicts?: string[];
  foreshadowing?: (string | BibleForeshadow)[];
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
  /**
   * 原文边界：每本参考书「故事进行到第几章」。
   * 角色对话 / 续写共用的可见性规则；null = 未设边界。
   */
  reference_cutoffs?: ReferenceCutoff[] | null;
  /** 当前项目已应用的人物塑造模板 id；null/缺省 = 没应用过。 */
  character_template_id?: string | null;
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
  /** 真实字符数（preview 模式下 text 被截断，仍可用它显示字数）。 */
  chars?: number;
  summary: unknown;
}

/** 章节正文的句子区间（与后端 patch.split_sentences 同口径）。 */
export interface GenerationSentence {
  index: number;
  start: number;
  end: number;
  text: string;
}

export interface GenerationSentencesResponse {
  generation_id: number;
  segment_no: number;
  chapter_range: string | null;
  count: number;
  items: GenerationSentence[];
}

/** 章前「这一章怎么开头」的一条建议。 */
export interface ChapterOpeningOption {
  id: string;
  title: string;
  summary: string;
  opening: string;
  why: string;
}

export interface ChapterOpeningResult {
  chapter: number;
  is_first_chapter: boolean;
  /** model = 模型生成；fallback = 规则兜底。 */
  source: "model" | "fallback";
  options: ChapterOpeningOption[];
  brief: {
    outline: string;
    outline_total: number;
    node: Record<string, unknown>;
    previous: string;
    hooks: string[];
    characters: string[];
  };
}

/** 右侧写作进度。 */
export interface WritingProgress {
  current_chapter: number;
  next_chapter: number;
  generated_chapters: number;
  outline_total: number;
  outline_percent: number;
  to_outline_percent: number;
  chapters_remaining: number;
  timeline: {
    exists: boolean;
    current_node: {
      id: string;
      title: string;
      chapter_start: number;
      chapter_end: number;
      index: number;
      total: number;
    } | null;
    next_node: {
      id: string;
      title: string;
      chapter_start: number;
      chapter_end: number;
    } | null;
    node_progress: number;
    to_next_node_percent: number;
    main_progress: number;
  };
}


/** 手动改正文后的配套重算结果（总结 / 摘要树 / 图谱）。 */
export interface GenerationEditCascade {
  /** 正文没变时为 true：没有触发任何重算。 */
  skipped: boolean;
  /** L1 剧情总结是否重算成功。 */
  summary: boolean;
  /** L1/L2/L3 摘要树是否重建成功（依赖 summary）。 */
  tree: boolean;
  /** v2.6 图谱是否重建完整（无失败段）。 */
  kg: boolean;
  kg_stats?: {
    segments?: number | null;
    replayed?: number | null;
    complete?: boolean | null;
    failed?: unknown[];
  } | null;
  /** v3 批抽是否成功入队（异步落库）。 */
  v3_enqueued: boolean;
  v3_pending?: number | null;
  /** 失败原因（正文仍已保存）。 */
  errors: string[];
}

export interface GenerationEditResult extends GenerationOut {
  cascade: GenerationEditCascade;
}

/**
 * 生成进度（轻量轮询）：只有对账字段，**不含正文**。
 * `/api/projects/{id}/generation-status`。
 */
export interface GenerationStatus {
  project_id: number;
  /** 已落库段数。 */
  count: number;
  /** 已落库正文字数合计（SQLite length()，按字符计）。 */
  total_chars: number;
  last_segment_no: number | null;
  last_chapter: string | null;
  /** 进程内租约是否被占用（只覆盖走租约的生成入口）。 */
  running: boolean;
}

/**
 * 时间线（世界线）节点：`GET /api/projects/{id}/timeline`。
 * 与 `app/generation/timeline.py::PlotNode` 对齐，只读展示用。
 */
export interface PlotTimelineNode {
  id: string;
  kind: "main" | "branch";
  title: string;
  emotion: string;
  entry_condition: string;
  exit_condition: string;
  beats: string[];
  segment_budget: number;
  parent: string | null;
  children: string[];
  chapter_start: number;
  chapter_end: number;
  source: string;
}

export interface PlotTimelineState {
  /** false = 还没规划过（首次生成或追加节点时才建骨架）。 */
  exists: boolean;
  outline_chapters: number;
  nodes: PlotTimelineNode[];
  order: string[];
  current_node: string | null;
  current_beat: number;
  done: number[];
  early_done: number[];
  branch_used: number;
  has_chapter_ranges: boolean;
  planning_status: string;
  planning_incomplete: boolean;
  planning_reason: string;
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
  /**
   * KG 抽取独立渠道 + 模型链（后端 `kg_extract` 块）。
   *
   * 旧后端不返回该字段（undefined）：界面不渲染这一块，不伪造。
   * 这里**只影响知识图谱抽取**，与写正文的 writer 渠道 / 预算完全隔离。
   */
  kg_extract?: LlmKgExtractConfig | null;
}

/**
 * KG 抽取配置（GET /api/settings/llm 的 `kg_extract`）。
 *
 * 关键区分：
 *   - `provider` 是**已保存**的选择，空串 = 跟随主渠道；
 *   - `effective_provider` / `effective_label` / `models` 是**实际生效值**
 *     （来自抽取单例，抽取真正在用的就是它）；
 *   - `valid=false` 时 `error` 给出原因——非法值不会静默回退主渠道。
 */
export interface LlmKgExtractConfig {
  provider: string;
  follow_main: boolean;
  main_provider: string;
  effective_provider: string;
  effective_label: string;
  /** 当前实际生效的模型链（首选在首位）。 */
  models: string[];
  /** 链的来源：随主渠道 / 所选渠道默认链 / 自定义链。 */
  models_source: "follow_main" | "channel" | "custom";
  /** 已保存的自定义链（空 = 没有自定义，用渠道默认链）。 */
  models_override: string[];
  /** 各渠道的默认链（换渠道时前端据此回填输入框）。 */
  channel_models: Record<string, string[]>;
  allowed_providers: string[];
  total_timeout: number;
  first_byte_timeout: number;
  is_fallback: boolean;
  valid: boolean;
  error: string | null;
}

/** KG 抽取配置的保存载荷（PUT /api/settings/llm 的 `kg_extract` 字段）。 */
export interface LlmKgExtractSavePayload {
  /** 空串 = 跟随主渠道；必须是 allowed_providers 之一或空串。 */
  provider: string;
  /** 空数组 = 清空自定义链，回到所选渠道的默认链。 */
  models: string[];
}

export interface LlmConfigIn {
  /** legacy / 角色模式必填；`kg_extract` 模式不需要（由 kg_extract 决定渠道）。 */
  provider?: string;
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
  /** 思考档位：low/medium/high/xhigh/max/off（后端白名单校验，未知档位 400）。 */
  thinking?: string | null;
  /** 预算地板开关：false 时必须同时给出 max_tokens 与 timeout（后端 400）。 */
  budget_floor?: boolean | null;
  /** KG 抽取模式：只写抽取渠道 / 模型链，不得与上面的字段混用（后端 400）。 */
  kg_extract?: LlmKgExtractSavePayload | null;
}

/** POST /api/settings/llm/test 请求体（role 可选，只测该角色）。 */
export interface LlmTestIn {
  provider: string;
  api_key?: string | null;
  model?: string | null;
  role?: LlmRole;
}

export interface LlmLocalModelsResponse {
  ok: boolean;
  base_url: string;
  models: string[];
  error: string | null;
}

/** GET /api/settings/web-search（多渠道）。 */
export interface WebSearchProviderMeta {
  value: string;
  label: string;
  /** 该渠道是否需要 API key（SearXNG 不需要）。 */
  needs_key: boolean;
  key_hint: string;
  /** 该渠道的默认端点；空串 = 必须由用户填写（自建实例）。 */
  default_base_url: string;
}

export interface WebSearchConfig {
  enabled: boolean;
  provider: string;
  has_key: boolean;
  api_key_masked: string | null;
  base_url: string;
  max_results: number;
  timeout: number;
  safe_search: boolean;
  providers: WebSearchProviderMeta[];
}

export interface WebSearchConfigIn {
  enabled?: boolean;
  provider?: string;
  api_key?: string;
  base_url?: string;
  max_results?: number;
  timeout?: number;
  safe_search?: boolean;
}

export interface WebSearchTestResult {
  ok: boolean;
  provider?: string;
  latency_ms?: number;
  count?: number;
  /** 被清洗规则剔除的低质条数。 */
  dropped?: number;
  cached?: boolean;
  error?: string | null;
  preview?: string[];
}

/**
 * 前端「分角色保存」载荷（RoleModelPanel → LlmSettings 视图 → api）。
 *
 * 按角色只带该角色接受的字段（后端按角色 fail-loud，多带 = 400）：
 *   - writer：带 models（限流切换链）；max_tokens / timeout / thinking /
 *     budget_floor 三角色统一可配（红线已解除）；
 *   - assistant / distiller：不带 models（单模型配置，没有限流切换链）。
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
  /** 三角色：max_tokens / timeout（null = 不改）。 */
  max_tokens?: number | null;
  timeout?: number | null;
  /** 思考档位（恒传当前档位；null = 不改）。 */
  thinking?: string | null;
  /** 预算地板开关（null = 不改；false 必须同时给 max_tokens 与 timeout）。 */
  budget_floor?: boolean | null;
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
  /** 当前思考档位（后端已解析出默认值，空串不会出现）。 */
  thinking: string;
  /** 预算地板开关：on = 不足 32768/300 自动抬高；off = 严格按用户值。 */
  budget_floor: boolean;
  /** 该渠道+模型的思考控制能力（下拉选项 / 说明 / 实测依据）。 */
  thinking_control: LlmThinkingControl;
  is_fallback: boolean;
  label: string;
}

/** 思考控制契约（后端 `thinking_control_for` 的翻译结果）。 */
export interface LlmThinkingControl {
  /** effort = 可调档；disable = 只能开关；none = 没有可用的思考开关。 */
  mode: "effort" | "disable" | "none";
  /** 请求体里对应的字段名（展示用）；none 时为 null。 */
  field: string | null;
  /** 可选档位；none 时为空数组。 */
  levels: string[];
  note: string;
  verification: "verified" | "unverified";
  evidence: string;
}

/**
 * 生成侧章纲回执（S4）：本次提示词**实际**拿到的章纲与校验状态。
 *
 * - `prompt_entry === null` ⇒ 这一章的提示词里没有章纲（缺纲 / 空条目 / 超出末章）；
 * - `blocking` 恒为 false：只提示、不拦生成，历史 43 章不会被卡住；
 * - `validation.skipped_checks` 必须展示，否则「没报错」会被读成「检查过了」。
 */
export interface GenerationOutlineChapter {
  chapter: number;
  status: ChapterOutlineStatus;
  /** 正文是否已经写到这一章（决定缺失标签是「缺失」还是「正文已写·无章纲」）。 */
  written: boolean;
  prompt_entry: string | null;
}

export interface GenerationOutlineNotice {
  from_chapter: number;
  to_chapter: number;
  chapters: GenerationOutlineChapter[];
  missing_chapters: number[];
  first_missing_chapter: number | null;
  prompt_has_outline: boolean;
  validation: ChapterOutlineValidation;
  blocking: false;
  note: string;
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
  /** 本次生成会拿到的章纲回执（缺纲 / 跳过项显式可见）；旧后端不返回 → undefined。 */
  outline?: GenerationOutlineNotice;
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
  /** 本章实际收到的章纲回执；旧后端不返回 → undefined。 */
  outline?: GenerationOutlineNotice;
}

export interface GenerateLoopResult {
  segments: (GenerationOut & {
    summary: unknown;
    /** 每段各自的章纲回执；旧后端不返回 → undefined。 */
    outline?: GenerationOutlineNotice;
  })[];
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
 * 生成流里的「作者 Agent」阶段事件（SSE `{type:"stage"}`）。
 *
 * 后端在装配上下文 / 等待首字 / 模型思考 / 流式撰写 / 重算摘要与图谱
 * 这五个节点各发一次，用来替代「只有一个思考字数」的干等展示。
 * 旧后端不发 stage → 前端保持既有的字数 + 计时展示。
 */
export interface GenerationStageEvent {
  type: "stage";
  stage: "prepare" | "model" | "thinking" | "writing" | "post";
  label: string;
}

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
  /** 本章实际收到的章纲回执（缺纲/跳过项）；旧后端不返回 → undefined。 */
  outline?: GenerationOutlineNotice;
  /**
   * 结构检查结果（后端 `done` 实际发射的 `structure_check`，dict | None）。
   * 旧后端不返回 → undefined；前端只在拿到值时渲染，不写死兜底文案。
   */
  structure_check?: Record<string, unknown> | null;
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
  /** 本章实际收到的章纲回执（缺纲/跳过项）；旧后端不返回 → undefined。 */
  outline?: GenerationOutlineNotice;
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

export type AssistantProposalType =
  | "text_edit"
  | "prompt_edit"
  | "fact_resolution"
  /** 语料清洗产出（clean-corpus 只产 pending，不写 novel_chunks）。 */
  | "corpus_text_edit";

export type AssistantTargetKind =
  | "generation_text"
  | "project_plot_bible"
  | "prompt_override"
  /** 语料分片正文（target_id 是 novel_chunks.id 的十进制字符串）。 */
  | "novel_chunk_text";

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
  /** 本轮带的文本附件（`/assistant/attachments` 上传后得到的 id）。 */
  attachment_ids?: string[];
}

/** GET /api/assistant/attachments/{id}/preview —— 投放前的清洗预览（只读）。 */
export interface AttachmentCleaningPreview {
  attachment_id: string;
  chapters: number;
  report: Record<string, unknown>;
  changes: {
    chapter_no: number | null;
    kind: string;
    before: string;
    after: string;
    removed_chars: number;
    added_chars?: number;
    chapter_index?: number;
  }[];
  unresolved_count: number;
  untrusted: boolean;
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

/** 卡片类型：新建提案 / 调动生成 / 附件投放 / 局部修订 / **选项** / 动作提案。 */
export type AssistantPendingActionKind =
  | "proposal"
  | "invoke_generation"
  | "ingest_attachment"
  | "body_repair"
  | "choice"
  /**
   * 动作提案：agent 提「做什么事」（启动蒸馏 / 规划大纲 / 扩展规划 / 调动生成）。
   * 确认这一步只是**记下**提案；执行是「批准 → 执行」之后的事。
   */
  | "action_proposal";

/** 选择框的一个选项（服务端下发：id/标题/说明 + 对应的请求体补丁）。 */
export interface AssistantChoiceOption {
  id: string;
  label: string;
  description?: string;
  body_patch?: Record<string, unknown>;
}

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
  /**
   * 选择框的选项（只有 kind="choice" 时有）。
   * 选项与后果由服务端成对下发，前端不参与构造。
   */
  options?: AssistantChoiceOption[];
}

// ---------- 工具调用轨迹（function calling 接入） ----------

/**
 * 一次工具调用（后端 `/qa` 的 `tool_trace[]`）。
 *
 * 只含只读工具名 / 参数原文（截断）/ 状态 / 耗时，**没有** endpoint、body
 * 等写语义字段 —— 前端只用来展示「模型调了什么、拿到没有」，不据此发请求。
 */
export interface AssistantToolTraceItem {
  /** 第几轮请求里发起的（模型可在一轮里发多个调用）。 */
  step: number;
  /** 本次问答内的调用序号（从 1 开始）。 */
  seq: number;
  /** 工具名（必然在只读白名单内；旧后端不会出现这个字段）。 */
  name: string;
  /** 模型给的参数原文（服务端已按 300 字符截断并打标记）。 */
  arguments: string;
  /** ok=已执行；denied=不在白名单，未执行；error=参数非法或执行失败。 */
  status: "ok" | "denied" | "error" | string;
  /** 工具执行耗时（毫秒）。 */
  latency_ms: number;
}

/**
 * 动作目录项（后端 `available_actions[]`）。
 *
 * 档 2 / 档 3 只会出现在这里（文本发现），永远不会变成可调用的工具；
 * 前端当前只按需展示，不据此直接发写请求（写操作仍走待确认卡片）。
 */
export interface AssistantAvailableAction {
  key: string;
  title: string;
  summary: string;
  category: "read" | "write" | "destructive" | string;
  policy: "auto_read" | "propose_write" | "frontend_only" | "unwired_read" | string;
  /** 面向用户的一句话策略解释（服务端下发，前端不自己编）。 */
  policy_hint: string;
  endpoint: string | null;
  method: string | null;
  read_tool: string | null;
  frontend_entry: string;
  triggers: string[];
  requires_confirmation: boolean;
  batch: boolean;
  touches_config: boolean;
}

// ---------- 响应体 ----------

export interface AssistantWebSource {
  index: number;
  title: string;
  url: string;
  snippet: string;
  provider: string;
  published_at?: string | null;
}

export interface AssistantWebSearchMeta {
  enabled: boolean;
  provider: string;
  cached?: boolean;
  /** 被清洗规则剔除的低质条数（登录墙 / 重复 / 同站超限 / 导流垃圾）。 */
  dropped?: number;
  error?: string | null;
}

/**
 * 模型在正文前的推理留痕（只报客观数字，不含推理内容）。
 *
 * 推理内容由后端 provider 层剥离，这里只让界面能如实画出「思考了 N 秒」，
 * 避免把真实发生的推理当成不存在。
 */
export interface AssistantThinking {
  /** 推理字数（>0 才会下发）。 */
  chars: number;
  /** 首批推理 token 到首个正文 token 的耗时；渠道不区分钟无法计算时为 null。 */
  duration_ms?: number | null;
}

export interface AssistantQaResponse {
  answer: string;
  evidence: AssistantEvidence[];
  confidence: number;
  agent_run_id: number;
  tool_results: Record<string, unknown>;
  version_id: number | null;
  /** 联网检索来源；未配置/未触发时为空数组或 undefined。 */
  web_sources?: AssistantWebSource[];
  web_search?: AssistantWebSearchMeta;
  /** 本轮确实发生推理时的留痕；没有推理（或渠道不暴露）时为 null/undefined。 */
  thinking?: AssistantThinking | null;
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
  /**
   * 本次取证的执行方式：`model`=模型自主选了工具；`deterministic`=回落确定性检索；
   * `chat`=剧情讨论的流式回答（不跑工具循环）。
   */
  tool_mode?: "model" | "deterministic" | "chat";
  /** 模型自主调用轨迹；确定性路径为空数组，旧后端不返回 → undefined。 */
  tool_trace?: AssistantToolTraceItem[];
  /** 工具循环实际走了多少步（0/undefined = 没走模型工具路径）。 */
  tool_steps?: number;
  /** 收尾是否靠最后一步「不带 tools」强制完成（true = 步数已用满）。 */
  forced_wrap_up?: boolean;
  /** 回落原因（仅 deterministic 时非空；供诊断，不展示为正文）。 */
  tool_fallback_reason?: string;
  /** 本次问题命中的动作目录项（档 2/3 只会出现在这里）。 */
  available_actions?: AssistantAvailableAction[];
  /**
   * 本次问答是否改动了长期记忆（用户说「记住…」/「忘掉…」）。
   * 旧后端不返回 → undefined → 不刷新记忆抽屉。
   */
  memory_changed?: boolean;
  /** 记忆变更后的全量清单（仅 `memory_changed=true` 时返回）。 */
  memories?: AssistantMemory[];
}

/** POST /api/assistant/chat/stream 的 SSE 事件（meta / steps / delta / done / error）。 */
export type AssistantChatStreamEvent =
  | {
      type: "meta";
      agent_run_id: number;
      model?: string;
      /** 证据条数 / 置信度：在正文开始前下发，前端可提前显示徽标。 */
      evidence_count?: number;
      confidence?: number;
    }
  | {
      type: "steps";
      steps: { name: string; meta?: string; detail?: string }[];
      /** true = 增量进度（追加到已有步骤）；缺省 = 整轮覆盖。 */
      append?: boolean;
    }
  | { type: "delta"; text: string }
  | ({ type: "done" } & AssistantQaResponse)
  | { type: "error"; message: string };

/** 一条项目级长期记忆（助手在问答后自动抽取写入）。 */
export interface AssistantMemory {
  id: number;
  text: string;
  /** auto = 助手自动抽取。 */
  source: string;
  created_at: string | null;
}

/** GET/DELETE `/api/assistant/memories` 的响应。 */
export interface AssistantMemoryListResponse {
  project_id: number;
  items: AssistantMemory[];
  count: number;
  limit: number;
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
  /** 含糊指令澄清：上一次 202 返回的 clarification_id。 */
  clarification_id?: number | null;
  /** 从候选清单里选定的区间（原样回传，服务端再校验一次）。 */
  selected?: { from_index: number; to_index: number } | null;
  /** 「都不对」的补充说明（最多两轮，超过 → 422 clarify_round_limit）。 */
  clarify_reply?: string | null;
}

/** 含糊指令澄清候选（preview 由服务端按编号从原文截取）。 */
export interface AssistantClarifyCandidate {
  id: string;
  from_index: number;
  to_index: number;
  preview: string;
  reason: string;
}

/**
 * 澄清响应（HTTP 202）：`status='needs_clarification'`，与 200 成功一眼区分。
 * 澄清阶段**不写正文**，只是把可选区间摆出来。
 */
export interface AssistantClarifyResponse {
  status: "needs_clarification";
  code: "needs_clarification";
  clarification_id: number;
  segment_no: number;
  instruction: string;
  round: number;
  max_rounds: number;
  candidates: AssistantClarifyCandidate[];
  /** 每个候选最多几句（局部上界）。 */
  limit: number;
  rounds_left: number;
  before_hash: string;
  message: string;
}

export type AssistantInvokeGenerationResult =
  | AssistantInvokeGenerationResponse
  | AssistantClarifyResponse;

/** POST /api/assistant/invoke-generation 响应。 */
export interface AssistantInvokeGenerationResponse {
  /** 后端成功响应不返回 status；此字段只用于前端与 202 澄清响应判别。 */
  status?: undefined;
  answer: string;
  agent_run_id: number;
  action: AssistantGenerationAction;
  /** Writer 实际使用的模型（本地兜底时 "(local_fallback)"）。 */
  writer_model: string;
  /** 与 GET /api/generations/{pid} 同形状，可复用渲染。 */
  segment: GenerationOut;
  chars: number;
  /** 本次改写来自哪次澄清（null = 非澄清路径）。 */
  clarification_id?: number | null;
  /** 局部补丁留痕（scope_source='user' 即用户从候选选定）。 */
  patch?: {
    patch_count: number;
    scope_source: string;
    applied: Array<{
      from_index: number;
      to_index: number;
      before: string;
      replacement: string;
    }>;
  } | null;
  scope_mismatch?: Record<string, unknown> | null;
  length_drop?: Record<string, unknown> | null;
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
  /**
   * 任务产物种类（新后端字段）：
   * narrative（叙事档案）/ character_template（人物模板）/ book_digest（全书摘要）。
   * 旧后端不返回 → undefined，按旧行为处理。
   */
  kind?: string | null;
  /** 按书产物（全书摘要）对应的语料 id；叙事提炼为 null。 */
  novel_id?: number | null;
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

// ---------- 全书摘要（BookDigest） ----------

/**
 * POST /api/projects/{id}/digests 的请求体。
 *
 * 注意提交端点是**项目级**（后台任务挂在项目下），产物是**书级**的
 * （同一本书的摘要任何项目都能复用）。
 */
export interface BookDigestStartRequest {
  /** 要做摘要的参考语料（raw_novels.id）。 */
  novel_id: number;
  /** auto = 按书大小与渠道能力自动选；whole = 整本直读；retrieval = 检索式。 */
  mode?: "auto" | "whole" | "retrieval";
  /** 压缩比目标（输出 ≤ 输入 / 该值），后端限制 1~100。 */
  target_ratio?: number;
  /** true = 忽略已有摘要强制重算。 */
  force?: boolean;
}

/** POST /api/projects/{id}/digests 的响应：立即返回，摘要在后台跑。 */
export interface BookDigestStartResponse {
  job_id: number;
  status: DistillStatus;
  novel_id: number;
  /** true = 复用了这本书正在跑的任务，没有新起一条。 */
  reused: boolean;
}

/** 单项验收结论。`ok === null` 表示这一项**不适用**（没跑到，不算通过）。 */
export interface BookDigestCheck {
  ok: boolean | null;
  applicable?: boolean;
  value?: number | null;
  covered?: number;
  candidates?: number;
  threshold?: number;
  chars_in?: number;
  chars_out?: number;
  ratio?: number;
  target_ratio?: number;
  dropped?: number;
  kept?: number;
  reattributed?: number;
  dropped_ratio?: number | null;
  [key: string]: unknown;
}

/** GET /api/books/{novel_id}/digest（没有摘要时后端 404）。 */
export interface BookDigest {
  id: number;
  novel_id: number;
  digest_version: number;
  mode: string;
  mode_reason: string | null;
  status: string;
  stats: Record<string, unknown>;
  verify_passed: boolean | null;
  checks: Record<string, BookDigestCheck>;
  created_at: string | null;
  updated_at: string | null;
  /** ok=null 的检查项名（不适用，不算失败）。 */
  not_applicable?: string[];
  /** 证据丢弃明细（界面要能区分「模型在编」和「引文写错一个标点」）。 */
  evidence_detail?: {
    dropped: number;
    kept: number;
    reattributed: number;
    dropped_ratio: number | null;
    threshold: number | null;
    drop_reasons?: Record<string, number>;
    dropped_by_block?: Record<string, number>;
    drop_samples?: Record<string, unknown>;
  };
  payload: Record<string, unknown>;
  /** 渲染后的摘要正文（给人读的那份）。 */
  text: string;
}

/** GET /api/books/{novel_id}/digest/verify。 */
export interface BookDigestVerify {
  novel_id: number;
  digest_id: number;
  passed: boolean;
  checks: Record<string, BookDigestCheck>;
  stats: Record<string, unknown>;
}

/** 历史列表项：摘要字段齐全，但不带正文（正文按需取单条详情）。 */
export type BookDigestSummary = Omit<BookDigest, "payload" | "text">;

/** GET /api/books/{novel_id}/digests（新的在前，含 active / archived）。 */
export interface BookDigestListResponse {
  items: BookDigestSummary[];
}

// ---------- 人物塑造模板 ----------

/** GET /api/projects/character-templates 的列表项（新的在前）。 */
export interface CharacterTemplateItem {
  /** 形如 `full:9a3f1166f4e6:1f2a3b4c`，自带档位。 */
  id: string;
  tier: string;
  tier_signature: string;
  source_ids: number[] | null;
  content_hash: string;
  rule_count: number;
  n_chunks: number;
  rejected: number | null;
  created_at: string | null;
  applied_at: string | null;
}

export interface CharacterTemplateListResponse {
  items: CharacterTemplateItem[];
}

/** 单条模板详情（列表只在点开时取它，避免一次传几十条规则）。 */
export interface CharacterTemplateDetail extends CharacterTemplateItem {
  rules: string[];
}

/** POST /api/projects/{id}/character-template 的请求体。 */
export interface CharacterTemplateStartRequest {
  /** 档位 key；不传 = 沿用项目已存的 narrative_tier / 默认档。 */
  tier?: string | null;
  /** true = 同档位也强制重抽。 */
  force?: boolean;
}

/** POST /api/projects/{id}/character-template 的响应（后台任务）。 */
export interface CharacterTemplateStartResponse {
  job_id: number;
  status: DistillStatus;
  tier: string;
  kind: string;
  reused: boolean;
  /** true = 库里已有同档同语料模板，本次直接复用。 */
  from_cache: boolean;
  cache_source: "library" | null;
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
  /** 「第a–b章」或「章号未知」；后端保证不为 null。 */
  chapters: string;
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

/** GET /kg-v3/corpus-windows：语料摄取窗口进度（只读聚合）。 */
export interface KgV3CorpusWindowFailure {
  window_key: string;
  chapter_start: number;
  chapter_end: number;
  attempts: number;
  /** 后端按 error 文本做的展示归类；原始 error 见 `error`。 */
  reason: "parse_failed" | "channel_unavailable" | "other";
  error: string | null;
  updated_at: string | null;
}
export interface KgV3CorpusWindowsResponse {
  /** false = 窗口表为空（从未摄取），不是「没有实体」。 */
  ingested: boolean;
  total: number;
  counts: { pending: number; running: number; success: number; failed: number };
  pending: number;
  running: number;
  success: number;
  failed: number;
  failed_reasons: { parse_failed: number; channel_unavailable: number; other: number };
  /** error 里带「离线兜底」的失败窗口数（本质是渠道不可用）。 */
  suspected_local_fallback: number;
  failures: KgV3CorpusWindowFailure[];
  last_run_at: string | null;
}

/** GET /kg-v3/book-scope：单表书维度计数。 */
export interface KgV3TableScope {
  total: number;
  in_scope: number;
  project_level_null: number;
  /** 被读侧过滤掉的别书行数（不显示 ≠ 不存在）。 */
  other_books_excluded: number;
}

export interface KgV3EntityScope extends KgV3TableScope {
  /** 章号未知（first/last 任一为空）的实体数。 */
  chapter_unknown: number;
  /** 现行区间与当前 mention 聚合不一致的实体数（只计数、不覆盖）。 */
  chapter_range_drift: number;
}

export interface KgV3RelationScope extends KgV3TableScope {
  /** 边自身无书、但两端同属一本非 NULL 书 → 书维度可从端点推导。 */
  derived_from_endpoints: number;
  /** 两端分属不同非 NULL 书 → 真实跨书边（读侧会挡掉，但必须可见）。 */
  cross_book_edges: number;
}

export interface KgV3ForeshadowScope extends KgV3TableScope {
  /** 归不出书的伏笔（老数据 novel_id 为空）。 */
  unattributable: number;
  /** 同名跨书撞 uq_kg_foreshadow(project_id,name)，写侧转存隔离区的次数。 */
  cross_book_name_collision: number;
}

export interface KgV3BookScopeResponse {
  project_id: number;
  book: {
    continuation_novel_id: number | null;
    active_novel_id: number | null;
    resolved: number | null;
    scope: (number | null)[];
  };
  entities: KgV3EntityScope;
  relations: KgV3RelationScope;
  foreshadows: KgV3ForeshadowScope;
  events: KgV3TableScope;
  mentions: KgV3TableScope;
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
  type: "single" | "multi" | "number" | "text" | "confirm" | "cards";
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
  /** 仅 type="cards"：剧情圣经里的角色清单（角色卡步骤）。 */
  characters?: WizardCardCharacter[];
}

/** 角色卡步骤里的一个角色（是否已有声纹卡）。 */
export interface WizardCardCharacter {
  name: string;
  role: string;
  has_card: boolean;
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
  stage: "outline" | "cards" | "ready" | "role" | "done";
  applied: string[];
  message?: string;
  distill?: Record<string, unknown> | null;
  bible?: Record<string, unknown> | null;
  /** 引导提交时批量补角色卡的结果；失败时 reason 非空，不影响大纲。 */
  character_cards?: {
    generated: number;
    names: string[];
    /** 本次没生成成功的角色名（模型漏掉或失败）。 */
    missing?: string[];
    /** 缺卡二次补建成功的角色名（批量生成后自动再补一轮）。 */
    retried?: string[];
    collisions: number;
    reason?: string;
  };
  /** 提交时生成的力量体系（题材为仙侠/奇幻/游戏且选择生成时才有）。 */
  power_system?: {
    ok: boolean;
    systems?: number;
    tiers?: number;
    /** 已有体系被沿用时为 true。 */
    skipped?: boolean;
    detail?: string;
  };
  character?: string;
  scene?: string;
  segments?: WizardCommitSegment[];
}

export interface WizardStateResponse {
  session: WizardSessionView | null;
  question: WizardQuestion | null;
  /** 角色卡步骤：刚生成（或改过）的卡，前端复用结果气泡里的卡编辑 UI。 */
  cards_preview?: WizardCommitResult | null;
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
    | "chapter"
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
  /** 进入角色卡步骤时随题返回的卡预览。 */
  cards_preview?: WizardCommitResult | null;
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
  /** 进入角色卡步骤时随题返回的卡预览。 */
  cards_preview?: WizardCommitResult | null;
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

// ==================================================================
// 档 3 前端显式入口：语料清洗 / 快照回滚 / 批量应用提案
// ------------------------------------------------------------------
// 三者在 backend/app/agents/action_catalog.py 里登记为 frontend_only：
// 助手只能引导用户来页面操作，页面自己完成「影响范围确认 → 二次确认 →
// 可核对回执」。前端不得从对话框直接触发这三条链路。
// ==================================================================

/** POST /api/assistant/clean-corpus 请求体（project_id / novel_id 至少给一个）。 */
export interface AssistantCleanCorpusRequest {
  project_id?: number | null;
  novel_id?: number | null;
  chunk_ids?: number[] | null;
  /** 后端合法区间 [1, MAX_CLEAN_CHUNK_LIMIT=200]，默认 20。 */
  limit?: number;
  /** true = 只检测与复核，不创建提案。 */
  dry_run?: boolean;
}

/** 一条建议修正（suggested_edits[]）；proposed=true 表示已落成提案。 */
export interface AssistantCleanEdit {
  chunk_id: number;
  novel_id: number;
  rules: string[];
  /** rule = 确定性修正；model = 模型复核。 */
  source: string;
  before: string;
  after: string;
  reason: string;
  confidence: number;
  proposed: boolean;
  skip_reason?: string;
}

/** 被护栏丢弃的模型输出（model_rejected[]）。 */
export interface AssistantCleanReject {
  chunk_id: number;
  reason: string;
}

/** POST /api/assistant/clean-corpus 响应。 */
export interface AssistantCleanCorpusResponse {
  answer: string;
  issues: AssistantIssue[];
  findings: AssistantIssue[];
  suggested_edits: AssistantCleanEdit[];
  model_rejected: AssistantCleanReject[];
  proposals: AssistantProposal[];
  proposal_count: number;
  dry_run: boolean;
  model_participated: boolean;
  model_note: string;
  evidence: AssistantEvidence[];
  confidence: number;
  agent_run_id: number;
  tool_results: {
    chunks_scanned: number;
    limit: number;
    novel_id: number | null;
    chunk_ids: number[] | null;
    findings_by_rule: Record<string, number>;
    findings_by_severity: Record<string, number>;
    model_note: string;
    model_rejected: AssistantCleanReject[];
    hint_only_rules: string[];
  };
  rules_evaluated: string[];
  scope: {
    project_id: number;
    novel_id: number | null;
    chunk_ids: number[] | null;
    limit: number;
    chunks_scanned: number;
  };
}

/** GenerationSnapshot 的一行（GET /api/assistant/snapshots）。 */
export interface AssistantSnapshot {
  id: number;
  project_id: number;
  generation_id: number;
  segment_no: number;
  batch_id: string | null;
  operation: string;
  /** pending | applied | failed | rolled_back */
  status: string;
  before_hash: string | null;
  after_hash: string | null;
  instruction: string | null;
  scope_mismatch: Record<string, unknown> | null;
  length_drop: Record<string, unknown> | null;
  agent_run_id: number | null;
  bible_hash: string | null;
  error_reason: string | null;
  clarification: Record<string, unknown> | null;
  created_at: string | null;
  finished_at: string | null;
  rolled_back_at: string | null;
  rolled_back_by_snapshot_id: number | null;
  before_chars: number;
  after_chars: number;
  /** 快照指向的正文行是否还在。false 时点确认只会 409，界面应直接禁用。 */
  generation_exists: boolean;
  /** 只有 applied 且正文行还在的旧快照还能回滚。 */
  undoable: boolean;
}

export interface AssistantSnapshotListResponse {
  items: AssistantSnapshot[];
  count: number;
}

/** 一次操作的聚合（GET /api/assistant/snapshots/groups），整组回滚用。 */
export interface AssistantSnapshotGroup {
  batch_id: string;
  operation: string;
  created_at: string | null;
  segments: number[];
  applied: number;
  failed: number;
  rolled_back: number;
  /** 组内 applied 快照指向的正文行是否都还在。false 时整组回滚必被拒。 */
  generation_exists: boolean;
  undoable: boolean;
}

export interface AssistantSnapshotGroupListResponse {
  items: AssistantSnapshotGroup[];
  count: number;
}

/** auto：设定变过就 409 让用户选；text_only：只退正文；with_bible：先退设定再退正文。 */
export type AssistantRollbackMode = "auto" | "text_only" | "with_bible";

export interface AssistantRollbackRequest {
  project_id: number;
  snapshot_id?: number | null;
  segment_no?: number | null;
  mode?: AssistantRollbackMode;
  actor?: string | null;
}

export interface AssistantRollbackBatchRequest {
  project_id: number;
  batch_id: string;
  mode?: AssistantRollbackMode;
  actor?: string | null;
}

/** 单段回滚成功回执。 */
export interface AssistantRollbackResult {
  ok: true;
  restored_snapshot_id: number;
  rollback_snapshot_id: number;
  rollback_batch_id: string;
  segment_no: number;
  chars: number;
  text_hash: string;
  operation: string;
  actor: string | null;
  warnings: string[];
  derived: Record<string, unknown>;
}

/** 整组回滚成功回执。 */
export interface AssistantRollbackBatchResult {
  ok: true;
  batch_id: string;
  rollback_batch_id: string;
  segments: {
    segment_no: number;
    restored_snapshot_id: number;
    rollback_snapshot_id: number;
    chars: number;
  }[];
  count: number;
  operation: string;
  actor: string | null;
  warnings: string[];
  derived: Record<string, unknown>;
}

/** 批量应用的逐条失败原因。 */
export interface AssistantApplyConflictItem {
  proposal_id: number | null;
  status?: string;
  reason: string;
}

/** POST /api/assistant/proposals/apply-selected 请求体（必须显式枚举 id）。 */
export interface AssistantApplySelectedRequest {
  action: "apply";
  proposal_ids: number[];
  actor?: string | null;
}

/** apply-selected 成功回执；409 时 detail 是同一形状且 ok=false。 */
export interface AssistantApplySelectedResponse {
  ok: boolean;
  applied: AssistantApplyResult[];
  stale: AssistantApplyConflictItem[];
  blocked: AssistantApplyConflictItem[];
  reason: string;
  proposal_ids?: number[];
}

// ==================================================================
// 工序链（助理 agent 编排的全流程）
// ------------------------------------------------------------------
// 工序状态是服务端**重新评估**出来的（查库 + 只读规则），
// 不是日志里的一句话 —— 正文改过、蒸馏重跑过、提案批了，状态都跟着变。
// ==================================================================

/** 一道工序的状态。 */
export type WorkflowStepState = "done" | "pending" | "blocked" | "skipped";

/** 一道工序。 */
export interface WorkflowStep {
  id: string;
  title: string;
  state: WorkflowStepState;
  /**
   * 人话状态词（服务端给的）。
   *
   * 为什么不自己映射：「没有待批项」和「已完成」在中文里差得很远，
   * 而映射表写死在两处就会漂；「下一步」更是只有服务端知道。
   */
  state_label?: string;
  reason: string;
  /** 这一步该调的只读工具（agent 的工具箱里已注册）。 */
  read_tools: string[];
  /** 这一步若要写，产出哪一类提案（写操作走审批闸门）。 */
  write_proposal: string | null;
  /** 哪条轨："prompt"（提示词）/ "body"（正文）。 */
  track: string;
  /** 派给谁做（人话）。 */
  dispatch: string;
  note: string;
}

/** 一条等着你处理的改动（只读清单；批准与写进正文仍走审批闸门）。 */
export interface WorkflowPendingProposal {
  id: number;
  /** 要改哪儿（人话，例如「正文第 3 段」）。 */
  target: string;
  /** 为什么改。 */
  reason: string;
  /** 改动大小（人话，例如「改动较大」）。 */
  risk: string;
  risk_level: string;
  /** "pending"（等你批）/ "approved"（批了，等你写进正文）。 */
  status: string;
  created_at: string | null;
}

/**
 * **动作提案**：agent 提「做什么事」，人批了才执行。
 *
 * 与文本提案（WorkflowPendingProposal）分开：文本提案回答「改哪些字」，
 * 动作提案回答「做什么事」（启动蒸馏 / 规划大纲 / 扩展规划 / 调动生成），
 * 它的过期判据是**前提**变了没有。
 */
export interface ActionProposalOut {
  id: number;
  project_id: number;
  run_id: number | null;
  kind: string;
  title: string;
  detail: Record<string, unknown>;
  endpoint: string;
  method: string;
  body: Record<string, unknown>;
  /** 前提指纹（漂移检测用；界面不显示）。 */
  precondition_fingerprint: string;
  /** 人话：这条提案的前提是什么。 */
  precondition_note: string | null;
  risk_level: string;
  status: "pending" | "approved" | "rejected" | "applied" | "stale";
  created_at: string | null;
  approved_at: string | null;
  applied_at: string | null;
  applied_result: Record<string, unknown> | null;
}

export interface ActionProposalListResponse {
  project_id: number;
  status: string | null;
  count: number;
  proposals: ActionProposalOut[];
}

/** 创建动作提案的请求体（卡片确认后前端原样回传，服务端拼好）。 */
export interface ActionProposalCreateRequest {
  project_id: number;
  kind: string;
  title: string;
  detail?: Record<string, unknown>;
  action_endpoint: string;
  action_method?: string;
  action_body?: Record<string, unknown>;
  params?: Record<string, unknown>;
  risk_level?: string;
}

/** 当前需要用户决断的岔路口。 */
export interface WorkflowDecision {
  key: string;
  title: string;
  question: string;
  step_id: string;
}

/** 一个项目的工序链状态。 */
export interface WorkflowState {
  project_id: number;
  status: "active" | "done" | "blocked";
  current_step: string | null;
  summary: string;
  steps: WorkflowStep[];
  gates: string[];
  needs_decision: WorkflowDecision | null;
  /** 等着你处理的改动（待批 + 已批待写进正文）。 */
  open_proposals: WorkflowPendingProposal[];
  round: number;
  updated_at: string | null;
}

// ==================================================================
// 角色对话（第三板块）
// ==================================================================

/** 角色对话：一条带**来源标注**的证据。 */
export interface RoleplaySource {
  chunk_id: number;
  chapter_no: number | null;
  idx: number | null;
  /**
   * 「亲历」= 他自己开过口 / 这件事记在他名下；
   * 「在场听到」= 别人在说，他在旁边；
   * 「只是被提到」= 文里写到了他，**不代表他在场、也不代表他知道**。
   */
  origin: "亲历" | "在场听到" | "只是被提到";
  /** 判据说明（人可复核）。 */
  basis: string;
  score: number;
  text: string;
  /** KG 里明确记着他知道的事。 */
  known_facts: string[];
}

/** 角色对话的一条记录。 */
export interface RoleplayMessage {
  id: number;
  character: string;
  question: string;
  answer: string;
  sources: RoleplaySource[];
  novel_ids: number[];
  created_at: string | null;
}

/** GET /projects/{id}/roleplay/characters */
export interface RoleplayCharactersResponse {
  /** 两组合并（项目里的在前）。 */
  characters: string[];
  /** 这个项目里的角色（剧情圣经 + 项目实体表里的）。数量少，默认展开。 */
  project: string[];
  /** 参考书里的角色（叙事档案的声音词典里有一整套）。折叠 + 可搜索。 */
  reference: string[];
  /**
   * 角色的别名（规范名 → 别名列表）。
   *
   * 别名**不单独占一格**：「沈砚的父亲」「他爹」这类指代铺开会让清单看着多了一倍角色。
   */
  aliases?: Record<string, string[]>;
  /** 后端当前认定的「被扮演角色」。 */
  current: string | null;
}

/** POST /projects/{id}/roleplay/chat */
export interface RoleplayChatResult {
  id: number;
  character: string;
  question: string;
  answer: string;
  sources: RoleplaySource[];
  has_evidence: boolean;
  elapsed: number;
}

// ====================================================================
// 角色卡（2026-10-29）：身份 / 能力（武器·功法·能力）/ 声纹 / 关系
// ====================================================================

/** 武器/功法/能力的一条。 */
export interface CardAbilityItem {
  名称: string;
  说明?: string;
}

/** 角色卡的「能力」组。 */
export interface CardAbility {
  武器?: CardAbilityItem[];
  功法?: CardAbilityItem[];
  能力?: CardAbilityItem[];
  /** 他做不到什么 —— 防止角色越写越强。 */
  能力上限?: string[];
}

/** 角色卡的「声纹」组 —— **可执行锚点**，不是形容词。 */
export interface CardVoice {
  自称?: string;
  称呼他人?: string[];
  句长?: string;
  句末?: string;
  口头禅?: string[];
  禁用词?: string[];
  语气底色?: string;
}

/** 角色卡的「量级」组：力量层次 / 出手规模 / 能压制谁。 */
export interface CardScale {
  量级?: string;
  出手规模?: string;
  压制?: string;
}

/** 角色卡。 */
export interface CharacterCard {
  身份?: Record<string, string>;
  能力?: CardAbility;
  声纹?: CardVoice;
  量级?: CardScale;
  /** 修炼档案（当前境界/天赋/瓶颈/路线…），由力量体系页维护。 */
  修炼?: CultivationProfile;
  关系?: Record<string, string>;
  来源?: string;
}

/** GET /projects/{id}/character-cards 的一项。 */
export interface CharacterCardItem {
  name: string;
  role: string;
  motivation: string;
  card: CharacterCard;
  card_source: string;
  persona: Record<string, unknown>;
  persona_source: string;
}

/** 声纹撞车（「这几个人说话太像」）。 */
export interface VoiceCollision {
  人物: string[];
  相同: string[];
  建议: string;
}

/** GET /projects/{id}/character-cards 的响应。 */
export interface CharacterCardListResponse {
  characters: CharacterCardItem[];
  voice_collisions: VoiceCollision[];
}

/** 单角色 AI 补全：只返回「补充」，不落库。 */
export interface CharacterCardGenerateResponse {
  ok: boolean;
  name: string;
  补充: CharacterCard;
  说明: string;
  voice_collisions: VoiceCollision[];
}

/** 批量生成新角色：只返回草稿，采纳后逐条保存。 */
export interface CharacterCardsBatchResponse {
  ok: boolean;
  角色: Record<string, CharacterCard>;
  冲突: VoiceCollision[];
  轮次: number;
}

// ==================================================================
// 力量体系 / 档位分层 / 角色修炼档案
// ==================================================================

export interface PowerStage {
  名称?: string;
  说明?: string;
}

export interface PowerRealm {
  名称?: string;
  说明?: string;
  档位?: string;
  小境界?: PowerStage[];
}

export interface PowerSystemDef {
  名称?: string;
  说明?: string;
  /** 细分型 / 序列型 / 粗放型（后端 normalize_system_type）。 */
  类型?: string;
  境界?: PowerRealm[];
}

/** GET /api/projects/{id}/power-system。 */
export interface PowerSystemState {
  power_system: { 体系: PowerSystemDef[] };
  tiers: PowerTier[];
}

/** 档位层：分层群像用（路人只记档位）。 */
export interface PowerTier {
  名称?: string;
  档位?: string;
  人数?: string;
  特征?: string;
}

export interface PowerSystemGenerateResponse {
  ok: boolean;
  体系?: PowerSystemDef[];
  detail?: string;
  原文?: string;
}

export interface TierGenerateResponse {
  ok: boolean;
  档位层?: PowerTier[];
  detail?: string;
}

/** 角色修炼档案（挂在角色卡的 `card.修炼` 下）。 */
export interface CultivationProfile {
  当前境界?: string;
  当前小境界?: string;
  档位?: string;
  天赋?: string;
  瓶颈?: string;
  目标境界?: string;
  代价?: string;
  来历?: string;
  路线?: string[];
}

export interface CultivationPlanResponse {
  ok: boolean;
  路线?: CultivationProfile;
  /** 由已有数据推导的处境（不调模型）。 */
  处境?: string;
  detail?: string;
}

// ==================================================================
// KG v3：世界观 / 剧情时间线 / Worker 补抽与恢复
// ==================================================================

/** KG v3 批抽 worker 的最近一次运行行。 */
export interface KgV3WorkerRun {
  id: number;
  segment_no: number | null;
  status: string;
  stuck: boolean;
  empty: boolean;
  duration_ms: number | null;
  batch_stats: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
}

/** KG v3 批抽 worker 状态（线程存活 / 队列积压 / 汇总计数 / 最近行）。 */
export interface KgV3WorkerStatus {
  worker_alive: boolean;
  pending: number;
  summary: { success: number; failed: number; running: number; stuck: number; empty: number };
  recent_runs: KgV3WorkerRun[];
}

export interface KgStoryEvidence {
  chapter?: number | null;
  event_type?: string;
  summary?: string;
}

export interface KgWorldviewEntity {
  name: string;
  aliases?: string[];
  first_chapter?: number | null;
  last_chapter?: number | null;
  mention_count?: number | null;
  kind_origin?: string;
  /** defined = 来自「力量体系」页的作者定义（非 KG 抽取）。 */
  source?: string;
  attrs?: Record<string, unknown>;
  evidence?: KgStoryEvidence[];
}

/** GET /api/projects/{id}/kg-v3/worldview。 */
export interface KgWorldviewResponse {
  /** null = 本书（生成正文）的 KG。 */
  novel_id: number | null;
  factions: KgWorldviewEntity[];
  locations: KgWorldviewEntity[];
  power_systems: KgWorldviewEntity[];
  resources: KgWorldviewEntity[];
  rules: KgWorldviewEntity[];
  counts: Record<string, number>;
}

export interface KgStoryTimelineEvent {
  chapter?: number | null;
  who?: string;
  where?: string;
  what?: string;
  result?: string;
  event_type?: string;
  [key: string]: unknown;
}

/** GET /api/projects/{id}/kg-v3/timeline。 */
export interface KgStoryTimelineResponse {
  /** null = 本书（生成正文）的 KG。 */
  novel_id: number | null;
  from_chapter: number;
  to_chapter: number | null;
  offset: number;
  limit: number;
  total: number;
  events: KgStoryTimelineEvent[];
  gaps?: number[][];
}

/** POST /api/projects/{id}/kg-v3/worker/backfill。 */
export interface KgBackfillResponse {
  enqueued: { segment_no: number; run_id?: number }[];
  skipped: { segment_no: number; reason: string; run_id?: number }[];
}

/** POST /api/projects/{id}/kg-v3/worker/recover。 */
export interface KgRecoverResponse {
  requeued: number;
  missing: number[];
  overflow?: unknown[];
  skipped?: unknown[];
  segments?: unknown[];
}

/** 文风档案里「禁用词与档案正文自相矛盾」的一条。 */
export interface StyleNarrativeConflict {
  word: string;
  dimension: string;
  rule_dimension: string;
  context: string;
  rule_text: string;
}

/** GET /api/projects/{id}/style-narrative/conflicts（只读，不调模型）。 */
export interface StyleNarrativeConflictsResponse {
  has_archive: boolean;
  /** error 级：会挡住生成（档案正文在教模型用禁用词）。 */
  conflicts: StyleNarrativeConflict[];
  /** 名词性用法：列出但不挡生成。 */
  attributive: StyleNarrativeConflict[];
  can_generate: boolean;
  note: string;
}

// ==================================================================
// 章纲写入/编辑（独立页 /projects/:id/outline）
// ------------------------------------------------------------------
// 后端仍存 projects.outline（list[str]，缺失是空串）。这里镜像
// routers/chapter_outline.py 的返回：状态逐章显式给出，校验回执
// 固定带 checks / skipped_checks —— 界面不许把它折叠成一句「成功」。
// ==================================================================

/** 单章在章纲里的状态；已写正文却无章纲时后端报 missing（不是 beyond_end）。 */
export type ChapterOutlineStatus = "confirmed" | "missing" | "beyond_end";

/** 校验命中的原文位置（硬拦专名/时间标记时带）。 */
export interface ChapterOutlinePosition {
  start: number;
  end: number;
  snippet: string;
}

/** 一条校验错误或告警；`term` 是命中的具体词，`position` 是它出现的位置。 */
export interface ChapterOutlineIssue {
  code: string;
  message: string;
  chapter?: number;
  term?: string;
  position?: ChapterOutlinePosition;
  [key: string]: unknown;
}

/** 一项校验的结论：checked / skipped / failed。 */
export interface ChapterOutlineCheck {
  rule: string;
  status: "checked" | "skipped" | "failed";
  detail?: string;
  reason?: string;
}

/** 保存/起草共用的校验回执；`skipped_checks` 与 `checks` 里的 skipped 对应。 */
export interface ChapterOutlineValidation {
  ok: boolean;
  errors: ChapterOutlineIssue[];
  warnings: ChapterOutlineIssue[];
  checks: ChapterOutlineCheck[];
  skipped_checks: string[];
}

/** GET 返回的单章条目。 */
export interface ChapterOutlineEntry {
  chapter: number;
  entry: string | null;
  status: ChapterOutlineStatus;
  /** 正文是否已经写到这一章。 */
  written: boolean;
  node_id: string | null;
  node_range: [number, number] | null;
}

/** GET /projects/{id}/chapter-outline 的响应。 */
export interface ChapterOutlineListResponse {
  project_id: number;
  outline_len: number;
  written_chapters: number;
  missing_chapters: number[];
  first_missing_chapter: number | null;
  source_fingerprint: string;
  entries: ChapterOutlineEntry[];
  checks: ChapterOutlineCheck[];
  skipped_checks: string[];
  warnings: ChapterOutlineIssue[];
}

/** 改章纲对时间线的影响：默认只回执，不重建节点。 */
export interface ChapterOutlineTimelineImpact {
  outline_len_before: number;
  outline_len_after: number;
  chapter_ranges_recomputed: boolean;
  node_content_stale: boolean;
  reset_timeline: boolean;
  note: string;
}

/** PUT / apply 的写回执（entry 仅单章保存有）。 */
export interface ChapterOutlineWriteResponse {
  project_id: number;
  from_chapter?: number;
  to_chapter?: number;
  entry?: { chapter: number; entry: string | null; status: ChapterOutlineStatus };
  auto_filled_missing: number[];
  changed: number[];
  validation: ChapterOutlineValidation;
  timeline_impact: ChapterOutlineTimelineImpact;
  source_fingerprint: string;
}

/** PUT /projects/{id}/chapter-outline/{chapter} 请求体。 */
export interface ChapterOutlinePutRequest {
  entry: string;
  expected_fingerprint: string;
  confirm: boolean;
}

/** apply 的单章条目；`entry: null` 表示显式缺省。 */
export interface ChapterOutlineEntryInput {
  chapter: number;
  entry: string | null;
}

/** POST /projects/{id}/chapter-outline/apply 请求体。 */
export interface ChapterOutlineApplyRequest {
  from_chapter: number;
  to_chapter: number;
  entries: ChapterOutlineEntryInput[];
  expected_fingerprint: string;
  confirm: boolean;
  /** 显式 true 才清空时间线（会丢已完成进度）。 */
  reset_timeline?: boolean;
}

/** POST /projects/{id}/chapter-outline/draft 请求体（只起草，不落库）。 */
export interface ChapterOutlineDraftRequest {
  from_chapter: number;
  to_chapter: number;
  direction?: string | null;
}

/** 起草响应：候选 + 指纹 + 预校验，必须人工确认后再 apply。 */
export interface ChapterOutlineDraftResponse {
  project_id: number;
  status: "draft";
  from_chapter: number;
  to_chapter: number;
  entries: ChapterOutlineEntryInput[];
  validation: ChapterOutlineValidation;
  apply_requires_confirm: boolean;
  source_fingerprint: string;
}


// ---------- 设定卡（人物 / 势力 / 场景 / 道具 / 规则 / 自定义） ----------
export type StoryCardKind =
  | "character"
  | "faction"
  | "scene"
  | "item"
  | "rule"
  | "custom";

/** 字段规格：后端 FIELD_SCHEMA 原样下发，前端据此渲染表单。 */
export interface StoryCardFieldSpec {
  key: string;
  label: string;
  hint?: string;
  multiline?: boolean;
  max_chars?: number;
}

export interface StoryCardKindSpec {
  kind: StoryCardKind;
  label: string;
  fields: StoryCardFieldSpec[];
  /** true = 字段名由用户自拟（自定义卡），界面渲染可增删的「字段名 + 内容」行。 */
  freeform?: boolean;
}

export interface StoryCardOut {
  id: number;
  project_id: number;
  kind: StoryCardKind;
  title: string;
  fields: Record<string, string>;
  enabled: boolean;
  sort_order: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface StoryCardListResponse {
  project_id: number;
  kinds: StoryCardKindSpec[];
  cards: StoryCardOut[];
  /** false = 还没从剧情圣经 / 知识图谱自动同步过，前端可触发一次同步。 */
  seeded?: boolean;
}

export interface StoryCardSyncResponse {
  project_id: number;
  created: number;
  skipped: number;
  /** 本次有映射字段被更新的卡数（旧字段名，兼容保留）。 */
  filled: number;
  /** 本次有字段更新的卡数。 */
  updated?: number;
  /** 因作者手改而保留字段的卡数。 */
  kept?: number;
  seeded: boolean;
  cards: StoryCardOut[];
}

export interface StoryCardCreateRequest {
  kind: StoryCardKind;
  title: string;
  fields?: Record<string, string>;
  enabled?: boolean;
  sort_order?: number;
}

export interface StoryCardPatchRequest {
  kind?: StoryCardKind;
  title?: string;
  fields?: Record<string, string>;
  enabled?: boolean;
  sort_order?: number;
}


// ---------- 世界观（作者手写：一句话世界 / 硬规则 / 势力与地点） ----------
export interface WorldBackground {
  "一句话世界"?: string;
  "硬规则"?: string[];
  "势力与地点"?: string[];
}

export interface WorldBackgroundResponse {
  background: WorldBackground;
}

export interface WorldBackgroundSaveRequest {
  background: WorldBackground;
}

export interface WorldBackgroundGenerateRequest {
  note?: string;
}

export interface WorldBackgroundGenerateResponse {
  ok: boolean;
  background?: WorldBackground;
  detail?: string;
  /** AI 回了但不像背景组时的原文（截断），便于作者手工摘取。 */
  "原文"?: string;
}


// ---------- 伏笔台账（作者意图层，与 KG 事实层分开） ----------
export type ForeshadowStatus = "open" | "resolved" | "dropped";

/** 伏笔级别：主线（全书）/ 卷级 / 小节级。 */
export type ForeshadowTier = "main" | "arc" | "chapter";

/** 节奏规划里的一个升级/呼应点。 */
export interface ForeshadowPlanBeat {
  chapter: number;
  kind: string;
}

/** 主线揭晓窗口里的一层。 */
export interface ForeshadowPlanLayer {
  chapter: number;
  layer: number;
}

export interface ForeshadowPlanEntry {
  id: number;
  title: string;
  tier: ForeshadowTier;
  current: {
    planted_chapter: number | null;
    planned_chapter: number | null;
  };
  plan: {
    plant_chapter: number;
    payoff_chapter: number | null;
    span: number;
    beats: ForeshadowPlanBeat[];
    reveal_layers: ForeshadowPlanLayer[];
  };
  issues: string[];
}

export interface ForeshadowPlanResponse {
  project_id: number;
  total_chapters: number;
  entries: ForeshadowPlanEntry[];
  summary: {
    counts: Record<string, number>;
    issues: string[];
  };
  /** apply 端点额外返回实际写入条数。 */
  applied?: number;
}

export interface ForeshadowEntry {
  id: number;
  project_id: number;
  title: string;
  status: ForeshadowStatus;
  tier: ForeshadowTier;
  planted_chapter: number | null;
  planned_chapter: number | null;
  resolved_chapter: number | null;
  evidence: string;
  resolution: string;
  /** 主线伏笔的真相：只在作者侧与揭晓章使用，平时不进写作提示。 */
  truth: string;
  /** 主线伏笔的暗示点（每行一条）。 */
  hints: string;
  /** 主线揭晓跨度（章）：三层反转在这个窗口内均匀分布。 */
  reveal_span: number | null;
  /** 全书计划章数（百分比揭晓窗口的分母）。 */
  total_chapters: number | null;
  /** 揭晓起点占全书比例（0–1）。 */
  reveal_ratio: number | null;
  /** 揭晓跨度占全书比例（0–1）。 */
  reveal_span_ratio: number | null;
  /** 节奏规划：升级/呼应点。 */
  beat_plan: ForeshadowPlanBeat[];
  source: "manual" | "bible" | "kg" | string;
  created_at: string | null;
  updated_at: string | null;
}

export interface ForeshadowSummary {
  open: number;
  resolved: number;
  dropped: number;
}

export interface ForeshadowListResponse {
  project_id: number;
  entries: ForeshadowEntry[];
  summary: ForeshadowSummary;
  /** 剧情圣经 `foreshadowing` 里尚未导入台账的旧伏笔标题。 */
  legacy: string[];
  /** KG 联动清单（只读）：可登记的未填坑 + 已回收的回收建议。 */
  kg: ForeshadowKgLinkage;
}

/** KG 里仍 open 的坑：可一键登记进台账。 */
export interface ForeshadowKgHole {
  question: string;
  status: string;
  /** KG 的段号（不是台账的章号），登记时只写进「依据」。 */
  first_chapter: number | null;
}

/** 台账仍 open、但同名 KG 坑已不是 open：建议标记已回收。 */
export interface ForeshadowKgResolvable {
  entry_id: number;
  title: string;
  kg_status: string;
  first_chapter: number | null;
}

export interface ForeshadowKgLinkage {
  importable: ForeshadowKgHole[];
  resolvable: ForeshadowKgResolvable[];
}

export interface ForeshadowImportResponse {
  project_id: number;
  created: number;
  titles: string[];
  entries: ForeshadowEntry[];
  summary: ForeshadowSummary;
  legacy: string[];
  kg: ForeshadowKgLinkage;
}

export interface ForeshadowCreateRequest {
  title: string;
  status?: ForeshadowStatus;
  tier?: ForeshadowTier;
  planted_chapter?: number | null;
  planned_chapter?: number | null;
  resolved_chapter?: number | null;
  evidence?: string;
  resolution?: string;
  truth?: string;
  hints?: string;
  reveal_span?: number | null;
  total_chapters?: number | null;
  reveal_ratio?: number | null;
  reveal_span_ratio?: number | null;
}

export interface ForeshadowPatchRequest {
  title?: string;
  status?: ForeshadowStatus;
  tier?: ForeshadowTier;
  planted_chapter?: number | null;
  planned_chapter?: number | null;
  resolved_chapter?: number | null;
  evidence?: string;
  resolution?: string;
  truth?: string;
  hints?: string;
  reveal_span?: number | null;
  total_chapters?: number | null;
  reveal_ratio?: number | null;
  reveal_span_ratio?: number | null;
}

// ---------- 定向返修（文风规则 → 可执行返修指令） ----------

export interface StyleRepairIssue {
  kind: "metric" | "forbidden";
  /** 对应蒸馏档案里的维度，如「节奏控制」「禁用转折词」。 */
  dimension: string;
  title: string;
  /** 面向人的偏离说明：当前值 / 参考区间 / 命中的表达。 */
  detail: string;
  /** 面向模型的具体修法。 */
  fix: string;
  severity: "high" | "medium";
  /** 仅 metric：统计特征名、实测值、z 分数。 */
  feature?: string;
  value?: number;
  z?: number;
  /** 仅 forbidden：命中的短表达，如「突然」×2。 */
  hits?: string[];
}

export interface StyleRepairPlan {
  project_id: number;
  segment_no: number;
  generation_id: number;
  ok: boolean;
  summary: string;
  /** no_archive / empty_text / clean；有可修问题时为空串。 */
  reason: string;
  issues: StyleRepairIssue[];
  /** 可直接交给 invoke-generation 的返修指令；无问题时为空串。 */
  instruction: string;
  archive: {
    has_profile: boolean;
    has_rules: boolean;
  };
}

/** 自动返修前后的关键指标（与闸门同口径）。 */
export interface AutoRepairSnapshot {
  chars: number;
  paras: number;
  dialogue_chars: number;
  dialogue_ratio: number;
  ultra_short_paras: number;
  ultra_short_ratio: number;
  attribution: number;
  gate_passed: boolean;
  gate_skipped: boolean;
  gate_violations: string[];
}

export interface AutoRepairCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface AutoRepairAcceptance {
  ok: boolean;
  checks: AutoRepairCheck[];
  before: AutoRepairSnapshot;
  after: AutoRepairSnapshot;
}

/** POST /api/projects/{id}/segments/{n}/auto-repair 的返回。 */
export interface AutoRepairResult {
  ok: boolean;
  project_id: number;
  segment_no: number;
  generation_id: number;
  changed: boolean;
  deterministic_applied: boolean;
  before: AutoRepairSnapshot;
  after: AutoRepairSnapshot;
  deterministic: {
    changed: boolean;
    punctuation: { fixed: number; dropped_tail: number; changed: boolean; samples: string[] };
    numbers: { fixed: number; changed?: boolean; samples: string[] };
    multi_speaker: {
      before_paras: number;
      split: number;
      after_paras: number;
      changed: boolean;
      samples: string[];
    };
    quotes: { target: string; replaced: number; changed: boolean };
  };
  plan: {
    ok: boolean;
    summary: string;
    reason: string;
    issues: StyleRepairIssue[];
  };
  model: {
    used?: boolean;
    snapshot_id?: number;
    chars?: number;
    answer?: string;
    error?: string;
    rollback_error?: string;
  } | null;
  acceptance: AutoRepairAcceptance | null;
  rolled_back: boolean;
  text: string;
}

// ---------- 每本书专属素材包 ----------

export interface ProjectMaterial {
  id: number;
  project_id: number;
  job_id: number | null;
  origin: "research" | "manual" | string;
  title: string;
  url: string;
  snippet: string;
  summary: string;
  use_case: string;
  chapter_hint: number | null;
  category: string;
  published_year: number | null;
  published_at: string;
  provider: string;
  query: string;
  tags: string[];
  status: "raw" | "usable" | "hidden" | string;
  consolidated_by: string;
  use_count: number;
  last_used_at: string | null;
  created_at: string | null;
}

export interface ProjectMaterialListResponse {
  items: ProjectMaterial[];
  total: number;
  page: number;
  page_size: number;
}

export interface MaterialTocGroup {
  key: string;
  count: number;
}

export interface MaterialToc {
  stats: {
    total: number;
    usable: number;
    raw: number;
    hidden: number;
    unknown_year: number;
    used: number;
  };
  categories: MaterialTocGroup[];
  years: MaterialTocGroup[];
  use_cases: MaterialTocGroup[];
  directory: MaterialDirectorySection[];
  recent: { id: number; title: string; category: string }[];
}

export interface MaterialDirectoryItem {
  id: number;
  title: string;
  published_year: number | null;
  use_case: string;
}

export interface MaterialDirectorySection {
  category: string;
  count: number;
  items: MaterialDirectoryItem[];
}

export interface MaterialResearchJob {
  id: number;
  project_id: number;
  chapter_no: number | null;
  trigger: string;
  topics: string[];
  queries: string[];
  status: "queued" | "running" | "consolidating" | "done" | "partial" | "failed" | "cancelled" | string;
  target_raw: number;
  target_usable: number;
  raw_count: number;
  usable_count: number;
  zero_streak: number;
  attempts: number;
  error: string;
  meta: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
}

export interface MaterialJobListResponse {
  items: MaterialResearchJob[];
}

export interface MaterialCreateRequest {
  title: string;
  summary?: string;
  use_case?: string;
  category?: string;
  published_year?: number | null;
  url?: string;
  snippet?: string;
  tags?: string[];
  status?: string;
}

export type MaterialPatchRequest = Partial<MaterialCreateRequest>;
