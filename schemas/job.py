"""
Job Description related schemas.
"""
from typing import List, Optional
from pydantic import BaseModel, Field, validator


class Competency(BaseModel):
    """Competency with weight."""
    name: str = Field(..., description="Competency name")
    weight: float = Field(..., ge=0.0, le=1.0, description="Weight in rubric (0-1)")
    level: Optional[str] = Field(None, description="Expected level: entry, intermediate, expert")
    importance: Optional[str] = Field(None, description="Importance: critical, high, medium, low")


class Requirement(BaseModel):
    """Job requirement."""
    id: str
    title: str
    category: str  # "required", "preferred", "nice_to_have"
    description: Optional[str] = None


class JobDescription(BaseModel):
    """Parsed and structured job description."""
    job_id: str
    title: str
    description: str
    department: Optional[str] = None
    level: Optional[str] = None  # entry, junior, mid, senior, principal
    
    # Extracted components
    required_skills: List[str] = Field(default_factory=list)
    preferred_skills: List[str] = Field(default_factory=list)
    required_qualifications: List[str] = Field(default_factory=list)
    preferred_qualifications: List[str] = Field(default_factory=list)
    experience_years: Optional[int] = None
    
    # Responsibilities
    responsibilities: List[str] = Field(default_factory=list)
    
    # Competencies with weights for evaluation
    competencies: List[Competency] = Field(default_factory=list)
    
    # Interview topics
    interview_topics: List[str] = Field(default_factory=list)
    
    # Rubric
    evaluation_rubric: Optional[dict] = Field(None, description="Structure for evaluation")
    
    # Metadata
    posting_date: Optional[str] = None
    closing_date: Optional[str] = None

    @validator("competencies")
    def validate_weights_sum(cls, v):
        """Validate that competency weights sum to approximately 1.0."""
        if v:
            total = sum(c.weight for c in v)
            if abs(total - 1.0) > 0.01:  # Allow 1% tolerance
                raise ValueError(f"Competency weights must sum to 1.0, got {total}")
        return v
