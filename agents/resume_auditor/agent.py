"""
Resume Auditor Agent.
Verifies resume claims against interview responses.
"""
from typing import Any, Dict, List
import uuid

from agents.base import BaseAgent
from schemas.evaluation import ClaimVerification, ResumeClaim
from schemas.interview import InterviewTranscript
from schemas.llm_outputs import ClaimVerificationResult
from schemas.resume import ParsedResume
from utils.evidence import resolve_transcript_evidence


class ResumeAuditorAgent(BaseAgent):
    """Audits resume claims against interview responses."""

    def __init__(self, **kwargs):
        super().__init__(name="resume_auditor", **kwargs)

    async def execute(
        self,
        parsed_resume: ParsedResume,
        interview_transcript: InterviewTranscript,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Verify resume claims against interview.
        
        Args:
            parsed_resume: Candidate resume
            interview_transcript: Sealed interview transcript
            
        Returns:
            List of ClaimVerification objects
        """
        self.logger.info(f"Auditing resume claims for candidate {parsed_resume.candidate_id}")

        try:
            # Extract major claims from resume
            claims = self._extract_resume_claims(parsed_resume)
            self.logger.info(f"Extracted {len(claims)} claims for verification")

            # Verify each claim
            verifications = []
            for claim in claims:
                verification = await self._verify_claim(claim, interview_transcript, parsed_resume)
                verifications.append(verification)

            return {
                "claim_verifications": verifications,
                "verification_count": len(verifications),
                "supported_count": len([v for v in verifications if v.verification_status == "supported"]),
            }

        except Exception as e:
            self.logger.error(f"Resume audit failed: {str(e)}")
            return {"claim_verifications": [], "error": str(e)}

    async def _verify_claim(
        self,
        claim: ResumeClaim,
        interview_transcript: InterviewTranscript,
        parsed_resume: ParsedResume
    ) -> ClaimVerification:
        """Verify a single resume claim."""

        transcript_text = self._format_transcript(interview_transcript)

        prompt = self.load_prompt("resume_auditor.md")
        prompt = prompt.format(
            resume=f"Claim: {claim.resume_claim}",
            transcript=transcript_text,
        )

        result: ClaimVerificationResult = await self.call_llm_structured(
            prompt,
            schema=ClaimVerificationResult.model_json_schema(),
            validate=ClaimVerificationResult.model_validate,
        )

        status = result.verification_status
        # "inconsistent" means the transcript CONTRADICTS the resume claim;
        # "supported"/"partially_supported" mean it backs the claim up.
        evidence_type = "contradicting" if status == "inconsistent" else "supporting"
        evidence_item = resolve_transcript_evidence(
            transcript=interview_transcript,
            question_number=result.evidence_question_number,
            agent="resume_auditor",
            explanation=result.explanation,
            candidate_id=parsed_resume.candidate_id,
            evidence_type=evidence_type,
        )

        # A verified/contradicted/partial verdict without a real transcript
        # exchange to point to isn't actually grounded - route it to human
        # review rather than asserting a status we can't back up. Only
        # "insufficient_evidence" is a legitimate verdict without evidence,
        # since it means the interview never addressed the claim at all.
        if status in ("supported", "partially_supported", "inconsistent") and not evidence_item:
            status = "requires_human_review"

        verification = ClaimVerification(
            verification_id=f"claim_ver_{uuid.uuid4().hex[:8]}",
            candidate_id=parsed_resume.candidate_id,
            job_id=interview_transcript.job_id,
            interview_id=interview_transcript.interview_id,
            claim=claim,
            verification_status=status,
            confidence=result.confidence,
            evidence=[evidence_item] if evidence_item else [],
            explanation=result.explanation,
            requires_human_review=status in [
                "inconsistent",
                "requires_human_review",
            ],
        )

        return verification

    def _extract_resume_claims(self, resume: ParsedResume) -> List[ResumeClaim]:
        """Extract major claims from resume."""
        claims = []

        # Extract from achievements
        for exp in resume.work_experience:
            if exp.achievements:
                for achievement in exp.achievements[:2]:  # Top 2 per role
                    claims.append(
                        ResumeClaim(
                            claim_id=f"claim_{len(claims)}",
                            resume_claim=achievement,
                            source=f"Achievement in {exp.position}",
                        )
                    )

        # Extract from projects
        for project in resume.projects[:2]:  # Top 2 projects
            if project.outcome:
                claims.append(
                    ResumeClaim(
                        claim_id=f"claim_{len(claims)}",
                        resume_claim=project.outcome,
                        source=f"Project: {project.name}",
                    )
                )

        return claims

    def _format_transcript(self, transcript: InterviewTranscript) -> str:
        """Format transcript for LLM."""
        lines = []
        for i, (question, answer) in enumerate(transcript.exchanges, 1):
            lines.append(f"Q{i}: {question.question_text}")
            lines.append(f"A{i}: {answer.answer_text}\n")
        return "\n".join(lines)
