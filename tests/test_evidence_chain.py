"""Tests for P0-1 (evidence chain) and P0-2 (job_id threading).

These use a scripted fake LLM provider (never a real API call) so we control
exactly what the "LLM" claims and can assert the agent only turns that claim
into evidence when it's actually traceable to the sealed transcript.
"""
import json
import pytest

from agents.technical_evaluator.agent import TechnicalEvaluatorAgent
from agents.behavioral_evaluator.agent import BehavioralEvaluatorAgent
from agents.resume_auditor.agent import ResumeAuditorAgent
from agents.integrity.agent import IntegrityAgent
from tests.fakes import SingleResponseLLMProvider


class TestTechnicalEvaluatorEvidence:
    @pytest.mark.asyncio
    async def test_evidence_resolves_to_real_question_id(
        self, sample_job_description, sample_parsed_resume, sample_interview_transcript
    ):
        fake = SingleResponseLLMProvider(json.dumps({
            "technical_score": 8.0,
            "competency_scores": {
                "Python": {
                    "score": 9.0,
                    "confidence": 0.9,
                    "evidence_question_number": 1,
                    "explanation": "Demonstrated real async experience",
                },
            },
            "strengths": ["Async programming"],
            "weaknesses": [],
            "explanation": "Strong technical showing",
            "confidence": 0.85,
        }))
        agent = TechnicalEvaluatorAgent(llm_provider=fake)

        result = await agent.execute(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )

        evaluation = result["technical_evaluation"]
        # "System Design" was never returned by the LLM - it must not appear
        # as a fabricated/defaulted competency score.
        assert len(evaluation.competency_scores) == 1
        cs = evaluation.competency_scores[0]
        assert cs.competency_name == "Python"
        assert len(cs.evidence) == 1
        evidence = cs.evidence[0]
        # question_id must be the REAL id from the transcript, never "unknown".
        assert evidence.question_id == "eq_tech_001"
        assert evidence.question_id != "unknown"
        # text must be the candidate's actual answer, not an LLM paraphrase.
        assert evidence.text == sample_interview_transcript.exchanges[0][1].answer_text
        # Top-level evidence is threaded through too.
        assert len(evaluation.evidence) == 1

    @pytest.mark.asyncio
    async def test_ungrounded_competency_not_fabricated(
        self, sample_job_description, sample_parsed_resume, sample_interview_transcript
    ):
        """If the LLM never mentions evidence_question_number, we don't
        invent a question_id - the CompetencyScore is kept (there IS a score
        and explanation) but with no fabricated evidence."""
        fake = SingleResponseLLMProvider(json.dumps({
            "technical_score": 7.0,
            "competency_scores": {
                "Python": {"score": 7.0, "confidence": 0.6, "explanation": "General impression"},
            },
            "strengths": [],
            "weaknesses": [],
            "explanation": "ok",
            "confidence": 0.6,
        }))
        agent = TechnicalEvaluatorAgent(llm_provider=fake)

        result = await agent.execute(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )
        cs = result["technical_evaluation"].competency_scores[0]
        assert cs.evidence == []


class TestBehavioralEvaluatorEvidence:
    @pytest.mark.asyncio
    async def test_evidence_resolves_to_real_question_id(
        self, sample_job_description, sample_parsed_resume, sample_interview_transcript
    ):
        fake = SingleResponseLLMProvider(json.dumps({
            "behavioral_score": 7.5,
            "communication": 7.5,
            "problem_solving": 7.5,
            "teamwork": 7.0,
            "adaptability": 7.0,
            "competency_scores": {
                "System Design": {
                    "score": 6.5,
                    "confidence": 0.7,
                    "evidence_question_number": 2,
                    "explanation": "Methodical debugging approach",
                },
            },
            "strengths": [],
            "weaknesses": [],
            "explanation": "ok",
            "confidence": 0.7,
        }))
        agent = BehavioralEvaluatorAgent(llm_provider=fake)

        result = await agent.execute(
            job_description=sample_job_description,
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )
        evaluation = result["behavioral_evaluation"]
        assert len(evaluation.competency_scores) == 1
        evidence = evaluation.competency_scores[0].evidence[0]
        assert evidence.question_id == "eq_sql_002"
        assert evidence.text == sample_interview_transcript.exchanges[1][1].answer_text
        assert len(evaluation.evidence) == 1


