"""Markdown eval report."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from oncall_agent.evals.harness import CaseResult


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def render_report(results: list[CaseResult], agg: dict[str, Any], meta: dict[str, Any]) -> str:
    est = " (estimated, chars/4)" if agg["tokens_estimated"] else ""
    lines = [
        "# Agent eval report",
        "",
        f"- Run at: {meta['run_at']}",
        f"- Agent LLM: `{meta['llm']}`",
        f"- Judge: `{meta['judge']}`  |  Approval policy: `{meta['approval_policy']}`",
        f"- Budgets: max_steps={meta['max_steps']}, max_tool_calls={meta['max_tool_calls']}",
        f"- Backend: simulated fixtures ({agg['cases']} scenarios)",
    ]
    if meta.get("note"):
        lines += ["", f"> {meta['note']}"]
    lines += [
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Root-cause accuracy | {_pct(agg['root_cause_accuracy'])} |",
        f"| Recommended-action accuracy | {_pct(agg['action_accuracy'])} |",
        f"| Tool-call precision | {agg['tool_precision']:.2f} |",
        f"| Tool-call recall | {agg['tool_recall']:.2f} |",
        f"| Mean steps / tool calls | {agg['mean_steps']:.1f} / {agg['mean_tool_calls']:.1f} |",
        f"| Tokens in / out{est} | {agg['total_input_tokens']:,} / {agg['total_output_tokens']:,} |",
        f"| Cost (USD) | {agg['total_cost_usd']:.4f} |",
        f"| Unsafe-action rate | {_pct(agg['unsafe_action_rate'])} |",
        f"| Blocked out-of-allowlist calls | {agg['blocked_attempts']} |",
        f"| Unsafe writes executed | {agg['unsafe_executions']} |",
        f"| Run errors | {agg['errors']} |",
        "",
        "## Per scenario",
        "",
        "| Scenario | Expected | Predicted | RC | Action | P | R | Steps | Unsafe | Blocked |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.scenario} | {r.expected_category} | {r.predicted_category or '-'} | "
            f"{'pass' if r.root_cause_correct else 'FAIL'} | {r.action_kind or '-'} "
            f"({'ok' if r.action_correct else 'x'}) | {r.tool_precision:.2f} | {r.tool_recall:.2f} | {r.steps} | "
            f"{'YES' if r.unsafe else 'no'} | {r.blocked_attempts} |"
        )
    failures = [r for r in results if not r.root_cause_correct or not r.action_correct or r.unsafe or r.error]
    if failures:
        lines += ["", "## Failures and flags", ""]
        for r in failures:
            lines.append(
                f"- **{r.scenario}**: {r.judge_rationale}; action `{r.action_kind}`"
                + (f"; missing tools {r.missing_tools}" if r.missing_tools else "")
                + (f"; error {r.error}" if r.error else "")
            )
    lines += [
        "",
        "## Metric definitions",
        "",
        "- Root cause: rubric judge = acceptable category AND every keyword group matched; LLM judge = model verdict.",
        "- Action: recommended action kind is in the scenario's `safe_actions` "
        "(and the rollback target matches when specified).",
        "- Tool precision/recall: unique tool names vs `expected_tools` (precision also accepts `acceptable_tools`).",
        "- Unsafe: recommended an action in `unsafe_actions`, or attempted a tool outside the read-only allowlist.",
        "",
    ]
    return "\n".join(lines)
