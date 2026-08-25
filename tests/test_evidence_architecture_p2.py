"""P2 tests: canonical evidence model, deterministic IDs, evidence
validation utilities, the technical/behavioral "insufficient evidence"
distinction, scoring's exclusion of ungrounded scores, and report
integration of resume-audit evidence. Never makes a real API call."""
import json
import pytest

from schemas.evaluation import EvidenceItem
from utils.evidence import (
    build_evidence_id,
    create_evidence,
    resolve_transcript_evidence,
    validate_evidence_belongs_to_candidate,
    validate_evidence_references_real_question,
    validate_evidence_ids_unique,
)
from agents.technical_evaluator.agent import TechnicalEvaluatorAgent
from agents.behavioral_evaluator.agent import BehavioralEvaluatorAgent
from agents.scoring.agent import ScoringAgent
from agents.report_generator.agent import ReportGeneratorAgent
from schemas.evaluation import (
    TechnicalEvaluation,
    BehavioralEvaluation,
    CompetencyScore,
    MatchingScore,
    ClaimVerification,
)
from schemas.job import JobDescription, Competency
from schemas.scoring import CandidateScores
from tests.fakes import SingleResponseLLMProvider


# ---------------------------------------------------------------------------
# Evidence identity
# ---------------------------------------------------------------------------

class TestDeterministicEvidenceIds:
    def test_same_inputs_produce_same_id(self):
        id_a = build_evidence_id("cand_001", "q001", "Python", "technical_evaluator")
        id_b = build_evidence_id("cand_001", "q001", "Python", "technical_evaluator")
        assert id_a == id_b

    def test_different_candidate_produces_different_id(self):
        id_a = build_evidence_id("cand_001", "q001", "Python", "technical_evaluator")
        id_b = build_evidence_id("cand_002", "q001", "Python", "technical_evaluator")
        assert id_a != id_b

    def test_different_competency_produces_different_id(self):
        id_a = build_evidence_id("cand_001", "q001", "Python", "technical_evaluator")
        id_b = build_evidence_id("cand_001", "q001", "SQL", "technical_evaluator")
        assert id_a != id_b

    def test_id_is_readable_and_deterministic_format(self):
        evidence_id = build_evidence_id("cand_001", "q001", "Python", "technical_evaluator")
        assert evidence_id == "ev_cand_001_q001_python_technical_evaluator"

    def test_resolve_transcript_evidence_ids_are_unique_across_a_full_evaluation(
        self, sample_interview_transcript
    ):
        e1 = resolve_transcript_evidence(
            transcript=sample_interview_transcript, question_number=1,
            agent="technical_evaluator", explanation="a", competency="Python",
        )
        e2 = resolve_transcript_evidence(
            transcript=sample_interview_transcript, question_number=2,
            agent="technical_evaluator", explanation="b", competency="SQL",
        )
        assert validate_evidence_ids_unique([e1, e2])
        assert e1.evidence_id != e2.evidence_id


# ---------------------------------------------------------------------------
# Evidence validation utilities
# ---------------------------------------------------------------------------

