"""Runtime configuration (environment variables prefixed with ``ONCALL_``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["mock", "mock-injectable", "gemini", "vertex", "anthropic", "openai"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ONCALL_", env_file=".env", extra="ignore")

    # --- Tool backends -------------------------------------------------------------------
    backend: Literal["simulated", "gcp"] = "simulated"
    scenario: str = "bad-deploy"
    # Comma-separated list of GCP project ids the tools may touch. Empty = no restriction
    # (not recommended outside the simulated backend).
    allowed_projects: str = "demo-project-123"
    # Writes (rollback) are disabled on the real backend unless explicitly enabled.
    allow_writes: bool = False
    approval_secret: SecretStr | None = None
    approval_ttl_seconds: int = 300

    # --- LLM -----------------------------------------------------------------------------
    llm_provider: LLMProvider = "mock"
    llm_model: str | None = None
    gcp_project: str | None = None  # for Vertex AI
    gcp_location: str = "global"

    # --- Budgets -------------------------------------------------------------------------
    max_steps: int = Field(default=10, ge=1, le=50)
    max_tool_calls: int = Field(default=16, ge=1, le=100)
    max_total_tokens: int = Field(default=250_000, ge=1_000)
    max_cost_usd: float = Field(default=1.0, ge=0)
    # USD per 1M tokens; set these for the model you use. 0 = cost not tracked.
    price_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0

    # --- MCP connection used by the agent ------------------------------------------------
    mcp_transport: Literal["inprocess", "stdio", "http"] = "inprocess"
    mcp_url: str = "http://127.0.0.1:8765/mcp"

    # --- API -----------------------------------------------------------------------------
    webhook_token: SecretStr | None = None
    pubsub_audience: str | None = None
    cors_origins: str = "http://localhost:5173"

    # --- Telemetry -----------------------------------------------------------------------
    otel_console: bool = False

    @property
    def allowed_project_list(self) -> list[str]:
        return [p.strip() for p in self.allowed_projects.split(",") if p.strip()]

    @property
    def writes_enabled(self) -> bool:
        # The simulated backend has no real side effects, so the HITL flow can be demoed.
        return self.allow_writes or self.backend == "simulated"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
