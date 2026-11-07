from __future__ import annotations

import pytest

from oncall_agent.config import Settings


@pytest.fixture
def settings() -> Settings:
    # Explicit values so a developer's local .env cannot change test behaviour.
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        backend="simulated",
        llm_provider="mock",
        mcp_transport="inprocess",
        allowed_projects="demo-project-123",
    )
