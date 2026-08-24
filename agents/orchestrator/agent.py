"""
Orchestrator Agent.
Coordinates the entire hiring evaluation pipeline.
"""
from typing import Any, Dict, List, Optional
import uuid

from agents.base import BaseAgent
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from schemas.interview import InterviewTranscript


class OrchestratorAgent(BaseAgent):
    """Orchestrates the entire evaluation pipeline."""

    def __init__(self, **kwargs):
        super().__init__(name="orchestrator", **kwargs)

    async def execute(
        self,
        job_description: JobDescription = None,
        candidates: List[ParsedResume] = None,
        interview_transcripts: Dict[str, InterviewTranscript] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Orchestrate full pipeline.
        
        Args:
            job_description: Job description
            candidates: List of candidate resumes
            interview_transcripts: Interview transcripts by candidate_id
            
        Returns:
            Pipeline status and intermediate results
        """
        self.logger.info("Starting orchestration")

        try:
            # Validate inputs
            if not job_description:
                raise ValueError("Job description required")
            if not candidates or len(candidates) == 0:
                raise ValueError("At least one candidate required")

            self.logger.info(f"Orchestrating evaluation for {len(candidates)} candidates")

            # Pipeline state
            pipeline_state = {
                "job_id": job_description.job_id,
                "candidates_count": len(candidates),
                "transcripts_available": len(interview_transcripts) if interview_transcripts else 0,
                "stage": "initialized",
                "run_id": f"run_{uuid.uuid4().hex[:8]}",
            }

            return pipeline_state

        except Exception as e:
            self.logger.error(f"Orchestration failed: {str(e)}")
            return {"error": str(e)}

    async def validate_inputs(
        self,
        job_description: JobDescription,
        candidates: List[ParsedResume],
    ) -> bool:
        """Validate all inputs before processing."""
        if not job_description or not job_description.job_id:
            raise ValueError("Invalid job description")

        if not candidates or len(candidates) == 0:
            raise ValueError("No candidates provided")

        for candidate in candidates:
            if not candidate.candidate_id:
                raise ValueError(f"Candidate missing ID")

        return True
