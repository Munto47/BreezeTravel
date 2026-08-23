from datetime import datetime
from typing import Literal

from pydantic import ConfigDict
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=("settings_",),
    )

    runtime_profile: Literal["demo", "test", "local_fixture", "local_real", "public"] = "local_real"
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
    embedding_model: str = "BAAI/bge-m3"          # SiliconFlow 可用的中文 embedding 模型（1024 维）

    # ── Advanced RAG 配置 ─────────────────────────────────────────────
    # HyDE：用 LLM 生成假设文档再做 embedding，提升稀疏查询召回率
    hyde_enabled: bool = True
    hyde_model: str = "deepseek-chat"   # 生成假设文档所用模型
    multi_query_enabled: bool = False
    deterministic_routing_enabled: bool = True
    reranker_min_candidates: int = 8
    place_meta_lookup_enabled: bool = True

    # Reranker：bge-reranker-v2-m3 本地推理（需要 FlagEmbedding + GPU）
    # Docker 轻量部署默认关闭；本地有 FlagEmbedding 时可设 RERANKER_ENABLED=true
    reranker_enabled: bool = False
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_device: str = "cpu"        # "cpu"（默认安全）| "cuda"（有 GPU 时显式配置）

    # ── 高德地图 ──────────────────────────────────────────────────────
    amap_api_key: str = ""
    amap_js_key: str = ""
    amap_mock: bool = True  # 默认 Mock，保护配额
    # SuggestionSet 的 Provider 模式是一个独立的、显式的选择。
    # auto 保留旧行为；frozen_snapshot 只会重放指定快照，不会回退到
    # Amap live 或 fixture。快照模式的三个标识和重放时钟都必须显式配置。
    suggestion_provider_mode: Literal["auto", "live", "fixture", "frozen_snapshot"] = "auto"
    suggestion_snapshot_path: str = ""
    suggestion_snapshot_sha256: str = ""
    suggestion_snapshot_id: str = ""
    suggestion_snapshot_replay_at: datetime | None = None
    # P8 临行复检默认只重放已持久化的证据。只有在明确开启、且运行在
    # 非 fixture 的真实 Provider 配置下，才允许逐地点刷新 Amap POI 事实。
    # 这不是发布开关，也不能把未发生的调用写成 live receipt。
    pre_trip_live_provider_recheck_enabled: bool = False

    # ── 和风天气 ──────────────────────────────────────────────────────
    qweather_api_key: str = ""
    qweather_api_host: str = "devapi.qweather.com"
    qweather_auth_type: str = "jwt"          # "jwt"（推荐）或 "apikey"
    qweather_private_key: str = ""           # PKCS8 Ed25519 私钥 base64 正文
    qweather_key_id: str = ""               # 控制台凭据 ID（kid）
    qweather_project_id: str = ""           # 控制台项目 ID（sub）

    # ── Brave 风险来源 ────────────────────────────────────────────────
    brave_api_key: str = ""

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
    # Public demo guard.  It is disabled locally by default; use an edge/WAF or
    # Redis limiter when horizontally scaling beyond this single-instance guard.
    public_demo_mode: bool = False
    public_demo_chat_requests_per_minute: int = 12
    trust_proxy_headers: bool = False
    tool_timeout_seconds: float = 12.0
    amap_tool_timeout_seconds: float = 20.0
    tool_max_concurrency: int = 3
    chat_max_tool_calls: int = 6
    e2e_cleanup_secret: str = ""
    e2e_restart_gate_mode: bool = False
    router_input_cost_per_million: float = 0.0
    router_output_cost_per_million: float = 0.0
    model_pricing_version: str = "unconfigured"
    chat_deadline_seconds: float = 30.0
    llm_max_concurrency: int = 4
    amap_max_concurrency: int = 4
    weather_max_concurrency: int = 4
    embedding_max_concurrency: int = 4
    provider_failure_threshold: int = 5
    provider_circuit_open_seconds: float = 30.0
    auto_migrate: bool = False
    require_schema_check: bool = True
    checkpoint_bootstrap_on_start: bool = True
    required_migration: str = "024_advice_bundles.sql"
    memory_enabled_default: bool = True
    memory_min_confidence: float = 0.65
    memory_ttl_days: int = 180
    memory_max_items: int = 5
    memory_max_text_length: int = 500
    yjs_max_payload_bytes: int = 262144
    yjs_max_connections_per_ip: int = 20
    otel_enabled: bool = False
    otel_service_name: str = "breezetravel-backend"

    # ── 开发/演示登录旁路 ─────────────────────────────────────────────
    # 启用后，/api/auth/send-code 不真发短信，验证码固定为 dev_login_code
    # 用于本地开发、CI、演示环境，避免吃真实 SMS 配额
    dev_login_bypass: bool = False
    dev_login_code: str = "888888"

    # 同号码每日发送上限（命中后返回 429，避免触发运营商日级流控）
    sms_daily_limit_per_phone: int = 5

    # 测试账号一键登录（仅在 dev_login_bypass=true 时启用）
    test_account_phone: str = "10000000000"
    test_account_nickname: str = "测试旅行者"

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

@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    """Explicit test hook; avoids import-order dependent Settings state."""
    get_settings.cache_clear()


settings = get_settings()
