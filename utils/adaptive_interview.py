"""
Deterministic adaptive-interview logic (P3).

This module is the "Python validates and applies" half of the adaptive
interview engine described in the P3 roadmap. It deliberately contains NO
LLM calls - every function here is a pure function of (InterviewState,
JobDescription, ...) -> a new value, so it can be unit-tested directly
without any provider, mock or otherwise (P3 Phase 11: "prefer deterministic
prioritization + LLM question generation... this makes the system
predictable, testable, explainable").

The LLM's only two jobs in the adaptive loop (agents/interviewer/agent.py)
are (1) judge the quality of one answer against one named competency
(AnswerEvaluationResult) and (2) phrase the actual text of the next question
given a decision this module already made (AdaptiveQuestionResult). The LLM
never chooses WHICH competency to target, WHAT action to take, or WHETHER to
finish - those are exactly the decisions NextQuestionDecision carries, and
every one of them is produced and checked by the code below.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from config.settings import MAX_FOLLOW_UPS_PER_COMPETENCY, MIN_CONFIDENCE_FOR_COVERAGE
from schemas.evaluation import EvidenceItem
from schemas.interview import (
    CompetencySignal,
    InterviewAnswer,
    InterviewQuestion,
    InterviewState,
    NextQuestionDecision,
)
from schemas.job import Competency, JobDescription
from schemas.llm_outputs import AnswerEvaluationResult
from utils.evidence import create_transcript_evidence

# Actions that spend a "follow-up budget" on a competency, as opposed to
# ask_new which opens a competency for the first time and finish which asks
# nothing. Kept as a tuple (not re-derived from QuestionAction) because the
# set of "counts against the depth limit" actions is a business rule, not
# structural - it doesn't have to be every non-ask_new action forever.
_DEPTH_ACTIONS = ("follow_up", "probe", "clarify")


class AdaptiveInterviewError(Exception):
    """A proposed decision, question, or state transition violates the
    adaptive interview's invariants (unknown competency, invalid action for
    the current state, answering a question that was never asked, ...).
    Raised instead of silently coercing bad input into something plausible -
    matches the rest of this codebase's "explicit failure over fabricated
    success" policy (P0/P1)."""


class DuplicateQuestionError(AdaptiveInterviewError):
    """A generated question text duplicates (exactly, or after
    normalization) a question already asked in this interview (P3 Phase 9)."""


# ---------------------------------------------------------------------------
# Question deduplication (P3 Phase 9)
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[?.!,;:]+$")


def normalize_question_text(text: str) -> str:
    """Lightweight, deterministic normalization for duplicate detection -
    lowercase, collapsed whitespace, no trailing punctuation. No embeddings/
    fuzzy matching (P3 Phase 9 explicitly rules that out for this phase);
    this only catches exact-or-near-exact repeats, which is the stated
    minimum bar."""
    normalized = text.strip().lower()
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    normalized = _TRAILING_PUNCT_RE.sub("", normalized)
    return normalized


def previously_asked_texts(state: InterviewState) -> List[str]:
    """All question texts already asked in this interview, including the
    currently-pending question (if any) - reused by both dedup checks and
    prompt construction, so the two can never see a different history."""
    texts = [q.question_text for q, _ in state.exchanges]
    if state.current_question is not None:
        texts.append(state.current_question.question_text)
    return texts


def is_duplicate_question(question_text: str, state: InterviewState) -> bool:
    """True if `question_text` matches (after normalization) any question
    already asked in this interview."""
    normalized = normalize_question_text(question_text)
    return any(normalized == normalize_question_text(t) for t in previously_asked_texts(state))


# ---------------------------------------------------------------------------
# Competency prioritization (P3 Phase 6 / Phase 11)
# ---------------------------------------------------------------------------

def questions_asked_for_competency(state: InterviewState, competency: str) -> int:
    """Derived from `exchanges` (plus a pending `current_question`) rather
    than stored as its own counter, so it can never drift out of sync with
    the actual question history (P3 Phase 2: avoid duplicating transcript
    data)."""
    count = sum(1 for q, _ in state.exchanges if q.competency == competency)
    if state.current_question is not None and state.current_question.competency == competency:
        count += 1
    return count


