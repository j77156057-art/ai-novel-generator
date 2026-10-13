from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # LLM（M4 分段生成环）
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

    # 智谱 GLM（open.bigmodel.cn）：GLM-4-Flash 永久免费、不限 token，中文写作强，国内低延迟
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_model: str = "glm-4-flash"
    zhipu_models: str = "glm-4-flash,glm-z1-flash"

    # 硅基流动 SiliconFlow（api.siliconflow.cn）：国产开源模型最全，新用户送 2000 万 token
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_model: str = "Qwen/Qwen2.5-7B-Instruct"
    siliconflow_models: str = "Qwen/Qwen2.5-7B-Instruct,THUDM/glm-4-9b-chat"

    # MiniMax（国内站 api.minimaxi.com；key 仅国内站有效，国际站 401）
    # 2026-10-12 实测 9 款模型：只有 M3.1-Flash-Preview 把推理放在独立字段 reasoning_content，
    # content 干净（JSON 可直接解析、中文质量好）；M3/M2.7/M2.5 等把 <think> 混进 content，
    # 且传 thinking:{"type":"disabled"} 无效。故默认与切换链均只用 M3.1-Flash-Preview。
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_model: str = "MiniMax-M3.1-Flash-Preview"
    minimax_models: str = "MiniMax-M3.1-Flash-Preview"

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

    @staticmethod
    def _dedup_models(csv: str) -> list[str]:
        seen: list[str] = []
        for m in csv.split(","):
            m = m.strip()
            if m and m not in seen:
                seen.append(m)
        return seen


settings = Settings()
