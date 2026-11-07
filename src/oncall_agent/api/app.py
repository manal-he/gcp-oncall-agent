"""FastAPI service: alert webhooks in, live investigation trace and approvals out.

Endpoints
    POST /webhooks/cloud-monitoring   Cloud Monitoring webhook notification channel (token auth)
    POST /webhooks/pubsub             Pub/Sub push subscription (OIDC-verified when configured)
    POST /api/investigations          start one manually (UI / demo)
    GET  /api/investigations[/{id}]   snapshots
    GET  /api/investigations/{id}/events   Server-Sent Events stream of the trace
    POST /api/investigations/{id}/decision approve / reject the pending write action
    GET  /api/scenarios, /healthz

Production notes: put the UI/API behind Identity-Aware Proxy; the approver identity is then
taken from the IAP-signed ``X-Goog-Authenticated-User-Email`` header rather than the body.
State is in memory (one instance); see README "Limitations".
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from oncall_agent import __version__
from oncall_agent.agent.runner import Investigation
from oncall_agent.config import Settings, get_settings
from oncall_agent.telemetry import setup_tracing

MAX_INVESTIGATIONS = 50
APPROVAL_TIMEOUT_S = 3600


class StartRequest(BaseModel):
    scenario: str = Field(default="bad-deploy", pattern=r"^[a-z0-9-]{1,40}$")


class Decision(BaseModel):
    approved: bool
    approver: str = Field(default="ui-user", max_length=100)
    comment: str = Field(default="", max_length=500)


class InvestigationManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.items: dict[str, Investigation] = {}
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.by_incident: dict[str, str] = {}

    def _prune(self) -> None:
        done = [i for i, inv in self.items.items() if inv.status in ("completed", "failed")]
        while len(self.items) >= MAX_INVESTIGATIONS and done:
            self.items.pop(done.pop(0), None)

    async def aclose(self) -> None:
        """Cancel in-flight investigations (graceful shutdown, e.g. Cloud Run SIGTERM)."""
        for task in self.tasks.values():
            task.cancel()
        if self.tasks:
            await asyncio.wait(list(self.tasks.values()), timeout=8)

    def start(
        self, scenario: str | None, alert: dict[str, Any] | None = None, incident_id: str | None = None
    ) -> Investigation:
        incident_id = incident_id or ((alert or {}).get("incident") or {}).get("incident_id")
        if incident_id and incident_id in self.by_incident:  # Monitoring retries / repeats
            existing = self.items.get(self.by_incident[incident_id])
            if existing and existing.status not in ("failed",):
                return existing
        self._prune()
        if len(self.items) >= MAX_INVESTIGATIONS:
            raise HTTPException(429, "too many investigations in progress")
        inv = Investigation(scenario, alert)
        self.items[inv.id] = inv
        if incident_id:
            self.by_incident[incident_id] = inv.id

        async def approver(request: dict[str, Any]) -> dict[str, Any]:
            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self.pending[inv.id] = fut
            try:
                return await asyncio.wait_for(fut, APPROVAL_TIMEOUT_S)
            except TimeoutError:
                return {
                    "approved": False,
                    "approver": "policy:timeout",
                    "comment": "no decision within timeout",
                }
            finally:
                self.pending.pop(inv.id, None)

        async def run() -> None:
            with contextlib.suppress(Exception):  # failure is recorded on the investigation
                await inv.run(self.settings, approver)

        self.tasks[inv.id] = asyncio.create_task(run())
        return inv

    def decide(self, inv_id: str, decision: dict[str, Any]) -> None:
        fut = self.pending.get(inv_id)
        if fut is None or fut.done():
            raise HTTPException(409, "investigation is not awaiting approval")
        fut.set_result(decision)


def _check_bearer(expected: str | None, authorization: str | None, token: str | None) -> None:
    supplied = token or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None)
    if expected is None:
        return
    if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "invalid webhook token")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_tracing(settings)
    manager = InvestigationManager(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await manager.aclose()

    app = FastAPI(title="gcp-oncall-agent", version=__version__, lifespan=lifespan)
    app.state.manager = manager
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )

    def require_webhook_auth(authorization: str | None, token: str | None) -> None:
        secret = settings.webhook_token.get_secret_value() if settings.webhook_token else None
        if secret is None and settings.backend == "gcp":
            raise HTTPException(503, "ONCALL_WEBHOOK_TOKEN must be configured for the gcp backend")
        _check_bearer(secret, authorization, token)

    def get_inv(inv_id: str) -> Investigation:
        inv = manager.items.get(inv_id)
        if inv is None:
            raise HTTPException(404, "investigation not found")
        return inv

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "backend": settings.backend,
            "llm": settings.llm_provider,
        }

    @app.get("/api/scenarios")
    def scenarios() -> list[dict[str, str]]:
        from oncall_agent.mcp_server.backends.simulated import list_scenarios

        return list_scenarios() if settings.backend == "simulated" else []

    @app.post("/api/investigations", status_code=202)
    async def start(req: StartRequest) -> dict[str, Any]:
        if settings.backend != "simulated":
            raise HTTPException(400, "manual start is only available with the simulated backend; use the webhooks")
        inv = manager.start(req.scenario)
        return {"id": inv.id, "status": inv.status}

    @app.get("/api/investigations")
    def list_investigations() -> list[dict[str, Any]]:
        return [
            {"id": i.id, "scenario": i.scenario, "status": i.status, "created_at": i.created_at}
            for i in reversed(manager.items.values())
        ]

    @app.get("/api/investigations/{inv_id}")
    def get_investigation(inv_id: str) -> dict[str, Any]:
        return get_inv(inv_id).snapshot()

    @app.get("/api/investigations/{inv_id}/events")
    async def events(inv_id: str, since: int = 0) -> StreamingResponse:
        inv = get_inv(inv_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def stream() -> AsyncIterator[str]:
            inv.listeners.append(queue.put_nowait)
            try:
                for e in inv.events[since:]:
                    yield f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e, default=str)}\n\n"
                last = len(inv.events) - 1
                while inv.status not in ("completed", "failed") or not queue.empty():
                    try:
                        e = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if e["seq"] <= last:
                        continue
                    last = e["seq"]
                    yield f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e, default=str)}\n\n"
                yield "event: end\ndata: {}\n\n"
            finally:
                with contextlib.suppress(ValueError):
                    inv.listeners.remove(queue.put_nowait)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/investigations/{inv_id}/decision")
    def decide(
        inv_id: str,
        decision: Decision,
        iap_user: str | None = Header(default=None, alias="X-Goog-Authenticated-User-Email"),
    ) -> dict[str, Any]:
        get_inv(inv_id)
        if settings.backend == "gcp" and not iap_user:
            # Fail closed: on the real backend an approval must carry an IAP-asserted identity.
            # The body's `approver` field is self-declared and only accepted for the simulated demo.
            raise HTTPException(403, "approvals on the gcp backend require an IAP-authenticated identity")
        approver = iap_user.split(":", 1)[-1] if iap_user else decision.approver
        manager.decide(inv_id, {"approved": decision.approved, "approver": approver, "comment": decision.comment})
        return {"ok": True}

    @app.post("/webhooks/cloud-monitoring", status_code=202)
    async def cloud_monitoring(
        request: Request,
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
        scenario: str | None = Query(default=None, pattern=r"^[a-z0-9-]{1,40}$"),
    ) -> dict[str, Any]:
        require_webhook_auth(authorization, token)
        payload = await request.json()
        if not isinstance(payload, dict) or "incident" not in payload:
            raise HTTPException(422, "not a Cloud Monitoring notification")
        if payload["incident"].get("state") == "closed":
            return {"status": "ignored", "reason": "incident closed"}
        inv = _start_from_alert(payload, scenario)
        return {"id": inv.id, "status": inv.status}

    @app.post("/webhooks/pubsub", status_code=202)
    async def pubsub(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if settings.pubsub_audience:
            _verify_pubsub_oidc(authorization, settings.pubsub_audience)
        elif settings.backend == "gcp":
            raise HTTPException(503, "ONCALL_PUBSUB_AUDIENCE must be configured for the gcp backend")
        body = await request.json()
        try:
            data = json.loads(base64.b64decode(body["message"]["data"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(422, "invalid Pub/Sub push body") from exc
        if not isinstance(data, dict) or "incident" not in data:
            return {"status": "ignored"}  # ack non-alert messages so they are not redelivered
        inv = _start_from_alert(data, None)
        return {"id": inv.id, "status": inv.status}

    def _start_from_alert(payload: dict[str, Any], scenario: str | None) -> Investigation:
        if settings.backend == "simulated":
            # Fixture data only matches the fixture's own alert, so the payload body is not used.
            incident_id = str((payload.get("incident") or {}).get("incident_id") or "") or None
            return manager.start(scenario or settings.scenario, incident_id=incident_id)
        return manager.start(None, payload)

    ui_dist = Path(os.environ.get("ONCALL_UI_DIST", Path(__file__).resolve().parents[3] / "ui" / "dist"))
    if ui_dist.is_dir():
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")
    return app


def _verify_pubsub_oidc(authorization: str | None, audience: str) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing OIDC token")
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(authorization[7:], google_requests.Request(), audience=audience)
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(503, "google-auth not installed (pip install .[gcp])") from exc
    except ValueError as exc:
        raise HTTPException(401, "invalid OIDC token") from exc
    if not claims.get("email_verified"):
        raise HTTPException(401, "unverified OIDC identity")
