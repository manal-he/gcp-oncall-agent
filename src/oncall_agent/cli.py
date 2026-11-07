"""Command-line interface: ``oncall --help``."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from oncall_agent.config import Settings, get_settings

app = typer.Typer(add_completion=False, help="AI on-call engineer for Google Cloud, built on MCP.")
console = Console(stderr=True)


def _settings(**overrides: Any) -> Settings:
    base = get_settings()
    return base.model_copy(update={k: v for k, v in overrides.items() if v is not None})


@app.command()
def scenarios() -> None:
    """List the simulated incident scenarios."""
    from oncall_agent.mcp_server.backends.simulated import list_scenarios

    table = Table("name", "title")
    for s in list_scenarios():
        table.add_row(s["name"], s["title"])
    Console().print(table)


@app.command()
def investigate(
    scenario: str = typer.Option("bad-deploy", "--scenario", "-s", help="Simulated scenario name"),
    llm: str | None = typer.Option(None, "--llm", help="mock | mock-injectable | gemini | vertex | anthropic | openai"),
    transport: str | None = typer.Option(None, "--transport", help="MCP transport: inprocess | stdio | http"),
    auto_approve: bool = typer.Option(False, "--auto-approve", help="Approve write actions without prompting"),
    auto_reject: bool = typer.Option(False, "--auto-reject", help="Reject write actions without prompting"),
    json_out: Path | None = typer.Option(None, "--json-out", help="Write the full investigation snapshot here"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print tool results"),
) -> None:
    """Investigate an alert and produce an incident report."""
    from oncall_agent.agent.report import IncidentReport, render_markdown
    from oncall_agent.agent.runner import Investigation, approve_all, reject_all
    from oncall_agent.telemetry import setup_tracing

    settings = _settings(llm_provider=llm, mcp_transport=transport, scenario=scenario)
    setup_tracing(settings)

    def show(event: dict[str, Any]) -> None:
        t, d = event["type"], event["data"]
        if t == "plan":
            console.print(Panel(d["text"], title="plan", border_style="cyan"))
        elif t == "tool_call":
            status = "[green]ok[/]" if d["ok"] else f"[red]error[/] {d['error']}"
            flag = f" [yellow]({d['suspected_injection']} suspected injection)[/]" if d["suspected_injection"] else ""
            console.print(f"  [bold]{d['ref']}[/] {d['tool']}({json.dumps(d['args'])}) {status}{flag}")
            if verbose and d.get("result") is not None:
                console.print_json(json.dumps(d["result"])[:4000])
        elif t == "blocked":
            console.print(f"  [bold red]BLOCKED[/] {d['tool']}({json.dumps(d['arguments'])}) - not in allowlist")
        elif t == "budget":
            console.print(f"[yellow]budget: {d['reason']}[/]")
        elif t == "action_executed":
            console.print(Panel(json.dumps(d, indent=2), title="action executed", border_style="green"))

    async def approver(request: dict[str, Any]) -> dict[str, Any]:
        if auto_approve:
            return await approve_all(request)
        if auto_reject:
            return await reject_all(request)
        a = request.get("action") or {}
        console.print(
            Panel(
                f"{a.get('description')}\nparams: {json.dumps(a.get('params'))}\n"
                f"confidence: {request.get('confidence')}",
                title="APPROVAL REQUIRED",
                border_style="red",
            )
        )
        ok = await asyncio.to_thread(typer.confirm, "Execute this action?", default=False)
        return {
            "approved": ok,
            "approver": "cli-user",
            "comment": "approved in CLI" if ok else "rejected in CLI",
        }

    inv = Investigation(scenario)
    inv.listeners.append(show)
    console.print(
        f"[bold]Investigating[/] scenario={scenario} llm={settings.llm_provider} "
        f"transport={settings.mcp_transport} backend={settings.backend}"
    )
    snap = asyncio.run(inv.run(settings, approver))
    if snap.get("report"):
        Console().print(Markdown(render_markdown(IncidentReport.model_validate(snap["report"]))))
    u = snap.get("usage") or {}
    console.print(
        f"steps={snap['steps']} tool_calls={len(snap['tool_calls'])} tokens_in={u.get('input_tokens')} "
        f"tokens_out={u.get('output_tokens')}{' (estimated)' if u.get('estimated') else ''} "
        f"time={snap['duration_s']}s"
    )
    if json_out:
        json_out.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")


@app.command()
def mcp(
    transport: str = typer.Option("stdio", "--transport", "-t", help="stdio | http"),
    port: int = typer.Option(8765, "--port"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address for http (0.0.0.0 in containers)"),
    scenario: str | None = typer.Option(None, "--scenario", "-s"),
) -> None:
    """Run the MCP server (for Claude Desktop, Claude Code, ADK or any MCP client)."""
    from oncall_agent.mcp_server.server import build_backend, create_server

    settings = _settings(scenario=scenario)
    server = create_server(build_backend(settings), settings)
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=host, port=port)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run the FastAPI service (webhooks + UI API)."""
    import uvicorn

    uvicorn.run("oncall_agent.api.app:create_app", factory=True, host=host, port=port)


