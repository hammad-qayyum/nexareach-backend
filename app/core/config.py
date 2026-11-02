from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration. Values come from env vars or the repo-root .env file."""

    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    database_url: str = "postgresql+psycopg://fastnexa:fastnexa@localhost:5433/fastnexa_outreach"

    # 32 random bytes, base64. Encrypts mailbox passwords, tokens, API keys and TOTP secrets.
    encryption_key: str = ""

    app_base_url: str = "http://localhost:3000"
    cookie_secure: bool = False
    session_idle_hours: int = 12
    session_max_days: int = 7
    invite_valid_days: int = 7

    # Login rate limiting
    login_max_failures_per_email: int = 5
    login_max_failures_per_ip: int = 20
    login_window_minutes: int = 15

    sentry_dsn: str = ""

    @property
    def queue_conninfo(self) -> str:
        """Plain libpq URL for Procrastinate (no SQLAlchemy driver suffix)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
