"""Tests for span retrieval and loud embedding failure (Task 3).

The outage tests are the point: returning 0.0 on a provider failure makes an
infrastructure outage indistinguishable from a terrible candidate, which
turns an outage into a wave of rejections.
"""
import pytest

from schemas.rubric import AnchoredCompetency
from services.resume_spans import ResumeSpan
from services.semantic_matching import EmbeddingUnavailableError, SemanticMatcher


class _StubEmbeddings:
    """Embeds by keyword presence so similarity is predictable in tests."""

    def __init__(self, fail=False):
        self.fail = fail

    async def embed_batch(self, texts):
        if self.fail:
            raise RuntimeError("provider down")
        return [[1.0, 0.0] if "kubernetes" in t.lower() else [0.0, 1.0] for t in texts]


def _competency():
    return AnchoredCompetency(
        name="Kubernetes operations",
        definition="Running production Kubernetes",
        weight=1.0,
        anchors={1: "a", 2: "b", 3: "c", 4: "d", 5: "e"},
    )


def _spans():
    return [
        ResumeSpan(span_id="sp_1", span_type="achievement", text="Ran Kubernetes in production"),
        ResumeSpan(span_id="sp_2", span_type="achievement", text="Wrote marketing copy"),
    ]


@pytest.mark.asyncio
async def test_retrieval_ranks_relevant_span_first():
    matcher = SemanticMatcher(embedding_provider=_StubEmbeddings())
    result = await matcher.retrieve_spans_for_competency(_competency(), _spans(), k=2)
    assert result[0].span_id == "sp_1"


@pytest.mark.asyncio
async def test_retrieval_respects_k():
    matcher = SemanticMatcher(embedding_provider=_StubEmbeddings())
    result = await matcher.retrieve_spans_for_competency(_competency(), _spans(), k=1)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_no_spans_yields_no_retrieval():
    matcher = SemanticMatcher(embedding_provider=_StubEmbeddings())
    assert await matcher.retrieve_spans_for_competency(_competency(), []) == []


@pytest.mark.asyncio
async def test_embedding_outage_raises_rather_than_scoring_zero():
    matcher = SemanticMatcher(embedding_provider=_StubEmbeddings(fail=True))
    with pytest.raises(EmbeddingUnavailableError):
        await matcher.retrieve_spans_for_competency(_competency(), _spans())


@pytest.mark.asyncio
async def test_skill_similarity_outage_raises_rather_than_returning_zero():
    matcher = SemanticMatcher(embedding_provider=_StubEmbeddings(fail=True))
    with pytest.raises(EmbeddingUnavailableError):
        await matcher.calculate_skill_semantic_similarity(["Python"], ["Python"], [])


@pytest.mark.asyncio
async def test_jd_similarity_outage_raises_rather_than_returning_zero():
    matcher = SemanticMatcher(embedding_provider=_StubEmbeddings(fail=True))
    with pytest.raises(EmbeddingUnavailableError):
        await matcher.calculate_job_description_similarity("resume text", "job text")