def _importance_multiplier(comp: Competency) -> float:
    """Required/high-importance competencies are prioritized over
    explicitly low-importance ones (P3 Phase 6/11: "required vs preferred
    competency"). JobDescription.Competency.importance is optional and
    mostly unset in this codebase's real data (see data/sample_job.json), so
    unset/"medium" is treated as the neutral default rather than assuming
    every competency is either critical or nice-to-have."""
    importance = (comp.importance or "medium").lower()
    if importance in ("critical", "high"):
        return 1.3
    if importance == "low":
        return 0.7
    return 1.0


def is_required_competency(comp: Competency) -> bool:
    """A competency counts as "required" for termination purposes unless
    explicitly marked low-importance - see _importance_multiplier for the
    same reasoning applied to prioritization."""
    return (comp.importance or "medium").lower() != "low"


def _uncertainty_factor(state: InterviewState, competency: str) -> float:
    """How much is still unknown about this competency, in [0, 1].

    - Never asked: maximal uncertainty (confidence treated as 0.0).
    - Asked but evidence_status == "insufficient": uncertainty is `1 -
      confidence`, i.e. a low-confidence insufficient answer (SQL: conf
      0.32) is MORE uncertain than a higher-confidence one, and both stay
      close to fully uncertain regardless.
    - Asked and evidence_status == "supported": uncertainty is heavily
      damped (x0.15) even at moderate confidence, so a competency with real
      grounded evidence is deprioritized relative to one that is still
      genuinely insufficient - this is the mechanism behind "already
      strongly supported competency is deprioritized" without hardcoding
      "lowest score wins" (P3 Phase 6 explicitly rules that simplistic rule
      out).
    """
    confidence = state.competency_confidence.get(competency, 0.0)
    status = state.evidence_coverage.get(competency)
    base = max(0.0, 1.0 - confidence)
    if status == "supported":
        return base * 0.15
    return base


def competency_priority(comp: Competency, state: InterviewState) -> float:
    """Deterministic priority score for asking about `comp` next. Combines
    rubric weight, importance, and how uncertain we still are about it, then
    applies a small penalty for how many questions have already gone to it
    (diversity - P3 Phase 6: "question diversity")."""
    asked = questions_asked_for_competency(state, comp.name)
    diversity_penalty = 0.05 * asked
    score = comp.weight * _importance_multiplier(comp) * _uncertainty_factor(state, comp.name)
    return max(0.0, score - diversity_penalty)


def is_competency_exhausted(state: InterviewState, competency: str) -> bool:
    """True once the follow-up depth budget for this competency is spent
    (P3 Phase 8) - such a competency is no longer eligible to be selected as
    the next target, regardless of how uncertain it still is."""
    return state.follow_up_counts.get(competency, 0) >= MAX_FOLLOW_UPS_PER_COMPETENCY


def select_target_competency(state: InterviewState, job: JobDescription) -> Optional[str]:
    """Pick the single best competency to target next, or None if nothing
    remains worth asking about (every competency is either well-supported
    or has exhausted its follow-up budget while still insufficient).

    Deterministic tie-break: highest priority score wins; ties broken by
    rubric weight, then by name - so the same state+job always yields the
    same choice (P3 Phase 11: predictable/testable)."""
    candidates = [c for c in job.competencies if not is_competency_exhausted(state, c.name)]
    scored = [(competency_priority(c, state), c) for c in candidates]
    scored = [(score, c) for score, c in scored if score > 0.0]
    if not scored:
        return None
    scored.sort(key=lambda pair: (-pair[0], -pair[1].weight, pair[1].name))
    return scored[0][1].name


# ---------------------------------------------------------------------------
# Action selection (P3 Phase 7)
# ---------------------------------------------------------------------------

