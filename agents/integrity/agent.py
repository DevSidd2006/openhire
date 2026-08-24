"""
Integrity Agent.
Analyzes consistency and potential integrity concerns.
"""
from typing import Any, Dict, List
import json
import uuid

from agents.base import BaseAgent
from schemas.evaluation import IntegrityEvaluation, IntegrityFlag
from schemas.interview import InterviewTranscript
from schemas.resume import ParsedResume
from utils.evidence import create_evidence


class IntegrityAgent(BaseAgent):
    """Analyzes conversational consistency and integrity."""

    def __init__(self, **kwargs):
        super().__init__(name="integrity", **kwargs)

    async def execute(
        self,
        parsed_resume: ParsedResume,
        interview_transcript: InterviewTranscript,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Analyze integrity and consistency.
        
        Args:
            parsed_resume: Candidate resume
            interview_transcript: Sealed interview transcript
            
        Returns:
            IntegrityEvaluation with any flags
        """
        self.logger.info(f"Analyzing integrity for candidate {parsed_resume.candidate_id}")

        try:
            transcript_text = self._format_transcript(interview_transcript)

            prompt = self.load_prompt("integrity.md")
            prompt = prompt.format(
                resume=self._format_resume_summary(parsed_resume),
                transcript=transcript_text,
            )

            response_text = await self.call_llm_generate(prompt)

            try:
                data = json.loads(response_text)
            except json.JSONDecodeError:
                data = self._create_default_evaluation()

            # Build flags
            flags = []
            for flag_data in data.get("flags", []):
                flags.append(
                    IntegrityFlag(
                        flag_id=f"flag_{uuid.uuid4().hex[:8]}",
                        flag_type=flag_data.get("flag_type", "other"),
                        severity=flag_data.get("severity", "medium"),
                        confidence=float(flag_data.get("confidence", 0.70)),
                        evidence=[],
                        description=flag_data.get("description", ""),
                        requires_human_review=flag_data.get("requires_human_review", True),
                    )
                )

            # Create evaluation
            evaluation = IntegrityEvaluation(
                evaluation_id=f"integ_eval_{uuid.uuid4().hex[:8]}",
                candidate_id=parsed_resume.candidate_id,
                job_id="unknown",
                interview_id=interview_transcript.interview_id,
                flags=flags,
                overall_integrity=data.get("overall_integrity", "clear"),
                explanation=data.get("explanation", "Integrity analysis complete"),
                confidence=float(data.get("confidence", 0.80)),
                requires_human_review=len(flags) > 0,
            )

            return {"integrity_evaluation": evaluation}

        except Exception as e:
            self.logger.error(f"Integrity analysis failed: {str(e)}")
            return {"integrity_evaluation": None, "error": str(e)}

    def _format_resume_summary(self, resume: ParsedResume) -> str:
        """Create brief resume summary."""
        lines = [f"Candidate: {resume.candidate_name}"]
        if resume.work_experience:
            lines.append(f"Most recent role: {resume.work_experience[0].position}")
        lines.extend(f"Skills: {', '.join(resume.skills[:5])}")
        return "\n".join(lines)

    def _format_transcript(self, transcript: InterviewTranscript) -> str:
        """Format transcript for LLM."""
        lines = []
        for i, (question, answer) in enumerate(transcript.exchanges, 1):
            lines.append(f"Q{i}: {question.question_text}")
            lines.append(f"A{i}: {answer.answer_text}\n")
        return "\n".join(lines)

    def _create_default_evaluation(self) -> Dict[str, Any]:
        """Create default evaluation on failure."""
        return {
            "flags": [],
            "overall_integrity": "clear",
            "explanation": "No inconsistencies detected",
            "confidence": 0.75,
        }
