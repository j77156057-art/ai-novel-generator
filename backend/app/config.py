from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 仓库根目录（backend/app/config.py → backend → 仓库根）。
#: 凡是从配置读到的相对路径（如 SEED_DIR）都按它解析，不依赖进程启动时的工作目录——
#: 否则 `cd backend` 启动与从仓库根启动会解析到两个不同的目录。
REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_yaml_defaults(path: str) -> dict:
    """读 providers.yaml 默认配置块；缺失或解析失败时返回空 dict（向后兼容老部署）。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    return data


# providers.yaml 默认值：单例加载一次，类字段默认值直接吃进去。
# env 仍可通过 WRITER_* / ASSISTANT_* 覆盖（pydantic-settings env 大小写不敏感）。
_PROVIDERS_YAML = _load_yaml_defaults(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "providers.yaml")
)
_WRITER_DEFAULTS = _PROVIDERS_YAML.get("writer") if isinstance(_PROVIDERS_YAML.get("writer"), dict) else {}
_ASSISTANT_DEFAULTS = _PROVIDERS_YAML.get("assistant") if isinstance(_PROVIDERS_YAML.get("assistant"), dict) else {}
_DISTILLER_DEFAULTS = _PROVIDERS_YAML.get("distiller") if isinstance(_PROVIDERS_YAML.get("distiller"), dict) else {}
#: KG 批抽专用预算块（与 writer 完全独立，不在 6 字段角色配置里）。
_KG_EXTRACT_DEFAULTS = _PROVIDERS_YAML.get("kg_extract") if isinstance(_PROVIDERS_YAML.get("kg_extract"), dict) else {}

#: `kg_extract.provider` 允许的显式渠道值（空 = 跟随主渠道 `llm_provider`）。
#: 与 `_build_channel_provider` 支持的分支同源；**未知值不是配置**，在 Settings
#: 构造时就抛错（启动即失败），绝不静默回退主渠道——回退会把
#: `KG_EXTRACT_PROVIDER=zhiphu` 这类笔误藏成「配置已生效」。
KG_EXTRACT_CHANNELS: frozenset[str] = frozenset(
    {"openrouter", "zhipu", "siliconflow", "minimax", "openai", "dashscope", "local"}
)

# Assistant / Distiller **显式留空**时的渠道回退表（backend/config/providers.yaml 的注释承诺）：
#   <role>.api_key 为空 → 按 <role>.provider 的渠道名去 legacy 字段取对应 *_api_key
#   <role>.base_url 为空 → 同上取对应 *_base_url
#
# 这里的「legacy」指的是 Writer 路径原本就有的老字段（zhipu_api_key / openrouter_base_url
# 等）——它们早在双轨拆分之前就存在，用户的 key 一直配在这些字段上。Assistant 只是
# **读零拷贝地借一把钥匙**，不写回、不改变 Writer 的任何字段，也不接 Writer 的模型链 /
# 单例 / 超时；两者仍然是完全独立的两条轨道。
#
# 未知渠道名（含空串）不在表内 → 返回 ""（保持「降级 LocalFallback」的既有行为），
# 不抛错：渠道名可能来自运行时 PUT /api/settings/llm 的自由输入。
_LEGACY_CHANNEL_FIELDS: dict[str, tuple[str, str]] = {
    "zhipu": ("zhipu_api_key", "zhipu_base_url"),
    "openrouter": ("openrouter_api_key", "openrouter_base_url"),
    "siliconflow": ("siliconflow_api_key", "siliconflow_base_url"),
    "minimax": ("minimax_api_key", "minimax_base_url"),
    "dashscope": ("dashscope_api_key", "dashscope_base_url"),
    # 通用 OpenAI 兼容接口：没有厂商默认值，端点必须来自本表（用户可覆盖）。
    "openai": ("openai_api_key", "openai_base_url"),
}

#: 走「渠道 legacy 字段回退」的角色（Writer 有自己的 legacy 路径，不走这张表）。
_CHANNEL_FALLBACK_ROLES: tuple[str, ...] = ("assistant", "distiller")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./novel_dev.db"
    # 相对路径按 REPO_ROOT 解析（见 app/ingestion/seed.py 的 resolve_seed_dir）。
    seed_dir: str = str(REPO_ROOT / "data" / "seed")
    chunk_size: int = 1200
    chunk_overlap: int = 200
    upload_max_bytes: int = 20_000_000

    # ------------------------------------------------------------------
    # 时代越界护栏（era_guard，独立区块）
    #
    # v17（2026-11-15）**默认打开，且维持 report（只判不拦）**：此前默认关，
    # 已落库章节没有任何时代错误率数据。打开后正文不改、`passed` 不受影响、
    # 不进 `violations`；扫描结果并进 `quality_json.era_check`。
    # 约束注入与扫描留痕是**两个独立子开关**（都默认开）：报告里必须能区分
    # 「本章注没注入那 209 字约束」，否则命中率变化分不清是正文变好还是被压住。
    # 项目没有显式 genre / 世界设定时不注入、不扫描（meta 记 baseline_unknown）。
    # 改写仍必须显式把 ERA_GUARD_MODE 设成 "rewrite"：实验已证明句级独立重写
    # 会把语义拼散（铜镜→导航、电脑→砚台）。
    # ------------------------------------------------------------------
    era_guard_enabled: bool = True
    #: 约束注入开关（默认开）：只决定 system 里加不加那 209 字时代约束。
    #: 与 `era_guard_scan_enabled` **互不牵连**——要纯观测命中率就关它、留着扫描。
    era_guard_prompt_enabled: bool = True
    #: 扫描留痕开关（默认开）：只决定生成后扫不扫、写不写 `quality_json.era_check`。
    #: 关掉后 `era_check.status` 记 `scan_disabled`，不再产命中数。
    era_guard_scan_enabled: bool = True
    era_guard_mode: str = "report"  # report | rewrite
    era_guard_max_rounds: int = 2

    # 通道滥用聚合（v18，只判不拦）：例外通道命中但同句缺引出人（留痕键
    # `channel_hit_no_introducer`）本来逐条只记录；某章扎堆或连续多章出现，
    # 说明通道被当成放行通道用，需要看到。阈值走配置，<=0 关闭对应判定。
    era_channel_abuse_threshold: int = 3  # 单章条数阈值
    era_channel_abuse_streak: int = 2  # 连续章数阈值
    era_channel_abuse_lookback: int = 8  # 回看最近多少条已落库留痕

    # ------------------------------------------------------------------
    # 一致性层（coherence，与文风闸门并排、互不干涉）
    #
    # 查四类硬伤：说话人归属 / 时间线 / 指代 / 伏笔提前点破。判定所需数据全部来自
    # 项目本身（plot_bible / outline / 前文正文 / 已有实体），代码里不写任何书名、
    # 人名、年份、章号。
    #
    # **默认只报不拦**：结果写进 `generations.quality_json["coherence"]`，
    # 不改变文风闸门的 `passed`。要让 definite 级问题触发重写，需显式设
    # COHERENCE_GUARD_MODE=rewrite；设为 off 则连计算都跳过。
    # ------------------------------------------------------------------
    coherence_guard_enabled: bool = True
    coherence_guard_mode: str = "report"  # report | rewrite | off

    # ------------------------------------------------------------------
    # canon 词表来源（KG_CANON_SOURCE，纯增量开关，默认行为不变）
    #
    # legacy：仓库里原来那套 canon —— 从主库已有节点筛出来的约 300 条 v2 词表，
    #         实际读取的是原著层 sqlite 的 new_nodes / new_edges。
    #         **它不是抽取链路的版本号**：抽取链路另有 v2（规则式 extractor）
    #         与 v3（LLM 抽取器），两条并行，与本开关无关。
    # cleaned：清洗后的 63 条 + 人物层复核并入 53 条（104 实体）
    #         → app/kg/data/canon_layers.json（实体层 + 称呼层）。
    #         实体层只放人物；称呼层只收带专名部分的写法（许郎/许公子/许二叔/
    #         魏公/临安公主/白莲道长），纯称谓（大哥/老爷/师父/夫人）不入称呼层。
    #         纯封号只有「唯一原文直证 + 全库唯一归属」可破例（长公主 → 怀庆）。
    # mixed：分域判定，不合成一份名单——人物域用 cleaned，势力/地点域用 legacy
    #         的 faction 节点，但必须过结构化后缀门，power_system/item 整体退出。
    # 读取点只有 app/kg/canon_source.py 一处；cleaned 读不出来时降级 legacy
    # 并记 WARN，绝不当空表（空表会让所有实体被判成 mech_only）。
    # 默认仍是 legacy；mixed 是可选口径，未翻默认值。
    # ------------------------------------------------------------------
    kg_canon_source: str = "legacy"  # legacy | cleaned | mixed

    # ------------------------------------------------------------------
    # 物品持有事实注入（生成侧读通道，2026-11-10）
    #
    # 只读 ``generated.db`` 的 kg_item_holding，不搬表、不 ATTACH、不改数据。
    # 关掉后 build_context 不组装 item_holding_block，prompt 与旧版逐字节相同，
    # 便于 A/B 对照「有/无持有约束」的生成差异。
    # ------------------------------------------------------------------
    kg_item_holding_inject: bool = True
    kg_generated_db: str = str(REPO_ROOT / "backend" / "data" / "generated.db")

    # 集合/上位端点的边级门（canon_source.collection_edge_gate）。
    # 默认关闭：2026-11-10 形态判据复核显示，三种结构判据在 A/B 两组上都做不到
    # 「拦住错的、放行对的」（旧门 1 真错/5 误伤、S 判据 2 真错/4 误伤且 A 组 0 拦、
    # 只按 kind 判组织则 10 真错/41 误伤），门本身净收益为负。
    # 关闭时门函数直接返回 None，边一律 status='active'；集合/上位节点照样保留
    # （mixed 边 710→724 的收益与门无关）。保留开关与 status 列，将来能开。
    kg_collection_gate: bool = False

    # ------------------------------------------------------------------
    # v3 批抽的超时重捞周期（秒）。
    #
    # 背景：写章节的进程可能是短命进程，内存队列随进程退出丢失，库里只剩
    # status=running 的行；旧实现只能等后端重启或人工 requeue 才捞得回来
    # （实测有两章卡在 running 18–30 小时）。worker 线程按本周期扫库，
    # 把超过 STUCK_MINUTES 的 running 行重新入队——不依赖重启。
    # <=0 关闭周期扫描（保留人工 /worker/recover 入口）。
    # ------------------------------------------------------------------
    kg_stuck_sweep_seconds: int = 120

    # 启动时收割「上次进程遗留的 running 行」（`kg.batch_worker.reap_stale_runs`）。
    #
    # 周期扫描（上面那个）只处理**超过 STUCK_MINUTES** 的行；进程被重启时
    # 队列（内存 deque）直接丢失，那一批行永远 running，要等 30 分钟才被扫到。
    # 启动收割把 `started_at` 早于本次启动的 running 行直接标 failed
    # （带 `interrupted` 标记，前端才不会算出「跑了 3 天」）。
    #
    # **默认关闭**：它会写库（改 AgentRun 状态），是否在启动时自动动手由部署方决定。
    # 关闭时仍可手动调 `reap_stale_runs(db)`。
    kg_reap_stale_on_startup: bool = False

    # 作者 agent 的「写后定向自查」（app.generation.author_research）。
    # 默认关闭（2026-11-13）：段 11 实测用段 9 的 1/58 自查 token 只换来与段 9
    # 持平的闸门表现（差 3），且段落类缺口在参考语料里方向相反，检索会帮倒忙。
    # 保留代码与开关，需要做对照实验时显式打开；关闭时 quality_json 记
    # `author_research.status = "disabled"`。
    author_research_enabled: bool = False

    # Embedding（M2 混合检索）
    embedding_provider: str = "dashscope"  # dashscope | local
    embedding_dim: int = 1536
    embedding_model: str = "text-embedding-v3"
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # LLM（M4 分段生成环）—— Writer 路径的全部原始字段，保留不动。
    llm_provider: str = "dashscope"  # dashscope | openrouter | local
    llm_model: str = "qwen-plus"
    llm_base_url: str = ""  # 空则复用 dashscope_base_url

    # OpenRouter（可选 provider：免费大上下文模型）
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # 实测 15 款 :free 模型后选定：
    #   apodex/apodex-1.1-mini:free            ~2.5s/段，中文干净（默认，适合分段循环生成）
    #   nvidia/nemotron-3-super-120b-a12b:free ~5s/段，质量好
    #   nvidia/nemotron-3-ultra-550b-a55b:free ~22s/段，1M 上下文，输出最好（长文用）
    openrouter_model: str = "apodex/apodex-1.1-mini:free"
    # 限流自动切换链（逗号分隔）：当前模型 429/403 时按序换下一个。
    # 默认链 = 实测可用（按速度）：mini → super → lightning → ultra。
    # 未列入：poolside/laguna（中英混杂）、inkling*(403)、gemma-4*(429)、lfm-2.5(400)。
    openrouter_models: str = (
        "apodex/apodex-1.1-mini:free,"
        "nvidia/nemotron-3-super-120b-a12b:free,"
        "nvidia/nemotron-3.5-lightning:free,"
        "nvidia/nemotron-3-ultra-550b-a55b:free"
    )

    # 智谱 GLM（open.bigmodel.cn）：GLM-4-Flash 系列免费不限 token，中文写作强，国内低延迟。
    # 默认升级到 glm-4-flash-250414（2025-04 快照）：7 类抽取全填，event_type 多样化，
    # in_story_date 100% 填充，knowledge/state/foreshadows 从 0 提升到 8/6/4，速度 -37%。
    # 依据：docs/kg-nemotron-compare-redirect.md N=4 实测（raw 数据未入库，仅本地保留）。
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_model: str = "glm-4-flash-250414"
    # 候选链：250414（默认，最优抽取）→ 旧 glm-4-flash（回退）→ glm-z1-flash（带推理）
    # → glm-4.6 / glm-4.5-air（付费档，长文与复杂结构更好）。链尾两款不免费，仅作末位兜底。
    zhipu_models: str = "glm-4-flash-250414,glm-4-flash,glm-z1-flash,glm-4.6,glm-4.5-air"

    # 硅基流动 SiliconFlow（api.siliconflow.cn）：国产开源模型最全，新用户送 2000 万 token
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_model: str = "Qwen/Qwen2.5-7B-Instruct"
    siliconflow_models: str = (
        "Qwen/Qwen2.5-7B-Instruct,"
        "THUDM/glm-4-9b-chat,"
        "Qwen/Qwen3-8B,"
        "Qwen/Qwen2.5-72B-Instruct"
    )

    # MiniMax（国内站 api.minimaxi.com；key 仅国内站有效，国际站 401）
    # 2026-10-12 实测 9 款模型：只有 M3.1-Flash-Preview 把推理放在独立字段 reasoning_content，
    # content 干净（JSON 可直接解析、中文质量好）；M3/M2.7/M2.5 等把 <think> 混进 content，
    # 且传 thinking:{"type":"disabled"} 无效。故默认与切换链均只用 M3.1-Flash-Preview；
    # 2026-11-13：MiniMax-Text-01 已下线（404 "is no longer available"），移出默认链，
    # 否则链尾每次回退都是一次注定失败的 404，把真错误伪装成「网络 404」。
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_model: str = "MiniMax-M3.1-Flash-Preview"
    minimax_models: str = "MiniMax-M3.1-Flash-Preview"
    #: 老键兜底：角色级 `*_thinking` 留空且渠道是 minimax 时，取本值当思考档
    #: （env `MINIMAX_REASONING_EFFORT`）。仍为空则回落 `DEFAULT_THINKING`（high）。
    #: 角色级显式值优先于本键——新键优先、老键继续可用。
    minimax_reasoning_effort: str = ""

    # 阿里百炼 DashScope（兼容 OpenAI 协议）
    dashscope_models: str = "qwen-plus,qwen-max,qwen-turbo,qwen-flash"

    # ------------------------------------------------------------------
    # 通用 OpenAI 兼容接口（新增渠道 "openai"）
    #
    # 不是某一家厂商：任何实现了 /chat/completions + Bearer 鉴权的服务都能接
    # （OpenAI 官方 / 自建 one-api / vLLM / LM Studio / LocalAI / 中转站…）。
    # 因此 base_url **必须由用户给完整地址**（形如 https://xxx/v1），
    # 默认仅回落到官方 https://api.openai.com/v1。
    # 复用既有的 OpenAICompatibleChatProvider（纯 stdlib urllib），零新增依赖。
    # ------------------------------------------------------------------
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_models: str = "gpt-4o-mini,gpt-4o"

    # ------------------------------------------------------------------
    # ADR-2026-10-12 第 2 步：Writer / Assistant 双轨隔离（第 8 步再加 Distiller 第三轨）。
    #
    # 6 字段各自独立（provider / api_key / base_url / model / max_tokens / timeout），
    # 默认值来自 config/providers.yaml，env 可覆盖
    #（WRITER_* / ASSISTANT_* / DISTILLER_*）。
    #
    # Writer 字段默认空：工厂 get_provider_config("writer") 看到 provider 为空时，
    # 会委托给 get_llm_provider() 的原 Writer 单例（不复制构建逻辑），保证 b58c763
    # 的角色锁定 / 节拍约束 / KG 透传 / 提示词限长不被绕过。
    #
    # Assistant 字段默认 zhipu / glm-4-flash：永久免费不限 token、中文诊断稳，
    # 且与 Writer 默认 zhipu 共享渠道但**完全独立**的 api_key / timeout / max_tokens。
    # ------------------------------------------------------------------
    writer_provider: str = _WRITER_DEFAULTS.get("provider", "")
    writer_api_key: str = _WRITER_DEFAULTS.get("api_key", "")
    writer_base_url: str = _WRITER_DEFAULTS.get("base_url", "")
    writer_model: str = _WRITER_DEFAULTS.get("model", "")
    #: Writer 单次输出上限。默认值与 config/providers.yaml 的 writer.max_tokens
    #: 保持一致（2026-11-07 由 8192 提到 65536）：MiniMax M3.1-Flash-Preview 的
    #: reasoning_content 会先吃 max_tokens，32768 档实测出现整章 0 字正文。
    writer_max_tokens: int = int(_WRITER_DEFAULTS.get("max_tokens", 65536))
    #: 2026-11-14 由 120 提到 180，与流式 stream_timeout 同档：4000 字级单章
    #: 实测一次生成需 180s 上下，120s 的同步入口在长篇幅下必然读超时。
    writer_timeout: int = int(_WRITER_DEFAULTS.get("timeout", 180))
    #: 流式续写的读超时。此前写死在 provider 函数的默认参数里，换机器/换模型
    #: 只能改代码；现与 `writer_timeout` 成对配置（默认 180 = 现网值）。
    writer_stream_timeout: int = int(_WRITER_DEFAULTS.get("stream_timeout", 180))
    #: Writer 角色的思考档位（`_thinking_kwargs` 翻译成 provider 构造参数）。
    #: 空 = 走 `effective_thinking()` 的解析（MINIMAX_REASONING_EFFORT 老键 → high）。
    writer_thinking: str = ""
    #: 预算地板开关：on（默认）时不足 FLOOR_FULL(32768/300) 自动抬高；
    #: off 时严格按 `writer_max_tokens` / `writer_timeout` 执行，不做任何抬升。
    writer_budget_floor: bool = bool(_WRITER_DEFAULTS.get("budget_floor", True))
    #: 文风改写开关（生成三档预设会写这里）：极速档关闭、平衡/高质量档开启。
    #: 项目级 `projects.style_rewrite` 显式设置时以项目为准。
    style_rewrite: bool = True
    #: 文风改写的成本闸门：开 = 只在偏离参考书「严重」时才多花一次模型调用；
    #: 关 = 只要 `style_rewrite` 开就一律改写。判据见 `engine._rewrite_gate`。
    style_rewrite_gate: bool = True
    #: 改写这一步单独指定模型（空 = 与写手同一个 provider）。
    #: 渠道不认这个模型 / 没 key / 构建抛错时自动回落写手 provider。
    style_rewrite_model: str = "MiniMax-M3"
    #: 形态校正：把对白的组织方式掰到参考书自己的形态（逐单元分布带）。
    #: 实测提示层挪不动对白形态，只有事后改写能改。
    style_form_correction: bool = True
    #: 形态校正的做法：`merge` = 只做确定性并段（零模型调用）；
    #: `rewrite` = 并段后再调一次模型重写不达标段落。
    style_form_correction_mode: str = "merge"
    #: 引语补全：给连续对白段补「XX道：」这类引导语，直到声音归属进带。
    style_lead_in: bool = True
    #: 归属校正：把「说话人归属」的写法密度掰到参考书自己的（实测差 5 倍）。
    style_attribution_correction: bool = True
    #: 省略号习惯校正：对白结尾省略号密度对齐参考书（机械改动，代价低）。
    style_ellipsis_correction: bool = True
    #: beat 插入：连续对白段开头补一句动作/神态叙述。默认关——它**新增**叙述，
    #: 是唯一会动情节信息量的一道，要显式开。
    style_beat_insert: bool = False
    #: 照搬门禁：正文与参考语料逐字重合超过阈值就必须改写，改不掉不落库。
    style_verbatim_gate: bool = True
    #: 照搬判定阈值（连续相同字数），与提示里写的一致。
    style_verbatim_threshold: int = 25
    #: 照搬回灌的最多改写次数（0 = 只判不改，超标直接判失败）。
    style_verbatim_max_rewrites: int = 2
    #: 非流式补全（重写轮走的 `complete_message`）的**总时长硬顶**，默认 960s。
    #:
    #: 2026-11-14 新增：`writer_timeout` 只是 socket 每次 recv 的空闲超时，不是
    #: 总时长——服务端慢速滴包会不断重置它，模型链 × 每模型 4 次重试还会把它乘
    #: 起来。第 16 章实测一次重写静默挂 19 分钟才被人工终止。这里给整条调用
    #: （含换模型、429 退避）一个硬截止。
    #:
    #: **本条只是极端兜底，挂起的主判据是吞吐速率**（provider 的
    #: `_RATE_FLOOR_BPS` / `_RATE_WINDOW_SECONDS`）：「每 1s 滴 1 字节」那种
    #: 假活响应在速率窗口内就会被判死，走不到这里。
    #:
    #: 为什么从 720 提到 960（= max×2）：11 章样本单次重写耗时 p50=294s、
    #: p90=464s、max=481s；但 720s（原「max×1.5」）在第 17 章掐断了一次
    #: 「慢但正常」的重写，终稿回落初稿、段均 19.4 违规——max 已经漂了，
    #: 不能再按 max×1.5 贴着取。960s 只用于兜住真正无响应的调用。
    writer_total_timeout: int = int(_WRITER_DEFAULTS.get("total_timeout", 960))
    #: KG 批抽**专用**的单次 `complete_message` 总时长硬顶（秒）。默认 300。
    #:
    #: 为什么必须独立：KG 抽取复用了 writer 的 provider 单例，于是继承它的
    #: 960s 预算——而抽取是「1 段正文 → 一坨结构化 JSON」的小任务，实测
    #: `agent_runs #205` 连续 3 次各烧满 960s 才报 `The read operation timed out`，
    #: 整段失败耗时 48 分钟。抽取不该沿用长文生成的预算。
    #:
    #: 最坏总时长 = 外层 `RETRY_LIMIT + 1`（默认 3）× 本值 = 3 × 300s = 15 分钟；
    #: 首字节挂起则每次在 `kg_extract_first_byte_timeout` 内先暴露。
    #: 只影响 KG 批抽（`get_kg_extract_provider()`），**writer/正文生成不读它**。
    kg_extract_total_timeout: int = int(
        _KG_EXTRACT_DEFAULTS.get("total_timeout", 300)
    )
    #: KG 批抽**首字节阶段**上限（秒）。默认 120。
    #:
    #: 改造前首字节前直接用剩余总预算当 socket 超时（`provider.py` 的
    #: `urlopen(timeout=remaining)`），180s 的 `writer_timeout` 被完全绕过，
    #: 服务端一个字节都不给时要等整份预算耗尽才报错。抽取路径传本值后，
    #: 挂起在 2 分钟内就被判死，而不是拖到 5 分钟（总预算）或 16 分钟（旧 writer 预算）。
    #: None/0 = 沿用原行为（只受总预算约束）——writer 路径不传，行为不变。
    kg_extract_first_byte_timeout: int = int(
        _KG_EXTRACT_DEFAULTS.get("first_byte_timeout", 120)
    )
    #: KG 批抽**独立渠道**（可覆盖项，env `KG_EXTRACT_PROVIDER`）。
    #:
    #: - 空（默认）= 跟随主渠道 `llm_provider`，现有行为零变化；
    #: - `zhipu` = 抽取改走智谱：key / base_url 取 `zhipu_*`，模型链取库里
    #:   `llm.zhipu_models`（启动时由 `load_persisted_llm_config` 落进
    #:   `settings.zhipu_models`），预算仍用上面 300s / 120s；
    #: - 其它非空且不在 `KG_EXTRACT_CHANNELS` 内的值 = **配置错误**，Settings
    #:   构造时直接抛错（启动失败），不静默回退主渠道。
    #:
    #: 只影响 `get_kg_extract_provider()`；writer/正文生成不读本字段。
    kg_extract_provider: str = str(_KG_EXTRACT_DEFAULTS.get("provider", "") or "")
    #: KG 抽取的**自定义模型链**（逗号分隔，空 = 用所选渠道自己的链）。
    #:
    #: 为什么不直接写库里的 `llm.<channel>_models`：那条链是**主渠道共用**的，
    #: 抽取侧改它等于顺手改了写正文的候选链（违反「不改 writer 渠道与预算」）。
    #: 这里单独一槽，`get_kg_extract_provider()` 才会套用；Writer 完全不读。
    #: 由 `PUT /api/settings/llm` 的 `kg_extract.models` 写入并热生效。
    kg_extract_models: str = str(_KG_EXTRACT_DEFAULTS.get("models", "") or "")
    #: KG 抽取的**跨渠道兜底列表**（逗号分隔渠道名，按序尝试；默认空 = 关闭）。
    #:
    #: 为什么需要：渠道内的 `models` 链共用同一份 base_url / api_key，只能换
    #: model id；MiniMax 对抽取类请求整条挂死时，换 id 救不回来。这里让主渠道
    #: 在超时 / 连接错误 / 429 / 5xx 后切到另一渠道（自己的 key 与模型链）。
    #: 只有可用后备渠道时才包一层 `FallbackChannelProvider`；空列表 = 原路径。
    #: 未知渠道名或未配 key 的条目会写 `channel_skipped_no_key` 并跳过。
    #: env：`KG_EXTRACT_FALLBACK_PROVIDERS`。
    kg_extract_fallback_providers: str = str(
        _KG_EXTRACT_DEFAULTS.get("fallback_providers", "") or ""
    )
    #: 跨渠道兜底的**链级预算**（秒），主渠道 + 后备渠道共享，默认 300。
    #:
    #: 每个渠道拿到「链剩余额度」与自身 `kg_extract_total_timeout` 的较小值，
    #: 所以整批最坏仍是 `(RETRY_LIMIT + 1) × 300s ≈ 15 分钟`，不会叠加成 3×600。
    #: env：`KG_EXTRACT_FALLBACK_CHAIN_TIMEOUT`。
    kg_extract_fallback_chain_timeout: int = int(
        _KG_EXTRACT_DEFAULTS.get("fallback_chain_timeout", 300)
    )
    #: 重写允许的字数上浮比例，默认 8%。**手写值，不是档案推导**：来源是第 4 章
    #: 实测（2,987→2,868 汉字，段均 23.2→24.8）——修碎段要给短对白补神态/动作，
    #: 必然增字。它只防借补写无限膨胀，不是篇幅预算；篇幅上限由 `length_limits`
    #: （generation_length × 1.2）决定。
    rewrite_growth_allowance: float = 0.08
    #: 重写「跑飞」回退阈值，默认 16%（= 上浮比例的 2 倍）。同样是**手写值，
    #: 不是档案推导**，来源与上一条相同；只用于整轮回退，不写进提示词。
    rewrite_growth_reject_ratio: float = 0.16
    #: 重写轮数上限（**调度项**，不改任何闸门判据/阈值/违规项定义）。
    #: 默认 2 = 旧行为；设 1 只允许一轮；设 0 关闭重写轮（超长仍走压缩兜底）。
    rewrite_max_rounds: int = 2
    #: 一轮重写没有带来可测改善时，是否停止后续重写轮（默认 True）。
    #: 「可测改善」只读既有闸门输出：从不过变为通过，或剩余问题条数严格减少。
    #: 设为 False 可回退到旧的「跑满 rewrite_max_rounds」调度。
    rewrite_stop_without_gain: bool = True
    #: 初稿已超字数硬上限时，是否跳过整章重写、直接走压缩（默认 True）。
    #: 依据：2026-11-15 第 23–32 章报告——超长章的重写轮净收益为负
    #: （第 24 章两轮净 −4 字仍超限，第 23 章两轮全丢），压缩才是有效手段。
    #: 设为 False 可回退「先重写、仍超再压缩」的旧调度。
    rewrite_skip_oversize: bool = True
    #: 初稿补完轮数上限（**调度项**）。默认 2 = 旧行为；设 0 关闭补写。
    draft_completion_max_rounds: int = 2
    #: 初稿提示里的字数下限占目标字数的比例（**只影响提示措辞**，不改任何闸门）。
    #: None（默认）= 取 `_archive_rhythm()` 推出的 `completion_min_ratio`，
    #: 即与补完触发线**同一条线**（本项目第 32 章为 0.826 → 4000×0.826=3304 字）；
    #: 旧行为是写死 `max_chars × 0.8`（3200 字，比触发线低 104 字，模型按提示
    #: 写 3200 仍会被判偏短），所以**默认值不等于旧行为**。设 0.8 可退回旧口径。
    draft_prompt_min_ratio: float | None = None
    #: 卷长（章）的**退回值**：优先从项目大纲的卷结构解析，解析不到才用这里。
    #: 默认 14 是本书卷 1 的章数；换项目且大纲无卷标记时必须改。
    volume_chapters: int = 14
    #: `极短纯对白段占比` 上限的**退回值**（v10）：正常路径由参考档案的
    #: 「非零章 p90」现推，换参考书自动重推；只有参考书里该指标全为 0
    #: （推不出非零章分位）时才用这里。留空表示不设配置覆盖，退回常量表
    #: （0.0，即旧口径），并由 `gate_limits.fallback_used/fallback_reason` 留痕。
    ultra_short_dialogue_max_ratio: float | None = None
    #: `对白字占比` 参考线（p25/p50/p75）的**退回值**（v12，非判罚）：正常路径由
    #: 参考档案的章级分位现推，换参考书自动重推；只有无参考书/有效章不足时
    #: 才用这里。留空表示不设配置覆盖，退回常量表
    #: `ARCHIVE_CHAPTER_PERCENTILES["对白字占比"]`，并由
    #: `gate_limits.fallback_used/fallback_reason` 留痕。
    dialogue_ratio_reference: list[float] | None = None
    #: `句长均值` 参考线（p10/p50/p90）的**退回值**（v13，非判罚）：正常路径由
    #: 参考档案的卷级分位现推，换参考书自动重推；只有无参考书/有效卷不足时
    #: 才用这里。留空表示不设配置覆盖，退回常量表
    #: `ARCHIVE_CHAPTER_PERCENTILES["句长均值"]`，并沿用 `fallback_used` 留痕。
    sentence_length_reference: list[float] | None = None
    #: `短句占比` / `长句占比` 参考（p10/p50/p90）的**退回值**（v14，非判罚）：
    #: 正常路径由参考档案的卷口径分位现推，换参考书自动重推；只有无参考书/
    #: 有效卷不足时才用这里，留空退回块级常量表并沿用 `fallback_used` 留痕。
    short_sentence_ratio_reference: list[float] | None = None
    long_sentence_ratio_reference: list[float] | None = None
    #: 对白句类配比的**退回值**（v14，非判罚），依序为
    #: [陈述句 p50, 问句 p25, 问句 p90, 感叹句首个非零分位, 叹词起头首个非零分位]。
    #: 正常路径由参考书**章级**分位现推。
    dialogue_mix_reference: list[float] | None = None
    #: 角色名提取里「泛称/无名」词表的**退回值**（v14）。此前是代码常量
    #: `_VOICE_GENERIC_MARKERS`；这组词无法由参考书频次分位生成（频次分不出
    #: 「泛称」与「高频主角」），故降级为配置项。留空用原常量表。
    voice_generic_markers: list[str] | None = None
    #: 初稿被 `finish_reason=length` 截断后，**优先重生成**的次数（v15）。
    #: 默认 1：一次足以区分「偶发截断」与「稳定撞 max_tokens 硬顶」；设 0 即
    #: 直接降级为「压缩断头稿」并在 `quality_json.truncation` 留痕。
    #: 与档案无关（供给侧行为），所以是配置项而不是分位推导。
    truncation_regen_attempts: int = 1
    #: 结构约束检测（v15）：`report` 只告警落库 / `block` 阻断落库。
    #: 检测口径全部来自 `project.outline` 与角色卡，不含任何写死的词表。
    structure_guard_mode: str = "report"
    #: 本章情节点「覆盖」判据：该事件的中文二元组有这么多比例出现在正文里。
    #: 条数不足 `structure_beat_min_units` 时，同一阈值改判「本章大纲独有词
    #: 三元组覆盖率」（`basis=fallback_term_coverage`），并与其他项同权进入
    #: `blocking_reasons`；`report` 模式仍只判不拦。
    #: **待验证**：0.34 是退回值（无档案可推，取「同义改写容忍度约三分之一」的
    #: 起点），没有经过样本校准，不是推导值。
    structure_beat_min_coverage: float = 0.34
    #: 情节点切分后至少要有这么多条，覆盖率才有区分度（不足时 1/1 恒 1.00）。
    #: 不足时**不跳过**，改用 `fallback_term_coverage` 作正式判据。
    #: **待验证**：3 是退回值——少于 3 条时覆盖率只剩 0/0.5/1 这种量级，
    #: 不具区分度；条数阈值本身未按样本校准，改了要重跑回放报告。
    structure_beat_min_units: int = 3
    #: 越章材料只查当前章之后的这么多章；超出的章不参与差集（太远易误报）。
    #: **待验证**：3 是退回值，只用来压住远端章的同词噪声，未经样本校准。
    structure_premature_lookahead: int = 3
    #: 越章材料命中的章节数超过它才判阻断（默认 3 = 四章全越才拦）。
    #: **待验证**：3 是退回值，目的是先只抓「大面积搬运后续材料」，未校准。
    structure_premature_max_hits: int = 3
    #: 性别不一致的条数超过它才判阻断（默认 0 = 一条都不许）。
    #: 该值不待验证：检测口径只认「同小句内明确性别断言词」，一条即硬冲突。
    structure_gender_max_mismatches: int = 0
    #: 名字前后多少字符内找「明确性别断言词」作为该角色的一致性证据。
    #: **待验证**：12 字是退回值（约一个小句），未按真实误报率校准。
    structure_gender_window: int = 12
    #: 逐章推进台账（v1）：角色阵容停滞 / 属性零变化 / 章间断档。
    #: **只判不拦**：台账只出报告，不改 `passed`、不进 `violations`。
    #: 各字段为 `None` 时用 `app.generation.progress_ledger.DEFAULTS` 的兜底值，
    #: 避免同一阈值在两处各写一遍。所有数值均「待验证」，口径见该模块文档。
    progress_ledger_enabled: bool = True
    progress_ledger_cast_overlap_high: float | None = None
    progress_ledger_cast_core_max: int | None = None
    progress_ledger_cast_stagnant_chapters: int | None = None
    progress_ledger_attr_stale_chapters: int | None = None
    progress_ledger_head_paragraphs: int | None = None
    progress_ledger_tail_paragraphs: int | None = None
    progress_ledger_question_tail_paragraphs: int | None = None
    progress_ledger_question_top: int | None = None
    progress_ledger_question_overlap: float | None = None
    progress_ledger_time_jump_min_days: int | None = None
    progress_ledger_window_chapters: int | None = None
    #: 连通性探测（设置页「测试连接」）用的超时：同步 120 / 流式 180，
    #: 与旧 `build_provider` 默认值逐秒一致，env 可覆盖（LLM_PROBE_TIMEOUT）。
    llm_probe_timeout: int = 120
    llm_stream_timeout: int = 180

    assistant_provider: str = _ASSISTANT_DEFAULTS.get("provider", "zhipu")
    assistant_api_key: str = _ASSISTANT_DEFAULTS.get("api_key", "")
    assistant_base_url: str = _ASSISTANT_DEFAULTS.get("base_url", "")
    assistant_model: str = _ASSISTANT_DEFAULTS.get("model", "glm-4-flash")
    assistant_max_tokens: int = int(_ASSISTANT_DEFAULTS.get("max_tokens", 1024))
    assistant_timeout: int = int(_ASSISTANT_DEFAULTS.get("timeout", 60))
    #: Assistant 角色的思考档位（空 = 走 `effective_thinking()` 解析）。
    assistant_thinking: str = ""
    #: 预算地板开关（默认 on）：不足 FLOOR_FULL(32768/300) 自动抬高；off 严格按用户值。
    assistant_budget_floor: bool = bool(_ASSISTANT_DEFAULTS.get("budget_floor", True))
    #: 实测**不可用**于 function calling 的模型名单（逗号分隔，env 可覆盖）。
    #:
    #: 来源：2026-11-14 最小探针（`%TEMP%\tool_probe\probe*.py`，同一套线上渠道
    #: 配置，四段请求：控制组 / tool_choice=auto / required / 工具结果回传）。
    #: `glm-z1-flash`：auto 下 `finish_reason=length`（预算全被 `<think>` 吃掉）；
    #: `max_tokens=4096 + tool_choice=required` 时 `finish_reason=tool_calls` 但
    #: `tool_calls=[]`、content 673 字——**finish_reason 会撒谎**，所以按模型名
    #: 直接排除，而不是运行时探测。
    #: 注意：`glm-4.6` / `glm-4.5-air` 当时因账号欠额（HTTP 429 / code 1113）
    #: 无法验证，**不**列入本名单（它们不是「实测不支持」）。
    assistant_tools_unsupported_models: str = str(
        _ASSISTANT_DEFAULTS.get("tools_unsupported_models", "glm-z1-flash")
    )

    # ---- 蒸馏模型（Distiller，第 3 个角色）----
    # 用途：**文风档案抽取**（把参考语料压成可注入的文风特征），与
    # NarrativeTier 的「蒸馏档位」（full / balanced / fast，采样与计算档位）
    # 不是一回事——档位决定「抽多细」，本角色决定「用哪个模型抽」。
    # 它是长任务（一次要读几十段语料），所以默认 timeout 明显大于 Assistant。
    distiller_provider: str = _DISTILLER_DEFAULTS.get("provider", "zhipu")
    distiller_api_key: str = _DISTILLER_DEFAULTS.get("api_key", "")
    distiller_base_url: str = _DISTILLER_DEFAULTS.get("base_url", "")
    distiller_model: str = _DISTILLER_DEFAULTS.get("model", "glm-4-flash")
    #: 蒸馏每次 LLM 调用的输出上限。
    #:
    #: 2026-10-19 从 2048 提到 8192。2048 会让叙事规则 JSON 被拦腰截断
    #: （智谱 finish_reason=length），截断的 JSON 解析失败 → 该块所有维度归零，
    #: 表现为 rejected 计数暴涨（雪中悍刀行 990 章实测 rejected=740）。
    #: 2026-11-07 KG 抽取链路从 8192 提到 32768：30 章三路对照中 deepseek 有 16 章
    #: 在 8192 触顶、qwen 30/30 章触顶并有一章整章失败；单章 completion 峰值 32,805
    #: token、输出文本最长 13,782 字，reasoning 占 completion 的 71%；32768 给
    #: reasoning 留出余量，调大后两路截断归零。
    #: 兜底值与 config/providers.yaml 的 distiller.max_tokens 保持一致。
    distiller_max_tokens: int = int(_DISTILLER_DEFAULTS.get("max_tokens", 32768))
    distiller_timeout: int = int(_DISTILLER_DEFAULTS.get("timeout", 180))
    #: Distiller 角色的思考档位（空 = 走 `effective_thinking()` 解析）。
    distiller_thinking: str = ""
    #: 预算地板开关（默认 on）：不足 FLOOR_FULL(32768/300) 自动抬高；off 严格按用户值。
    distiller_budget_floor: bool = bool(_DISTILLER_DEFAULTS.get("budget_floor", True))

    # ------------------------------------------------------------------
    # 量级与体系护栏（power_guard，独立区块）
    #
    # 默认开，但只做与体系类型匹配的检查；关闭后退回改造前的单判据
    # （realm_regression）行为。
    # ------------------------------------------------------------------
    power_guard_enabled: bool = True

    def _legacy_channel_field(self, provider: str, field: str) -> str:
        """按渠道名取 legacy 字段值（Assistant / Distiller 的 api_key / base_url 回退用）。

        Args:
            provider: 渠道名（zhipu / openrouter / siliconflow / minimax / dashscope / openai）。
            field: 'api_key' 或 'base_url'。

        Returns:
            对应 legacy 字段的字符串值。渠道名未知（或空）时返回空串——让
            build_assistant_provider() 继续走 LocalFallback 降级，绝不抛错。
        """
        key_field, url_field = _LEGACY_CHANNEL_FIELDS.get((provider or "").strip().lower(), ("", ""))
        name = key_field if field == "api_key" else url_field
        if not name:
            return ""
        value = getattr(self, name, "")
        return str(value or "").strip()

    @field_validator("kg_extract_provider")
    @classmethod
    def _validate_kg_extract_provider(cls, value: str) -> str:
        """未知渠道值 = 配置错误，启动即失败（绝不静默回退主渠道）。

        空串是默认值（跟随主渠道），必须放行；大小写不敏感，但保留原值
        不做小写化——工厂侧统一 `.strip().lower()` 后再比对。
        """
        raw = (value or "").strip()
        if raw.lower() not in KG_EXTRACT_CHANNELS | {""}:
            allowed = "、".join(sorted(KG_EXTRACT_CHANNELS))
            raise ValueError(
                f"kg_extract.provider={value!r} 不是已知渠道（可选：空 = 跟随主渠道，"
                f"或 {allowed}）。不会静默回退到主渠道。"
            )
        return raw

    @property
    def openrouter_model_list(self) -> list[str]:
        """解析候选模型链：去空、去重、保序。"""
        return self._dedup_models(self.openrouter_models)

    @property
    def zhipu_model_list(self) -> list[str]:
        return self._dedup_models(self.zhipu_models)

    @property
    def siliconflow_model_list(self) -> list[str]:
        return self._dedup_models(self.siliconflow_models)

    @property
    def minimax_model_list(self) -> list[str]:
        return self._dedup_models(self.minimax_models)

    @property
    def dashscope_model_list(self) -> list[str]:
        return self._dedup_models(self.dashscope_models)

    @property
    def openai_model_list(self) -> list[str]:
        """通用 OpenAI 兼容渠道的候选链。"""
        return self._dedup_models(self.openai_models)

    @staticmethod
    def _dedup_models(csv: str) -> list[str]:
        seen: list[str] = []
        for m in csv.split(","):
            m = m.strip()
            if m and m not in seen:
                seen.append(m)
        return seen

    # ------------------------------------------------------------------
    # 按 role 返回 6 字段独立 config 的工厂（ADR-2026-10-12 第 2 步 / 第 5 步第 1 步）。
    # ------------------------------------------------------------------
    def effective_thinking(self, role: str, provider: str = "") -> str:
        """解析某个角色**当前生效**的思考档位（空串已不可能：总有默认）。

        优先级：角色显式值 > `minimax_reasoning_effort`（仅 minimax 渠道的老键）
        > `DEFAULT_THINKING`（high）。

        `provider` 留空时按角色推断实际渠道：writer 看 `writer_provider`，
        为空则回落到它委托的 legacy 渠道 `llm_provider`；其余角色看 `<role>_provider`。
        """
        from app.llm.thinking_models import DEFAULT_THINKING

        explicit = str(getattr(self, f"{role}_thinking", "") or "").strip().lower()
        if explicit:
            return explicit
        channel = (provider or "").strip().lower()
        if not channel:
            if role == "writer":
                channel = str(self.writer_provider or self.llm_provider or "").strip().lower()
            else:
                channel = str(getattr(self, f"{role}_provider", "") or "").strip().lower()
        legacy = str(self.minimax_reasoning_effort or "").strip().lower()
        if channel == "minimax" and legacy:
            return legacy
        return DEFAULT_THINKING

    def get_provider_config(self, role: str) -> dict[str, object]:
        """按 role 读取 writer / assistant / distiller 的独立配置块。

        - role="writer"     : 字段名 = writer_* / 6 字段；与 legacy llm_provider 共存但
                              **不冲突**。委托方（registry._build）若发现 writer_provider
                              为空，会自动委托给原 get_llm_provider() 单例，不复制构建逻辑。
        - role="assistant"  : 字段名 = assistant_* / 6 字段；从不读 legacy llm_provider。
        - role="distiller"  : 字段名 = distiller_* / 6 字段；与 assistant 同构（同样走
                              渠道 legacy 回退），但默认 timeout 更大（长任务）。

        assistant / distiller 共用「渠道回退」逻辑：显式留空时按 provider 渠道名
        去 `_LEGACY_CHANNEL_FIELDS` 借对应的 `*_api_key` / `*_base_url`。
        渠道 "openai" 也在这张表里——通用 OpenAI 兼容接口的端点默认来自
        `openai_base_url`，用户可覆盖。

        返回 dict 字段稳定：provider / api_key / base_url / model / max_tokens /
        timeout / thinking / budget_floor，这是 ProviderRegistry 与
        build_*_provider() 的唯一契约。

        抛出 ValueError 当 role 不是已知值（让上层立刻 fail-fast）。
        """
        if role == "writer":
            return {
                "provider": self.writer_provider,
                "api_key": self.writer_api_key,
                "base_url": self.writer_base_url,
                "model": self.writer_model,
                "max_tokens": self.writer_max_tokens,
                "timeout": self.writer_timeout,
                # 思考档位：显式角色值 > MINIMAX_REASONING_EFFORT（仅 minimax）> high
                "thinking": self.effective_thinking("writer", self.writer_provider),
                # 预算地板开关：on = 不足 FLOOR_FULL 自动抬高；off = 严格按用户值
                "budget_floor": bool(self.writer_budget_floor),
                # 标记：writer 空 provider 时由工厂委托给原 Writer 单例
                "_fallback_to_legacy": not bool(self.writer_provider),
            }
        if role in _CHANNEL_FALLBACK_ROLES:
            # assistant / distiller 同构：按 role 前缀取各自的 6 个字段，
            # 不复制两份代码（避免以后加第 4 个角色时又漏一处）。
            provider = str(getattr(self, f"{role}_provider") or "").strip()
            api_key = str(getattr(self, f"{role}_api_key") or "").strip()
            base_url = str(getattr(self, f"{role}_base_url") or "").strip()
            # providers.yaml 承诺的回退：显式留空 → 按渠道名去 legacy 字段取。
            # 修复前 assistant_api_key 恒为空（yaml 里 assistant.api_key 就是 ""），
            # 于是 build_assistant_provider() 恒定降级 LocalFallback——Assistant 从来
            # 没调过真实模型。注释一直是对的，是代码没跟上。
            # 显式配置优先：非空时**不**被 legacy 值覆盖。
            if not api_key:
                api_key = self._legacy_channel_field(provider, "api_key")
            if not base_url:
                base_url = self._legacy_channel_field(provider, "base_url")
            return {
                "provider": provider,
                "api_key": api_key,
                "base_url": base_url,
                "model": str(getattr(self, f"{role}_model") or "").strip(),
                "max_tokens": getattr(self, f"{role}_max_tokens"),
                "timeout": getattr(self, f"{role}_timeout"),
                "thinking": self.effective_thinking(role, provider),
                "budget_floor": bool(getattr(self, f"{role}_budget_floor")),
                "_fallback_to_legacy": False,
            }
        raise ValueError(f"未知 role: {role!r}（应为 'writer' / 'assistant' / 'distiller'）")


settings = Settings()
