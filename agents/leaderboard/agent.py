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
        incomplete_candidates: List[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create leaderboard from candidate reports.

        Args:
            reports: List of candidate reports
            job_id: Job identifier
            incomplete_candidates: Candidate IDs that were shortlisted but
                never produced a scored report (missing evaluation, failed
                generation, etc.) - recorded on the leaderboard rather than
                silently omitted (P0-4).

        Returns:
            CandidateLeaderboard with ranked entries
        """
        incomplete_candidates = incomplete_candidates or []
        openings = int(kwargs.get("openings") or 1)
        if openings < 1:
            openings = 1

        self.logger.info(
            f"Creating leaderboard for {len(reports)} candidates for job {job_id} "
            f"(openings: {openings}, {len(incomplete_candidates)} incomplete)"
        )

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
                percentile = ((len(sorted_reports) - rank) / len(sorted_reports)) * 100 if sorted_reports else 0.0

                is_selected = rank <= openings
                selection_status = "selected" if is_selected else "waitlisted"

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
                    is_selected=is_selected,
                    selection_status=selection_status,
                )
                entries.append(entry)

            selected_candidates = [e for e in entries if e.is_selected]
            selected_candidate_ids = [e.candidate_id for e in selected_candidates]

            # Create leaderboard
            leaderboard = CandidateLeaderboard(
                leaderboard_id=f"leaderboard_{uuid.uuid4().hex[:8]}",
                job_id=job_id,
                openings=openings,
                total_candidates=len(reports),
                strong_candidates=len([e for e in entries if e.recommendation == "strong_candidate"]),
                candidates=len([e for e in entries if e.recommendation == "candidate"]),
                requires_review=len([e for e in entries if e.requires_human_review]),
                entries=entries,
                top_candidates=[e for e in entries[:3]],
                selected_candidates=selected_candidates,
                selected_candidate_ids=selected_candidate_ids,
                incomplete_candidates=incomplete_candidates,
                explanation=self._generate_summary(entries, incomplete_candidates, openings),
            )

            return {"leaderboard": leaderboard}

        except Exception as e:
            self.logger.error(f"Leaderboard generation failed: {str(e)}")
            return {"leaderboard": None, "error": str(e)}

    def _generate_summary(
        self, entries: List[LeaderboardEntry], incomplete_candidates: List[str] = None, openings: int = 1
    ) -> str:
        """Generate leaderboard summary."""
        incomplete_candidates = incomplete_candidates or []

        if not entries:
            # No fabricated ranking when nobody could be scored - state the
            # incomplete/human-review status explicitly instead.
            if incomplete_candidates:
                return (
                    f"No candidates could be ranked - all {len(incomplete_candidates)} shortlisted "
                    f"candidate(s) are incomplete (missing evaluation) and require human review: "
                    f"{', '.join(incomplete_candidates)}."
                )
            return "No candidates were shortlisted for this job."

        selected = [e for e in entries if e.is_selected]
        lines = [
            f"Target Openings: {openings} | Selected Candidates: {len(selected)}",
            f"Total candidates ranked: {len(entries)}",
            f"Top candidate: {entries[0].candidate_name} (Score: {entries[0].weighted_score:.1f}/10)",
        ]

        if len(entries) > 1:
            lines.append(f"Second: {entries[1].candidate_name} (Score: {entries[1].weighted_score:.1f}/10)")
        if len(entries) > 2:
            lines.append(f"Third: {entries[2].candidate_name} (Score: {entries[2].weighted_score:.1f}/10)")

        if incomplete_candidates:
            lines.append(
                f"Excluded (incomplete evaluation, not ranked): {', '.join(incomplete_candidates)}"
            )

        return "\n".join(lines)
