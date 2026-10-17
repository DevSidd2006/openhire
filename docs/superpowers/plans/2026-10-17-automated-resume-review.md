# Automated Resume Review Implementation Plan

> **For agentic workers:** Implement task-by-task with tests first. Do not commit or push unless the user explicitly requests it.

**Goal:** Make resume review fully automated for practice and main job applications while retaining human-review signals as non-blocking metadata.

**Architecture:** Keep the existing evidence, coverage, and review-flag fields. Change the scoring seam so partial evidence yields a normalized score, use semantic similarity only when no rubric evidence exists, and remove review flags from status/recommendation gates. Preserve pending states for genuine scoring failures and keep leaderboard/report metadata intact.

**Tech Stack:** Python 3, FastAPI, Pydantic, pytest, pytest-asyncio, existing in-memory repositories and agent orchestration.

---

## File map

- Modify `services/rubric_aggregation.py`: return a normalized score whenever at least one competency has valid evidence, including below the old coverage threshold.
- Modify `services/application_service.py`: ignore the informational matching review flag for shortlist/reject decisions and fall back to semantic score when rubric score is absent.
- Modify `orchestration/graph.py`: treat every successfully scored matching result as rankable, regardless of its review flag.
- Modify `api/routes/rubrics.py`: expose flagged-but-scored rows in the ranked list while retaining the review flag/list for informational filtering.
- Modify `agents/report_generator/agent.py`: derive recommendations only from score bands; keep report-level review flags true when evidence/integrity concerns exist.
- Modify `pages/candidate.html` and `pages/report.html` only if focused UI assertions show blocked/review-required wording remains visible; change wording to informational rather than workflow-blocking.
- Extend `tests/test_rubric_aggregation.py`, `tests/test_backend_jobs_candidates_applications.py`, `tests/test_evidence_bound_matcher.py`, `tests/test_evidence_architecture_p2.py`, and add focused service/orchestration tests where existing fixtures do not cover the behavior.

## Task 1: Make partial resume evidence scoreable

**Files:**
- Modify: `services/rubric_aggregation.py:46-90`
- Test: `tests/test_rubric_aggregation.py`

- [ ] **Step 1: Add a failing test for partial evidence with a score.**

Add a test beside the existing low-coverage tests:

```python
def test_partial_coverage_returns_normalized_score_and_review_flag():
    comps = [_competency("A", 0.25), _competency("B", 0.75)]
    result = aggregate(comps, [_verdict("A", 4)])

    assert result.coverage == 0.25
    assert result.final_score == 0.8
    assert result.band == "strong"
    assert result.needs_human_review is True
```

Use the repository's existing `_competency` and `_verdict` fixture helpers. If the current test helper uses different names, preserve its existing constructor shape and only add the assertions above.

- [ ] **Step 2: Run the focused test and verify the current behavior fails.**

Run:

```bash
pytest tests/test_rubric_aggregation.py::test_partial_coverage_returns_normalized_score_and_review_flag -q
```

Expected: FAIL because the current implementation returns `final_score=None` for coverage below `0.5`. The intended result is a score whenever `counted_weight > 0`, with the flag still true when coverage is below `COVERAGE_THRESHOLD`.

- [ ] **Step 3: Implement the smallest aggregation change.**

Change the low-coverage branch so only the zero-evidence case returns no score. The algorithm should be:

```python
coverage = counted_weight / total_weight
needs_review = coverage < COVERAGE_THRESHOLD or counted_weight == 0.0

if counted_weight == 0.0:
    return AggregateResult(
        final_score=None,
        coverage=coverage,
        band=None,
        needs_human_review=True,
        scored_competencies=scored,
        uncited_competencies=uncited,
    )

normalized = (weighted_sum / counted_weight) / MAX_ANCHOR_SCORE
return AggregateResult(
    final_score=normalized,
    coverage=coverage,
    band=_band(normalized),
    needs_human_review=needs_review,
    scored_competencies=scored,
    uncited_competencies=uncited,
)
```

