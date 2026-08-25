"""
Technical Evaluator Agent.
Evaluates technical competencies from interview responses.
"""
from typing import Any, Dict
import uuid

from agents.base import BaseAgent
from schemas.evaluation import TechnicalEvaluation, CompetencyScore
from schemas.interview import InterviewTranscript
from schemas.job import JobDescription
from schemas.llm_outputs import TechnicalEvaluationResult
from schemas.resume import ParsedResume
from utils.evidence import resolve_transcript_evidence


class TechnicalEvaluatorAgent(BaseAgent):
    """Evaluates technical skills from interview."""

    def __init__(self, **kwargs):
        super().__init__(name="technical_evaluator", **kwargs)

    async def execute(
        self,
        job_description: JobDescription,
        parsed_resume: ParsedResume,
        interview_transcript: InterviewTranscript,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Evaluate technical competencies.

        Args:
            job_description: Job requirements with rubric
            parsed_resume: Candidate resume for context
            interview_transcript: Sealed interview transcript

        Returns:
            TechnicalEvaluation with competency scores
        """
        self.logger.info(
            f"Evaluating technical skills for candidate {parsed_resume.candidate_id}"
        )

        try:
            # Extract transcript text
            transcript_text = self._format_transcript(interview_transcript)

            # Create evaluation prompt
            prompt = self.load_prompt("technical_evaluator.md")
            prompt = prompt.format(
                job_description=job_description.description,
                transcript=transcript_text,
            )

            # Get validated, structured LLM evaluation - no manual
            # json.loads()/JSONDecodeError handling needed here; malformed or
            # schema-invalid output is retried and, if still failing, raised
            # as an explicit error by call_llm_structured (see agents/base.py).
            result: TechnicalEvaluationResult = await self.call_llm_structured(
                prompt,
                schema=TechnicalEvaluationResult.model_json_schema(),
                validate=TechnicalEvaluationResult.model_validate,
            )

            # Build competency scores. Only competencies the LLM actually
            # returned a judgment for are scored - a competency the transcript
            # never touched on gets no CompetencyScore entry rather than a
            # fabricated default (see P0-4: a missing judgment must not look
            # like an average one).
            #
            # A competency the LLM DID score, but whose cited evidence never
            # resolves to a real transcript exchange, is still recorded (so
            # the report can show what the LLM claimed) but marked
            # evidence_status="insufficient" (P2 Phase 5) - this is the
            # distinction ScoringAgent relies on to exclude ungrounded scores
            # from the rubric computation rather than silently treating them
            # as normal supported results.
            competency_scores = []
            for comp in job_description.competencies:
                judgment = result.competency_scores.get(comp.name)
                if judgment is None:
                    continue

                evidence_item = resolve_transcript_evidence(
                    transcript=interview_transcript,
                    question_number=judgment.evidence_question_number,
                    agent="technical_evaluator",
                    explanation=judgment.explanation,
                    candidate_id=parsed_resume.candidate_id,
                    competency=comp.name,
                )

                competency_scores.append(
                    CompetencyScore(
                        competency_name=comp.name,
                        score=judgment.score,
                        confidence=judgment.confidence,
                        evidence=[evidence_item] if evidence_item else [],
                        explanation=judgment.explanation or "Technical evaluation",
                        evidence_status="supported" if evidence_item else "insufficient",
                    )
                )

            # Create evaluation. Top-level evidence is the union of every
            # competency's grounded evidence, so the evaluation as a whole is
            # traceable back to the transcript, not just individual scores.
            evaluation = TechnicalEvaluation(
                evaluation_id=f"tech_eval_{uuid.uuid4().hex[:8]}",
                candidate_id=parsed_resume.candidate_id,
                job_id=job_description.job_id,
                interview_id=interview_transcript.interview_id,
                competency_scores=competency_scores,
                technical_score=result.technical_score,
                strengths=result.strengths,
                weaknesses=result.weaknesses,
                evidence=[e for cs in competency_scores for e in cs.evidence],
                explanation=result.explanation,
                confidence=result.confidence,
            )

            return {"technical_evaluation": evaluation}

        except Exception as e:
            self.logger.error(f"Technical evaluation failed: {str(e)}")
            return {"technical_evaluation": None, "error": str(e)}

    def _format_transcript(self, transcript: InterviewTranscript) -> str:
        """Format transcript for LLM."""
        lines = []
        for i, (question, answer) in enumerate(transcript.exchanges, 1):
            lines.append(f"Q{i}: {question.question_text}")
            lines.append(f"A{i}: {answer.answer_text}\n")
        return "\n".join(lines)
