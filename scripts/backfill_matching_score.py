"""
Operator script: backfill `matching_score` on an application that already
advanced past shortlisting (interviewed, or even evaluated) without one -
the gap `ApplicationService.shortlist()` now closes for future shortlists,
but does not retroactively fix (see services/application_service.py's
`shortlist` docstring). `ScoringAgent`/evaluation strictly require
`matching_score` and never fabricate it, so an application stuck without one
can never produce a report until this is backfilled.

Run manually against the target database/deployment - this is an operator
action, not an application feature, same pattern as scripts/promote_admin.py.

Usage:
    python -m scripts.backfill_matching_score <application_id> [--retry-evaluation]

Reads DATABASE_URL (and any other AppSettings env vars) from the
environment exactly like the running service does. Against an empty/unset
DATABASE_URL this reads/writes the ephemeral in-memory store, which only
matches production if this process IS production's running process (e.g.
run inside the deployed container/shell) - a separate process with an
unset DATABASE_URL backfills nothing a live server will ever see.

With --retry-evaluation, also finds the application's most recent FAILED
evaluation job (if any) for its session and retries it immediately after
the backfill, so a real report is produced in the same run.
"""
from __future__ import annotations

import asyncio
import sys

from core.config import AppSettings
from core.container import build_default_container
from services.application_service import MatchingService
from services.evaluation_service import EvaluationService


async def backfill(application_id: str, *, retry_evaluation: bool) -> None:
    settings = AppSettings.from_env()
    container = build_default_container(settings)
    try:
        application = await container.application_repository.get(application_id)
        if application is None:
            print(f"No application found for application_id={application_id!r}.", file=sys.stderr)
            sys.exit(1)

        if application.matching_score is not None:
            print(
                f"{application_id} already has a matching_score "
                f"(rubric_version={application.matching_score.rubric_version!r}); nothing to backfill."
            )
        else:
            rubric = await container.rubric_repository.get_approved_for_job(application.job_id)
            if rubric is None:
                print(
                    f"No approved rubric for job_id={application.job_id!r} - "
                    "cannot honestly compute a matching_score. Approve a rubric first.",
                    file=sys.stderr,
                )
                sys.exit(1)

            candidate = await container.candidate_repository.get(application.candidate_id)
            if candidate is None or candidate.resume is None:
                print(
                    f"No parsed resume on record for candidate_id={application.candidate_id!r} - "
                    "cannot compute a matching_score.",
                    file=sys.stderr,
                )
                sys.exit(1)

            matching_service = MatchingService(
                resume_matcher_factory=container.resume_matcher_factory_for(None),
            )
            matching_score = await matching_service.compute_match(rubric, candidate.resume)
            updated = application.model_copy(update={"matching_score": matching_score})
            application = await container.application_repository.save(updated)
            print(
                f"Backfilled matching_score for {application_id} "
                f"(match_score={matching_score.match_score}, "
                f"shortlist_recommendation={matching_score.shortlist_recommendation})."
            )

        if not retry_evaluation:
            return

        evaluation_service = EvaluationService(
            evaluation_repository=container.evaluation_repository,
            session_repository=container.session_repository,
            transcript_repository=container.transcript_repository,
            application_repository=container.application_repository,
            dispatcher=container.evaluation_dispatcher,
            agent_factories=container.evaluation_agent_factories_for(None),
        )
        if application.session_id is None:
            print("Application has no linked session_id; nothing to retry.", file=sys.stderr)
            return

        evaluation_job = await evaluation_service.get_evaluation_for_session(application.session_id)
        if evaluation_job is None:
            print(
                f"No evaluation job exists yet for session_id={application.session_id!r} - "
                "nothing to retry (one will be created once the interview seals normally).",
                file=sys.stderr,
            )
            return
        if evaluation_job.status.value != "failed":
            print(
                f"Evaluation {evaluation_job.evaluation_id} is {evaluation_job.status.value}, "
                "not failed - not retrying.",
            )
            return

        retried = await evaluation_service.retry_evaluation(evaluation_job.evaluation_id)
        print(f"Retry scheduled for evaluation {retried.evaluation_id}.")
    finally:
        await container.aclose()


def main() -> None:
    args = sys.argv[1:]
    if not args or len(args) > 2:
        print(
            "Usage: python -m scripts.backfill_matching_score <application_id> [--retry-evaluation]",
            file=sys.stderr,
        )
        sys.exit(2)
    application_id = args[0]
    retry_evaluation = "--retry-evaluation" in args[1:]
    asyncio.run(backfill(application_id, retry_evaluation=retry_evaluation))


if __name__ == "__main__":
    main()
