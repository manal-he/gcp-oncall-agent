"""Backend contract shared by the real GCP backend and the simulated (fixture) backend."""

from __future__ import annotations

from typing import Any, Literal, Protocol

Severity = Literal["DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"]
SEVERITY_ORDER: dict[str, int] = {
    "DEFAULT": 0,
    "DEBUG": 100,
    "INFO": 200,
    "NOTICE": 300,
    "WARNING": 400,
    "ERROR": 500,
    "CRITICAL": 600,
    "ALERT": 700,
    "EMERGENCY": 800,
}

ResourceType = Literal["any", "cloud_run_revision", "k8s_container", "cloudsql_database", "build"]

# Friendly metric names -> Cloud Monitoring metric type (+ optional extra filter).
# The allowlist keeps the LLM from issuing arbitrary MQL/PromQL and bounds cost.
MetricName = Literal[
    "run_request_count_5xx",
    "run_request_latency_p99",
    "run_container_memory_utilization",
    "run_container_cpu_utilization",
    "run_instance_count",
    "gke_container_memory_used_bytes",
    "gke_container_restart_count",
    "gke_node_cpu_utilization",
    "cloudsql_connections",
    "api_quota_usage_ratio",
]

METRIC_CATALOG: dict[str, dict[str, str]] = {
    "run_request_count_5xx": {
        "type": "run.googleapis.com/request_count",
        "filter": 'metric.labels.response_code_class = "5xx"',
        "resource": "cloud_run_revision",
        "aligner": "ALIGN_RATE",
    },
    "run_request_latency_p99": {
        "type": "run.googleapis.com/request_latencies",
        "resource": "cloud_run_revision",
        "aligner": "ALIGN_PERCENTILE_99",
    },
    "run_container_memory_utilization": {
        "type": "run.googleapis.com/container/memory/utilizations",
        "resource": "cloud_run_revision",
        "aligner": "ALIGN_PERCENTILE_99",
    },
    "run_container_cpu_utilization": {
        "type": "run.googleapis.com/container/cpu/utilizations",
        "resource": "cloud_run_revision",
        "aligner": "ALIGN_PERCENTILE_99",
    },
    "run_instance_count": {
        "type": "run.googleapis.com/container/instance_count",
        "resource": "cloud_run_revision",
        "aligner": "ALIGN_MAX",
    },
    "gke_container_memory_used_bytes": {
        "type": "kubernetes.io/container/memory/used_bytes",
        "resource": "k8s_container",
        "aligner": "ALIGN_MAX",
    },
    "gke_container_restart_count": {
        "type": "kubernetes.io/container/restart_count",
        "resource": "k8s_container",
        "aligner": "ALIGN_DELTA",
    },
    "gke_node_cpu_utilization": {
        "type": "kubernetes.io/node/cpu/allocatable_utilization",
        "resource": "k8s_node",
        "aligner": "ALIGN_MEAN",
    },
    "cloudsql_connections": {
        "type": "cloudsql.googleapis.com/database/postgresql/num_backends",
        "resource": "cloudsql_database",
        "aligner": "ALIGN_MAX",
    },
    "api_quota_usage_ratio": {
        "type": "serviceruntime.googleapis.com/quota/rate/net_usage",
        "resource": "consumer_quota",
        "aligner": "ALIGN_RATE",
    },
}


class BackendError(Exception):
    """Expected, user-facing backend failure (not found, permission denied, ...)."""


class Backend(Protocol):
    name: str

    def list_active_alerts(self, project_id: str) -> list[dict[str, Any]]: ...

    def query_logs(
        self,
        project_id: str,
        *,
        resource_type: ResourceType,
        service: str | None,
        min_severity: str,
        contains: str,
        lookback_minutes: int,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def get_metric(
        self, project_id: str, *, metric: str, service: str | None, lookback_minutes: int
    ) -> list[dict[str, Any]]: ...

    def list_cloud_run_revisions(self, project_id: str, *, region: str, service: str, limit: int) -> dict[str, Any]: ...

    def get_gke_pods(self, project_id: str, *, location: str, cluster: str, namespace: str) -> list[dict[str, Any]]: ...

    def list_builds(self, project_id: str, *, limit: int) -> list[dict[str, Any]]: ...

    def rollback_cloud_run(
        self, project_id: str, *, region: str, service: str, target_revision: str
    ) -> dict[str, Any]: ...
