"""Real Google Cloud backend.

Uses Application Default Credentials only (``gcloud auth application-default login`` locally,
the attached service account / Workload Identity on Cloud Run or GKE). No key files.
Required permissions are listed in ``docs/iam.md``.

Client libraries are optional dependencies (``pip install .[gcp]``) and are imported lazily
so the package works offline without them.

Note: this backend is exercised by unit tests only through its query-building helpers; the
API calls themselves need a real project and are not run in CI.
"""

from __future__ import annotations

import base64
import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from oncall_agent.mcp_server.backends.base import METRIC_CATALOG, BackendError


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(ts)


def build_log_filter(
    *,
    resource_type: str,
    service: str | None,
    min_severity: str,
    contains: str,
    lookback_minutes: int,
    now: datetime | None = None,
) -> str:
    """Build a Cloud Logging filter from *validated* structured arguments.

    The LLM never writes raw filter syntax; arguments are regex-validated at the MCP layer
    (no quotes or operators can reach this function), which prevents filter injection.
    """
    now = now or datetime.now(UTC)
    since = (now - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f'timestamp >= "{since}"', f"severity >= {min_severity}"]
    if resource_type != "any":
        parts.append(f'resource.type = "{resource_type}"')
    if service:
        parts.append(
            f'(resource.labels.service_name = "{service}" OR resource.labels.container_name = "{service}"'
            f' OR labels."k8s-pod/app" = "{service}")'
        )
    if contains:
        parts.append(f'"{contains}"')
    return " AND ".join(parts)


def build_metric_filter(metric: str, service: str | None) -> str:
    spec = METRIC_CATALOG[metric]
    parts = [f'metric.type = "{spec["type"]}"']
    if "filter" in spec:
        parts.append(spec["filter"])
    if service:
        if spec["resource"] == "cloud_run_revision":
            parts.append(f'resource.labels.service_name = "{service}"')
        elif spec["resource"] == "k8s_container":
            parts.append(f'resource.labels.container_name = "{service}"')
        elif spec["resource"] == "cloudsql_database":
            parts.append(f'resource.labels.database_id = has_substring("{service}")')
    return " AND ".join(parts)


