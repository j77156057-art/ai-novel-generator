from __future__ import annotations

import os

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    seed_dir: str = "data/seed"
    chunk_size: int = 1200
    chunk_overlap: int = 200
    upload_max_bytes: int = 20_000_000

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
    # 依据：docs/kg-nemotron-compare-redirect.md N=4 实测 + docs/journey_glm_versions_compare.json。
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
    # MiniMax-Text-01 仅作链尾兜底（content 可能混思维链，实际很少走到）。
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    # 2026-10-20 实测选型（900 字长句改写，同一 prompt）：
    #   M3            关思考 15s / 句长 60.2 / 引号 16   ← 首选
    #   M3            默认   147s / 句长 49.2 / reason 16445
    #   M3.1-Flash    关不掉（400 requires adaptive thinking），61-88s / 句长 59-69
    #   M2.7/M2.5     返回空正文或 200s+，不可用
    # M3 关思考后内容不带 <think> 标签，无需 _strip_thinking 兜底。
    #
    # 2026-10-25：写手默认模型改成 M3.1-Flash。
    # 真跑实测（同一提示 / 同一上下文 / 同一真书续写任务，M3 3 配对、Flash 4 次）：
    #   模型            对白段典型   纯台词段   句长均值   单段用时
    #   MiniMax-M3         17.7 字    58.1%     19.6      20~28s
    #   MiniMax-M3.1-Flash 39.5 字    27.7%     28.9     112~141s
    #   参考书（真书 990 章）61 字    20.8%     48.5        ——
    # 换模型把「对白写得碎」几乎砍半，比四轮提示层实验（在噪声内或更差）有效得多。
    # 代价：M3.1-Flash 是 adaptive thinking 模型，思考关不掉 → 慢 4~5 倍、
    # 预算要 32K token / 300s 超时（provider 里有按模型的预算地板兜着）。
    minimax_model: str = "MiniMax-M3.1-Flash"
    # 限流切换链（主模型 429/403 时按这个顺序换下一个）。
    # 与 `PROVIDER_META["minimax"]["default_models"]`、`_default_model_for("minimax")`
    # **三处必须一致**（下拉里能选的模型得在运行时切换链上，否则界面与生成链路对不上）。
    #
    # 顺序按实测：M3.1-Flash-Preview（推理在独立字段、正文干净）→ M3（关思考后干净、快）
    # → M3.1-Flash（写手默认：对白形态最好，但 adaptive thinking 慢）→ Text-01（链尾兜底）。
    # M2.7/M2.5 已从链里去掉：实测「返回空正文或 200s+，不可用」。
    #
    # 注意：写手默认（`minimax_model`）= M3.1-Flash 与**链首**不同是有意的 ——
    # 前者是 2026-10-25 按「对白形态」实测选出来的，后者是限流兜底的首选。
    minimax_models: str = "MiniMax-M3.1-Flash-Preview,MiniMax-M3,MiniMax-M3.1-Flash,MiniMax-Text-01"
    # ------------------------------------------------------------------
    # MiniMax 的**思考深度**（M3.1 系）。
    #
    # 官方：思考**关不掉**（thinking={"type":"disabled"} / effort="none" → 400
    # "requires adaptive thinking"），但 effort 可调档：low / medium / high /
    # xhigh / max，**省略 = max**（最慢最贵）。
    #
    # 2026-10-26 真跑（api.minimaxi.com，OpenAI 兼容端点，字段名 reasoning_effort）：
    #   2400 字续写（max_tokens=8000）
    #     effort 省略(=max)：66.7s  completion 8000 打满  finish=length  **正文为空**
    #                       （预算全被思考吃掉 ⇒ 正文被拦腰截断）
    #     effort=low        ：36.0s  completion 2605      finish=stop    正文完整 4079 字
    #   300 字小任务
    #     M3.1-Flash        ：默认 20.5s/3081 token → low 5.2s/262 token
    #     M3.1-Flash-Preview：默认 20.9s/2160 token → low 13.8s/284 token
    #   **M3（老）不吃 effort**：传 low 反而更慢（22.8s vs 19.9s）—— 它是"老式 adaptive"，
    #   思考混在 content 里，只能靠 thinking=disabled + _strip_thinking 兜底。
    #
    # 2026-10-26 晚 档位实测（写 2400 字正文，两轮共 15 次调用，见 ENGINEERING.md）：
    #   档位    成功/总   用时      reasoning   裸台词段(项目尺)
    #   low      7/7     55~99s    92~398      70~92%（场景差异大）
    #   medium   6/7     40~57s    250~3203    4~98%（同上）
    #   high     6/6     53~127s   429~2835    66~89%
    #   xhigh    2/3     150~169s  5763~8191   **1 次正文 0 字**（8192 打满）
    #   max      0/2     **>300s 未返回**（32768 预算也是）
    #
    # 为什么**写手**默认仍是 low（2026-10-26 复核后**维持原判**）：
    #   一度想改成 high（第一轮 n=2 看着形态最好），但第二轮 3 场景复测**推翻了它** ——
    #   三个可用档位在形态上分不出高下（组内方差比组间差异大得多），
    #   而 low 是**最可靠**（7/7 成功，其余档位都出现过超时/空正文）、
    #   **思考 token 最省**（92~398 vs 其余上千）、用时也不吃亏太多。
    #   ⇒ 没有证据就不改默认。**medium 快约 25%**（48s vs 67s，两轮一致），
    #     要吞吐的话可以在界面上选 medium —— 但思考 token 多 3 倍。
    # 为什么**蒸馏/诊断**不传：那两个角色要"判断对错"，思考有实际价值 ⇒ 留默认 max。
    minimax_reasoning_effort: str = "low"
    # 全局默认的文风改写开关（生成档位会写它）。
    # 项目级 projects.style_rewrite 非空时以项目为准——项目是显式选择，
    # 全局只是「新项目/未设置时的默认值」。三者优先级：
    #   请求体 style_rewrite > project.style_rewrite（若已显式设置）> 本全局值
    style_rewrite: bool = True
    # 改写的**触发方式**：开着改写时，是每段都改，还是只改「偏离严重」的段？
    # True（默认）= 闸门：拿参考书自己的分布当尺（句长 < p10 / 句号密度 > p90），
    #   偏离不到就不动 —— 改写要**多一次模型调用**，而它只修得动「碎」；
    # False = 每段都改（旧行为）。
    # 项目级 projects.style_rewrite = 1 视为显式选择「每段都改」。
    style_rewrite_gate: bool = True
    # 形态校正总开关：把生成正文的「对白组织方式」掰到**参考书自己的形态**
    # （见 app/style/form.py 与 form_merge.py）。默认开 —— 它有闸门（只有偏离
    # 参考书自己分布带的段落才动），且不做就永远带着模型的对白先验。
    style_form_correction: bool = True
    # 形态校正的**做法**：
    #   "merge"（默认）= 机械校正：把「纯台词段」并进**旁边**那段纯叙述 ——
    #     按**最大匹配**选配对（含向后并），只改段落边界，
    #     **一个字都不新增**；唯一的例外是并接处把「。」改成「：」
    #     （对齐参考书的混合段形态：参考书 2059 个混合段里只有 3 处用「。」+引号）。
    #     零模型调用、100% 可复现。实测 9 段真实产物：纯台词段占比 71.5% → 42.2%。
    #   "llm" = 让模型改写形态（app/style/form.py 的 correct_form）。
    #     **机制可行但不可靠**：同一段输入重复 4 次 0/4 采纳，正式测量 0/20；
    #     三次成功都是偶然（同输入同提示：一次 76%→24%，一次 65%→21%）。
    #     所以默认不用它 —— 要试得显式开。
    style_form_correction_mode: str = "merge"
    # 引导语补写：**机械合并到顶之后**再用模型补一句动作/神态叙述。
    #
    # 机械合并只改段落边界：实测 9 段真实产物，纯台词段占比 71.5% → 42.2%
    # （参考书 22.7%）—— **到不了**。剩下的差距只能靠**新增内容**补。
    # 这一步把引导语**并进台词段**（冒号形态 `叙述：“台词”`），
    # 实测 71.5% → 42.2% → **21.3%**，采纳率 90%。
    #
    # ⚠️ 它**会新增文字**（机械合并不新增）⇒ 判据更严（六道），
    #    且只有「机械合并之后仍在参考书分布带外」才会付这次模型调用的钱。
    # ⚠️ 默认关：它给每段生成加约 10 次模型调用（实测 9 段共 109 秒）。
    style_lead_in: bool = False
    # 改写用哪个模型（**同一渠道，只换模型名**）。留空 = 用写手的 provider。
    # 为什么默认不是写手模型：改写要的只是「把碎句合成长句」，不需要写作模型的
    # 推理能力。实测（真书续写的真实正文，MiniMax-M3.1-Flash 做写手）：
    #   M3 改写        6~9s · 3/3 成功 · 句长 21.8→36.8 · 纯台词段 42%→35%
    #   M3.1-Flash 改写 159s · 1/3 成功（另两次 603s 超时）· 句长 21.8→54.4
    # Flash 改写效果更好但慢且不稳 —— 把它留给「写」这步更合适。
    # 渠道不认这个模型 / 没有 key → 回落到写手的 provider（不阻断生成）。
    style_rewrite_model: str = "MiniMax-M3"

    # 候选池：同一提示采样 N 份，按「参考书自己的分布」选最好的那份落库
    # （Best-of-N / 拒绝采样，见 app/generation/candidate.py）。
    # 1 = 关闭（默认）。理由：多采样 = 多花模型时间与额度（一次真跑 4~9 分钟/段），
    # 而实测收益集中在「默认写手之外的模型」上 —— Flash 经机械校正后 4/4 候选
    # 已进带，候选池只是让离分布中心更近；M3 上机械校正后仍有 2/3 不在带内，
    # 那才是候选池的兜底场景。所以默认关，要用得显式开。
    style_candidate_pool: int = 1

    # beat 插入：在连续对白段的开头补一句动作/神态叙述（app/style/beat.py）。
    # **默认关闭，而且默认关着是对的**（2026-10-25 真跑量完才看清）：
    # 1. 15 本真书的「光台词段」占比是 **中位 72%**（min 7% / max 88%），
    #    《北荒刀歌》（7%）是**特例**；我们生成的是 61% —— **已在真书区间下沿**，
    #    不是「差了一倍」。
    # 2. 真跑曲线：插 6/12/24/48 处 ⇒ 动作密度升到真书的 5 倍（= 网球法），
    #    而 A 类只降到 35%；采纳率还随量下降（模型在敷衍）。
    # ⇒ **对多数书这条路是错的**。留在这里是因为：① 目标比例取自**参考书自己**
    #    （`_reference_plain_dialogue_ratio`），对北荒刀歌/《道途魔典》那种动作密集的书
    #    仍然有意义；② 它同时是「局部插入」这条路的**实现与判据**范例
    #    （整段形态改写已实测 0/20 不可靠，局部插入采纳率 5/6）。
    style_beat_insert: bool = False

    # 照搬门禁：生成出来的正文 vs 参考语料原文（app/generation/verbatim.py）。
    # 为什么要有：已有的 copy_check 只管「规划节点 vs 参考规则」，水印检测只管
    # 语料清洗 —— **正文复读原文**这一段是空的，而项目自己踩过
    # （v2.272：5 章全在重写同一段，第 4/5 段前 3 句几乎逐字相同）。
    #
    # 阈值 25 是实测标定的，不是拍的（2026-10-26，498 万字真实语料）：
    #   真实生产产物 vs 语料 → 最长公共串 9 字
    #   本轮 25 篇生成      → 11 字
    #   **无关真书 vs 语料** → 15 字（中文共用地毯的实测上限）
    # 25 远高于上限 ⇒ 判得死，够得上 error 级；12~24 字只告警不拦。
    style_verbatim_gate: bool = True
    style_verbatim_threshold: int = 25
    # 回灌自改最多几次（把命中的片段指给模型，让它自己换措辞）。
    # 改不掉就不落库 —— 宁可这一段没有，不能落一段照搬的。
    style_verbatim_max_rewrites: int = 2

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
    writer_max_tokens: int = int(_WRITER_DEFAULTS.get("max_tokens", 2048))
    # 思考档位（2026-10-26）。取值由渠道+模型决定，见
    # `routers/settings.py` 的 thinking_control_for()：
    #   MiniMax M3.1 → low/medium/high/xhigh/max（关不掉，只能调档）
    #   DeepSeek / 通用渠道 → off（关掉思考）
    #   留空 → 不发任何字段（保持官方默认，行为与加这个开关之前一致）
    writer_thinking: str = ""
    assistant_thinking: str = ""
    distiller_thinking: str = ""

    writer_timeout: int = int(_WRITER_DEFAULTS.get("timeout", 120))

    assistant_provider: str = _ASSISTANT_DEFAULTS.get("provider", "zhipu")
    assistant_api_key: str = _ASSISTANT_DEFAULTS.get("api_key", "")
    assistant_base_url: str = _ASSISTANT_DEFAULTS.get("base_url", "")
    assistant_model: str = _ASSISTANT_DEFAULTS.get("model", "glm-4-flash")
    assistant_max_tokens: int = int(_ASSISTANT_DEFAULTS.get("max_tokens", 1024))
    assistant_timeout: int = int(_ASSISTANT_DEFAULTS.get("timeout", 60))

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
    #: 表现为 rejected 计数暴涨（《北荒刀歌》 990 章实测 rejected=740）。
    #: 蒸馏要一次输出 11 个维度的规则数组，8192 才够。
    distiller_max_tokens: int = int(_DISTILLER_DEFAULTS.get("max_tokens", 8192))
    distiller_timeout: int = int(_DISTILLER_DEFAULTS.get("timeout", 180))
    # 蒸馏并发上限覆盖。0 = 按渠道自动（provider.max_concurrent_hint）。
    # 实测依据见 provider.py 各渠道类的注释：
    #   zhipu   真实上限 2（免费档约 28K TPM，官方「并发 30」是并发数口径，长 prompt 到不了）
    #   minimax 实测 15（0.86 req/s，114K TPM）
    # 只有确认自己渠道额度更高时才需要设这个值。
    distill_max_concurrent: int = 0

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

        返回 dict 字段稳定：provider / api_key / base_url / model / max_tokens / timeout，
        这是 ProviderRegistry 与 build_*_provider() 的唯一契约。

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
                "thinking": self.writer_thinking,
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
                "thinking": str(getattr(self, f"{role}_thinking") or "").strip(),
                "_fallback_to_legacy": False,
            }
        raise ValueError(f"未知 role: {role!r}（应为 'writer' / 'assistant' / 'distiller'）")


settings = Settings()
