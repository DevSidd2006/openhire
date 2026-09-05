"""
P6: tests for the evaluation framework itself, plus the two cross-cutting
checks that don't fit the per-case golden-dataset model in evaluation/cases/:

    - candidate isolation through the COMPLETE real pipeline (P6 Phase 21) -
      needs two candidates run through orchestration/graph.py together,
      which is a pipeline-level property, not a single-agent case.
    - deterministic-consistency (P6 Phase 22) - needs the SAME case run
      multiple times and compared, which the runner's per-case model
      doesn't do by default.

Framework self-tests confirm evaluation/runner.py actually loads and
executes the golden dataset (catches "the JSON is malformed" or "an agent
name has no adapter" before they'd silently show as 0 cases).
"""
from tests.rubric_fixtures import approved_rubric
import copy

import pytest

from evaluation.runner import load_cases, run_all, run_case
from orchestration.graph import PipelineState, get_pipeline


# ---------------------------------------------------------------------------
# Framework self-tests
# ---------------------------------------------------------------------------

class TestFrameworkLoadsRealCases:
    def test_loads_all_case_files(self):
        cases = load_cases()
        assert len(cases) >= 70, "golden dataset should have a substantial number of cases"
        agents = {c.agent for c in cases}
        assert "technical_evaluator" in agents
        assert "scoring" in agents
        assert "leaderboard" in agents

    def test_every_case_has_an_adapter(self):
        from evaluation.adapters import ADAPTERS
        cases = load_cases()
        missing = sorted({c.agent for c in cases if c.agent not in ADAPTERS})
        assert missing == [], f"cases reference agents with no registered adapter: {missing}"

    @pytest.mark.asyncio
    async def test_running_all_cases_never_silently_no_ops(self):
        """Every case must produce a real PASS/FAIL/ERROR verdict - not an
        empty/skipped result (P6 Phase 24: never hide a finding)."""
        results = await run_all()
        assert len(results) == len(load_cases())
        assert all(r.verdict is not None for r in results)


# ---------------------------------------------------------------------------
# Candidate isolation through the complete pipeline (P6 Phase 21)
# ---------------------------------------------------------------------------

class TestCandidateIsolationThroughFullPipeline:
    @pytest.mark.asyncio
    async def test_candidate_a_and_b_never_cross_contaminate(self):
        from schemas.interview import InterviewAnswer, InterviewQuestion, InterviewTranscript

        transcript_a = InterviewTranscript(
            interview_id="int_a", candidate_id="cand_isolation_a", job_id="job_isolation",
            start_time="t0", is_sealed=True,
            exchanges=[(
                InterviewQuestion(question_id="qa1", question_text="Tell us about your Python experience.", category="technical"),
                InterviewAnswer(question_id="qa1", answer_text="I built an async FastAPI service with asyncio.gather."),
            )],
        )
        transcript_b = InterviewTranscript(
            interview_id="int_b", candidate_id="cand_isolation_b", job_id="job_isolation",
            start_time="t0", is_sealed=True,
            exchanges=[(
                InterviewQuestion(question_id="qb1", question_text="Tell us about your SQL experience.", category="technical"),
                InterviewAnswer(question_id="qb1", answer_text="I optimize slow queries with EXPLAIN ANALYZE and targeted indexes."),
            )],
        )

        state = PipelineState(
            job_description_text="Backend role requiring Python, FastAPI, SQL, REST APIs.",
            candidates_resume_texts=["Candidate A resume text.", "Candidate B resume text."],
            interview_transcripts={"cand_001": transcript_a.model_copy(update={"candidate_id": "cand_001"}), "cand_002": transcript_b.model_copy(update={"candidate_id": "cand_002"})},
            job_description=None,
            # Matching is rubric-driven; a job with no approved rubric
            # is deliberately not scorable.
            job_rubric=approved_rubric(), parsed_resumes={}, matching_scores={}, shortlisted_candidates=[],
            # Interview/evaluation stages are gated behind an explicit
            # recruiter advance; matching alone never starts an interview.
            recruiter_advanced_candidates=["cand_001", "cand_002"],
            interview_questions={}, technical_evaluations={}, behavioral_evaluations={}, resume_audits={},
            integrity_evaluations={}, bias_audits={}, candidate_scores={}, candidate_reports={},
            leaderboard=None, run_id="run_p6_isolation", errors=[], audit_logs=[],
        )

        result = await get_pipeline().ainvoke(state)

        tech_evals = result["technical_evaluations"]
        assert "cand_001" in tech_evals and "cand_002" in tech_evals

        eval_a, eval_b = tech_evals["cand_001"], tech_evals["cand_002"]
        assert eval_a.candidate_id == "cand_001"
        assert eval_b.candidate_id == "cand_002"

        # No evidence from A's evaluation is stamped with B's candidate_id, or vice versa.
        for ev in eval_a.evidence:
            assert ev.candidate_id in (None, "cand_001"), "candidate A's evidence leaked candidate B's ID"
        for ev in eval_b.evidence:
            assert ev.candidate_id in (None, "cand_002"), "candidate B's evidence leaked candidate A's ID"

        # A's evidence text never contains B's transcript content, and vice versa.
        a_texts = {ev.text for ev in eval_a.evidence}
        b_texts = {ev.text for ev in eval_b.evidence}
        assert not (a_texts & b_texts), "candidate A and B evidence text overlaps - possible cross-contamination"
        assert "EXPLAIN ANALYZE" not in "".join(a_texts)
        assert "asyncio.gather" not in "".join(b_texts)

        scores = result["candidate_scores"]
        if "cand_001" in scores and "cand_002" in scores:
            assert scores["cand_001"].candidate_id == "cand_001"
            assert scores["cand_002"].candidate_id == "cand_002"


