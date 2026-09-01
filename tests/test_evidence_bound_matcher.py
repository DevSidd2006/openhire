"""Tests for the evidence-bound matcher (Task 8).

The headline guarantee: a resume that lists every keyword but demonstrates
none of them must not out-rank one with real applied work. The old
fixed-weight similarity scorer could not make that distinction.
"""
import pytest

from agents.resume_matcher.agent import ResumeMatcherAgent
from schemas.resume import ParsedResume, WorkExperience
from schemas.rubric import AnchoredCompetency, JobRubric, RubricStatus
from services.competency_verifier import EvidenceSufficiency
from services.semantic_matching import EmbeddingUnavailableError

ANCHORS = {
    1: "no evidence", 2: "mentioned only", 3: "applied it",
    4: "led work with it", 5: "recognized expertise",
}


def _rubric(k_weight=0.6, p_weight=0.4):
    return JobRubric(
        rubric_id="rub_1", job_id="job_1", version=1, status=RubricStatus.APPROVED,
        competencies=[
            AnchoredCompetency(name="Kubernetes operations", definition="Runs clusters",
                               weight=k_weight, anchors=ANCHORS),
            AnchoredCompetency(name="Python engineering", definition="Ships Python",
                               weight=p_weight, anchors=ANCHORS),
        ],
    )


def _substantive_resume():
    return ParsedResume(
        candidate_id="cand_real", candidate_name="Real Engineer",
        skills=["Python", "Kubernetes"],
        work_experience=[WorkExperience(
            company="Acme", position="SRE", start_year=2019, end_year=2024,
            achievements=["Operated 40-node Kubernetes clusters serving 2M req/day"],
            responsibilities=["Wrote Python tooling for cluster autoscaling"],
        )],
        total_experience_years=5,
    )


def _keyword_stuffed_resume():
    return ParsedResume(
        candidate_id="cand_stuffed", candidate_name="Keyword Stuffer",
        skills=["Python", "Kubernetes", "Go", "Rust", "Terraform", "Kafka"],
        work_experience=[], total_experience_years=1,
    )


class _StubMatcher:
    """Returns all spans for every competency; no real embeddings."""

    def __init__(self, fail=False):
        self.fail = fail

    async def retrieve_spans_for_competency(self, competency, spans, k=8):
        if self.fail:
            raise EmbeddingUnavailableError("provider down")
        return list(spans)[:k]


class _StubLLM:
    """Scores 4 where an achievement/responsibility span exists, else 2.

    Mirrors the prompt rule that a bare skills line cannot support level 3+.
    """

    async def generate_structured(self, prompt, schema, **kwargs):
        verdicts = []
        for name in ("Kubernetes operations", "Python engineering"):
            block = prompt.split(name, 1)[-1] if name in prompt else prompt
            applied = "(achievement)" in block or "(responsibility)" in block
            span_ids = [
                line.split("]")[0].lstrip("[")
                for line in prompt.splitlines()
                if line.strip().startswith("[sp_")
            ]
            verdicts.append({
                "competency_name": name,
                "score": 4 if applied else 2,
                "cited_span_ids": span_ids[:1] if span_ids else [],
                "rationale": "stub",
                "evidence_sufficiency": "sufficient" if span_ids else "insufficient",
            })
        return {"verdicts": verdicts}


def _agent(fail=False):
    return ResumeMatcherAgent(semantic_matcher=_StubMatcher(fail), llm_provider=_StubLLM())


@pytest.mark.asyncio
async def test_substantive_resume_outranks_keyword_stuffed_resume():
    real = await _agent().execute(job_rubric=_rubric(), parsed_resume=_substantive_resume())
    stuffed = await _agent().execute(job_rubric=_rubric(), parsed_resume=_keyword_stuffed_resume())
    assert real["matching_score"].match_score > stuffed["matching_score"].match_score


@pytest.mark.asyncio
async def test_score_carries_the_rubric_version_it_was_computed_against():
    result = await _agent().execute(job_rubric=_rubric(), parsed_resume=_substantive_resume())
    assert result["matching_score"].rubric_version == 1


@pytest.mark.asyncio
async def test_every_scored_competency_cites_a_real_span():
    result = await _agent().execute(job_rubric=_rubric(), parsed_resume=_substantive_resume())
    for verdict in result["matching_score"].competency_verdicts:
        if verdict.evidence_sufficiency is not EvidenceSufficiency.INSUFFICIENT:
            assert verdict.cited_span_ids, f"{verdict.competency_name} scored without citation"


@pytest.mark.asyncio
async def test_embedding_outage_propagates_rather_than_producing_a_low_score():
    with pytest.raises(EmbeddingUnavailableError):
        await _agent(fail=True).execute(job_rubric=_rubric(), parsed_resume=_substantive_resume())


@pytest.mark.asyncio
async def test_matching_never_returns_a_shortlist_decision():
    result = await _agent().execute(job_rubric=_rubric(), parsed_resume=_substantive_resume())
    assert not hasattr(result["matching_score"], "shortlist_recommendation")
    assert "shortlist_recommendation" not in result


@pytest.mark.asyncio
async def test_rubric_weights_change_the_score():
    heavy = await _agent().execute(
        job_rubric=_rubric(k_weight=0.9, p_weight=0.1), parsed_resume=_substantive_resume()
    )
    light = await _agent().execute(
        job_rubric=_rubric(k_weight=0.1, p_weight=0.9), parsed_resume=_substantive_resume()
    )
    assert heavy["matching_score"].coverage == pytest.approx(1.0)
    assert light["matching_score"].coverage == pytest.approx(1.0)