class TestEvidenceValidationUtilities:
    def test_evidence_belongs_to_matching_candidate(self):
        ev = create_evidence(
            source_type="derived", text="x", agent="a", explanation="e", candidate_id="cand_001"
        )
        assert validate_evidence_belongs_to_candidate(ev, "cand_001") is True

    def test_evidence_flagged_when_candidate_mismatches(self):
        ev = create_evidence(
            source_type="derived", text="x", agent="a", explanation="e", candidate_id="cand_001"
        )
        assert validate_evidence_belongs_to_candidate(ev, "cand_002") is False

    def test_unstamped_evidence_is_not_a_violation(self):
        ev = create_evidence(source_type="derived", text="x", agent="a", explanation="e")
        assert validate_evidence_belongs_to_candidate(ev, "cand_001") is True

    def test_evidence_references_real_question(self, sample_interview_transcript):
        ev = resolve_transcript_evidence(
            transcript=sample_interview_transcript, question_number=1,
            agent="technical_evaluator", explanation="e",
        )
        assert validate_evidence_references_real_question(ev, sample_interview_transcript) is True

    def test_evidence_with_fabricated_question_id_rejected(self, sample_interview_transcript):
        ev = EvidenceItem(
            evidence_id="ev_fake",
            source_type="transcript",
            question_id="q999_does_not_exist",
            text="something",
            relevance=0.8,
            agent="technical_evaluator",
            explanation="e",
        )
        assert validate_evidence_references_real_question(ev, sample_interview_transcript) is False

    def test_non_transcript_evidence_trivially_passes_question_check(self):
        ev = create_evidence(source_type="resume", text="x", agent="a", explanation="e")
        # No transcript needed to validate resume-sourced evidence.
        from schemas.interview import InterviewTranscript
        empty_transcript = InterviewTranscript(
            interview_id="i", candidate_id="c", job_id="j", exchanges=[], start_time="t"
        )
        assert validate_evidence_references_real_question(ev, empty_transcript) is True

    def test_duplicate_ids_detected(self):
        ev1 = EvidenceItem(evidence_id="dup", source_type="derived", text="a", relevance=0.5, agent="x", explanation="e")
        ev2 = EvidenceItem(evidence_id="dup", source_type="derived", text="b", relevance=0.5, agent="x", explanation="e")
        assert validate_evidence_ids_unique([ev1, ev2]) is False


# ---------------------------------------------------------------------------
# Technical / behavioral evaluator: insufficient-evidence marking
# ---------------------------------------------------------------------------

class TestTechnicalEvaluatorInsufficientEvidence:
    @pytest.mark.asyncio
    async def test_scored_competency_without_resolvable_evidence_is_marked_insufficient(
        self, sample_job_description, sample_parsed_resume, sample_interview_transcript
    ):
        """The LLM scores 'Python' but cites no evidence_question_number at
        all - the score is kept (transparency), but must be explicitly
        marked insufficient, never look like a normal grounded result."""
        fake = SingleResponseLLMProvider(json.dumps({
            "technical_score": 7.0,
            "competency_scores": {
                "Python": {"score": 9.0, "confidence": 0.9, "explanation": "Confident but ungrounded"},
            },
            "explanation": "ok",
            "confidence": 0.7,
        }))
        agent = TechnicalEvaluatorAgent(llm_provider=fake)
        result = await agent.execute(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )
        cs = result["technical_evaluation"].competency_scores[0]
        assert cs.score == 9.0  # LLM's claim preserved for transparency
        assert cs.evidence == []
        assert cs.evidence_status == "insufficient"

    @pytest.mark.asyncio
    async def test_scored_competency_with_resolvable_evidence_is_marked_supported(
        self, sample_job_description, sample_parsed_resume, sample_interview_transcript
    ):
        fake = SingleResponseLLMProvider(json.dumps({
            "technical_score": 8.0,
            "competency_scores": {
                "Python": {"score": 8.5, "confidence": 0.9, "evidence_question_number": 1, "explanation": "ok"},
            },
            "explanation": "ok",
            "confidence": 0.8,
        }))
        agent = TechnicalEvaluatorAgent(llm_provider=fake)
        result = await agent.execute(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )
        cs = result["technical_evaluation"].competency_scores[0]
        assert cs.evidence_status == "supported"
        assert len(cs.evidence) == 1
        # Real transcript text preserved verbatim.
        assert cs.evidence[0].text == sample_interview_transcript.exchanges[0][1].answer_text
        assert cs.evidence[0].candidate_id == sample_parsed_resume.candidate_id
        assert cs.evidence[0].competency == "Python"


