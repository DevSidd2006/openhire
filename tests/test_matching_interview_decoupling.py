"""Matching must never auto-start an AI interview (Task 6).

Scoring ends at the leaderboard; advancing a candidate to interview is a
recruiter action taken after reading the ranking. The pipeline previously
wired match_resumes straight into generate_questions, which made a match
score sufficient to trigger an interview with no human in between.
"""
from langgraph.graph import END

from orchestration.graph import create_pipeline_graph, route_after_matching


def _state(**overrides):
    state = {
        "job_description": object(),
        "parsed_resumes": {},
        "matching_scores": {},
        "shortlisted_candidates": [],
        "recruiter_advanced_candidates": [],
        "errors": [],
    }
    state.update(overrides)
    return state


def test_no_advanced_candidates_halts_before_interview_generation():
    assert route_after_matching(_state()) == END


def test_a_missing_field_also_halts_rather_than_defaulting_to_advance():
    """Absence must fail closed. Defaulting to advance would reintroduce the
    auto-start through the back door."""
    state = _state()
    del state["recruiter_advanced_candidates"]
    assert route_after_matching(state) == END


def test_a_high_scoring_shortlist_alone_does_not_advance():
    """Shortlisting is a matching output; advancing is a recruiter decision.
    A full shortlist with no recruiter action must still halt."""
    state = _state(shortlisted_candidates=["cand_1", "cand_2"])
    assert route_after_matching(state) == END


def test_explicit_recruiter_advance_proceeds_to_interview_generation():
    state = _state(recruiter_advanced_candidates=["cand_1"])
    assert route_after_matching(state) == "generate_questions"


def test_the_unconditional_auto_advance_edge_is_gone():
    """Any path from matching to interview generation must be gated.

    LangGraph still renders a conditional edge in the graph view, so the
    guarantee is not "no edge exists" but "no UNCONDITIONAL edge exists" -
    an unconditional one would advance every candidate automatically.
    """
    graph = create_pipeline_graph().get_graph()
    unconditional = {
        (e.source, e.target) for e in graph.edges if not e.conditional
    }
    assert ("match_resumes", "generate_questions") not in unconditional


def test_matching_can_terminate_the_run_without_interviewing():
    graph = create_pipeline_graph().get_graph()
    targets = {e.target for e in graph.edges if e.source == "match_resumes"}
    assert "__end__" in targets
