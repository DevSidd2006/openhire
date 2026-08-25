"""
LangGraph-based orchestration for the hiring evaluation pipeline.
Defines the flow from input to final leaderboard.
"""
import asyncio
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from agents import (
    JDAnalyzerAgent,
    ResumeParserAgent,
    ResumeMatcherAgent,
    InterviewerAgent,
    TechnicalEvaluatorAgent,
    BehavioralEvaluatorAgent,
    ResumeAuditorAgent,
    IntegrityAgent,
    BiasCheckerAgent,
    ScoringAgent,
    ReportGeneratorAgent,
    LeaderboardAgent,
)


class PipelineState(TypedDict):
    """Pipeline state schema. Every field a node reads or writes must be declared here
    so LangGraph can build a state channel for it."""

    # Inputs
    job_description_text: Optional[str]
    candidates_resume_texts: List[str]
    interview_transcripts: Dict[str, Any]  # candidate_id -> InterviewTranscript

    # Intermediate results
    job_description: Optional[Any]
    parsed_resumes: Dict[str, Any]  # candidate_id -> ParsedResume
    matching_scores: Dict[str, Any]  # candidate_id -> MatchingScore
    shortlisted_candidates: List[str]
    interview_questions: Dict[str, Any]  # candidate_id -> list of questions

    # Evaluation results
    technical_evaluations: Dict[str, Any]
    behavioral_evaluations: Dict[str, Any]
    resume_audits: Dict[str, Any]
    integrity_evaluations: Dict[str, Any]
    bias_audits: Dict[str, Any]
    candidate_scores: Dict[str, Any]
    candidate_reports: Dict[str, Any]
    leaderboard: Optional[Any]

    # Execution tracking
    run_id: Optional[str]
    errors: List[str]
    audit_logs: List[Any]


