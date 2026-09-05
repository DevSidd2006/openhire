# Evidence-Bound Resume-to-JD Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed-weight similarity matcher with a rubric-driven, evidence-bound matcher whose every competency score must cite verbatim resume spans, feeding a per-job leaderboard.

**Architecture:** Four stages. Stage 0 synthesizes a recruiter-approved, versioned rubric from the JD. Stage 1 chunks the resume into stable-ID spans and retrieves the top-k spans per competency via embeddings. Stage 2 makes one LLM call that scores each competency 1-5 against written anchors and must cite span IDs; uncited scores are coerced to `insufficient` and excluded. Stage 3 aggregates rubric-weighted over cited competencies only, emitting a coverage figure alongside the score.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, pytest, asyncio. Existing `BaseAgent.call_llm_structured` for validated LLM output. Embeddings via `providers/embeddings`.

**Spec:** `docs/superpowers/specs/2026-09-02-resume-jd-matching-design.md`

## Global Constraints

- **Never push to GitHub.** Local commits only, and only when the user asks. No `git push` at any point in this plan.
- **No Alembic.** The README mentions Alembic but no Alembic tree exists. Persistence follows the real pattern: ABC in `repositories/interfaces.py`, in-memory impl in `repositories/memory.py`, DDL appended to `repositories/postgres/schema.sql`.
- **The matcher ranks and explains; it does not decide.** No auto-reject, no auto-advance to interview.
- **A system failure must never look like a candidate failure.** Every failure path yields `SCORING_PENDING`, never a score of 0.0 and never a rejection.
- **Competency count is 3-6 inclusive.** Weights sum to 1.0 +/- 0.001.
- **Anchor scale is 1-5** with all five descriptors required and non-empty.
- **Coverage threshold is 0.5.** Below it, the candidate is `NEEDS_HUMAN_REVIEW`, not ranked.
- **Retrieval k is 8** spans per competency.

---

### Task 1: Anchored rubric schemas

**Files:**
- Create: `schemas/rubric.py`
- Test: `tests/test_rubric_schema.py`

**Interfaces:**
- Consumes: nothing (foundation task)
- Produces: `AnchoredCompetency(name, definition, weight, anchors: dict[int,str])`, `JobRubric(rubric_id, job_id, version, status, competencies)`, `RubricStatus` enum with `DRAFT`/`APPROVED`/`SUPERSEDED`, and `JobRubric.validate_approvable() -> list[str]` returning human-readable violations (empty list means approvable).

- [ ] **Step 1: Write the failing test**

```python
import pytest
from pydantic import ValidationError
from schemas.rubric import AnchoredCompetency, JobRubric, RubricStatus


def _comp(name, weight):
    return AnchoredCompetency(
        name=name,
        definition=f"Ability to do {name}",
        weight=weight,
        anchors={1: "none", 2: "emerging", 3: "proficient", 4: "strong", 5: "exceptional"},
    )


def _rubric(comps, status=RubricStatus.DRAFT):
    return JobRubric(
        rubric_id="rub_1", job_id="job_1", version=1, status=status, competencies=comps
    )


def test_anchors_must_cover_all_five_levels():
    with pytest.raises(ValidationError):
        AnchoredCompetency(
            name="Python", definition="d", weight=1.0, anchors={1: "a", 2: "b", 3: "c"}
        )


def test_anchor_text_may_not_be_blank():
    with pytest.raises(ValidationError):
        AnchoredCompetency(
            name="Python",
            definition="d",
            weight=1.0,
            anchors={1: "a", 2: "  ", 3: "c", 4: "d", 5: "e"},
        )


def test_valid_rubric_is_approvable():
    rubric = _rubric([_comp("Python", 0.5), _comp("SQL", 0.3), _comp("Ownership", 0.2)])
    assert rubric.validate_approvable() == []


def test_too_few_competencies_is_not_approvable():
    rubric = _rubric([_comp("Python", 0.6), _comp("SQL", 0.4)])
    violations = rubric.validate_approvable()
    assert any("3" in v for v in violations)


def test_too_many_competencies_is_not_approvable():
    comps = [_comp(f"C{i}", 1 / 7) for i in range(7)]
    assert _rubric(comps).validate_approvable() != []


def test_weights_must_sum_to_one():
    rubric = _rubric([_comp("Python", 0.5), _comp("SQL", 0.2), _comp("Ownership", 0.2)])
    assert any("sum" in v.lower() for v in rubric.validate_approvable())


def test_duplicate_competency_names_rejected():
    rubric = _rubric([_comp("Python", 0.4), _comp("Python", 0.3), _comp("SQL", 0.3)])
    assert any("duplicate" in v.lower() for v in rubric.validate_approvable())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rubric_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schemas.rubric'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Anchored rubric schemas.

A rubric is the single mechanism through which every dimension of fit is
scored - skills, trajectory, recency, culture. Expressing all of them as
competencies (rather than as parallel bolt-on scores) is what removes the
gaps between subsystems that a candidate could otherwise fall through.

Distinct from schemas/job.py's `Competency`, which carries a weight but no
written anchors. Anchors are what make a 1-5 score absolute rather than
relative, and absolute scores are what make a leaderboard comparable across
candidates scored at different times.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List

from pydantic import BaseModel, Field, field_validator

ANCHOR_LEVELS = (1, 2, 3, 4, 5)
MIN_COMPETENCIES = 3
MAX_COMPETENCIES = 6
WEIGHT_SUM_TOLERANCE = 0.001


class RubricStatus(str, Enum):
    """DRAFT is not usable for scoring. APPROVED is immutable - an edit mints
    a new version and supersedes the old one."""

    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class AnchoredCompetency(BaseModel):
    """One scored dimension, with written descriptors for each 1-5 level."""

    name: str
    definition: str
    weight: float = Field(ge=0.0, le=1.0)
    anchors: Dict[int, str]

    @field_validator("anchors")
    @classmethod
    def all_five_anchors_present_and_non_blank(cls, v: Dict[int, str]) -> Dict[int, str]:
        missing = [lvl for lvl in ANCHOR_LEVELS if lvl not in v]
        if missing:
            raise ValueError(f"anchors missing for level(s) {missing}; all of 1-5 required")
        blank = [lvl for lvl in ANCHOR_LEVELS if not v[lvl].strip()]
        if blank:
            raise ValueError(f"anchor text is blank for level(s) {blank}")
        return v


class JobRubric(BaseModel):
    """A versioned set of competencies for one job."""

    rubric_id: str
    job_id: str
    version: int = Field(ge=1)
    status: RubricStatus = RubricStatus.DRAFT
    competencies: List[AnchoredCompetency] = Field(default_factory=list)

    def validate_approvable(self) -> List[str]:
        """Return every reason this rubric may not be approved.

        Returns a list rather than raising so the recruiter sees all problems
        at once instead of fixing them one round-trip at a time. A malformed
        rubric silently corrupts every ranking on the job, so this gate
        rejects rather than warns.
        """
        violations: List[str] = []
        n = len(self.competencies)
        if n < MIN_COMPETENCIES:
            violations.append(f"rubric has {n} competencies; at least {MIN_COMPETENCIES} required")
        if n > MAX_COMPETENCIES:
            violations.append(
                f"rubric has {n} competencies; at most {MAX_COMPETENCIES} allowed "
                "(scoring quality degrades beyond six)"
            )
        total = sum(c.weight for c in self.competencies)
        if self.competencies and abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            violations.append(f"competency weights sum to {total:.4f}; must sum to 1.0")
        names = [c.name.strip().lower() for c in self.competencies]
        dupes = {n_ for n_ in names if names.count(n_) > 1}
        if dupes:
            violations.append(f"duplicate competency names: {sorted(dupes)}")
        return violations
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rubric_schema.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add schemas/rubric.py tests/test_rubric_schema.py
git commit -m "feat(rubric): add anchored competency and versioned rubric schemas"
```

---

### Task 2: Resume span extraction

**Files:**
- Create: `services/resume_spans.py`
- Test: `tests/test_resume_spans.py`

**Interfaces:**
- Consumes: `ParsedResume` from `schemas/resume.py`
- Produces: `ResumeSpan(span_id, span_type, text)` and `extract_spans(resume: ParsedResume) -> list[ResumeSpan]`. Span IDs are stable: the same resume yields the same IDs across runs, because Stage 2 citations reference them and a leaderboard cannot tolerate IDs that shift between scorings.

- [ ] **Step 1: Write the failing test**

