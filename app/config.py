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

    lava_base_url: str = ""
    lava_shop_id: str = ""
    lava_api_key: str = ""
    lava_webhook_secret: str = ""
    payment_provider: str = "lava"
    mock_payments_enabled: bool = Field(default=False, validation_alias="MOCK_PAYMENTS_ENABLED")

    scheduler_interval_seconds: int = 60
    pending_payment_check_seconds: int = 300

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
