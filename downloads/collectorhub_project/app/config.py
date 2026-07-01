from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "CollectorHub"
    app_env: str = "local"
    admin_password: str = "change_me_now"
    database_url: str = "sqlite+aiosqlite:///./collectorhub.db"

    ai_enabled: bool = False
    openai_api_key: str | None = None

    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    tg_session_name: str = "collectorhub"
    tg_source_chats: str = ""
    tg_target_chat: str = ""

    forward_delay_seconds: int = 15

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def source_chats_list(self) -> list[str]:
        return [x.strip() for x in self.tg_source_chats.split(",") if x.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
