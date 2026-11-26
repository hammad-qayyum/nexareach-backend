import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration. Values come from env vars or the .env file in this folder."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://fastnexa:fastnexa@localhost:5433/fastnexa_outreach"

    # 32 random bytes, base64. Encrypts mailbox passwords, tokens, API keys and TOTP secrets.
    encryption_key: str = ""

    # "development" on a laptop, "production" on the server. A development copy only sends email
    # through Mailpit (see mailer.development_block), so it can never email real people.
    app_env: str = "production"

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
    # Interactive API docs at /api/docs. Off in production so the API isn't described to the world.
    api_docs: bool = True

    # Allow unencrypted SMTP (security NONE). Only for local testing against Mailpit.
    smtp_allow_plain: bool = False

    @property
    def is_development(self) -> bool:
        return self.app_env.strip().lower() == "development"

    @property
    def is_test_copy(self) -> bool:
        """Development or staging: a copy for trying things out, never the live site."""
        return self.app_env.strip().lower() in ("development", "staging")

    @property
    def queue_conninfo(self) -> str:
        """Plain libpq URL for Procrastinate (no SQLAlchemy driver suffix)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    # NEXAREACH_ENV_FILE picks another settings file, e.g. .env.staging for the staging copy on a laptop.
    return Settings(_env_file=os.environ.get("NEXAREACH_ENV_FILE", ".env"))
