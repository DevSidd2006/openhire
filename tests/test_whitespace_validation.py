import pytest
from pydantic import TypeAdapter, ValidationError

from api.models import SubmitAnswerRequest
from api.models_bugs import FileBugReportRequest
from api.models_candidates import RegisterCandidateRequest
from api.models_jobs import CreateJobRequest
from schemas.types import NonEmptyStr

BLANKS = ["", " ", "   ", "\t", "\n", " \t\n "]


@pytest.mark.parametrize("blank", BLANKS)
def test_non_empty_str_rejects_blank(blank):
    with pytest.raises(ValidationError):
        TypeAdapter(NonEmptyStr).validate_python(blank)


def test_non_empty_str_strips_surrounding_whitespace():
    assert TypeAdapter(NonEmptyStr).validate_python("  hi  ") == "hi"


@pytest.mark.parametrize("blank", BLANKS)
def test_bug_report_rejects_blank_title(blank):
    with pytest.raises(ValidationError):
        FileBugReportRequest(title=blank, description="valid description")


@pytest.mark.parametrize("blank", BLANKS)
def test_bug_report_rejects_blank_description(blank):
    with pytest.raises(ValidationError):
        FileBugReportRequest(title="valid title", description=blank)


def test_bug_report_strips_surrounding_whitespace():
    req = FileBugReportRequest(title="  Crash on login  ", description="  details  ")
    assert req.title == "Crash on login"
    assert req.description == "details"


@pytest.mark.parametrize("blank", BLANKS)
def test_register_candidate_rejects_blank_resume_text(blank):
    with pytest.raises(ValidationError):
        RegisterCandidateRequest(resume_text=blank, candidate_name="Jane Doe")


@pytest.mark.parametrize("blank", BLANKS)
def test_register_candidate_rejects_blank_name(blank):
    with pytest.raises(ValidationError):
        RegisterCandidateRequest(resume_text="Some resume", candidate_name=blank)


@pytest.mark.parametrize("blank", BLANKS)
def test_create_job_rejects_blank_description(blank):
    with pytest.raises(ValidationError):
        CreateJobRequest(description=blank)


@pytest.mark.parametrize("blank", BLANKS)
def test_submit_answer_rejects_blank_answer(blank):
    with pytest.raises(ValidationError):
        SubmitAnswerRequest(answer_text=blank)


def test_valid_inputs_still_accepted():
    assert CreateJobRequest(description="Backend engineer").description == "Backend engineer"
    assert SubmitAnswerRequest(answer_text="My answer").answer_text == "My answer"


# ---------------------------------------------------------------------------
# UpdateCandidateRequest (PATCH) and EvidenceItem
# ---------------------------------------------------------------------------
from api.models_candidates import UpdateCandidateRequest  # noqa: E402
from schemas.evaluation import EvidenceItem  # noqa: E402

# Add any other required EvidenceItem fields (e.g. the evidence type) here.
VALID_EVIDENCE = {
    "evidence_id": "ev_test_1",
    "source_type": "transcript",
    "text": "Candidate described the design trade-offs clearly.",
    "relevance": 0.8,
    "agent": "test_agent",
    "explanation": "Directly answers the question.",
}


@pytest.mark.parametrize("blank", BLANKS)
def test_update_candidate_rejects_blank_name(blank):
    with pytest.raises(ValidationError):
        UpdateCandidateRequest(candidate_name=blank)


@pytest.mark.parametrize("blank", BLANKS)
def test_update_candidate_rejects_blank_resume_text(blank):
    with pytest.raises(ValidationError):
        UpdateCandidateRequest(resume_text=blank)


def test_update_candidate_strips_name_and_allows_omission():
    assert UpdateCandidateRequest(candidate_name="  Jane Doe  ").candidate_name == "Jane Doe"
    assert UpdateCandidateRequest().candidate_name is None


@pytest.mark.parametrize("blank", BLANKS)
def test_evidence_item_rejects_blank_text(blank):
    with pytest.raises(ValidationError):
        EvidenceItem(**{**VALID_EVIDENCE, "text": blank})


def test_evidence_item_strips_text():
    item = EvidenceItem(**{**VALID_EVIDENCE, "text": "  solid answer  "})
    assert item.text == "solid answer"
