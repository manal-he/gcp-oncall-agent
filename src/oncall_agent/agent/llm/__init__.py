"""LLM providers behind one interface (see ``base.LLM``)."""

from __future__ import annotations

from oncall_agent.agent.llm.base import LLM, LLMResponse, Message, ToolCall, ToolSpec, Usage
from oncall_agent.config import Settings

__all__ = ["LLM", "LLMResponse", "Message", "ToolCall", "ToolSpec", "Usage", "make_llm"]


def make_llm(settings: Settings, provider: str | None = None) -> LLM:
    provider = provider or settings.llm_provider
    model = settings.llm_model
    if provider in ("mock", "mock-injectable"):
        from oncall_agent.agent.llm.mock import MockLLM

        return MockLLM(injectable=provider == "mock-injectable")
    if provider == "anthropic":
        from oncall_agent.agent.llm.providers import AnthropicLLM

        return AnthropicLLM(model)
    if provider == "openai":
        from oncall_agent.agent.llm.providers import OpenAILLM

        return OpenAILLM(model)
    if provider in ("gemini", "vertex"):
        from oncall_agent.agent.llm.providers import GeminiLLM

        return GeminiLLM(
            model, vertex=provider == "vertex", project=settings.gcp_project, location=settings.gcp_location
        )
    raise ValueError(f"unknown LLM provider {provider!r}")