class GcpBackend:
    name = "gcp"

    # ------------------------------------------------------------------ alerts
    def list_active_alerts(self, project_id: str) -> list[dict[str, Any]]:
        # The public v3 API exposes alert *policies*; open incidents reach us via the
        # webhook / Pub/Sub notification channel. We return enabled policies so the agent
        # knows what is being watched.
        from google.cloud import monitoring_v3

        client = monitoring_v3.AlertPolicyServiceClient()
        out = []
        for p in client.list_alert_policies(name=f"projects/{project_id}"):
            if not p.enabled:
                continue
            out.append(
                {
                    "policy_name": p.display_name,
                    "conditions": [c.display_name for c in p.conditions],
                    "severity": getattr(p.severity, "name", None),
                }
            )
        return out[:50]

    # ------------------------------------------------------------------ logging
    def query_logs(
        self,
        project_id: str,
        *,
        resource_type: str,
        service: str | None,
        min_severity: str,
        contains: str,
        lookback_minutes: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        from google.cloud import logging_v2

        client = logging_v2.Client(project=project_id)
        flt = build_log_filter(
            resource_type=resource_type,
            service=service,
            min_severity=min_severity,
            contains=contains,
            lookback_minutes=lookback_minutes,
        )
        out = []
        for entry in client.list_entries(
            resource_names=[f"projects/{project_id}"],
            filter_=flt,
            order_by=logging_v2.DESCENDING,
            max_results=limit,
        ):
            payload = entry.payload
            if isinstance(payload, dict):
                text = payload.get("message") or str(payload)
            else:
                text = str(payload)
            out.append(
                {
                    "timestamp": _iso(entry.timestamp),
                    "severity": entry.severity,
                    "resource": {"type": entry.resource.type, "labels": dict(entry.resource.labels)},
                    "text_payload": text,
                }
            )
        return out

    # ------------------------------------------------------------------ monitoring
    def get_metric(
        self, project_id: str, *, metric: str, service: str | None, lookback_minutes: int
    ) -> list[dict[str, Any]]:
        from google.cloud import monitoring_v3

        spec = METRIC_CATALOG[metric]
        client = monitoring_v3.MetricServiceClient()
        now = int(time.time())
        interval = monitoring_v3.TimeInterval(
            {"end_time": {"seconds": now}, "start_time": {"seconds": now - lookback_minutes * 60}}
        )
        aggregation = monitoring_v3.Aggregation(
            {
                "alignment_period": {"seconds": 60},
                "per_series_aligner": monitoring_v3.Aggregation.Aligner[spec["aligner"]],
            }
        )
        results = client.list_time_series(
            request={
                "name": f"projects/{project_id}",
                "filter": build_metric_filter(metric, service),
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                "aggregation": aggregation,
            }
        )
        out = []
        for ts in list(results)[:10]:
            pts = []
            for p in reversed(ts.points):
                v = p.value
                value = v.double_value if v.double_value else float(v.int64_value)
                pts.append({"time": _iso(p.interval.end_time), "value": round(value, 4)})
            labels = {**dict(ts.resource.labels), **dict(ts.metric.labels)}
            out.append({"metric": metric, "labels": labels, "unit": ts.unit, "points": pts})
        return out

    # ------------------------------------------------------------------ cloud run
    def list_cloud_run_revisions(self, project_id: str, *, region: str, service: str, limit: int) -> dict[str, Any]:
        from google.cloud import run_v2

        svc_name = f"projects/{project_id}/locations/{region}/services/{service}"
        svc = run_v2.ServicesClient().get_service(name=svc_name)
        traffic = {t.revision: t.percent for t in svc.traffic_statuses if t.revision}
        if not traffic and svc.latest_ready_revision:
            traffic = {svc.latest_ready_revision.rsplit("/", 1)[-1]: 100}
        revs = []
        for r in run_v2.RevisionsClient().list_revisions(parent=svc_name):
            short = r.name.rsplit("/", 1)[-1]
            ready = next((c for c in r.conditions if c.type_ == "Ready"), None)
            container = r.containers[0] if r.containers else None
            revs.append(
                {
                    "name": short,
                    "create_time": _iso(r.create_time),
                    "image": container.image if container else None,
                    "traffic_percent": traffic.get(short, 0),
                    "ready": bool(ready and ready.state.name == "CONDITION_SUCCEEDED"),
                    "ready_message": ready.message if ready else None,
                    "config": {
                        "max_instances": r.scaling.max_instance_count,
                        "min_instances": r.scaling.min_instance_count,
                        "memory": container.resources.limits.get("memory") if container else None,
                        # Env var *names* only: values may be secrets.
                        "env_names": sorted(e.name for e in container.env) if container else [],
                    },
                }
            )
        revs.sort(key=lambda r: r["create_time"] or "", reverse=True)
        return {"service": service, "region": region, "url": svc.uri, "revisions": revs[:limit]}

    def rollback_cloud_run(self, project_id: str, *, region: str, service: str, target_revision: str) -> dict[str, Any]:
        from google.cloud import run_v2

        client = run_v2.ServicesClient()
        svc_name = f"projects/{project_id}/locations/{region}/services/{service}"
        svc = client.get_service(name=svc_name)
        svc.traffic = [
            run_v2.TrafficTarget(
                type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
                revision=target_revision,
                percent=100,
            )
        ]
        op = client.update_service(service=svc)
        result = op.result(timeout=300)
        return {
            "status": "ROLLED_BACK",
            "service": service,
            "region": region,
            "traffic": {t.revision: t.percent for t in result.traffic_statuses},
            "simulated": False,
        }

    # ------------------------------------------------------------------ gke
    def get_gke_pods(self, project_id: str, *, location: str, cluster: str, namespace: str) -> list[dict[str, Any]]:
        import google.auth
        import google.auth.transport.requests
        from google.cloud import container_v1
        from kubernetes import client as k8s

        c = container_v1.ClusterManagerClient().get_cluster(
            name=f"projects/{project_id}/locations/{location}/clusters/{cluster}"
        )
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())

        fd, ca_path = tempfile.mkstemp(suffix=".crt")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(c.master_auth.cluster_ca_certificate))
            cfg = k8s.Configuration()
            cfg.host = f"https://{c.endpoint}"
            cfg.ssl_ca_cert = ca_path
            cfg.api_key = {"authorization": f"Bearer {creds.token}"}
            api = k8s.CoreV1Api(k8s.ApiClient(cfg))
            pods = api.list_namespaced_pod(namespace=namespace, limit=100)
        except Exception as exc:
            raise BackendError(f"failed to list pods: {type(exc).__name__}") from exc
        finally:
            os.unlink(ca_path)

        out = []
        for p in pods.items:
            statuses = []
            for cs in p.status.container_statuses or []:
                term = cs.last_state.terminated if cs.last_state else None
                waiting = cs.state.waiting if cs.state else None
                statuses.append(
                    {
                        "name": cs.name,
                        "ready": cs.ready,
                        "restart_count": cs.restart_count,
                        "image": cs.image,
                        "state": "waiting" if waiting else ("running" if cs.state.running else "terminated"),
                        "waiting_reason": waiting.reason if waiting else None,
                        "last_termination_reason": term.reason if term else None,
                        "last_exit_code": term.exit_code if term else None,
                    }
                )
            limits = {}
            for ctr in p.spec.containers:
                if ctr.resources and ctr.resources.limits:
                    limits[ctr.name] = dict(ctr.resources.limits)
            out.append(
                {
                    "name": p.metadata.name,
                    "namespace": namespace,
                    "phase": p.status.phase,
                    "node": p.spec.node_name,
                    "labels": dict(p.metadata.labels or {}),
                    "start_time": _iso(p.status.start_time),
                    "containers": statuses,
                    "limits": limits,
                }
            )
        return out

    # ------------------------------------------------------------------ cloud build
    def list_builds(self, project_id: str, *, limit: int) -> list[dict[str, Any]]:
        from google.cloud.devtools import cloudbuild_v1

        client = cloudbuild_v1.CloudBuildClient()
        out = []
        for b in client.list_builds(project_id=project_id, page_size=limit):
            out.append(
                {
                    "id": b.id,
                    "status": b.status.name,
                    "create_time": _iso(b.create_time),
                    "finish_time": _iso(b.finish_time),
                    "images": list(b.images),
                    "trigger_id": b.build_trigger_id,
                    "substitutions": {
                        k: v for k, v in b.substitutions.items() if k in ("BRANCH_NAME", "SHORT_SHA", "REPO_NAME")
                    },
                    "failure_detail": b.failure_info.detail if b.failure_info else None,
                }
            )
            if len(out) >= limit:
                break
        return out