```python
from schemas.resume import ParsedResume, WorkExperience, Project
from services.resume_spans import extract_spans, ResumeSpan


def _resume():
    return ParsedResume(
        candidate_id="cand_1",
        candidate_name="Test Person",
        summary="Backend engineer.",
        skills=["Python", "Kubernetes"],
        work_experience=[
            WorkExperience(
                company="Acme",
                position="Senior Engineer",
                start_year=2020,
                end_year=2023,
                responsibilities=["Ran production Kubernetes clusters"],
                achievements=["Cut p99 latency by 40%"],
            )
        ],
        projects=[Project(name="Sched", description="Built a job scheduler", technologies=["Go"])],
    )


def test_extracts_a_span_per_bullet_project_and_skill_line():
    spans = extract_spans(_resume())
    types = {s.span_type for s in spans}
    assert "responsibility" in types
    assert "achievement" in types
    assert "project" in types
    assert "skills" in types
    assert "summary" in types


def test_span_ids_are_unique():
    spans = extract_spans(_resume())
    assert len({s.span_id for s in spans}) == len(spans)


def test_span_ids_are_stable_across_runs():
    first = extract_spans(_resume())
    second = extract_spans(_resume())
    assert [s.span_id for s in first] == [s.span_id for s in second]


def test_blank_text_produces_no_span():
    resume = ParsedResume(candidate_id="c", candidate_name="n", skills=[], summary="   ")
    assert extract_spans(resume) == []


def test_span_text_is_verbatim():
    spans = extract_spans(_resume())
    achievement = next(s for s in spans if s.span_type == "achievement")
    assert achievement.text == "Cut p99 latency by 40%"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_resume_spans.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.resume_spans'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Chunk a parsed resume into individually citable spans.

Span IDs are the backbone of the evidence system: Stage 2 scores cite them,
Stage 3 validates those citations, and the leaderboard renders the quoted
text behind every score. They must therefore be deterministic - a span ID
that shifted between scoring runs would silently invalidate stored citations.
IDs are derived from a content hash plus an ordinal, never from a UUID.
"""
from __future__ import annotations

import hashlib
from typing import List

from pydantic import BaseModel

from schemas.resume import ParsedResume


class ResumeSpan(BaseModel):
    """One verbatim, citable fragment of a resume."""

    span_id: str
    span_type: str  # summary | skills | responsibility | achievement | project | education
    text: str


def _span_id(span_type: str, text: str, ordinal: int) -> str:
    digest = hashlib.sha1(f"{span_type}:{text}".encode("utf-8")).hexdigest()[:8]
    return f"sp_{ordinal:03d}_{digest}"


def extract_spans(resume: ParsedResume) -> List[ResumeSpan]:
    """Return every non-blank citable fragment of `resume`, in stable order."""
    raw: List[tuple[str, str]] = []

    if resume.summary:
        raw.append(("summary", resume.summary))
    if resume.skills:
        raw.append(("skills", ", ".join(resume.skills)))

    for exp in resume.work_experience:
        header = f"{exp.position} at {exp.company} ({exp.start_year}-{exp.end_year or 'present'})"
        if exp.description:
            raw.append(("responsibility", f"{header}: {exp.description}"))
        for r in exp.responsibilities:
            raw.append(("responsibility", r))
        for a in exp.achievements:
            raw.append(("achievement", a))

    for proj in resume.projects:
        tech = f" [{', '.join(proj.technologies)}]" if proj.technologies else ""
        raw.append(("project", f"{proj.name}: {proj.description}{tech}"))

    for edu in resume.education:
        raw.append(("education", f"{edu.degree} in {edu.field_of_study}, {edu.institution}"))

    spans: List[ResumeSpan] = []
    for ordinal, (span_type, text) in enumerate(raw):
        stripped = text.strip()
        if not stripped:
            continue
        spans.append(
            ResumeSpan(span_id=_span_id(span_type, stripped, ordinal), span_type=span_type, text=stripped)
        )
    return spans
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_resume_spans.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add services/resume_spans.py tests/test_resume_spans.py
git commit -m "feat(matching): extract stable-ID citable spans from parsed resumes"
```

---

### Task 3: Fail loud on embedding outage, and add span retrieval

**Files:**
- Modify: `services/semantic_matching.py`
- Test: `tests/test_semantic_retrieval.py`

**Interfaces:**
- Consumes: `ResumeSpan` from Task 2, `AnchoredCompetency` from Task 1
- Produces: `EmbeddingUnavailableError`, and `SemanticMatcher.retrieve_spans_for_competency(competency, spans, k=8) -> list[ResumeSpan]` returning the k most similar spans, highest-similarity first.

This task fixes a live bug. `calculate_skill_semantic_similarity` and `calculate_job_description_similarity` currently `return 0.0` inside `except` blocks (lines 30, 71, 83, 102, 108, 115). An embedding-provider outage therefore becomes a wave of near-zero match scores and, downstream, a wave of rejections. A provider outage must be distinguishable from a genuinely poor candidate.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from schemas.rubric import AnchoredCompetency
from services.resume_spans import ResumeSpan
from services.semantic_matching import SemanticMatcher, EmbeddingUnavailableError


class _StubEmbeddings:
    """Embeds by keyword overlap so similarity is predictable in tests."""

    def __init__(self, fail=False):
        self.fail = fail

    async def embed_batch(self, texts):
        if self.fail:
            raise RuntimeError("provider down")
        return [[1.0, 0.0] if "kubernetes" in t.lower() else [0.0, 1.0] for t in texts]


def _competency():
    return AnchoredCompetency(
        name="Kubernetes operations",
        definition="Running production Kubernetes",
        weight=1.0,
        anchors={1: "a", 2: "b", 3: "c", 4: "d", 5: "e"},
    )


def _spans():
    return [
        ResumeSpan(span_id="sp_1", span_type="achievement", text="Ran Kubernetes in production"),
        ResumeSpan(span_id="sp_2", span_type="achievement", text="Wrote marketing copy"),
    ]


@pytest.mark.asyncio
async def test_retrieval_ranks_relevant_span_first():
    matcher = SemanticMatcher(embedding_provider=_StubEmbeddings())
    result = await matcher.retrieve_spans_for_competency(_competency(), _spans(), k=2)
    assert result[0].span_id == "sp_1"


@pytest.mark.asyncio
async def test_retrieval_respects_k():
    matcher = SemanticMatcher(embedding_provider=_StubEmbeddings())
    result = await matcher.retrieve_spans_for_competency(_competency(), _spans(), k=1)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_embedding_outage_raises_rather_than_scoring_zero():
    matcher = SemanticMatcher(embedding_provider=_StubEmbeddings(fail=True))
    with pytest.raises(EmbeddingUnavailableError):
        await matcher.retrieve_spans_for_competency(_competency(), _spans())


