"""Tests for P0-3 (scoring uses the job's actual competency rubric, and
ONLY that rubric - see agents/scoring/agent.py module docstring for why
job_fit_score is deliberately excluded from weighted_final_score) and P0-4
(a missing evaluation is never silently scored as if it were average)."""
import pytest

from agents.scoring.agent import ScoringAgent
from schemas.job import JobDescription, Competency
from schemas.evaluation import (
    TechnicalEvaluation,
    BehavioralEvaluation,
    CompetencyScore,
    MatchingScore,
)


def _job(weights):
    """weights: dict of competency name -> weight, must sum to 1.0."""
    return JobDescription(
        job_id="job_rubric_test",
        title="Test Role",
        description="Test",
        competencies=[Competency(name=name, weight=w) for name, w in weights.items()],
    )


def _tech_eval(competency_scores, technical_score=7.0):
    return TechnicalEvaluation(
        evaluation_id="tech_1",
        candidate_id="cand_001",
        job_id="job_rubric_test",
        interview_id="int_001",
        competency_scores=competency_scores,
        technical_score=technical_score,
        explanation="test",
        confidence=0.8,
    )


def _behav_eval(competency_scores=None, behavioral_score=7.0):
    return BehavioralEvaluation(
        evaluation_id="behav_1",
        candidate_id="cand_001",
        job_id="job_rubric_test",
        interview_id="int_001",
        competency_scores=competency_scores or [],
        behavioral_score=behavioral_score,
        communication=7.0,
        problem_solving=7.0,
        teamwork=7.0,
        adaptability=7.0,
        explanation="test",
        confidence=0.8,
    )


def _matching_score(match_score=0.8):
    return MatchingScore(
        match_id="match_1",
        candidate_id="cand_001",
        job_id="job_rubric_test",
        match_score=match_score,
        experience_match=0.8,
        skill_gap=0.2,
        explanation="test",
        confidence=0.8,
    )


