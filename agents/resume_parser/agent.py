"""
Resume Parser Agent.
Extracts structured information from resumes.

Structured-output architecture (P7): uses call_llm_structured() +
ResumeParseResult (schemas/llm_outputs.py) - the same P1 pattern every
other migrated agent uses (JD Analyzer, Technical/Behavioral Evaluator,
Resume Auditor, Integrity, Bias Checker, Interviewer). No json.loads(), no
JSONDecodeError handling here - that belongs to the provider layer.

Failure policy (P7 Phase 4), explicit and three-tiered:
  1. Structured output valid -> ParsedResume built from it (normal path).
  2. Structured output fails after BaseAgent's bounded retry (malformed
     JSON or schema-invalid output on every attempt -> RuntimeError) or a
     permanent provider error (LLMPermanentError, e.g. bad credentials) ->
     falls back to _fallback_parse(), a deterministic, evidence-preserving
     extractor that only pulls facts (email/phone via regex, skills from a
     fixed known-skill list) verifiably present in the ACTUAL resume text -
     never a fabricated resume. `used_fallback`/`error` are set on the
     returned dict so a caller can tell this happened, rather than
     mistaking a limited fallback result for a full structured parse.
  3. Any other unexpected exception (a genuine bug) -> explicit failure:
     parsed_resume=None, error set - matching every other agent's P0-4
     "never let a failure look like success" contract.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from agents.base import BaseAgent
from providers.base import LLMPermanentError
from schemas.llm_outputs import (
    ResumeCertificationExtract,
    ResumeEducationExtract,
    ResumeParseResult,
    ResumeProjectExtract,
    ResumeWorkExperienceExtract,
)
from schemas.resume import Certification, Education, ParsedResume, Project, WorkExperience


class ResumeParserAgent(BaseAgent):
    """Parses and normalizes resume data."""

    def __init__(self, **kwargs):
        super().__init__(name="resume_parser", **kwargs)

    async def execute(
        self,
        resume_text: str,
        candidate_id: str,
        candidate_name: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Parse and structure resume data.

        Args:
            resume_text: Resume content (text or extracted from PDF)
            candidate_id: Unique candidate identifier
            candidate_name: Candidate's name

        Returns:
            {"parsed_resume": ParsedResume, "skill_count": int,
             "experience_years": Optional[float], "used_fallback": bool,
             "error": str (only present if used_fallback or on failure)}
            parsed_resume is None only on a genuinely unexpected failure -
            never fabricated (see module docstring policy tier 3).
        """
        self.logger.info(f"Parsing resume for candidate: {candidate_id} ({candidate_name})")

        try:
            result, used_fallback, fallback_reason = await self._parse_resume_content(resume_text)
            parsed_resume = self._build_parsed_resume(result, candidate_id, candidate_name, resume_text)
        except Exception as e:
            self.logger.error(f"Resume parsing failed: {str(e)}")
            return {"parsed_resume": None, "error": str(e)}

        response: Dict[str, Any] = {
            "parsed_resume": parsed_resume,
            "skill_count": len(parsed_resume.skills),
            "experience_years": parsed_resume.total_experience_years,
            "used_fallback": used_fallback,
        }
        if used_fallback:
            response["error"] = fallback_reason
        return response

    async def _parse_resume_content(
        self, resume_text: str
    ) -> Tuple[ResumeParseResult, bool, Optional[str]]:
        """Returns (result, used_fallback, fallback_reason)."""
        prompt = self.load_prompt("resume_parser.md").format(resume_text=resume_text)

        try:
            result: ResumeParseResult = await self.call_llm_structured(
                prompt,
                schema=ResumeParseResult.model_json_schema(),
                validate=ResumeParseResult.model_validate,
            )
            return result, False, None
        except (RuntimeError, LLMPermanentError) as exc:
            # RuntimeError: BaseAgent's bounded retry budget exhausted
            # (malformed/schema-invalid output on every attempt).
            # LLMPermanentError: a provider-level failure retrying can't
            # fix (bad credentials, etc.). Both fall back to the
            # deterministic extractor - it can only ever surface facts
            # verifiably present in resume_text, never fabricate, so this
            # is a safe degradation rather than a full failure.
            self.logger.warning(f"Structured resume parsing failed, using deterministic fallback: {exc}")
            return self._fallback_parse(resume_text), True, str(exc)

    def _fallback_parse(self, resume_text: str) -> ResumeParseResult:
        """Deterministic, evidence-preserving fallback (P7 Phase 4 "GOOD"
        path). Regex/keyword extraction over the ACTUAL resume text only -
        never invents education, work history, projects, or certifications
        (always empty here); `summary` is a literal truncation of the real
        text, never a generated one; skills are only ever ones from a fixed
        known-skill list that are actually substrings of the text."""
        return ResumeParseResult(
            email=self._extract_email(resume_text),
            phone=self._extract_phone(resume_text),
            summary=resume_text[:200] if resume_text else None,
            skills=self._extract_skills(resume_text),
        )

    def _extract_email(self, text: str) -> Optional[str]:
        """Extract email from text."""
        import re
        match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
        return match.group(0) if match else None

    def _extract_phone(self, text: str) -> Optional[str]:
        """Extract phone number from text."""
        import re
        match = re.search(r"[\+]?[(]?[0-9]{3}[)]?[-\s\.]?[0-9]{3}[-\s\.]?[0-9]{4,6}", text)
        return match.group(0) if match else None

    def _extract_skills(self, text: str) -> List[str]:
        """Extract common skills mentioned in resume."""
        common_skills = [
            "Python", "Java", "C++", "JavaScript", "SQL", "React", "Angular",
            "Node.js", "Django", "FastAPI", "Docker", "Kubernetes", "AWS",
            "Azure", "Git", "Linux", "Machine Learning", "Data Science",
            "REST APIs", "MongoDB", "PostgreSQL", "Redis", "Agile",
        ]
        text_lower = text.lower()
        found_skills = [skill for skill in common_skills if skill.lower() in text_lower]
        return found_skills[:10]  # Return top 10 found

    # ---------------------------------------------------------------
    # ResumeParseResult -> ParsedResume: deterministic/business validation
    # (P7 Phase 2). Each nested entry is kept only if it clears the DOMAIN
    # model's required fields - a partially-extracted entry is dropped,
    # never completed with a fabricated value.
    # ---------------------------------------------------------------

    def _build_parsed_resume(
        self,
        result: ResumeParseResult,
        candidate_id: str,
        candidate_name: str,
        resume_text: str,
    ) -> ParsedResume:
        return ParsedResume(
            candidate_id=candidate_id,
            candidate_name=candidate_name,
            email=result.email,
            phone=result.phone,
            location=result.location,
            summary=result.summary,
            education=self._build_education(result.education),
            work_experience=self._build_work_experience(result.work_experience),
            projects=self._build_projects(result.projects),
            certifications=self._build_certifications(result.certifications),
            skills=result.skills,
            technologies=result.technologies,
            languages=result.languages,
            total_experience_years=result.total_experience_years,
            raw_text=resume_text,
            parse_date=datetime.now().isoformat(),
        )

    def _build_education(self, entries: List[ResumeEducationExtract]) -> List[Education]:
        built = []
        for e in entries:
            if not (e.institution and e.degree and e.field_of_study):
                self.logger.debug(f"Dropping education entry missing a required field: {e}")
                continue
            built.append(Education(
                institution=e.institution, degree=e.degree, field_of_study=e.field_of_study,
                graduation_year=e.graduation_year, gpa=e.gpa, honors=e.honors,
            ))
        return built

    def _build_work_experience(self, entries: List[ResumeWorkExperienceExtract]) -> List[WorkExperience]:
        built = []
        for w in entries:
            if not (w.company and w.position and w.start_year is not None):
                self.logger.debug(f"Dropping work experience entry missing a required field: {w}")
                continue
            built.append(WorkExperience(
                company=w.company, position=w.position, start_year=w.start_year,
                end_year=w.end_year, is_current=w.is_current, duration_months=w.duration_months,
                description=w.description, responsibilities=w.responsibilities, achievements=w.achievements,
            ))
        return built

    def _build_projects(self, entries: List[ResumeProjectExtract]) -> List[Project]:
        built = []
        for p in entries:
            if not (p.name and p.description):
                self.logger.debug(f"Dropping project entry missing a required field: {p}")
                continue
            built.append(Project(
                name=p.name, description=p.description, technologies=p.technologies,
                url=p.url, role=p.role, outcome=p.outcome,
            ))
        return built

    def _build_certifications(self, entries: List[ResumeCertificationExtract]) -> List[Certification]:
        built = []
        for c in entries:
            if not c.name:
                self.logger.debug(f"Dropping certification entry missing a name: {c}")
                continue
            built.append(Certification(
                name=c.name, issuer=c.issuer, issue_date=c.issue_date,
                expiration_date=c.expiration_date, credential_url=c.credential_url,
            ))
        return built
