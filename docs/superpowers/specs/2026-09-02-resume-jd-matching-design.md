# Evidence-Bound Resume-to-JD Matching

**Date:** 2026-09-02
**Status:** Approved design, pending implementation

## Problem

The current matcher (`agents/resume_matcher/agent.py`) scores a candidate with
fixed weights — 50% skill match, 20% job-description similarity, 30% experience
— and recommends shortlisting above a 0.6 threshold. This has four defects that
together make rankings unreliable and indefensible:

1. **Cosine similarity cannot distinguish a claim from evidence.** A resume that
   mentions Kubernetes scores the same whether the candidate ran production
   clusters or attended a webinar. Keyword stuffing therefore beats the system.
2. **Skills are binary.** "Python" on a skills line scores identically to five
   years of shipping Python.
3. **No score is cited.** Nothing traces back to a span of resume text, so a
   ranking cannot be audited by a recruiter or contested by a candidate.
4. **Weights are hardcoded and job-agnostic.** `ScoringAgent` already performs
   rubric-weighted scoring for interviews; the matcher ignores that machinery
   and invents its own fixed priorities.

Two further defects are live bugs rather than design gaps:

- `services/semantic_matching.py` returns `0.0` on embedding-provider failure
  (lines 30, 71, 83, 102, 108, 115). An outage silently becomes a wave of
  near-zero match scores, and therefore a wave of rejections.
- `orchestration/graph.py` wires `match_resumes` directly to
  `generate_questions` (line 555), so a match score automatically triggers
  interview generation with no human in between.

## Goals

- **High recall and high precision simultaneously.** Do not miss viable
  candidates; do not waste recruiter time on unqualified ones.
- **No loopholes in the rubric.** Every dimension of fit is scored through one
  mechanism with one evidence standard, so nothing falls through a gap between
  subsystems.
- **Real-time scoring** on application submit, feeding a persistent per-job
  leaderboard.
- **Every rank auditable** down to the quoted resume text that produced it.

## Non-Goals

- Fine-tuning a domain cross-encoder on hire/no-hire outcomes. This has the
  highest accuracy ceiling but needs thousands of labeled outcome pairs the
  project does not yet have, and it produces no citations — making it the worst
  option for auditability. Revisit once outcome data accumulates.
- Automating the advance-to-interview or reject decisions. See "Decision
  boundary" below.

## Key Design Constraint

**A leaderboard is comparative, but real-time scoring is solitary.** Each
candidate is scored alone at application time, without visibility into the rest
of the field. Scores must therefore be **absolute against fixed anchors**, never
relative to the current pool — otherwise a candidate's rank silently depends on
who applied before them.

This is the decisive argument for anchored rubric scoring over similarity
scores: a cosine similarity is not comparable across jobs or across time, while
a 1–5 anchored competency score is.

## Decision Boundary

The matching pipeline **ranks and explains. It does not decide.**

- It does not auto-reject. A low score is a low rank, not a rejection.
- It does not auto-advance. Scoring ends at the leaderboard; a recruiter
  initiates the AI interview after reviewing the ranking.

Both directions of the decision belong to a human. Implementation must remove
the `match_resumes → generate_questions` edge in `orchestration/graph.py`
(line 555) rather than preserve it.

## Architecture

Four stages. Stage 0 runs once per job at creation; stages 1–3 run per candidate
on application submit.

```
Stage 0  Rubric synthesis     once per job    recruiter-gated
Stage 1  Retrieve             ~100ms          embeddings + structured features
Stage 2  Verify               2-4s            one LLM call, citations required
Stage 3  Aggregate & rank     instant         rubric-weighted, coverage-aware
```

A job with no approved rubric is not scorable. Applications to such a job are
accepted and queued, then scored once a rubric is approved.

---

### Stage 0 — Rubric Synthesis

Extends `JDAnalyzerAgent` into a new `agents/rubric_generator/`. From job
description text it drafts:

- **4–6 competencies.** The hiring-science literature is consistent that beyond
  roughly six, scoring quality degrades because no evaluator — human or model —
  scores deeply across more dimensions. Three is the floor.
- **A definition per competency**, in the language of the role.
- **Five anchor descriptors per competency** — written text describing what
  Insufficient, Emerging, Proficient, Strong, and Exceptional look like for
  *this* competency on *this* job.
- **Weights summing to 1.0.**

**Every dimension of fit is expressed as a competency.** Career trajectory,
skill recency, and culture fit are competencies in the rubric, not separate
parallel scores bolted onto the side. This is the structural choice that closes
rubric loopholes: one mechanism, one evidence standard, no gaps between
subsystems for a candidate to fall through.

Culture fit in particular is scored as defined behavioral traits with anchors,
never as an unfalsifiable overall impression. "Shares our values" must be
separable from "feels familiar" — the former is scoreable against evidence, the
latter is bias.

**Approval gate.** The draft is persisted with `status=draft` and is not usable
for scoring. A recruiter reviews, edits, and approves it. Before a draft may be
approved it must pass validation:

- 3–6 competencies
- weights sum to 1.0 ± 0.001
- every competency carries all five anchors, each non-empty
- no duplicate competency names

A malformed rubric is rejected at this gate, not warned about. A bad rubric
silently corrupts every ranking on the job, so it is itself a loophole.

**Versioning.** Approved rubrics are immutable. Editing an approved rubric mints
version N+1 and enqueues a background re-score of every candidate already scored
on that job. The leaderboard renders exactly one rubric version at a time and
displays a re-scoring banner while the new version backfills. Ranks are never
mixed across versions.

