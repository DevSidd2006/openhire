"""
Interview Question Generation Agent.
Generates adaptive interview questions based on candidate and role.
"""
from typing import Any, Dict, List
import json
import uuid

from agents.base import BaseAgent
from schemas.interview import InterviewQuestion, InterviewAnswer
from schemas.job import JobDescription
from schemas.resume import ParsedResume


class InterviewerAgent(BaseAgent):
    """Generates adaptive interview questions."""

    def __init__(self, **kwargs):
        super().__init__(name="interviewer", **kwargs)

    async def execute(
        self,
        job_description: JobDescription,
        parsed_resume: ParsedResume,
        previous_answers: List[Dict[str, Any]] = None,
        question_count: int = 1,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate interview questions.
        
        Args:
            job_description: Job requirements
            parsed_resume: Candidate resume
            previous_answers: Previous interview answers (for context)
            question_count: Number of questions to generate
            
        Returns:
            List of InterviewQuestion objects
        """
        self.logger.info(
            f"Generating {question_count} interview question(s) for candidate {parsed_resume.candidate_id}"
        )

        try:
            questions = []
            for i in range(question_count):
                # Generate one question at a time for better quality
                question = await self._generate_single_question(
                    job_description, parsed_resume, previous_answers or []
                )
                questions.append(question)
                if previous_answers is None:
                    previous_answers = []
                previous_answers.append({"question": question.question_text})

            return {
                "questions": questions,
                "question_count": len(questions),
            }

        except Exception as e:
            self.logger.error(f"Question generation failed: {str(e)}")
            return {"questions": [], "error": str(e)}

    async def _generate_single_question(
        self,
        job_description: JobDescription,
        parsed_resume: ParsedResume,
        previous_answers: List[Dict[str, Any]],
    ) -> InterviewQuestion:
        """Generate a single interview question."""

        # Format previous answers
        previous_context = "\n".join(
            [f"- {a.get('question', 'Previous Q')}" for a in previous_answers[-3:]]
        ) if previous_answers else "None"

        prompt = self.load_prompt("interviewer.md")
        prompt = prompt.format(
            job_description=job_description.description,
            candidate_resume=parsed_resume.raw_text or f"{parsed_resume.candidate_name}: {parsed_resume.skills}",
            previous_answers=previous_context,
        )

        response_text = await self.call_llm_generate(prompt)

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            # Fallback to default question
            data = self._create_default_question_data()

        question = InterviewQuestion(
            question_id=f"q_{uuid.uuid4().hex[:8]}",
            question_text=data.get("question_text", "Can you tell us about your experience?"),
            category=data.get("category", "general"),
            competency=data.get("competency"),
            difficulty=data.get("difficulty", "medium"),
            reason=data.get("reason"),
            expected_duration_seconds=data.get("expected_duration_seconds", 60),
        )

        self.logger.debug(f"Generated question: {question.question_text[:50]}...")
        return question

    def _create_default_question_data(self) -> Dict[str, Any]:
        """Create default question data on parsing failure."""
        return {
            "question_text": "Can you walk us through your most relevant project and explain your role?",
            "category": "technical",
            "difficulty": "medium",
            "reason": "To understand practical experience",
            "expected_duration_seconds": 120,
        }