@pytest.mark.asyncio
async def test_skill_similarity_outage_raises_rather_than_returning_zero():
    matcher = SemanticMatcher(embedding_provider=_StubEmbeddings(fail=True))
    with pytest.raises(EmbeddingUnavailableError):
        await matcher.calculate_skill_semantic_similarity(["Python"], ["Python"], [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_semantic_retrieval.py -v`
Expected: FAIL with `ImportError: cannot import name 'EmbeddingUnavailableError'`

- [ ] **Step 3: Write the implementation**

Add near the top of `services/semantic_matching.py`, after the existing imports:

```python
class EmbeddingUnavailableError(Exception):
    """The embedding provider could not be reached or returned garbage.

    Raised instead of returning 0.0. A 0.0 similarity is indistinguishable
    from "this candidate is a terrible match", so swallowing an outage here
    silently converts an infrastructure failure into a wave of rejections.
    Callers are expected to route the application to SCORING_PENDING.
    """
```

Change `SemanticMatcher.__init__` to accept an injectable provider (the current no-arg version makes the failure paths untestable):

```python
    def __init__(self, embedding_provider=None):
        self.embedding_provider = embedding_provider or get_embedding_provider()
```

In `calculate_skill_semantic_similarity`, replace the trailing handler:

```python
        except Exception as e:
            logger.error(f"Embedding provider failed during skill matching: {e}")
            raise EmbeddingUnavailableError(str(e)) from e
```

In `calculate_job_description_similarity`, replace its trailing handler identically:

```python
        except Exception as e:
            logger.error(f"Embedding provider failed during JD similarity: {e}")
            raise EmbeddingUnavailableError(str(e)) from e
```

Then append the retrieval method to the class:

```python
    async def retrieve_spans_for_competency(
        self, competency, spans: list, k: int = 8
    ) -> list:
        """Return the `k` spans most semantically similar to `competency`.

        Deliberately generous: recall is won here, and a span that is
        retrieved but goes uncited in Stage 2 costs nothing. Being stingy
        with k, by contrast, silently caps recall.
        """
        if not spans:
            return []

        query = f"{competency.name}: {competency.definition}"
        try:
            embeddings = await self.embedding_provider.embed_batch([query] + [s.text for s in spans])
        except Exception as e:
            logger.error(f"Embedding provider failed during span retrieval: {e}")
            raise EmbeddingUnavailableError(str(e)) from e

        query_embedding, span_embeddings = embeddings[0], embeddings[1:]
        scored = [
            (self._cosine_similarity(query_embedding, emb), span)
            for emb, span in zip(span_embeddings, spans)
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [span for _, span in scored[:k]]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_semantic_retrieval.py -v && pytest tests/ -k "matching or matcher" -v`
Expected: new file PASSes 4 tests. Existing matcher tests may now fail where they relied on the silent 0.0 fallback — that is the bug being fixed; update those tests to expect `EmbeddingUnavailableError`.

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add services/semantic_matching.py tests/test_semantic_retrieval.py
git commit -m "fix(matching): fail loud on embedding outage instead of scoring 0.0"
```

---

### Task 4: Competency verification with mandatory citations

**Files:**
- Create: `services/competency_verifier.py`
- Create: `prompts/competency_verifier.md`
- Test: `tests/test_competency_verifier.py`

**Interfaces:**
- Consumes: `AnchoredCompetency` (Task 1), `ResumeSpan` (Task 2)
- Produces: `EvidenceSufficiency` enum (`SUFFICIENT`/`PARTIAL`/`INSUFFICIENT`), `CompetencyVerdict(competency_name, score, cited_span_ids, rationale, evidence_sufficiency)`, and `validate_citations(verdict, retrieved_span_ids) -> CompetencyVerdict` which coerces a verdict citing unknown or zero spans to `INSUFFICIENT`.

This is the load-bearing rule of the whole design. Citation validation is pure and synchronous, so it is tested without any LLM.

- [ ] **Step 1: Write the failing test**

```python
from services.competency_verifier import (
    CompetencyVerdict,
    EvidenceSufficiency,
    validate_citations,
)


def _verdict(cited, sufficiency=EvidenceSufficiency.SUFFICIENT, score=4):
    return CompetencyVerdict(
        competency_name="Kubernetes operations",
        score=score,
        cited_span_ids=cited,
        rationale="Ran clusters in production",
        evidence_sufficiency=sufficiency,
    )


def test_valid_citations_are_preserved():
    result = validate_citations(_verdict(["sp_1"]), {"sp_1", "sp_2"})
    assert result.evidence_sufficiency is EvidenceSufficiency.SUFFICIENT
    assert result.score == 4


def test_hallucinated_span_id_is_coerced_to_insufficient():
    result = validate_citations(_verdict(["sp_999"]), {"sp_1", "sp_2"})
    assert result.evidence_sufficiency is EvidenceSufficiency.INSUFFICIENT
    assert result.cited_span_ids == []


def test_citing_nothing_is_coerced_to_insufficient():
    result = validate_citations(_verdict([]), {"sp_1"})
    assert result.evidence_sufficiency is EvidenceSufficiency.INSUFFICIENT


def test_partially_hallucinated_citation_keeps_only_real_spans():
    result = validate_citations(_verdict(["sp_1", "sp_999"]), {"sp_1"})
    assert result.cited_span_ids == ["sp_1"]
    assert result.evidence_sufficiency is EvidenceSufficiency.SUFFICIENT


def test_coercion_records_the_reason_in_the_rationale():
    result = validate_citations(_verdict(["sp_999"]), {"sp_1"})
    assert "uncited" in result.rationale.lower() or "citation" in result.rationale.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_competency_verifier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.competency_verifier'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Stage 2: score each competency against its anchors, citing resume spans.

The load-bearing rule of the whole matcher lives in `validate_citations`: a
score whose cited span IDs do not exist in what was actually retrieved is
coerced to INSUFFICIENT and excluded from the weighted score - not counted
as zero, not silently averaged in.

That single rule does most of the work of closing rubric loopholes:
hallucinated evidence cannot survive validation; absent evidence is reported
as absent rather than disguised as a low score; and keyword stuffing fails,
because a bare skills line cannot support a Proficient-or-above anchor that
requires demonstrated application.
"""
from __future__ import annotations

from enum import Enum
from typing import List, Set

from pydantic import BaseModel, Field


class EvidenceSufficiency(str, Enum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class CompetencyVerdict(BaseModel):
    """One competency's score plus the evidence that justifies it."""

    competency_name: str
    score: int = Field(ge=1, le=5)
    cited_span_ids: List[str] = Field(default_factory=list)
    rationale: str
    evidence_sufficiency: EvidenceSufficiency


def validate_citations(
    verdict: CompetencyVerdict, retrieved_span_ids: Set[str]
) -> CompetencyVerdict:
    """Drop citations that do not correspond to a retrieved span.

    If nothing survives, the verdict becomes INSUFFICIENT regardless of the
    score the model assigned, and Stage 3 will exclude it from the weighted
    average rather than treating it as a low score.
    """
    real = [sid for sid in verdict.cited_span_ids if sid in retrieved_span_ids]
    dropped = len(verdict.cited_span_ids) - len(real)

    if not real:
        return verdict.model_copy(
            update={
                "cited_span_ids": [],
                "evidence_sufficiency": EvidenceSufficiency.INSUFFICIENT,
                "rationale": (
                    f"{verdict.rationale} [Coerced to insufficient: score was uncited or "
                    f"cited {dropped} span(s) not present in the retrieved evidence.]"
                ),
            }
        )

    if dropped:
        return verdict.model_copy(
            update={
                "cited_span_ids": real,
                "rationale": (
                    f"{verdict.rationale} [Dropped {dropped} invalid citation(s) during "
                    "citation validation.]"
                ),
            }
        )

    return verdict
```

Also create `prompts/competency_verifier.md`:

```markdown
You are scoring one candidate against one competency from a hiring rubric.

## Competency
Name: {competency_name}
Definition: {competency_definition}

## Scoring anchors
1 - Insufficient: {anchor_1}
2 - Emerging: {anchor_2}
3 - Proficient: {anchor_3}
4 - Strong: {anchor_4}
5 - Exceptional: {anchor_5}

## Candidate evidence (the ONLY evidence you may cite)
{spans_block}

## Structured facts
{structured_features}

## Rules
- Score 1-5 by choosing the anchor the evidence actually satisfies. Do not
  average, do not split the difference, do not be generous.
- You MUST cite the span_id of every span supporting your score. A score
  with no citation will be discarded.
- Cite ONLY span_ids that appear above. Inventing a span_id discards the score.
- A skill merely listed is not a skill demonstrated. A bare mention in a
  skills line supports at most level 2, never 3 or above, because levels 3+
  require evidence of applied work.
- If the evidence does not let you judge this competency, set
  evidence_sufficiency to "insufficient" and say what is missing. Reporting
  a gap honestly is correct behavior, not a failure.

Return JSON: competency_name, score, cited_span_ids, rationale, evidence_sufficiency.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_competency_verifier.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add services/competency_verifier.py prompts/competency_verifier.md tests/test_competency_verifier.py
git commit -m "feat(matching): add citation-validated competency verdicts"
```

---

### Task 5: Coverage-aware rubric aggregation

**Files:**
- Create: `services/rubric_aggregation.py`
- Test: `tests/test_rubric_aggregation.py`

**Interfaces:**
- Consumes: `AnchoredCompetency` (Task 1), `CompetencyVerdict` + `EvidenceSufficiency` (Task 4)
- Produces: `AggregateResult(final_score, coverage, band, needs_human_review, scored_competencies, uncited_competencies)` and `aggregate(competencies, verdicts) -> AggregateResult`. `final_score` is normalized 0.0-1.0.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from schemas.rubric import AnchoredCompetency
from services.competency_verifier import CompetencyVerdict, EvidenceSufficiency
from services.rubric_aggregation import aggregate, COVERAGE_THRESHOLD


def _comp(name, weight):
    return AnchoredCompetency(
        name=name, definition="d", weight=weight,
        anchors={1: "a", 2: "b", 3: "c", 4: "d", 5: "e"},
    )


def _verdict(name, score, sufficiency=EvidenceSufficiency.SUFFICIENT):
    return CompetencyVerdict(
        competency_name=name, score=score,
        cited_span_ids=["sp_1"] if sufficiency is not EvidenceSufficiency.INSUFFICIENT else [],
        rationale="r", evidence_sufficiency=sufficiency,
    )


def test_full_coverage_weighted_score():
    comps = [_comp("A", 0.5), _comp("B", 0.5)]
    result = aggregate(comps, [_verdict("A", 5), _verdict("B", 1)])
    assert result.coverage == pytest.approx(1.0)
    assert result.final_score == pytest.approx(0.6)  # (5*.5 + 1*.5)/5 = 3/5


def test_weights_actually_matter():
    comps = [_comp("A", 0.9), _comp("B", 0.1)]
    result = aggregate(comps, [_verdict("A", 5), _verdict("B", 1)])
    assert result.final_score > 0.9


def test_insufficient_evidence_is_excluded_not_zeroed():
    comps = [_comp("A", 0.5), _comp("B", 0.5)]
    result = aggregate(
        comps, [_verdict("A", 5), _verdict("B", 1, EvidenceSufficiency.INSUFFICIENT)]
    )
    assert result.coverage == pytest.approx(0.5)
    assert result.final_score == pytest.approx(1.0)  # only A counted, not averaged with a zero
    assert result.uncited_competencies == ["B"]


def test_low_coverage_routes_to_human_review_instead_of_ranking():
    comps = [_comp("A", 0.3), _comp("B", 0.7)]
    result = aggregate(
        comps, [_verdict("A", 4), _verdict("B", 3, EvidenceSufficiency.INSUFFICIENT)]
    )
    assert result.coverage < COVERAGE_THRESHOLD
    assert result.needs_human_review is True


def test_no_evidence_at_all_is_review_not_zero_score():
    comps = [_comp("A", 1.0)]
    result = aggregate(comps, [_verdict("A", 1, EvidenceSufficiency.INSUFFICIENT)])
    assert result.needs_human_review is True
    assert result.final_score is None


def test_bands_are_assigned_from_absolute_thresholds():
    comps = [_comp("A", 1.0)]
    assert aggregate(comps, [_verdict("A", 5)]).band == "strong"
    assert aggregate(comps, [_verdict("A", 3)]).band == "potential"
    assert aggregate(comps, [_verdict("A", 1)]).band == "weak"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rubric_aggregation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.rubric_aggregation'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Stage 3: combine competency verdicts into one rubric-weighted score.

Uses the same weighted formula agents/scoring/agent.py already applies to
interview evaluations, so resume matching and interview scoring finally
produce commensurable numbers on one scale.

Coverage is a first-class output, not a footnote. A resume too sparse to
judge is an UNKNOWN, not a REJECT, and conflating those two is where most
false-negative risk lives - so below COVERAGE_THRESHOLD the candidate is
routed to human review rather than given a misleadingly confident low rank.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

from schemas.rubric import AnchoredCompetency
from services.competency_verifier import CompetencyVerdict, EvidenceSufficiency

COVERAGE_THRESHOLD = 0.5
MAX_ANCHOR_SCORE = 5
STRONG_BAND_MIN = 0.75
POTENTIAL_BAND_MIN = 0.45


class AggregateResult(BaseModel):
    """The candidate's standing on one rubric version."""

    final_score: Optional[float] = None  # None when coverage is too low to rank
    coverage: float
    band: Optional[str] = None  # strong | potential | weak
    needs_human_review: bool
    scored_competencies: List[str]
    uncited_competencies: List[str]


def _band(normalized: float) -> str:
    if normalized >= STRONG_BAND_MIN:
        return "strong"
    if normalized >= POTENTIAL_BAND_MIN:
        return "potential"
    return "weak"


def aggregate(
    competencies: List[AnchoredCompetency], verdicts: List[CompetencyVerdict]
) -> AggregateResult:
    """Weighted-average the cited verdicts; report coverage separately."""
    by_name = {v.competency_name: v for v in verdicts}

    weighted_sum = 0.0
    counted_weight = 0.0
    scored: List[str] = []
    uncited: List[str] = []

    for comp in competencies:
        verdict = by_name.get(comp.name)
        if verdict is None or verdict.evidence_sufficiency is EvidenceSufficiency.INSUFFICIENT:
            uncited.append(comp.name)
            continue
        weighted_sum += comp.weight * verdict.score
        counted_weight += comp.weight
        scored.append(comp.name)

    total_weight = sum(c.weight for c in competencies) or 1.0
    coverage = counted_weight / total_weight

    if coverage < COVERAGE_THRESHOLD or counted_weight == 0.0:
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
        needs_human_review=False,
        scored_competencies=scored,
        uncited_competencies=uncited,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rubric_aggregation.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add services/rubric_aggregation.py tests/test_rubric_aggregation.py
git commit -m "feat(matching): add coverage-aware rubric aggregation"
```

---

### Task 6: Decouple matching from interview generation

**Files:**
- Modify: `orchestration/graph.py:555`
- Test: `tests/test_matching_interview_decoupling.py`

**Interfaces:**
- Consumes: the existing compiled pipeline from `orchestration/graph.py`
- Produces: no new symbols. The pipeline edge `match_resumes -> generate_questions` is removed; `match_resumes` terminates at `leaderboard`.

The current graph wires `graph.add_edge("match_resumes", "generate_questions")`, so a match score automatically triggers interview question generation with no human in between. Scoring must end at the leaderboard; a recruiter decides who advances.

- [ ] **Step 1: Write the failing test**

```python
from orchestration.graph import build_pipeline


def test_matching_does_not_flow_into_interview_generation():
    """A match score must never auto-start an interview. Advancing a
    candidate is a recruiter action taken after reading the leaderboard."""
    graph = build_pipeline().get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    assert ("match_resumes", "generate_questions") not in edges


def test_matching_terminates_at_the_leaderboard():
    graph = build_pipeline().get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    assert ("match_resumes", "leaderboard") in edges
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_matching_interview_decoupling.py -v`
Expected: FAIL on the first test — the edge currently exists.

Note: if the pipeline builder is not exported as `build_pipeline`, use the actual factory name found at the bottom of `orchestration/graph.py` (there is a `_pipeline` singleton there) and adjust both tests.

- [ ] **Step 3: Edit the graph**

In `orchestration/graph.py`, delete line 555:

```python
    graph.add_edge("match_resumes", "generate_questions")
```

and replace it with:

```python
    # Matching terminates at the leaderboard. It deliberately does NOT flow
    # into generate_questions: a match score must never auto-start an AI
    # interview. Ranked candidates are surfaced to the recruiter, who
    # initiates interviews explicitly. The matcher ranks and explains; it
    # does not decide - in either direction (no auto-advance, no auto-reject).
    graph.add_edge("match_resumes", "leaderboard")
```

Then make `generate_questions` reachable only from an explicit recruiter-initiated entry point rather than from `match_resumes`. If the interview stages must remain runnable as a pipeline, extract them into a second graph entered at `generate_questions`; do not re-link them to `match_resumes`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_matching_interview_decoupling.py -v && pytest tests/test_pipeline.py -v`
Expected: new tests PASS. `test_pipeline.py` assertions that expected interview generation to follow matching must be updated — that coupling is the bug being removed.

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add orchestration/graph.py tests/test_matching_interview_decoupling.py
git commit -m "fix(pipeline): matching no longer auto-triggers interview generation"
```

---

### Task 7: Rubric generator agent

**Files:**
- Create: `agents/rubric_generator/__init__.py`
- Create: `agents/rubric_generator/agent.py`
- Create: `prompts/rubric_generator.md`
- Test: `tests/test_rubric_generator.py`

**Interfaces:**
- Consumes: `JobDescription` (`schemas/job.py`), `JobRubric`/`AnchoredCompetency`/`RubricStatus` (Task 1), `BaseAgent` (`agents/base.py`)
- Produces: `RubricGeneratorAgent.execute(job_description) -> dict` with key `rubric` holding a `JobRubric` at `status=DRAFT`.

The draft is never auto-approved. A rubric that decides every ranking on the job must be owned by a human.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from agents.rubric_generator.agent import RubricGeneratorAgent
from schemas.job import JobDescription
from schemas.rubric import RubricStatus


class _StubLLM:
    def __init__(self, payload):
        self.payload = payload

    async def generate_structured(self, prompt, schema, **kwargs):
        return self.payload


def _job():
    return JobDescription(
        job_id="job_1", title="Backend Engineer",
        description="Build and run Python services on Kubernetes.",
        required_skills=["Python", "Kubernetes"], experience_years=3,
    )


def _payload():
    anchors = {"1": "none", "2": "emerging", "3": "proficient", "4": "strong", "5": "exceptional"}
    return {
        "competencies": [
            {"name": "Python engineering", "definition": "Writes production Python", "weight": 0.4, "anchors": anchors},
            {"name": "Kubernetes operations", "definition": "Runs clusters", "weight": 0.35, "anchors": anchors},
            {"name": "Ownership", "definition": "Drives work to completion", "weight": 0.25, "anchors": anchors},
        ]
    }


@pytest.mark.asyncio
async def test_generates_a_draft_rubric():
    agent = RubricGeneratorAgent(llm_provider=_StubLLM(_payload()))
    result = await agent.execute(job_description=_job())
    rubric = result["rubric"]
    assert rubric.status is RubricStatus.DRAFT
    assert len(rubric.competencies) == 3


@pytest.mark.asyncio
async def test_generated_rubric_passes_its_own_approval_gate():
    agent = RubricGeneratorAgent(llm_provider=_StubLLM(_payload()))
    result = await agent.execute(job_description=_job())
    assert result["rubric"].validate_approvable() == []


@pytest.mark.asyncio
async def test_rubric_is_never_auto_approved():
    agent = RubricGeneratorAgent(llm_provider=_StubLLM(_payload()))
    result = await agent.execute(job_description=_job())
    assert result["rubric"].status is not RubricStatus.APPROVED


@pytest.mark.asyncio
async def test_weights_are_normalized_when_the_model_returns_a_bad_sum():
    payload = _payload()
    payload["competencies"][0]["weight"] = 0.9  # sums to 1.5
    agent = RubricGeneratorAgent(llm_provider=_StubLLM(payload))
    result = await agent.execute(job_description=_job())
    assert result["rubric"].validate_approvable() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rubric_generator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agents.rubric_generator'`

- [ ] **Step 3: Write minimal implementation**

`agents/rubric_generator/__init__.py`:

```python
from agents.rubric_generator.agent import RubricGeneratorAgent

__all__ = ["RubricGeneratorAgent"]
```

`agents/rubric_generator/agent.py`:

```python
"""Stage 0: draft an anchored rubric from a job description.

The output is always a DRAFT. A rubric decides every ranking on its job, so
a bad auto-generated rubric silently corrupts the whole leaderboard - which
makes an unreviewed rubric a loophole in its own right. A recruiter must
approve it before any candidate can be scored.

All dimensions of fit - skills, career trajectory, skill recency, culture -
are drafted as competencies here rather than as separate parallel scores.
One mechanism, one evidence standard, no gaps between subsystems.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from agents.base import BaseAgent
from schemas.job import JobDescription
from schemas.rubric import (
    MAX_COMPETENCIES,
    MIN_COMPETENCIES,
    AnchoredCompetency,
    JobRubric,
    RubricStatus,
)


class _LLMCompetency(BaseModel):
    name: str
    definition: str
    weight: float
    anchors: Dict[str, str]  # the model returns string keys in JSON


class _LLMRubric(BaseModel):
    competencies: List[_LLMCompetency] = Field(min_length=MIN_COMPETENCIES, max_length=MAX_COMPETENCIES)


class RubricGeneratorAgent(BaseAgent):
    """Drafts an anchored, weighted rubric from job description text."""

    def __init__(self, **kwargs):
        super().__init__(name="rubric_generator", **kwargs)

    async def execute(self, job_description: JobDescription, **kwargs) -> Dict[str, Any]:
        self.logger.info(f"Drafting rubric for job {job_description.job_id}")

        prompt = self.load_prompt("rubric_generator.md").format(
            title=job_description.title,
            description=job_description.description,
            required_skills=", ".join(job_description.required_skills),
            preferred_skills=", ".join(job_description.preferred_skills),
            experience_years=job_description.experience_years or "unspecified",
            responsibilities="\n".join(f"- {r}" for r in job_description.responsibilities),
        )

        drafted = await self.call_llm_structured(
            prompt,
            _LLMRubric.model_json_schema(),
            validate=_LLMRubric.model_validate,
        )

        competencies = self._normalize(drafted.competencies)

        return {
            "rubric": JobRubric(
                rubric_id=f"rub_{uuid.uuid4().hex[:8]}",
                job_id=job_description.job_id,
                version=1,
                status=RubricStatus.DRAFT,
                competencies=competencies,
            )
        }

    @staticmethod
    def _normalize(raw: List[_LLMCompetency]) -> List[AnchoredCompetency]:
        """Rescale weights to sum to exactly 1.0.

        Models routinely emit weights summing to 0.95 or 1.5. Rescaling
        preserves the model's intended *relative* emphasis while satisfying
        the approval gate, which is better than rejecting an otherwise sound
        draft over arithmetic.
        """
        total = sum(c.weight for c in raw) or 1.0
        return [
            AnchoredCompetency(
                name=c.name,
                definition=c.definition,
                weight=c.weight / total,
                anchors={int(level): text for level, text in c.anchors.items()},
            )
            for c in raw
        ]
```

`prompts/rubric_generator.md`:

```markdown
You are designing a hiring rubric for one role. The rubric decides how every
applicant to this job is ranked, so it must be specific to this role rather
than generic.

## Job
Title: {title}
Description: {description}
Required skills: {required_skills}
Preferred skills: {preferred_skills}
Years of experience expected: {experience_years}
Responsibilities:
{responsibilities}

## Rules
- Produce between 3 and 6 competencies. Never more than 6: beyond six,
  scoring degrades because no evaluator scores deeply across more dimensions.
- Choose the competencies that SEPARATE strong candidates from average ones
  for this specific role. Do not list every skill mentioned in the posting.
- Cover more than hard skills. Where the role warrants it, include career
  trajectory (growth and progression), skill recency (how current the
  relevant experience is), and behavioral/values competencies.
- Behavioral competencies must be written as observable behaviors, never as
  cultural similarity. "Drives ambiguous work to completion" is scoreable;
  "would fit in with the team" is not, and is bias.
- Weights must sum to 1.0 and reflect real importance to this role.
- Write all five anchors (1-5) for every competency, as concrete descriptions
  of what evidence at that level looks like ON A RESUME. Level 3+ anchors
  must require evidence of applied work, not a mention.

Return JSON: {{"competencies": [{{"name", "definition", "weight", "anchors": {{"1".."5"}}}}]}}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rubric_generator.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add agents/rubric_generator/ prompts/rubric_generator.md tests/test_rubric_generator.py
git commit -m "feat(rubric): draft anchored rubrics from job descriptions"
```

---

### Task 8: Rewire ResumeMatcherAgent onto the rubric pipeline

**Files:**
- Modify: `agents/resume_matcher/agent.py` (full rewrite of `_calculate_match`)
- Modify: `schemas/evaluation.py:195-213` (extend `MatchingScore`)
- Test: `tests/test_evidence_bound_matcher.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5
- Produces: `ResumeMatcherAgent.execute(job_rubric, parsed_resume) -> dict` with keys `matching_score` (a `MatchingScore`) and `needs_human_review` (bool). `MatchingScore` gains `coverage: float`, `rubric_version: int`, `competency_verdicts: list[CompetencyVerdict]`, `band: Optional[str]`, and loses `shortlist_recommendation`.

Removing `shortlist_recommendation` is deliberate: the system no longer makes that call. Note that `services/application_service.py` and `orchestration/graph.py` both read this field today, so both must be updated — see Task 9.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from agents.resume_matcher.agent import ResumeMatcherAgent
from schemas.resume import ParsedResume, WorkExperience
from schemas.rubric import AnchoredCompetency, JobRubric, RubricStatus
from services.semantic_matching import EmbeddingUnavailableError


def _rubric():
    anchors = {1: "no evidence", 2: "mentioned only", 3: "applied it",
               4: "led work with it", 5: "recognized expertise"}
    return JobRubric(
        rubric_id="rub_1", job_id="job_1", version=1, status=RubricStatus.APPROVED,
        competencies=[
            AnchoredCompetency(name="Kubernetes operations", definition="Runs clusters",
                               weight=0.6, anchors=anchors),
            AnchoredCompetency(name="Python engineering", definition="Ships Python",
                               weight=0.4, anchors=anchors),
        ],
    )


def _substantive_resume():
    return ParsedResume(
        candidate_id="cand_real", candidate_name="Real Engineer",
        skills=["Python", "Kubernetes"],
        work_experience=[WorkExperience(
            company="Acme", position="SRE", start_year=2019, end_year=2024,
            achievements=["Operated 40-node Kubernetes clusters serving 2M req/day"],
            responsibilities=["Wrote Python tooling for cluster autoscaling"],
        )],
        total_experience_years=5,
    )


def _keyword_stuffed_resume():
    return ParsedResume(
        candidate_id="cand_stuffed", candidate_name="Keyword Stuffer",
        skills=["Python", "Kubernetes", "Go", "Rust", "Terraform", "Kafka"],
        work_experience=[], total_experience_years=1,
    )


class _StubMatcher:
    """Returns all spans for every competency; no real embeddings."""
    def __init__(self, fail=False):
        self.fail = fail

    async def retrieve_spans_for_competency(self, competency, spans, k=8):
        if self.fail:
            raise EmbeddingUnavailableError("provider down")
        return spans[:k]


class _StubLLM:
    """Scores by whether an achievement/responsibility span exists."""
    async def generate_structured(self, prompt, schema, **kwargs):
        has_applied_evidence = "achievement" in prompt or "Operated" in prompt
        span_ids = [line.split("]")[0].strip("[ ") for line in prompt.splitlines()
                    if line.strip().startswith("[sp_")]
        return {"verdicts": [
            {"competency_name": name, "score": 4 if has_applied_evidence else 2,
             "cited_span_ids": span_ids[:1] if span_ids else [],
             "rationale": "stub", "evidence_sufficiency": "sufficient" if span_ids else "insufficient"}
            for name in ("Kubernetes operations", "Python engineering")
        ]}


@pytest.mark.asyncio
async def test_substantive_resume_outranks_keyword_stuffed_resume():
    agent = ResumeMatcherAgent(semantic_matcher=_StubMatcher(), llm_provider=_StubLLM())
    real = await agent.execute(job_rubric=_rubric(), parsed_resume=_substantive_resume())
    stuffed = await agent.execute(job_rubric=_rubric(), parsed_resume=_keyword_stuffed_resume())
    assert real["matching_score"].match_score > stuffed["matching_score"].match_score


@pytest.mark.asyncio
async def test_score_carries_the_rubric_version_it_was_computed_against():
    agent = ResumeMatcherAgent(semantic_matcher=_StubMatcher(), llm_provider=_StubLLM())
    result = await agent.execute(job_rubric=_rubric(), parsed_resume=_substantive_resume())
    assert result["matching_score"].rubric_version == 1


@pytest.mark.asyncio
async def test_every_scored_competency_cites_a_real_span():
    agent = ResumeMatcherAgent(semantic_matcher=_StubMatcher(), llm_provider=_StubLLM())
    result = await agent.execute(job_rubric=_rubric(), parsed_resume=_substantive_resume())
    from services.competency_verifier import EvidenceSufficiency
    for verdict in result["matching_score"].competency_verdicts:
        if verdict.evidence_sufficiency is not EvidenceSufficiency.INSUFFICIENT:
            assert verdict.cited_span_ids


@pytest.mark.asyncio
async def test_embedding_outage_raises_rather_than_producing_a_low_score():
    agent = ResumeMatcherAgent(semantic_matcher=_StubMatcher(fail=True), llm_provider=_StubLLM())
    with pytest.raises(EmbeddingUnavailableError):
        await agent.execute(job_rubric=_rubric(), parsed_resume=_substantive_resume())


@pytest.mark.asyncio
async def test_matching_never_returns_a_shortlist_decision():
    agent = ResumeMatcherAgent(semantic_matcher=_StubMatcher(), llm_provider=_StubLLM())
    result = await agent.execute(job_rubric=_rubric(), parsed_resume=_substantive_resume())
    assert not hasattr(result["matching_score"], "shortlist_recommendation")
    assert "shortlist_recommendation" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_evidence_bound_matcher.py -v`
Expected: FAIL — `execute()` does not accept `job_rubric`.

- [ ] **Step 3: Extend MatchingScore, then rewrite the matcher**

In `schemas/evaluation.py`, replace the `MatchingScore` body (lines 195-213) with:

```python
class MatchingScore(BaseModel):
    """Resume to job matching score, computed against one rubric version.

    Carries `coverage` and `rubric_version` as first-class fields because a
    leaderboard is only coherent when every row was scored against the same
    rubric, and because a score computed from half the rubric is a different
    kind of claim than one computed from all of it.

    Deliberately carries NO shortlist_recommendation: the matcher ranks and
    explains, it does not decide. Advancing and rejecting are both recruiter
    actions.
    """
    match_id: str
    candidate_id: str
    job_id: str

    rubric_version: int
    match_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    coverage: float = Field(ge=0.0, le=1.0)
    band: Optional[str] = None

    competency_verdicts: List["CompetencyVerdict"] = Field(default_factory=list)
    needs_human_review: bool = False

    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)
```

Add at the top of `schemas/evaluation.py`:

```python
from services.competency_verifier import CompetencyVerdict
```

Then rewrite `agents/resume_matcher/agent.py` entirely:

```python
"""Evidence-bound resume matcher.

Replaces the previous fixed-weight similarity scorer (50% skills / 20% JD
similarity / 30% experience). That design could not distinguish a claim from
evidence - a resume mentioning Kubernetes scored the same whether the
candidate ran production clusters or attended a webinar - which made keyword
stuffing a winning strategy and made no ranking auditable.

Here, every competency in the job's rubric is scored 1-5 against written
anchors, and every score must cite the resume spans that justify it. Scores
that cite nothing real are excluded rather than counted as zero.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from pydantic import BaseModel

from agents.base import BaseAgent
from schemas.evaluation import MatchingScore
from schemas.resume import ParsedResume
from schemas.rubric import JobRubric
from services.competency_verifier import CompetencyVerdict, validate_citations
from services.resume_spans import ResumeSpan, extract_spans
from services.rubric_aggregation import aggregate
from services.semantic_matching import SemanticMatcher

RETRIEVAL_K = 8


class _VerdictBatch(BaseModel):
    verdicts: List[CompetencyVerdict]


class ResumeMatcherAgent(BaseAgent):
    """Scores a resume against a job's approved rubric, with citations."""

    def __init__(self, semantic_matcher=None, **kwargs):
        super().__init__(name="resume_matcher", **kwargs)
        self.semantic_matcher = semantic_matcher or SemanticMatcher()

    async def execute(
        self, job_rubric: JobRubric, parsed_resume: ParsedResume, **kwargs
    ) -> Dict[str, Any]:
        """Score one candidate against one rubric version.

        Deliberately does NOT catch exceptions: an embedding outage or LLM
        failure must propagate so the caller can mark the application
        SCORING_PENDING. Swallowing them here would turn an infrastructure
        failure into a low score, and a low score into a rejection.
        """
        self.logger.info(
            f"Matching candidate {parsed_resume.candidate_id} against rubric "
            f"{job_rubric.rubric_id} v{job_rubric.version}"
        )

        spans = extract_spans(parsed_resume)

        retrieved: Dict[str, List[ResumeSpan]] = {}
        for comp in job_rubric.competencies:
            retrieved[comp.name] = await self.semantic_matcher.retrieve_spans_for_competency(
                comp, spans, k=RETRIEVAL_K
            )

        verdicts = await self._verify(job_rubric, retrieved, parsed_resume)
        result = aggregate(job_rubric.competencies, verdicts)

        explanation = self._explain(result, verdicts)

        return {
            "matching_score": MatchingScore(
                match_id=f"match_{uuid.uuid4().hex[:8]}",
                candidate_id=parsed_resume.candidate_id,
                job_id=job_rubric.job_id,
                rubric_version=job_rubric.version,
                match_score=result.final_score,
                coverage=result.coverage,
                band=result.band,
                competency_verdicts=verdicts,
                needs_human_review=result.needs_human_review,
                explanation=explanation,
                confidence=result.coverage,
            ),
            "needs_human_review": result.needs_human_review,
        }

    async def _verify(
        self, rubric: JobRubric, retrieved: Dict[str, List[ResumeSpan]], resume: ParsedResume
    ) -> List[CompetencyVerdict]:
        """One LLM call scoring every competency, then citation validation."""
        prompt = self._build_prompt(rubric, retrieved, resume)

        batch = await self.call_llm_structured(
            prompt, _VerdictBatch.model_json_schema(), validate=_VerdictBatch.model_validate
        )

        validated: List[CompetencyVerdict] = []
        for verdict in batch.verdicts:
            allowed = {s.span_id for s in retrieved.get(verdict.competency_name, [])}
            validated.append(validate_citations(verdict, allowed))
        return validated

    def _build_prompt(
        self, rubric: JobRubric, retrieved: Dict[str, List[ResumeSpan]], resume: ParsedResume
    ) -> str:
        template = self.load_prompt("competency_verifier.md")
        blocks = []
        for comp in rubric.competencies:
            spans_block = "\n".join(
                f"[{s.span_id}] ({s.span_type}) {s.text}" for s in retrieved.get(comp.name, [])
            ) or "(no evidence retrieved for this competency)"
            blocks.append(
                template.format(
                    competency_name=comp.name,
                    competency_definition=comp.definition,
                    anchor_1=comp.anchors[1], anchor_2=comp.anchors[2], anchor_3=comp.anchors[3],
                    anchor_4=comp.anchors[4], anchor_5=comp.anchors[5],
                    spans_block=spans_block,
                    structured_features=(
                        f"Total experience: {resume.total_experience_years or 'unknown'} years; "
                        f"{len(resume.work_experience)} roles listed"
                    ),
                )
            )
        return "\n\n---\n\n".join(blocks) + '\n\nReturn JSON: {"verdicts": [...]}'

    @staticmethod
    def _explain(result, verdicts: List[CompetencyVerdict]) -> str:
        if result.needs_human_review:
            return (
                f"Insufficient resume evidence to rank this candidate "
                f"(coverage {result.coverage:.0%}). Unscored: "
                f"{', '.join(result.uncited_competencies)}. Routed to human review "
                "rather than scored low - a sparse resume is an unknown, not a reject."
            )
        parts = [
            f"{v.competency_name}: {v.score}/5 ({len(v.cited_span_ids)} cited span(s))"
            for v in verdicts
            if v.competency_name in result.scored_competencies
        ]
        return f"Band {result.band}, coverage {result.coverage:.0%}. " + "; ".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_evidence_bound_matcher.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add agents/resume_matcher/agent.py schemas/evaluation.py tests/test_evidence_bound_matcher.py
git commit -m "feat(matching): rewrite matcher as evidence-bound rubric scorer"
```

---

### Task 9: Application states for pending and review

**Files:**
- Modify: `schemas/application.py:42-62`
- Modify: `services/application_service.py`
- Modify: `services/matching_service.py`
- Test: `tests/test_matching_failure_states.py`

**Interfaces:**
- Consumes: `MatchingScore` (Task 8), `EmbeddingUnavailableError` (Task 3)
- Produces: `ApplicationStatus.SCORING_PENDING` and `ApplicationStatus.NEEDS_HUMAN_REVIEW`; `ApplicationService.score_application(application_id) -> Application`.

`SHORTLISTED`/`REJECTED` remain in the enum but are now set only by explicit recruiter action, never by the matcher.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from schemas.application import ApplicationStatus
from services.semantic_matching import EmbeddingUnavailableError


def test_pending_and_review_states_exist():
    assert ApplicationStatus.SCORING_PENDING.value == "scoring_pending"
    assert ApplicationStatus.NEEDS_HUMAN_REVIEW.value == "needs_human_review"


@pytest.mark.asyncio
async def test_embedding_outage_yields_pending_not_rejected(application_service_with_failing_matcher):
    service, app_id = application_service_with_failing_matcher
    result = await service.score_application(app_id)
    assert result.status is ApplicationStatus.SCORING_PENDING
    assert result.status is not ApplicationStatus.REJECTED


@pytest.mark.asyncio
async def test_low_coverage_yields_review_not_rejected(application_service_with_sparse_resume):
    service, app_id = application_service_with_sparse_resume
    result = await service.score_application(app_id)
    assert result.status is ApplicationStatus.NEEDS_HUMAN_REVIEW


@pytest.mark.asyncio
async def test_scoring_never_sets_rejected(application_service_with_weak_candidate):
    service, app_id = application_service_with_weak_candidate
    result = await service.score_application(app_id)
    assert result.status is not ApplicationStatus.REJECTED
```

Fixtures go in `tests/conftest.py`, building an `ApplicationService` over `repositories/memory.py` implementations with a stubbed matcher (raising `EmbeddingUnavailableError`, returning `needs_human_review=True`, and returning a low but covered score respectively).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_matching_failure_states.py -v`
Expected: FAIL with `AttributeError: SCORING_PENDING`

- [ ] **Step 3: Write minimal implementation**

In `schemas/application.py`, add to `ApplicationStatus` with docstring entries:

```python
    SCORING_PENDING = "scoring_pending"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
```

Extend the enum docstring:

```
    SCORING_PENDING   scoring could not complete (embedding/LLM outage, or no
                 approved rubric yet). NOT a judgment about the candidate -
                 a system failure must never look like a candidate failure.
    NEEDS_HUMAN_REVIEW  scoring completed but rubric coverage was below
                 threshold: too little resume evidence to rank fairly. An
                 unknown, not a reject.
```

In `services/application_service.py`, add:

```python
    async def score_application(self, application_id: str) -> Application:
        """Score one application against its job's approved rubric.

        Never rejects and never shortlists. Failure routes to
        SCORING_PENDING; thin evidence routes to NEEDS_HUMAN_REVIEW; success
        stores the score and leaves the application awaiting a recruiter's
        decision.
        """
        application = await self._applications.get(application_id)
        if application is None:
            raise NotFoundError(f"Application {application_id} not found")

        rubric = await self._rubrics.get_approved_for_job(application.job_id)
        if rubric is None:
            logger.info(f"No approved rubric for job {application.job_id}; queuing {application_id}")
            application.status = ApplicationStatus.SCORING_PENDING
            return await self._applications.save(application)

        try:
            score = await self._matching.compute_match(rubric, application.parsed_resume)
        except Exception as e:
            logger.error(f"Scoring failed for {application_id}, marking pending: {e}")
            application.status = ApplicationStatus.SCORING_PENDING
            return await self._applications.save(application)

        application.matching_score = score
        application.status = (
            ApplicationStatus.NEEDS_HUMAN_REVIEW if score.needs_human_review
            else ApplicationStatus.SUBMITTED
        )
        return await self._applications.save(application)
```

Update `services/matching_service.py`'s `compute_match` signature from `(job_description, parsed_resume)` to `(job_rubric, parsed_resume)` and pass `job_rubric=` through to the agent. Remove any code reading `shortlist_recommendation` in `services/application_service.py` and `orchestration/graph.py` — that field no longer exists.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_matching_failure_states.py -v && pytest tests/test_backend_jobs_candidates_applications.py -v`
Expected: new tests PASS. Existing tests asserting auto-shortlisting must be updated; auto-decision is the behavior being removed.

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add schemas/application.py services/application_service.py services/matching_service.py tests/test_matching_failure_states.py tests/conftest.py
git commit -m "feat(applications): add pending and review states; stop auto-deciding"
```

---

### Task 10: Rubric persistence

**Files:**
- Modify: `repositories/interfaces.py`
- Modify: `repositories/memory.py`
- Modify: `repositories/postgres/schema.sql`
- Test: `tests/test_rubric_repository.py`

**Interfaces:**
- Consumes: `JobRubric`, `RubricStatus` (Task 1)
- Produces: `RubricRepository` ABC with `save(rubric)`, `get(rubric_id)`, `get_approved_for_job(job_id)`, `list_versions_for_job(job_id)`, `approve(rubric_id)`; plus `InMemoryRubricRepository`.

`approve` supersedes any previously approved rubric for the job, so exactly one approved rubric exists per job at any time — the invariant that keeps a leaderboard from mixing rubric versions.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from repositories.memory import InMemoryRubricRepository
from schemas.rubric import AnchoredCompetency, JobRubric, RubricStatus


def _rubric(rubric_id, version, status=RubricStatus.DRAFT):
    anchors = {1: "a", 2: "b", 3: "c", 4: "d", 5: "e"}
    return JobRubric(
        rubric_id=rubric_id, job_id="job_1", version=version, status=status,
        competencies=[
            AnchoredCompetency(name="A", definition="d", weight=0.4, anchors=anchors),
            AnchoredCompetency(name="B", definition="d", weight=0.3, anchors=anchors),
            AnchoredCompetency(name="C", definition="d", weight=0.3, anchors=anchors),
        ],
    )


@pytest.mark.asyncio
async def test_draft_is_not_returned_as_approved():
    repo = InMemoryRubricRepository()
    await repo.save(_rubric("rub_1", 1))
    assert await repo.get_approved_for_job("job_1") is None


@pytest.mark.asyncio
async def test_approve_makes_it_the_active_rubric():
    repo = InMemoryRubricRepository()
    await repo.save(_rubric("rub_1", 1))
    await repo.approve("rub_1")
    active = await repo.get_approved_for_job("job_1")
    assert active.rubric_id == "rub_1"


@pytest.mark.asyncio
async def test_approving_v2_supersedes_v1():
    repo = InMemoryRubricRepository()
    await repo.save(_rubric("rub_1", 1))
    await repo.approve("rub_1")
    await repo.save(_rubric("rub_2", 2))
    await repo.approve("rub_2")
    assert (await repo.get_approved_for_job("job_1")).rubric_id == "rub_2"
    assert (await repo.get("rub_1")).status is RubricStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_exactly_one_approved_rubric_per_job():
    repo = InMemoryRubricRepository()
    for i in (1, 2, 3):
        await repo.save(_rubric(f"rub_{i}", i))
        await repo.approve(f"rub_{i}")
    versions = await repo.list_versions_for_job("job_1")
    approved = [r for r in versions if r.status is RubricStatus.APPROVED]
    assert len(approved) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rubric_repository.py -v`
Expected: FAIL with `ImportError: cannot import name 'InMemoryRubricRepository'`

- [ ] **Step 3: Write minimal implementation**

Append to `repositories/interfaces.py`:

```python
class RubricRepository(ABC):
    """Storage for versioned job rubrics.

    At most one APPROVED rubric exists per job at any time; approving a new
    version supersedes the old one. That invariant is what keeps a
    leaderboard from mixing rows scored under different rubrics, which would
    make ranks incomparable.
    """

    @abstractmethod
    async def save(self, rubric: "JobRubric") -> "JobRubric": ...

    @abstractmethod
    async def get(self, rubric_id: str) -> Optional["JobRubric"]: ...

    @abstractmethod
    async def get_approved_for_job(self, job_id: str) -> Optional["JobRubric"]: ...

    @abstractmethod
    async def list_versions_for_job(self, job_id: str) -> list["JobRubric"]: ...

    @abstractmethod
    async def approve(self, rubric_id: str) -> "JobRubric": ...
```

Append to `repositories/memory.py`:

```python
class InMemoryRubricRepository(RubricRepository):
    """In-memory rubric storage for tests and local runs."""

    def __init__(self):
        self._rubrics: dict[str, JobRubric] = {}

    async def save(self, rubric: JobRubric) -> JobRubric:
        self._rubrics[rubric.rubric_id] = rubric
        return rubric

    async def get(self, rubric_id: str) -> Optional[JobRubric]:
        return self._rubrics.get(rubric_id)

    async def get_approved_for_job(self, job_id: str) -> Optional[JobRubric]:
        for rubric in self._rubrics.values():
            if rubric.job_id == job_id and rubric.status is RubricStatus.APPROVED:
                return rubric
        return None

    async def list_versions_for_job(self, job_id: str) -> list[JobRubric]:
        return sorted(
            (r for r in self._rubrics.values() if r.job_id == job_id),
            key=lambda r: r.version,
        )

    async def approve(self, rubric_id: str) -> JobRubric:
        rubric = self._rubrics[rubric_id]
        violations = rubric.validate_approvable()
        if violations:
            raise ValueError(f"Rubric {rubric_id} is not approvable: {violations}")
        for other in self._rubrics.values():
            if other.job_id == rubric.job_id and other.status is RubricStatus.APPROVED:
                self._rubrics[other.rubric_id] = other.model_copy(
                    update={"status": RubricStatus.SUPERSEDED}
                )
        approved = rubric.model_copy(update={"status": RubricStatus.APPROVED})
        self._rubrics[rubric_id] = approved
        return approved
```

Append to `repositories/postgres/schema.sql`:

```sql
-- Versioned hiring rubrics. At most one APPROVED row per job_id, enforced
-- by the partial unique index below: a leaderboard whose rows were scored
-- under different rubric versions has incomparable ranks.
CREATE TABLE IF NOT EXISTS job_rubrics (
    rubric_id    TEXT PRIMARY KEY,
    job_id       TEXT NOT NULL,
    version      INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('draft', 'approved', 'superseded')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at  TIMESTAMPTZ,
    approved_by  TEXT,
    UNIQUE (job_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_approved_rubric_per_job
    ON job_rubrics (job_id) WHERE status = 'approved';

CREATE TABLE IF NOT EXISTS rubric_competencies (
    competency_id TEXT PRIMARY KEY,
    rubric_id     TEXT NOT NULL REFERENCES job_rubrics(rubric_id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    definition    TEXT NOT NULL,
    weight        DOUBLE PRECISION NOT NULL CHECK (weight >= 0 AND weight <= 1),
    anchor_1      TEXT NOT NULL,
    anchor_2      TEXT NOT NULL,
    anchor_3      TEXT NOT NULL,
    anchor_4      TEXT NOT NULL,
    anchor_5      TEXT NOT NULL,
    UNIQUE (rubric_id, name)
);

-- Verbatim citable resume fragments. Retained because leaderboard rows
-- render the quoted text behind every score; a citation whose span was
-- discarded is unauditable.
CREATE TABLE IF NOT EXISTS resume_spans (
    span_id        TEXT NOT NULL,
    application_id TEXT NOT NULL,
    span_type      TEXT NOT NULL,
    text           TEXT NOT NULL,
    PRIMARY KEY (application_id, span_id)
);

CREATE TABLE IF NOT EXISTS competency_scores (
    score_id             TEXT PRIMARY KEY,
    application_id       TEXT NOT NULL,
    rubric_version       INTEGER NOT NULL,
    competency_name      TEXT NOT NULL,
    score                INTEGER CHECK (score BETWEEN 1 AND 5),
    evidence_sufficiency TEXT NOT NULL
        CHECK (evidence_sufficiency IN ('sufficient', 'partial', 'insufficient')),
    rationale            TEXT NOT NULL,
    UNIQUE (application_id, rubric_version, competency_name)
);

CREATE TABLE IF NOT EXISTS competency_score_citations (
    score_id       TEXT NOT NULL REFERENCES competency_scores(score_id) ON DELETE CASCADE,
    application_id TEXT NOT NULL,
    span_id        TEXT NOT NULL,
    PRIMARY KEY (score_id, span_id),
    FOREIGN KEY (application_id, span_id) REFERENCES resume_spans(application_id, span_id)
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rubric_repository.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add repositories/interfaces.py repositories/memory.py repositories/postgres/schema.sql tests/test_rubric_repository.py
git commit -m "feat(persistence): add versioned rubric storage and citation tables"
```

---

### Task 11: Rubric and leaderboard endpoints

**Files:**
- Create: `api/routes/rubrics.py`
- Modify: `api/routes/jobs.py`
- Modify: `api/app.py` (register the rubrics router)
- Test: `tests/test_rubric_api.py`

**Interfaces:**
- Consumes: `RubricRepository` (Task 10), `RubricGeneratorAgent` (Task 7), `ApplicationService.score_application` (Task 9)
- Produces: `POST /jobs/{job_id}/rubric/draft`, `GET /jobs/{job_id}/rubric`, `PUT /jobs/{job_id}/rubric/{rubric_id}`, `POST /jobs/{job_id}/rubric/{rubric_id}/approve`, `GET /jobs/{job_id}/leaderboard`, `POST /applications/{application_id}/advance-to-interview`.

`GET /jobs/{job_id}/shortlist` in `api/routes/jobs.py` is replaced by the leaderboard endpoint; the matcher no longer produces a shortlist.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from fastapi.testclient import TestClient

from api.app import app

client = TestClient(app)


def test_approving_a_malformed_rubric_returns_422_with_all_violations(auth_headers, job_id):
    bad = {"competencies": [
        {"name": "A", "definition": "d", "weight": 0.5,
         "anchors": {"1": "a", "2": "b", "3": "c", "4": "d", "5": "e"}},
        {"name": "A", "definition": "d", "weight": 0.2,
         "anchors": {"1": "a", "2": "b", "3": "c", "4": "d", "5": "e"}},
    ]}
    draft = client.post(f"/jobs/{job_id}/rubric/draft", json=bad, headers=auth_headers).json()
    response = client.post(
        f"/jobs/{job_id}/rubric/{draft['rubric_id']}/approve", headers=auth_headers
    )
    assert response.status_code == 422
    body = response.json()["detail"]
    assert any("duplicate" in v.lower() for v in body)
    assert any("sum" in v.lower() for v in body)


def test_leaderboard_is_empty_until_a_rubric_is_approved(auth_headers, job_id):
    response = client.get(f"/jobs/{job_id}/leaderboard", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["rows"] == []


def test_leaderboard_row_exposes_citations(auth_headers, scored_job_id):
    response = client.get(f"/jobs/{scored_job_id}/leaderboard", headers=auth_headers)
    row = response.json()["rows"][0]
    assert "coverage" in row and "band" in row
    verdict = row["competency_verdicts"][0]
    assert "cited_spans" in verdict
    assert verdict["cited_spans"][0]["text"]


def test_advancing_to_interview_is_an_explicit_recruiter_action(auth_headers, scored_job_id):
    leaderboard = client.get(f"/jobs/{scored_job_id}/leaderboard", headers=auth_headers).json()
    app_id = leaderboard["rows"][0]["application_id"]
    assert leaderboard["rows"][0]["status"] != "interview_linked"
    response = client.post(
        f"/applications/{app_id}/advance-to-interview", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "interview_linked"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rubric_api.py -v`
Expected: FAIL with 404s — the routes do not exist.

- [ ] **Step 3: Write the implementation**

Create `api/routes/rubrics.py` with the five rubric/leaderboard routes. The approve route is the gate:

```python
@router.post("/jobs/{job_id}/rubric/{rubric_id}/approve")
async def approve_rubric(job_id: str, rubric_id: str, rubrics=Depends(get_rubric_repository)):
    """Approve a draft rubric, making the job scorable.

    Returns every violation at once rather than the first, so a recruiter
    fixes the rubric in one pass instead of one round-trip per problem. A
    malformed rubric is rejected here, not warned about, because it would
    silently corrupt every ranking on the job.
    """
    rubric = await rubrics.get(rubric_id)
    if rubric is None or rubric.job_id != job_id:
        raise HTTPException(status_code=404, detail="Rubric not found for this job")

    violations = rubric.validate_approvable()
    if violations:
        raise HTTPException(status_code=422, detail=violations)

    approved = await rubrics.approve(rubric_id)
    # Approving a new version invalidates existing scores: enqueue a re-score
    # so the leaderboard never mixes rubric versions.
    await enqueue_rescore(job_id, approved.version)
    return approved
```

The leaderboard route ranks by `match_score` descending, excludes `SCORING_PENDING` and `NEEDS_HUMAN_REVIEW` rows from the ranked list while returning them in a separate `needs_review` array, and joins `competency_score_citations` to `resume_spans` so each verdict carries its quoted text.

The advance route sets `ApplicationStatus.INTERVIEW_LINKED` and creates the interview session — the only path by which an interview is ever created from matching.

In `api/routes/jobs.py`, delete the `GET /jobs/{job_id}/shortlist` handler and the `POST /jobs/{job_id}/match` handler's shortlist logic. Register the new router in `api/app.py` alongside the existing routers.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rubric_api.py -v && pytest tests/ -v`
Expected: new tests PASS and the full suite is green.

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add api/routes/rubrics.py api/routes/jobs.py api/app.py tests/test_rubric_api.py
git commit -m "feat(api): add rubric approval and evidence-bearing leaderboard endpoints"
```

---

### Task 12: Adversarial and regression suite

**Files:**
- Modify: `evaluation/cases/resume_matcher.json`
- Create: `tests/test_matching_adversarial.py`

**Interfaces:**
- Consumes: everything above
- Produces: no new symbols; regression coverage for the spec's stated guarantees.

The existing golden cases in `evaluation/cases/resume_matcher.json` assert on `output.shortlist_recommendation` and `output.missing_required_skills`, neither of which exists any more. They must be rewritten against `match_score`, `coverage`, `band`, and `needs_human_review`.

- [ ] **Step 1: Write the failing test**

```python
"""Regression tests for the guarantees the matcher exists to provide."""
import pytest

from services.competency_verifier import EvidenceSufficiency


@pytest.mark.asyncio
async def test_keyword_stuffing_does_not_beat_substance(matcher, rubric, stuffed_resume, real_resume):
    """The headline regression. A resume listing every buzzword with no
    supporting work history must not out-rank one with demonstrated work."""
    stuffed = await matcher.execute(job_rubric=rubric, parsed_resume=stuffed_resume)
    real = await matcher.execute(job_rubric=rubric, parsed_resume=real_resume)
    assert real["matching_score"].match_score > stuffed["matching_score"].match_score


@pytest.mark.asyncio
async def test_every_score_is_traceable_to_quoted_text(matcher, rubric, real_resume):
    result = await matcher.execute(job_rubric=rubric, parsed_resume=real_resume)
    spans = {s.span_id for s in result["matching_score"].competency_verdicts[0].cited_span_ids}
    for verdict in result["matching_score"].competency_verdicts:
        if verdict.evidence_sufficiency is not EvidenceSufficiency.INSUFFICIENT:
            assert verdict.cited_span_ids, f"{verdict.competency_name} scored without citation"


@pytest.mark.asyncio
async def test_sparse_resume_is_unknown_not_rejected(matcher, rubric, sparse_resume):
    result = await matcher.execute(job_rubric=rubric, parsed_resume=sparse_resume)
    assert result["needs_human_review"] is True
    assert result["matching_score"].match_score is None


@pytest.mark.asyncio
async def test_same_resume_and_rubric_yield_a_stable_band(matcher, rubric, real_resume):
    """Non-determinism poisons a leaderboard: a candidate must not drift
    between bands across scoring runs."""
    bands = set()
    for _ in range(3):
        result = await matcher.execute(job_rubric=rubric, parsed_resume=real_resume)
        bands.add(result["matching_score"].band)
    assert len(bands) == 1


@pytest.mark.asyncio
async def test_weights_change_the_ranking(matcher, real_resume):
    """A rubric weighting Kubernetes heavily must rank a Kubernetes-strong
    candidate above one weighting it lightly - otherwise weights are decorative."""
    from tests.conftest import rubric_weighted
    heavy = await matcher.execute(
        job_rubric=rubric_weighted({"Kubernetes operations": 0.9, "Python engineering": 0.1}),
        parsed_resume=real_resume,
    )
    light = await matcher.execute(
        job_rubric=rubric_weighted({"Kubernetes operations": 0.1, "Python engineering": 0.9}),
        parsed_resume=real_resume,
    )
    assert heavy["matching_score"].match_score != light["matching_score"].match_score
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_matching_adversarial.py -v`
Expected: FAIL on missing fixtures (`matcher`, `rubric`, `stuffed_resume`, `real_resume`, `sparse_resume`, `rubric_weighted`).

- [ ] **Step 3: Add the fixtures and rewrite the golden cases**

Add the fixtures to `tests/conftest.py`, reusing the stub matcher and stub LLM from Task 8's test file (extract them into `tests/stubs.py` so both files share one definition rather than duplicating).

Rewrite each case in `evaluation/cases/resume_matcher.json` to supply a `rubric` instead of `required_skills`, and to check `output.match_score`, `output.coverage`, `output.band`, and `output.needs_human_review`. Add a new case:

```json
{
  "case_id": "matcher_keyword_stuffed_resume",
  "agent": "resume_matcher",
  "description": "Resume lists every required keyword but has no work history demonstrating any of them.",
  "input": {
    "rubric": {"rubric_id": "rub_adv", "job_id": "job_adv", "version": 1, "status": "approved",
      "competencies": [
        {"name": "Kubernetes operations", "definition": "Runs production clusters", "weight": 0.6,
         "anchors": {"1": "no evidence", "2": "listed only", "3": "applied it", "4": "led work", "5": "expert"}},
        {"name": "Python engineering", "definition": "Ships production Python", "weight": 0.4,
         "anchors": {"1": "no evidence", "2": "listed only", "3": "applied it", "4": "led work", "5": "expert"}}
      ]},
    "resume": {"candidate_id": "cand_adv", "candidate_name": "Keyword Stuffer",
      "skills": ["Python", "Kubernetes", "Go", "Rust", "Terraform", "Kafka", "gRPC"],
      "work_experience": [], "total_experience_years": 1}
  },
  "expected_behavior": "Bare skill mentions cannot support level-3+ anchors, so the score stays low or coverage stays thin. Keyword stuffing must not produce a strong band.",
  "checks": [
    {"metric": "no_error"},
    {"metric": "field_not_equals", "field": "output.band", "value": "strong"}
  ],
  "metadata": {"category": "adversarial"}
}
```

- [ ] **Step 4: Run the full suite**

Run: `pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 5: Commit (local only, no push)**

```bash
git add tests/test_matching_adversarial.py tests/conftest.py tests/stubs.py evaluation/cases/resume_matcher.json
git commit -m "test(matching): add adversarial and stability regression suite"
```

---

## Self-Review

**Spec coverage.** Stage 0 → Tasks 1, 7, 10, 11. Stage 1 → Tasks 2, 3. Stage 2 → Task 4, integrated in 8. Stage 3 → Tasks 5, 8. Decision boundary → Tasks 6, 9, 11. Error handling → Tasks 3, 9. Data model → Task 10. Testing → Task 12, plus per-task tests.

**Two spec corrections made here:** (1) the spec called for Alembic migrations; no Alembic tree exists, so Task 10 appends DDL to `repositories/postgres/schema.sql` following the real pattern. (2) The spec did not note that removing `shortlist_recommendation` breaks existing readers in `services/application_service.py` and `orchestration/graph.py`; Task 9 handles both.

**Known scope risks.** Task 11 depends on DI wiring (`core/container.py`) not inspected while writing this plan; the implementer should read it before adding `get_rubric_repository`. Task 6's `build_pipeline` export name is unconfirmed — `orchestration/graph.py` holds a `_pipeline` singleton, so the factory name must be checked. The `enqueue_rescore` background mechanism in Task 11 has no queue infrastructure identified yet; if none exists, implement it synchronously first and note the follow-up.

**Type consistency.** `CompetencyVerdict` (Task 4) is used unchanged in Tasks 5, 8, 12. `AnchoredCompetency`/`JobRubric` (Task 1) flow through 7, 8, 10. `ResumeSpan` (Task 2) flows through 3, 8. `EmbeddingUnavailableError` (Task 3) is caught in Task 9. `aggregate()` returns `AggregateResult` consumed only in Task 8.
