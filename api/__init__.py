"""
P5: HTTP service layer for the live adaptive interview.

This package contains ONLY transport concerns: request/response translation,
HTTP status codes, and an in-memory session registry. It must never contain
interview intelligence - no competency prioritization, no LLM calls, no
scoring. The source of truth for all of that remains
utils.interview_session.InterviewSessionRunner (P4) and everything it
delegates to (utils/adaptive_interview.py, agents/interviewer/agent.py,
utils/evidence.py). See api/routes/interview.py for the thin route handlers.
"""
