"""
Job Description Analyzer Agent.
Extracts structured requirements from job descriptions.
"""
from typing import Any, Dict, Optional
import json

from agents.base import BaseAgent
from schemas.job import JobDescription
from utils.validation import validate_competencies


class JDAnalyzerAgent(BaseAgent):
    """Analyzes job descriptions and extracts structured requirements."""

    def __init__(self, **kwargs):
        super().__init__(name="jd_analyzer", **kwargs)

    async def execute(self, job_description: str, job_id: str = "job_001", **kwargs) -> Dict[str, Any]:
        """
        Extract structured job requirements from job description.
        
        Args:
            job_description: Raw job description text
            job_id: Unique job identifier
            
        Returns:
            Structured JobDescription object
        """
        self.logger.info(f"Analyzing job description: {job_id}")

        # Load prompt template
        prompt_template = self.load_prompt("jd_analyzer.md")
        prompt = prompt_template.format(job_description=job_description)

        # Call LLM
        response_text = await self.call_llm_generate(prompt)
        self.logger.debug(f"LLM response: {response_text[:200]}...")

        # Parse response
        try:
            # Extract JSON from response
            response_data = self._extract_json(response_text)

            # Add job_id
            response_data["job_id"] = job_id
            response_data["description"] = job_description

            # Validate competencies
            if "competencies" in response_data:
                is_valid, error = validate_competencies(response_data["competencies"])
                if not is_valid:
                    self.logger.warning(f"Competency validation failed: {error}. Normalizing...")
                    response_data["competencies"] = self._normalize_competencies(
                        response_data["competencies"]
                    )

            # Create JobDescription object
            job_desc = JobDescription(**response_data)
            self.logger.info(f"Successfully extracted {len(job_desc.competencies)} competencies")

            return {
                "job_description": job_desc,
                "competency_count": len(job_desc.competencies),
                "required_skills_count": len(job_desc.required_skills),
            }

        except Exception as e:
            self.logger.error(f"Failed to parse job description: {str(e)}")
            # Return mock structure on failure
            return {
                "job_description": self._create_default_job_description(job_id, job_description),
                "error": str(e),
            }

    def _extract_json(self, text: str) -> Dict[str, Any]:
        """Extract JSON from LLM response."""
        try:
            # Try direct JSON parse
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code blocks
            if "```json" in text:
                start = text.index("```json") + 7
                end = text.index("```", start)
                return json.loads(text[start:end].strip())
            elif "```" in text:
                start = text.index("```") + 3
                end = text.index("```", start)
                return json.loads(text[start:end].strip())
            else:
                raise ValueError("Could not extract JSON from response")

    def _normalize_competencies(self, competencies: list) -> list:
        """Normalize competency weights to sum to 1.0."""
        if not competencies:
            return competencies

        total_weight = sum(c.get("weight", 0) for c in competencies)
        if total_weight == 0:
            # Distribute equally
            equal_weight = 1.0 / len(competencies)
            for c in competencies:
                c["weight"] = equal_weight
        else:
            # Normalize
            for c in competencies:
                c["weight"] = c.get("weight", 0) / total_weight

        return competencies

    def _create_default_job_description(self, job_id: str, description: str) -> JobDescription:
        """Create a default job description on parsing failure."""
        return JobDescription(
            job_id=job_id,
            title="Unstructured Job",
            description=description,
            required_skills=[],
            preferred_skills=[],
            competencies=[
                {"name": "General Competency", "weight": 1.0}
            ],
        )
