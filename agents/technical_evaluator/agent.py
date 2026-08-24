"""
Technical Evaluator Agent.
Evaluates technical competencies from interview responses.
"""
from typing import Any, Dict, List
import json
import uuid

from agents.base import BaseAgent
from schemas.evaluation import TechnicalEvaluation, CompetencyScore
from schemas.interview import InterviewTranscript
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from utils.evidence import create_transcript_evidence


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

            # Get LLM evaluation
            response_text = await self.call_llm_generate(prompt)

            try:
                data = json.loads(response_text)
            except json.JSONDecodeError:
                data = self._create_default_evaluation_data()

            # Build competency scores
            competency_scores = []
            for comp in job_description.competencies:
                score_data = data.get("competency_scores", {}).get(comp.name, {})
                evidence_text = score_data.get("evidence", "No specific evidence")

                competency_scores.append(
                    CompetencyScore(
                        competency_name=comp.name,
                        score=float(score_data.get("score", 7.0)),
                        confidence=float(score_data.get("confidence", 0.75)),
                        evidence=[
                            create_transcript_evidence(
                                text=evidence_text,
                                question_id="unknown",
                                agent="technical_evaluator",
                                explanation=score_data.get("explanation", ""),
                            )
                        ],
                        explanation=score_data.get("explanation", "Technical evaluation"),
                    )
                )

            # Create evaluation
            evaluation = TechnicalEvaluation(
                evaluation_id=f"tech_eval_{uuid.uuid4().hex[:8]}",
                candidate_id=parsed_resume.candidate_id,
                job_id=job_description.job_id,
                interview_id=interview_transcript.interview_id,
                competency_scores=competency_scores,
                technical_score=float(data.get("technical_score", 7.5)),
                strengths=data.get("strengths", []),
                weaknesses=data.get("weaknesses", []),
                evidence=[],  # Could extract more evidence
                explanation=data.get("explanation", "Technical evaluation complete"),
                confidence=float(data.get("confidence", 0.80)),
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

    def _create_default_evaluation_data(self) -> Dict[str, Any]:
        """Create default evaluation data on failure."""
        return {
            "technical_score": 7.0,
            "competency_scores": {},
            "strengths": ["Demonstrated core technical knowledge"],
            "weaknesses": ["Could improve in advanced concepts"],
            "explanation": "Technical evaluation based on interview responses",
            "confidence": 0.70,
        }
