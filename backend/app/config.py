from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── 主 LLM：DeepSeek API ──────────────────────────────────────────
    # deepseek-chat   : 通用对话，适合 Router / Synthesizer
    # deepseek-reasoner: 深度推理（R1），适合复杂规划任务
    deepseek_api_key: str = ""
    deepseek_api_url: str = "https://api.deepseek.com/v1"

    # ── 备用 LLM：OpenAI 兼容接口 ────────────────────────────────────
    # 支持 OpenAI 官方 / SiliconFlow / 其他兼容服务
    openai_api_key: str = ""
    openai_api_url: str = "https://api.openai.com/v1"

    # ── LLM 模型名称 ─────────────────────────────────────────────────
    llm_model_router: str = "deepseek-chat"
    llm_model_synthesizer: str = "deepseek-chat"

    # ── Embedding API（独立配置，可与主 LLM 不同） ────────────────────
    # 留空时自动复用 openai_api_key / openai_api_url
    embedding_api_key: str = ""
    embedding_api_url: str = ""
    embedding_model: str = "text-embedding-3-small"

    # ── Advanced RAG 配置 ─────────────────────────────────────────────
    # HyDE：用 LLM 生成假设文档再做 embedding，提升稀疏查询召回率
    hyde_enabled: bool = True
    hyde_model: str = "deepseek-chat"   # 生成假设文档所用模型

    # Reranker：bge-reranker-v2-m3 本地推理（需要 FlagEmbedding + GPU）
    # Docker 轻量部署默认关闭；本地有 FlagEmbedding 时可设 RERANKER_ENABLED=true
    reranker_enabled: bool = False
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_device: str = "cpu"        # "cpu"（默认安全）| "cuda"（有 GPU 时显式配置）

    # ── 高德地图 ──────────────────────────────────────────────────────
    amap_api_key: str = ""
    amap_js_key: str = ""
    amap_mock: bool = True  # 默认 Mock，保护配额

    # ── 和风天气 ──────────────────────────────────────────────────────
    qweather_api_key: str = ""
    qweather_api_host: str = "devapi.qweather.com"
    qweather_auth_type: str = "jwt"          # "jwt"（推荐）或 "apikey"
    qweather_private_key: str = ""           # PKCS8 Ed25519 私钥 base64 正文
    qweather_key_id: str = ""               # 控制台凭据 ID（kid）
    qweather_project_id: str = ""           # 控制台项目 ID（sub）

    # ── 数据库 ────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/travel_agent"
    redis_url: str = "redis://localhost:6379"

    # ── Sprint 3：微调 Router 分类器 ──────────────────────────────────
    # 启用后：用户查询先经本地 Qwen2.5 LoRA 分类，再决定是否调 DeepSeek
    # 需先运行 scripts/generate_training_data.py + scripts/train_router.py
    ft_router_enabled: bool = False
    ft_router_model_path: str = "models/router_lora"

    # ── 阿里云短信服务 ────────────────────────────────────────────────
    alibaba_cloud_access_key_id: str = ""
    alibaba_cloud_access_key_secret: str = ""
    alibaba_cloud_sms_sign_name: str = "BreezeTravel"
    alibaba_cloud_sms_template_code: str = ""

    # ── JWT 鉴权 ──────────────────────────────────────────────────────
    jwt_secret_key: str = "change-me-in-production-please"

    # ── Demo 模式 ─────────────────────────────────────────────────────
    demo_mode: bool = False

    # ── LangSmith 可观测性（Sprint 5 新增）────────────────────────────
    # 配置后所有 LangChain / LangGraph 调用自动上报追踪数据
    # 控制台：https://smith.langchain.com/
    langsmith_api_key: str = ""
    langsmith_project: str = "BreezeTravel"

    # ── CORS ──────────────────────────────────────────────────────────
    cors_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

    # ── 派生属性（运行时计算，不从环境变量读取） ───────────────────────
    @property
    def effective_llm_api_key(self) -> str:
        """主 LLM Key：优先 DeepSeek，回退 OpenAI"""
        return self.deepseek_api_key or self.openai_api_key

    @property
    def effective_llm_api_url(self) -> str:
        """主 LLM URL：有 DeepSeek Key 时用 DeepSeek，否则用 OpenAI"""
        if self.deepseek_api_key:
            return self.deepseek_api_url
        return self.openai_api_url

    @property
    def effective_embedding_api_key(self) -> str:
        """Embedding Key：优先独立配置，回退 openai_api_key"""
        return self.embedding_api_key or self.openai_api_key

    @property
    def effective_embedding_api_url(self) -> str:
        """Embedding URL：优先独立配置，回退 openai_api_url"""
        return self.embedding_api_url or self.openai_api_url

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
