"""LangGraph state machine for an investigation.

    plan -> investigate <-> tools -> report -> [approval (interrupt) -> execute] -> END

* ``investigate`` only ever sees the read-only tool allowlist.
* ``tools`` enforces the allowlist again (defence in depth: a model can emit a call to a
  tool it was not offered) and the tool-call budget.
* ``report`` asks for a pydantic ``IncidentReport`` and then grounds it: evidence refs that
  do not correspond to a real tool call are dropped and confidence is reduced.
* ``approval`` pauses the graph with ``interrupt()``; a human decision resumes it.
* ``execute`` is the only node that can call the write tool, and it mints the approval token
  bound to the exact parameters the human saw.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from oncall_agent.agent.llm.base import LLM, LLMResponse, Message, ToolSpec, Usage
from oncall_agent.agent.mcp_gateway import MCPGateway
from oncall_agent.agent.prompts import INVESTIGATE_PROMPT, PLAN_PROMPT, REPORT_PROMPT, SYSTEM_PROMPT
from oncall_agent.agent.report import ActionKind, IncidentReport, RecommendedAction
from oncall_agent.config import Settings
from oncall_agent.mcp_server.server import READ_ONLY_TOOLS
from oncall_agent.safety import approval_secret, issue_approval_token, wrap_tool_output
from oncall_agent.telemetry import tracer

Emit = Callable[[str, dict[str, Any]], None]

_NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")


class AgentState(TypedDict, total=False):
    investigation_id: str
    alert: dict[str, Any]
    messages: list[Message]
    tool_calls: list[dict[str, Any]]
    steps: int
    usage: dict[str, Any]
    report: dict[str, Any] | None
    approval: dict[str, Any] | None
    action_result: dict[str, Any] | None
    stop_reason: str | None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_usage(state: AgentState, u: Usage, settings: Settings) -> dict[str, Any]:
    cur = dict(state.get("usage") or {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "estimated": False})
    cur["input_tokens"] += u.input_tokens
    cur["output_tokens"] += u.output_tokens
    cur["cost_usd"] = round(
        cur["input_tokens"] * settings.price_input_per_mtok / 1e6
        + cur["output_tokens"] * settings.price_output_per_mtok / 1e6,
        6,
    )
    cur["estimated"] = cur["estimated"] or u.estimated
    return cur


def _budget_exceeded(state: AgentState, settings: Settings) -> str | None:
    u = state.get("usage") or {}
    if state.get("steps", 0) >= settings.max_steps:
        return f"max_steps ({settings.max_steps}) reached"
    if len(state.get("tool_calls") or []) >= settings.max_tool_calls:
        return f"max_tool_calls ({settings.max_tool_calls}) reached"
    if u.get("input_tokens", 0) + u.get("output_tokens", 0) >= settings.max_total_tokens:
        return f"max_total_tokens ({settings.max_total_tokens}) reached"
    if settings.max_cost_usd and u.get("cost_usd", 0) >= settings.max_cost_usd:
        return f"max_cost_usd ({settings.max_cost_usd}) reached"
    return None


def validate_rollback_params(params: dict[str, Any], settings: Settings) -> str | None:
    for key in ("project_id", "region", "service", "target_revision"):
        if not isinstance(params.get(key), str) or not params[key]:
            return f"missing {key}"
    if not _NAME_RE.match(params["service"]) or not _NAME_RE.match(params["target_revision"]):
        return "invalid service/revision name"
    allowed = settings.allowed_project_list
    if allowed and params["project_id"] not in allowed:
        return "project not in allowlist"
    return None


def build_graph(
    llm: LLM,
    gateway: MCPGateway,
    tool_specs: list[ToolSpec],
    settings: Settings,
    emit: Emit,
    checkpointer: InMemorySaver | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    allowlist = set(READ_ONLY_TOOLS)
    read_tools = [t for t in tool_specs if t.name in allowlist]

    async def call_llm(op: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        with tracer().start_as_current_span(f"chat {llm.model}") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.provider.name", llm.provider)
            span.set_attribute("gen_ai.request.model", llm.model)
            span.set_attribute("oncall.agent.phase", op)
            resp = await llm.complete(messages, tools)
            span.set_attribute("gen_ai.usage.input_tokens", resp.usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", resp.usage.output_tokens)
            return resp

    def assistant_message(resp: LLMResponse) -> Message:
        msg: Message = {
            "role": "assistant",
            "content": resp.text,
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in resp.tool_calls],
        }
        if resp.raw is not None:
            key = "_gemini_content" if llm.provider in ("gemini", "vertex") else f"_{llm.provider}_content"
            msg[key] = resp.raw
        return msg

    # ------------------------------------------------------------------ nodes
    async def plan(state: AgentState) -> dict[str, Any]:
        with tracer().start_as_current_span("agent.plan"):
            system = SYSTEM_PROMPT.format(max_steps=settings.max_steps, max_tool_calls=settings.max_tool_calls)
            messages: list[Message] = [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": PLAN_PROMPT.format(alert_json=json.dumps(state["alert"], indent=2)),
                },
            ]
            resp = await call_llm("plan", messages, [])
            messages.append(assistant_message(resp))
            messages.append({"role": "user", "content": INVESTIGATE_PROMPT})
            emit("plan", {"text": resp.text})
            return {
                "messages": messages,
                "steps": state.get("steps", 0) + 1,
                "usage": _add_usage(state, resp.usage, settings),
                "tool_calls": state.get("tool_calls") or [],
            }

    async def investigate(state: AgentState) -> dict[str, Any]:
        reason = _budget_exceeded(state, settings)
        if reason:
            emit("budget", {"reason": reason})
            return {"stop_reason": reason}
        with tracer().start_as_current_span("agent.investigate") as span:
            span.set_attribute("oncall.agent.step", state.get("steps", 0))
            resp = await call_llm("investigate", state["messages"], read_tools)
            emit(
                "llm_step",
                {
                    "text": resp.text,
                    "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in resp.tool_calls],
                },
            )
            return {
                "messages": [*state["messages"], assistant_message(resp)],
                "steps": state.get("steps", 0) + 1,
                "usage": _add_usage(state, resp.usage, settings),
            }

    async def tools(state: AgentState) -> dict[str, Any]:
        records = list(state.get("tool_calls") or [])
        messages = list(state["messages"])
        stop_reason = None
        for call in messages[-1].get("tool_calls", []):
            ref = f"T{len(records) + 1}"
            rec: dict[str, Any] = {
                "ref": ref,
                "tool": call["name"],
                "args": call["arguments"],
                "ok": False,
                "blocked": False,
                "error": None,
                "duration_ms": 0.0,
                "result": None,
                "suspected_injection": 0,
                "phase": "investigate",
            }
            if len(records) >= settings.max_tool_calls:
                stop_reason = f"max_tool_calls ({settings.max_tool_calls}) reached"
                content = f"NOT EXECUTED: tool-call budget exhausted ({stop_reason})."
                rec["error"] = "budget"
            elif call["name"] not in allowlist:
                rec["blocked"] = True
                rec["error"] = "tool not in investigation allowlist"
                content = (
                    f"BLOCKED: '{call['name']}' is not available during investigation. Write actions are only "
                    "executed after a human approves the recommended_action in the final report."
                )
                with tracer().start_as_current_span("guardrail.blocked_tool_call") as span:
                    span.set_attribute("gen_ai.tool.name", call["name"])
                emit("blocked", {"ref": ref, "tool": call["name"], "arguments": call["arguments"]})
            else:
                res = await gateway.call(call["name"], call["arguments"])
                rec.update(ok=res.ok, error=res.error, duration_ms=round(res.duration_ms, 1), result=res.payload)
                if res.ok:
                    entries = res.payload.get("entries", []) if isinstance(res.payload, dict) else []
                    rec["suspected_injection"] = sum(1 for e in entries if e.get("suspected_prompt_injection"))
                    content = wrap_tool_output(ref, call["name"], res.payload)
                else:
                    content = f'<tool_error ref="{ref}" tool="{call["name"]}">{res.error}</tool_error>'
                emit(
                    "tool_call",
                    {k: rec[k] for k in ("ref", "tool", "args", "ok", "error", "duration_ms", "suspected_injection")}
                    | {"result": rec["result"]},
                )
            records.append(rec)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": content,
                    "is_error": not rec["ok"],
                }
            )
        out: dict[str, Any] = {"messages": messages, "tool_calls": records}
        if stop_reason:
            out["stop_reason"] = stop_reason
        return out

    async def report(state: AgentState) -> dict[str, Any]:
        with tracer().start_as_current_span("agent.report") as span:
            messages = [*state["messages"], {"role": "user", "content": REPORT_PROMPT}]
            rep, usage = await llm.structured(messages, IncidentReport)
            records = state.get("tool_calls") or []
            valid = {r["ref"] for r in records if r["ok"]}
            grounded = [e for e in rep.evidence if e.ref in valid]
            if len(grounded) < len(rep.evidence):
                rep.open_questions.append(
                    f"{len(rep.evidence) - len(grounded)} evidence item(s) cited non-existent tool calls "
                    "and were removed."
                )
                rep.confidence = round(rep.confidence * 0.8, 2)
            rep.evidence = grounded
            for t in rep.timeline:
                t.evidence_refs = [r for r in t.evidence_refs if r in valid]
            if any(r["suspected_injection"] for r in records) and not rep.security_notes:
                rep.security_notes.append("Tool output contained text flagged as possible prompt injection.")
            if any(r["blocked"] for r in records):
                rep.security_notes.append(
                    "The model attempted tool calls outside the read-only allowlist; they were blocked."
                )
            if state.get("stop_reason"):
                rep.open_questions.append(f"Investigation stopped early: {state['stop_reason']}.")
            act = rep.recommended_action
            if act.kind == ActionKind.rollback_cloud_run:
                problem = validate_rollback_params(act.params, settings)
                if problem:
                    rep.recommended_action = RecommendedAction(
                        kind=ActionKind.escalate,
                        description=f"Rollback suggested but parameters were invalid ({problem}): {act.description}",
                    )
                else:
                    act.requires_approval = True
            span.set_attribute("oncall.report.category", rep.root_cause_category.value)
            span.set_attribute("oncall.report.confidence", rep.confidence)
            data = rep.model_dump(mode="json")
            emit("report", data)
            return {
                "report": data,
                "usage": _add_usage(state, usage, settings),
                "steps": state.get("steps", 0) + 1,
            }

    async def approval(state: AgentState) -> dict[str, Any]:
        rep = state["report"] or {}
        decision = interrupt(
            {
                "type": "approval_request",
                "action": rep.get("recommended_action"),
                "root_cause": rep.get("root_cause"),
                "confidence": rep.get("confidence"),
            }
        )
        if not isinstance(decision, dict):
            decision = {"approved": False, "comment": "invalid decision payload"}
        result = {
            "approved": bool(decision.get("approved")),
            "approver": str(decision.get("approver", "unknown"))[:100],
            "comment": str(decision.get("comment", ""))[:500],
            "decided_at": _now(),
        }
        with tracer().start_as_current_span("agent.approval") as span:
            span.set_attribute("oncall.approval.approved", bool(result["approved"]))
        emit("approval", result)
        return {"approval": result}

    async def execute(state: AgentState) -> dict[str, Any]:
        params = (state["report"] or {})["recommended_action"]["params"]
        action = {
            "tool": "rollback_cloud_run_revision",
            "project_id": params["project_id"],
            "region": params["region"],
            "service": params["service"],
            "target_revision": params["target_revision"],
        }
        token = issue_approval_token(approval_secret(settings), action, settings.approval_ttl_seconds)
        approver = (state.get("approval") or {}).get("approver", "unknown")
        args = {k: v for k, v in action.items() if k != "tool"} | {
            "approval_token": token,
            "reason": f"approved by {approver} for investigation {state.get('investigation_id')}",
        }
        with tracer().start_as_current_span("agent.execute"):
            res = await gateway.call("rollback_cloud_run_revision", args)
        records = list(state.get("tool_calls") or [])
        safe_args = {k: v for k, v in args.items() if k != "approval_token"}
        records.append(
            {
                "ref": f"T{len(records) + 1}",
                "tool": "rollback_cloud_run_revision",
                "args": safe_args,
                "ok": res.ok,
                "blocked": False,
                "error": res.error,
                "duration_ms": round(res.duration_ms, 1),
                "result": res.payload,
                "suspected_injection": 0,
                "phase": "execute",
            }
        )
        result = {"ok": res.ok, "result": res.payload, "error": res.error}
        emit("action_executed", result)
        return {"action_result": result, "tool_calls": records}

    # ------------------------------------------------------------------ routing
    def after_investigate(state: AgentState) -> str:
        if state.get("stop_reason"):
            return "report"
        last = state["messages"][-1]
        return "tools" if last["role"] == "assistant" and last.get("tool_calls") else "report"

    def after_tools(state: AgentState) -> str:
        return "report" if state.get("stop_reason") else "investigate"

    def after_report(state: AgentState) -> str:
        action = (state.get("report") or {}).get("recommended_action", {})
        return "approval" if action.get("kind") == ActionKind.rollback_cloud_run.value else END

    def after_approval(state: AgentState) -> str:
        return "execute" if (state.get("approval") or {}).get("approved") else END

    g = StateGraph(AgentState)
    g.add_node("plan", plan)
    g.add_node("investigate", investigate)
    g.add_node("tools", tools)
    g.add_node("report", report)
    g.add_node("approval", approval)
    g.add_node("execute", execute)
    g.add_edge(START, "plan")
    g.add_edge("plan", "investigate")
    g.add_conditional_edges("investigate", after_investigate, ["tools", "report"])
    g.add_conditional_edges("tools", after_tools, ["investigate", "report"])
    g.add_conditional_edges("report", after_report, ["approval", END])
    g.add_conditional_edges("approval", after_approval, ["execute", END])
    g.add_edge("execute", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())
