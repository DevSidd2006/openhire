"""Tests for utilities."""
import pytest

from utils.evidence import create_evidence, create_transcript_evidence
from utils.validation import (
    validate_weights,
    validate_score,
    validate_confidence,
)


class TestEvidenceCreation:
    """Test evidence creation utilities."""

    def test_create_evidence(self):
        """Test creating evidence item."""
        evidence = create_evidence(
            source_type="transcript",
            question_id="q001",
            text="Test evidence text",
            relevance=0.95,
            agent="technical_evaluator",
            explanation="This is relevant to Python skills"
        )
        
        assert evidence.source_type == "transcript"
        assert evidence.question_id == "q001"
        assert evidence.text == "Test evidence text"
        assert evidence.relevance == 0.95
        assert evidence.agent == "technical_evaluator"

    def test_create_transcript_evidence(self):
        """Test creating evidence from transcript."""
        from schemas.interview import InterviewQuestion, InterviewAnswer
        
        question = InterviewQuestion(
            question_id="q001",
            question_text="Tell us about your Python experience",
            category="technical",
            difficulty="medium",
            competency="Python",
            reason="",
            expected_duration_seconds=60,
        )
        
        answer = InterviewAnswer(
            question_id="q001",
            answer_text="I have 5 years of Python experience using asyncio",
            duration_seconds=45,
        )

        evidence = create_transcript_evidence(
            text=answer.answer_text,
            question_id=question.question_id,
            relevance=0.9,
            agent="technical_evaluator",
            explanation="Demonstrates Python expertise"
        )
        
        assert evidence.source_type == "transcript"
        assert evidence.question_id == "q001"
        assert "5 years" in evidence.text


class TestValidation:
    """Test validation utilities."""

    def test_validate_weights_sum_to_one(self):
        """Test weights sum to 1.0."""
        weights = [0.3, 0.3, 0.4]
        assert validate_weights(weights) == True
        
    def test_validate_weights_fail_out_of_range(self):
        """Test weights outside acceptable range."""
        weights = [0.3, 0.3, 0.3]  # Sum to 0.9
        assert validate_weights(weights) == False

    def test_validate_score_valid(self):
        """Test score validation."""
        assert validate_score(5.0) == True
        assert validate_score(0.0) == True
        assert validate_score(10.0) == True

    def test_validate_score_invalid(self):
        """Test invalid score."""
        assert validate_score(11.0) == False
        assert validate_score(-1.0) == False

    def test_validate_confidence_valid(self):
        """Test confidence validation."""
        assert validate_confidence(0.5) == True
        assert validate_confidence(0.0) == True
        assert validate_confidence(1.0) == True

    def test_validate_confidence_invalid(self):
        """Test invalid confidence."""
        assert validate_confidence(1.5) == False
        assert validate_confidence(-0.1) == False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
