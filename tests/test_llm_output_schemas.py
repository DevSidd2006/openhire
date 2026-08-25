"""P1 schema tests: representative LLM output schemas (schemas/llm_outputs.py)
correctly reject invalid fields - this is the "agent/schema validation"
layer from Step 7 ("is the score in range? is severity a valid value?"),
distinct from provider-level JSON validity and from agent business logic."""
import pytest
from pydantic import ValidationError

from schemas.llm_outputs import (
    TechnicalEvaluationResult,
    BehavioralEvaluationResult,
    ClaimVerificationResult,
    IntegrityCheckResult,
    IntegrityFlagResult,
    BiasCheckResult,
    BiasFlagResult,
    CompetencyJudgment,
)


class TestTechnicalEvaluationResultValidation:
    def test_valid_result_passes(self):
        result = TechnicalEvaluationResult(
            technical_score=8.0,
            competency_scores={"Python": CompetencyJudgment(score=8.5, confidence=0.9)},
        )
        assert result.technical_score == 8.0

    def test_score_above_ten_rejected(self):
        with pytest.raises(ValidationError):
            TechnicalEvaluationResult(technical_score=15.0)

    def test_score_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            TechnicalEvaluationResult(technical_score=-1.0)

    def test_missing_required_technical_score_rejected(self):
        with pytest.raises(ValidationError):
            TechnicalEvaluationResult()

    def test_competency_judgment_confidence_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            CompetencyJudgment(score=5.0, confidence=1.5)


class TestBehavioralEvaluationResultValidation:
    def test_valid_result_passes(self):
        result = BehavioralEvaluationResult(behavioral_score=7.5)
        assert result.behavioral_score == 7.5

    def test_communication_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            BehavioralEvaluationResult(behavioral_score=7.0, communication=20.0)

    def test_missing_required_behavioral_score_rejected(self):
        with pytest.raises(ValidationError):
            BehavioralEvaluationResult()


class TestClaimVerificationResultValidation:
    def test_valid_status_passes(self):
        result = ClaimVerificationResult(verification_status="supported")
        assert result.verification_status == "supported"

    def test_invalid_status_literal_rejected(self):
        """verification_status must be one of the known enum values - a
        model hallucinating an unlisted status is a genuine schema
        violation, not a business-logic concern."""
        with pytest.raises(ValidationError):
            ClaimVerificationResult(verification_status="probably_true")

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            ClaimVerificationResult(verification_status="supported", confidence=2.0)


class TestIntegrityCheckResultValidation:
    def test_valid_flag_passes(self):
        flag = IntegrityFlagResult(flag_type="answer_inconsistency", severity="high", evidence_question_numbers=[1, 2])
        result = IntegrityCheckResult(flags=[flag], overall_integrity="flagged")
        assert result.overall_integrity == "flagged"
        assert result.flags[0].severity == "high"

    def test_invalid_severity_rejected(self):
        with pytest.raises(ValidationError):
            IntegrityFlagResult(flag_type="other", severity="catastrophic")

    def test_invalid_overall_integrity_literal_rejected(self):
        with pytest.raises(ValidationError):
            IntegrityCheckResult(overall_integrity="definitely_lying")

    def test_evidence_question_numbers_must_be_ints(self):
        with pytest.raises(ValidationError):
            IntegrityFlagResult(flag_type="other", evidence_question_numbers=["not-a-number"])


class TestBiasCheckResultValidation:
    def test_valid_flag_passes(self):
        flag = BiasFlagResult(bias_type="personality_assumption", severity="low", evidence_source="behavioral_evaluation")
        result = BiasCheckResult(flags=[flag], fairness_status="flagged")
        assert result.flags[0].evidence_source == "behavioral_evaluation"

    def test_invalid_evidence_source_literal_rejected(self):
        with pytest.raises(ValidationError):
            BiasFlagResult(bias_type="other", evidence_source="candidate_appearance")

    def test_invalid_fairness_status_rejected(self):
        with pytest.raises(ValidationError):
            BiasCheckResult(fairness_status="definitely_biased")

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            BiasFlagResult(bias_type="other", confidence=-0.5)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
