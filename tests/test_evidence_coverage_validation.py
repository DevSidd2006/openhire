"""Tests for deterministic evidence-backed coverage validation.

Tests the post-evaluation metric measuring evidence-backed coverage of
evaluated competencies:
    "How much of the candidate's evaluated competency profile is backed by
    valid, traceable evidence?"

Validates the pure computation function `compute_evidence_backed_coverage`,
evidence validation helpers (`is_valid_evidence`, `is_competency_evidence_backed`),
and integration with `ScoringAgent` and `CandidateScores`.
"""
import pytest

from agents.scoring.agent import ScoringAgent
from schemas.evaluation import (
    BehavioralEvaluation,
    CompetencyScore,
    EvidenceItem,
    MatchingScore,
    TechnicalEvaluation,
)
from schemas.interview import InterviewAnswer, InterviewQuestion, InterviewTranscript
from schemas.job import Competency, JobDescription
from schemas.scoring import CandidateScores
from utils.evidence import (
    compute_evidence_backed_coverage,
    create_evidence,
    create_transcript_evidence,
    is_competency_evidence_backed,
    is_valid_evidence,
)


def _make_transcript(candidate_id: str = "cand_001") -> InterviewTranscript:
    """Helper to create a simple two-exchange transcript."""
    return InterviewTranscript(
        interview_id="int_001",
        candidate_id=candidate_id,
        job_id="job_001",
        start_time="2026-10-04T00:00:00Z",
        exchanges=[
            (
                InterviewQuestion(
                    question_id="q1",
                    question_text="Tell me about Python.",
                    category="technical",
                    competency="Python",
                ),
                InterviewAnswer(question_id="q1", answer_text="I have built async services in Python."),
            ),
            (
                InterviewQuestion(
                    question_id="q2",
                    question_text="Explain system design.",
                    category="technical",
                    competency="System Design",
                ),
                InterviewAnswer(question_id="q2", answer_text="I designed distributed queues with Kafka."),
            ),
        ],
    )


def _make_job(weights: dict) -> JobDescription:
    """Helper to create a JobDescription with specified competency weights."""
    return JobDescription(
        job_id="job_001",
        title="Software Engineer",
        description="Role description",
        competencies=[Competency(name=name, weight=w) for name, w in weights.items()],
    )


def _make_evidence(
    question_id: str = "q1",
    candidate_id: str = "cand_001",
    evidence_type: str = "supporting",
    text: str = "I have built async services in Python.",
    competency: str = "Python",
) -> EvidenceItem:
    """Helper to create a transcript EvidenceItem."""
    return create_transcript_evidence(
        text=text,
        question_id=question_id,
        agent="technical_evaluator",
        explanation="Direct transcript answer",
        candidate_id=candidate_id,
        evidence_type=evidence_type,
        competency=competency,
    )


