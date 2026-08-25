"""
Interviewer Agent - PRE-INTERVIEW QUESTION PLANNING (batch, unchanged since
P0/P1/P2), plus P3's live ADAPTIVE interview support.

BATCH PLANNING (execute() / _generate_single_question, unchanged by P3):
this agent generates a batch of candidate-specific questions before any
interview happens. It is called once per shortlisted candidate in
orchestration/graph.py's `generate_questions` node, and its output
(`interview_questions` in PipelineState) is NOT currently consumed by
anything downstream - technical/behavioral evaluation, resume audit, and
integrity checking all run against the actual `interview_transcript`, which
is supplied separately (in the demo pipeline, from sample data) and is not
required to contain the questions this agent generated. `previous_answers`
here only accumulates the questions generated earlier in the SAME batch call
(see `_generate_single_question`, which appends `{"question": ...}`, never a
real candidate answer) - it never sees an actual candidate response. This
path is kept as-is: useful for preparing an interview script in advance, and
harmless in the pipeline as long as it is not mistaken for the adaptive loop
below or claimed to drive scoring.

ADAPTIVE INTERVIEW (P3 - evaluate_answer() / generate_next_question()):
a real live-interview loop that reacts to what the candidate actually said.
Unlike the batch path, these methods never decide WHICH competency to ask
about or WHETHER to continue - that is decided beforehand by the pure,
LLM-free logic in utils/adaptive_interview.py (decide_next_action). These
methods only (1) judge one already-happened answer against one named
competency, and (2) phrase the text of one already-decided next question.
See tests/test_adaptive_interview.py for the full turn loop
(decide -> generate/evaluate -> apply) that drives them.
"""
from typing import Any, Dict, List, Optional
import uuid

from agents.base import BaseAgent
from schemas.interview import InterviewAnswer, InterviewQuestion, InterviewState, NextQuestionDecision
from schemas.job import JobDescription
from schemas.llm_outputs import AdaptiveQuestionResult, AnswerEvaluationResult, InterviewQuestionResult
from schemas.resume import ParsedResume
from utils.adaptive_interview import (
    AdaptiveInterviewError,
    DuplicateQuestionError,
    is_duplicate_question,
    previously_asked_texts,
)


class InterviewerAgent(BaseAgent):
    """Pre-interview question planning (batch, non-adaptive). See module
    docstring - this is not a live adaptive interviewer."""

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

        result: InterviewQuestionResult = await self.call_llm_structured(
            prompt,
            schema=InterviewQuestionResult.model_json_schema(),
            validate=InterviewQuestionResult.model_validate,
        )

        question = InterviewQuestion(
            question_id=f"q_{uuid.uuid4().hex[:8]}",
            question_text=result.question_text,
            category=result.category,
            competency=result.competency,
            difficulty=result.difficulty,
            reason=result.reason,
            expected_duration_seconds=result.expected_duration_seconds,
        )

        self.logger.debug(f"Generated question: {question.question_text[:50]}...")
        return question

    # -----------------------------------------------------------------
    # P3: Adaptive interview support.
    #
    # These two methods are the only LLM-calling surface of the adaptive
    # engine. Everything about WHICH competency to target, WHAT action to
    # take, and WHETHER to finish is decided beforehand by
    # utils.adaptive_interview.decide_next_action() (pure Python, no LLM) -
    # these methods only (1) judge one already-happened answer and (2)
    # phrase one already-decided next question. Neither method is called by
    # the pre-P3 batch pipeline (orchestration/graph.py); see
    # tests/test_adaptive_interview.py for the adaptive turn loop that
    # drives them.
    # -----------------------------------------------------------------

    async def evaluate_answer(
        self,
        question: InterviewQuestion,
        answer: InterviewAnswer,
        target_competency: str,
    ) -> AnswerEvaluationResult:
        """Judge one candidate answer against one named competency.

        Structured output only (P1/P3 Phase 12) - no json.loads(), no
        fabricated default score (AnswerEvaluationResult.score/confidence
        have no default, so a malformed response fails validation and is
        retried/raised by call_llm_structured rather than silently scoring
        the answer as adequate).
        """
        prompt = self.load_prompt("answer_evaluator.md")
        prompt = prompt.format(
            target_competency=target_competency,
            question_text=question.question_text,
            answer_text=answer.answer_text,
        )

        return await self.call_llm_structured(
            prompt,
            schema=AnswerEvaluationResult.model_json_schema(),
            validate=AnswerEvaluationResult.model_validate,
        )

    async def generate_next_question(
        self,
        job_description: JobDescription,
        parsed_resume: ParsedResume,
        state: InterviewState,
        decision: NextQuestionDecision,
        previous_answer: Optional[InterviewAnswer] = None,
    ) -> InterviewQuestion:
        """Phrase the next question for an already-made, already-validated
        `decision`. Raises DuplicateQuestionError if the LLM's phrasing
        duplicates a question already asked (P3 Phase 9) - callers must not
        catch this and silently reuse the duplicate; regenerating is a
        reasonable P4 addition, not attempted here.

        `decision.action` must not be "finish" - generating a question for a
        finished interview is a caller bug, not a recoverable case.
        """
        if decision.action == "finish":
            raise AdaptiveInterviewError("Cannot generate a question for a finish decision")
        if decision.target_competency is None:
            raise AdaptiveInterviewError("generate_next_question requires decision.target_competency")

        asked_questions = previously_asked_texts(state)
        prompt = self.load_prompt("adaptive_interviewer.md")
        prompt = prompt.format(
            action=decision.action,
            target_competency=decision.target_competency,
            reason=decision.reason,
            expected_evidence=decision.expected_evidence or "N/A",
            difficulty=decision.difficulty or "medium",
            previous_answer=previous_answer.answer_text if previous_answer else "None",
            asked_questions="\n".join(f"- {t}" for t in asked_questions) or "None",
            job_description=job_description.description,
            candidate_resume=parsed_resume.raw_text or f"{parsed_resume.candidate_name}: {parsed_resume.skills}",
        )

        result: AdaptiveQuestionResult = await self.call_llm_structured(
            prompt,
            schema=AdaptiveQuestionResult.model_json_schema(),
            validate=AdaptiveQuestionResult.model_validate,
        )

        if is_duplicate_question(result.question_text, state):
            raise DuplicateQuestionError(
                f"Generated question duplicates a previous question: {result.question_text!r}"
            )

        question_id = f"q_{state.candidate_id}_{state.questions_asked + 1:03d}"
        return InterviewQuestion(
            question_id=question_id,
            question_text=result.question_text,
            category=result.question_type,
            competency=decision.target_competency,
            difficulty=result.difficulty,
            reason=decision.reason,
            expected_duration_seconds=result.expected_duration_seconds,
            question_type=result.question_type,
        )
