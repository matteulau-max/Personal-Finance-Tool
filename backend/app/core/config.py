"""Application configuration.

Every value the app needs that changes between machines (your laptop vs. the
production server) lives here, and is read from *environment variables* --
never hard-coded into the source. That is the single most important security
habit in this project: secrets stay out of Git.

`BaseSettings` reads, in order of priority:
  1. real environment variables
  2. the `.env` file sitting next to `backend/`
and validates the types. If a required variable is missing, the app refuses
to start with a clear error instead of failing mysteriously later.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Ignore unrelated variables that happen to exist in the environment.
        extra="ignore",
    )

    # --- General ---
    APP_NAME: str = "Personal Finance API"
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    DEBUG: bool = True

    # --- CORS: which frontend origins may call this API ---
    # In production this becomes your real Vercel domain, nothing else.
    FRONTEND_ORIGIN: str = "http://localhost:3000"

    # --- Database (wired up in Milestone 3) ---
    DATABASE_URL: str = "postgresql+psycopg://finance:finance@localhost:5432/finance"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached accessor.

    `lru_cache` means the .env file is parsed once per process, not on every
    request. Import `get_settings` anywhere you need configuration.
    """
    return Settings()
