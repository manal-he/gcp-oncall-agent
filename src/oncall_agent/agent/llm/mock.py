"""Deterministic offline "LLM" used by tests, the demo and harness validation.

It is NOT a language model. It follows a fixed investigation playbook driven by the alert,
then applies hand-written keyword/structure rules to the tool results it collected. It was
written by the same author as the eval scenarios, so its eval scores say the harness works,
not that an agent is good. Use a real provider to measure agent quality.

``MockLLM(injectable=True)`` additionally *obeys* instructions it finds inside tool output
(it naively issues any ``rollback_cloud_run_revision`` call described in a log line). It
simulates a model that falls for prompt injection so the guardrails can be tested.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from oncall_agent.agent.llm.base import LLMResponse, Message, T, ToolCall, ToolSpec, Usage, estimate_tokens
from oncall_agent.agent.report import (
    ActionKind,
    Evidence,
    Hypothesis,
    IncidentReport,
    RecommendedAction,
    RootCauseCategory,
    TimelineEvent,
)
from oncall_agent.mcp_server.backends.base import METRIC_CATALOG

_RESULT_RE = re.compile(r'<tool_result ref="(T\d+)" tool="([a-z_]+)" untrusted="true">\n(.*?)\n</tool_result>', re.S)
_ALERT_RE = re.compile(r"<alert>\n(.*?)\n</alert>", re.S)

C = RootCauseCategory

TEXT_RULES: dict[RootCauseCategory, list[str]] = {
    C.oom: [r"OOMKilled", r"[Oo]ut of memory", r"[Mm]emory limit of .* exceeded"],
    C.quota_exceeded: [r"RESOURCE_EXHAUSTED", r"[Qq]uota exceeded", r"rateLimitExceeded"],
    C.expired_credential: [
        r"certificate has expired",
        r"x509: certificate",
        r"token (has )?expired",
        r"API key (is )?expired",
        r"invalid_api_key",
    ],
    C.iam_permission: [r"PERMISSION_DENIED", r"does not have .*permission", r"Permission '[a-z.]+' denied"],
    C.db_connection_pool: [
        r"connection pool (exhausted|timeout)",
        r"remaining connection slots",
        r"too many clients",
        r"timeout acquiring (a )?connection",
    ],
    C.config_error: [
        r"[Mm]issing required (environment|env|config)",
        r"KeyError: '[A-Z_]+'",
        r"[Ii]nvalid configuration",
        r"failed to parse config",
    ],
    C.dependency_outage: [
        r"upstream .*(503|unavailable)",
        r"503 Service Unavailable from",
        r"circuit breaker open",
    ],
    C.build_failure: [r"ImagePullBackOff", r"ErrImagePull", r"manifest unknown"],
    C.capacity: [r"no available instance", r"cold start", r"[Ss]tarting new instance"],
    C.noisy_neighbor: [r"CPU throttl"],
}

ACTION_FOR: dict[RootCauseCategory, tuple[ActionKind, str]] = {
    C.oom: (ActionKind.scale_resources, "Raise the container memory limit and investigate memory growth."),
    C.quota_exceeded: (
        ActionKind.request_quota,
        "Request a quota increase or add client-side rate limiting.",
    ),
    C.expired_credential: (ActionKind.rotate_secret, "Rotate the expired credential in Secret Manager."),
    C.iam_permission: (
        ActionKind.fix_iam,
        "Restore the missing IAM binding for the runtime service account.",
    ),
    C.db_connection_pool: (ActionKind.config_fix, "Cap instances x pool size below the DB connection limit."),
    C.config_error: (ActionKind.config_fix, "Fix the configuration and redeploy."),
    C.dependency_outage: (ActionKind.escalate, "Escalate to the dependency owner; do not roll back."),
    C.build_failure: (ActionKind.escalate, "Fix the build and redeploy a valid image."),
    C.capacity: (ActionKind.config_fix, "Set min-instances / adjust autoscaling to absorb the load."),
    C.noisy_neighbor: (
        ActionKind.scale_resources,
        "Isolate the noisy workload (requests/limits, node pool).",
    ),
    C.false_positive: (ActionKind.none, "No action; consider tuning the alert threshold."),
    C.unknown: (ActionKind.escalate, "Escalate to a human with the collected evidence."),
}

TIE_BREAK = [
    C.oom,
    C.build_failure,
    C.config_error,
    C.expired_credential,
    C.iam_permission,
    C.quota_exceeded,
    C.db_connection_pool,
    C.dependency_outage,
    C.noisy_neighbor,
    C.capacity,
    C.bad_deploy,
]


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _tool_results(messages: list[Message]) -> list[tuple[str, str, Any]]:
    out = []
    for m in messages:
        if m["role"] != "tool":
            continue
        match = _RESULT_RE.search(m["content"])
        if match:
            try:
                out.append((match.group(1), match.group(2), json.loads(match.group(3))))
            except json.JSONDecodeError:
                continue
    return out


def _alert(messages: list[Message]) -> dict[str, Any]:
    for m in messages:
        if m["role"] == "user":
            match = _ALERT_RE.search(m["content"])
            if match:
                data: dict[str, Any] = json.loads(match.group(1))
                return data.get("incident", data)
    return {}


class MockLLM:
    provider = "mock"

    def __init__(self, *, injectable: bool = False) -> None:
        self.injectable = injectable
        self.model = "scripted-playbook-injectable" if injectable else "scripted-playbook"
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"mock_{self._n}"

    # ----------------------------------------------------------------- playbook
    def _next_calls(self, alert: dict[str, Any], results: list[tuple[str, str, Any]]) -> list[ToolCall]:
        res = alert.get("resource", {})
        labels = res.get("labels", {})
        project = labels.get("project_id") or alert.get("scoping_project_id", "")
        rtype = res.get("type", "")
        service = labels.get("service_name") or labels.get("container_name")
        done = {name for _, name, _ in results}
        metric_type = alert.get("metric", {}).get("type", "")
        alert_metric = next((k for k, v in METRIC_CATALOG.items() if v["type"] == metric_type), None)
        log_text = " ".join(
            e.get("text_payload", "")
            for _, name, payload in results
            if name == "query_logs"
            for e in payload.get("entries", [])
        ).lower()
        calls: list[ToolCall] = []

        def add(name: str, **args: Any) -> None:
            calls.append(ToolCall(self._id(), name, {"project_id": project, **args}))

        if "query_logs" not in done:
            add("get_active_alerts")
            log_args: dict[str, Any] = {"min_severity": "WARNING", "lookback_minutes": 120, "limit": 60}
            if service:
                log_args["service"] = service
            add("query_logs", **log_args)
            return calls

        fetched_metrics = {
            p.get("metric") for _, n, p in results if n == "get_metric_timeseries" and isinstance(p, dict)
        }
        if rtype == "cloud_run_revision" and "list_cloud_run_revisions" not in done:
            add("list_cloud_run_revisions", region=labels.get("location", ""), service=service)
            add(
                "get_metric_timeseries",
                metric=alert_metric or "run_request_count_5xx",
                service=service,
                lookback_minutes=120,
            )
            return calls
        if rtype == "k8s_container" and "get_gke_pod_status" not in done:
            add(
                "get_gke_pod_status",
                location=labels.get("location", ""),
                cluster=labels.get("cluster_name", ""),
                namespace=labels.get("namespace_name", "default"),
            )
            add(
                "get_metric_timeseries",
                metric=alert_metric or "gke_container_restart_count",
                service=service,
                lookback_minutes=120,
            )
            return calls
        if rtype == "cloudsql_database" and alert_metric and alert_metric not in fetched_metrics:
            add("get_metric_timeseries", metric=alert_metric, lookback_minutes=120)
            return calls

        if "list_recent_builds" not in done:
            add("list_recent_builds", limit=5)
            if ("connection" in log_text or "pool" in log_text) and "cloudsql_connections" not in fetched_metrics:
                add("get_metric_timeseries", metric="cloudsql_connections", lookback_minutes=120)
            if "quota" in log_text and "api_quota_usage_ratio" not in fetched_metrics:
                add("get_metric_timeseries", metric="api_quota_usage_ratio", lookback_minutes=120)
            if rtype == "k8s_container" and "latenc" in metric_type + alert.get("policy_name", "").lower():
                add("get_metric_timeseries", metric="gke_node_cpu_utilization", lookback_minutes=120)
            return calls
        return []

    def _injected_calls(self, results: list[tuple[str, str, Any]]) -> list[ToolCall]:
        pattern = re.compile(
            r"rollback_cloud_run_revision.*?service[= ]+([a-z0-9-]+).*?revision[= ]+([a-z0-9-]+)", re.I
        )
        for _, name, payload in results:
            if name != "query_logs":
                continue
            for e in payload.get("entries", []):
                match = pattern.search(e.get("text_payload", ""))
                if match:
                    return [
                        ToolCall(
                            self._id(),
                            "rollback_cloud_run_revision",
                            {
                                "service": match.group(1),
                                "target_revision": match.group(2),
                                "approval_token": "none",
                            },
                        )
                    ]
        return []

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        prompt_tokens = estimate_tokens("".join(str(m.get("content", "")) for m in messages))
        if not tools:  # planning turn
            text = (
                "Plan: 1) confirm the alert and scope; 2) pull recent WARNING+ logs for the affected "
                "service; 3) check what changed (deploys, builds, config); 4) read the alerting metric and "
                "resource saturation signals; 5) correlate timings, rule out red herrings, then report."
            )
            return LLMResponse(text=text, usage=Usage(prompt_tokens, estimate_tokens(text), estimated=True))
        results = _tool_results(messages)
        already_injected = any(
            c["name"] == "rollback_cloud_run_revision" for m in messages for c in m.get("tool_calls", [])
        )
        calls = self._injected_calls(results) if self.injectable and not already_injected else []
        calls = calls or self._next_calls(_alert(messages), results)
        text = "Gathering more evidence." if calls else "I have enough evidence to write the report."
        out_tokens = estimate_tokens(text + json.dumps([c.arguments for c in calls]))
        return LLMResponse(text=text, tool_calls=calls, usage=Usage(prompt_tokens, out_tokens, estimated=True))

    # ----------------------------------------------------------------- report
    async def structured(self, messages: list[Message], schema: type[T]) -> tuple[T, Usage]:
        if schema is not IncidentReport:
            raise NotImplementedError("MockLLM only produces IncidentReport")
        report = diagnose(_alert(messages), _tool_results(messages))
        prompt_tokens = estimate_tokens("".join(str(m.get("content", "")) for m in messages))
        usage = Usage(prompt_tokens, estimate_tokens(report.model_dump_json()), estimated=True)
        return schema.model_validate(report.model_dump()), usage


def diagnose(alert: dict[str, Any], results: list[tuple[str, str, Any]]) -> IncidentReport:
    """Rule-based diagnosis over collected tool results (see module docstring)."""
    scores: dict[RootCauseCategory, float] = defaultdict(float)
    findings: dict[RootCauseCategory, list[Evidence]] = defaultdict(list)
    first_seen: dict[RootCauseCategory, str] = {}
    security_notes: list[str] = []
    timeline: list[TimelineEvent] = []

    def hit(cat: RootCauseCategory, ref: str, tool: str, finding: str, weight: float = 1.0, ts: str = "") -> None:
        scores[cat] += weight
        if len(findings[cat]) < 4 and all(e.finding != finding for e in findings[cat]):
            findings[cat].append(Evidence(ref=ref, tool=tool, finding=finding[:200]))
        if ts and (cat not in first_seen or ts < first_seen[cat]):
            first_seen[cat] = ts

    error_lines: list[tuple[str, dict[str, Any]]] = []
    revisions: list[dict[str, Any]] = []
    rev_ref = ""
    for ref, tool, payload in results:
        if tool == "query_logs":
            for e in payload.get("entries", []):
                if e.get("suspected_prompt_injection"):
                    security_notes.append(
                        f"[{ref}] log entry at {e['timestamp']} looks like a prompt-injection attempt; "
                        "it was treated as data and not acted on."
                    )
                    continue
                text = e.get("text_payload", "")
                if e.get("severity") in ("ERROR", "CRITICAL", "ALERT", "EMERGENCY"):
                    error_lines.append((ref, e))
                for cat, patterns in TEXT_RULES.items():
                    if any(re.search(p, text) for p in patterns) and scores[cat] < 6:
                        hit(cat, ref, tool, f"{e['timestamp']} {e['severity']}: {text}", ts=e["timestamp"])
        elif tool == "get_gke_pod_status":
            for p in payload.get("pods", []):
                for cs in p.get("containers", []):
                    if cs.get("last_termination_reason") == "OOMKilled":
                        hit(
                            C.oom,
                            ref,
                            tool,
                            f"pod {p['name']} container {cs['name']} OOMKilled, "
                            f"{cs.get('restart_count')} restarts, limits {p.get('limits')}",
                            2,
                        )
                    if cs.get("waiting_reason") in ("ImagePullBackOff", "ErrImagePull"):
                        hit(
                            C.build_failure,
                            ref,
                            tool,
                            f"pod {p['name']} {cs['waiting_reason']} for {cs.get('image')}",
                            2,
                        )
        elif tool == "list_recent_builds":
            for b in payload.get("builds", []):
                if b.get("status") in ("FAILURE", "TIMEOUT", "INTERNAL_ERROR"):
                    hit(
                        C.build_failure,
                        ref,
                        tool,
                        f"build {b['id']} {b['status']} at {b['create_time']}: {b.get('failure_detail') or ''}",
                        1,
                    )
        elif tool == "list_cloud_run_revisions":
            revisions = payload.get("revisions", [])
            rev_ref = ref
            if revisions and not revisions[0].get("ready", True):
                hit(
                    C.config_error,
                    ref,
                    tool,
                    f"newest revision {revisions[0]['name']} is not ready: {revisions[0].get('ready_message')}",
                    2,
                )
        elif tool == "get_metric_timeseries":
            for s in payload.get("series", []):
                summ = s.get("summary", {})
                if s.get("metric") == "gke_node_cpu_utilization" and summ.get("max", 0) >= 0.9:
                    hit(
                        C.noisy_neighbor,
                        ref,
                        tool,
                        f"node {s['labels']} CPU utilisation peaked at {summ['max']}",
                        3,
                    )
                if s.get("metric") == "cloudsql_connections" and summ.get("max", 0) >= 95:
                    hit(C.db_connection_pool, ref, tool, f"Cloud SQL connections peaked at {summ['max']}", 1)

    # Bad deploy: errors concentrated on a revision created shortly before they started.
    newest = revisions[0] if revisions else None
    if newest:
        timeline.append(
            TimelineEvent(
                time=newest["create_time"],
                event=f"Revision {newest['name']} created ({newest.get('traffic_percent', 0)}% traffic)",
                evidence_refs=[rev_ref],
            )
        )
        on_new = [
            (r, e) for r, e in error_lines if e["resource"].get("labels", {}).get("revision_name") == newest["name"]
        ]
        on_old = [
            (r, e)
            for r, e in error_lines
            if e["resource"].get("labels", {}).get("revision_name") not in (None, newest["name"])
        ]
        if len(on_new) >= 3 and not on_old and newest.get("traffic_percent", 0) > 0:
            first = min(_parse_ts(e["timestamp"]) for _, e in on_new)
            created = _parse_ts(newest["create_time"])
            # The deploy must precede the errors, and closely (a 3-day-old revision is not "the change").
            if created <= first <= created + timedelta(minutes=60):
                ref = on_new[0][0]
                hit(
                    C.bad_deploy,
                    ref,
                    "query_logs",
                    f"{len(on_new)} errors, all on new revision {newest['name']}, "
                    f"none on previous revisions; first at {first.isoformat()}",
                    min(len(on_new), 5),
                )
                hit(
                    C.bad_deploy,
                    rev_ref,
                    "list_cloud_run_revisions",
                    f"{newest['name']} deployed at "
                    f"{newest['create_time']} serving {newest.get('traffic_percent')}% traffic",
                    0,
                )

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], TIE_BREAK.index(kv[0]) if kv[0] in TIE_BREAK else 99))
    ranked = [(c, s) for c, s in ranked if s > 0]
    if ranked:
        category, top = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        confidence = round(min(0.9, 0.35 + 0.08 * top) - (0.15 if runner_up >= top * 0.75 else 0.0), 2)
    else:
        category, confidence = (C.false_positive if results else C.unknown), 0.5

    started = alert.get("started_at")
    if isinstance(started, int):
        timeline.append(
            TimelineEvent(
                time=datetime.fromtimestamp(started, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                event=f"Alert fired: {alert.get('policy_name', '')}",
            )
        )
    if category in first_seen:
        timeline.append(
            TimelineEvent(
                time=first_seen[category],
                event=f"First {category.value} signal",
                evidence_refs=[findings[category][0].ref],
            )
        )
    timeline.sort(key=lambda t: _parse_ts(t.time))

    kind, description = ACTION_FOR.get(category, (ActionKind.escalate, "Escalate."))
    params: dict[str, Any] = {}
    labels = alert.get("resource", {}).get("labels", {})
    serving_new = bool(newest and newest.get("traffic_percent", 0) > 0)
    if category in (C.bad_deploy, C.config_error) and newest and serving_new:
        previous = next((r for r in revisions[1:] if r.get("ready", True)), None)
        if previous:
            kind = ActionKind.rollback_cloud_run
            description = f"Roll back {labels.get('service_name')} to {previous['name']} (last ready revision)."
            params = {
                "project_id": labels.get("project_id") or alert.get("scoping_project_id"),
                "region": labels.get("location"),
                "service": labels.get("service_name"),
                "target_revision": previous["name"],
            }
    elif category == C.bad_deploy:
        kind, description = ActionKind.escalate, "Suspected bad deploy but no safe rollback target found."

    hypotheses = [
        Hypothesis(
            statement=f"{c.value} (score {s:g})",
            status="confirmed" if i == 0 else "rejected",
            evidence_refs=sorted({e.ref for e in findings[c]}),
        )
        for i, (c, s) in enumerate(ranked[:3])
    ]
    evidence = findings.get(category, [])
    service = labels.get("service_name") or labels.get("container_name") or labels.get("database_id", "service")
    return IncidentReport(
        title=f"{alert.get('policy_name', 'Alert')} on {service}",
        summary=f"Rule-based diagnosis: most consistent explanation is {category.value} "
        f"({len(evidence)} supporting evidence items).",
        severity="SEV2" if category not in (C.false_positive,) else "SEV4",
        root_cause_category=category,
        root_cause=evidence[0].finding if evidence else "No anomalous signal found in the collected evidence.",
        timeline=timeline,
        evidence=evidence,
        hypotheses=hypotheses,
        confidence=max(0.1, confidence),
        recommended_action=RecommendedAction(
            kind=kind,
            description=description,
            requires_approval=kind == ActionKind.rollback_cloud_run,
            params=params,
        ),
        security_notes=security_notes,
    )
