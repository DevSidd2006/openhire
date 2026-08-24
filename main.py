"""
Main entry point for the OpenHire hiring evaluation pipeline.
Demonstrates the complete flow from job description through leaderboard generation.
"""
import asyncio
import json
import uuid
from pathlib import Path
from typing import List

from config.settings import OUTPUTS_DIR
from utils.logging import setup_logging
from agents import JDAnalyzerAgent, ResumeParserAgent, InterviewerAgent
from orchestration.graph import get_pipeline, PipelineState
from schemas.job import JobDescription
from schemas.resume import ParsedResume
from schemas.interview import InterviewTranscript, InterviewQuestion, InterviewAnswer
from data import (
    load_job_description,
    load_all_resumes,
    load_interview_transcript,
)


logger = setup_logging(__name__)


def build_job_from_dict(job_dict: dict) -> JobDescription:
    """Convert loaded job dict to JobDescription schema."""
    from schemas.job import Competency
    
    competencies = [
        Competency(
            name=c["name"],
            weight=c.get("weight", 1.0 / len(job_dict["competencies"])),
            importance="high",
            description=""
        )
        for c in job_dict.get("competencies", [])
    ]
    
    return JobDescription(
        job_id=job_dict.get("job_id", f"job_{uuid.uuid4().hex[:8]}"),
        title=job_dict.get("title", "Unknown Position"),
        description=job_dict.get("description", ""),
        experience_years=job_dict.get("experience_years", 3),
        required_skills=job_dict.get("requirements", []),
        preferred_skills=job_dict.get("preferred_skills", []),
        competencies=competencies,
        raw_text=json.dumps(job_dict),
    )


def build_resume_from_dict(resume_dict: dict) -> ParsedResume:
    """Convert loaded resume dict to ParsedResume schema."""
    from datetime import datetime
    from schemas.resume import WorkExperience, Education

    current_year = datetime.now().year

    work_exp = [
        WorkExperience(
            position=w.get("position", ""),
            company=w.get("company", ""),
            start_year=current_year - int(w.get("duration_years", 0)),
            duration_months=w.get("duration_years", 0) * 12 + w.get("duration_months", 0),
            description=w.get("description", ""),
            achievements=w.get("achievements", []),
        )
        for w in resume_dict.get("work_experience", [])
    ]

    education = [
        Education(
            degree=e.get("degree", ""),
            field_of_study=e.get("field", ""),
            institution=e.get("institution", ""),
            graduation_year=e.get("graduation_year", 0),
        )
        for e in resume_dict.get("education", [])
    ]
    
    return ParsedResume(
        candidate_id=resume_dict.get("candidate_id", f"cand_{uuid.uuid4().hex[:8]}"),
        candidate_name=resume_dict.get("candidate_name", "Unknown"),
        email=resume_dict.get("email", ""),
        phone=resume_dict.get("phone", ""),
        education=education,
        work_experience=work_exp,
        projects=resume_dict.get("projects", []),
        skills=resume_dict.get("skills", []),
        raw_text=json.dumps(resume_dict),
        total_experience_years=resume_dict.get("total_experience_years", 0),
    )


def build_transcript_from_dict(transcript_dict: dict) -> InterviewTranscript:
    """Convert loaded transcript dict to InterviewTranscript schema."""
    exchanges = []
    for ex in transcript_dict.get("exchanges", []):
        question_data = ex.get("question", {})
        answer_data = ex.get("answer", {})
        
        question = InterviewQuestion(
            question_id=question_data.get("question_id", f"q_{uuid.uuid4().hex[:8]}"),
            question_text=question_data.get("question_text", ""),
            category=question_data.get("category", "general"),
            difficulty=question_data.get("difficulty", "medium"),
            competency=question_data.get("competency", "general"),
            reason="",
            expected_duration_seconds=question_data.get("duration_seconds", 60),
        )
        
        answer = InterviewAnswer(
            question_id=question.question_id,
            answer_text=answer_data.get("answer_text", ""),
            duration_seconds=answer_data.get("duration_seconds", 60),
        )

        exchanges.append((question, answer))

    return InterviewTranscript(
        interview_id=transcript_dict.get("interview_id", f"int_{uuid.uuid4().hex[:8]}"),
        candidate_id=transcript_dict.get("candidate_id", ""),
        job_id=transcript_dict.get("job_id", ""),
        exchanges=exchanges,
        start_time=transcript_dict.get("created_at", ""),
        is_sealed=True,
    )