class TestEvidenceCoverageUnitScenarios:
    """Focused unit tests for the 13 required evidence coverage scenarios."""

    def test_1_100_percent_coverage(self):
        """1. 100% coverage: all applicable evaluated competencies have valid traceable evidence."""
        job = _make_job({"Python": 0.6, "System Design": 0.4})
        transcript = _make_transcript()

        ev1 = _make_evidence(question_id="q1", competency="Python")
        ev2 = _make_evidence(
            question_id="q2",
            competency="System Design",
            text="I designed distributed queues with Kafka.",
        )

        cs_list = [
            CompetencyScore(
                competency_name="Python", score=9.0, confidence=0.9, explanation="Strong", evidence=[ev1]
            ),
            CompetencyScore(
                competency_name="System Design", score=8.0, confidence=0.8, explanation="Solid", evidence=[ev2]
            ),
        ]

        coverage = compute_evidence_backed_coverage(
            competency_scores=cs_list,
            rubric=job,
            candidate_id="cand_001",
            transcript=transcript,
        )
        assert coverage == pytest.approx(1.0)

    def test_2_0_percent_coverage(self):
        """2. 0% coverage: applicable evaluated competencies have no valid traceable evidence."""
        job = _make_job({"Python": 0.6, "System Design": 0.4})

        cs_list = [
            CompetencyScore(
                competency_name="Python",
                score=9.0,
                confidence=0.9,
                explanation="No evidence",
                evidence=[],
                evidence_status="insufficient",
            ),
            CompetencyScore(
                competency_name="System Design",
                score=8.0,
                confidence=0.8,
                explanation="No evidence",
                evidence=[],
                evidence_status="insufficient",
            ),
        ]

        coverage = compute_evidence_backed_coverage(
            competency_scores=cs_list,
            rubric=job,
            candidate_id="cand_001",
        )
        assert coverage == 0.0

    def test_3_partial_weighted_coverage(self):
        """3. Partial weighted coverage: only a subset of evaluated competencies has valid evidence.
        Python (0.4) is valid, System Design (0.6) is insufficient -> 0.4 / (0.4 + 0.6) = 0.4."""
        job = _make_job({"Python": 0.4, "System Design": 0.6})
        ev_python = _make_evidence(question_id="q1", competency="Python")

        cs_list = [
            CompetencyScore(
                competency_name="Python",
                score=9.0,
                confidence=0.9,
                explanation="Grounded",
                evidence=[ev_python],
                evidence_status="supported",
            ),
            CompetencyScore(
                competency_name="System Design",
                score=7.0,
                confidence=0.7,
                explanation="Ungrounded",
                evidence=[],
                evidence_status="insufficient",
            ),
        ]

        coverage = compute_evidence_backed_coverage(
            competency_scores=cs_list,
            rubric=job,
            candidate_id="cand_001",
        )
        assert coverage == pytest.approx(0.4)

    def test_4_empty_competency_scores(self):
        """4. Empty competency scores: returns 0.0 deterministically."""
        job = _make_job({"Python": 1.0})
        coverage = compute_evidence_backed_coverage(
            competency_scores=[],
            rubric=job,
            candidate_id="cand_001",
        )
        assert coverage == 0.0

    def test_5_empty_evidence(self):
        """5. Empty evidence: competency score has evidence=[] -> counts as ungrounded."""
        job = _make_job({"Python": 1.0})
        cs = CompetencyScore(
            competency_name="Python",
            score=8.0,
            confidence=0.8,
            explanation="Claimed without evidence",
            evidence=[],
            evidence_status="supported",  # Even if status was mistakenly set to supported
        )

        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs],
            rubric=job,
            candidate_id="cand_001",
        )
        assert coverage == 0.0

    def test_6_insufficient_evidence(self):
        """6. Insufficient evidence: explicit evidence_status='insufficient' or item with 'insufficient' type."""
        job = _make_job({"Python": 1.0})
        ev_insufficient = _make_evidence(
            question_id="q1",
            competency="Python",
            evidence_type="insufficient",
        )
        cs = CompetencyScore(
            competency_name="Python",
            score=5.0,
            confidence=0.5,
            explanation="No transcript exchange addressed this",
            evidence=[ev_insufficient],
            evidence_status="insufficient",
        )

        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs],
            rubric=job,
            candidate_id="cand_001",
        )
        assert coverage == 0.0

    def test_7_valid_supporting_evidence(self):
        """7. Valid supporting evidence: correctly recognized as grounded."""
        job = _make_job({"Python": 1.0})
        ev = _make_evidence(question_id="q1", competency="Python", evidence_type="supporting")
        cs = CompetencyScore(
            competency_name="Python",
            score=9.0,
            confidence=0.9,
            explanation="Supported by transcript",
            evidence=[ev],
            evidence_status="supported",
        )

        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs],
            rubric=job,
            candidate_id="cand_001",
        )
        assert coverage == 1.0

    def test_8_valid_contradicting_evidence_grounded(self):
        """8. Valid contradicting evidence: repository considers contradicting evidence grounded."""
        job = _make_job({"Python": 1.0})
        ev_contradicting = _make_evidence(
            question_id="q1",
            competency="Python",
            evidence_type="contradicting",
            text="Candidate stated they have never used Python before.",
        )
        cs = CompetencyScore(
            competency_name="Python",
            score=3.0,
            confidence=0.9,
            explanation="Grounded negative evidence",
            evidence=[ev_contradicting],
            evidence_status="supported",
        )

        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs],
            rubric=job,
            candidate_id="cand_001",
        )
        assert coverage == 1.0

    def test_9_invalid_or_missing_question_references(self):
        """9. Invalid or missing question references:
        a) transcript evidence missing question_id -> ungrounded
        b) transcript evidence with fabricated question_id not in transcript -> ungrounded."""
        job = _make_job({"Python": 0.5, "System Design": 0.5})
        transcript = _make_transcript()

        # Item with missing question_id
        ev_no_qid = EvidenceItem(
            evidence_id="ev_no_qid",
            source_type="transcript",
            question_id=None,
            text="Some quote",
            relevance=0.8,
            agent="technical_evaluator",
            explanation="Missing qid",
        )
        cs1 = CompetencyScore(
            competency_name="Python",
            score=8.0,
            confidence=0.8,
            explanation="Missing qid",
            evidence=[ev_no_qid],
        )

        # Item with fake question_id verified against real transcript
        ev_fake_qid = EvidenceItem(
            evidence_id="ev_fake_qid",
            source_type="transcript",
            question_id="q999_fabricated",
            text="Fabricated quote",
            relevance=0.8,
            agent="technical_evaluator",
            explanation="Fake qid",
        )
        cs2 = CompetencyScore(
            competency_name="System Design",
            score=8.0,
            confidence=0.8,
            explanation="Fake qid",
            evidence=[ev_fake_qid],
        )

        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs1, cs2],
            rubric=job,
            candidate_id="cand_001",
            transcript=transcript,
        )
        assert coverage == 0.0

    def test_10_multiple_evidence_items_for_one_competency(self):
        """10. Multiple evidence items for one competency:
        Competency weight is counted once (not multiplied) even if multiple valid items exist."""
        job = _make_job({"Python": 1.0})
        ev1 = _make_evidence(question_id="q1", competency="Python", text="First piece of evidence.")
        ev2 = _make_evidence(question_id="q1", competency="Python", text="Second piece of evidence.")

        cs = CompetencyScore(
            competency_name="Python",
            score=9.0,
            confidence=0.9,
            explanation="Two evidence items",
            evidence=[ev1, ev2],
        )

        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs],
            rubric=job,
            candidate_id="cand_001",
        )
        assert coverage == 1.0

    def test_10b_duplicate_evidence_items(self):
        """Duplicate evidence items are handled cleanly without multiplying coverage."""
        job = _make_job({"Python": 1.0})
        ev1 = _make_evidence(question_id="q1", competency="Python")

        cs = CompetencyScore(
            competency_name="Python",
            score=9.0,
            confidence=0.9,
            explanation="Duplicate evidence items",
            evidence=[ev1, ev1],
        )

        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs],
            rubric=job,
            candidate_id="cand_001",
        )
        assert coverage == 1.0

    def test_11_unmatched_competencies(self):
        """11. Unmatched competencies:
        An evaluator score for a competency not declared in the job's rubric
        does not belong to the rubric and is excluded from the evaluated profile.
        Only applicable (rubric-matched) competencies determine the denominator."""
        job = _make_job({"Python": 0.5, "System Design": 0.5})

        ev_python = _make_evidence(question_id="q1", competency="Python")
        ev_docker = _make_evidence(question_id="q1", competency="Docker", text="I use Docker.")

        cs_python = CompetencyScore(
            competency_name="Python",
            score=9.0,
            confidence=0.9,
            explanation="In rubric",
            evidence=[ev_python],
        )
        cs_docker = CompetencyScore(
            competency_name="Docker",  # Not in job rubric
            score=8.0,
            confidence=0.8,
            explanation="Not in rubric",
            evidence=[ev_docker],
        )

        # Candidate's evaluated rubric profile is ONLY Python (weight 0.5).
        # Since Python has valid evidence, 100% of candidate's evaluated profile is backed by evidence!
        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs_python, cs_docker],
            rubric=job,
            candidate_id="cand_001",
        )
        assert coverage == pytest.approx(1.0)

    def test_11b_all_unmatched_competencies_yields_zero(self):
        """If all evaluated competencies are unmatched, total applicable weight is zero -> 0.0."""
        job = _make_job({"Python": 1.0})
        cs_unmatched = CompetencyScore(
            competency_name="Cooking",
            score=10.0,
            confidence=1.0,
            explanation="Unrelated skill",
            evidence=[_make_evidence(competency="Cooking")],
        )
        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs_unmatched],
            rubric=job,
            candidate_id="cand_001",
        )
        assert coverage == 0.0

    def test_12_non_normalized_weights(self):
        """12. Non-normalized weights:
        Weights do not sum to 1.0 (e.g. raw point weights 5.0 and 15.0).
        Coverage normalizes cleanly to [0.0, 1.0] by dividing by total applicable weight."""
        weights = {"Python": 5.0, "System Design": 15.0}  # sum = 20.0
        ev_python = _make_evidence(question_id="q1", competency="Python")

        cs_list = [
            CompetencyScore(
                competency_name="Python", score=9.0, confidence=0.9, explanation="ok", evidence=[ev_python]
            ),
            CompetencyScore(
                competency_name="System Design", score=7.0, confidence=0.7, explanation="ok", evidence=[]
            ),
        ]

        coverage = compute_evidence_backed_coverage(
            competency_scores=cs_list,
            rubric=weights,
            candidate_id="cand_001",
        )
        # 5.0 / (5.0 + 15.0) = 5.0 / 20.0 = 0.25
        assert coverage == pytest.approx(0.25)
        assert 0.0 <= coverage <= 1.0

    def test_candidate_id_mismatch_invalidates_evidence(self):
        """Evidence stamped with another candidate's ID fails validation."""
        ev = _make_evidence(question_id="q1", candidate_id="cand_wrong")
        assert is_valid_evidence(ev, candidate_id="cand_001") is False

        cs = CompetencyScore(
            competency_name="Python",
            score=9.0,
            confidence=0.9,
            explanation="Wrong candidate",
            evidence=[ev],
        )
        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs],
            rubric={"Python": 1.0},
            candidate_id="cand_001",
        )
        assert coverage == 0.0

    def test_zero_total_applicable_weight(self):
        """Rubric with zero weights returns 0.0 without ZeroDivisionError."""
        coverage = compute_evidence_backed_coverage(
            competency_scores=[
                CompetencyScore(competency_name="Python", score=8.0, confidence=0.8, explanation="ok")
            ],
            rubric={"Python": 0.0},
            candidate_id="cand_001",
        )
        assert coverage == 0.0

    def test_is_competency_evidence_backed_helper(self):
        """Direct tests for is_competency_evidence_backed helper."""
        ev = _make_evidence()
        cs_supported = CompetencyScore(
            competency_name="Python", score=9.0, confidence=0.9, explanation="ok", evidence=[ev]
        )
        assert is_competency_evidence_backed(cs_supported, candidate_id="cand_001") is True

        cs_insufficient = CompetencyScore(
            competency_name="Python", score=9.0, confidence=0.9, explanation="ok",
            evidence=[ev], evidence_status="insufficient"
        )
        assert is_competency_evidence_backed(cs_insufficient, candidate_id="cand_001") is False
        assert is_competency_evidence_backed(None) is False

    def test_resume_sourced_evidence_is_valid(self):
        """Resume-sourced evidence without question_id is valid for evidence coverage."""
        ev_resume = create_evidence(
            source_type="resume",
            text="Led team building Python microservices.",
            agent="resume_evaluator",
            explanation="Resume section",
            candidate_id="cand_001",
            source_id="experience_sec_1",
        )
        cs = CompetencyScore(
            competency_name="Python", score=8.5, confidence=0.85, explanation="ok", evidence=[ev_resume]
        )
        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs],
            rubric={"Python": 1.0},
            candidate_id="cand_001",
        )
        assert coverage == 1.0


