# Automated Resume Review Design

## Goal

Remove human review as a blocking decision in both practice interviews and main job applications. Resume evidence quality, integrity findings, and other review signals remain visible as informational metadata, but they must not park a candidate or change an automated recommendation to `human_review`.

## Scope

In scope:

- Resume-to-job matching and application status decisions.
- Matching orchestration and recruiter leaderboard population.
- Practice/main post-interview report recommendations.
- Tests and user-facing status text that imply a candidate is waiting for a human review.
- Candidate UI separation between private mock interviews and real recruiter-posted job listings.
- Candidate-facing role labels use the job title; job IDs remain internal routing identifiers.

Out of scope:

- Removing evidence citations, integrity flags, bias flags, claim verifications, or confidence/coverage fields.
- Treating infrastructure failures as candidate decisions. Failed scoring remains `scoring_pending` and retryable.
- Removing recruiter controls such as explicit shortlist/reject overrides.
- Changing internal API identifiers or URL/query parameters used to route to a job.

## Decision rules

### Resume matching

The matcher continues to calculate coverage, competency verdicts, and the informational `needs_human_review` flag. The flag means “quality/evidence caveat,” not “workflow stop.”

When cited competency evidence exists, the matcher produces an automated normalized score from the available evidence even if coverage is below the former review threshold. The score is normalized over the evidence-backed competency weight, and coverage remains attached so consumers can distinguish a complete score from a sparse one.

When no competency evidence is available, the application decision falls back to the rubric-independent semantic resume/JD score when present. If neither score exists, the application remains `scoring_pending` because no automated scoring result exists; this represents missing input or a scoring failure, not a human-review decision.

### Main job applications

`needs_human_review` must not cause `NEEDS_HUMAN_REVIEW` status. Applications with an automated rubric score are automatically `SHORTLISTED` or `REJECTED` using the existing threshold. Applications with only a semantic score use the existing semantic threshold. The informational review flag remains on the matching score and is exposed to clients.

The orchestration graph and job leaderboard include every successfully scored candidate, including candidates whose matching score carries the informational review flag. Leaderboard review counts/filters may continue to expose that flag, but candidates must not be excluded from ranking solely because of it.

### Practice and post-interview reports

Report recommendations are determined by score bands, not by `requires_human_review` on integrity evaluations, bias audits, or resume claim verifications. Those flags and their evidence remain in the report for transparency. Keep the existing strong-candidate threshold (`>= 8.5`) and insufficient-evidence threshold (`< 5.5`); map the entire middle range (`5.5` through `< 8.5`) to `candidate`. The literal recommendation `human_review` is no longer emitted solely because a review flag is present.

The report UI may continue displaying that informational flags exist, but must not present them as a required workflow step or a blocked practice result.

### Candidate UI separation

The candidate dashboard links to separate Mock Interviews and Real Job Listings pages. Mock interviews use the candidate-scoped practice endpoint and are never included in the real listing endpoint; real listings continue to use the backend's non-practice job filter. Visible role labels, confirmations, headers, and dashboard cards show the job title rather than the internal job ID.

## Data flow

```text
resume + approved rubric
        |
        v
validated competency evidence -----> informational review flag + coverage
        |                                      |
        +--> automated rubric score ----------+
        |                                      |
        +--> no evidence: semantic score -----+
                                               v
                                  shortlist/reject or pending on failure
                                               |
                                               v
                                  ranked main/practice result
```

## Error handling

- LLM, embedding, repository, and missing-input failures remain retryable/pending and are not converted to rejection.
- A candidate with partial evidence receives an automated result plus a lower coverage signal.
- A candidate with no usable score remains pending only when the system genuinely has no score to use; this is not a human-review state.
- Existing audit logging should record the automated decision and preserve review-flag metadata.

## Verification

Add or update focused tests to prove:

1. A matching score with `needs_human_review=True` still becomes shortlisted/rejected from `match_score`.
2. A low-coverage result with cited evidence receives a normalized automated score and remains flagged.
3. A no-evidence result falls back to semantic score when available.
4. The orchestration graph includes flagged-but-scored candidates in its rankable population.
5. Practice/main reports retain flags but do not emit `human_review` solely because flags are present.
6. Genuine scoring failures still result in `scoring_pending` and are reported as errors.