def _unwrap(agent_result: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap the {"result": ..., "audit_log": ..., "error": ...} envelope that
    BaseAgent.run() returns, giving back the agent's actual execute() output."""
    return agent_result.get("result") or {}


def _build_evaluation_sections(
    technical_evaluation: Any,
    behavioral_evaluation: Any,
    resume_audit: Any,
    integrity_evaluation: Any,
) -> Dict[str, str]:
    """Build the evaluator-rationale text the BiasCheckerAgent audits (P0-5).

    The bias checker's job is to audit the SYSTEM'S evaluation, not the raw
    candidate transcript, so this pulls together each evaluator's actual
    explanation/strengths/weaknesses/verdicts - never the candidate's own
    words - into named sections BiasCheckerAgent can both show the LLM and
    cite back as evidence_source on a flag.
    """
    sections: Dict[str, str] = {}

    if technical_evaluation:
        parts = [technical_evaluation.explanation]
        if technical_evaluation.strengths:
            parts.append("Strengths: " + "; ".join(technical_evaluation.strengths))
        if technical_evaluation.weaknesses:
            parts.append("Weaknesses: " + "; ".join(technical_evaluation.weaknesses))
        sections["technical_evaluation"] = "\n".join(parts)

    if behavioral_evaluation:
        parts = [behavioral_evaluation.explanation]
        if behavioral_evaluation.strengths:
            parts.append("Strengths: " + "; ".join(behavioral_evaluation.strengths))
        if behavioral_evaluation.weaknesses:
            parts.append("Weaknesses: " + "; ".join(behavioral_evaluation.weaknesses))
        sections["behavioral_evaluation"] = "\n".join(parts)

    if resume_audit:
        sections["resume_audit"] = "\n".join(
            f"- Claim \"{v.claim.resume_claim}\" -> {v.verification_status}: {v.explanation}"
            for v in resume_audit
        )

    if integrity_evaluation:
        parts = [f"Overall integrity: {integrity_evaluation.overall_integrity}. {integrity_evaluation.explanation}"]
        parts.extend(f"- Flag ({flag.severity}): {flag.description}" for flag in integrity_evaluation.flags)
        sections["integrity"] = "\n".join(parts)

    return sections


def _audit_logs(agent_results) -> List[Any]:
    """Collect the AuditLog objects BaseAgent.run() attaches to every call, so
    the audit trail survives into the final pipeline state/output rather than
    being discarded when the result envelope is unwrapped."""
    if isinstance(agent_results, dict):
        agent_results = [agent_results]
    return [r["audit_log"] for r in agent_results if r.get("audit_log") is not None]


def create_pipeline_graph():
    """Create the LangGraph workflow graph."""

    graph = StateGraph(PipelineState)

    # Initialize agents
    jd_analyzer = JDAnalyzerAgent()
    resume_parser = ResumeParserAgent()
    resume_matcher = ResumeMatcherAgent()
    interviewer = InterviewerAgent()
    technical_evaluator = TechnicalEvaluatorAgent()
    behavioral_evaluator = BehavioralEvaluatorAgent()
    resume_auditor = ResumeAuditorAgent()
    integrity_agent = IntegrityAgent()
    bias_checker = BiasCheckerAgent()
    scoring_agent = ScoringAgent()
    report_generator = ReportGeneratorAgent()
    leaderboard_agent = LeaderboardAgent()

    # ==== Node Definitions ====
    # Every node returns a partial state update (a dict of only the keys it
    # changed) rather than mutating and returning the whole state, so LangGraph's
    # channel-merge semantics apply correctly.

    async def node_analyze_job(state: PipelineState) -> Dict[str, Any]:
        """Analyze job description.

        A failed/invalid JD analysis (bad structured LLM output, or
        JobDescription construction/validation failure) must never look like
        a successful one - JDAnalyzerAgent.execute() returns
        job_description=None on failure rather than fabricating a plausible
        placeholder, so this check is a real "did it actually succeed?" test,
        not just a null-check formality. Leaving state["job_description"]
        unset here means every downstream node (matching, question
        generation, evaluation, scoring, leaderboard) already treats it as
        "nothing to do" with an explicit error, degrading the whole run to
        zero processed candidates rather than silently evaluating everyone
        against an invented job description.
        """
        agent_result = await jd_analyzer.run(
            run_id=state.get("run_id"),
            job_description=state["job_description_text"],
            job_id="job_001",
        )
        result = _unwrap(agent_result)
        audit_logs = state["audit_logs"] + _audit_logs(agent_result)
        if result.get("job_description"):
            return {"job_description": result["job_description"], "audit_logs": audit_logs}
        # The failure reason is normally inside the unwrapped result (see
        # JDAnalyzerAgent.execute's except branch); agent_result.get("error")
        # is only populated in the rarer case where execute() itself raised
        # past its own try/except and BaseAgent.run() caught it.
        error_reason = result.get("error") or agent_result.get("error")
        return {
            "errors": state["errors"] + [f"JD Analysis failed: {error_reason}"],
            "audit_logs": audit_logs,
        }

    async def node_parse_resumes(state: PipelineState) -> Dict[str, Any]:
        """Parse all candidate resumes in parallel."""
        parse_tasks = [
            resume_parser.run(
                run_id=state.get("run_id"),
                resume_text=resume_text,
                candidate_id=f"cand_{i + 1:03d}",
                candidate_name=f"Candidate {i + 1}",
            )
            for i, resume_text in enumerate(state["candidates_resume_texts"])
        ]

        agent_results = await asyncio.gather(*parse_tasks)

        parsed_resumes = {}
        errors = []
        for i, agent_result in enumerate(agent_results):
            candidate_id = f"cand_{i + 1:03d}"
            result = _unwrap(agent_result)
            if result.get("parsed_resume"):
                parsed_resumes[candidate_id] = result["parsed_resume"]
            else:
                errors.append(f"Resume parsing failed for {candidate_id}")

        return {
            "parsed_resumes": {**state["parsed_resumes"], **parsed_resumes},
            "errors": state["errors"] + errors,
            "audit_logs": state["audit_logs"] + _audit_logs(agent_results),
        }

    async def node_match_resumes(state: PipelineState) -> Dict[str, Any]:
        """Match resumes to job."""
        if not state["job_description"]:
            return {"errors": state["errors"] + ["No job description; skipping resume matching"]}

        candidate_ids = list(state["parsed_resumes"].keys())
        match_tasks = [
            resume_matcher.run(
                run_id=state.get("run_id"),
                job_description=state["job_description"],
                parsed_resume=state["parsed_resumes"][candidate_id],
            )
            for candidate_id in candidate_ids
        ]

        agent_results = await asyncio.gather(*match_tasks)

        matching_scores = {}
        shortlisted = []
        errors = []
        for candidate_id, agent_result in zip(candidate_ids, agent_results):
            result = _unwrap(agent_result)
            if result.get("matching_score"):
                matching_scores[candidate_id] = result["matching_score"]
                if result.get("shortlist_recommendation"):
                    shortlisted.append(candidate_id)
            else:
                errors.append(f"Matching failed for {candidate_id}")

        return {
            "matching_scores": {**state["matching_scores"], **matching_scores},
            "shortlisted_candidates": state["shortlisted_candidates"] + shortlisted,
            "errors": state["errors"] + errors,
            "audit_logs": state["audit_logs"] + _audit_logs(agent_results),
        }

    async def node_generate_interview_questions(state: PipelineState) -> Dict[str, Any]:
        """Pre-interview question PLANNING for shortlisted candidates - not a
        live adaptive interview. See agents/interviewer/agent.py's module
        docstring: this batch-generates a candidate-specific question set
        before any interview happens, and its output is informational only -
        technical/behavioral evaluation and resume/integrity checks run
        against the separately-supplied `interview_transcript`, not this
        node's `interview_questions` output."""
        if not state["job_description"]:
            return {"errors": state["errors"] + ["No job description; skipping interview questions"]}

        candidate_ids = [
            c for c in state["shortlisted_candidates"] if c in state["parsed_resumes"]
        ]
        question_tasks = [
            interviewer.run(
                run_id=state.get("run_id"),
                job_description=state["job_description"],
                parsed_resume=state["parsed_resumes"][candidate_id],
                question_count=8,
            )
            for candidate_id in candidate_ids
        ]

        agent_results = await asyncio.gather(*question_tasks)

        interview_questions = {}
        errors = []
        for candidate_id, agent_result in zip(candidate_ids, agent_results):
            result = _unwrap(agent_result)
            if result.get("questions"):
                interview_questions[candidate_id] = result["questions"]
            else:
                errors.append(f"Question generation failed for {candidate_id}")

        return {
            "interview_questions": {**state["interview_questions"], **interview_questions},
            "errors": state["errors"] + errors,
            "audit_logs": state["audit_logs"] + _audit_logs(agent_results),
        }

    async def node_run_parallel_evaluations(state: PipelineState) -> Dict[str, Any]:
        """Run technical, behavioral, resume audit, and integrity evaluations for every
        eligible candidate concurrently (all candidates x all 4 eval types in one gather)."""

        eligible_candidates = [
            candidate_id
            for candidate_id in state["shortlisted_candidates"]
            if candidate_id in state["parsed_resumes"] and candidate_id in state["interview_transcripts"]
        ]

        # Flat list of (candidate_id, eval_type, coroutine) so every evaluation for
        # every candidate is scheduled onto one asyncio.gather - true concurrency
        # across candidates, not just within one candidate's four eval types.
        job_desc = state["job_description"]
        task_specs = []
        for candidate_id in eligible_candidates:
            resume = state["parsed_resumes"][candidate_id]
            transcript = state["interview_transcripts"][candidate_id]

            task_specs.append((candidate_id, "technical", technical_evaluator.run(
                run_id=state.get("run_id"),
                job_description=job_desc,
                parsed_resume=resume,
                interview_transcript=transcript,
            )))
            task_specs.append((candidate_id, "behavioral", behavioral_evaluator.run(
                run_id=state.get("run_id"),
                job_description=job_desc,
                parsed_resume=resume,
                interview_transcript=transcript,
            )))
            task_specs.append((candidate_id, "resume_audit", resume_auditor.run(
                run_id=state.get("run_id"),
                parsed_resume=resume,
                interview_transcript=transcript,
            )))
            task_specs.append((candidate_id, "integrity", integrity_agent.run(
                run_id=state.get("run_id"),
                parsed_resume=resume,
                interview_transcript=transcript,
            )))

        agent_results = await asyncio.gather(*[spec[2] for spec in task_specs])

        technical_evaluations = {}
        behavioral_evaluations = {}
        resume_audits = {}
        integrity_evaluations = {}
        errors = []

        for (candidate_id, eval_type, _), agent_result in zip(task_specs, agent_results):
            result = _unwrap(agent_result)
            if eval_type == "technical" and result.get("technical_evaluation"):
                technical_evaluations[candidate_id] = result["technical_evaluation"]
            elif eval_type == "behavioral" and result.get("behavioral_evaluation"):
                behavioral_evaluations[candidate_id] = result["behavioral_evaluation"]
            elif eval_type == "resume_audit" and result.get("claim_verifications") is not None:
                resume_audits[candidate_id] = result["claim_verifications"]
            elif eval_type == "integrity" and result.get("integrity_evaluation"):
                integrity_evaluations[candidate_id] = result["integrity_evaluation"]
            else:
                errors.append(f"{eval_type} evaluation failed for {candidate_id}: {agent_result.get('error')}")

        return {
            "technical_evaluations": {**state["technical_evaluations"], **technical_evaluations},
            "behavioral_evaluations": {**state["behavioral_evaluations"], **behavioral_evaluations},
            "resume_audits": {**state["resume_audits"], **resume_audits},
            "integrity_evaluations": {**state["integrity_evaluations"], **integrity_evaluations},
            "errors": state["errors"] + errors,
            "audit_logs": state["audit_logs"] + _audit_logs(agent_results),
        }

    async def node_run_bias_check(state: PipelineState) -> Dict[str, Any]:
        """Check for bias in evaluations, for every candidate concurrently."""
        job_id = state["job_description"].job_id if state["job_description"] else "unknown"

        candidate_ids = [
            c for c in state["shortlisted_candidates"] if c in state["interview_transcripts"]
        ]
        bias_tasks = []
        for candidate_id in candidate_ids:
            transcript = state["interview_transcripts"][candidate_id]
            tech_eval = state["technical_evaluations"].get(candidate_id)
            behav_eval = state["behavioral_evaluations"].get(candidate_id)
            resume_audit = state["resume_audits"].get(candidate_id)
            integrity_eval = state["integrity_evaluations"].get(candidate_id)

            bias_tasks.append(bias_checker.run(
                run_id=state.get("run_id"),
                candidate_id=candidate_id,
                job_id=job_id,
                interview_transcript=transcript,
                # A missing evaluation is passed through as None rather than
                # disguised as an average 7.0 score (P0-4).
                technical_score=tech_eval.technical_score if tech_eval else None,
                behavioral_score=behav_eval.behavioral_score if behav_eval else None,
                evaluation_sections=_build_evaluation_sections(
                    tech_eval, behav_eval, resume_audit, integrity_eval
                ),
            ))

        agent_results = await asyncio.gather(*bias_tasks)

        bias_audits = {}
        errors = []
        for candidate_id, agent_result in zip(candidate_ids, agent_results):
            result = _unwrap(agent_result)
            if result.get("bias_audit"):
                bias_audits[candidate_id] = result["bias_audit"]
            else:
                errors.append(f"Bias check failed for {candidate_id}")

        return {
            "bias_audits": {**state["bias_audits"], **bias_audits},
            "errors": state["errors"] + errors,
            "audit_logs": state["audit_logs"] + _audit_logs(agent_results),
        }

    async def node_score_candidates(state: PipelineState) -> Dict[str, Any]:
        """Calculate final scores for all candidates.

        A shortlisted candidate missing a technical, behavioral, or matching
        evaluation is excluded from scoring entirely with an explicit reason
        recorded in `errors` (P0-4) - never scored as if evaluation had
        succeeded. node_create_leaderboard surfaces these same exclusions via
        CandidateLeaderboard.incomplete_candidates.
        """
        candidate_ids = []
        exclusion_errors = []
        for c in state["shortlisted_candidates"]:
            missing = [
                name
                for name, bucket in (
                    ("technical_evaluation", state["technical_evaluations"]),
                    ("behavioral_evaluation", state["behavioral_evaluations"]),
                    ("matching_score", state["matching_scores"]),
                )
                if c not in bucket
            ]
            if missing:
                exclusion_errors.append(
                    f"Candidate {c} excluded from scoring: missing {', '.join(missing)}"
                )
            else:
                candidate_ids.append(c)

        scoring_tasks = [
            scoring_agent.run(
                run_id=state.get("run_id"),
                job_description=state["job_description"],
                technical_evaluation=state["technical_evaluations"][candidate_id],
                behavioral_evaluation=state["behavioral_evaluations"][candidate_id],
                matching_score=state["matching_scores"][candidate_id],
                candidate_id=candidate_id,
            )
            for candidate_id in candidate_ids
        ]

        agent_results = await asyncio.gather(*scoring_tasks)

        candidate_scores = {}
        errors = []
        for candidate_id, agent_result in zip(candidate_ids, agent_results):
            result = _unwrap(agent_result)
            if result.get("candidate_scores"):
                candidate_scores[candidate_id] = result["candidate_scores"]
            else:
                errors.append(f"Scoring failed for {candidate_id}")

        return {
            "candidate_scores": {**state["candidate_scores"], **candidate_scores},
            "errors": state["errors"] + exclusion_errors + errors,
            "audit_logs": state["audit_logs"] + _audit_logs(agent_results),
        }

    async def node_generate_reports(state: PipelineState) -> Dict[str, Any]:
        """Generate comprehensive reports for all candidates."""
        candidate_ids = [c for c in state["shortlisted_candidates"] if c in state["candidate_scores"]]

        report_tasks = [
            report_generator.run(
                run_id=state.get("run_id"),
                candidate_scores=state["candidate_scores"][candidate_id],
                parsed_resume=state["parsed_resumes"][candidate_id],
                technical_evaluation=state["technical_evaluations"].get(candidate_id),
                behavioral_evaluation=state["behavioral_evaluations"].get(candidate_id),
                bias_audit=state["bias_audits"].get(candidate_id),
                integrity_evaluation=state["integrity_evaluations"].get(candidate_id),
                # P2 Phase 10: resume-audit evidence previously never reached
                # the report at all - now threaded through explicitly.
                resume_audits=state["resume_audits"].get(candidate_id),
            )
            for candidate_id in candidate_ids
        ]

        agent_results = await asyncio.gather(*report_tasks)

        candidate_reports = {}
        errors = []
        for candidate_id, agent_result in zip(candidate_ids, agent_results):
            result = _unwrap(agent_result)
            if result.get("candidate_report"):
                candidate_reports[candidate_id] = result["candidate_report"]
            else:
                errors.append(f"Report generation failed for {candidate_id}")

        return {
            "candidate_reports": {**state["candidate_reports"], **candidate_reports},
            "errors": state["errors"] + errors,
            "audit_logs": state["audit_logs"] + _audit_logs(agent_results),
        }

    async def node_create_leaderboard(state: PipelineState) -> Dict[str, Any]:
        """Create final leaderboard.

        Always produces a real CandidateLeaderboard when at least one
        candidate was shortlisted - even if EVERY shortlisted candidate ended
        up incomplete (e.g. every technical evaluation failed). That case is
        represented explicitly (entries=[], everyone in
        incomplete_candidates, explanation states human review is required)
        rather than short-circuited into a bare error with no leaderboard
        object at all. Only a genuinely empty shortlist (nothing to rank or
        report on, in any state) skips leaderboard creation.
        """
        reports = list(state["candidate_reports"].values())

        if not state["shortlisted_candidates"]:
            return {"errors": state["errors"] + ["No shortlisted candidates; nothing to rank"]}

        # Candidates that were shortlisted but never made it to a report
        # (missing evaluation, failed report generation, etc.) are surfaced
        # explicitly on the leaderboard rather than silently disappearing (P0-4).
        incomplete_candidates = [
            c for c in state["shortlisted_candidates"] if c not in state["candidate_reports"]
        ]

        agent_result = await leaderboard_agent.run(
            run_id=state.get("run_id"),
            reports=reports,
            job_id=state["job_description"].job_id if state["job_description"] else "unknown",
            incomplete_candidates=incomplete_candidates,
        )
        result = _unwrap(agent_result)
        audit_logs = state["audit_logs"] + _audit_logs(agent_result)

        errors = state["errors"]
        if not reports:
            errors = errors + [
                f"No candidate could be scored for this run - all {len(incomplete_candidates)} "
                "shortlisted candidate(s) are incomplete and require human review."
            ]

        if result.get("leaderboard"):
            return {"leaderboard": result["leaderboard"], "audit_logs": audit_logs, "errors": errors}
        return {"errors": errors + ["Leaderboard creation failed"], "audit_logs": audit_logs}

    # ==== Add nodes to graph ====
    graph.add_node("analyze_job", node_analyze_job)
    graph.add_node("parse_resumes", node_parse_resumes)
    graph.add_node("match_resumes", node_match_resumes)
    graph.add_node("generate_questions", node_generate_interview_questions)
    graph.add_node("parallel_evaluations", node_run_parallel_evaluations)
    graph.add_node("bias_check", node_run_bias_check)
    graph.add_node("score", node_score_candidates)
    graph.add_node("reports", node_generate_reports)
    graph.add_node("leaderboard", node_create_leaderboard)

    # ==== Define edges ====
    graph.add_edge(START, "analyze_job")
    graph.add_edge("analyze_job", "parse_resumes")
    graph.add_edge("parse_resumes", "match_resumes")
    graph.add_edge("match_resumes", "generate_questions")
    graph.add_edge("generate_questions", "parallel_evaluations")
    graph.add_edge("parallel_evaluations", "bias_check")
    graph.add_edge("bias_check", "score")
    graph.add_edge("score", "reports")
    graph.add_edge("reports", "leaderboard")
    graph.add_edge("leaderboard", END)

    return graph.compile()


# Singleton pipeline
_pipeline = None


def get_pipeline():
    """Get or create the compiled pipeline."""
    global _pipeline
    if _pipeline is None:
        _pipeline = create_pipeline_graph()
    return _pipeline
