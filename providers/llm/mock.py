"""
Mock LLM provider for development and testing.
"""
import json
import re
from typing import Any, Dict

from providers.base import LLMProvider


class MockLLMProvider(LLMProvider):
    """Mock LLM that returns deterministic responses for testing."""

    def __init__(self):
        self.call_count = 0

    async def generate(self, prompt: str, **kwargs) -> str:
        """Generate deterministic mock responses.

        Dispatch is keyed on each prompt template's own fixed title header
        (e.g. "# Resume Auditor Agent Prompt"), not on generic keywords.
        Prompts embed arbitrary transcript/resume free text, and a candidate's
        interview answer can easily contain words like "question" or
        "extract" incidentally - matching on those caused prompts to be
        silently misrouted to the wrong mock response. Template titles are
        fixed strings we control, so they can't collide with candidate text.
        """
        self.call_count += 1
        prompt_lower = prompt.lower()

        if "structured information from this resume" in prompt_lower:
            # Built inline in ResumeParserAgent, not loaded from a prompts/*.md file.
            return self._mock_resume_parse_response(prompt)
        elif "# jd analyzer agent prompt" in prompt_lower:
            return self._mock_jd_response(prompt)
        elif "# technical evaluator agent prompt" in prompt_lower:
            return self._mock_technical_evaluation(prompt)
        elif "# behavioral evaluator agent prompt" in prompt_lower:
            return self._mock_behavioral_evaluation(prompt)
        elif "# interview question generation prompt" in prompt_lower:
            return self._mock_interview_question(prompt)
        elif "# resume auditor agent prompt" in prompt_lower:
            return self._mock_claim_verification(prompt)
        elif "# integrity agent prompt" in prompt_lower:
            return self._mock_integrity_check(prompt)
        elif "# bias checker agent prompt" in prompt_lower:
            return self._mock_bias_check(prompt)
        elif "score" in prompt_lower or "ranking" in prompt_lower:
            return self._mock_scoring(prompt)
        elif "report" in prompt_lower or "summary" in prompt_lower:
            return self._mock_report(prompt)
        else:
            return "Mock response to prompt"

    async def generate_structured(
        self, prompt: str, schema: Dict[str, Any], **kwargs
    ) -> Dict[str, Any]:
        """Generate structured mock responses matching schema."""
        response = await self.generate(prompt, **kwargs)
        # Try to parse as JSON, otherwise create mock structure
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return self._create_mock_structure(schema, prompt)

    def _mock_resume_parse_response(self, prompt: str) -> str:
        """Mock resume parsing. Keys match schemas.resume field names
        (Education.field_of_study, WorkExperience.start_year, etc.)."""
        return json.dumps(
            {
                "email": "candidate@example.com",
                "phone": "+1-555-0100",
                "location": "Remote",
                "summary": "Experienced software engineer.",
                "education": [
                    {
                        "institution": "State University",
                        "degree": "Bachelor of Science",
                        "field_of_study": "Computer Science",
                        "graduation_year": 2018,
                    }
                ],
                "work_experience": [
                    {
                        "company": "TechCorp",
                        "position": "Senior Backend Engineer",
                        "start_year": 2021,
                        "is_current": True,
                        "duration_months": 30,
                        "description": "Led development of core microservices.",
                        "responsibilities": [],
                        "achievements": ["Improved system throughput 10x"],
                    }
                ],
                "projects": [],
                "certifications": [],
                "skills": ["Python", "FastAPI", "SQL", "REST APIs", "Docker"],
                "technologies": ["PostgreSQL", "Redis", "AWS"],
                "languages": ["English"],
                "total_experience_years": 6,
            }
        )

    def _mock_jd_response(self, prompt: str) -> str:
        """Mock job description analysis."""
        return json.dumps(
            {
                "job_id": "job_001",
                "title": "Python Backend Developer",
                "required_skills": ["Python", "FastAPI", "SQL", "REST APIs"],
                "preferred_skills": ["Docker", "Kubernetes", "PostgreSQL"],
                "experience_years": 3,
                "competencies": [
                    {"name": "Python", "weight": 0.30},
                    {"name": "Backend Development", "weight": 0.30},
                    {"name": "SQL", "weight": 0.20},
                    {"name": "Problem Solving", "weight": 0.20},
                ],
                "interview_topics": ["Python fundamentals", "API design", "SQL queries"],
            }
        )

    def _mock_technical_evaluation(self, prompt: str) -> str:
        """Mock technical evaluation. competency_scores is keyed by competency
        name (technical_evaluator does `competency_scores.get(comp.name, {})`
        for each competency in the job's rubric)."""
        return json.dumps(
            {
                "technical_score": 8.0,
                "competency_scores": {
                    "Python": {
                        "score": 8.5,
                        "confidence": 0.85,
                        "explanation": "Strong Python fundamentals demonstrated",
                    },
                    "SQL": {
                        "score": 7.5,
                        "confidence": 0.80,
                        "explanation": "Good SQL query writing",
                    },
                },
                "strengths": ["Problem solving", "Code quality"],
                "weaknesses": ["System design"],
                "explanation": "Candidate shows strong technical skills",
            }
        )

    def _mock_behavioral_evaluation(self, prompt: str) -> str:
        """Mock behavioral evaluation."""
        return json.dumps(
            {
                "behavioral_score": 7.5,
                "communication": 8.0,
                "problem_solving": 8.5,
                "teamwork": 7.0,
                "adaptability": 7.5,
                "competency_scores": {
                    "Communication": {
                        "score": 8.0,
                        "confidence": 0.80,
                        "explanation": "Clear and structured communication",
                    },
                },
                "strengths": ["Clear communication", "Collaborative"],
                "weaknesses": ["Could improve on conflict resolution"],
                "explanation": "Candidate demonstrates good behavioral competencies",
            }
        )

    def _mock_interview_question(self, prompt: str) -> str:
        """Mock interview question generation."""
        return json.dumps(
            {
                "question_id": "q_001",
                "question_text": "Can you walk us through a recent project where you used Python?",
                "category": "technical",
                "competency": "Python",
                "difficulty": "medium",
                "reason": "To assess practical Python experience",
            }
        )

    def _mock_claim_verification(self, prompt: str) -> str:
        """Mock claim verification."""
        return json.dumps(
            {
                "verification_status": "supported",
                "confidence": 0.87,
                "explanation": "Interview response aligns with resume claim",
                "requires_human_review": False,
            }
        )

    def _mock_integrity_check(self, prompt: str) -> str:
        """Mock integrity check."""
        return json.dumps(
            {
                "flags": [],
                "overall_integrity": "clear",
                "explanation": "No inconsistencies detected",
                "requires_human_review": False,
            }
        )

    def _mock_bias_check(self, prompt: str) -> str:
        """Mock bias audit. Key is "flags" (not "bias_flags") to match what
        BiasCheckerAgent reads off the response."""
        return json.dumps(
            {
                "flags": [],
                "fairness_status": "pass",
                "explanation": "Evaluation appears fair and job-relevant",
                "requires_human_review": False,
            }
        )

    def _mock_scoring(self, prompt: str) -> str:
        """Mock scoring."""
        return json.dumps(
            {
                "technical_score": 8.0,
                "behavioral_score": 7.5,
                "experience_score": 8.5,
                "job_fit_score": 8.2,
                "weighted_final_score": 81.5,
                "confidence": 0.85,
            }
        )

    def _mock_report(self, prompt: str) -> str:
        """Mock report generation."""
        return "Mock comprehensive evaluation report for the candidate."

    def _create_mock_structure(self, schema: Dict[str, Any], prompt: str) -> Dict[str, Any]:
        """Create a mock structure matching the schema."""
        result = {}
        if "properties" in schema:
            for key, prop in schema["properties"].items():
                if prop.get("type") == "string":
                    result[key] = f"Mock {key}"
                elif prop.get("type") == "number":
                    result[key] = 0.5
                elif prop.get("type") == "integer":
                    result[key] = 1
                elif prop.get("type") == "boolean":
                    result[key] = True
                elif prop.get("type") == "array":
                    result[key] = []
                else:
                    result[key] = None
        return result
