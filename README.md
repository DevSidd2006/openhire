# OpenHire

> An AI-powered interview platform for high-volume candidate screening with structured, voice-based interviews and multi-agent evaluation.

OpenHire automates campus hiring at scale by running structured voice interviews, parsing resumes, matching candidates to job descriptions, and providing explainable, evidence-backed evaluation reports through a specialized multi-agent pipeline.

## 📋 Table of Contents

- [Key Features](#key-features)
- [Project Status](#project-status)
- [Development Stages](#development-stages)
- [Getting Started](#getting-started)
- [Project Structure](#project-structure)
- [Development](#development)
- [Architecture](#architecture)
- [Documentation](#documentation)

## ✨ Key Features

**Current (Stage 1):**
- **Voice-Based Interviews** — Structured interview delivery via voice transcription
- **Candidate Management** — Upload resumes, create applications, track interview status
- **Job Configuration** — Recruiters define jobs and fixed interview question sets
- **LLM-Powered Evaluation** — Automatic scoring of interview transcripts against job requirements
- **Explainable Reports** — Candidate rankings with scoring breakdown and evidence
- **Multi-Provider Support** — Works with NVIDIA NIM, OpenAI, Groq, or Google Gemini as LLM backends

**Planned (Stage 2+):**
- Adaptive follow-up questions based on candidate responses
- Per-competency scoring with configurable rubrics
- Resume screening and JD matching
- Multi-interview type support (technical, behavioral, aptitude)
- Recruiter and candidate dashboards

## 📊 Project Status

**Current Stage: 1 (Basic Prototype)** — Single-candidate linear interview loop with resume parsing, job configuration, and LLM-based evaluation.

**Features Implemented:**
- ✅ User authentication (signup/login with JWT)
- ✅ Resume upload and parsing
- ✅ Job creation and management
- ✅ Interview session creation and voice/text input
- ✅ LLM-based interview evaluation and scoring
- ✅ Candidate and evaluation tracking
- ✅ PostgreSQL data persistence
- ✅ Multi-agent system foundation

**In Progress:**
- 🔄 Interview mediator and session management
- 🔄 Voice interview UI components
- 🔄 Candidate dashboard

**Roadmap:** See [`pages/roadmap.html`](pages/roadmap.html) for interactive progress tracker.

## 🎯 Development Stages

Development is staged so every milestone is a working, demoable system — not a partial build.

### Stage 1 — Basic Prototype (Current) ✅

**Scope:** Single-candidate linear interview loop. Recruiter creates a job with fixed questions, candidate completes interview via link, transcript is scored by LLM, recruiter sees results.

- One interview type (scripted questions)
- One language (English)
- Basic resume parsing
- Fixed question sets
- Single-pass LLM evaluation
- No adaptive follow-ups
- One candidate at a time

**Stack:** Python FastAPI backend, React frontend, PostgreSQL database, LLM providers (NVIDIA NIM/OpenAI/Groq/Google), voice support via edge-tts.

**Goal:** Prove the interview-to-evaluation loop works end to end.

### Stage 2 — Advanced Features 🔄

Adaptive questioning based on responses, per-competency scoring with configurable rubrics, multiple interview types, role-specific scoring weights, functional dashboards.

**Key Additions:** State management, rubric builder, competency-based scoring, filterable dashboards, recruiter/candidate role management.

### Stage 3 — Multi-Agent System 🔮

Split into specialized agents: Orchestrator, Technical Evaluator, Integrity Agent, Scoring Agent, Report Generator. Async evaluation on sealed transcripts. Vector DB for context and question-bank lookup.

**Key Additions:** Async task queue, vector embeddings, agent audit logging, evidence tracking.

### Stage 4 — Production Scale 🚀

Full-featured production system with multilingual support, reliability improvements, bias testing, security hardening, monitoring/observability, and scalable architecture for burst loads.

## 🚀 Getting Started

### Prerequisites

- Python 3.13+
- PostgreSQL 13+
- Node.js 18+ (for frontend development)
- LLM API key (NVIDIA NIM recommended, or OpenAI/Groq/Google Gemini)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/DevSidd2006/OpenHire.git
   cd OpenHire
   ```

2. **Create environment file:**
   ```bash
   cp .env.example .env
   ```

3. **Configure your LLM provider:**
   ```bash
   # In .env, set one of:
   NVIDIA_NIM_API_KEY=your_nvidia_key
   NVIDIA_NIM_BASE_URL=https://integrate.api.nvidia.com/v1
   # OR
   GROQ_API_KEY=your_groq_key
   # OR
   OPENAI_API_KEY=your_openai_key
   # OR
   GEMINI_API_KEY=your_gemini_key
   ```

4. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

5. **Set up database:**
   ```bash
   # Start PostgreSQL (Docker)
   docker-compose -f docker-compose.postgres.yml up -d
   
   # Run migrations
   python -m alembic upgrade head
   ```

6. **Run the server:**
   ```bash
   python -m uvicorn api.app:app --reload
   ```

   Server runs at `http://localhost:8000`
   API docs at `http://localhost:8000/docs`

### Running Tests

```bash
pytest tests/ -v
```

## 📁 Project Structure

```
OpenHire/
├── api/                        # FastAPI REST API
│   ├── routes/                 # API endpoints (auth, jobs, interviews, etc.)
│   ├── models/                 # Pydantic request/response models
│   ├── errors.py               # Error handling & exceptions
│   └── app.py                  # FastAPI app initialization
│
├── agents/                     # Multi-agent system (Stage 3+)
│   ├── base.py                 # Base agent class
│   ├── orchestrator/           # Interview orchestration
│   ├── interviewer/            # Conversational interviewer
│   ├── resume_parser/          # Resume text extraction
│   ├── resume_matcher/         # Resume-JD matching
│   ├── jd_analyzer/            # Job description analysis
│   ├── technical_evaluator/    # Technical skills evaluation
│   ├── behavioral_evaluator/   # Behavioral assessment
│   ├── scoring/                # Score aggregation
│   ├── report_generator/       # Report generation
│   └── ...                     # Other specialized agents
│
├── core/                       # Core business logic
│   ├── config.py               # AppSettings configuration
│   ├── container.py            # Dependency injection container
│   ├── security.py             # Auth and security utilities
│   ├── errors.py               # Custom exceptions
│   └── dependencies.py         # Request dependencies
│
├── services/                   # Business logic services
│   ├── auth_service.py         # User authentication & JWT
│   └── ...                     # Other services
│
├── repositories/               # Data access layer
│   ├── interfaces.py           # Repository interfaces
│   ├── memory/                 # In-memory implementations (testing)
│   └── postgres/               # PostgreSQL implementations
│
├── providers/                  # External service integrations
│   ├── llm/                    # LLM providers (NVIDIA NIM, OpenAI, Groq, Gemini)
│   ├── audio/                  # Speech synthesis (edge-tts)
│   ├── embeddings/             # Embedding generation (local/remote)
│   └── vector_store/           # Vector database (FAISS, mock)
│
├── schemas/                    # Data models & validation
│   ├── llm_outputs.py          # LLM response schemas
│   ├── audit.py                # Audit log schemas
│   └── ...                     # Other data schemas
│
├── utils/                      # Utility functions
│   ├── resume_documents.py     # Resume parsing utilities
│   ├── logging.py              # Custom logging
│   └── ...                     # Other utilities
│
├── db/                         # Database
│   ├── migrations/             # Alembic migrations
│   └── schema.sql              # Database schema
│
├── config/                     # Configuration
│   └── settings.py             # Global settings & env vars
│
├── tests/                      # Test suite
│   └── fixtures/               # Test data & fixtures
│
├── pages/                      # Frontend (React)
│   ├── js/                     # React components
│   ├── css/                    # Stylesheets
│   └── roadmap.html            # Interactive progress tracker
│
├── prompts/                    # LLM prompt templates
├── design-system/              # UI design system
├── deploy/                     # Deployment configs
├── data/                       # Sample data
│
├── main.py                     # Legacy entry point
├── requirements.txt            # Python dependencies
├── .env.example                # Environment template
├── docker-compose.postgres.yml # Postgres Docker setup
└── README.md                   # This file
```

## 🏗️ Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Frontend (React)                         │
│              Job Creation, Interview, Dashboard              │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTP/WebSocket
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   FastAPI REST API                           │
│  Auth · Jobs · Candidates · Interviews · Evaluations · Users │
└────────────────────────┬────────────────────────────────────┘
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   ┌─────────┐     ┌──────────────┐    ┌──────────────┐
   │PostgreSQL│    │   LLM API    │    │ Speech TTS   │
   │Database  │    │ (NVIDIA NIM) │    │ (edge-tts)   │
   └─────────┘    │ (OpenAI)     │    └──────────────┘
                  │ (Groq)       │
                  │ (Gemini)     │
                  └──────────────┘
```

### Database Schema

Key tables:
- **ORGANIZATIONS** — Employer organizations
- **USERS** — Recruiters and candidates
- **JOBS** — Job postings with questions
- **CANDIDATES** — Candidate profiles
- **APPLICATIONS** — Job applications
- **INTERVIEWS** — Interview sessions
- **TRANSCRIPTS** — Interview transcripts
- **EVALUATIONS** — Evaluation results and scores

See [`api/models.py`](api/models.py) for detailed schema.

### Tech Stack

| Layer | Technology |
|-------|-----------|
| **Frontend** | React 18, TypeScript, CSS3 |
| **Backend** | Python 3.13, FastAPI |
| **Database** | PostgreSQL 13+ |
| **Auth** | JWT tokens, bcrypt hashing |
| **LLM** | NVIDIA NIM, OpenAI, Groq, Google Gemini |
| **Embeddings** | NVIDIA NIM (nv-embed-v2), Local (sentence-transformers) |
| **Vector Store** | FAISS (semantic search) |
| **Speech** | edge-tts (TTS), WebRTC (audio) |
| **Agents** | LangGraph, Pydantic |
| **Deployment** | Docker, Render (staging) |

## 🛠️ Development

### Running Locally

1. **Start PostgreSQL:**
   ```bash
   docker-compose -f docker-compose.postgres.yml up -d
   ```

2. **Run migrations:**
   ```bash
   python -m alembic upgrade head
   ```

3. **Start the server:**
   ```bash
   python -m uvicorn api.app:app --reload
   ```

4. **Access the app:**
   - API: http://localhost:8000
   - Docs: http://localhost:8000/docs

### Environment Variables

Key variables in `.env`:

```bash
# Database
DATABASE_URL=postgresql://user:pass@localhost/openhire

# LLM Provider (choose one)
LLM_PROVIDER=nvidia-nim
NVIDIA_NIM_API_KEY=your_key
NVIDIA_NIM_BASE_URL=https://integrate.api.nvidia.com/v1

# Auth
JWT_SECRET_KEY=your_secret_key_here
JWT_ALGORITHM=HS256
JWT_EXPIRATION_MINUTES=30

# Optional
EMBEDDING_PROVIDER=local
VECTOR_STORE_TYPE=faiss
```

### Running Tests

```bash
# All tests
pytest tests/ -v

# Specific test file
pytest tests/test_auth.py -v

# With coverage
pytest --cov=. tests/
```

### Code Organization

- **services/** — Business logic (auth, interview flow)
- **repositories/** — Data access (PostgreSQL, in-memory)
- **agents/** — Multi-agent system components
- **providers/** — External service adapters (LLM, speech, embeddings)
- **api/routes/** — REST endpoint handlers
- **schemas/** — Pydantic data models

## 📚 Documentation

- **[Interactive Roadmap](pages/roadmap.html)** — Live progress tracker and milestone checklist
- **[Multi-Agent System](docs/multi-agent-system.md)** — Agent architecture and flow
- **[API Reference](docs/api.md)** — REST endpoint documentation
- **[Stage 1 Project Flow](docs/roles/PROJECT_FLOW.md)** — Interview workflow and role responsibilities

## 🤝 Contributing

Contributions welcome! Please:

1. Create a feature branch
2. Make your changes
3. Write tests for new functionality
4. Submit a pull request

## 📝 License

MIT License — See LICENSE file for details.

## 👥 Team & Support

Built by the OpenHire team. For questions or issues:
- Open an issue on GitHub
- Check existing documentation
- Review test cases for usage examples