class TestResumeAuditorEvidenceAndJobId:
    @pytest.mark.asyncio
    async def test_supported_claim_gets_real_job_id_and_evidence(
        self, sample_parsed_resume, sample_interview_transcript
    ):
        fake = SingleResponseLLMProvider(json.dumps({
            "verification_status": "supported",
            "confidence": 0.9,
            "evidence_question_number": 1,
            "explanation": "Matches the async project described",
            "requires_human_review": False,
        }))
        agent = ResumeAuditorAgent(llm_provider=fake)

        result = await agent.execute(
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )
        verifications = result["claim_verifications"]
        assert len(verifications) >= 1
        v = verifications[0]
        # job_id comes from the sealed transcript, never "unknown".
        assert v.job_id == sample_interview_transcript.job_id
        assert v.job_id != "unknown"
        assert v.verification_status == "supported"
        assert len(v.evidence) == 1
        assert v.evidence[0].question_id == "eq_tech_001"

    @pytest.mark.asyncio
    async def test_unsupported_claim_without_evidence_routes_to_human_review(
        self, sample_parsed_resume, sample_interview_transcript
    ):
        """If the LLM asserts 'supported' but never points at a real
        exchange, we don't trust the unverifiable assertion - route to human
        review instead of publishing an ungrounded verdict."""
        fake = SingleResponseLLMProvider(json.dumps({
            "verification_status": "supported",
            "confidence": 0.9,
            "explanation": "Looks fine",
            "requires_human_review": False,
        }))
        agent = ResumeAuditorAgent(llm_provider=fake)

        result = await agent.execute(
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )
        v = result["claim_verifications"][0]
        assert v.verification_status == "requires_human_review"
        assert v.requires_human_review is True
        assert v.evidence == []


class TestIntegrityEvidenceAndJobId:
    @pytest.mark.asyncio
    async def test_grounded_flag_kept_with_real_job_id(
        self, sample_parsed_resume, sample_interview_transcript
    ):
        fake = SingleResponseLLMProvider(json.dumps({
            "flags": [
                {
                    "flag_type": "answer_inconsistency",
                    "severity": "medium",
                    "confidence": 0.7,
                    "evidence_question_numbers": [1, 2],
                    "description": "Contradicts earlier claim",
                    "requires_human_review": True,
                }
            ],
            "overall_integrity": "flagged",
            "explanation": "One inconsistency found",
            "confidence": 0.75,
        }))
        agent = IntegrityAgent(llm_provider=fake)

        result = await agent.execute(
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )
        evaluation = result["integrity_evaluation"]
        assert evaluation.job_id == sample_interview_transcript.job_id
        assert evaluation.job_id != "unknown"
        assert len(evaluation.flags) == 1
        flag = evaluation.flags[0]
        assert len(flag.evidence) == 2
        assert {e.question_id for e in flag.evidence} == {"eq_tech_001", "eq_sql_002"}

    @pytest.mark.asyncio
    async def test_ungrounded_flag_is_dropped(self, sample_parsed_resume, sample_interview_transcript):
        """A flag with no resolvable transcript evidence must not survive -
        we don't accuse a candidate without something concrete to point to."""
        fake = SingleResponseLLMProvider(json.dumps({
            "flags": [
                {
                    "flag_type": "suspicious_claim",
                    "severity": "high",
                    "confidence": 0.9,
                    "evidence_question_numbers": [99],  # out of range
                    "description": "Vague claim",
                    "requires_human_review": True,
                }
            ],
            "overall_integrity": "flagged",
            "explanation": "N/A",
            "confidence": 0.5,
        }))
        agent = IntegrityAgent(llm_provider=fake)

        result = await agent.execute(
            parsed_resume=sample_parsed_resume,
            interview_transcript=sample_interview_transcript,
        )
        assert result["integrity_evaluation"].flags == []


class TestIntegrityResumeSummaryFormatting:
    """Regression test: _format_resume_summary used to build its 'Skills:
    ...' line with `lines.extend(f"Skills: ...")` - extend() on a string
    inserts one character per list element, so the skills line silently
    turned into dozens of single-character lines instead of one real line.
    Fixed to lines.append(...)."""

    def test_skills_line_is_one_line_not_exploded_into_characters(self, sample_parsed_resume):
        agent = IntegrityAgent()
        summary = agent._format_resume_summary(sample_parsed_resume)
        lines = summary.split("\n")

        skills_lines = [line for line in lines if line.startswith("Skills:")]
        assert len(skills_lines) == 1

        expected = f"Skills: {', '.join(sample_parsed_resume.skills[:5])}"
        assert skills_lines[0] == expected

        # The bug specifically produced one line per character (e.g. "S",
        # "k", "i", "l", "l", "s", ":", " ", ...) - guard against that shape
        # directly, not just against the exact string.
        assert not any(len(line) == 1 for line in lines)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