class TestUnstampedEvidenceRegression:
    """Regression tests for the Sourcery finding:

    When candidate_id is supplied, an EvidenceItem with candidate_id=None
    (unstamped) must NOT pass is_valid_evidence(), and must NOT contribute to
    evidence_backed_coverage.  validate_evidence_belongs_to_candidate() retains
    its original "None = not yet a violation" behaviour for all other callers.
    """

    # ------------------------------------------------------------------
    # Helper: build an unstamped EvidenceItem (candidate_id explicitly None)
    # ------------------------------------------------------------------
    @staticmethod
    def _make_unstamped(source_type: str = "resume") -> EvidenceItem:
        return EvidenceItem(
            evidence_id="ev_unstamped",
            source_type=source_type,
            text="Some valid evidence text without a candidate stamp.",
            relevance=0.8,
            agent="test_agent",
            explanation="Unstamped – no candidate_id",
            candidate_id=None,
        )

    # 1. candidate_id supplied + evidence.candidate_id=None => invalid
    def test_unstamped_evidence_invalid_when_candidate_id_provided(self):
        """Regression: unstamped evidence must be INVALID when caller supplies a candidate_id."""
        ev = self._make_unstamped()
        assert is_valid_evidence(ev, candidate_id="cand_001") is False

    # 2. candidate_id supplied + matching candidate_id => valid
    def test_stamped_matching_candidate_id_is_valid(self):
        """Regression: evidence stamped with the CORRECT candidate_id must remain valid."""
        ev = create_evidence(
            source_type="resume",
            text="Stamped, matching candidate evidence.",
            agent="test_agent",
            explanation="Correctly stamped",
            candidate_id="cand_001",
        )
        assert is_valid_evidence(ev, candidate_id="cand_001") is True

    # 3. candidate_id supplied + different candidate_id => invalid
    def test_stamped_wrong_candidate_id_is_invalid(self):
        """Regression: evidence stamped with a DIFFERENT candidate_id must be invalid."""
        ev = create_evidence(
            source_type="resume",
            text="Evidence belonging to another candidate.",
            agent="test_agent",
            explanation="Wrong stamp",
            candidate_id="cand_999",
        )
        assert is_valid_evidence(ev, candidate_id="cand_001") is False

    # 4. coverage does not count unstamped evidence
    def test_coverage_excludes_unstamped_evidence(self):
        """Regression: compute_evidence_backed_coverage must NOT count unstamped evidence
        as valid when candidate_id is provided, even though the evidence text is valid."""
        ev_unstamped = self._make_unstamped()
        cs = CompetencyScore(
            competency_name="Python",
            score=9.0,
            confidence=0.9,
            explanation="Grounded by unstamped evidence – should not count",
            evidence=[ev_unstamped],
        )
        coverage = compute_evidence_backed_coverage(
            competency_scores=[cs],
            rubric={"Python": 1.0},
            candidate_id="cand_001",
        )
        assert coverage == 0.0

    # 5. validate_evidence_belongs_to_candidate global semantic is UNCHANGED
    def test_validate_evidence_belongs_to_candidate_still_allows_none(self):
        """Regression: the lower-level helper must keep treating None as 'not yet a
        violation' so existing pipeline callers are unaffected."""
        from utils.evidence import validate_evidence_belongs_to_candidate

        ev_unstamped = self._make_unstamped()
        # Global helper: None is NOT a violation (original behaviour preserved)
        assert validate_evidence_belongs_to_candidate(ev_unstamped, "cand_001") is True

    # 6. Without candidate_id, unstamped evidence still passes is_valid_evidence
    def test_unstamped_evidence_valid_when_no_candidate_id_required(self):
        """Regression: if no candidate_id is supplied at all, unstamped evidence must
        still pass is_valid_evidence (no regression on the no-candidate_id path)."""
        ev = self._make_unstamped()
        assert is_valid_evidence(ev) is True


