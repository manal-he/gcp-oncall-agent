"""The agent's only path to tools: an MCP client session.

The agent never imports backends directly. Whether the server runs in-process (tests, demo),
as a stdio subprocess, or remotely over streamable HTTP, the agent sees the same MCP tool
list and the same validation/sanitization.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver import MCPServer

from oncall_agent.agent.llm.base import ToolSpec
from oncall_agent.telemetry import tracer


class ToolResult:
    def __init__(self, ok: bool, payload: Any, error: str | None, duration_ms: float) -> None:
        self.ok = ok
        self.payload = payload
        self.error = error
        self.duration_ms = duration_ms


class MCPGateway:
    def __init__(self, target: MCPServer | StdioServerParameters | str) -> None:
        self._target = target
        self._client: Client | None = None

    @classmethod
    def stdio(cls, scenario: str, env: dict[str, str] | None = None) -> MCPGateway:
        # Merged over the SDK's safe default environment; only pass what the server needs.
        child_env = {k: v for k, v in os.environ.items() if k.startswith(("ONCALL_", "GOOGLE_", "CLOUDSDK_"))}
        child_env.update({"ONCALL_SCENARIO": scenario, **(env or {})})
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "oncall_agent.mcp_server", "stdio"], env=child_env
        )
        return cls(params)

    async def __aenter__(self) -> MCPGateway:
        self._client = Client(self._target)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("MCPGateway used outside 'async with'")
        return self._client

    async def list_tools(self) -> list[ToolSpec]:
        result = await self.client.list_tools()
        return [ToolSpec(t.name, t.description or "", dict(t.input_schema)) for t in result.tools]

    async def tool_annotations(self) -> dict[str, dict[str, Any]]:
        result = await self.client.list_tools()
        return {t.name: (t.annotations.model_dump() if t.annotations else {}) for t in result.tools}

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        with tracer().start_as_current_span(f"execute_tool {name}") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", name)
            span.set_attribute("oncall.tool.arg_keys", sorted(arguments))
            start = time.perf_counter()
            res = await self.client.call_tool(name, arguments)
            duration = (time.perf_counter() - start) * 1000
            text = "".join(getattr(c, "text", "") for c in res.content)
            span.set_attribute("oncall.tool.is_error", bool(res.is_error))
            span.set_attribute("oncall.tool.duration_ms", round(duration, 1))
            if res.is_error:
                return ToolResult(False, None, text[:1000] or "tool error", duration)
            payload: Any = res.structured_content
            if payload is None:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"text": text}
            return ToolResult(True, payload, None, duration)
