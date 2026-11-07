"""Provider-neutral LLM interface.

Messages are plain dicts so they can live in LangGraph checkpoints:

    {"role": "system" | "user" | "assistant" | "tool", "content": str,
     "tool_calls": [{"id", "name", "arguments"}],   # assistant only
     "tool_call_id": str, "name": str}               # tool only
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Message = dict[str, Any]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    raw: Any = None  # provider-native assistant content to replay verbatim (e.g. thinking blocks)


class LLM(Protocol):
    provider: str
    model: str

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        """One model turn. May return text, tool calls, or both."""
        ...

    async def structured(self, messages: list[Message], schema: type[T]) -> tuple[T, Usage]:
        """Return an instance of ``schema`` (validated) for the conversation so far."""
        ...


def estimate_tokens(text: str) -> int:
    """Rough chars/4 estimate, used only by offline providers."""
    return max(1, len(text) // 4)
