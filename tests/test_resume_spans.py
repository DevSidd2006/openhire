"""Tests for resume span extraction (Task 2).

Span IDs must be deterministic: Stage 2 scores cite them and the leaderboard
renders the quoted text behind every score, so an ID that shifted between
runs would silently invalidate stored citations.
"""
from schemas.resume import ParsedResume, Project, WorkExperience
from services.resume_spans import ResumeSpan, extract_spans


def _resume():
    return ParsedResume(
        candidate_id="cand_1",
        candidate_name="Test Person",
        summary="Backend engineer.",
        skills=["Python", "Kubernetes"],
        work_experience=[
            WorkExperience(
                company="Acme",
                position="Senior Engineer",
                start_year=2020,
                end_year=2023,
                responsibilities=["Ran production Kubernetes clusters"],
                achievements=["Cut p99 latency by 40%"],
            )
        ],
        projects=[Project(name="Sched", description="Built a job scheduler", technologies=["Go"])],
    )


def test_extracts_a_span_per_bullet_project_and_skill_line():
    spans = extract_spans(_resume())
    types = {s.span_type for s in spans}
    assert "responsibility" in types
    assert "achievement" in types
    assert "project" in types
    assert "skills" in types
    assert "summary" in types


def test_span_ids_are_unique():
    spans = extract_spans(_resume())
    assert len({s.span_id for s in spans}) == len(spans)


def test_span_ids_are_stable_across_runs():
    first = extract_spans(_resume())
    second = extract_spans(_resume())
    assert [s.span_id for s in first] == [s.span_id for s in second]


def test_blank_text_produces_no_span():
    resume = ParsedResume(candidate_id="c", candidate_name="n", skills=[], summary="   ")
    assert extract_spans(resume) == []


def test_span_text_is_verbatim():
    spans = extract_spans(_resume())
    achievement = next(s for s in spans if s.span_type == "achievement")
    assert achievement.text == "Cut p99 latency by 40%"
