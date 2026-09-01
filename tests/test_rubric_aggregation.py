"""Tests for coverage-aware aggregation (Task 5).

Coverage is a first-class output: a resume too sparse to judge is an
UNKNOWN, not a REJECT, and conflating the two is where false-negative risk
lives.
"""
import pytest

from schemas.rubric import AnchoredCompetency
from services.competency_verifier import CompetencyVerdict, EvidenceSufficiency
from services.rubric_aggregation import COVERAGE_THRESHOLD, aggregate


def _comp(name, weight):
    return AnchoredCompetency(
        name=name, definition="d", weight=weight,
        anchors={1: "a", 2: "b", 3: "c", 4: "d", 5: "e"},
    )


def _verdict(name, score, sufficiency=EvidenceSufficiency.SUFFICIENT):
    return CompetencyVerdict(
        competency_name=name, score=score,
        cited_span_ids=["sp_1"] if sufficiency is not EvidenceSufficiency.INSUFFICIENT else [],
        rationale="r", evidence_sufficiency=sufficiency,
    )


def test_full_coverage_weighted_score():
    comps = [_comp("A", 0.5), _comp("B", 0.5)]
    result = aggregate(comps, [_verdict("A", 5), _verdict("B", 1)])
    assert result.coverage == pytest.approx(1.0)
    assert result.final_score == pytest.approx(0.6)


def test_weights_actually_matter():
    comps = [_comp("A", 0.9), _comp("B", 0.1)]
    result = aggregate(comps, [_verdict("A", 5), _verdict("B", 1)])
    assert result.final_score > 0.9


def test_insufficient_evidence_is_excluded_not_zeroed():
    comps = [_comp("A", 0.5), _comp("B", 0.5)]
    result = aggregate(
        comps, [_verdict("A", 5), _verdict("B", 1, EvidenceSufficiency.INSUFFICIENT)]
    )
    assert result.coverage == pytest.approx(0.5)
    assert result.final_score == pytest.approx(1.0)
    assert result.uncited_competencies == ["B"]


def test_low_coverage_routes_to_human_review_instead_of_ranking():
    comps = [_comp("A", 0.3), _comp("B", 0.7)]
    result = aggregate(
        comps, [_verdict("A", 4), _verdict("B", 3, EvidenceSufficiency.INSUFFICIENT)]
    )
    assert result.coverage < COVERAGE_THRESHOLD
    assert result.needs_human_review is True


def test_no_evidence_at_all_is_review_not_zero_score():
    comps = [_comp("A", 1.0)]
    result = aggregate(comps, [_verdict("A", 1, EvidenceSufficiency.INSUFFICIENT)])
    assert result.needs_human_review is True
    assert result.final_score is None


def test_a_missing_verdict_is_treated_as_uncited_not_as_zero():
    comps = [_comp("A", 0.5), _comp("B", 0.5)]
    result = aggregate(comps, [_verdict("A", 4)])
    assert result.uncited_competencies == ["B"]
    assert result.coverage == pytest.approx(0.5)


def test_bands_are_assigned_from_absolute_thresholds():
    comps = [_comp("A", 1.0)]
    assert aggregate(comps, [_verdict("A", 5)]).band == "strong"
    assert aggregate(comps, [_verdict("A", 3)]).band == "potential"
    assert aggregate(comps, [_verdict("A", 1)]).band == "weak"


def test_raising_any_competency_score_never_lowers_the_final_score():
    """Monotonicity of the aggregation itself.

    This used to be asserted as a golden evaluation case, but scoring is now
    an LLM judgment, so the end-to-end score is not monotonic in the way a
    pure similarity calculation was. The property still holds - and is worth
    guarding - at the layer that actually owns it: the weighted aggregation.
    """
    comps = [_comp("A", 0.6), _comp("B", 0.4)]
    previous = None
    for score in range(1, 6):
        result = aggregate(comps, [_verdict("A", score), _verdict("B", 3)])
        if previous is not None:
            assert result.final_score >= previous
        previous = result.final_score


def test_gaining_evidence_never_lowers_coverage():
    comps = [_comp("A", 0.5), _comp("B", 0.5)]
    uncovered = aggregate(
        comps, [_verdict("A", 3), _verdict("B", 3, EvidenceSufficiency.INSUFFICIENT)]
    )
    covered = aggregate(comps, [_verdict("A", 3), _verdict("B", 3)])
    assert covered.coverage >= uncovered.coverage


def test_a_zero_weight_competency_cannot_move_the_score():
    """A competency the rubric says does not matter must not affect ranking."""
    comps = [_comp("A", 1.0), _comp("B", 0.0)]
    high = aggregate(comps, [_verdict("A", 4), _verdict("B", 5)])
    low = aggregate(comps, [_verdict("A", 4), _verdict("B", 1)])
    assert high.final_score == low.final_score
