"""
Resume Parser Agent.
Extracts structured information from resumes.
"""
from typing import Any, Dict, Optional
import json

from agents.base import BaseAgent
from schemas.resume import ParsedResume
from datetime import datetime


class ResumeParserAgent(BaseAgent):
    """Parses and normalizes resume data."""

    def __init__(self, **kwargs):
        super().__init__(name="resume_parser", **kwargs)

    async def execute(
        self,
        resume_text: str,
        candidate_id: str,
        candidate_name: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Parse and structure resume data.
        
        Args:
            resume_text: Resume content (text or extracted from PDF)
            candidate_id: Unique candidate identifier
            candidate_name: Candidate's name
            
        Returns:
            Structured ParsedResume object
        """
        self.logger.info(f"Parsing resume for candidate: {candidate_id} ({candidate_name})")

        try:
            # For this implementation, create a structured resume from provided text
            # In production, would use more sophisticated parsing
            parsed_resume = await self._parse_resume_content(
                resume_text, candidate_id, candidate_name
            )

            return {
                "parsed_resume": parsed_resume,
                "skill_count": len(parsed_resume.skills),
                "experience_years": parsed_resume.total_experience_years,
            }

        except Exception as e:
            self.logger.error(f"Resume parsing failed: {str(e)}")
            return {
                "parsed_resume": self._create_minimal_resume(candidate_id, candidate_name, resume_text),
                "error": str(e),
            }

    async def _parse_resume_content(
        self, resume_text: str, candidate_id: str, candidate_name: str
    ) -> ParsedResume:
        """Parse resume content into structured format."""

        # Use LLM to extract structured data from resume text
        prompt = self._create_parsing_prompt(resume_text)
        response_text = await self.call_llm_generate(prompt)

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            # Fallback to manual parsing
            data = self._fallback_parse(resume_text)

        # Create ParsedResume with extracted data
        parsed_resume = ParsedResume(
            candidate_id=candidate_id,
            candidate_name=candidate_name,
            email=data.get("email"),
            phone=data.get("phone"),
            location=data.get("location"),
            summary=data.get("summary"),
            education=data.get("education", []),
            work_experience=data.get("work_experience", []),
            projects=data.get("projects", []),
            certifications=data.get("certifications", []),
            skills=data.get("skills", []),
            technologies=data.get("technologies", []),
            languages=data.get("languages", []),
            total_experience_years=data.get("total_experience_years"),
            raw_text=resume_text,
            parse_date=datetime.now().isoformat(),
        )

        return parsed_resume

    def _create_parsing_prompt(self, resume_text: str) -> str:
        """Create prompt for resume parsing."""
        return f"""
Extract structured information from this resume. Return valid JSON with:
- email, phone, location, summary
- education (list of education entries)
- work_experience (list of work experiences with dates, position, company, description)
- projects (list of projects)
- certifications (list)
- skills (list of skills)
- technologies (list of technologies/tools)
- languages (list)
- total_experience_years (estimated years of experience)

Resume:
{resume_text}

Return only valid JSON.
"""

    def _fallback_parse(self, resume_text: str) -> Dict[str, Any]:
        """Fallback parsing when LLM parsing fails."""
        return {
            "email": self._extract_email(resume_text),
            "phone": self._extract_phone(resume_text),
            "summary": resume_text[:200] if resume_text else None,
            "skills": self._extract_skills(resume_text),
            "education": [],
            "work_experience": [],
            "projects": [],
            "certifications": [],
            "total_experience_years": None,
        }

    def _extract_email(self, text: str) -> Optional[str]:
        """Extract email from text."""
        import re
        match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
        return match.group(0) if match else None

    def _extract_phone(self, text: str) -> Optional[str]:
        """Extract phone number from text."""
        import re
        match = re.search(r"[\+]?[(]?[0-9]{3}[)]?[-\s\.]?[0-9]{3}[-\s\.]?[0-9]{4,6}", text)
        return match.group(0) if match else None

    def _extract_skills(self, text: str) -> list[str]:
        """Extract common skills mentioned in resume."""
        common_skills = [
            "Python", "Java", "C++", "JavaScript", "SQL", "React", "Angular",
            "Node.js", "Django", "FastAPI", "Docker", "Kubernetes", "AWS",
            "Azure", "Git", "Linux", "Machine Learning", "Data Science",
            "REST APIs", "MongoDB", "PostgreSQL", "Redis", "Agile",
        ]
        text_lower = text.lower()
        found_skills = [skill for skill in common_skills if skill.lower() in text_lower]
        return found_skills[:10]  # Return top 10 found

    def _create_minimal_resume(
        self, candidate_id: str, candidate_name: str, resume_text: str
    ) -> ParsedResume:
        """Create minimal resume on parsing failure."""
        return ParsedResume(
            candidate_id=candidate_id,
            candidate_name=candidate_name,
            raw_text=resume_text,
            skills=self._extract_skills(resume_text),
            parse_date=datetime.now().isoformat(),
        )
