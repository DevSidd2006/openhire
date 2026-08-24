"""Tests for schema validation."""
import pytest
from pydantic import ValidationError

from schemas.job import JobDescription, Competency
from schemas.resume import ParsedResume, WorkExperience, Education
from schemas.interview import InterviewTranscript, InterviewQuestion, InterviewAnswer
from schemas.scoring import CandidateScores
from schemas.evaluation import (
    TechnicalEvaluation,
    BehavioralEvaluation,
    CompetencyScore,
)


class TestJobDescription:
    """Test JobDescription schema."""

    def test_valid_job_description(self, sample_job_description):
        """Test valid job description."""
        assert sample_job_description.job_id == "job_test_001"
        assert sample_job_description.title == "Test Python Developer"
        assert len(sample_job_description.competencies) == 2

    def test_job_description_requires_job_id(self):
        """Test that job_id is required."""
        with pytest.raises(ValidationError):
            JobDescription(
                job_id=None,
                title="Test",
                description="Test",
                experience_years=1,
                required_skills=[],
                competencies=[]
            )

    def test_competency_weights_sum_to_one(self, sample_job_description):
        """Test that competency weights sum to 1.0 ±0.01."""
        total = sum(c.weight for c in sample_job_description.competencies)
        assert abs(total - 1.0) <= 0.01

    def test_competency_weights_normalized(self):
        """Test that weight normalization works."""
        competencies = [
            Competency(name="A", weight=0.7, importance="high"),
            Competency(name="B", weight=0.2, importance="high"),
            Competency(name="C", weight=0.1, importance="high"),
        ]
        job = JobDescription(
            job_id="job_001",
            title="Test",
            description="Test",
            experience_years=1,
            required_skills=[],
            competencies=competencies
        )
        total = sum(c.weight for c in job.competencies)
        assert abs(total - 1.0) <= 0.01


class TestParsedResume:
    """Test ParsedResume schema."""

    def test_valid_resume(self, sample_parsed_resume):
        """Test valid resume."""
        assert sample_parsed_resume.candidate_id == "cand_test_001"
        assert sample_parsed_resume.candidate_name == "Test Candidate"
        assert sample_parsed_resume.total_experience_years == 3
        assert len(sample_parsed_resume.skills) == 3
        assert "Python" in sample_parsed_resume.skills

    def test_resume_requires_candidate_id(self):
        """Test that candidate_id is required."""
        with pytest.raises(ValidationError):
            ParsedResume(
                candidate_id=None,
                candidate_name="Test",
                email="test@example.com",
                phone="555-0000",
                education=[],
                work_experience=[],
                projects=[],
                skills=[],
                raw_text="",
                total_experience_years=0
            )

    def test_work_experience_validation(self):
        """Test WorkExperience validation."""
        work_exp = WorkExperience(
            position="Developer",
            company="Test Corp",
            start_year=2022,
            duration_months=30,
            description="Test",
            achievements=["Achievement 1"],
        )
        assert work_exp.position == "Developer"
        assert work_exp.start_year == 2022


class TestInterviewTranscript:
    """Test InterviewTranscript schema."""

    def test_interview_transcript_creation(self):
        """Test creating interview transcript."""
        question = InterviewQuestion(
            question_id="q001",
            question_text="Test question?",
            category="technical",
            difficulty="medium",
            competency="Python",
            reason="Test reason",
            expected_duration_seconds=60,
        )
        
        answer = InterviewAnswer(
            question_id="q001",
            answer_text="Test answer",
            duration_seconds=45,
        )
        
        exchanges = [(question, answer)]
        
        transcript = InterviewTranscript(
            interview_id="int_001",
            candidate_id="cand_001",
            job_id="job_001",
            exchanges=exchanges,
            start_time="2024-01-15T10:00:00Z",
        )
        
        assert transcript.interview_id == "int_001"
        assert len(transcript.exchanges) == 1


class TestCandidateScores:
    """Test CandidateScores schema."""

    def test_valid_scores(self):
        """Test valid candidate scores."""
        scores = CandidateScores(
            score_id="score_001",
            candidate_id="cand_001",
            job_id="job_001",
            technical_score=8.5,
            behavioral_score=7.8,
            job_fit_score=8.0,
            weighted_final_score=8.1,
            percentile_rank=85.0,
            explanation="Test explanation",
            confidence=0.92,
        )

        assert scores.technical_score == 8.5
        assert scores.weighted_final_score == 8.1
        assert 0 <= scores.percentile_rank <= 100

    def test_scores_in_valid_range(self):
        """Test that scores must be 0-10."""
        with pytest.raises(ValidationError):
            CandidateScores(
                score_id="score_001",
                candidate_id="cand_001",
                job_id="job_001",
                technical_score=15.0,  # Out of range
                behavioral_score=7.0,
                job_fit_score=8.0,
                weighted_final_score=8.0,
                explanation="Test explanation",
                confidence=0.9,
            )

    def test_confidence_in_valid_range(self):
        """Test that confidence must be 0-1."""
        with pytest.raises(ValidationError):
            CandidateScores(
                score_id="score_001",
                candidate_id="cand_001",
                job_id="job_001",
                technical_score=8.0,
                behavioral_score=7.0,
                job_fit_score=8.0,
                weighted_final_score=8.0,
                explanation="Test explanation",
                confidence=1.5,  # Out of range
            )


class TestTechnicalEvaluation:
    """Test TechnicalEvaluation schema."""

    def test_technical_evaluation_creation(self):
        """Test creating technical evaluation."""
        competency_score = CompetencyScore(
            competency_name="Python",
            score=8.5,
            confidence=0.92,
            evidence=[],
            explanation="Strong Python skills demonstrated",
        )
        
        evaluation = TechnicalEvaluation(
            evaluation_id="eval_001",
            candidate_id="cand_001",
            job_id="job_001",
            interview_id="int_001",
            competency_scores=[competency_score],
            technical_score=8.5,
            strengths=["Strong async patterns"],
            weaknesses=["Limited GraphQL"],
            evidence=[],
            explanation="Strong technical performance",
            confidence=0.90,
        )
        
        assert evaluation.technical_score == 8.5
        assert len(evaluation.competency_scores) == 1
        assert evaluation.strengths[0] == "Strong async patterns"


class TestBehavioralEvaluation:
    """Test BehavioralEvaluation schema."""

    def test_behavioral_evaluation_creation(self):
        """Test creating behavioral evaluation."""
        evaluation = BehavioralEvaluation(
            evaluation_id="eval_001",
            candidate_id="cand_001",
            job_id="job_001",
            interview_id="int_001",
            communication=8.0,
            problem_solving=7.5,
            teamwork=8.5,
            adaptability=7.8,
            competency_scores=[],
            behavioral_score=8.0,
            strengths=["Good communicator"],
            weaknesses=["Could improve problem-solving"],
            evidence=[],
            explanation="Strong behavioral performance",
            confidence=0.88,
        )

        assert evaluation.behavioral_score == 8.0
        assert evaluation.communication == 8.0
        assert evaluation.teamwork == 8.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
