"""
P6: per-agent adapters. P8B.1: provider-injection.

Each function here takes one EvaluationCase (and, as of P8B.1, an optional
injected LLMProvider) and returns a `ctx` dict for evaluation/metrics.py to
check. An adapter's job is ONLY to (1) build real domain objects
(JobDescription, ParsedResume, InterviewTranscript, ...) from the case's
plain-dict `input`, (2) construct the REAL agent (agents/*/agent.py - never
a reimplementation) with the provider it should use, (3) call the agent's
real execute(), and (4) hand back whatever the metrics need to check
(`result`, `output`, and any supporting objects like `transcript`).

Provider selection (P8B.1 - see `_resolve_provider` below): every adapter
that calls an LLM takes a `provider: Optional[LLMProvider] = None` keyword.

  - `provider=None` (the default, and everything before P8B.1): builds a
    fresh `ScriptedLLMProvider` from the case's own `input["script"]`,
    EXACTLY as before - existing behavior is completely unchanged when no
    provider is injected. This is what `python -m evaluation.runner` still
    does by default.
  - `provider=<a real LLMProvider>` (e.g. a `GeminiProvider`, set up by
    evaluation/runner.py's `--provider gemini`): used directly, and
    `input["script"]` is ignored entirely - a real provider generates its
    own response from the real prompt the agent builds out of the case's
    other input fields (the real job description text, resume text,
    transcript, ...), it does not consume a canned script. The exact same
    adapter function, the exact same agent, the exact same
    BaseAgent.call_llm_structured retry/validation path runs either way -
    only which LLMProvider answers the call changes.

Why ScriptedLLMProvider (not the shared MockLLMProvider) for the default
path: MockLLMProvider returns the same fixed canned response regardless of
prompt content (see providers/llm/mock.py), so it cannot represent "a
strong answer" vs "a weak answer" - there would be nothing to evaluate.
ScriptedLLMProvider lets each case simulate exactly what a CORRECTLY OR
ADVERSARIALLY behaving LLM would return for that scenario, so these
evaluations measure whether the AGENT CODE handles that judgment correctly
- not whether a real LLM would produce good judgment in the first place.
P8B.1 is what makes that second question finally askable, using the exact
same cases and adapters, by injecting a real provider instead.
"""
from typing import Any, Dict, Optional

from providers.base import LLMProvider

from agents.behavioral_evaluator.agent import BehavioralEvaluatorAgent
from agents.bias_checker.agent import BiasCheckerAgent
from agents.integrity.agent import IntegrityAgent
from agents.interviewer.agent import InterviewerAgent
from agents.jd_analyzer.agent import JDAnalyzerAgent
from agents.leaderboard.agent import LeaderboardAgent
from agents.report_generator.agent import ReportGeneratorAgent
from agents.resume_auditor.agent import ResumeAuditorAgent
from agents.resume_matcher.agent import ResumeMatcherAgent
from agents.resume_parser.agent import ResumeParserAgent
from agents.scoring.agent import ScoringAgent
from agents.technical_evaluator.agent import TechnicalEvaluatorAgent
from evaluation.models import EvaluationCase
from providers.base import LLMPermanentError, LLMTransientError
from schemas.evaluation import BehavioralEvaluation, MatchingScore, TechnicalEvaluation
from schemas.interview import InterviewAnswer, InterviewQuestion, InterviewTranscript
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from schemas.scoring import CandidateReport, CandidateScores
from tests.fakes import ScriptedLLMProvider
from utils.interview_session import InterviewSessionRunner

# JSON case files can't represent a raw Python exception instance (what
# ScriptedLLMProvider's own script format requires to simulate a provider-
# level failure) - a script entry of this shape is translated into a real
# exception object here before the case ever reaches ScriptedLLMProvider.
_ERROR_MARKERS = {
    "__llm_permanent_error__": LLMPermanentError,
    "__llm_transient_error__": LLMTransientError,
}


def _resolve_script_entry(entry):
    if isinstance(entry, dict) and len(entry) == 1:
        (marker, message), = entry.items()
        error_cls = _ERROR_MARKERS.get(marker)
        if error_cls is not None:
            return error_cls(message)
    return entry


