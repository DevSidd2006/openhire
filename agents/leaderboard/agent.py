"""
Leaderboard Agent.
Ranks multiple candidates by score.
"""
from typing import Any, Dict, List
import uuid

from agents.base import BaseAgent
from schemas.scoring import CandidateLeaderboard, LeaderboardEntry, CandidateReport


class LeaderboardAgent(BaseAgent):
    """Ranks candidates and creates leaderboard."""

    def __init__(self, **kwargs):
        super().__init__(name="leaderboard", **kwargs)

    async def execute(
        self,
        reports: List[CandidateReport],
        job_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create leaderboard from candidate reports.
        
        Args:
            reports: List of candidate reports
            job_id: Job identifier
            
        Returns:
            CandidateLeaderboard with ranked entries
        """
        self.logger.info(f"Creating leaderboard for {len(reports)} candidates for job {job_id}")

        try:
            # Sort by weighted score
            sorted_reports = sorted(
                reports,
                key=lambda r: r.scores.weighted_final_score,
                reverse=True
            )

            # Create leaderboard entries
            entries = []
            for rank, report in enumerate(sorted_reports, 1):
                # Calculate percentile
                percentile = ((len(sorted_reports) - rank) / len(sorted_reports)) * 100

                entry = LeaderboardEntry(
                    entry_id=f"entry_{uuid.uuid4().hex[:8]}",
                    rank=rank,
                    candidate_id=report.candidate_id,
                    candidate_name=report.candidate_name,
                    weighted_score=report.scores.weighted_final_score,
                    technical_score=report.scores.technical_score,
                    behavioral_score=report.scores.behavioral_score,
                    job_fit_score=report.scores.job_fit_score,
                    percentile_rank=percentile,
                    recommendation=report.recommendation,
                    has_integrity_issues=len(report.integrity_flags) > 0,
                    has_bias_concerns=len(report.bias_flags) > 0,
                    requires_human_review=report.requires_human_review,
                )
                entries.append(entry)

            # Create leaderboard
            leaderboard = CandidateLeaderboard(
                leaderboard_id=f"leaderboard_{uuid.uuid4().hex[:8]}",
                job_id=job_id,
                total_candidates=len(reports),
                strong_candidates=len([e for e in entries if e.recommendation == "strong_candidate"]),
                candidates=len([e for e in entries if e.recommendation == "candidate"]),
                requires_review=len([e for e in entries if e.requires_human_review]),
                entries=entries,
                top_candidates=[e for e in entries[:3]],
                explanation=self._generate_summary(entries),
            )

            return {"leaderboard": leaderboard}

        except Exception as e:
            self.logger.error(f"Leaderboard generation failed: {str(e)}")
            return {"leaderboard": None, "error": str(e)}

    def _generate_summary(self, entries: List[LeaderboardEntry]) -> str:
        """Generate leaderboard summary."""
        lines = [
            f"Total candidates ranked: {len(entries)}",
            f"Top candidate: {entries[0].candidate_name} (Score: {entries[0].weighted_score:.1f}/10)",
        ]

        if len(entries) > 1:
            lines.append(f"Second: {entries[1].candidate_name} (Score: {entries[1].weighted_score:.1f}/10)")
        if len(entries) > 2:
            lines.append(f"Third: {entries[2].candidate_name} (Score: {entries[2].weighted_score:.1f}/10)")

        return "\n".join(lines)
