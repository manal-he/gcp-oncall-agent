import json

import pytest
from mcp import Client

from oncall_agent.mcp_server.backends.simulated import SimulatedBackend, list_scenarios
from oncall_agent.mcp_server.server import READ_ONLY_TOOLS, WRITE_TOOLS, create_server
from oncall_agent.safety import approval_secret, issue_approval_token

P = "demo-project-123"
ROLLBACK = {
    "project_id": P,
    "region": "europe-west1",
    "service": "checkout",
    "target_revision": "checkout-00041-xit",
}


def payload(res):
    assert not res.is_error, res.content
    return json.loads(res.content[0].text)


async def test_lists_tools_with_annotations(settings):
    async with Client(create_server(SimulatedBackend("bad-deploy"), settings)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == set(READ_ONLY_TOOLS) | set(WRITE_TOOLS)
    for name in READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is True
    assert tools["rollback_cloud_run_revision"].annotations.destructive_hint is True


@pytest.mark.parametrize(
    "args",
    [
        {"project_id": "Bad_Project"},  # invalid id
        {"project_id": P, "contains": 'x" OR severity>=DEBUG'},  # filter injection attempt
        {"project_id": P, "lookback_minutes": 100000},  # out of bounds
        {"project_id": P, "min_severity": "EVERYTHING"},
    ],
)
async def test_argument_validation(settings, args):
    async with Client(create_server(SimulatedBackend("bad-deploy"), settings)) as c:
        res = await c.call_tool("query_logs", args)
    assert res.is_error


async def test_project_allowlist(settings):
    async with Client(create_server(SimulatedBackend("bad-deploy"), settings)) as c:
        res = await c.call_tool("get_active_alerts", {"project_id": "other-project-999"})
    assert res.is_error and "allowlist" in res.content[0].text


async def test_query_logs_filters_and_labels_untrusted(settings):
    async with Client(create_server(SimulatedBackend("prompt-injection"), settings)) as c:
        data = payload(await c.call_tool("query_logs", {"project_id": P, "service": "payments"}))
        errors = payload(await c.call_tool("query_logs", {"project_id": P, "min_severity": "ERROR", "contains": "403"}))
    assert data["untrusted_content"] is True
    assert data["suspected_prompt_injection_entries"] == 1
    assert errors["count"] == 6 and all(e["severity"] == "ERROR" for e in errors["entries"])


async def test_metric_summary(settings):
    async with Client(create_server(SimulatedBackend("bad-deploy"), settings)) as c:
        data = payload(
            await c.call_tool(
                "get_metric_timeseries",
                {"project_id": P, "metric": "run_request_count_5xx", "service": "checkout"},
            )
        )
    assert data["series"][0]["summary"]["max"] == 12.6


async def test_rollback_requires_valid_token(settings):
    backend = SimulatedBackend("bad-deploy")
    async with Client(create_server(backend, settings)) as c:
        res = await c.call_tool("rollback_cloud_run_revision", {**ROLLBACK, "approval_token": "123.deadbeef"})
        assert res.is_error and "human must approve" in res.content[0].text
        token = issue_approval_token(approval_secret(settings), {"tool": "rollback_cloud_run_revision", **ROLLBACK})
        ok = payload(await c.call_tool("rollback_cloud_run_revision", {**ROLLBACK, "approval_token": token}))
    assert ok["status"] == "ROLLED_BACK"
    assert backend.write_log and backend.write_log[0]["traffic"] == {"checkout-00041-xit": 100}


async def test_writes_disabled_on_real_backend_by_default(settings):
    gcp_settings = settings.model_copy(update={"backend": "gcp", "allow_writes": False})
    token = issue_approval_token(approval_secret(gcp_settings), {"tool": "rollback_cloud_run_revision", **ROLLBACK})
    async with Client(create_server(SimulatedBackend("bad-deploy"), gcp_settings)) as c:
        res = await c.call_tool("rollback_cloud_run_revision", {**ROLLBACK, "approval_token": token})
    assert res.is_error and "disabled" in res.content[0].text


def test_all_fixtures_load_and_use_safe_data():
    names = [s["name"] for s in list_scenarios()]
    assert len(names) >= 10
    for n in names:
        b = SimulatedBackend(n)
        raw = json.dumps(b.fixture)
        assert b.project_id == "demo-project-123"
        assert "AIza" not in raw and "sk-" not in raw
        assert b.alert["incident"]["resource"]["labels"]["project_id"] == "demo-project-123"
