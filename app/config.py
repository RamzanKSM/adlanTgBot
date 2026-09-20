from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True)

    app_env: str = "local"
    app_base_url: str = "http://localhost:8000"
    database_path: Path = Path("./data/bot.sqlite3")

    bot_token: str = Field(default="")
    telegram_group_id: int = 0
    telegram_webhook_secret: str = "local-secret"

    admin_ids_raw: str = Field(default="", validation_alias="ADMIN_IDS")
    admin_usernames_raw: str = Field(default="", validation_alias="ADMIN_USERNAMES")

    lava_base_url: str = "https://api.lava.ru"
    lava_shop_id: str = ""
    lava_secret_key: str = ""
    lava_additional_key: str = ""
    payment_provider: str = "lava"
    mock_payments_enabled: bool = Field(default=False, validation_alias="MOCK_PAYMENTS_ENABLED")

    scheduler_interval_seconds: int = 60
    pending_payment_check_seconds: int = 300
    ai_enabled: bool = False
    # This is deliberately opt-in because LLM input can contain complete
    # Telegram messages and retrieved channel knowledge.
    ai_debug_logging: bool = False
    ai_turn_debounce_seconds: int = 30
    ai_user_max_turns_per_window: int = 100
    ai_user_limit_window_seconds: int = 18_000
    ai_deferred_batch_max_chars: int = 2_000
    ai_recent_context_limit: int = 40
    ai_router_context_max_chars: int = 8_000
    ai_router_context_message_max_chars: int = 2_000
    ai_answer_context_limit: int = 6
    ai_answer_context_max_chars: int = 3_000
    ai_retrieval_top_k: int = 10
    ai_retrieval_context_chars: int = 12_000
    ai_scheduler_interval_seconds: int = 5
    ai_worker_executable: str = "codex"
    ai_worker_timeout_seconds: int = 90
    ai_worker_model: str = "gpt-5.6-luna"
    ai_worker_reasoning_effort: str = "medium"
    ai_session_timeout_seconds: int = 600
    ai_retry_max_attempts: int = 5
    ai_retry_base_seconds: int = 30
    ai_processing_lease_seconds: int = 180
    ai_embedding_cache_dir: Path = Path("./data/models/fastembed")

    @property
    def admin_ids(self) -> list[int]:
        return [int(item.strip()) for item in self.admin_ids_raw.split(",") if item.strip()]

    @property
    def admin_usernames(self) -> list[str]:
        return [item.strip().lstrip("@").lower() for item in self.admin_usernames_raw.split(",") if item.strip()]

    @field_validator("payment_provider")
    @classmethod
    def validate_payment_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"lava", "mock"}:
            raise ValueError("PAYMENT_PROVIDER must be 'lava' or 'mock'")
        return normalized

    @field_validator("ai_worker_model")
    @classmethod
    def validate_ai_worker_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("AI_WORKER_MODEL must not be empty")
        return normalized

    @field_validator("ai_worker_reasoning_effort")
    @classmethod
    def validate_ai_worker_reasoning_effort(cls, value: str) -> str:
        normalized = value.strip().lower()
        allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        if normalized not in allowed:
            raise ValueError("AI_WORKER_REASONING_EFFORT must be one of: none, minimal, low, medium, high, xhigh, max")
        return normalized

    @field_validator(
        "ai_turn_debounce_seconds",
        "ai_user_max_turns_per_window",
        "ai_user_limit_window_seconds",
        "ai_deferred_batch_max_chars",
        "ai_recent_context_limit",
        "ai_router_context_max_chars",
        "ai_router_context_message_max_chars",
        "ai_answer_context_limit",
        "ai_answer_context_max_chars",
    )
    @classmethod
    def validate_positive_ai_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("AI turn and quota settings must be positive")
        return value

    @property
    def is_mock_payments_enabled(self) -> bool:
        return self.payment_provider == "mock" or self.mock_payments_enabled

    def is_admin(self, telegram_user_id: int | None, username: str | None = None) -> bool:
        if telegram_user_id is not None and telegram_user_id in self.admin_ids:
            return True
        if username:
            return username.strip().lstrip("@").lower() in self.admin_usernames
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
