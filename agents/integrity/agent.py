"""
Integrity Agent.
Analyzes consistency and potential integrity concerns.
"""
from typing import Any, Dict
import uuid

from agents.base import BaseAgent
from schemas.evaluation import IntegrityEvaluation, IntegrityFlag
from schemas.interview import InterviewTranscript
from schemas.llm_outputs import IntegrityCheckResult
from schemas.resume import ParsedResume
from utils.evidence import resolve_transcript_evidence


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

            result: IntegrityCheckResult = await self.call_llm_structured(
                prompt,
                schema=IntegrityCheckResult.model_json_schema(),
                validate=IntegrityCheckResult.model_validate,
            )

            # Build flags. A flag is only kept if at least one of its cited
            # question numbers actually resolves to a real transcript
            # exchange - an integrity accusation with no grounded evidence is
            # dropped rather than kept with an empty evidence list, per the
            # "do not create a flag merely to satisfy the evidence
            # requirement" rule.
            flags = []
            for flag_result in result.flags:
                evidence_items = [
                    item
                    for qn in flag_result.evidence_question_numbers
                    if (item := resolve_transcript_evidence(
                        transcript=interview_transcript,
                        question_number=qn,
                        agent="integrity",
                        explanation=flag_result.description,
                        candidate_id=parsed_resume.candidate_id,
                        evidence_type="contradicting",
                    )) is not None
                ]
                if not evidence_items:
                    self.logger.warning(
                        "Dropping integrity flag with no resolvable transcript evidence: "
                        f"{flag_result.flag_type}"
                    )
                    continue

                flags.append(
                    IntegrityFlag(
                        flag_id=f"flag_{uuid.uuid4().hex[:8]}",
                        flag_type=flag_result.flag_type,
                        severity=flag_result.severity,
                        confidence=flag_result.confidence,
                        evidence=evidence_items,
                        description=flag_result.description,
                        requires_human_review=flag_result.requires_human_review,
                    )
                )

            # Create evaluation
            evaluation = IntegrityEvaluation(
                evaluation_id=f"integ_eval_{uuid.uuid4().hex[:8]}",
                candidate_id=parsed_resume.candidate_id,
                job_id=interview_transcript.job_id,
                interview_id=interview_transcript.interview_id,
                flags=flags,
                overall_integrity=result.overall_integrity,
                explanation=result.explanation,
                confidence=result.confidence,
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
        lines.append(f"Skills: {', '.join(resume.skills[:5])}")
        return "\n".join(lines)

    def _format_transcript(self, transcript: InterviewTranscript) -> str:
        """Format transcript for LLM."""
        lines = []
        for i, (question, answer) in enumerate(transcript.exchanges, 1):
            lines.append(f"Q{i}: {question.question_text}")
            lines.append(f"A{i}: {answer.answer_text}\n")
        return "\n".join(lines)