@app.command("eval")
def run_eval(
    llm: str = typer.Option("mock", "--llm"),
    judge: str = typer.Option("rubric", "--judge", help="rubric | llm"),
    judge_llm: str | None = typer.Option(None, "--judge-llm", help="provider for the LLM judge"),
    approval_policy: str = typer.Option("approve-all", "--approval-policy", help="approve-all | oracle | reject-all"),
    cases: Path = typer.Option(Path("evals/cases"), "--cases"),
    out: Path = typer.Option(Path("evals/reports/latest.md"), "--out"),
    scenario: list[str] = typer.Option(None, "--scenario", "-s"),
) -> None:
    """Run the eval suite and write a Markdown + JSON report."""
    from oncall_agent.agent.llm import make_llm
    from oncall_agent.evals.harness import load_cases, run_case, write_outputs
    from oncall_agent.evals.judge import Judge, LLMJudge, RubricJudge

    settings = _settings()
    case_list = load_cases(cases, scenario or None)
    if not case_list:
        raise typer.BadParameter(f"no cases found in {cases}")
    j: Judge = LLMJudge(make_llm(settings, judge_llm or llm)) if judge == "llm" else RubricJudge()

    async def main() -> list[Any]:
        results = []
        for c in case_list:
            r = await run_case(c, settings, llm, j, approval_policy)
            mark = "[green]pass[/]" if r.root_cause_correct else "[red]FAIL[/]"
            console.print(
                f"{c.scenario:<22} {mark} predicted={r.predicted_category} action={r.action_kind} "
                f"P={r.tool_precision:.2f} R={r.tool_recall:.2f} unsafe={r.unsafe}"
            )
            results.append(r)
        return results

    results = asyncio.run(main())
    probe = make_llm(settings, llm)
    note = None
    if llm.startswith("mock"):
        note = (
            "The mock LLM is a deterministic rule-based playbook written alongside these scenarios. "
            "These numbers validate the harness and guardrails; they are not a measure of agent quality."
        )
    meta = {
        "run_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "llm": f"{probe.provider}:{probe.model}",
        "judge": j.name,
        "approval_policy": approval_policy,
        "max_steps": settings.max_steps,
        "max_tool_calls": settings.max_tool_calls,
        "note": note,
    }
    agg = write_outputs(results, meta, out)
    console.print_json(json.dumps(agg))
    console.print(f"report written to {out}")


@app.command("issue-approval-token")
def issue_token(
    project_id: str = typer.Option(...),
    region: str = typer.Option(...),
    service: str = typer.Option(...),
    target_revision: str = typer.Option(...),
) -> None:
    """Mint a short-lived approval token for a rollback (running this IS the human approval).

    Lets an external MCP client (e.g. Claude Desktop) execute the write tool after a human has
    reviewed the action. Requires ONCALL_APPROVAL_SECRET to be shared with the MCP server.
    """
    from oncall_agent.safety import approval_secret, issue_approval_token

    settings = get_settings()
    if settings.approval_secret is None:
        raise typer.BadParameter("set ONCALL_APPROVAL_SECRET (same value as the MCP server)")
    action = {
        "tool": "rollback_cloud_run_revision",
        "project_id": project_id,
        "region": region,
        "service": service,
        "target_revision": target_revision,
    }
    typer.echo(issue_approval_token(approval_secret(settings), action, settings.approval_ttl_seconds))


if __name__ == "__main__":
    app()
