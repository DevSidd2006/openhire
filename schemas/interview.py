"""
Interview related schemas.
"""
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from config.settings import MAX_QUESTIONS_PER_INTERVIEW

# The intent behind a question - distinct from InterviewQuestion.category
# (which has always meant a coarse subject-matter bucket: "technical",
# "behavioral", "role_specific", or the pre-P3 ad-hoc "follow_up"). P3's
# adaptive engine needs to distinguish WHY a question is being asked
# (opening a new topic vs. following up vs. probing a specific gap vs.
# asking for clarification vs. deepening) independently of subject matter,
# so this is a new, separate vocabulary rather than overloading `category`
# further - existing consumers that already treat `category` as a subject
# label are unaffected.
QuestionType = Literal[
    "initial", "follow_up", "probe", "clarification", "depth",
    "behavioral", "technical", "role_specific", "introduction",
]


class InterviewQuestion(BaseModel):
    """Interview question."""
    question_id: str
    question_text: str
    category: str  # "technical", "behavioral", "role_specific", "follow_up"
    competency: Optional[str] = None
    difficulty: Optional[str] = None  # "easy", "medium", "hard"
    reason: Optional[str] = None  # Why this question was asked
    expected_duration_seconds: Optional[int] = None
    # P3: set only by the adaptive engine (agents/interviewer's
    # generate_next_question); None for questions from the pre-P3 batch
    # planner, which has no concept of adaptive intent.
    question_type: Optional[QuestionType] = None


class InterviewAnswer(BaseModel):
    """Candidate's answer to a question."""
    question_id: str
    answer_text: str
    duration_seconds: Optional[float] = None
    timestamp_start: Optional[float] = None  # In seconds from start
    timestamp_end: Optional[float] = None
    answer_quality: Optional[str] = None  # filled by evaluators


class InterviewTranscript(BaseModel):
    """Interview transcript."""
    interview_id: str
    candidate_id: str
    job_id: str
    start_time: str
    end_time: Optional[str] = None
    duration_seconds: Optional[int] = None
    
    # Question-answer pairs
    exchanges: List[tuple[InterviewQuestion, InterviewAnswer]] = Field(default_factory=list)
    
    # Interview metadata
    interviewer_name: Optional[str] = None
    interview_type: Optional[str] = None  # "initial", "technical", "behavioral", "final"
    format: Optional[str] = None  # "voice", "video", "text"
    
    # Sealed status
    is_sealed: bool = False
    seal_timestamp: Optional[str] = None
    
    # Raw transcript text
    raw_transcript: Optional[str] = None


class InterviewEvaluation(BaseModel):
    """Placeholder for interview evaluation context."""
    interview_id: str
    candidate_id: str
    job_id: str
    transcript: InterviewTranscript


# What the deterministic next-question decision may direct the engine to do
# (P3 Phase 4). FINISH is deliberately included here (not modeled as a
# separate bool) so "should we stop" and "what should we ask" go through the
# exact same validated decision object - there is no second, less-guarded
# path by which an interview can end.
QuestionAction = Literal["ask_new", "follow_up", "probe", "clarify", "finish"]


class CompetencySignal(BaseModel):
    """The most recent answer-evaluation signal recorded for one competency -
    used only to pick the next action (e.g. a vague answer -> follow_up, a
    named missing detail -> probe on that detail). Deliberately NOT a
    running history: only the latest signal per competency is kept, since
    that is all the prioritization logic in utils/adaptive_interview.py
    reads (P3 Phase 7)."""
    is_vague: bool = False
    missing_detail: Optional[str] = None


class NextQuestionDecision(BaseModel):
    """A structured, system-controlled decision about what to do next in an
    adaptive interview (P3 Phase 4). Produced by the deterministic
    prioritization logic in utils/adaptive_interview.py (never by an LLM -
    see that module's docstring for why), and is the ONLY input the question-
    generation LLM call and the state-transition functions are allowed to
    act on. An LLM may propose question *phrasing* (AdaptiveQuestionResult),
    never the action/target_competency/termination_reason themselves."""
    action: QuestionAction
    target_competency: Optional[str] = None  # None only when action == "finish"
    reason: str  # human-readable justification, for logging/prompting/audit
    difficulty: Optional[str] = None  # "easy", "medium", "hard" - hint for ask_new/depth
    expected_evidence: Optional[str] = None  # what a probe/follow_up should try to elicit
    termination_reason: Optional[str] = None  # set only when action == "finish"


class InterviewState(BaseModel):
    """State of an ongoing interview.

    P3 extends this with the fields the adaptive engine actually reads/
    writes (utils/adaptive_interview.py); see that module's docstring for
    the state-transition rules. Fields that can be derived from existing
    data are deliberately NOT duplicated here - e.g. there is no separate
    "remaining_competencies" field, since that is always
    `{c.name for c in job.competencies} - set(covered_competencies)`, and no
    "questions asked per competency" counter, since that is derived from
    `exchanges` on demand (see questions_asked_for_competency).
    """
    interview_id: str
    candidate_id: str
    job_id: str
    current_question_index: int = 0
    questions_asked: int = 0
    questions_answered: int = 0
    exchanges: List[tuple[InterviewQuestion, InterviewAnswer]] = Field(default_factory=list)
    conversation_history: List[str] = Field(default_factory=list)
    start_time: str
    last_activity_time: str
    is_completed: bool = False

    # The question currently awaiting an answer, if any. Kept separate from
    # `exchanges` (which only ever holds answered question/answer pairs) so
    # "a question was asked but not yet answered" is a real, inspectable
    # state rather than an implicit gap.
    current_question: Optional[InterviewQuestion] = None

    # Competencies that have received at least one question so far.
    covered_competencies: List[str] = Field(default_factory=list)

    # Running per-competency estimates, updated by record_answer() after
    # every evaluated answer (utils/adaptive_interview.py). Absent key means
    # "never evaluated yet", not "score 0" - callers must not treat a
    # missing entry as a real zero score.
    competency_scores: Dict[str, float] = Field(default_factory=dict)
    competency_confidence: Dict[str, float] = Field(default_factory=dict)

    # Mirrors CompetencyScore.evidence_status (schemas/evaluation.py) at the
    # per-turn level: "supported" once at least one answer gave concrete,
    # on-topic evidence for the competency; "insufficient" if answers were
    # given but stayed vague/off-topic/ungrounded. No entry means the
    # competency has not been asked about at all yet.
    evidence_coverage: Dict[str, Literal["supported", "insufficient"]] = Field(default_factory=dict)

    # Latest vagueness/missing-detail signal per competency (P3 Phase 7).
    competency_signals: Dict[str, CompetencySignal] = Field(default_factory=dict)

    # How many follow-up/probe/clarify questions have been spent on each
    # competency so far (P3 Phase 8 depth control). Does not count the
    # initial ask_new question for that competency.
    follow_up_counts: Dict[str, int] = Field(default_factory=dict)

    # Deterministic upper bound on total questions for this interview.
    max_questions: int = MAX_QUESTIONS_PER_INTERVIEW

    # Set exactly once, when the interview is deterministically decided to
    # be finished (utils/adaptive_interview.decide_next_action /
    # check_termination) - never inferred after the fact.
    termination_reason: Optional[str] = None