---

### Stage 1 — Retrieve

Recall is won here, so the net is cast deliberately wide.

**Span extraction.** The parsed resume is chunked into spans — individual
experience bullets, project descriptions, skill-list lines, education entries.
Each span carries a **stable span ID** and character offsets into the source
text. Span IDs are the backbone of the entire evidence system; everything
downstream cites them.

**Retrieval.** Embed each competency definition, embed each span, take the
**top-k spans per competency (k ≈ 8)**. A retrieved span that goes unused costs
nothing, so generosity here is free recall.

**Structured features.** Computed arithmetically from parsed dates rather than
inferred by the model, because they are calculation and not judgment:

- total tenure
- months elapsed since each skill was last used (recency)
- title progression sequence
- employment gaps

These are passed to Stage 2 alongside the spans.

---

### Stage 2 — Verify

Precision is won here. One LLM call per candidate.

**Input:** the competency set with anchors, the retrieved spans per competency,
and the structured features.

**Output, per competency:**

```
score:               1-5
cited_span_ids:      [span_id, ...]
rationale:           str
evidence_sufficiency: sufficient | partial | insufficient
```

**The load-bearing rule.** Cited span IDs are validated against the set actually
retrieved for that competency. A score citing span IDs that do not exist, or
citing none at all, is coerced to `insufficient` and **excluded from the
weighted score** — not counted as zero, not silently averaged in.

This single rule does most of the work:

- Hallucinated evidence cannot survive validation.
- Absent evidence is reported as absent rather than disguised as a low score.
- Keyword stuffing fails: a bare skills line retrieves as a span but cannot
  support a Proficient-or-above anchor requiring demonstrated application, so it
  scores low **with its own thin evidence quoted beside it**.

---

### Stage 3 — Aggregate and Rank

```
final_score = Σ(weight_i × score_i) / Σ(weight_i)    # cited competencies only
coverage    = Σ(weight_i for cited competencies)      # 0.0 - 1.0
```

This is the formula `ScoringAgent` already uses for interview scoring, so resume
matching and interview scoring finally produce commensurable numbers on one
scale.

**Coverage is a first-class output, not a footnote.** Below a coverage of 0.5
the candidate is routed to `needs_human_review` rather than assigned a rank. A
resume too sparse to judge is an *unknown*, not a *reject* — and conflating those
two is where most false-negative risk lives.

**Leaderboard rows** carry rank, final score, band (strong / potential / weak),
coverage, and expandable per-competency detail showing each score beside the
resume spans quoted in its support. Every position on the leaderboard is
auditable down to the sentence that produced it.

---

## Data Model

New tables, with Alembic migrations:

**`job_rubrics`** — `id`, `job_id`, `version`, `status` (draft | approved |
superseded), `created_at`, `approved_at`, `approved_by`.

**`rubric_competencies`** — `id`, `rubric_id`, `name`, `definition`, `weight`,
`anchor_1` … `anchor_5`.

**`resume_spans`** — `id` (the stable span ID), `application_id`, `span_type`,
`text`, `char_start`, `char_end`.

**`competency_scores`** — `id`, `application_id`, `rubric_version`,
`competency_id`, `score`, `evidence_sufficiency`, `rationale`, plus a join table
to cited span IDs.

`MatchingScore` in `schemas/evaluation.py` gains `coverage`, `rubric_version`,
and per-competency detail. Its `shortlist_recommendation` field is removed —
the system no longer makes that call.

## Error Handling

The governing rule: **a system failure must never look like a candidate
failure.**

| Failure | Behavior |
|---|---|
| LLM call fails | Retry once, then enqueue; application enters `scoring_pending` |
| Embedding provider fails | Fail loud into `scoring_pending` — never score 0.0 |
| Malformed LLM output | One reprompt, then `scoring_pending` |
| No approved rubric | Application accepted and queued; scored on rubric approval |

Nothing auto-rejects and nothing auto-advances. `scoring_pending` is a visible
state on the leaderboard, not a silent hole.

Fixing the `0.0`-on-failure fallback in `services/semantic_matching.py` is a
required part of this work, not a follow-up.

## Testing

Extending the property tests already in `evaluation/cases/resume_matcher.json`:

- **Adversarial (headline regression):** a keyword-stuffed resume with no
  substance must not out-rank a substantive one.
- **Citation integrity:** every non-`insufficient` score cites at least one
  span, and every cited span ID exists in that competency's retrieved set.
- **Coverage honesty:** a sparse resume yields `insufficient` evidence and low
  coverage, not a confidently low score.
- **Stability:** the same resume against the same rubric version yields the same
  band across runs. Non-determinism poisons a leaderboard.
- **Failure isolation:** an embedding or LLM outage produces `scoring_pending`,
  never a rejection.
- **Rubric validation:** malformed drafts — bad weight sums, missing anchors,
  seven competencies — are rejected at the approval gate.
- **Decoupling:** completing a match never creates or schedules an interview.

## Files Affected

- **New:** `agents/rubric_generator/`, `prompts/rubric_generator.md`,
  `prompts/competency_verifier.md`
- **Rewritten:** `agents/resume_matcher/agent.py`
- **Modified:** `services/semantic_matching.py` (span retrieval; remove the
  `0.0` failure fallback), `services/application_service.py` (pending states,
  re-score queue), `schemas/evaluation.py`, `orchestration/graph.py` (remove the
  `match_resumes → generate_questions` edge)
- **New endpoints:** rubric draft/approve/version, leaderboard read,
  recruiter-initiated advance-to-interview
- **New migrations:** the four tables above
