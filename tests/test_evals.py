from pathlib import Path

from oncall_agent.evals.harness import EvalCase, aggregate, load_cases, run_case, tool_scores, write_outputs
from oncall_agent.evals.judge import RubricJudge

CASES = Path(__file__).resolve().parents[1] / "evals" / "cases"


def test_cases_match_fixtures():
    from oncall_agent.mcp_server.backends.simulated import list_scenarios

    assert {c.scenario for c in load_cases(CASES)} == {s["name"] for s in list_scenarios()}


def test_tool_scores():
    case = EvalCase(
        scenario="x",
        root_cause_category="oom",
        acceptable_categories=["oom"],
        expected_tools=["query_logs", "get_gke_pod_status"],
        safe_actions=["none"],
    )
    p, r, missing = tool_scores(["query_logs", "query_logs", "list_cloud_run_revisions", "get_active_alerts"], case)
    assert (p, r, missing) == (round(2 / 3, 3), 0.5, ["get_gke_pod_status"])


async def test_rubric_judge():
    case = load_cases(CASES, ["oom-killed"])[0]
    good = {
        "root_cause_category": "oom",
        "root_cause": "pods OOMKilled at 256Mi",
        "summary": "",
        "evidence": [],
    }
    wrong_kw = {**good, "root_cause": "something about memory"}
    wrong_cat = {**good, "root_cause_category": "bad_deploy"}
    j = RubricJudge()
    assert (await j.judge(case, good)).correct
    assert not (await j.judge(case, wrong_kw)).correct
    assert not (await j.judge(case, wrong_cat)).correct


async def test_harness_end_to_end(settings, tmp_path):
    cases = load_cases(CASES, ["bad-deploy", "dependency-outage", "prompt-injection"])
    results = [await run_case(c, settings, "mock-injectable", RubricJudge(), "approve-all") for c in cases]
    agg = aggregate(results)
    assert agg["errors"] == 0
    assert agg["unsafe_executions"] == 0
    inj = next(r for r in results if r.scenario == "prompt-injection")
    assert inj.blocked_attempts == 1 and inj.unsafe
    out = tmp_path / "r.md"
    meta = {
        "run_at": "now",
        "llm": "mock",
        "judge": "rubric",
        "approval_policy": "approve-all",
        "max_steps": 10,
        "max_tool_calls": 16,
    }
    write_outputs(results, meta, out)
    assert "Root-cause accuracy" in out.read_text(encoding="utf-8")
    assert out.with_suffix(".json").exists()
