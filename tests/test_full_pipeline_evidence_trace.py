"""P2 Phase 14 'Full pipeline' test: runs the real mock LangGraph pipeline
end to end and verifies a concrete, complete evidence trace survives all the
way from a specific transcript answer through to the leaderboard:

    candidate -> question -> answer -> evidence -> competency -> score -> report -> leaderboard

Never makes a real API call (mock provider only)."""
import pytest

from data import load_job_description, load_all_resumes, load_interview_transcript
from main import build_job_from_dict, build_resume_from_dict, build_transcript_from_dict
from orchestration.graph import create_pipeline_graph, PipelineState
from utils.evidence import (
    validate_evidence_belongs_to_candidate,
    validate_evidence_references_real_question,
    validate_evidence_ids_unique,
)


def _build_state(run_id: str) -> PipelineState:
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
        parsed_resumes={},
        matching_scores={},
        shortlisted_candidates=[],
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
async def test_concrete_evidence_trace_survives_the_full_pipeline():
    state = _build_state("run_evidence_trace_test")
    pipeline = create_pipeline_graph()
    result = await pipeline.ainvoke(state)

    assert result["errors"] == []
    candidate_id = "cand_001"
    transcript = state["interview_transcripts"][candidate_id]

    # --- candidate -> question -> answer (the raw sealed transcript) ---
    question, answer = transcript.exchanges[0]
    assert question.question_id == "q001"
    assert answer.answer_text.startswith("I've been working with async Python")

    # --- -> evidence (technical evaluator's Python competency) ---
    tech_eval = result["technical_evaluations"][candidate_id]
    python_cs = next(cs for cs in tech_eval.competency_scores if cs.competency_name == "Python")
    assert python_cs.evidence_status == "supported"
    assert len(python_cs.evidence) == 1
    evidence = python_cs.evidence[0]

    # Evidence genuinely traces back to the exact question/answer pulled above.
    assert evidence.question_id == question.question_id
    assert evidence.text == answer.answer_text
    assert evidence.candidate_id == candidate_id
    assert evidence.competency == "Python"
    assert evidence.evidence_type == "supporting"

    # Evidence validation utilities agree this is a real, well-formed trace.
    assert validate_evidence_belongs_to_candidate(evidence, candidate_id)
    assert validate_evidence_references_real_question(evidence, transcript)
    assert validate_evidence_ids_unique(tech_eval.evidence)

    # --- -> competency -> score (ScoringAgent used this exact CompetencyScore) ---
    candidate_scores = result["candidate_scores"][candidate_id]
    scored_python = next(cs for cs in candidate_scores.competency_scores if cs.competency_name == "Python")
    assert scored_python.evidence[0].evidence_id == evidence.evidence_id
    assert scored_python.score == python_cs.score
    assert 0.0 <= candidate_scores.weighted_final_score <= 10.0

    # --- -> report (same evidence object reachable from the final report) ---
    report = result["candidate_reports"][candidate_id]
    reported_python = next(cs for cs in report.scores.competency_scores if cs.competency_name == "Python")
    assert reported_python.evidence[0].evidence_id == evidence.evidence_id
    assert reported_python.evidence[0].text == answer.answer_text

    # --- -> leaderboard (candidate ranked, traceable back to report_id) ---
    leaderboard = result["leaderboard"]
    entry = next(e for e in leaderboard.entries if e.candidate_id == candidate_id)
    assert entry.weighted_score == pytest.approx(candidate_scores.weighted_final_score)


@pytest.mark.asyncio
async def test_no_evidence_id_collisions_across_the_whole_run():
    """Uniqueness holds not just within one candidate's evaluation but
    across every candidate evaluated in the same run."""
    state = _build_state("run_evidence_uniqueness_test")
    pipeline = create_pipeline_graph()
    result = await pipeline.ainvoke(state)

    all_evidence_ids = []
    for tech_eval in result["technical_evaluations"].values():
        all_evidence_ids.extend(e.evidence_id for e in tech_eval.evidence)
    for behav_eval in result["behavioral_evaluations"].values():
        all_evidence_ids.extend(e.evidence_id for e in behav_eval.evidence)

    assert len(all_evidence_ids) > 0
    assert len(all_evidence_ids) == len(set(all_evidence_ids))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
