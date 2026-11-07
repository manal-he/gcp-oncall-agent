import asyncio
import base64
import json

import httpx
import pytest
from pydantic import SecretStr

from oncall_agent.api.app import create_app


async def _wait_status(client, inv_id, wanted, wait_s=20.0):
    snap = {}
    for _ in range(int(wait_s / 0.05)):
        snap = (await client.get(f"/api/investigations/{inv_id}")).json()
        if snap["status"] in wanted:
            return snap
        await asyncio.sleep(0.05)
    raise AssertionError(f"status never reached {wanted}: {snap.get('status')}")


@pytest.fixture
async def client_factory(settings):
    apps = []

    def make(**overrides):
        app = create_app(settings.model_copy(update=overrides))
        apps.append(app)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    yield make
    for app in apps:  # ASGITransport does not run lifespan; stop background investigations ourselves
        await app.state.manager.aclose()


async def test_full_flow_with_approval(client_factory):
    async with client_factory() as c:
        assert (await c.get("/healthz")).json()["status"] == "ok"
        assert len((await c.get("/api/scenarios")).json()) >= 10
        inv_id = (await c.post("/api/investigations", json={"scenario": "bad-deploy"})).json()["id"]
        snap = await _wait_status(c, inv_id, {"awaiting_approval"})
        assert snap["report"]["recommended_action"]["kind"] == "rollback_cloud_run"
        r = await c.post(
            f"/api/investigations/{inv_id}/decision",
            json={"approved": True},
            headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:oncall@example.com"},
        )
        assert r.status_code == 200
        snap = await _wait_status(c, inv_id, {"completed", "failed"})
        assert snap["status"] == "completed"
        assert snap["approval"]["approver"] == "oncall@example.com"
        assert snap["action_result"]["ok"]
        # SSE replays the whole trace and terminates once the investigation is done
        events = (await c.get(f"/api/investigations/{inv_id}/events")).text
        assert "event: action_executed" in events and "event: end" in events


async def test_decision_conflict(client_factory):
    async with client_factory() as c:
        inv_id = (await c.post("/api/investigations", json={"scenario": "oom-killed"})).json()["id"]
        await _wait_status(c, inv_id, {"completed"})
        r = await c.post(f"/api/investigations/{inv_id}/decision", json={"approved": True})
        assert r.status_code == 409


async def test_invalid_scenario_rejected(client_factory):
    async with client_factory() as c:
        r = await c.post("/api/investigations", json={"scenario": "../etc/passwd"})
        assert r.status_code == 422


async def test_cloud_monitoring_webhook_auth(client_factory):
    body = {"version": "1.2", "incident": {"incident_id": "abc", "state": "open"}}
    auth = {"Authorization": "Bearer test-token"}
    async with client_factory(webhook_token=SecretStr("test-token")) as c:
        assert (await c.post("/webhooks/cloud-monitoring", json=body)).status_code == 401
        assert (await c.post("/webhooks/cloud-monitoring?token=wrong", json=body)).status_code == 401
        r = await c.post("/webhooks/cloud-monitoring?scenario=oom-killed", json=body, headers=auth)
        assert r.status_code == 202
        # a repeated notification for the same incident is de-duplicated
        r2 = await c.post("/webhooks/cloud-monitoring?scenario=oom-killed", json=body, headers=auth)
        assert r2.json()["id"] == r.json()["id"]
        closed = {"incident": {"incident_id": "abc", "state": "closed"}}
        r3 = await c.post("/webhooks/cloud-monitoring", json=closed, headers=auth)
        assert r3.json()["status"] == "ignored"


async def test_gcp_backend_requires_webhook_token(client_factory):
    async with client_factory(backend="gcp") as c:
        r = await c.post("/webhooks/cloud-monitoring", json={"incident": {}})
        assert r.status_code == 503


async def test_pubsub_push(client_factory):
    data = base64.b64encode(json.dumps({"incident": {"incident_id": "ps-1", "state": "open"}}).encode()).decode()
    async with client_factory() as c:
        r = await c.post("/webhooks/pubsub", json={"message": {"data": data, "messageId": "1"}, "subscription": "s"})
        assert r.status_code == 202 and "id" in r.json()
        bad = await c.post("/webhooks/pubsub", json={"message": {}})
        assert bad.status_code == 422


async def test_gcp_backend_decision_requires_iap_identity(settings):
    from oncall_agent.agent.runner import Investigation

    app = create_app(settings.model_copy(update={"backend": "gcp"}))
    inv = Investigation(None, {"incident": {"incident_id": "x"}})
    app.state.manager.items[inv.id] = inv
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(f"/api/investigations/{inv.id}/decision", json={"approved": True, "approver": "me"})
        assert r.status_code == 403
