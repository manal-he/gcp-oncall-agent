from typing import Any

from oncall_agent.agent.llm.base import LLMResponse, ToolCall, Usage
from oncall_agent.agent.llm.mock import MockLLM
from oncall_agent.agent.report import (
    ActionKind,
    Evidence,
    IncidentReport,
    RecommendedAction,
    RootCauseCategory,
)
from oncall_agent.agent.runner import Investigation, approve_all, reject_all


async def test_bad_deploy_approved_rolls_back(settings):
    inv = Investigation("bad-deploy")
    snap = await inv.run(settings, approve_all)
    assert snap["status"] == "completed"
    assert snap["report"]["root_cause_category"] == "bad_deploy"
    assert snap["approval"]["approved"] is True
    assert snap["action_result"]["ok"] is True
    assert inv.backend.write_log[0]["traffic"] == {"checkout-00041-xit": 100}
    types = [e["type"] for e in snap["events"]]
    assert types.index("approval_required") < types.index("approval") < types.index("action_executed")
    # the approval token never leaks into the recorded trace
    assert all("approval_token" not in r["args"] for r in snap["tool_calls"])


async def test_rejection_means_no_write(settings):
    inv = Investigation("bad-deploy")
    snap = await inv.run(settings, reject_all)
    assert snap["approval"]["approved"] is False
    assert snap["action_result"] is None
    assert inv.backend.write_log == []


async def test_non_rollback_report_does_not_ask_for_approval(settings):
    asked: list[Any] = []

    async def approver(req):
        asked.append(req)
        return {"approved": True}

    snap = await Investigation("oom-killed").run(settings, approver)
    assert snap["report"]["root_cause_category"] == "oom"
    assert asked == []


async def test_prompt_injection_attempt_is_blocked(settings):
    inv = Investigation("prompt-injection")
    snap = await inv.run(settings, approve_all, llm=MockLLM(injectable=True))
    blocked = [r for r in snap["tool_calls"] if r["blocked"]]
    assert blocked and blocked[0]["tool"] == "rollback_cloud_run_revision"
    assert inv.backend.write_log == []  # nothing executed even with an approve-everything human
    assert any("blocked" in n for n in snap["report"]["security_notes"])
    assert snap["report"]["root_cause_category"] == "iam_permission"


async def test_tool_call_budget_stops_investigation(settings):
    s = settings.model_copy(update={"max_tool_calls": 3})
    snap = await Investigation("bad-deploy").run(s, reject_all)
    executed = [r for r in snap["tool_calls"] if r["phase"] == "investigate" and r["error"] != "budget"]
    assert len(executed) == 3
    # calls requested beyond the budget are answered with "NOT EXECUTED" so the transcript stays valid
    assert any(r["error"] == "budget" for r in snap["tool_calls"])
    assert "max_tool_calls" in (snap["stop_reason"] or "")
    assert any("stopped early" in q for q in snap["report"]["open_questions"])


async def test_step_budget(settings):
    s = settings.model_copy(update={"max_steps": 2})
    snap = await Investigation("bad-deploy").run(s, reject_all)
    assert "max_steps" in (snap["stop_reason"] or "")


class HallucinatingLLM:
    """Calls one tool, then cites a non-existent evidence ref and proposes an off-allowlist rollback."""

    provider, model = "fake", "fake"

    def __init__(self) -> None:
        self.turn = 0

    async def complete(self, messages, tools):
        self.turn += 1
        if self.turn == 2:
            return LLMResponse(tool_calls=[ToolCall("c1", "get_active_alerts", {"project_id": "demo-project-123"})])
        return LLMResponse(text="done", usage=Usage(10, 5))

    async def structured(self, messages, schema):
        rep = IncidentReport(
            title="t",
            summary="s",
            severity="SEV2",
            root_cause_category=RootCauseCategory.bad_deploy,
            root_cause="r",
            evidence=[
                Evidence(ref="T1", tool="get_active_alerts", finding="real"),
                Evidence(ref="T99", tool="query_logs", finding="invented"),
            ],
            confidence=0.9,
            recommended_action=RecommendedAction(
                kind=ActionKind.rollback_cloud_run,
                description="x",
                params={
                    "project_id": "evil-project-666",
                    "region": "europe-west1",
                    "service": "checkout",
                    "target_revision": "checkout-00001",
                },
            ),
        )
        return rep, Usage(10, 5)


async def test_report_grounding_and_param_validation(settings):
    snap = await Investigation("bad-deploy").run(settings, approve_all, llm=HallucinatingLLM())
    rep = snap["report"]
    assert [e["ref"] for e in rep["evidence"]] == ["T1"]
    assert rep["confidence"] == 0.72
    assert rep["recommended_action"]["kind"] == "escalate"  # project not allowlisted -> downgraded
    assert snap["approval"] is None


async def test_stdio_transport_end_to_end(settings):
    s = settings.model_copy(update={"mcp_transport": "stdio"})
    snap = await Investigation("bad-deploy").run(s, approve_all)
    assert snap["report"]["root_cause_category"] == "bad_deploy"
    assert snap["action_result"]["ok"] is True  # token minted here, verified by the child process