class TestRubricDrivenScoring:
    @pytest.mark.asyncio
    async def test_a_changing_jd_weights_changes_rubric_score(self):
        """(A) Same evaluator scores, different job rubric weights -> different
        rubric_score. This is the load-bearing proof for P0-3: the rubric
        weights must actually influence the outcome, not just exist."""
        competency_scores = [
            CompetencyScore(competency_name="Python", score=9.0, confidence=0.9, explanation="strong"),
            CompetencyScore(competency_name="System Design", score=5.0, confidence=0.7, explanation="weak"),
        ]
        tech_eval = _tech_eval(competency_scores)
        behav_eval = _behav_eval()
        matching = _matching_score()

        agent = ScoringAgent()

        job_favors_python = _job({"Python": 0.9, "System Design": 0.1})
        result_a = await agent.execute(
            job_description=job_favors_python,
            technical_evaluation=tech_eval,
            behavioral_evaluation=behav_eval,
            matching_score=matching,
            candidate_id="cand_001",
        )

        job_favors_design = _job({"Python": 0.1, "System Design": 0.9})
        result_b = await agent.execute(
            job_description=job_favors_design,
            technical_evaluation=tech_eval,
            behavioral_evaluation=behav_eval,
            matching_score=matching,
            candidate_id="cand_001",
        )

        score_a = result_a["candidate_scores"].weighted_final_score
        score_b = result_b["candidate_scores"].weighted_final_score

        assert score_a != score_b
        # Weighting the higher-scoring competency (Python=9.0) more heavily
        # must produce a HIGHER final score than weighting the lower-scoring
        # one (System Design=5.0) more heavily.
        assert score_a > score_b

    @pytest.mark.asyncio
    async def test_b_weighted_final_score_equals_rubric_score(self):
        """(B) weighted_final_score must equal rubric_score exactly - no
        blending with job_fit_score or anything else."""
        tech_eval = _tech_eval([
            CompetencyScore(competency_name="Python", score=9.0, confidence=0.9, explanation="ok"),
            CompetencyScore(competency_name="System Design", score=6.0, confidence=0.8, explanation="ok"),
        ])
        behav_eval = _behav_eval()
        job = _job({"Python": 0.7, "System Design": 0.3})

        agent = ScoringAgent()
        # Independently compute the expected rubric_score using the same
        # documented formula, rather than reaching into agent internals.
        expected_rubric_score = 0.7 * 9.0 + 0.3 * 6.0

        result = await agent.execute(
            job_description=job,
            technical_evaluation=tech_eval,
            behavioral_evaluation=behav_eval,
            matching_score=_matching_score(match_score=0.5),
            candidate_id="cand_001",
        )
        scores = result["candidate_scores"]
        assert scores.weighted_final_score == pytest.approx(expected_rubric_score)

    @pytest.mark.asyncio
    async def test_c_and_d_job_fit_score_does_not_affect_weighted_final_score(self):
        """(C)+(D) Regression test: same competency scores and same JD rubric,
        different job_fit_score (via a different match_score) -> IDENTICAL
        weighted_final_score. job_fit_score itself still varies and is still
        reported (not deleted)."""
        tech_eval = _tech_eval([
            CompetencyScore(competency_name="Python", score=8.5, confidence=0.9, explanation="ok"),
            CompetencyScore(competency_name="System Design", score=7.0, confidence=0.8, explanation="ok"),
        ])
        behav_eval = _behav_eval()
        job = _job({"Python": 0.6, "System Design": 0.4})
        agent = ScoringAgent()

        result_low_fit = await agent.execute(
            job_description=job,
            technical_evaluation=tech_eval,
            behavioral_evaluation=behav_eval,
            matching_score=_matching_score(match_score=0.1),
            candidate_id="cand_001",
        )
        result_high_fit = await agent.execute(
            job_description=job,
            technical_evaluation=tech_eval,
            behavioral_evaluation=behav_eval,
            matching_score=_matching_score(match_score=0.99),
            candidate_id="cand_001",
        )

        scores_low = result_low_fit["candidate_scores"]
        scores_high = result_high_fit["candidate_scores"]

        # job_fit_score itself DOES differ (still computed and reported)...
        assert scores_low.job_fit_score != scores_high.job_fit_score
        # ...but weighted_final_score must be completely unaffected by it,
        # and must equal the rubric_score computed independently here.
        expected_rubric_score = 0.6 * 8.5 + 0.4 * 7.0
        assert scores_low.weighted_final_score == pytest.approx(scores_high.weighted_final_score)
        assert scores_low.weighted_final_score == pytest.approx(expected_rubric_score)

    @pytest.mark.asyncio
    async def test_g_rubric_coverage_reflects_matched_weight_fraction(self):
        """(G) Only 'Python' (weight 0.6) is matched; 'System Design' (0.4)
        has no evaluator score, so coverage must be 0.6, not 1.0."""
        tech_eval = _tech_eval([
            CompetencyScore(competency_name="Python", score=8.0, confidence=0.8, explanation="ok"),
        ])
        behav_eval = _behav_eval()
        matching = _matching_score()
        job = _job({"Python": 0.6, "System Design": 0.4})

        agent = ScoringAgent()
        result = await agent.execute(
            job_description=job,
            technical_evaluation=tech_eval,
            behavioral_evaluation=behav_eval,
            matching_score=matching,
            candidate_id="cand_001",
        )
        scores = result["candidate_scores"]
        assert scores.rubric_coverage == pytest.approx(0.6)
        assert len(scores.competency_scores) == 1
        # coverage < 1.0 here, and weighted_final_score is still exactly
        # rubric_score (computed only over the matched 0.6 weight, renormalized).
        assert scores.weighted_final_score == pytest.approx(8.0)

    @pytest.mark.asyncio
    async def test_no_rubric_match_falls_back_transparently(self):
        """If nothing in the job's rubric matches an evaluator competency
        name, the fallback to avg(technical, behavioral) must be explicit in
        the explanation, not silently applied."""
        tech_eval = _tech_eval([], technical_score=6.0)
        behav_eval = _behav_eval(behavioral_score=8.0)
        matching = _matching_score()
        job = _job({"Some Other Skill": 1.0})

        agent = ScoringAgent()
        result = await agent.execute(
            job_description=job,
            technical_evaluation=tech_eval,
            behavioral_evaluation=behav_eval,
            matching_score=matching,
            candidate_id="cand_001",
        )
        scores = result["candidate_scores"]
        assert scores.rubric_coverage == 0.0
        assert "falls back" in scores.explanation.lower()
        # Still purely rubric-derived (the fallback average), not blended
        # with job_fit_score.
        assert scores.weighted_final_score == pytest.approx((6.0 + 8.0) / 2)


