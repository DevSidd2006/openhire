"""
Report Generator Agent.
Creates comprehensive candidate evaluation reports.
"""
from typing import Any, Dict
import json
import uuid

from agents.base import BaseAgent
from schemas.scoring import CandidateReport, CandidateScores
from schemas.evaluation import (
    TechnicalEvaluation,
    BehavioralEvaluation,
    BiasAudit,
    IntegrityEvaluation,
)
from schemas.resume import ParsedResume


class ReportGeneratorAgent(BaseAgent):
    """Generates comprehensive evaluation reports."""

    def __init__(self, **kwargs):
        super().__init__(name="report_generator", **kwargs)

    async def execute(
        self,
        candidate_scores: CandidateScores,
        parsed_resume: ParsedResume,
        technical_evaluation: TechnicalEvaluation = None,
        behavioral_evaluation: BehavioralEvaluation = None,
        bias_audit: BiasAudit = None,
        integrity_evaluation: IntegrityEvaluation = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate comprehensive report.
        
        Args:
            candidate_scores: Synthesized scores
            parsed_resume: Candidate resume
            technical_evaluation: Technical eval (optional)
            behavioral_evaluation: Behavioral eval (optional)
            bias_audit: Bias check (optional)
            integrity_evaluation: Integrity check (optional)
            
        Returns:
            CandidateReport with all details
        """
        self.logger.info(f"Generating report for candidate {parsed_resume.candidate_id}")

        try:
            # Determine recommendation
            recommendation = self._determine_recommendation(
                candidate_scores,
                bias_audit,
                integrity_evaluation,
            )

            # Build report
            report = CandidateReport(
                report_id=f"report_{uuid.uuid4().hex[:8]}",
                candidate_id=parsed_resume.candidate_id,
                job_id=candidate_scores.job_id,
                candidate_name=parsed_resume.candidate_name,
                scores=candidate_scores,
                technical_summary=technical_evaluation.explanation if technical_evaluation else "No technical evaluation",
                behavioral_summary=behavioral_evaluation.explanation if behavioral_evaluation else "No behavioral evaluation",
                job_fit_summary=f"Match score: {candidate_scores.job_fit_score:.1f}/10",
                recommendation=recommendation,
                strengths=self._compile_strengths(technical_evaluation, behavioral_evaluation),
                weaknesses=self._compile_weaknesses(technical_evaluation, behavioral_evaluation),
                integrity_flags=integrity_evaluation.flags if integrity_evaluation else [],
                bias_flags=bias_audit.flags if bias_audit else [],
                requires_human_review=(
                    (integrity_evaluation and integrity_evaluation.requires_human_review) or
                    (bias_audit and bias_audit.requires_human_review) or
                    recommendation == "human_review"
                ),
                explanation=self._generate_summary(candidate_scores, recommendation),
            )

            return {"candidate_report": report}

        except Exception as e:
            self.logger.error(f"Report generation failed: {str(e)}")
            return {"candidate_report": None, "error": str(e)}

    def _determine_recommendation(
        self,
        scores: CandidateScores,
        bias_audit: BiasAudit = None,
        integrity_eval: IntegrityEvaluation = None,
    ) -> str:
        """Determine hiring recommendation."""
        final_score = scores.weighted_final_score

        # Check for disqualifying issues
        if integrity_eval and integrity_eval.requires_human_review and integrity_eval.overall_integrity != "clear":
            return "human_review"

        if bias_audit and bias_audit.requires_human_review:
            return "human_review"

        # Score-based recommendation
        if final_score >= 8.5:
            return "strong_candidate"
        elif final_score >= 7.0:
            return "candidate"
        elif final_score >= 5.5:
            return "human_review"
        else:
            return "insufficient_evidence"

    def _compile_strengths(
        self,
        tech_eval: TechnicalEvaluation = None,
        behav_eval: BehavioralEvaluation = None,
    ) -> list:
        """Compile strengths from evaluations."""
        strengths = []
        if tech_eval:
            strengths.extend(tech_eval.strengths[:2])
        if behav_eval:
            strengths.extend(behav_eval.strengths[:2])
        return strengths

    def _compile_weaknesses(
        self,
        tech_eval: TechnicalEvaluation = None,
        behav_eval: BehavioralEvaluation = None,
    ) -> list:
        """Compile weaknesses from evaluations."""
        weaknesses = []
        if tech_eval:
            weaknesses.extend(tech_eval.weaknesses[:2])
        if behav_eval:
            weaknesses.extend(behav_eval.weaknesses[:2])
        return weaknesses

    def _generate_summary(self, scores: CandidateScores, recommendation: str) -> str:
        """Generate executive summary."""
        return (
            f"Recommendation: {recommendation}\n"
            f"Overall Score: {scores.weighted_final_score:.1f}/10\n"
            f"Technical: {scores.technical_score:.1f}/10 | "
            f"Behavioral: {scores.behavioral_score:.1f}/10 | "
            f"Job Fit: {scores.job_fit_score:.1f}/10\n"
            f"See detailed sections below for evidence."
        )
