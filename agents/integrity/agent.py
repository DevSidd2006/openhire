"""
Integrity Agent.
Analyzes consistency and potential integrity concerns.
"""
from typing import Any, Dict
import uuid

from agents.base import BaseAgent
from config.settings import CONFIDENCE_THRESHOLD
from schemas.evaluation import IntegrityEvaluation, IntegrityFlag
from schemas.interview import InterviewTranscript
from schemas.llm_outputs import IntegrityCheckResult
from schemas.resume import ParsedResume
from utils.evidence import resolve_transcript_evidence

# Below this confidence, even a resolvable/grounded flag is not certain
# enough to stand as "high" severity - "low" is the ceiling. Between this
# and CONFIDENCE_THRESHOLD (0.7 - this project's general bar for a
# trustworthy judgment, config.settings.CONFIDENCE_THRESHOLD), "medium" is
# the ceiling. Only a confidence >= CONFIDENCE_THRESHOLD can stand as
# "high" severity. (P6 Phase 13 finding: weak evidence must not
# automatically become a fraud accusation - the LLM's raw severity claim
# was previously passed through unchecked as long as its cited question(s)
# resolved to a real exchange, even at confidence as low as e.g. 0.3.)
_LOW_SEVERITY_CEILING = 0.5
_SEVERITY_ORDER = ["low", "medium", "high"]


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
                        severity=self._cap_severity_by_confidence(flag_result.severity, flag_result.confidence),
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

    def _cap_severity_by_confidence(self, severity: str, confidence: float) -> str:
        """Cap the LLM's claimed severity by how confident it actually was
        (P6 Phase 13). A grounded-but-low-confidence flag must not read as
        a high-severity fraud accusation - never raises severity, only ever
        lowers it, and an unrecognized severity string passes through
        unchanged rather than being guessed at."""
        if severity not in _SEVERITY_ORDER:
            return severity
        if confidence < _LOW_SEVERITY_CEILING:
            ceiling = "low"
        elif confidence < CONFIDENCE_THRESHOLD:
            ceiling = "medium"
        else:
            ceiling = "high"
        if _SEVERITY_ORDER.index(severity) > _SEVERITY_ORDER.index(ceiling):
            return ceiling
        return severity

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
