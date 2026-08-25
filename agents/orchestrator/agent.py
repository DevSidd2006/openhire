"""
Orchestrator Agent.

STATUS (P0-7 audit finding, resolved by documenting rather than deleting):
This agent is NOT part of the current batch evaluation pipeline. The actual
pipeline (JD analysis -> resume matching -> parallel evaluation -> scoring ->
leaderboard) is orchestrated by LangGraph in orchestration/graph.py, which
does not call this class - `agents/__init__.py`'s exports and pytest wiring
are the only current references besides docs.

It is kept rather than deleted because docs/roles/multi-agent.md and
README.md's Stage 3 description explicitly list an "orchestrator (runs the
live conversation)" as one of the agents in this system's intended design -
i.e. this class's real, still-unfulfilled responsibility is coordinating a
LIVE interview session (turn-taking, adaptive follow-ups, sealing the
transcript when the conversation ends), not the batch post-interview
pipeline LangGraph already handles. Building that live session layer is
explicitly out of scope for this P0 pass (see InterviewerAgent's docstring
for the same boundary) - Pipecat/streaming/live-voice work is a later phase.

Until that phase, this class is a documented placeholder: safe to keep, not
wired into anything, and should not be forced into orchestration/graph.py
just to look "used" - that would misrepresent what it actually does.
"""
from typing import Any, Dict, List, Optional
import uuid

from agents.base import BaseAgent
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from schemas.interview import InterviewTranscript


class OrchestratorAgent(BaseAgent):
    """Reserved for live-interview-session orchestration (turn-taking,
    sealing the transcript). Not used by the batch LangGraph pipeline in
    orchestration/graph.py - see module docstring."""

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
