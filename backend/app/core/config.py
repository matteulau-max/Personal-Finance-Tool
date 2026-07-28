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

from pydantic import model_validator
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

    # --- Database ---
    DATABASE_URL: str = "postgresql+psycopg://finance:finance@localhost:5432/finance"

    # --- Authentication (Clerk) ---
    # The issuer identifies your Clerk instance, e.g.
    #   https://enough-mammal-42.clerk.accounts.dev
    # Every token we accept must claim to come from exactly this issuer.
    CLERK_ISSUER: str = ""

    # Where to fetch the public keys used to verify token signatures. Clerk
    # follows the OIDC convention, so this is derived from the issuer unless
    # explicitly overridden.
    CLERK_JWKS_URL: str = ""

    # The `azp` (authorized party) claim names the origin the token was minted
    # for. Validating it stops a token issued for some other site that shares
    # your Clerk instance from being replayed against this API. Clerk
    # explicitly recommends setting this.
    CLERK_AUTHORIZED_PARTIES: list[str] = []

    # Clock skew allowance, in seconds, when checking `exp` and `nbf`. Servers
    # are never perfectly synchronized; without a small tolerance you get rare,
    # unreproducible "token expired" failures.
    JWT_LEEWAY_SECONDS: int = 30

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def auth_configured(self) -> bool:
        return bool(self.CLERK_ISSUER)

    @property
    def jwks_url(self) -> str:
        if self.CLERK_JWKS_URL:
            return self.CLERK_JWKS_URL
        return f"{self.CLERK_ISSUER.rstrip('/')}/.well-known/jwks.json"

    @model_validator(mode="after")
    def _refuse_to_start_insecurely(self) -> "Settings":
        """Fail fast if production is misconfigured.

        An application that starts up happily with authentication disabled is
        one bad deploy away from serving everyone's financial data to the
        public internet. Refusing to boot is dramatically safer than a warning
        in a log nobody reads.

        This is the "fail closed" principle: when a security control cannot be
        applied, stop -- do not carry on without it.
        """
        if self.is_production:
            if not self.CLERK_ISSUER:
                raise ValueError("CLERK_ISSUER must be set in production")
            if not self.CLERK_AUTHORIZED_PARTIES:
                raise ValueError("CLERK_AUTHORIZED_PARTIES must be set in production")
            if self.DEBUG:
                raise ValueError("DEBUG must be false in production")
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached accessor.

    `lru_cache` means the .env file is parsed once per process, not on every
    request. Import `get_settings` anywhere you need configuration.
    """
    return Settings()
