"""
Behavioral Evaluator Agent.
Evaluates soft skills and behavioral competencies.
"""
from typing import Any, Dict
import uuid

from agents.base import BaseAgent
from schemas.evaluation import BehavioralEvaluation, CompetencyScore
from schemas.interview import InterviewTranscript
from schemas.job import JobDescription
from schemas.llm_outputs import BehavioralEvaluationResult
from schemas.resume import ParsedResume
from utils.evidence import resolve_transcript_evidence


class BehavioralEvaluatorAgent(BaseAgent):
    """Evaluates behavioral and soft skills from interview."""

    def __init__(self, **kwargs):
        super().__init__(name="behavioral_evaluator", **kwargs)

    async def execute(
        self,
        job_description: JobDescription,
        parsed_resume: ParsedResume,
        interview_transcript: InterviewTranscript,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Evaluate behavioral competencies.

        Args:
            job_description: Job requirements
            parsed_resume: Candidate resume
            interview_transcript: Sealed interview transcript

        Returns:
            BehavioralEvaluation with competency scores
        """
        self.logger.info(
            f"Evaluating behavioral skills for candidate {parsed_resume.candidate_id}"
        )

        try:
            transcript_text = self._format_transcript(interview_transcript)

            prompt = self.load_prompt("behavioral_evaluator.md")
            prompt = prompt.format(
                job_description=job_description.description,
                competencies=self._format_competencies(job_description),
                transcript=transcript_text,
            )

            result: BehavioralEvaluationResult = await self.call_llm_structured(
                prompt,
                schema=BehavioralEvaluationResult.model_json_schema(),
                validate=BehavioralEvaluationResult.model_validate,
            )

            # Build competency scores. Only competencies the LLM actually
            # returned a judgment for are scored (see P0-4). A competency the
            # LLM DID score but whose evidence never resolves is still kept
            # (for transparency) but marked evidence_status="insufficient"
            # (P2 Phase 6) so scoring excludes it from the rubric computation
            # rather than treating it as a normal grounded result.
            competency_scores = []
            for comp in job_description.competencies:
                judgment = result.competency_scores.get(comp.name)
                if judgment is None:
                    continue

                evidence_item = resolve_transcript_evidence(
                    transcript=interview_transcript,
                    question_number=judgment.evidence_question_number,
                    agent="behavioral_evaluator",
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
                        explanation=judgment.explanation,
                        evidence_status="supported" if evidence_item else "insufficient",
                    )
                )

            # Create evaluation
            evaluation = BehavioralEvaluation(
                evaluation_id=f"behav_eval_{uuid.uuid4().hex[:8]}",
                candidate_id=parsed_resume.candidate_id,
                job_id=job_description.job_id,
                interview_id=interview_transcript.interview_id,
                competency_scores=competency_scores,
                behavioral_score=result.behavioral_score,
                communication=result.communication,
                problem_solving=result.problem_solving,
                teamwork=result.teamwork,
                adaptability=result.adaptability,
                strengths=result.strengths,
                weaknesses=result.weaknesses,
                evidence=[e for cs in competency_scores for e in cs.evidence],
                explanation=result.explanation,
                confidence=result.confidence,
            )

            return {"behavioral_evaluation": evaluation}

        except Exception as e:
            self.logger.error(f"Behavioral evaluation failed: {str(e)}")
            return {"behavioral_evaluation": None, "error": str(e)}

    def _format_transcript(self, transcript: InterviewTranscript) -> str:
        """Format transcript for LLM."""
        lines = []
        for i, (question, answer) in enumerate(transcript.exchanges, 1):
            lines.append(f"Q{i}: {question.question_text}")
            lines.append(f"A{i}: {answer.answer_text}\n")
        return "\n".join(lines)

    def _format_competencies(self, job_description: JobDescription) -> str:
        """Render the job's exact competency names as a bullet list - see
        TechnicalEvaluatorAgent._format_competencies for the full rationale
        (P8B.2 finding). This agent's lookup below
        (`result.competency_scores.get(comp.name)`) is an exact string
        match, so the LLM needs the authoritative name list, not just the
        prompt's 5 hardcoded category labels, to score any job-specific
        competency correctly."""
        if not job_description.competencies:
            return "(none specified)"
        return "\n".join(f"- {c.name}" for c in job_description.competencies)
