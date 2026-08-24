"""
Validation utilities.
"""
from typing import List, Optional


def validate_weights(weights: List[float], tolerance: float = 0.01) -> bool:
    """Validate that weights sum to approximately 1.0."""
    if not weights:
        return True
    total = sum(weights)
    return abs(total - 1.0) <= tolerance


def validate_score(score: float, min_score: float = 0.0, max_score: float = 10.0) -> bool:
    """Validate that a score is within expected range."""
    return min_score <= score <= max_score


def validate_confidence(confidence: float) -> bool:
    """Validate that confidence is between 0 and 1."""
    return 0.0 <= confidence <= 1.0


def validate_competencies(competencies: List[dict]) -> tuple[bool, Optional[str]]:
    """Validate competencies structure."""
    if not competencies:
        return True, None

    total_weight = 0.0
    for comp in competencies:
        if "name" not in comp or "weight" not in comp:
            return False, "Each competency must have 'name' and 'weight'"

        if not isinstance(comp["weight"], (int, float)):
            return False, "Competency weight must be numeric"

        if not (0.0 <= comp["weight"] <= 1.0):
            return False, f"Competency weight must be between 0 and 1, got {comp['weight']}"

        total_weight += comp["weight"]

    if abs(total_weight - 1.0) > 0.01:
        return False, f"Competency weights must sum to 1.0, got {total_weight}"

    return True, None


def normalize_score(score: float, old_min: float = 0.0, old_max: float = 10.0, 
                   new_min: float = 0.0, new_max: float = 100.0) -> float:
    """Normalize a score from one range to another."""
    if old_min == old_max:
        return new_min
    return ((score - old_min) / (old_max - old_min)) * (new_max - new_min) + new_min