# ---------------------------------------------------------------------------
# Deterministic consistency (P6 Phase 22)
# ---------------------------------------------------------------------------

class TestDeterministicConsistency:
    """Runs the SAME case multiple times and compares stable vs. expected-
    nondeterministic fields. Random uuid4()-based IDs (evaluation_id,
    score_id, report_id, ...) are NOT expected to be stable across runs -
    documented here explicitly rather than silently ignored (P6 Phase 22:
    "if a component is intentionally nondeterministic, document that
    explicitly")."""

    @pytest.mark.asyncio
    async def test_technical_evaluator_case_is_stable_across_repeats(self):
        cases = {c.case_id: c for c in load_cases(["technical_evaluator"])}
        case = cases["tech_strong_answer_grounded"]

        results = [await run_case(copy.deepcopy(case)) for _ in range(3)]
        assert all(r.passed for r in results), "case must pass consistently, not flap"

        from evaluation.adapters import ADAPTERS
        outputs = [await ADAPTERS[case.agent](case) for _ in range(3)]
        scores = [ctx["output"].technical_score for ctx in outputs]
        evidence_ids = [ctx["output"].competency_scores[0].evidence[0].evidence_id for ctx in outputs]
        evidence_texts = [ctx["output"].competency_scores[0].evidence[0].text for ctx in outputs]
        candidate_ids = [ctx["output"].candidate_id for ctx in outputs]
        job_ids = [ctx["output"].job_id for ctx in outputs]

        assert len(set(scores)) == 1, f"technical_score is not stable across repeats: {scores}"
        assert len(set(evidence_ids)) == 1, f"evidence_id (deterministic by P2 design) is not stable: {evidence_ids}"
        assert len(set(evidence_texts)) == 1, f"evidence text is not stable: {evidence_texts}"
        assert len(set(candidate_ids)) == 1
        assert len(set(job_ids)) == 1

        # Documented nondeterminism: evaluation_id uses uuid4() and is NOT
        # expected to match across runs.
        evaluation_ids = [ctx["output"].evaluation_id for ctx in outputs]
        assert len(set(evaluation_ids)) == 3, (
            "evaluation_id is expected to vary across runs (uuid4-based) - "
            "if this ever becomes stable, update this test's documented expectation"
        )

    @pytest.mark.asyncio
    async def test_scoring_is_fully_deterministic_including_ids_it_does_not_randomize(self):
        """ScoringAgent's weighted_final_score/rubric_coverage computation
        is pure arithmetic over its inputs - must be byte-identical every
        run."""
        cases = {c.case_id: c for c in load_cases(["scoring"])}
        case = cases["scoring_weighted_average_two_competencies"]

        from evaluation.adapters import ADAPTERS
        outputs = [await ADAPTERS[case.agent](case) for _ in range(3)]
        finals = [ctx["output"].weighted_final_score for ctx in outputs]
        coverages = [ctx["output"].rubric_coverage for ctx in outputs]

        assert len(set(finals)) == 1
        assert len(set(coverages)) == 1
