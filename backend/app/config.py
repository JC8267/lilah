from pydantic_settings import BaseSettings
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    llm_provider: str = "anthropic"  # anthropic | gemini | openai | openrouter | ollama | openai_compatible
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    model_id: str = "claude-sonnet-4-20250514"
    llm_max_tokens: int = 4096
    llm_max_iterations: int = 10
    llm_timeout_seconds: float = 60.0
    llm_retry_attempts: int = 2
    llm_retry_backoff_seconds: float = 1.0
    llm_reasoning_effort: str = "minimal"  # "", minimal, low, medium, high
    llm_reasoning_escalate_on_retry: bool = True
    llm_fallback_models: str = ""  # comma-separated model IDs
    question_match_model_assist: bool = True
    question_match_model_top_k: int = 8
    question_match_model_min_confidence: float = 0.55
    question_match_model_timeout_seconds: float = 12.0
    allow_llm_override: bool = False
    chat_history_max_messages: int = 10
    chat_request_max_bytes: int = 65536
    chat_message_max_chars: int = 4000
    chat_filters_max_items: int = 12
    chat_filter_key_max_chars: int = 80
    chat_filter_value_max_chars: int = 200
    chat_rate_limit_per_minute: int = 30
    chat_max_active_streams_per_client: int = 2
    chat_max_active_streams_global: int = 40
    chat_disconnect_poll_ms: int = 300
    data_dir: Path = BACKEND_ROOT.parent / "normalized"
    sqlite_path: Path = BACKEND_ROOT / "lilah.db"
    max_query_rows: int = 200
    cors_allowed_origins: str = "http://localhost:5173,http://localhost:3000"
    cors_allow_origin_regex: str = r"https://.*\.vercel\.app"

    model_config = {
        "env_file": str(BACKEND_ROOT / ".env"),
        "env_file_encoding": "utf-8",
    }


settings = Settings()