def _resolve_provider(script, provider: Optional[LLMProvider]) -> LLMProvider:
    """The single choke point every LLM-calling adapter goes through
    (P8B.1). If a real provider was injected, use it AS-IS and ignore
    `script` entirely - no adapter builds its own hidden ScriptedLLMProvider
    once a real provider is in play. Otherwise (default/mock mode,
    provider=None) build a fresh ScriptedLLMProvider from the case's own
    script, exactly as every case did before P8B.1."""
    if provider is not None:
        return provider
    resolved = [_resolve_script_entry(e) for e in script] if script else []
    return ScriptedLLMProvider(script=resolved)


# ---------------------------------------------------------------------------
# JD Analyzer
# ---------------------------------------------------------------------------

async def adapt_jd_analyzer(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    inp = case.input
    agent = JDAnalyzerAgent(llm_provider=_resolve_provider(inp.get("script"), provider))
    result = await agent.execute(job_description=inp["job_description"], job_id=inp.get("job_id", "job_case"))
    return {"result": result, "output": result.get("job_description"), "source_text": inp["job_description"]}


# ---------------------------------------------------------------------------
# Resume Parser
# ---------------------------------------------------------------------------

async def adapt_resume_parser(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    inp = case.input
    agent = ResumeParserAgent(llm_provider=_resolve_provider(inp.get("script"), provider))
    result = await agent.execute(
        resume_text=inp["resume_text"],
        candidate_id=inp.get("candidate_id", "cand_case"),
        candidate_name=inp.get("candidate_name", "Case Candidate"),
    )
    return {"result": result, "output": result.get("parsed_resume"), "source_text": inp["resume_text"]}


# ---------------------------------------------------------------------------
# Resume Matcher (deterministic, no LLM)
# ---------------------------------------------------------------------------

async def adapt_resume_matcher(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    # No LLM call in this agent - `provider` is accepted only for a uniform
    # adapter interface (evaluation/runner.py calls every adapter the same
    # way) and is otherwise unused here.
    inp = case.input
    job = JobDescription.model_validate(inp["job"])
    resume = ParsedResume.model_validate(inp["resume"])
    agent = ResumeMatcherAgent()
    result = await agent.execute(job_description=job, parsed_resume=resume)
    ctx: Dict[str, Any] = {"result": result, "output": result.get("matching_score"), "job": job, "resume": resume, "facts": {}}

    monotonic = inp.get("monotonic_check")
    if monotonic:
        resume2 = resume.model_copy(update={"skills": resume.skills + [monotonic["added_required_skill"]]})
        result2 = await agent.execute(job_description=job, parsed_resume=resume2)
        ms1, ms2 = result["matching_score"], result2["matching_score"]
        ctx["facts"]["required_skill_match_non_decreasing"] = ms2.match_score >= ms1.match_score
    return ctx


# ---------------------------------------------------------------------------
# Interviewer - single-turn answer evaluation
# ---------------------------------------------------------------------------

async def adapt_interviewer_evaluate(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    inp = case.input
    agent = InterviewerAgent(llm_provider=_resolve_provider(inp.get("script"), provider))
    question = InterviewQuestion.model_validate(inp["question"])
    answer = InterviewAnswer(question_id=question.question_id, answer_text=inp["answer_text"])
    evaluation = await agent.evaluate_answer(question, answer, inp["target_competency"])
    return {"result": {"evaluation": evaluation.model_dump()}, "output": evaluation}


async def adapt_interviewer_session_turn(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    """Drives ONE real start()+submit_answer() turn through
    InterviewSessionRunner (P4) - used for checks that need the actual
    EvidenceItem a turn produces (evaluate_answer() alone never builds
    evidence; only the runner does, via
    utils.adaptive_interview.build_answer_evidence)."""
    inp = case.input
    job = JobDescription.model_validate(inp["job"])
    resume = ParsedResume.model_validate(inp["resume"])
    interviewer = InterviewerAgent(llm_provider=_resolve_provider(inp.get("script"), provider))
    runner = InterviewSessionRunner(
        job, resume, interviewer=interviewer, candidate_id=inp.get("candidate_id", "cand_case"),
        max_questions=inp.get("max_questions"),
    )
    question = await runner.start()
    result = await runner.submit_answer(inp["answer_text"])
    return {
        "result": {"question": question.model_dump(), "submission": result.model_dump()},
        "output": result,
        "candidate_id": inp.get("candidate_id", "cand_case"),
    }


# ---------------------------------------------------------------------------
# Interviewer - full adaptive sequence (drives the REAL InterviewSessionRunner,
# P4 - never reimplements the adaptive loop here)
# ---------------------------------------------------------------------------

async def adapt_interviewer_adaptive_sequence(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    """Runs two interviews from the SAME job/resume but different simulated
    answer quality, and records whether the resulting question sequence
    actually differs (P6 Phase 9: "do not accept an adaptive interviewer
    that produces the same fixed sequence regardless of candidate
    answers"). NOTE: this case type makes multiple LLM calls per scenario
    (one question + one evaluation per turn) x two scenarios - expensive in
    real-provider mode; prefer single-turn case types for a real-provider
    pilot (see evaluation/runner.py's --provider gemini cost notes).

    P8B.5: also computes `scenario_{a,b}_terminated_via_sufficient_evidence`
    - the reliable answer-quality signal (see the `facts` dict comment
    below for why the raw competency-sequence comparison alone is not)."""
    inp = case.input
    job = JobDescription.model_validate(inp["job"])
    resume = ParsedResume.model_validate(inp["resume"])
    candidate_id = inp.get("candidate_id", "cand_case")

    async def run_scenario(scenario):
        interviewer = InterviewerAgent(llm_provider=_resolve_provider(scenario.get("script"), provider))
        runner = InterviewSessionRunner(
            job, resume, interviewer=interviewer, candidate_id=candidate_id,
            max_questions=scenario.get("max_questions"),
        )
        question = await runner.start()
        trace = [(question.competency, question.question_id)] if question else []
        for answer_text in scenario["answer_texts"]:
            if question is None:
                break
            result = await runner.submit_answer(answer_text)
            question = result.next_question
            if question is not None:
                trace.append((question.competency, question.question_id))
        return runner, trace

    runner_a, trace_a = await run_scenario(inp["scenario_a"])
    scenario_b = inp.get("scenario_b")
    runner_b, trace_b = await run_scenario(scenario_b) if scenario_b else (runner_a, trace_a)

    question_ids_a = [qid for _, qid in trace_a]
    state_a = runner_a.get_state()
    state_b = runner_b.get_state()
    # Compare on COMPETENCY sequence only, never question_id - both scenarios
    # use the same candidate_id by design, so their deterministic question
    # IDs (q_<candidate_id>_NNN) coincide regardless of content; the
    # competency sequence (and how many turns it takes) is the real signal
    # of adaptive behavior.
    competencies_a = [c for c, _ in trace_a]
    competencies_b = [c for c, _ in trace_b]
    facts = {
        # P8B.5 finding: kept for backward-compatible reporting, but this
        # fact is NOT a reliable quality signal on its own - see
        # utils/adaptive_interview.py's competency_priority/
        # _uncertainty_factor. When two-or-more competencies of equal
        # weight are untouched, the deterministic breadth-first diversity
        # penalty makes the engine prefer opening a brand-new competency
        # over following up on the just-answered one REGARDLESS of how
        # weak that answer was (uncertainty for a never-asked competency is
        # always 1.0, undamped; an asked-but-insufficient competency's
        # uncertainty is at most 1.0 minus a diversity penalty that only
        # grows) - so the FIRST couple of competency choices can coincide
        # between a strong-answer and a weak-answer scenario even though
        # the engine is behaving exactly as documented. Verified
        # deterministically in mock mode (zero LLM variance) before this
        # was ever attributed to real-model behavior - see the P8B.5
        # report. `scenario_a_terminated_via_sufficient_evidence` /
        # `scenario_b_terminated_via_sufficient_evidence` below are the
        # reliable signal: MIN_CONFIDENCE_FOR_COVERAGE + evidence_status
        # are exactly what SHOULD differ by answer quality, and checking
        # them directly does not depend on the breadth-first tie-break.
        "sequence_differs_by_answer_quality": competencies_a != competencies_b,
        "no_duplicate_questions_in_scenario_a": len(question_ids_a) == len(set(question_ids_a)),
        "scenario_a_sealed": runner_a.status.value == "sealed",
        "scenario_b_sealed": runner_b.status.value == "sealed",
        "scenario_a_terminated_via_sufficient_evidence": state_a.termination_reason == "sufficient_evidence_collected",
        "scenario_b_terminated_via_sufficient_evidence": state_b.termination_reason == "sufficient_evidence_collected",
    }
    return {
        "result": {
            "trace_a": trace_a, "trace_b": trace_b,
            "termination_reason": state_a.termination_reason,
            "termination_reason_b": state_b.termination_reason,
        },
        "output": state_a,
        "facts": facts,
    }


# ---------------------------------------------------------------------------
# Technical Evaluator
# ---------------------------------------------------------------------------

async def adapt_technical_evaluator(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    inp = case.input
    job = JobDescription.model_validate(inp["job"])
    resume = ParsedResume.model_validate(inp["resume"])
    transcript = InterviewTranscript.model_validate(inp["transcript"])
    agent = TechnicalEvaluatorAgent(llm_provider=_resolve_provider(inp.get("script"), provider))
    result = await agent.execute(job_description=job, parsed_resume=resume, interview_transcript=transcript)
    output = result.get("technical_evaluation")
    return {
        "result": result, "output": output, "transcript": transcript,
        "candidate_id": resume.candidate_id, "job_id": job.job_id,
    }


# ---------------------------------------------------------------------------
# Behavioral Evaluator
# ---------------------------------------------------------------------------

async def adapt_behavioral_evaluator(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    inp = case.input
    job = JobDescription.model_validate(inp["job"])
    resume = ParsedResume.model_validate(inp["resume"])
    transcript = InterviewTranscript.model_validate(inp["transcript"])
    agent = BehavioralEvaluatorAgent(llm_provider=_resolve_provider(inp.get("script"), provider))
    result = await agent.execute(job_description=job, parsed_resume=resume, interview_transcript=transcript)
    output = result.get("behavioral_evaluation")
    return {
        "result": result, "output": output, "transcript": transcript,
        "candidate_id": resume.candidate_id, "job_id": job.job_id,
    }


# ---------------------------------------------------------------------------
# Resume Auditor
# ---------------------------------------------------------------------------

async def adapt_resume_auditor(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    # NOTE: ResumeAuditorAgent makes one LLM call PER extracted resume claim
    # (up to ~4, see agents/resume_auditor/agent.py._extract_resume_claims) -
    # costlier than a single-call case in real-provider mode.
    inp = case.input
    resume = ParsedResume.model_validate(inp["resume"])
    transcript = InterviewTranscript.model_validate(inp["transcript"])
    agent = ResumeAuditorAgent(llm_provider=_resolve_provider(inp.get("script"), provider))
    result = await agent.execute(parsed_resume=resume, interview_transcript=transcript)
    verifications = result.get("claim_verifications") or []
    return {
        "result": result, "output": verifications, "transcript": transcript,
        "candidate_id": resume.candidate_id, "job_id": transcript.job_id,
    }


# ---------------------------------------------------------------------------
# Integrity Checker
# ---------------------------------------------------------------------------

async def adapt_integrity(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    inp = case.input
    resume = ParsedResume.model_validate(inp["resume"])
    transcript = InterviewTranscript.model_validate(inp["transcript"])
    agent = IntegrityAgent(llm_provider=_resolve_provider(inp.get("script"), provider))
    result = await agent.execute(parsed_resume=resume, interview_transcript=transcript)
    output = result.get("integrity_evaluation")
    return {
        "result": result, "output": output, "transcript": transcript,
        "candidate_id": resume.candidate_id, "job_id": transcript.job_id,
    }


# ---------------------------------------------------------------------------
# Bias Checker
# ---------------------------------------------------------------------------

async def adapt_bias_checker(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    inp = case.input
    transcript = InterviewTranscript.model_validate(inp["transcript"])
    sections = inp.get("evaluation_sections", {})
    agent = BiasCheckerAgent(llm_provider=_resolve_provider(inp.get("script"), provider))
    result = await agent.execute(
        interview_transcript=transcript,
        candidate_id=inp.get("candidate_id", "cand_case"),
        job_id=inp.get("job_id", "job_case"),
        technical_score=inp.get("technical_score"),
        behavioral_score=inp.get("behavioral_score"),
        evaluation_sections=sections,
    )
    output = result.get("bias_audit")
    facts = {}
    if output is not None:
        section_texts = set(sections.values())
        facts["all_flag_evidence_matches_provided_sections"] = all(
            ev.text in section_texts for flag in output.flags for ev in flag.evidence
        )
    return {
        "result": result, "output": output, "transcript": transcript,
        "candidate_id": inp.get("candidate_id", "cand_case"), "job_id": inp.get("job_id", "job_case"),
        "facts": facts,
    }


# ---------------------------------------------------------------------------
# Scoring (deterministic, no LLM)
# ---------------------------------------------------------------------------

async def adapt_scoring(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    # No LLM call in this agent - see adapt_resume_matcher's note.
    inp = case.input
    job = JobDescription.model_validate(inp["job"])
    tech = TechnicalEvaluation.model_validate(inp["technical_evaluation"])
    behav = BehavioralEvaluation.model_validate(inp["behavioral_evaluation"])
    matching = MatchingScore.model_validate(inp["matching_score"])
    candidate_id = inp.get("candidate_id", "cand_case")
    agent = ScoringAgent()

    result = await agent.execute(
        job_description=job, technical_evaluation=tech, behavioral_evaluation=behav,
        matching_score=matching, candidate_id=candidate_id,
    )
    scores = result.get("candidate_scores")
    facts: Dict[str, Any] = {}

    job_fit_check = inp.get("job_fit_invariant_check")
    if job_fit_check and scores is not None:
        alt_matching = matching.model_copy(update={"match_score": job_fit_check["alt_match_score"]})
        result2 = await agent.execute(
            job_description=job, technical_evaluation=tech, behavioral_evaluation=behav,
            matching_score=alt_matching, candidate_id=candidate_id,
        )
        scores2 = result2["candidate_scores"]
        facts["job_fit_does_not_change_weighted_final"] = scores.weighted_final_score == scores2.weighted_final_score
        facts["job_fit_score_did_change"] = scores.job_fit_score != scores2.job_fit_score

    return {"result": result, "output": scores, "facts": facts}


# ---------------------------------------------------------------------------
# Report Generator (deterministic, no LLM)
# ---------------------------------------------------------------------------

async def adapt_report_generator(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    # No LLM call in this agent - see adapt_resume_matcher's note.
    inp = case.input
    resume = ParsedResume.model_validate(inp["resume"])
    scores = CandidateScores.model_validate(inp["candidate_scores"])
    kwargs: Dict[str, Any] = {"candidate_scores": scores, "parsed_resume": resume}

    if inp.get("technical_evaluation") is not None:
        kwargs["technical_evaluation"] = TechnicalEvaluation.model_validate(inp["technical_evaluation"])
    if inp.get("behavioral_evaluation") is not None:
        kwargs["behavioral_evaluation"] = BehavioralEvaluation.model_validate(inp["behavioral_evaluation"])
    if inp.get("bias_audit") is not None:
        from schemas.evaluation import BiasAudit
        kwargs["bias_audit"] = BiasAudit.model_validate(inp["bias_audit"])
    if inp.get("integrity_evaluation") is not None:
        from schemas.evaluation import IntegrityEvaluation
        kwargs["integrity_evaluation"] = IntegrityEvaluation.model_validate(inp["integrity_evaluation"])
    if inp.get("resume_audits") is not None:
        from schemas.evaluation import ClaimVerification
        kwargs["resume_audits"] = [ClaimVerification.model_validate(v) for v in inp["resume_audits"]]

    agent = ReportGeneratorAgent()
    result = await agent.execute(**kwargs)
    return {"result": result, "output": result.get("candidate_report")}


# ---------------------------------------------------------------------------
# Leaderboard (deterministic, no LLM)
# ---------------------------------------------------------------------------

async def adapt_leaderboard(case: EvaluationCase, provider: Optional[LLMProvider] = None) -> Dict[str, Any]:
    # No LLM call in this agent - see adapt_resume_matcher's note.
    inp = case.input
    reports = [CandidateReport.model_validate(r) for r in inp.get("reports", [])]
    agent = LeaderboardAgent()
    result = await agent.execute(
        reports=reports, job_id=inp.get("job_id", "job_case"),
        incomplete_candidates=inp.get("incomplete_candidates", []),
    )
    return {"result": result, "output": result.get("leaderboard")}


ADAPTERS = {
    "jd_analyzer": adapt_jd_analyzer,
    "resume_parser": adapt_resume_parser,
    "resume_matcher": adapt_resume_matcher,
    "interviewer_evaluate": adapt_interviewer_evaluate,
    "interviewer_session_turn": adapt_interviewer_session_turn,
    "interviewer_adaptive_sequence": adapt_interviewer_adaptive_sequence,
    "technical_evaluator": adapt_technical_evaluator,
    "behavioral_evaluator": adapt_behavioral_evaluator,
    "resume_auditor": adapt_resume_auditor,
    "integrity": adapt_integrity,
    "bias_checker": adapt_bias_checker,
    "scoring": adapt_scoring,
    "report_generator": adapt_report_generator,
    "leaderboard": adapt_leaderboard,
}
