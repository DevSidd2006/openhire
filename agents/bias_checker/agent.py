"""
Bias Checker Agent.
Audits evaluation outputs for potential bias or unfairness.
"""
from typing import Any, Dict, Optional
import uuid

from agents.base import BaseAgent
from schemas.evaluation import BiasAudit, BiasFlag
from schemas.interview import InterviewTranscript
from schemas.llm_outputs import BiasCheckResult
from utils.evidence import create_evidence

# Section keys the bias checker knows how to cite as evidence and their
# display headers in the composed evaluation text sent to the LLM. Must stay
# in sync with node_run_bias_check in orchestration/graph.py, which is the
# only caller that populates evaluation_sections.
_SECTION_HEADERS = {
    "technical_evaluation": "Technical Evaluation Rationale",
    "behavioral_evaluation": "Behavioral Evaluation Rationale",
    "resume_audit": "Resume Audit Rationale",
    "integrity": "Integrity Evaluation Rationale",
}


class BiasCheckerAgent(BaseAgent):
    """Audits for bias in evaluations."""

    def __init__(self, **kwargs):
        super().__init__(name="bias_checker", **kwargs)

    async def execute(
        self,
        interview_transcript: InterviewTranscript,
        candidate_id: str = "unknown",
        job_id: str = "unknown",
        technical_score: Optional[float] = None,
        behavioral_score: Optional[float] = None,
        evaluation_sections: Optional[Dict[str, str]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Audit the SYSTEM'S EVALUATION (not the raw candidate transcript) for bias.

        Args:
            interview_transcript: Interview transcript (context only)
            candidate_id: Candidate identifier
            job_id: Job identifier
            technical_score: Technical evaluation score
            behavioral_score: Behavioral evaluation score
            evaluation_sections: The actual rationale text produced by the
                other evaluators (technical_evaluation, behavioral_evaluation,
                resume_audit, integrity explanations/strengths/weaknesses).
                This is the artifact being audited for bias - without it the
                bias checker has nothing of the evaluators' own reasoning to
                inspect, only the transcript.

        Returns:
            BiasAudit with any flags found
        """
        self.logger.info("Auditing evaluations for potential bias")
        sections = evaluation_sections or {}

        try:
            transcript_text = self._format_transcript(interview_transcript)
            evaluation_text = self._format_sections(sections)

            prompt = self.load_prompt("bias_checker.md")
            prompt = prompt.format(
                transcript=transcript_text,
                technical_score=technical_score if technical_score is not None else "not evaluated",
                behavioral_score=behavioral_score if behavioral_score is not None else "not evaluated",
                evaluation_text=evaluation_text,
            )

            result: BiasCheckResult = await self.call_llm_structured(
                prompt,
                schema=BiasCheckResult.model_json_schema(),
                validate=BiasCheckResult.model_validate,
            )

            # Build flags. Evidence is only populated when the LLM points to a
            # real section of the evaluation rationale we actually sent it -
            # never fabricated. The flag itself is still kept even without a
            # resolvable source: unlike an integrity accusation about the
            # candidate, a bias flag is a request for human review of the
            # SYSTEM'S output, so an imprecisely-sourced concern is still a
            # legitimate thing to route to a reviewer.
            flags = []
            for flag_result in result.flags:
                source_key = flag_result.evidence_source
                source_text = sections.get(source_key) if source_key else None
                evidence_items = []
                if source_text:
                    evidence_items.append(
                        create_evidence(
                            source_type="derived",
                            source_id=source_key,
                            text=source_text,
                            agent="bias_checker",
                            explanation=flag_result.description
                            or "Cited as the source of a potential bias concern",
                            candidate_id=candidate_id,
                            evidence_type="supporting",  # supports the bias finding itself
                            relevance=0.75,
                        )
                    )

                flags.append(
                    BiasFlag(
                        flag_id=f"bias_flag_{uuid.uuid4().hex[:8]}",
                        bias_type=flag_result.bias_type,
                        severity=flag_result.severity,
                        confidence=flag_result.confidence,
                        evidence=evidence_items,
                        description=flag_result.description,
                        recommendation=flag_result.recommendation,
                    )
                )

            # Create audit
            audit = BiasAudit(
                audit_id=f"bias_audit_{uuid.uuid4().hex[:8]}",
                candidate_id=candidate_id,
                job_id=job_id,
                interview_id=interview_transcript.interview_id,
                flags=flags,
                fairness_status=result.fairness_status,
                explanation=result.explanation,
                confidence=result.confidence,
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

    def _format_sections(self, sections: Dict[str, str]) -> str:
        """Render evaluator rationale sections into labeled text for the
        prompt, using the same keys flags can later cite as evidence_source."""
        if not sections:
            return "No evaluator rationale was provided for this audit."

        blocks = []
        for key, header in _SECTION_HEADERS.items():
            text = sections.get(key)
            if text:
                blocks.append(f"### {header} ({key})\n{text}")
        return "\n\n".join(blocks) if blocks else "No evaluator rationale was provided for this audit."
