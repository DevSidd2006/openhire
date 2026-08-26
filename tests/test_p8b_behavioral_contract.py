"""P8B behavioral-contract decision: regression tests for Option A, the
contract the user chose to resolve the conflict Batch 1 surfaced between
prompts/technical_evaluator.md / prompts/behavioral_evaluator.md (P8B.3)
and two golden cases (behav_no_personality_invention,
tech_unrelated_answer_no_fabricated_competency) that still encoded the
older, pre-P8B.3 "omit the competency" behavior.

Final contract (Option A): if a competency was explicitly tested in the
interview (a question targeting it was actually asked), the evaluator MUST
include that competency in its evaluation - even when the answer is
weak/off-topic/unsupported - with an appropriately low score/confidence,
WITHOUT fabricating evidence (evidence_status reflects whether a real,
resolvable citation was given), and WITHOUT inventing a competency that was
never asked about at all.

TechnicalEvaluatorAgent/BehavioralEvaluatorAgent already implement this
correctly (`agents/technical_evaluator/agent.py`,
`agents/behavioral_evaluator/agent.py`): both build competency_scores
strictly from whatever the LLM's structured result actually contains
(`result.competency_scores.get(comp.name)`) - a competency present in the
result is kept (with evidence_status derived by
utils.evidence.resolve_transcript_evidence), a competency absent from the
result is never added back. Inclusion vs. omission is entirely the PROMPT's
responsibility (prompts/technical_evaluator.md, prompts/
behavioral_evaluator.md, both already P8B.3-compliant) - no agent code
change was needed or made for this contract decision.

These tests exercise the AGENT side of the contract via ScriptedLLMProvider
simulating a prompt-compliant model, covering all four required scenarios
for both agents:
  1. competency explicitly asked + strong answer
  2. competency explicitly asked + weak answer
  3. competency explicitly asked + irrelevant/off-topic answer
  4. competency never asked -> must NOT be fabricated
"""
import pytest

from agents.behavioral_evaluator.agent import BehavioralEvaluatorAgent
from agents.technical_evaluator.agent import TechnicalEvaluatorAgent
from schemas.interview import InterviewAnswer, InterviewQuestion, InterviewTranscript
from schemas.job import Competency, JobDescription
from schemas.resume import ParsedResume
from tests.fakes import ScriptedLLMProvider


def _job(*names_and_weights) -> JobDescription:
    return JobDescription(
        job_id="job_p8b_contract", title="T", description="d",
        competencies=[Competency(name=n, weight=w) for n, w in names_and_weights],
    )


def _resume(candidate_id: str) -> ParsedResume:
    return ParsedResume(candidate_id=candidate_id, candidate_name="Test")


def _transcript(candidate_id: str, job_id: str, exchanges) -> InterviewTranscript:
    pairs = []
    for i, (q_text, a_text) in enumerate(exchanges, start=1):
        question = InterviewQuestion(question_id=f"q{i}", question_text=q_text, category="technical")
        answer = InterviewAnswer(question_id=f"q{i}", answer_text=a_text)
        pairs.append((question, answer))
    return InterviewTranscript(
        interview_id="int_p8b_contract", candidate_id=candidate_id, job_id=job_id,
        start_time="t0", is_sealed=True, exchanges=pairs,
    )


# ---------------------------------------------------------------------------
# Technical evaluator
# ---------------------------------------------------------------------------

