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
    llm_fallback_models: str = ""  # comma-separated model IDs
    chat_history_max_messages: int = 10
    data_dir: Path = BACKEND_ROOT.parent / "normalized"
    sqlite_path: Path = BACKEND_ROOT / "lilah.db"
    max_query_rows: int = 200

    model_config = {
        "env_file": str(BACKEND_ROOT / ".env"),
        "env_file_encoding": "utf-8",
    }


settings = Settings()
