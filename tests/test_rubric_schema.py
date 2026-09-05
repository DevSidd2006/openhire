"""Tests for the anchored rubric schemas (Task 1).

The approval gate is the point of these tests: a malformed rubric silently
corrupts every ranking on its job, so `validate_approvable` must catch every
violation rather than warn about them.
"""
import pytest
from pydantic import ValidationError

from schemas.rubric import AnchoredCompetency, JobRubric, RubricStatus


def _comp(name, weight):
    return AnchoredCompetency(
        name=name,
        definition=f"Ability to do {name}",
        weight=weight,
        anchors={1: "none", 2: "emerging", 3: "proficient", 4: "strong", 5: "exceptional"},
    )


def _rubric(comps, status=RubricStatus.DRAFT):
    return JobRubric(
        rubric_id="rub_1", job_id="job_1", version=1, status=status, competencies=comps
    )


def test_anchors_must_cover_all_five_levels():
    with pytest.raises(ValidationError):
        AnchoredCompetency(
            name="Python", definition="d", weight=1.0, anchors={1: "a", 2: "b", 3: "c"}
        )


def test_anchor_text_may_not_be_blank():
    with pytest.raises(ValidationError):
        AnchoredCompetency(
            name="Python",
            definition="d",
            weight=1.0,
            anchors={1: "a", 2: "  ", 3: "c", 4: "d", 5: "e"},
        )


def test_valid_rubric_is_approvable():
    rubric = _rubric([_comp("Python", 0.5), _comp("SQL", 0.3), _comp("Ownership", 0.2)])
    assert rubric.validate_approvable() == []


def test_too_few_competencies_is_not_approvable():
    rubric = _rubric([_comp("Python", 0.6), _comp("SQL", 0.4)])
    violations = rubric.validate_approvable()
    assert any("3" in v for v in violations)


def test_too_many_competencies_is_not_approvable():
    comps = [_comp(f"C{i}", 1 / 7) for i in range(7)]
    assert _rubric(comps).validate_approvable() != []


def test_weights_must_sum_to_one():
    rubric = _rubric([_comp("Python", 0.5), _comp("SQL", 0.2), _comp("Ownership", 0.2)])
    assert any("sum" in v.lower() for v in rubric.validate_approvable())


def test_duplicate_competency_names_rejected():
    rubric = _rubric([_comp("Python", 0.4), _comp("Python", 0.3), _comp("SQL", 0.3)])
    assert any("duplicate" in v.lower() for v in rubric.validate_approvable())
