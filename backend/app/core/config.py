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
    # Root log level. Application loggers are wired up at startup -- see
    # app/core/logging.py for why that is not automatic under uvicorn.
    LOG_LEVEL: str = "INFO"

    # --- CORS: which frontend origins may call this API ---
    # In production this becomes your real Vercel domain, nothing else.
    FRONTEND_ORIGIN: str = "http://localhost:3000"

    # Host names this API answers to, checked against the Host header in
    # production. Empty means no check -- fine locally, refused at startup in
    # production by the validator below.
    ALLOWED_HOSTS: list[str] = []

    # --- Database ---
    DATABASE_URL: str = "postgresql+psycopg://finance:finance@localhost:5432/finance"

    # The role every request switches into, and the one Row-Level Security
    # policies actually apply to. It must own no tables and hold no BYPASSRLS
    # -- PostgreSQL exempts both from policies. Created by the migration that
    # adds RLS; see app/db/rls.py.
    DB_APP_ROLE: str = "finance_app"

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

    # --- Encryption at rest ---
    # Fernet keys, newest first. The first is used for new writes; the rest
    # exist so data written under an older key still decrypts during rotation.
    # Generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    ENCRYPTION_KEYS: list[str] = []

    # --- Plaid ---
    PLAID_CLIENT_ID: str = ""
    PLAID_SECRET: str = ""
    # sandbox = fake banks and test credentials, no real money, free.
    # production = real institutions. There is no "development" tier any more.
    PLAID_ENV: Literal["sandbox", "production"] = "sandbox"
    PLAID_PRODUCTS: list[str] = ["transactions"]
    PLAID_COUNTRY_CODES: list[str] = ["US"]
    # Shown to the user inside the Plaid Link dialog.
    PLAID_CLIENT_NAME: str = "Personal Finance Dashboard"
    # Public HTTPS URL Plaid posts webhooks to. Empty disables webhooks, which
    # is the normal state in local development.
    PLAID_WEBHOOK_URL: str = ""
    # Reject webhooks whose JWT was issued more than this long ago. Plaid
    # recommends 5 minutes; it is what stops a captured webhook being replayed.
    PLAID_WEBHOOK_MAX_AGE_SECONDS: int = 300

    # --- Background work ---
    # Whether the API process also drains the webhook queue. True is the
    # right default for a single-process deployment; set it false and run
    # `scripts/run_worker.py` when you want the sync load isolated from the
    # request path. See app/services/job_worker.py.
    WEBHOOK_WORKER_ENABLED: bool = True
    WEBHOOK_WORKER_POLL_SECONDS: float = 5.0

    # --- AI insights (Anthropic) ---
    # Unset means the feature is OFF: /api/insights returns 503 rather than
    # degrading into an answer nobody computed. An AI feature that half-works
    # is worse than one that is plainly unavailable.
    ANTHROPIC_API_KEY: str = ""
    AI_MODEL: str = "claude-opus-5"
    # Generous: a question needing several tool calls does several round trips
    # inside one request, and a timeout mid-conversation wastes every call
    # already paid for.
    AI_TIMEOUT_SECONDS: float = 120.0
    AI_MAX_RETRIES: int = 2

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def auth_configured(self) -> bool:
        return bool(self.CLERK_ISSUER)

    @property
    def plaid_configured(self) -> bool:
        return bool(self.PLAID_CLIENT_ID and self.PLAID_SECRET)

    @property
    def ai_configured(self) -> bool:
        return bool(self.ANTHROPIC_API_KEY)

    @property
    def plaid_host(self) -> str:
        return {
            "sandbox": "https://sandbox.plaid.com",
            "production": "https://production.plaid.com",
        }[self.PLAID_ENV]

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
            if not self.ENCRYPTION_KEYS:
                raise ValueError("ENCRYPTION_KEYS must be set in production")
            if self.PLAID_ENV != "production":
                raise ValueError("PLAID_ENV must be 'production' in production")
            if not self.PLAID_WEBHOOK_URL.startswith("https://"):
                raise ValueError("PLAID_WEBHOOK_URL must be an HTTPS URL in production")
            if not self.ALLOWED_HOSTS:
                raise ValueError("ALLOWED_HOSTS must be set in production")
            if not self.FRONTEND_ORIGIN.startswith("https://"):
                raise ValueError("FRONTEND_ORIGIN must be an HTTPS URL in production")
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached accessor.

    `lru_cache` means the .env file is parsed once per process, not on every
    request. Import `get_settings` anywhere you need configuration.
    """
    return Settings()
