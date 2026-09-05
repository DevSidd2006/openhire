# OpenHire — Product Context

**What it is:** An AI-powered interview platform for high-volume candidate screening, using structured voice-based interviews and multi-agent evaluation. It automates campus hiring at scale.

**Core flow:** Recruiter creates a job with fixed questions → candidate completes a voice interview via link → transcript is scored by LLM → recruiter sees an explainable, evidence-backed evaluation report.

## Key Features (Current)

- Voice-based structured interviews (transcription-driven)
- Resume upload and parsing
- Job configuration (recruiters define questions per role)
- LLM-powered automatic scoring of interview transcripts
- Explainable candidate rankings with scoring breakdown and evidence
- Multi-LLM provider support (NVIDIA NIM, OpenAI, Groq, Google Gemini)

## Planned Features

- Adaptive follow-up questions based on candidate responses
- Per-competency scoring with configurable rubrics
- Resume-to-JD matching
- Multiple interview types (technical, behavioral, aptitude)
- Recruiter and candidate dashboards

## Multi-Agent Architecture (in progress / roadmap)

Specialized agents in the evaluation pipeline:
- Orchestrator — interview orchestration
- Interviewer — conversational interview delivery
- Resume Parser — resume text extraction
- Resume Matcher — resume-to-JD matching
- JD Analyzer — job description analysis
- Technical Evaluator — technical skills assessment
- Behavioral Evaluator — behavioral assessment
- Scoring Agent — score aggregation
- Report Generator — evaluation report generation

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 18, TypeScript, CSS3 |
| Backend | Python 3.13, FastAPI |
| Database | PostgreSQL 13+ |
| Auth | JWT tokens, bcrypt hashing |
| LLM | NVIDIA NIM, OpenAI, Groq, Google Gemini |
| Embeddings | NVIDIA NIM (nv-embed-v2), local (sentence-transformers) |
| Vector Store | FAISS (semantic search) |
| Speech | edge-tts (TTS), WebRTC (audio) |
| Agents | LangGraph, Pydantic |
| Deployment | Docker, Render (staging) |

## Target Audience

Recruiters and hiring teams doing high-volume or campus hiring who need to screen many candidates consistently and fairly, replacing manual resume/phone screening with structured, scored voice interviews at scale.

## Project Status

Currently at Stage 1 (Basic Prototype): single-candidate linear interview loop with resume parsing, job configuration, and LLM-based evaluation working end to end. Later stages add adaptive questioning, the full multi-agent pipeline, and production-scale reliability/security.
