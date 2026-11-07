"""Structured incident report produced by the agent."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RootCauseCategory(StrEnum):
    bad_deploy = "bad_deploy"
    config_error = "config_error"
    oom = "oom"
    quota_exceeded = "quota_exceeded"
    expired_credential = "expired_credential"
    iam_permission = "iam_permission"
    db_connection_pool = "db_connection_pool"
    noisy_neighbor = "noisy_neighbor"
    dependency_outage = "dependency_outage"
    build_failure = "build_failure"
    capacity = "capacity"
    false_positive = "false_positive"
    unknown = "unknown"


class ActionKind(StrEnum):
    rollback_cloud_run = "rollback_cloud_run"  # the only action the agent can execute
    config_fix = "config_fix"
    scale_resources = "scale_resources"
    rotate_secret = "rotate_secret"  # noqa: S105 - enum value, not a secret
    fix_iam = "fix_iam"
    request_quota = "request_quota"
    escalate = "escalate"
    none = "none"


class TimelineEvent(BaseModel):
    time: str = Field(description="RFC 3339 timestamp")
    event: str
    evidence_refs: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    ref: str = Field(description="Tool-call reference, e.g. T3")
    tool: str
    finding: str


class Hypothesis(BaseModel):
    statement: str
    status: str = Field(description="confirmed | rejected | open")
    evidence_refs: list[str] = Field(default_factory=list)


class RecommendedAction(BaseModel):
    kind: ActionKind
    description: str
    requires_approval: bool = True
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="For rollback_cloud_run: project_id, region, service, target_revision",
    )


class IncidentReport(BaseModel):
    title: str
    summary: str
    severity: str = Field(description="SEV1 | SEV2 | SEV3 | SEV4")
    root_cause_category: RootCauseCategory
    root_cause: str
    timeline: list[TimelineEvent] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_action: RecommendedAction
    open_questions: list[str] = Field(default_factory=list)
    security_notes: list[str] = Field(default_factory=list, description="e.g. suspected prompt injection found in logs")


def render_markdown(report: IncidentReport) -> str:
    lines = [
        f"# {report.title}",
        "",
        f"**Severity:** {report.severity}  |  **Confidence:** {report.confidence:.0%}  |  "
        f"**Category:** `{report.root_cause_category.value}`",
        "",
        "## Summary",
        report.summary,
        "",
        "## Root cause",
        report.root_cause,
        "",
        "## Timeline",
    ]
    lines += [f"- `{t.time}` {t.event} {' '.join(f'[{r}]' for r in t.evidence_refs)}" for t in report.timeline]
    lines += ["", "## Evidence"]
    lines += [f"- **[{e.ref}]** `{e.tool}`: {e.finding}" for e in report.evidence]
    if report.hypotheses:
        lines += ["", "## Hypotheses"]
        lines += [f"- ({h.status}) {h.statement}" for h in report.hypotheses]
    a = report.recommended_action
    lines += ["", "## Recommended action", f"`{a.kind.value}`: {a.description}"]
    if a.params:
        lines.append(f"Parameters: `{a.params}`")
    if report.security_notes:
        lines += ["", "## Security notes"] + [f"- {n}" for n in report.security_notes]
    if report.open_questions:
        lines += ["", "## Open questions"] + [f"- {q}" for q in report.open_questions]
    return "\n".join(lines) + "\n"