class TestScoringAgentEvidenceCoverageIntegration:
    """Integration and regression tests for ScoringAgent and CandidateScores."""

    @pytest.mark.asyncio
    async def test_13_regression_weighted_final_score_is_unchanged(self):
        """13. Regression proving weighted_final_score is unchanged:
        Scoring the exact same candidate scores with and without valid evidence
        produces the exact same weighted_final_score when evaluated against the rubric,
        while evidence_backed_coverage reflects the evidence grounding."""
        job = _make_job({"Python": 0.5, "System Design": 0.5})
        matching = MatchingScore(
            match_id="m1", candidate_id="cand_001", job_id="job_001",
            match_score=0.8, experience_match=0.8, skill_gap=0.2, explanation="ok", confidence=0.8,
        )

        ev_python = _make_evidence(question_id="q1", competency="Python")
        ev_design = _make_evidence(question_id="q2", competency="System Design", text="Kafka design.")

        # Candidate A: fully grounded (100% evidence coverage)
        tech_eval_grounded = TechnicalEvaluation(
            evaluation_id="t1", candidate_id="cand_001", job_id="job_001", interview_id="i1",
            technical_score=8.5, explanation="Grounded", confidence=0.9,
            competency_scores=[
                CompetencyScore(
                    competency_name="Python", score=9.0, confidence=0.9, explanation="ok", evidence=[ev_python]
                ),
                CompetencyScore(
                    competency_name="System Design", score=8.0, confidence=0.8, explanation="ok", evidence=[ev_design]
                ),
            ],
        )

        # Candidate B: same competency scores and scores, but no evidence (0% evidence coverage)
        tech_eval_ungrounded = TechnicalEvaluation(
            evaluation_id="t2", candidate_id="cand_001", job_id="job_001", interview_id="i1",
            technical_score=8.5, explanation="Ungrounded", confidence=0.9,
            competency_scores=[
                CompetencyScore(
                    competency_name="Python", score=9.0, confidence=0.9, explanation="ok", evidence=[]
                ),
                CompetencyScore(
                    competency_name="System Design", score=8.0, confidence=0.8, explanation="ok", evidence=[]
                ),
            ],
        )

        behav_eval = BehavioralEvaluation(
            evaluation_id="b1", candidate_id="cand_001", job_id="job_001", interview_id="i1",
            behavioral_score=7.0, communication=7.0, problem_solving=7.0, teamwork=7.0, adaptability=7.0,
            explanation="ok", confidence=0.9,
        )

        agent = ScoringAgent()

        res_a = await agent.execute(
            job_description=job,
            technical_evaluation=tech_eval_grounded,
            behavioral_evaluation=behav_eval,
            matching_score=matching,
            candidate_id="cand_001",
        )
        scores_a: CandidateScores = res_a["candidate_scores"]

        res_b = await agent.execute(
            job_description=job,
            technical_evaluation=tech_eval_ungrounded,
            behavioral_evaluation=behav_eval,
            matching_score=matching,
            candidate_id="cand_001",
        )
        scores_b: CandidateScores = res_b["candidate_scores"]

        # 1. weighted_final_score is identical between both runs: (9.0*0.5 + 8.0*0.5) = 8.5
        assert scores_a.weighted_final_score == pytest.approx(8.5)
        assert scores_b.weighted_final_score == pytest.approx(8.5)
        assert scores_a.weighted_final_score == scores_b.weighted_final_score

        # 2. evidence_backed_coverage distinguishes grounded vs ungrounded
        assert scores_a.evidence_backed_coverage == pytest.approx(1.0)
        assert scores_b.evidence_backed_coverage == 0.0

    @pytest.mark.asyncio
    async def test_evidence_backed_coverage_with_transcript_in_scoring_agent(self):
        """ScoringAgent passes kwargs transcript through to validation."""
        job = _make_job({"Python": 1.0})
        transcript = _make_transcript()
        ev_valid = _make_evidence(question_id="q1", competency="Python")

        tech_eval = TechnicalEvaluation(
            evaluation_id="t1", candidate_id="cand_001", job_id="job_001", interview_id="i1",
            technical_score=9.0, explanation="ok", confidence=0.9,
            competency_scores=[
                CompetencyScore(
                    competency_name="Python", score=9.0, confidence=0.9, explanation="ok", evidence=[ev_valid]
                ),
            ],
        )
        behav_eval = BehavioralEvaluation(
            evaluation_id="b1", candidate_id="cand_001", job_id="job_001", interview_id="i1",
            behavioral_score=7.0, communication=7.0, problem_solving=7.0, teamwork=7.0, adaptability=7.0,
            explanation="ok", confidence=0.9,
        )
        matching = MatchingScore(
            match_id="m1", candidate_id="cand_001", job_id="job_001",
            match_score=0.8, experience_match=0.8, skill_gap=0.2, explanation="ok", confidence=0.8,
        )

        agent = ScoringAgent()
        res = await agent.execute(
            job_description=job,
            technical_evaluation=tech_eval,
            behavioral_evaluation=behav_eval,
            matching_score=matching,
            candidate_id="cand_001",
            interview_transcript=transcript,
        )
        scores = res["candidate_scores"]
        assert scores.evidence_backed_coverage == pytest.approx(1.0)

    def test_candidate_scores_model_default_evidence_backed_coverage(self):
        """CandidateScores defaults evidence_backed_coverage to 0.0 when not provided."""
        scores = CandidateScores(
            score_id="s1",
            candidate_id="cand_001",
            job_id="job_001",
            technical_score=8.0,
            behavioral_score=7.0,
            job_fit_score=7.5,
            weighted_final_score=7.5,
            explanation="Test explanation",
            confidence=0.9,
        )
        assert scores.evidence_backed_coverage == 0.0
