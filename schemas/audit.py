"""
Audit and execution tracking schemas.
"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class AuditLog(BaseModel):
    """Audit log entry for agent execution."""
    log_id: str
    run_id: str  # Overall pipeline run ID
    agent_name: str
    
    # Input/output references
    input_keys: List[str] = Field(default_factory=list)
    output_keys: List[str] = Field(default_factory=list)
    
    # Execution metadata
    start_time: str
    end_time: Optional[str] = None
    duration_seconds: Optional[float] = None
    
    # Model/provider info
    model_name: Optional[str] = None
    provider: Optional[str] = None
    
    # Status
    status: str  # "pending", "running", "success", "failed", "timeout"
    error_message: Optional[str] = None
    
    # Evidence references
    evidence_ids: List[str] = Field(default_factory=list)
    
    # Retry info
    attempt_number: int = 1
    max_attempts: int = 3


class PipelineRun(BaseModel):
    """Overall pipeline execution record."""
    run_id: str
    start_time: str
    end_time: Optional[str] = None
    duration_seconds: Optional[float] = None
    
    # What was processed
    job_id: str
    candidate_ids: List[str] = Field(default_factory=list)
    
    # Execution stages
    stage: str  # "pre_interview", "interview", "post_interview", "complete"
    
    # Audit trail
    audit_logs: List[AuditLog] = Field(default_factory=list)
    
    # Status
    status: str  # "pending", "running", "success", "failed"
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    
    # Summary
    total_agents_executed: int = 0
    successful_agents: int = 0
    failed_agents: int = 0
