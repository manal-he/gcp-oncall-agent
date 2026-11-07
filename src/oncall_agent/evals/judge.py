"""Root-cause judges.

``RubricJudge`` is deterministic: the predicted category must be acceptable AND every
keyword group (OR within a group) must appear in the report's root cause / summary /
evidence. It is cheap and reproducible but brittle to paraphrase.

``LLMJudge`` asks a model to compare the report with the ground-truth note and returns a
score with a rationale. It handles paraphrase but adds cost and its own variance; use a
different model family from the agent under test when possible, and spot-check verdicts.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field

from oncall_agent.agent.llm.base import LLM

if TYPE_CHECKING:
    from oncall_agent.evals.harness import EvalCase


class Verdict(BaseModel):
    correct: bool
    score: float = Field(ge=0, le=1)
    rationale: str


class Judge(Protocol):
    name: str

    async def judge(self, case: EvalCase, report: dict[str, Any]) -> Verdict: ...


def _report_text(report: dict[str, Any]) -> str:
    parts = [report.get("root_cause", ""), report.get("summary", "")]
    parts += [e.get("finding", "") for e in report.get("evidence", [])]
    return " ".join(parts).lower()


class RubricJudge:
    name = "rubric"

    async def judge(self, case: EvalCase, report: dict[str, Any]) -> Verdict:
        if not report:
            return Verdict(correct=False, score=0.0, rationale="no report")
        cat_ok = report.get("root_cause_category") in case.acceptable_categories
        text = _report_text(report)
        groups = [any(k.lower() in text for k in g) for g in case.root_cause_keywords]
        kw_ok = all(groups)
        score = 0.6 * cat_ok + 0.4 * (sum(groups) / len(groups) if groups else 1.0)
        why = f"category {'ok' if cat_ok else 'wrong'} ({report.get('root_cause_category')}); "
        why += f"keyword groups matched {sum(groups)}/{len(groups)}"
        return Verdict(correct=cat_ok and kw_ok, score=round(score, 2), rationale=why)


JUDGE_PROMPT = """You are grading an automated incident investigation.
Ground truth root cause category: {category}
Ground truth explanation: {notes}

Agent report (JSON):
{report}

Is the agent's root cause materially the same as the ground truth? Paraphrase is fine; a
different component or mechanism is not. Ignore remediation quality. Return correct, a
score in [0,1] and a one-sentence rationale."""


class LLMJudge:
    name = "llm"

    def __init__(self, llm: LLM) -> None:
        self.llm = llm
        self.fallback = RubricJudge()

    async def judge(self, case: EvalCase, report: dict[str, Any]) -> Verdict:
        if not report:
            return Verdict(correct=False, score=0.0, rationale="no report")
        slim = {k: report.get(k) for k in ("root_cause_category", "root_cause", "summary", "evidence")}
        prompt = JUDGE_PROMPT.format(category=case.root_cause_category, notes=case.notes, report=json.dumps(slim))
        try:
            verdict, _ = await self.llm.structured(
                [
                    {"role": "system", "content": "You are a strict, fair grader."},
                    {"role": "user", "content": prompt},
                ],
                Verdict,
            )
            return verdict
        except Exception as exc:  # judge failure must not crash the run
            v = await self.fallback.judge(case, report)
            v.rationale = f"LLM judge failed ({type(exc).__name__}); rubric fallback: {v.rationale}"
            return v