def _decide_action_for_competency(state: InterviewState, competency: str) -> str:
    """Given a chosen target competency, decide HOW to ask about it next.
    Only called for a competency select_target_competency actually returned,
    so it is never exhausted here."""
    status = state.evidence_coverage.get(competency)
    if status is None:
        return "ask_new"

    signal = state.competency_signals.get(competency, CompetencySignal())
    if status == "insufficient":
        if signal.is_vague:
            return "follow_up"
        if signal.missing_detail:
            return "probe"
        # Insufficient with no specific signal recorded (e.g. off-topic/
        # empty answer) - ask the candidate to address the question directly
        # rather than guessing at a deeper follow-up.
        return "clarify"

    # status == "supported" but confidence still below threshold: one more
    # probe to firm up confidence before this competency can count as
    # covered for termination purposes.
    return "probe"


def _expected_evidence_for(competency: str, signal: CompetencySignal) -> Optional[str]:
    if signal.missing_detail:
        return signal.missing_detail
    return f"concrete, specific evidence of {competency}"


def _difficulty_for(state: InterviewState, competency: str) -> str:
    """Simple progression: first question on a competency is medium, a
    supported-but-not-yet-confident competency gets a harder probe, an
    insufficient one gets an easier/more concrete follow-up so the candidate
    has a real chance to demonstrate the competency rather than repeating
    the same difficulty that already failed to elicit evidence."""
    status = state.evidence_coverage.get(competency)
    if status is None:
        return "medium"
    if status == "supported":
        return "hard"
    return "easy"


# ---------------------------------------------------------------------------
# Termination (P3 Phase 10)
# ---------------------------------------------------------------------------

def check_termination(
    state: InterviewState,
    job: JobDescription,
    *,
    force_reason: Optional[str] = None,
    elapsed_seconds: Optional[float] = None,
    time_budget_seconds: Optional[float] = None,
) -> Optional[str]:
    """Return a termination_reason if the interview must stop now, else
    None. This is the single source of truth for "is FINISH allowed" -
    decide_next_action() calls it to decide whether to propose FINISH, and
    it is exported separately so a proposed FINISH from any other source can
    be independently re-checked (P3 Phase 10: "LLM proposes FINISH -> Python
    checks termination criteria -> allowed/rejected")."""
    if force_reason:
        return force_reason

    if state.questions_asked >= state.max_questions:
        return "max_questions_reached"

    if time_budget_seconds is not None and elapsed_seconds is not None:
        if elapsed_seconds >= time_budget_seconds:
            return "time_budget_exceeded"

    required = [c for c in job.competencies if is_required_competency(c)]
    if required and all(
        state.evidence_coverage.get(c.name) == "supported"
        and state.competency_confidence.get(c.name, 0.0) >= MIN_CONFIDENCE_FOR_COVERAGE
        for c in required
    ):
        return "sufficient_evidence_collected"

    if select_target_competency(state, job) is None:
        return "no_further_progress_possible"

    return None


# ---------------------------------------------------------------------------
# Top-level decision (P3 Phase 4 / 5)
# ---------------------------------------------------------------------------

def decide_next_action(
    state: InterviewState,
    job: JobDescription,
    *,
    force_reason: Optional[str] = None,
    elapsed_seconds: Optional[float] = None,
    time_budget_seconds: Optional[float] = None,
) -> NextQuestionDecision:
    """The single deterministic entry point that decides what happens next
    in the interview. Never calls an LLM. Always returns a valid
    NextQuestionDecision - callers pass this straight to
    validate_decision()/apply_decision() without needing their own
    branching on top of it."""
    reason = check_termination(
        state, job,
        force_reason=force_reason,
        elapsed_seconds=elapsed_seconds,
        time_budget_seconds=time_budget_seconds,
    )
    if reason:
        return NextQuestionDecision(
            action="finish",
            target_competency=None,
            reason=f"Interview finished: {reason}",
            termination_reason=reason,
        )

    competency = select_target_competency(state, job)
    if competency is None:
        # Defensive: check_termination() should already have caught this via
        # "no_further_progress_possible", so this is unreachable in
        # practice, but decide_next_action must never propose asking about
        # nothing.
        return NextQuestionDecision(
            action="finish",
            target_competency=None,
            reason="Interview finished: no remaining competency to target",
            termination_reason="no_further_progress_possible",
        )

    action = _decide_action_for_competency(state, competency)
    signal = state.competency_signals.get(competency, CompetencySignal())
    return NextQuestionDecision(
        action=action,
        target_competency=competency,
        reason=_reason_for(action, competency, state),
        difficulty=_difficulty_for(state, competency),
        expected_evidence=_expected_evidence_for(competency, signal),
    )


