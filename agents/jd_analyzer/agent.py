"""
Job Description Analyzer Agent.
Extracts structured requirements from job descriptions.
"""
from typing import Any, Dict

from agents.base import BaseAgent
from schemas.job import JobDescription
from schemas.llm_outputs import JDAnalysisResult
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

        prompt_template = self.load_prompt("jd_analyzer.md")
        prompt = prompt_template.format(job_description=job_description)

        try:
            result: JDAnalysisResult = await self.call_llm_structured(
                prompt,
                schema=JDAnalysisResult.model_json_schema(),
                validate=JDAnalysisResult.model_validate,
            )

            # Business-logic repair (not schema validation): the LLM's
            # weights may not sum to exactly 1.0 even though each is
            # individually well-formed - normalize rather than fail the
            # whole analysis over a rounding issue. RawCompetency
            # deliberately has no weight range/sum constraint so this step
            # still gets a chance to run, same as before this migration.
            competencies = [c.model_dump() for c in result.competencies]
            is_valid, error = validate_competencies(competencies)
            if not is_valid:
                self.logger.warning(f"Competency validation failed: {error}. Normalizing...")
                competencies = self._normalize_competencies(competencies)

            job_desc = JobDescription(
                job_id=job_id,
                description=job_description,
                title=result.title,
                department=result.department,
                level=result.level,
                required_skills=result.required_skills,
                preferred_skills=result.preferred_skills,
                required_qualifications=result.required_qualifications,
                preferred_qualifications=result.preferred_qualifications,
                experience_years=result.experience_years,
                responsibilities=result.responsibilities,
                competencies=competencies,
                interview_topics=result.interview_topics,
            )
            self.logger.info(f"Successfully extracted {len(job_desc.competencies)} competencies")

            return {
                "job_description": job_desc,
                "competency_count": len(job_desc.competencies),
                "required_skills_count": len(job_desc.required_skills),
            }

        except Exception as e:
            # Never fabricate a plausible-looking JobDescription here - a
            # failed/invalid structured LLM result or a JobDescription
            # construction failure must surface as an explicit failure, not
            # a silently "successful" analysis. orchestration/graph.py's
            # node_analyze_job treats job_description=None as failure and
            # leaves state["job_description"] unset, which every downstream
            # node (matching, question generation, evaluation, scoring)
            # already treats as "nothing to do" with an explicit error -
            # the same P0-4 "exclude with an explicit reason" convention,
            # applied here to the JD itself rather than a candidate.
            self.logger.error(f"Failed to parse job description: {str(e)}")
            return {
                "job_description": None,
                "error": str(e),
            }

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