class TestTechnicalEvaluatorContractOptionA:
    @pytest.mark.asyncio
    async def test_1_asked_strong_answer_included_with_high_score(self):
        job = _job(("Python", 1.0))
        transcript = _transcript("cand_tech_strong", job.job_id, [
            ("Tell us about a Python project.",
             "I built a production REST API in FastAPI with auth, tests, and CI."),
        ])
        script = ['{"technical_score": 9.0, "competency_scores": {"Python": '
                  '{"score": 9.0, "confidence": 0.9, "evidence_question_number": 1, '
                  '"explanation": "Detailed, concrete, well-tested project."}}, '
                  '"strengths": ["Strong Python depth"], "weaknesses": [], "explanation": "x", "confidence": 0.9}']
        agent = TechnicalEvaluatorAgent(llm_provider=ScriptedLLMProvider(script=script))

        result = await agent.execute(
            job_description=job, parsed_resume=_resume("cand_tech_strong"), interview_transcript=transcript,
        )

        scores = result["technical_evaluation"].competency_scores
        assert len(scores) == 1
        assert scores[0].competency_name == "Python"
        assert scores[0].score >= 7.0
        assert scores[0].evidence_status == "supported"
        assert len(scores[0].evidence) == 1

    @pytest.mark.asyncio
    async def test_2_asked_weak_answer_included_with_low_score(self):
        """Mirrors evaluation/cases/technical_evaluator.json::tech_weak_answer_insufficient."""
        job = _job(("SQL", 1.0))
        transcript = _transcript("cand_tech_weak", job.job_id, [
            ("How do you optimize a query?", "I dunno, maybe add an index?"),
        ])
        script = ['{"technical_score": 2.0, "competency_scores": {"SQL": '
                  '{"score": 2.0, "confidence": 0.3, "explanation": "Vague, no depth."}}, '
                  '"strengths": [], "weaknesses": ["No depth"], "explanation": "Weak.", "confidence": 0.4}']
        agent = TechnicalEvaluatorAgent(llm_provider=ScriptedLLMProvider(script=script))

        result = await agent.execute(
            job_description=job, parsed_resume=_resume("cand_tech_weak"), interview_transcript=transcript,
        )

        scores = result["technical_evaluation"].competency_scores
        assert len(scores) == 1
        assert scores[0].competency_name == "SQL"
        assert scores[0].score <= 3.0
        assert scores[0].evidence_status == "insufficient"  # no citation given -> ungrounded, never fabricated

    @pytest.mark.asyncio
    async def test_3_asked_offtopic_answer_included_others_not_fabricated(self):
        """Mirrors the UPDATED evaluation/cases/technical_evaluator.json::
        tech_unrelated_answer_no_fabricated_competency (P8B contract decision)."""
        job = _job(("Python", 0.34), ("SQL", 0.33), ("System Design", 0.33))
        transcript = _transcript("cand_tech_offtopic", job.job_id, [
            ("Tell us about a Python project.", "I really enjoy hiking on weekends."),
        ])
        script = ['{"technical_score": 1.0, "competency_scores": {"Python": '
                  '{"score": 1.0, "confidence": 0.2, "explanation": "Completely off-topic."}}, '
                  '"strengths": [], "weaknesses": ["Off-topic response"], "explanation": "x", "confidence": 0.3}']
        agent = TechnicalEvaluatorAgent(llm_provider=ScriptedLLMProvider(script=script))

        result = await agent.execute(
            job_description=job, parsed_resume=_resume("cand_tech_offtopic"), interview_transcript=transcript,
        )

        scores = result["technical_evaluation"].competency_scores
        assert len(scores) == 1  # Python only - SQL/System Design never asked, never fabricated
        assert scores[0].competency_name == "Python"
        assert scores[0].score <= 3.0
        assert scores[0].evidence_status == "insufficient"

    @pytest.mark.asyncio
    async def test_4_never_asked_competency_not_fabricated(self):
        job = _job(("Python", 0.5), ("SQL", 0.5))
        transcript = _transcript("cand_tech_never_asked", job.job_id, [
            ("Tell us about a Python project.", "I wrote a Django app with a Postgres backend, load-tested it."),
        ])
        # A prompt-compliant model only ever reports a judgment for the
        # competency it was actually given a chance to probe.
        script = ['{"technical_score": 8.0, "competency_scores": {"Python": '
                  '{"score": 8.0, "confidence": 0.8, "evidence_question_number": 1, '
                  '"explanation": "Solid, concrete answer."}}, '
                  '"strengths": [], "weaknesses": [], "explanation": "x", "confidence": 0.8}']
        agent = TechnicalEvaluatorAgent(llm_provider=ScriptedLLMProvider(script=script))

        result = await agent.execute(
            job_description=job, parsed_resume=_resume("cand_tech_never_asked"), interview_transcript=transcript,
        )

        scores = result["technical_evaluation"].competency_scores
        names = {s.competency_name for s in scores}
        assert names == {"Python"}  # SQL was never asked - must not appear at all


# ---------------------------------------------------------------------------
# Behavioral evaluator
# ---------------------------------------------------------------------------

