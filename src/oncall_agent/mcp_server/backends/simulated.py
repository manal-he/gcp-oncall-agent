"""Simulated backend: serves deterministic scenario fixtures.

Fixtures express times as minute offsets relative to the scenario's ``now`` (``"t": -12``),
which keeps them easy to author and review; the backend renders absolute RFC 3339
timestamps so tool output looks like the real APIs.

The fixtures contain only observable signals (logs, metrics, deploys...). Ground truth for
evaluation lives separately under ``evals/cases`` and is never visible to the agent.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

from oncall_agent.mcp_server.backends.base import SEVERITY_ORDER, BackendError

FIXTURE_PACKAGE = "oncall_agent.scenarios.fixtures"


def list_scenarios() -> list[dict[str, str]]:
    out = []
    for entry in sorted(resources.files(FIXTURE_PACKAGE).iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".json"):
            data = json.loads(entry.read_text(encoding="utf-8"))
            out.append({"name": data["name"], "title": data.get("title", data["name"])})
    return out


def load_fixture(name: str) -> dict[str, Any]:
    if not name.replace("-", "").isalnum():
        raise BackendError(f"invalid scenario name: {name!r}")
    path = resources.files(FIXTURE_PACKAGE).joinpath(f"{name}.json")
    if not path.is_file():
        raise BackendError(f"unknown scenario: {name!r}")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SimulatedBackend:
    name = "simulated"

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.fixture = load_fixture(scenario)
        self.now = datetime.fromisoformat(self.fixture["now"].replace("Z", "+00:00"))
        self.project_id: str = self.fixture["project_id"]
        self._run = copy.deepcopy(self.fixture.get("cloud_run", {}))
        self.write_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers
    def _at(self, offset_min: float) -> str:
        return _iso(self.now + timedelta(minutes=offset_min))

    def _check_project(self, project_id: str) -> None:
        if project_id != self.project_id:
            raise BackendError(f"project {project_id!r} not found or permission denied")

    @property
    def alert(self) -> dict[str, Any]:
        """The alert that opens this scenario, rendered like a Cloud Monitoring webhook."""
        alert = copy.deepcopy(self.fixture["alert"])
        inc = alert["incident"]
        inc["started_at"] = int((self.now + timedelta(minutes=inc.pop("t"))).timestamp())
        inc.setdefault("scoping_project_id", self.project_id)
        return alert

    # ------------------------------------------------------------------ tools
    def list_active_alerts(self, project_id: str) -> list[dict[str, Any]]:
        self._check_project(project_id)
        alerts = [self.alert["incident"], *self.fixture.get("other_alerts", [])]
        out = []
        for a in alerts:
            a = dict(a)
            if "t" in a:
                a["started_at"] = int((self.now + timedelta(minutes=a.pop("t"))).timestamp())
            out.append(a)
        return out

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
        self._check_project(project_id)
        floor = SEVERITY_ORDER.get(min_severity, 0)
        out = []
        for e in self.fixture.get("logs", []):
            if e["t"] < -lookback_minutes or e["t"] > 0:
                continue
            if SEVERITY_ORDER.get(e["severity"], 0) < floor:
                continue
            res = e.get("resource", {})
            if resource_type != "any" and res.get("type") != resource_type:
                continue
            labels = res.get("labels", {})
            if service and service not in (
                labels.get("service_name"),
                labels.get("container_name"),
                labels.get("app"),
                labels.get("database_id"),
            ):
                continue
            if contains and contains.lower() not in e["text"].lower():
                continue
            out.append(
                {
                    "timestamp": self._at(e["t"]),
                    "severity": e["severity"],
                    "resource": res,
                    "text_payload": e["text"],
                }
            )
        out.sort(key=lambda x: x["timestamp"], reverse=True)  # newest first, like the API
        return out[:limit]

    def get_metric(
        self, project_id: str, *, metric: str, service: str | None, lookback_minutes: int
    ) -> list[dict[str, Any]]:
        self._check_project(project_id)
        series = self.fixture.get("metrics", {}).get(metric, [])
        out = []
        for s in series:
            labels = s.get("labels", {})
            if service and service not in labels.values():
                continue
            pts = [{"time": self._at(t), "value": v} for t, v in s["points"] if -lookback_minutes <= t <= 0]
            out.append({"metric": metric, "labels": labels, "unit": s.get("unit", ""), "points": pts})
        return out

    def list_cloud_run_revisions(self, project_id: str, *, region: str, service: str, limit: int) -> dict[str, Any]:
        self._check_project(project_id)
        svc = self._run.get(service)
        if svc is None or svc.get("region") != region:
            raise BackendError(f"Cloud Run service {service!r} not found in {region}")
        revs = []
        for r in sorted(svc["revisions"], key=lambda r: r["t"], reverse=True)[:limit]:
            rev = {k: v for k, v in r.items() if k != "t"}
            rev["create_time"] = self._at(r["t"])
            revs.append(rev)
        return {"service": service, "region": region, "url": svc.get("url"), "revisions": revs}

    def get_gke_pods(self, project_id: str, *, location: str, cluster: str, namespace: str) -> list[dict[str, Any]]:
        self._check_project(project_id)
        for c in self.fixture.get("gke", {}).get("clusters", []):
            if c["name"] == cluster and c["location"] == location:
                pods = []
                for p in c.get("pods", []):
                    if p.get("namespace", "default") != namespace:
                        continue
                    pod = {k: v for k, v in p.items() if k != "t"}
                    if "t" in p:
                        pod["start_time"] = self._at(p["t"])
                    pods.append(pod)
                return pods
        raise BackendError(f"GKE cluster {cluster!r} not found in {location}")

    def list_builds(self, project_id: str, *, limit: int) -> list[dict[str, Any]]:
        self._check_project(project_id)
        out = []
        for b in sorted(self.fixture.get("builds", []), key=lambda b: b["t"], reverse=True)[:limit]:
            build = {k: v for k, v in b.items() if k != "t"}
            build["create_time"] = self._at(b["t"])
            out.append(build)
        return out

    def rollback_cloud_run(self, project_id: str, *, region: str, service: str, target_revision: str) -> dict[str, Any]:
        self._check_project(project_id)
        svc = self._run.get(service)
        if svc is None or svc.get("region") != region:
            raise BackendError(f"Cloud Run service {service!r} not found in {region}")
        names = {r["name"] for r in svc["revisions"]}
        if target_revision not in names:
            raise BackendError(f"revision {target_revision!r} does not belong to {service!r}")
        target = next(r for r in svc["revisions"] if r["name"] == target_revision)
        if not target.get("ready", True):
            raise BackendError(f"revision {target_revision!r} is not ready; refusing to route traffic")
        for r in svc["revisions"]:
            r["traffic_percent"] = 100 if r["name"] == target_revision else 0
        record = {
            "status": "ROLLED_BACK",
            "service": service,
            "region": region,
            "traffic": {target_revision: 100},
            "simulated": True,
        }
        self.write_log.append(record)
        return record
