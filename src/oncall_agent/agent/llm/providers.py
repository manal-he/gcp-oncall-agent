"""Hosted LLM providers: Gemini (AI Studio or Vertex AI), Anthropic Claude, OpenAI.

SDKs are optional extras and imported lazily. The message-conversion functions are pure and
unit-tested offline; the network calls themselves are not exercised in CI (no keys).

Structured output: the final report is requested with a *flattened* transcript (one user
message containing the evidence) rather than the raw tool-call history. That keeps the
report step provider-agnostic and avoids provider-specific rules about replaying tool-use
blocks without tool definitions.
"""

from __future__ import annotations

import json
import os
from typing import Any

from oncall_agent.agent.llm.base import LLMResponse, Message, T, ToolCall, ToolSpec, Usage


def flatten_transcript(messages: list[Message]) -> tuple[str, str]:
    """Return (system_text, user_text) summarising the whole conversation."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    parts = []
    for m in messages:
        if m["role"] == "user":
            parts.append(f"[user]\n{m['content']}")
        elif m["role"] == "assistant":
            calls = "".join(
                f"\n-> call {c['name']}({json.dumps(c['arguments'], sort_keys=True)}) id={c['id']}"
                for c in m.get("tool_calls", [])
            )
            parts.append(f"[assistant]\n{m.get('content', '')}{calls}")
        elif m["role"] == "tool":
            parts.append(f"[tool {m.get('name')} id={m.get('tool_call_id')}]\n{m['content']}")
    parts.append(
        "[user]\nWrite the final incident report now as JSON matching the schema. Cite evidence by "
        "the ref attribute of the tool_result blocks (e.g. T3). Do not invent refs."
    )
    return system, "\n\n".join(parts)


# =========================================================================== Anthropic
def to_anthropic(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            if m.get("_anthropic_content"):  # replay provider blocks (incl. thinking) unchanged
                out.append({"role": "assistant", "content": m["_anthropic_content"]})
                continue
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]})
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if m.get("is_error"):
                block["is_error"] = True
            # All results for one assistant turn go in a single user message.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return system, out


class AnthropicLLM:
    provider = "anthropic"

    def __init__(self, model: str | None = None) -> None:
        import anthropic

        self.model = model or "claude-opus-5-5"
        self._client = anthropic.AsyncAnthropic()  # ANTHROPIC_API_KEY or `ant auth login` profile

    def _common(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": 16000,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "medium"},
            # Server-side refusal fallback (beta) so a classifier refusal does not stall an incident.
            "extra_headers": {"anthropic-beta": "server-side-fallback-2026-07-01"},
            "extra_body": {"fallbacks": "default"},
        }

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        system, msgs = to_anthropic(messages)
        resp = await self._client.messages.create(
            system=system,
            messages=msgs,
            tools=[{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools],
            tool_choice={"type": "auto"},
            **self._common(),
        )
        if resp.stop_reason == "refusal":
            return LLMResponse(text="[model refused this turn]", usage=Usage(resp.usage.input_tokens, 0))
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        out = LLMResponse(text=text, tool_calls=calls, usage=Usage(resp.usage.input_tokens, resp.usage.output_tokens))
        out.raw = [b.model_dump(exclude_none=True) for b in resp.content]
        return out

    async def structured(self, messages: list[Message], schema: type[T]) -> tuple[T, Usage]:
        system, user = flatten_transcript(messages)
        resp = await self._client.messages.parse(
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            **self._common(),
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise RuntimeError(f"no structured output (stop_reason={resp.stop_reason})")
        return resp.parsed_output, Usage(resp.usage.input_tokens, resp.usage.output_tokens)


# =========================================================================== OpenAI
def to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] in ("system", "user"):
            out.append({"role": m["role"], "content": m["content"]})
        elif m["role"] == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                    }
                    for c in m["tool_calls"]
                ]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


class OpenAILLM:
    provider = "openai"

    def __init__(self, model: str | None = None) -> None:
        import openai

        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
        self._client = openai.AsyncOpenAI()

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=to_openai(messages),
            tools=[
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ],
        )
        choice = resp.choices[0].message
        calls = [
            ToolCall(c.id, c.function.name, json.loads(c.function.arguments or "{}")) for c in choice.tool_calls or []
        ]
        u = resp.usage
        return LLMResponse(
            text=choice.content or "",
            tool_calls=calls,
            usage=Usage(u.prompt_tokens if u else 0, u.completion_tokens if u else 0),
        )

    async def structured(self, messages: list[Message], schema: type[T]) -> tuple[T, Usage]:
        system, user = flatten_transcript(messages)
        resp = await self._client.chat.completions.parse(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
        )
        parsed = resp.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError("no structured output")
        u = resp.usage
        return parsed, Usage(u.prompt_tokens if u else 0, u.completion_tokens if u else 0)


# =========================================================================== Gemini
def _gemini_usage(um: Any) -> Usage:
    if um is None:
        return Usage()
    return Usage(um.prompt_token_count or 0, um.candidates_token_count or 0)


def to_gemini(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Return (system_instruction, contents) using google-genai dict shapes."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    contents: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "user":
            contents.append({"role": "user", "parts": [{"text": m["content"]}]})
        elif m["role"] == "assistant":
            if m.get("_gemini_content"):  # keep thought signatures intact
                contents.append(m["_gemini_content"])
                continue
            parts: list[dict[str, Any]] = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            for c in m.get("tool_calls", []):
                parts.append({"function_call": {"id": c["id"], "name": c["name"], "args": c["arguments"]}})
            contents.append({"role": "model", "parts": parts})
        elif m["role"] == "tool":
            part = {
                "function_response": {
                    "id": m["tool_call_id"],
                    "name": m["name"],
                    "response": {"result": m["content"]},
                }
            }
            if contents and contents[-1]["role"] == "user" and "function_response" in contents[-1]["parts"][0]:
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
    return system, contents


class GeminiLLM:
    """Gemini via the Gen AI SDK. ``vertex=True`` uses Vertex AI with ADC (no API key)."""

    def __init__(
        self,
        model: str | None = None,
        *,
        vertex: bool = False,
        project: str | None = None,
        location: str = "global",
    ) -> None:
        from google import genai

        self.provider = "vertex" if vertex else "gemini"
        self.model = model or "gemini-2.5-flash"
        self._client = genai.Client(vertexai=True, project=project, location=location) if vertex else genai.Client()

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        from google.genai import types

        system, contents = to_gemini(messages)
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name, description=t.description, parameters_json_schema=t.parameters
                        )
                        for t in tools
                    ]
                )
            ],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp = await self._client.aio.models.generate_content(model=self.model, contents=contents, config=config)
        calls = [
            ToolCall(fc.id or f"call_{i}", fc.name or "", dict(fc.args or {}))
            for i, fc in enumerate(resp.function_calls or [])
        ]
        text = (
            "".join(p.text or "" for p in (resp.candidates[0].content.parts or []) if p.text) if resp.candidates else ""
        )
        um = resp.usage_metadata
        out = LLMResponse(
            text=text,
            tool_calls=calls,
            usage=_gemini_usage(um),
        )
        if resp.candidates and resp.candidates[0].content:
            out.raw = resp.candidates[0].content.model_dump(exclude_none=True)
        return out

    async def structured(self, messages: list[Message], schema: type[T]) -> tuple[T, Usage]:
        from google.genai import types

        system, user = flatten_transcript(messages)
        resp = await self._client.aio.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system, response_mime_type="application/json", response_schema=schema
            ),
        )
        parsed = resp.parsed if isinstance(resp.parsed, schema) else schema.model_validate_json(resp.text or "{}")
        um = resp.usage_metadata
        return parsed, _gemini_usage(um)
