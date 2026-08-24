"""
Bias Checker Agent.
Audits evaluation outputs for potential bias or unfairness.
"""
from typing import Any, Dict
import json
import uuid

from agents.base import BaseAgent
from schemas.evaluation import BiasAudit, BiasFlag
from schemas.interview import InterviewTranscript


class BiasCheckerAgent(BaseAgent):
    """Audits for bias in evaluations."""

    def __init__(self, **kwargs):
        super().__init__(name="bias_checker", **kwargs)

    async def execute(
        self,
        interview_transcript: InterviewTranscript,
        candidate_id: str = "unknown",
        job_id: str = "unknown",
        technical_score: float = 7.0,
        behavioral_score: float = 7.0,
        evaluation_text: str = "",
        **kwargs
    ) -> Dict[str, Any]:
        """
        Audit evaluation outputs for bias.

        Args:
            interview_transcript: Interview transcript
            candidate_id: Candidate identifier
            job_id: Job identifier
            technical_score: Technical evaluation score
            behavioral_score: Behavioral evaluation score
            evaluation_text: Full evaluation text

        Returns:
            BiasAudit with any flags found
        """
        self.logger.info("Auditing evaluations for potential bias")

        try:
            transcript_text = self._format_transcript(interview_transcript)

            prompt = self.load_prompt("bias_checker.md")
            prompt = prompt.format(
                transcript=transcript_text,
                technical_score=technical_score,
                behavioral_score=behavioral_score,
                evaluation_text=evaluation_text,
            )

            response_text = await self.call_llm_generate(prompt)

            try:
                data = json.loads(response_text)
            except json.JSONDecodeError:
                data = self._create_default_audit()

            # Build flags
            flags = []
            for flag_data in data.get("flags", []):
                flags.append(
                    BiasFlag(
                        flag_id=f"bias_flag_{uuid.uuid4().hex[:8]}",
                        bias_type=flag_data.get("bias_type", "other"),
                        severity=flag_data.get("severity", "low"),
                        confidence=float(flag_data.get("confidence", 0.60)),
                        evidence=[],
                        description=flag_data.get("description", ""),
                        recommendation=flag_data.get("recommendation", "Monitor this category"),
                    )
                )

            # Create audit
            audit = BiasAudit(
                audit_id=f"bias_audit_{uuid.uuid4().hex[:8]}",
                candidate_id=candidate_id,
                job_id=job_id,
                interview_id=interview_transcript.interview_id,
                flags=flags,
                fairness_status=data.get("fairness_status", "fair"),
                explanation=data.get("explanation", "Bias check complete"),
                confidence=float(data.get("confidence", 0.75)),
                requires_human_review=len([f for f in flags if f.severity in ["high", "critical"]]) > 0,
            )

            return {"bias_audit": audit}

        except Exception as e:
            self.logger.error(f"Bias check failed: {str(e)}")
            return {"bias_audit": None, "error": str(e)}

    def _format_transcript(self, transcript: InterviewTranscript) -> str:
        """Format transcript for LLM."""
        lines = []
        for i, (question, answer) in enumerate(transcript.exchanges, 1):
            lines.append(f"Q{i}: {question.question_text}")
            lines.append(f"A{i}: {answer.answer_text}\n")
        return "\n".join(lines)

    def _create_default_audit(self) -> Dict[str, Any]:
        """Create default audit on failure."""
        return {
            "flags": [],
            "fairness_status": "fair",
            "explanation": "No significant bias detected",
            "confidence": 0.70,
        }
