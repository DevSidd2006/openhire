"""
Resume related schemas.
"""
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field


class Education(BaseModel):
    """Education entry."""
    institution: str
    degree: str
    field_of_study: str
    graduation_year: Optional[int] = None
    gpa: Optional[float] = None
    honors: Optional[str] = None


class WorkExperience(BaseModel):
    """Work experience entry."""
    company: str
    position: str
    start_year: int
    end_year: Optional[int] = None  # None if current
    is_current: bool = False
    duration_months: Optional[int] = None
    description: Optional[str] = None
    responsibilities: List[str] = Field(default_factory=list)
    achievements: List[str] = Field(default_factory=list)


class Project(BaseModel):
    """Project entry."""
    name: str
    description: str
    technologies: List[str] = Field(default_factory=list)
    url: Optional[str] = None
    role: Optional[str] = None
    outcome: Optional[str] = None


class Certification(BaseModel):
    """Certification entry."""
    name: str
    issuer: Optional[str] = None
    issue_date: Optional[str] = None
    expiration_date: Optional[str] = None
    credential_url: Optional[str] = None


class ParsedResume(BaseModel):
    """Parsed and normalized resume."""
    candidate_id: str
    candidate_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    summary: Optional[str] = None
    
    # Core sections
    education: List[Education] = Field(default_factory=list)
    work_experience: List[WorkExperience] = Field(default_factory=list)
    projects: List[Project] = Field(default_factory=list)
    certifications: List[Certification] = Field(default_factory=list)
    
    # Extracted information
    skills: List[str] = Field(default_factory=list)
    technologies: List[str] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)
    total_experience_years: Optional[float] = None
    
    # Raw resume text (for matching)
    raw_text: Optional[str] = None
    
    # Metadata
    parse_date: Optional[str] = None
    source_format: Optional[str] = None  # pdf, docx, txt


class ResumeClaim(BaseModel):
    """Claimed achievement or responsibility from resume."""
    claim_id: str
    source: str  # job title, project name, achievement, etc.
    text: str
    claim_type: str  # "achievement", "responsibility", "skill_demonstration"
    quantifiable: bool = False
    metrics: Optional[List[str]] = Field(None, description="Extractable metrics like percentages, numbers")
