from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://monitor:monitor@localhost:5432/monitor"
    # Fernet key used to encrypt SSH credentials at rest.
    fernet_key: str = ""

    # Collector
    collect_tick_seconds: int = 15
    max_concurrency: int = 50
    ssh_connect_timeout: int = 15
    ssh_command_timeout: int = 30
    unreachable_after_failures: int = 3
    status_window_hours: int = 24
    # Sharding: each worker handles servers where id % worker_count == worker_index.
    worker_index: int = 0
    worker_count: int = 1

    rules_dir: Path = Path(__file__).resolve().parents[2] / "rules"

    # Telegram alerts: off unless both the token and the chat id are set.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_min_severity: Literal["info", "warning", "critical"] = "critical"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


settings = Settings()