class TestBehavioralEvaluatorContractOptionA:
    @pytest.mark.asyncio
    async def test_1_asked_strong_answer_included_with_high_score(self):
        job = _job(("Teamwork", 1.0))
        transcript = _transcript("cand_behav_strong", job.job_id, [
            ("Describe a time you worked on a team.",
             "I led a 4-person team through a tight deadline, split the work, and unblocked a teammate mid-sprint."),
        ])
        script = ['{"behavioral_score": 9.0, "communication": 8.0, "problem_solving": 8.0, '
                  '"teamwork": 9.0, "adaptability": 8.0, "competency_scores": {"Teamwork": '
                  '{"score": 9.0, "confidence": 0.9, "evidence_question_number": 1, '
                  '"explanation": "Concrete, detailed leadership example."}}, '
                  '"strengths": ["Strong teamwork"], "weaknesses": [], "explanation": "x", "confidence": 0.9}']
        agent = BehavioralEvaluatorAgent(llm_provider=ScriptedLLMProvider(script=script))

        result = await agent.execute(
            job_description=job, parsed_resume=_resume("cand_behav_strong"), interview_transcript=transcript,
        )

        scores = result["behavioral_evaluation"].competency_scores
        assert len(scores) == 1
        assert scores[0].competency_name == "Teamwork"
        assert scores[0].score >= 7.0
        assert scores[0].evidence_status == "supported"
        assert len(scores[0].evidence) == 1

    @pytest.mark.asyncio
    async def test_2_asked_weak_answer_included_with_low_score(self):
        """Mirrors the UPDATED evaluation/cases/behavioral_evaluator.json::
        behav_no_personality_invention (P8B contract decision)."""
        job = _job(("Teamwork", 1.0))
        transcript = _transcript("cand_behav_weak", job.job_id, [
            ("Describe a time you worked on a team.", "I don't really remember, it was a while ago."),
        ])
        script = ['{"behavioral_score": 2.0, "communication": 4.0, "problem_solving": 4.0, '
                  '"teamwork": 2.0, "adaptability": 4.0, "competency_scores": {"Teamwork": '
                  '{"score": 2.0, "confidence": 0.2, "explanation": "No concrete example given."}}, '
                  '"strengths": [], "weaknesses": ["No concrete example given"], "explanation": "x", "confidence": 0.3}']
        agent = BehavioralEvaluatorAgent(llm_provider=ScriptedLLMProvider(script=script))

        result = await agent.execute(
            job_description=job, parsed_resume=_resume("cand_behav_weak"), interview_transcript=transcript,
        )

        scores = result["behavioral_evaluation"].competency_scores
        assert len(scores) == 1
        assert scores[0].competency_name == "Teamwork"
        assert scores[0].score <= 3.0
        assert scores[0].evidence_status == "insufficient"
        assert len(scores[0].evidence) == 0

    @pytest.mark.asyncio
    async def test_3_asked_offtopic_answer_included_not_positive_evidence(self):
        job = _job(("Adaptability", 1.0))
        transcript = _transcript("cand_behav_offtopic", job.job_id, [
            ("Tell us about a time priorities changed suddenly.", "My favorite color is blue."),
        ])
        script = ['{"behavioral_score": 2.0, "communication": 5.0, "problem_solving": 5.0, '
                  '"teamwork": 5.0, "adaptability": 2.0, "competency_scores": {"Adaptability": '
                  '{"score": 2.0, "confidence": 0.3, "explanation": "Completely off-topic."}}, '
                  '"strengths": [], "weaknesses": ["Off-topic"], "explanation": "x", "confidence": 0.3}']
        agent = BehavioralEvaluatorAgent(llm_provider=ScriptedLLMProvider(script=script))

        result = await agent.execute(
            job_description=job, parsed_resume=_resume("cand_behav_offtopic"), interview_transcript=transcript,
        )

        scores = result["behavioral_evaluation"].competency_scores
        assert len(scores) == 1
        assert scores[0].competency_name == "Adaptability"
        assert scores[0].score <= 3.0
        assert scores[0].evidence_status == "insufficient"  # off-topic answer never becomes positive evidence

    @pytest.mark.asyncio
    async def test_4_never_asked_competency_not_fabricated(self):
        job = _job(("Teamwork", 0.5), ("Adaptability", 0.5))
        transcript = _transcript("cand_behav_never_asked", job.job_id, [
            ("Describe a time you worked on a team.",
             "I paired daily with a teammate who was struggling and we both grew from it."),
        ])
        script = ['{"behavioral_score": 8.0, "communication": 7.0, "problem_solving": 7.0, '
                  '"teamwork": 8.0, "adaptability": 7.0, "competency_scores": {"Teamwork": '
                  '{"score": 8.0, "confidence": 0.8, "evidence_question_number": 1, '
                  '"explanation": "Concrete pairing example."}}, '
                  '"strengths": [], "weaknesses": [], "explanation": "x", "confidence": 0.8}']
        agent = BehavioralEvaluatorAgent(llm_provider=ScriptedLLMProvider(script=script))

        result = await agent.execute(
            job_description=job, parsed_resume=_resume("cand_behav_never_asked"), interview_transcript=transcript,
        )

        scores = result["behavioral_evaluation"].competency_scores
        names = {s.competency_name for s in scores}
        assert names == {"Teamwork"}  # Adaptability was never asked - must not appear at all


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