def _reason_for(action: str, competency: str, state: InterviewState) -> str:
    status = state.evidence_coverage.get(competency)
    confidence = state.competency_confidence.get(competency, 0.0)
    if action == "ask_new":
        return f"{competency} has not been asked about yet"
    if status == "insufficient":
        return f"{competency} evidence is insufficient (confidence {confidence:.2f})"
    return f"{competency} is supported but confidence ({confidence:.2f}) is below the coverage threshold"


# ---------------------------------------------------------------------------
# Validation (P3 Phase 4 / 5 - "Python validates")
# ---------------------------------------------------------------------------

def validate_decision(decision: NextQuestionDecision, state: InterviewState, job: JobDescription) -> None:
    """Raise AdaptiveInterviewError if `decision` is not one the engine may
    act on. Called on every decision (including the ones this module's own
    decide_next_action() produces) as defense in depth, and is what makes
    "invalid action rejected" / "invalid competency rejected" a real,
    enforced guarantee rather than just an emergent property of
    decide_next_action() always behaving - a decision from any other source
    (e.g. a future LLM-proposed decision) must pass through here too before
    it can affect state."""
    competency_names = {c.name for c in job.competencies}

    if decision.action == "finish":
        if decision.termination_reason is None:
            raise AdaptiveInterviewError("A finish decision must carry a termination_reason")
        allowed = check_termination(state, job, force_reason=None)
        # A finish decision is only allowed if termination criteria are
        # ACTUALLY met right now, or the reason itself is a forced/explicit
        # one - a decision cannot simply assert "sufficient_evidence" when
        # the state does not support it.
        if allowed is None and decision.termination_reason != "explicit_termination":
            raise AdaptiveInterviewError(
                f"FINISH rejected: termination criteria not met "
                f"(proposed reason was {decision.termination_reason!r})"
            )
        return

    if decision.target_competency is None:
        raise AdaptiveInterviewError(f"Action {decision.action!r} requires a target_competency")
    if decision.target_competency not in competency_names:
        raise AdaptiveInterviewError(
            f"Unknown competency {decision.target_competency!r} - not in job rubric {sorted(competency_names)}"
        )
    if decision.action in _DEPTH_ACTIONS and is_competency_exhausted(state, decision.target_competency):
        raise AdaptiveInterviewError(
            f"Competency {decision.target_competency!r} has exhausted its follow-up budget"
        )


# ---------------------------------------------------------------------------
# State transitions (P3 Phase 5)
# ---------------------------------------------------------------------------

def start_question(state: InterviewState, question: InterviewQuestion, decision: NextQuestionDecision) -> InterviewState:
    """Apply an accepted decision + the question generated for it to the
    state. Pure: returns a new InterviewState rather than mutating the
    input, so a rejected/discarded question never leaves a partial trace."""
    if state.current_question is not None:
        raise AdaptiveInterviewError(
            "Cannot start a new question while one is still awaiting an answer"
        )

    covered = list(state.covered_competencies)
    if question.competency and question.competency not in covered:
        covered.append(question.competency)

    follow_up_counts = dict(state.follow_up_counts)
    if decision.action in _DEPTH_ACTIONS and question.competency:
        follow_up_counts[question.competency] = follow_up_counts.get(question.competency, 0) + 1

    return state.model_copy(update={
        "current_question": question,
        "questions_asked": state.questions_asked + 1,
        "covered_competencies": covered,
        "follow_up_counts": follow_up_counts,
    })


