"""Tests for citation validation (Task 4).

This is the load-bearing rule of the matcher: a score whose citations do not
correspond to retrieved evidence is excluded rather than counted, which is
what makes hallucinated evidence and keyword stuffing unable to win.
"""
from services.competency_verifier import (
    CompetencyVerdict,
    EvidenceSufficiency,
    validate_citations,
)


def _verdict(cited, sufficiency=EvidenceSufficiency.SUFFICIENT, score=4):
    return CompetencyVerdict(
        competency_name="Kubernetes operations",
        score=score,
        cited_span_ids=cited,
        rationale="Ran clusters in production",
        evidence_sufficiency=sufficiency,
    )


def test_valid_citations_are_preserved():
    result = validate_citations(_verdict(["sp_1"]), {"sp_1", "sp_2"})
    assert result.evidence_sufficiency is EvidenceSufficiency.SUFFICIENT
    assert result.score == 4


def test_hallucinated_span_id_is_coerced_to_insufficient():
    result = validate_citations(_verdict(["sp_999"]), {"sp_1", "sp_2"})
    assert result.evidence_sufficiency is EvidenceSufficiency.INSUFFICIENT
    assert result.cited_span_ids == []


def test_citing_nothing_is_coerced_to_insufficient():
    result = validate_citations(_verdict([]), {"sp_1"})
    assert result.evidence_sufficiency is EvidenceSufficiency.INSUFFICIENT


def test_partially_hallucinated_citation_keeps_only_real_spans():
    result = validate_citations(_verdict(["sp_1", "sp_999"]), {"sp_1"})
    assert result.cited_span_ids == ["sp_1"]
    assert result.evidence_sufficiency is EvidenceSufficiency.SUFFICIENT


def test_coercion_records_the_reason_in_the_rationale():
    result = validate_citations(_verdict(["sp_999"]), {"sp_1"})
    assert "uncited" in result.rationale.lower() or "citation" in result.rationale.lower()


def test_a_high_score_cannot_survive_without_evidence():
    """A model claiming 5/5 with no citation must not keep the 5."""
    result = validate_citations(_verdict([], score=5), {"sp_1"})
    assert result.evidence_sufficiency is EvidenceSufficiency.INSUFFICIENT
