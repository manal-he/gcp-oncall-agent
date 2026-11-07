"""Drive one investigation end to end: open MCP, run the graph, handle the approval pause.

One asyncio task owns the MCP session for the whole investigation (anyio task groups must be
entered and exited by the same task), and waits on an ``Approver`` callback while the graph
is interrupted. The CLI approver prompts on the terminal, the API approver waits for a
POST from the UI, the eval approver applies a policy.
"""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command
from pydantic import SecretStr

from oncall_agent.agent.graph import AgentState, build_graph
from oncall_agent.agent.llm import make_llm
from oncall_agent.agent.llm.base import LLM
from oncall_agent.agent.mcp_gateway import MCPGateway
from oncall_agent.config import Settings
from oncall_agent.mcp_server.server import build_backend, create_server
from oncall_agent.telemetry import tracer

Approver = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Listener = Callable[[dict[str, Any]], None]


async def reject_all(_: dict[str, Any]) -> dict[str, Any]:
    return {"approved": False, "approver": "policy:reject-all", "comment": "auto-rejected"}


async def approve_all(_: dict[str, Any]) -> dict[str, Any]:
    return {"approved": True, "approver": "policy:auto-approve", "comment": "auto-approved (demo)"}


@dataclass
class Investigation:
    scenario: str | None
    alert: dict[str, Any] | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "pending"  # pending | running | awaiting_approval | completed | failed
    events: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    duration_s: float = 0.0
    listeners: list[Listener] = field(default_factory=list, repr=False)
    backend: Any = field(default=None, repr=False)

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        event = {"seq": len(self.events), "type": kind, "at": datetime.now(UTC).isoformat(), "data": data}
        self.events.append(event)
        for listener in list(self.listeners):
            listener(event)

    def snapshot(self) -> dict[str, Any]:
        st = self.state
        return {
            "id": self.id,
            "scenario": self.scenario,
            "status": self.status,
            "created_at": self.created_at,
            "duration_s": round(self.duration_s, 2),
            "error": self.error,
            "alert": self.alert,
            "report": st.get("report"),
            "approval": st.get("approval"),
            "action_result": st.get("action_result"),
            "usage": st.get("usage"),
            "steps": st.get("steps", 0),
            "stop_reason": st.get("stop_reason"),
            "tool_calls": st.get("tool_calls", []),
            "events": self.events,
        }

    async def run(self, settings: Settings, approver: Approver, llm: LLM | None = None) -> dict[str, Any]:
        llm = llm or make_llm(settings)
        self.status = "running"
        start = time.perf_counter()
        try:
            gateway, alert = self._open(settings)
            self.alert = self.alert or alert
            with tracer().start_as_current_span("investigation") as span:
                span.set_attribute("oncall.investigation.id", self.id)
                span.set_attribute("oncall.scenario", self.scenario or "")
                async with gateway:
                    specs = await gateway.list_tools()
                    graph = build_graph(llm, gateway, specs, settings, self.emit)
                    config: Any = {"configurable": {"thread_id": self.id}}
                    init: AgentState = {"investigation_id": self.id, "alert": self.alert or {}}
                    self.emit("started", {"scenario": self.scenario, "llm": f"{llm.provider}:{llm.model}"})
                    result = await graph.ainvoke(init, config)
                    while result.get("__interrupt__"):
                        self.state = {k: v for k, v in result.items() if k != "__interrupt__"}
                        request = result["__interrupt__"][0].value
                        self.status = "awaiting_approval"
                        self.emit("approval_required", request)
                        decision = await approver(request)
                        self.status = "running"
                        result = await graph.ainvoke(Command(resume=decision), config)
                    self.state = dict(result)
                    span.set_attribute("oncall.investigation.steps", self.state.get("steps", 0))
            self.status = "completed"
            self.emit("completed", {"stop_reason": self.state.get("stop_reason")})
        except Exception as exc:
            self.status = "failed"
            self.error = f"{type(exc).__name__}: {exc}"
            self.emit("failed", {"error": self.error})
            raise
        finally:
            self.duration_s = time.perf_counter() - start
        return self.snapshot()

    def _open(self, settings: Settings) -> tuple[MCPGateway, dict[str, Any] | None]:
        """Pick the MCP transport. Returns (gateway, scenario alert if simulated)."""
        alert = None
        if settings.backend == "simulated":
            from oncall_agent.mcp_server.backends.simulated import SimulatedBackend

            sim = SimulatedBackend(self.scenario or settings.scenario)
            alert = sim.alert
            self.backend = sim
        if settings.mcp_transport == "stdio":
            # The child server must verify tokens minted here: share a fresh per-run secret.
            secret = settings.approval_secret.get_secret_value() if settings.approval_secret else secrets.token_hex(32)
            settings.approval_secret = SecretStr(secret)
            return MCPGateway.stdio(self.scenario or settings.scenario, {"ONCALL_APPROVAL_SECRET": secret}), alert
        if settings.mcp_transport == "http":
            return MCPGateway(settings.mcp_url), alert
        backend = self.backend if self.backend is not None else build_backend(settings, self.scenario)
        self.backend = backend
        return MCPGateway(create_server(backend, settings)), alert
