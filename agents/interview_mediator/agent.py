"""
Interview Mediator Agent - LLM-based response analysis and guidance.

Analyzes candidate responses in real-time and generates:
- Follow-up questions based on their answers
- Real-time coaching/guidance
- Response quality assessment
- Adaptive questioning recommendations

Works with any LLM provider configured (Gemini, Claude, etc.)
"""
import json
from typing import Any, Dict, Optional
from pydantic import BaseModel

from agents.base import BaseAgent
from providers import LLMProvider


class ResponseAnalysis(BaseModel):
    """Analysis of candidate response."""
    clarity: int  # 1-10
    completeness: int  # 1-10
    relevance: int  # 1-10
    strengths: list[str]
    areas_for_improvement: list[str]
    suggested_followup: str  # Next question to ask


class CoachingGuidance(BaseModel):
    """Real-time guidance for candidate."""
    guidance_text: str
    encouragement: bool
    suggestion: Optional[str] = None


class InterviewMediatorAgent(BaseAgent):
    """LLM-based mediator for adaptive interview flow."""

    def __init__(self, llm_provider: Optional[LLMProvider] = None):
        super().__init__("interview_mediator", llm_provider)

    async def analyze_response(
        self,
        candidate_name: str,
        question: str,
        answer: str,
        competency: str,
        interview_context: Optional[str] = None,
    ) -> ResponseAnalysis:
        """Analyze candidate's response to a question.

        Args:
            candidate_name: Name of the candidate
            question: The question asked
            answer: The candidate's response
            competency: The competency being evaluated
            interview_context: Optional context about the interview so far

        Returns:
            ResponseAnalysis with scoring and suggestions
        """
        prompt = f"""
Analyze this interview response as an experienced recruiter/interviewer:

Candidate: {candidate_name}
Competency Being Evaluated: {competency}
Question: {question}
Answer: {answer}
{f"Interview Context: {interview_context}" if interview_context else ""}

Provide your analysis in JSON format:
{{
    "clarity": <1-10 score>,
    "completeness": <1-10 score>,
    "relevance": <1-10 score>,
    "strengths": [<list of 2-3 key strengths>],
    "areas_for_improvement": [<list of 2-3 areas to develop>],
    "suggested_followup": "<suggested follow-up question to dig deeper>"
}}

Be specific and actionable. The follow-up should probe deeper into areas of interest or gaps.
"""

        try:
            response = await self.llm_provider.generate_structured(
                prompt=prompt,
                schema=ResponseAnalysis,
                temperature=0.7,
            )
            return response
        except Exception as e:
            self.logger.error(f"Error analyzing response: {e}")
            # Return default analysis on error
            return ResponseAnalysis(
                clarity=5,
                completeness=5,
                relevance=5,
                strengths=["Provided answer"],
                areas_for_improvement=["Could provide more detail"],
                suggested_followup="Can you elaborate on that point?",
            )

    async def generate_followup_question(
        self,
        question_history: list[Dict[str, str]],
        competency: str,
        difficulty_level: str = "medium",
        interview_stage: str = "mid",
    ) -> str:
        """Generate an intelligent follow-up question.

        Args:
            question_history: List of previous Q&A exchanges
            competency: Target competency
            difficulty_level: "easy", "medium", "hard"
            interview_stage: "opening", "mid", "closing"

        Returns:
            A follow-up question string
        """
        history_text = "\n".join([
            f"Q: {q.get('question', '')}\nA: {q.get('answer', '')}"
            for q in question_history[-3:]  # Last 3 exchanges
        ])

        prompt = f"""
Generate an adaptive follow-up interview question based on this conversation history:

{history_text}

Requirements:
- Target Competency: {competency}
- Difficulty: {difficulty_level}
- Interview Stage: {interview_stage}
- Should probe deeper into demonstrated skills or identify gaps
- Should be open-ended and specific
- Should take 2-5 minutes to answer

Return only the question text, no preamble.
"""

        try:
            question = await self.llm_provider.generate(
                prompt=prompt,
                temperature=0.8,
                max_tokens=200,
            )
            return question.strip()
        except Exception as e:
            self.logger.error(f"Error generating follow-up: {e}")
            return "Can you tell me more about your experience with this competency?"

    async def provide_coaching(
        self,
        candidate_name: str,
        response_quality: int,  # 1-10
        topic: str,
        needs_improvement: bool = False,
    ) -> CoachingGuidance:
        """Provide real-time coaching/guidance to candidate.

        Args:
            candidate_name: Candidate name
            response_quality: Quality score of response (1-10)
            topic: Topic being discussed
            needs_improvement: Whether response needs improvement

        Returns:
            CoachingGuidance with encouragement and suggestions
        """
        prompt = f"""
Provide brief, encouraging coaching to a job candidate in an interview.

Candidate: {candidate_name}
Response Quality: {response_quality}/10
Topic: {topic}
Needs Improvement: {needs_improvement}

Generate coaching in JSON format:
{{
    "guidance_text": "<brief, encouraging guidance in 1-2 sentences>",
    "encouragement": <true if high quality or improving, false if needs work>,
    "suggestion": "<optional specific suggestion for next answer, or null>"
}}

Be supportive and constructive. The candidate is nervous and needs confidence.
"""

        try:
            guidance = await self.llm_provider.generate_structured(
                prompt=prompt,
                schema=CoachingGuidance,
                temperature=0.7,
            )
            return guidance
        except Exception as e:
            self.logger.error(f"Error generating coaching: {e}")
            return CoachingGuidance(
                guidance_text="Great! Keep going.",
                encouragement=True,
            )

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute mediator analysis based on provided parameters."""
        action = kwargs.get("action", "analyze")

        if action == "analyze":
            result = await self.analyze_response(
                candidate_name=kwargs.get("candidate_name", "Candidate"),
                question=kwargs.get("question", ""),
                answer=kwargs.get("answer", ""),
                competency=kwargs.get("competency", "General"),
                interview_context=kwargs.get("context"),
            )
            return {
                "action": "analysis",
                "result": result.dict(),
            }

        elif action == "followup":
            question = await self.generate_followup_question(
                question_history=kwargs.get("history", []),
                competency=kwargs.get("competency", "General"),
                difficulty_level=kwargs.get("difficulty", "medium"),
                interview_stage=kwargs.get("stage", "mid"),
            )
            return {
                "action": "followup",
                "question": question,
            }

        elif action == "coaching":
            guidance = await self.provide_coaching(
                candidate_name=kwargs.get("candidate_name", "Candidate"),
                response_quality=kwargs.get("quality", 5),
                topic=kwargs.get("topic", ""),
                needs_improvement=kwargs.get("needs_improvement", False),
            )
            return {
                "action": "coaching",
                "guidance": guidance.dict(),
            }

        return {"error": "Unknown action"}
