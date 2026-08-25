"""
P6: reusable agent evaluation framework.

This is deliberately separate from tests/ (unit/integration correctness) -
evaluation/ measures BEHAVIORAL quality: does an agent, given a controlled
simulated LLM judgment, produce grounded, non-fabricated, schema-valid
output that respects this project's invariants (no evidence for a question
never asked, no candidate_id/job_id drift, no score outside range, no
treating "not discussed" as "false", ...)? See evaluation/runner.py's module
docstring for the full design rationale, and docs/multi-agent-system.md's
"Agent Evaluation Framework" section for the high-level picture.
"""
