"""Integration tests for P0-4: a candidate whose technical evaluation fails
must be excluded from scoring/reports/leaderboard with an explicit reason -
never silently scored as an average (~7.0) candidate - and this must hold
whether one candidate is affected or every candidate is. Runs the real
LangGraph pipeline in mock mode (no real LLM/API calls)."""
from tests.rubric_fixtures import approved_rubric
import pytest

from data import load_job_description, load_all_resumes, load_interview_transcript
from main import build_job_from_dict, build_resume_from_dict, build_transcript_from_dict
from orchestration.graph import create_pipeline_graph, PipelineState
from agents.technical_evaluator.agent import TechnicalEvaluatorAgent


def _build_initial_state(run_id: str) -> PipelineState:
    job_dict = load_job_description()
    resumes_dicts = load_all_resumes()
    transcript_dict = load_interview_transcript()

    job_description = build_job_from_dict(job_dict)
    parsed_resumes = [build_resume_from_dict(r) for r in resumes_dicts]
    base_transcript = build_transcript_from_dict(transcript_dict)

    interview_transcripts = {}
    for i in range(len(parsed_resumes)):
        candidate_id = f"cand_{i + 1:03d}"
        interview_transcripts[candidate_id] = base_transcript.model_copy(
            update={"candidate_id": candidate_id, "interview_id": f"int_{candidate_id}"}
        )

    return PipelineState(
        job_description_text=job_description.description,
        candidates_resume_texts=[r.raw_text for r in parsed_resumes],
        interview_transcripts=interview_transcripts,
        job_description=None,
            # Matching is rubric-driven; a job with no approved rubric
            # is deliberately not scorable.
            job_rubric=approved_rubric(),
        parsed_resumes={},
        matching_scores={},
        shortlisted_candidates=[],
        # This test exercises the interview/evaluation half of the pipeline,
        # which is now gated behind an explicit recruiter decision: matching
        # alone never advances a candidate to interview. Advancing here is
        # what a recruiter would do after reading the leaderboard.
        recruiter_advanced_candidates=list(interview_transcripts),
        interview_questions={},
        technical_evaluations={},
        behavioral_evaluations={},
        resume_audits={},
        integrity_evaluations={},
        bias_audits={},
        candidate_scores={},
        candidate_reports={},
        leaderboard=None,
        run_id=run_id,
        errors=[],
        audit_logs=[],
    )


@pytest.mark.asyncio
async def test_candidate_with_failed_technical_evaluation_is_excluded_not_defaulted(monkeypatch):
    original_execute = TechnicalEvaluatorAgent.execute

    async def failing_execute(self, job_description, parsed_resume, interview_transcript, **kwargs):
        if parsed_resume.candidate_id == "cand_002":
            raise RuntimeError("simulated technical evaluator failure")
        return await original_execute(
            self,
            job_description=job_description,
            parsed_resume=parsed_resume,
            interview_transcript=interview_transcript,
            **kwargs,
        )

    monkeypatch.setattr(TechnicalEvaluatorAgent, "execute", failing_execute)

    state = _build_initial_state("run_test_incomplete")
    pipeline = create_pipeline_graph()
    result = await pipeline.ainvoke(state)

    # cand_002 must never reach scoring/reports despite the rest of the
    # pipeline succeeding for it up to that point.
    assert "cand_002" not in result["candidate_scores"]
    assert "cand_002" not in result["candidate_reports"]

    # The exclusion must be explicit, not silent.
    assert any("cand_002" in e and "excluded" in e for e in result["errors"])

    # The leaderboard must represent the incomplete candidate rather than
    # just omitting it with no trace.
    assert result["leaderboard"] is not None
    assert "cand_002" in result["leaderboard"].incomplete_candidates
    assert all(entry.candidate_id != "cand_002" for entry in result["leaderboard"].entries)

    # The other candidates are unaffected and scored normally.
    assert "cand_001" in result["candidate_scores"]
    assert "cand_003" in result["candidate_scores"]
    assert result["leaderboard"].total_candidates == 2


@pytest.mark.asyncio
async def test_all_candidates_incomplete_produces_empty_leaderboard_not_crash(monkeypatch):
    """Every candidate's technical evaluation fails. The pipeline must still
    complete (no unhandled exception out of the graph), producing zero
    scores/reports, a leaderboard with entries=[] and everyone listed in
    incomplete_candidates, and no fabricated score or ranking anywhere."""

    async def always_failing_execute(self, job_description, parsed_resume, interview_transcript, **kwargs):
        raise RuntimeError("simulated technical evaluator failure")

    monkeypatch.setattr(TechnicalEvaluatorAgent, "execute", always_failing_execute)

    state = _build_initial_state("run_test_all_incomplete")
    pipeline = create_pipeline_graph()
    result = await pipeline.ainvoke(state)

    assert result["candidate_scores"] == {}
    assert result["candidate_reports"] == {}

    assert result["leaderboard"] is not None
    leaderboard = result["leaderboard"]
    assert leaderboard.entries == []
    assert leaderboard.top_candidates == []
    assert leaderboard.total_candidates == 0
    assert leaderboard.strong_candidates == 0
    assert leaderboard.candidates == 0
    assert set(leaderboard.incomplete_candidates) == {"cand_001", "cand_002", "cand_003"}
    assert "human review" in leaderboard.explanation.lower()

    # Every shortlisted candidate's exclusion is explicit in errors.
    for cid in ("cand_001", "cand_002", "cand_003"):
        assert any(cid in e and "excluded" in e for e in result["errors"])
    assert any("no candidate could be scored" in e.lower() for e in result["errors"])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