def build_answer_evidence(
    state: InterviewState,
    question: InterviewQuestion,
    answer: InterviewAnswer,
    evaluation: AnswerEvaluationResult,
    competency: str,
) -> EvidenceItem:
    """Build the EvidenceItem for one evaluated answer, straight from the
    real question/answer pair - never from an LLM-supplied text (mirrors
    utils.evidence.resolve_transcript_evidence's guarantee, specialized for
    the single-exchange case an adaptive turn always has)."""
    evidence_type = "supporting" if evaluation.evidence_status == "supported" else "insufficient"
    return create_transcript_evidence(
        text=answer.answer_text,
        question_id=question.question_id,
        agent="interviewer",
        explanation=evaluation.explanation,
        candidate_id=state.candidate_id,
        answer_id=question.question_id,
        evidence_type=evidence_type,
        competency=competency,
        relevance=evaluation.confidence,
    )


def _updated_running_score(state: InterviewState, competency: str, evaluation: AnswerEvaluationResult) -> float:
    """Confidence-weighted running average across every answer seen so far
    for this competency. Simple by design (P3 Phase 6 rules out anything
    beyond deterministic, explainable prioritization) - not a Bayesian
    estimator; a more principled aggregation is a reasonable P4 refinement."""
    prior_score = state.competency_scores.get(competency)
    prior_confidence = state.competency_confidence.get(competency, 0.0)
    if prior_score is None:
        return evaluation.score
    total_weight = prior_confidence + evaluation.confidence
    if total_weight <= 0:
        return evaluation.score
    return (prior_score * prior_confidence + evaluation.score * evaluation.confidence) / total_weight


def record_answer(
    state: InterviewState,
    answer: InterviewAnswer,
    evaluation: AnswerEvaluationResult,
    competency: str,
) -> InterviewState:
    """Apply an evaluated answer to the pending current_question. This is
    the ONLY function that moves a question from "pending" into
    `exchanges`, and it only ever reads state fields + the evaluation's own
    typed fields (score/confidence/evidence_status/is_vague/missing_detail)
    - candidate_id, job_id, question count, and every other structural field
    come from `state` itself, never from the LLM's output object, so no
    LLM response can mutate them regardless of what extra keys it contains
    (P3 Phase 5/18: "LLM proposes, Python validates and applies")."""
    question = state.current_question
    if question is None:
        raise AdaptiveInterviewError("No question is currently pending an answer")
    if question.question_id != answer.question_id:
        raise AdaptiveInterviewError(
            f"Answer question_id {answer.question_id!r} does not match the pending "
            f"question {question.question_id!r}"
        )

    new_confidence = max(state.competency_confidence.get(competency, 0.0), evaluation.confidence)
    new_score = _updated_running_score(state, competency, evaluation)

    competency_scores = {**state.competency_scores, competency: new_score}
    competency_confidence = {**state.competency_confidence, competency: new_confidence}
    evidence_coverage = {**state.evidence_coverage, competency: evaluation.evidence_status}
    competency_signals = {
        **state.competency_signals,
        competency: CompetencySignal(is_vague=evaluation.is_vague, missing_detail=evaluation.missing_detail),
    }

    return state.model_copy(update={
        "current_question": None,
        "exchanges": state.exchanges + [(question, answer)],
        "questions_answered": state.questions_answered + 1,
        "competency_scores": competency_scores,
        "competency_confidence": competency_confidence,
        "evidence_coverage": evidence_coverage,
        "competency_signals": competency_signals,
    })


def apply_finish(state: InterviewState, decision: NextQuestionDecision) -> InterviewState:
    """Apply a validated FINISH decision - the only place is_completed and
    termination_reason are ever set."""
    if decision.action != "finish":
        raise AdaptiveInterviewError("apply_finish called with a non-finish decision")
    return state.model_copy(update={
        "is_completed": True,
        "termination_reason": decision.termination_reason,
    })