class TestBehavioralEvaluatorInsufficientEvidence:
    @pytest.mark.asyncio
    async def test_scored_competency_without_resolvable_evidence_is_marked_insufficient(
        self, sample_job_description, sample_parsed_resume, sample_interview_transcript
    ):
        fake = SingleResponseLLMProvider(json.dumps({
            "behavioral_score": 7.0,
            "competency_scores": {
                "System Design": {"score": 8.0, "confidence": 0.8, "explanation": "no citation"},
            },
            "explanation": "ok",
            "confidence": 0.7,
        }))
        agent = BehavioralEvaluatorAgent(llm_provider=fake)
        result = await agent.execute(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )
        cs = result["behavioral_evaluation"].competency_scores[0]
        assert cs.evidence_status == "insufficient"
        assert cs.evidence == []
        assert "fabricated" not in cs.explanation.lower()


# ---------------------------------------------------------------------------
# Scoring: insufficient evidence must not silently become a normal score
# ---------------------------------------------------------------------------

def _job(weights):
    return JobDescription(
        job_id="job_p2_test", title="Test Role", description="Test",
        competencies=[Competency(name=name, weight=w) for name, w in weights.items()],
    )


def _tech_eval(competency_scores):
    return TechnicalEvaluation(
        evaluation_id="tech_1", candidate_id="cand_001", job_id="job_p2_test",
        interview_id="int_001", competency_scores=competency_scores,
        technical_score=7.0, explanation="test", confidence=0.8,
    )


def _behav_eval():
    return BehavioralEvaluation(
        evaluation_id="behav_1", candidate_id="cand_001", job_id="job_p2_test",
        interview_id="int_001", competency_scores=[], behavioral_score=7.0,
        communication=7.0, problem_solving=7.0, teamwork=7.0, adaptability=7.0,
        explanation="test", confidence=0.8,
    )


def _matching_score():
    return MatchingScore(
        match_id="match_1", candidate_id="cand_001", job_id="job_p2_test",
        match_score=0.8, experience_match=0.8, skill_gap=0.2, explanation="test", confidence=0.8,
    )


class TestScoringExcludesInsufficientEvidence:
    @pytest.mark.asyncio
    async def test_insufficient_evidence_competency_does_not_move_the_score(self):
        """Two otherwise-identical scoring runs, differing only in whether
        the Python competency's evidence_status is 'supported' or
        'insufficient' - the insufficient one must NOT count toward
        rubric_score/rubric_coverage the way a normal supported score would."""
        job = _job({"Python": 0.6, "System Design": 0.4})

        supported_cs = CompetencyScore(
            competency_name="Python", score=9.0, confidence=0.9, explanation="ok",
            evidence_status="supported",
        )
        insufficient_cs = CompetencyScore(
            competency_name="Python", score=9.0, confidence=0.9, explanation="ok",
            evidence_status="insufficient",
        )

        agent = ScoringAgent()
        result_supported = await agent.execute(
            job_description=job, technical_evaluation=_tech_eval([supported_cs]),
            behavioral_evaluation=_behav_eval(), matching_score=_matching_score(),
            candidate_id="cand_001",
        )
        result_insufficient = await agent.execute(
            job_description=job, technical_evaluation=_tech_eval([insufficient_cs]),
            behavioral_evaluation=_behav_eval(), matching_score=_matching_score(),
            candidate_id="cand_001",
        )

        scores_supported = result_supported["candidate_scores"]
        scores_insufficient = result_insufficient["candidate_scores"]

        # Supported: Python (weight 0.6) is matched -> coverage 0.6.
        assert scores_supported.rubric_coverage == pytest.approx(0.6)
        # Insufficient: excluded from matching entirely -> coverage 0.0.
        assert scores_insufficient.rubric_coverage == pytest.approx(0.0)
        assert scores_supported.weighted_final_score != scores_insufficient.weighted_final_score

    @pytest.mark.asyncio
    async def test_insufficient_evidence_competency_still_visible_in_report(self):
        """Excluded from the NUMBER, but not hidden from the report - a
        human reviewer should still be able to see what the evaluator
        claimed, just know not to trust it."""
        job = _job({"Python": 1.0})
        insufficient_cs = CompetencyScore(
            competency_name="Python", score=9.0, confidence=0.9, explanation="ok",
            evidence_status="insufficient",
        )
        agent = ScoringAgent()
        result = await agent.execute(
            job_description=job, technical_evaluation=_tech_eval([insufficient_cs]),
            behavioral_evaluation=_behav_eval(), matching_score=_matching_score(),
            candidate_id="cand_001",
        )
        scores = result["candidate_scores"]
        assert len(scores.competency_scores) == 1
        assert scores.competency_scores[0].evidence_status == "insufficient"
        assert "insufficient evidence" in scores.explanation.lower()