Update the module/class docstrings so they say low coverage is flagged but remains automatically scoreable when evidence exists.

- [ ] **Step 4: Run the complete aggregation tests.**

Run:

```bash
pytest tests/test_rubric_aggregation.py -q
```

Expected: PASS, with the existing no-evidence test still asserting `final_score is None` and `needs_human_review is True`.

## Task 2: Remove the application-status review gate

**Files:**
- Modify: `services/application_service.py:46-66, 235-276, 365-422`
- Test: `tests/test_backend_jobs_candidates_applications.py`

- [ ] **Step 1: Add failing status-decision tests.**

Add unit tests for `evaluate_shortlist_status` covering the three automated cases:

```python
def test_review_flag_does_not_block_rubric_shortlist():
    score = _matching_score(match_score=0.80, needs_human_review=True)
    assert evaluate_shortlist_status(score) == ApplicationStatus.SHORTLISTED


def test_review_flag_does_not_block_rubric_rejection():
    score = _matching_score(match_score=0.20, needs_human_review=True)
    assert evaluate_shortlist_status(score) == ApplicationStatus.REJECTED


def test_missing_rubric_score_uses_semantic_score():
    score = _matching_score(match_score=None, needs_human_review=True)
    assert evaluate_shortlist_status(score, semantic_score=0.80) == ApplicationStatus.SHORTLISTED
```

Use the existing matching-score/application fixtures from the file; do not add a second score model.

- [ ] **Step 2: Run the focused tests and confirm failure.**

Run:

```bash
pytest tests/test_backend_jobs_candidates_applications.py -k 'review_flag or semantic_score' -q
```

Expected: FAIL because the current function returns `NEEDS_HUMAN_REVIEW` before examining `match_score`, and returns `NEEDS_HUMAN_REVIEW` when the matching score is absent.

- [ ] **Step 3: Implement the status rule.**

Replace the `matching_score.needs_human_review` branch with this ordering:

```python
if matching_score is not None and matching_score.match_score is not None:
    return (
        ApplicationStatus.SHORTLISTED
        if matching_score.match_score >= threshold
        else ApplicationStatus.REJECTED
    )

if semantic_score is not None:
    return (
        ApplicationStatus.SHORTLISTED
        if semantic_score >= threshold
        else ApplicationStatus.REJECTED
    )

return ApplicationStatus.SCORING_PENDING
```

Keep `matching_score.needs_human_review` unchanged on the persisted score. Do not convert exceptions in `_auto_score_application` or `run_matching_for_job` into rejection.

- [ ] **Step 4: Update stale service comments/docstrings.**

Remove statements claiming a review flag parks the application. State that missing score/input remains `SCORING_PENDING`, while a present score is used automatically even when flagged.

- [ ] **Step 5: Run application-flow tests.**

Run:

```bash
pytest tests/test_backend_jobs_candidates_applications.py -q
```

Expected: PASS.

## Task 3: Keep flagged candidates in matching and leaderboard results

**Files:**
- Modify: `orchestration/graph.py:229-275`
- Modify: `api/routes/rubrics.py:230-273`
- Test: `tests/test_evidence_bound_matcher.py`
- Test: `tests/test_rubric_api.py`

- [ ] **Step 1: Add a failing orchestration test.**

Build the smallest `PipelineState`/agent-result fixture used by the existing graph tests and assert that a result with both `matching_score` and `needs_human_review=True` adds the candidate to `shortlisted_candidates`/rankable candidates. The expected condition is:

```python
assert candidate_id in output["shortlisted_candidates"]
assert candidate_id in output["matching_scores"]
```

- [ ] **Step 2: Run the focused graph test and verify it fails.**

Run the graph test added in Step 1 with its exact test path and `-q`. Expected: FAIL because the current code checks `if not result.get("needs_human_review")` before adding the candidate.

- [ ] **Step 3: Implement rankability based on score presence.**

In `node_match_resumes`, replace the rankability condition with a check that the unwrapped result contains a valid `matching_score` and that its `match_score` is not `None`. Preserve the informational score flag and existing error collection for missing/failed matching results.

