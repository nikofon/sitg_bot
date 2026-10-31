from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    bot_token: SecretStr
    database_url: str = "postgresql+asyncpg://sitg:sitg@localhost:5432/sitg"
    application_server_host: str = "127.0.0.1"
    application_server_port: int = Field(default=8765, ge=1, le=65535)
    application_client_token: SecretStr | None = Field(
        default=None,
        min_length=32,
        validation_alias="SITG_APPLICATION_CLIENT_TOKEN",
    )
    token_delivery_key: SecretStr | None = Field(
        default=None,
        min_length=32,
        validation_alias="SITG_TOKEN_DELIVERY_KEY",
    )
    application_client_version: str = "0.1.0"
    telegram_environment: Literal["production", "test"] = "production"
    application_security_key: SecretStr | None = None
    mini_app_base_url: str | None = None
    mini_app_allowed_origins: tuple[str, ...] = ()
    bot_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    bot_log_path: Path = Path("logs/sitg-bot.log")
    bot_log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    bot_log_backup_count: int = Field(default=5, ge=1, le=100)