def _serialize(obj):
    """Recursively convert pydantic models (and containers of them) to JSON-safe values."""
    if hasattr(obj, "model_dump"):
        return _serialize(obj.model_dump())
    if isinstance(obj, dict):
        return {str(k): _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    return obj


async def main():
    """Run the complete hiring evaluation pipeline."""
    logger.info("=" * 80)
    logger.info("OpenHire Hiring Evaluation Pipeline - Demo Run")
    logger.info("=" * 80)

    try:
        # Load sample data
        logger.info("\n📋 Loading sample data...")
        job_dict = load_job_description()
        resumes_dicts = load_all_resumes()
        transcript_dict = load_interview_transcript()

        # Build schema objects
        logger.info("🔨 Building schema objects...")
        job_description = build_job_from_dict(job_dict)
        parsed_resumes = [build_resume_from_dict(r) for r in resumes_dicts]
        base_transcript = build_transcript_from_dict(transcript_dict)

        logger.info(f"✓ Job: {job_description.title}")
        logger.info(f"✓ Candidates: {len(parsed_resumes)}")
        logger.info(f"✓ Interview exchanges: {len(base_transcript.exchanges)}")

        # Sample data only ships one interview transcript fixture. The graph
        # assigns candidate ids "cand_001".."cand_NNN" (1-indexed, matching
        # sample_resume_N.json) to every parsed resume, so to exercise the
        # full pipeline - and real cross-candidate concurrency - across all
        # sample candidates, the same transcript content is reused per
        # candidate with the candidate_id/interview_id re-stamped. This is a
        # demo-harness decision, not a claim that these are distinct real
        # interviews.
        interview_transcripts = {}
        for i in range(len(parsed_resumes)):
            candidate_id = f"cand_{i + 1:03d}"
            interview_transcripts[candidate_id] = base_transcript.model_copy(
                update={
                    "candidate_id": candidate_id,
                    "interview_id": f"int_{candidate_id}",
                }
            )

        # Create pipeline state
        logger.info("\n🔄 Initializing pipeline...")
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        state = PipelineState(
            job_description_text=job_description.description,
            candidates_resume_texts=[r.raw_text for r in parsed_resumes],
            interview_transcripts=interview_transcripts,
            job_description=None,
            parsed_resumes={},
            matching_scores={},
            shortlisted_candidates=[],
            interview_questions={},
            technical_evaluations={},
            behavioral_evaluations={},
            resume_audits={},
            integrity_evaluations={},
            bias_audits={},
            candidate_scores={},
            candidate_reports={},
            leaderboard=None,
            run_id=run_id,
            errors=[],
            audit_logs=[],
        )

        # Get compiled pipeline
        logger.info("📊 Loading orchestration graph...")
        pipeline = get_pipeline()

        # Run pipeline
        logger.info("\n🚀 Starting pipeline execution...\n")
        result = await pipeline.ainvoke(state)

        logger.info("\n✅ Pipeline execution complete")
        logger.info(f"   Run ID: {run_id}")
        logger.info(f"   Job ID: {job_description.job_id}")
        logger.info(f"   Candidates: {len(parsed_resumes)}")
        logger.info(f"   Audit log entries: {len(result.get('audit_logs', []))}")
        if result.get("errors"):
            logger.warning(f"   Errors reported during run: {result['errors']}")

        # Save outputs
        logger.info("\n💾 Saving outputs...")
        output_dir = OUTPUTS_DIR
        output_dir.mkdir(exist_ok=True)

        # Save the full resulting pipeline state (real evaluation/report/leaderboard data)
        state_file = output_dir / f"run_{run_id}_state.json"
        with open(state_file, 'w') as f:
            json.dump(_serialize(dict(result)), f, indent=2, default=str)
        logger.info(f"✓ Full pipeline state saved to: {state_file}")

        # Save the leaderboard separately for convenience
        if result.get("leaderboard"):
            leaderboard_file = output_dir / f"run_{run_id}_leaderboard.json"
            with open(leaderboard_file, 'w') as f:
                json.dump(_serialize(result["leaderboard"]), f, indent=2, default=str)
            logger.info(f"✓ Leaderboard saved to: {leaderboard_file}")

        # Save sample inputs
        job_file = output_dir / f"sample_job_{job_description.job_id}.json"
        with open(job_file, 'w') as f:
            json.dump(job_dict, f, indent=2)
        logger.info(f"✓ Job description saved to: {job_file}")

        for i, resume in enumerate(parsed_resumes):
            resume_file = output_dir / f"sample_resume_{resume.candidate_id}.json"
            with open(resume_file, 'w') as f:
                json.dump(resumes_dicts[i], f, indent=2)
            logger.info(f"✓ Resume saved to: {resume_file}")

        # Log summary
        logger.info("\n" + "=" * 80)
        logger.info("PIPELINE EXECUTION SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Run ID: {run_id}")
        logger.info(f"Job: {job_description.title} ({job_description.job_id})")
        logger.info(f"Required Experience: {job_description.experience_years}+ years")
        logger.info(f"Competencies: {len(job_description.competencies)}")
        logger.info(f"Candidates: {len(parsed_resumes)}")
        logger.info(f"Shortlisted: {result.get('shortlisted_candidates', [])}")
        logger.info(f"Scored candidates: {list(result.get('candidate_scores', {}).keys())}")
        if result.get("leaderboard"):
            for entry in result["leaderboard"].entries:
                logger.info(
                    f"  #{entry.rank} {entry.candidate_name}: "
                    f"{entry.weighted_score:.1f}/10 ({entry.recommendation})"
                )
        logger.info(f"Output Directory: {output_dir}")
        logger.info("=" * 80)

    except Exception as e:
        logger.error(f"Pipeline failed: {str(e)}", exc_info=True)
        return 1

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