class TestMissingEvaluationNeverBecomesAverage:
    """(E)+(F) P0-4: a missing technical/behavioral evaluation must never
    silently produce a normal-looking ~7.0 score - it must fail explicitly."""

    @pytest.mark.asyncio
    async def test_missing_technical_evaluation_raises(self):
        agent = ScoringAgent()
        with pytest.raises(ValueError, match="technical_evaluation"):
            await agent.execute(
                job_description=_job({"Python": 1.0}),
                technical_evaluation=None,
                behavioral_evaluation=_behav_eval(),
                matching_score=_matching_score(),
                candidate_id="cand_001",
            )

    @pytest.mark.asyncio
    async def test_missing_behavioral_evaluation_raises(self):
        agent = ScoringAgent()
        with pytest.raises(ValueError, match="behavioral_evaluation"):
            await agent.execute(
                job_description=_job({"Python": 1.0}),
                technical_evaluation=_tech_eval([]),
                behavioral_evaluation=None,
                matching_score=_matching_score(),
                candidate_id="cand_001",
            )

    @pytest.mark.asyncio
    async def test_both_missing_raises(self):
        agent = ScoringAgent()
        with pytest.raises(ValueError):
            await agent.execute(
                job_description=_job({"Python": 1.0}),
                technical_evaluation=None,
                behavioral_evaluation=None,
                matching_score=_matching_score(),
                candidate_id="cand_001",
            )

    @pytest.mark.asyncio
    async def test_run_wrapper_surfaces_missing_evaluation_as_explicit_error(self):
        """Through the BaseAgent.run() wrapper (as the graph actually calls
        it): no candidate_scores object is produced, and the failure is
        explicit - not a fabricated CandidateScores with 7.0 fields."""
        agent = ScoringAgent()
        outcome = await agent.run(
            run_id="run_test",
            job_description=_job({"Python": 1.0}),
            technical_evaluation=None,
            behavioral_evaluation=_behav_eval(),
            matching_score=_matching_score(),
            candidate_id="cand_001",
        )
        assert outcome["result"] is None
        assert outcome["error"] is not None
        assert outcome["audit_log"].status == "failed"

    @pytest.mark.asyncio
    async def test_valid_complete_evaluation_scores_normally(self):
        agent = ScoringAgent()
        result = await agent.execute(
            job_description=_job({"Python": 0.6, "System Design": 0.4}),
            technical_evaluation=_tech_eval([
                CompetencyScore(competency_name="Python", score=9.0, confidence=0.9, explanation="ok"),
                CompetencyScore(competency_name="System Design", score=8.0, confidence=0.8, explanation="ok"),
            ]),
            behavioral_evaluation=_behav_eval(),
            matching_score=_matching_score(match_score=0.9),
            candidate_id="cand_001",
        )
        scores = result["candidate_scores"]
        assert scores.rubric_coverage == pytest.approx(1.0)
        assert 0.0 <= scores.weighted_final_score <= 10.0
        # No component here happens to be 7.0, so this also guards against
        # a silent "everything defaults to 7.0" bug.
        assert scores.weighted_final_score != pytest.approx(7.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
