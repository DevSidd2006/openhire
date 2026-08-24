"""Sample data loading utilities."""
import json
from pathlib import Path
from typing import Dict, Any, List


def load_job_description() -> Dict[str, Any]:
    """Load sample job description."""
    path = Path(__file__).parent / "sample_job.json"
    with open(path) as f:
        return json.load(f)


def load_resume(candidate_number: int) -> Dict[str, Any]:
    """Load sample resume by number (1, 2, or 3)."""
    path = Path(__file__).parent / f"sample_resume_{candidate_number}.json"
    with open(path) as f:
        return json.load(f)


def load_all_resumes() -> List[Dict[str, Any]]:
    """Load all sample resumes."""
    return [load_resume(i) for i in range(1, 4)]


def load_interview_transcript() -> Dict[str, Any]:
    """Load sample interview transcript."""
    path = Path(__file__).parent / "sample_transcript.json"
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":
    # Test loading
    job = load_job_description()
    print(f"Job: {job.get('title')}")
    
    resumes = load_all_resumes()
    print(f"Loaded {len(resumes)} resumes")
    
    transcript = load_interview_transcript()
    print(f"Interview exchanges: {len(transcript.get('exchanges', []))}")
