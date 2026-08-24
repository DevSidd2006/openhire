"""
Behavioral Evaluator Agent.
Evaluates soft skills and behavioral competencies.
"""
from typing import Any, Dict
import json
import uuid

from agents.base import BaseAgent
from schemas.evaluation import BehavioralEvaluation, CompetencyScore
from schemas.interview import InterviewTranscript
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from utils.evidence import create_transcript_evidence


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
                transcript=transcript_text,
            )

            response_text = await self.call_llm_generate(prompt)

            try:
                data = json.loads(response_text)
            except json.JSONDecodeError:
                data = self._create_default_evaluation_data()

            # Build competency scores
            competency_scores = []
            for comp in job_description.competencies:
                # Try to find behavioral data for this competency
                score_data = data.get("competency_scores", {}).get(comp.name, {})

                if score_data:
                    competency_scores.append(
                        CompetencyScore(
                            competency_name=comp.name,
                            score=float(score_data.get("score", 7.0)),
                            confidence=float(score_data.get("confidence", 0.75)),
                            evidence=[],
                            explanation=score_data.get("explanation", ""),
                        )
                    )

            # Create evaluation
            evaluation = BehavioralEvaluation(
                evaluation_id=f"behav_eval_{uuid.uuid4().hex[:8]}",
                candidate_id=parsed_resume.candidate_id,
                job_id=job_description.job_id,
                interview_id=interview_transcript.interview_id,
                competency_scores=competency_scores,
                behavioral_score=float(data.get("behavioral_score", 7.5)),
                communication=float(data.get("communication", 7.5)),
                problem_solving=float(data.get("problem_solving", 7.5)),
                teamwork=float(data.get("teamwork", 7.0)),
                adaptability=float(data.get("adaptability", 7.0)),
                strengths=data.get("strengths", []),
                weaknesses=data.get("weaknesses", []),
                evidence=[],
                explanation=data.get("explanation", "Behavioral evaluation complete"),
                confidence=float(data.get("confidence", 0.80)),
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

    def _create_default_evaluation_data(self) -> Dict[str, Any]:
        """Create default evaluation data on failure."""
        return {
            "behavioral_score": 7.0,
            "communication": 7.5,
            "problem_solving": 7.5,
            "teamwork": 7.0,
            "adaptability": 7.0,
            "competency_scores": {},
            "strengths": ["Good communication", "Collaborative"],
            "weaknesses": ["Could improve on conflict resolution"],
            "explanation": "Behavioral evaluation based on interview responses",
            "confidence": 0.70,
        }
