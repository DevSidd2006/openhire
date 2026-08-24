"""
Interview related schemas.
"""
from typing import List, Optional
from pydantic import BaseModel, Field


class InterviewQuestion(BaseModel):
    """Interview question."""
    question_id: str
    question_text: str
    category: str  # "technical", "behavioral", "role_specific", "follow_up"
    competency: Optional[str] = None
    difficulty: Optional[str] = None  # "easy", "medium", "hard"
    reason: Optional[str] = None  # Why this question was asked
    expected_duration_seconds: Optional[int] = None


class InterviewAnswer(BaseModel):
    """Candidate's answer to a question."""
    question_id: str
    answer_text: str
    duration_seconds: Optional[float] = None
    timestamp_start: Optional[float] = None  # In seconds from start
    timestamp_end: Optional[float] = None
    answer_quality: Optional[str] = None  # filled by evaluators


class InterviewTranscript(BaseModel):
    """Interview transcript."""
    interview_id: str
    candidate_id: str
    job_id: str
    start_time: str
    end_time: Optional[str] = None
    duration_seconds: Optional[int] = None
    
    # Question-answer pairs
    exchanges: List[tuple[InterviewQuestion, InterviewAnswer]] = Field(default_factory=list)
    
    # Interview metadata
    interviewer_name: Optional[str] = None
    interview_type: Optional[str] = None  # "initial", "technical", "behavioral", "final"
    format: Optional[str] = None  # "voice", "video", "text"
    
    # Sealed status
    is_sealed: bool = False
    seal_timestamp: Optional[str] = None
    
    # Raw transcript text
    raw_transcript: Optional[str] = None


class InterviewEvaluation(BaseModel):
    """Placeholder for interview evaluation context."""
    interview_id: str
    candidate_id: str
    job_id: str
    transcript: InterviewTranscript


class InterviewState(BaseModel):
    """State of an ongoing interview."""
    interview_id: str
    candidate_id: str
    job_id: str
    current_question_index: int = 0
    questions_asked: int = 0
    exchanges: List[tuple[InterviewQuestion, InterviewAnswer]] = Field(default_factory=list)
    conversation_history: List[str] = Field(default_factory=list)
    start_time: str
    last_activity_time: str
    is_completed: bool = False
