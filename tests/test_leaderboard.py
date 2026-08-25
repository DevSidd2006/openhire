"""Tests for LeaderboardAgent, focused on the all-candidates-incomplete edge
case: zero scorable candidates must never crash leaderboard creation, and
must never fabricate a ranking."""
import pytest

from agents.leaderboard.agent import LeaderboardAgent


class TestAllCandidatesIncomplete:
    @pytest.mark.asyncio
    async def test_empty_reports_does_not_crash_and_lists_incomplete(self):
        agent = LeaderboardAgent()

        result = await agent.execute(
            reports=[],
            job_id="job_test_001",
            incomplete_candidates=["cand_001", "cand_002", "cand_003"],
        )

        assert result.get("error") is None
        leaderboard = result["leaderboard"]
        assert leaderboard is not None
        assert leaderboard.entries == []
        assert leaderboard.top_candidates == []
        assert leaderboard.total_candidates == 0
        assert leaderboard.strong_candidates == 0
        assert leaderboard.candidates == 0
        assert leaderboard.incomplete_candidates == ["cand_001", "cand_002", "cand_003"]
        # Explicit human-review/incomplete status, not a silent empty result.
        assert "human review" in leaderboard.explanation.lower()
        assert "cand_001" in leaderboard.explanation

    @pytest.mark.asyncio
    async def test_empty_reports_through_run_wrapper_does_not_fail(self):
        """Through BaseAgent.run() (as the graph actually calls it): no
        exception, a real leaderboard object, not an error envelope."""
        agent = LeaderboardAgent()
        outcome = await agent.run(
            run_id="run_test",
            reports=[],
            job_id="job_test_001",
            incomplete_candidates=["cand_001"],
        )
        assert outcome["audit_log"].status == "success"
        assert outcome.get("error") is None
        assert outcome["result"]["leaderboard"].total_candidates == 0

    @pytest.mark.asyncio
    async def test_no_shortlisted_and_no_incomplete_still_safe(self):
        """Degenerate case: nothing shortlisted at all, nothing incomplete
        either - still must not crash."""
        agent = LeaderboardAgent()
        result = await agent.execute(reports=[], job_id="job_test_001", incomplete_candidates=[])
        leaderboard = result["leaderboard"]
        assert leaderboard.entries == []
        assert leaderboard.incomplete_candidates == []
        assert "no candidates were shortlisted" in leaderboard.explanation.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