- [ ] **Step 4: Add a failing API test for flagged-but-scored rows.**

Extend the match leaderboard fixture in `tests/test_rubric_api.py` with one application whose score has `match_score` and `needs_human_review=True`. Assert that the response includes the row in `rows` and keeps it in `needs_review` or exposes the equivalent informational flag according to the existing response model.

- [ ] **Step 5: Implement leaderboard partitioning without exclusion.**

In `api/routes/rubrics.py`, keep the `needs_review` collection for compatibility/visibility, but also include each flagged row in the ranked `rows` list when it has a score. Sort all scored rows by `match_score`; leave truly pending rows in `scoring_pending`.

- [ ] **Step 6: Run matching and rubric API tests.**

Run:

```bash
pytest tests/test_evidence_bound_matcher.py tests/test_rubric_api.py -q
```

Expected: PASS.

## Task 4: Make practice/main reports score-based while retaining flags

**Files:**
- Modify: `agents/report_generator/agent.py:120-151`
- Test: `tests/test_evidence_architecture_p2.py`

- [ ] **Step 1: Add failing recommendation tests.**

Add report-generator tests that construct the existing `CandidateScores` fixture and attach flagged evaluations/claim verifications:

```python
assert report.recommendation == "candidate"  # score 5.5 through < 8.5
assert report.requires_human_review is True
```

Also assert that a score below `5.5` returns `"insufficient_evidence"` even when an integrity or claim flag is present.

- [ ] **Step 2: Run the focused tests and confirm failure.**

Run:

```bash
pytest tests/test_evidence_architecture_p2.py -k 'review or recommendation' -q
```

Expected: FAIL because `_determine_recommendation` currently returns `human_review` before applying score bands.

- [ ] **Step 3: Remove review flags from recommendation selection.**

Keep the `requires_human_review` expression in `execute()` so flags remain in the returned report. Change `_determine_recommendation` to only use the score:

```python
if final_score >= 8.5:
    return "strong_candidate"
if final_score >= 5.5:
    return "candidate"
return "insufficient_evidence"
```

Remove the integrity/bias/claim early returns and update the docstring to say flags are informational metadata.

- [ ] **Step 4: Run evidence/report tests.**

Run:

```bash
pytest tests/test_evidence_architecture_p2.py tests/test_evidence_chain.py -q
```

Expected: PASS after updating only assertions that explicitly encoded the old blocking recommendation.

## Task 5: Remove blocking language from the UI and verify the full path

**Files:**
- Modify: `pages/candidate.html` only if the application status renderer still shows `Under Review` as a workflow state.
- Modify: `pages/report.html` only if the report metric labels imply approval is required.
- Test: relevant frontend/API tests if present; otherwise verify via route payload tests.

- [ ] **Step 1: Search for remaining blocking wording.**

Run:

```bash
rg -n -i 'under review|human review|required|needs_human_review|human_review' pages api services orchestration agents schemas
```

Classify each occurrence as either an informational flag/filter or a blocking state. Do not remove informational fields.

- [ ] **Step 2: Update only blocking status text.**

If `candidate.html` still renders `needs_human_review` as `Under Review`, render it as an informational label such as `Scored with evidence caveat` and keep the automated shortlist/reject status visible. Keep report-level “Human Review” metrics only if they clearly say “flagged for attention” rather than “waiting for approval.”

- [ ] **Step 3: Run the full focused regression suite.**

Run:

```bash
pytest tests/test_rubric_aggregation.py tests/test_backend_jobs_candidates_applications.py tests/test_evidence_bound_matcher.py tests/test_rubric_api.py tests/test_evidence_architecture_p2.py tests/test_evidence_chain.py -q
```

Expected: PASS.

- [ ] **Step 4: Run the complete test suite.**

Run:

```bash
pytest -q
```

Expected: PASS, or a documented list of unrelated pre-existing failures. Do not commit or push as part of this task.