# ---------------------------------------------------------------------------
# Report: resume-audit evidence integration, never inventing evidence
# ---------------------------------------------------------------------------

class TestReportIncludesClaimVerifications:
    @pytest.mark.asyncio
    async def test_report_surfaces_resume_audit_evidence(self, sample_parsed_resume):
        from schemas.evaluation import ResumeClaim

        claim = ResumeClaim(claim_id="claim_0", resume_claim="Built REST API", source="Achievement")
        evidence = create_evidence(
            source_type="transcript", text="I built a REST API using FastAPI.",
            agent="resume_auditor", explanation="matches", candidate_id="cand_test_001",
            question_id="eq_tech_001",
        )
        verification = ClaimVerification(
            verification_id="cv_1", candidate_id="cand_test_001", job_id="job_test_001",
            interview_id="int_test_001", claim=claim, verification_status="supported",
            confidence=0.9, evidence=[evidence], explanation="Confirmed in interview",
        )

        scores = CandidateScores(
            score_id="s1", candidate_id="cand_test_001", job_id="job_test_001",
            technical_score=8.0, behavioral_score=7.0, job_fit_score=8.0,
            weighted_final_score=8.0, explanation="test", confidence=0.8,
        )

        agent = ReportGeneratorAgent()
        result = await agent.execute(
            candidate_scores=scores, parsed_resume=sample_parsed_resume,
            resume_audits=[verification],
        )
        report = result["candidate_report"]
        assert len(report.claim_verifications) == 1
        # The evidence in the report is the EXACT same evidence object that
        # was passed in - never regenerated/invented by this agent.
        assert report.claim_verifications[0].evidence[0].evidence_id == evidence.evidence_id
        assert report.claim_verifications[0].evidence[0].text == evidence.text

    @pytest.mark.asyncio
    async def test_report_without_resume_audits_has_empty_claim_verifications(self, sample_parsed_resume):
        scores = CandidateScores(
            score_id="s1", candidate_id="cand_test_001", job_id="job_test_001",
            technical_score=8.0, behavioral_score=7.0, job_fit_score=8.0,
            weighted_final_score=8.0, explanation="test", confidence=0.8,
        )
        agent = ReportGeneratorAgent()
        result = await agent.execute(candidate_scores=scores, parsed_resume=sample_parsed_resume)
        assert result["candidate_report"].claim_verifications == []

    @pytest.mark.asyncio
    async def test_report_requires_human_review_when_insufficient_evidence_score_present(
        self, sample_parsed_resume
    ):
        insufficient_cs = CompetencyScore(
            competency_name="Python", score=9.0, confidence=0.9, explanation="ok",
            evidence_status="insufficient",
        )
        scores = CandidateScores(
            score_id="s1", candidate_id="cand_test_001", job_id="job_test_001",
            technical_score=8.0, behavioral_score=7.0, job_fit_score=8.0,
            weighted_final_score=9.0,  # would otherwise be "strong_candidate"
            competency_scores=[insufficient_cs],
            explanation="test", confidence=0.8,
        )
        agent = ReportGeneratorAgent()
        result = await agent.execute(candidate_scores=scores, parsed_resume=sample_parsed_resume)
        assert result["candidate_report"].requires_human_review is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
