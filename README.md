# OpenHire

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Contributor Covenant](https://img.shields.io/badge/Contributor%20Covenant-2.1-4baaaa.svg)](CODE_OF_CONDUCT.md)
[![Tests](https://img.shields.io/badge/tests-911%20passed-success)](tests/)

> **AI-powered interview and candidate screening platform for high-volume hiring.** OpenHire automates campus recruitment through structured voice interviews, resume parsing, and explainable multi-agent evaluations.

---

## Quickstart

You do **not** need an API key or database to run or develop OpenHire. The default setup runs offline in mock mode:

```bash
# 1. Clone and set up environment
git clone https://github.com/DevSidd2006/OpenHire.git
cd OpenHire
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Copy default environment (configured for mock mode)
cp .env.example .env

# 3. Start the server
python -m uvicorn api.app:app --reload
```

- **Web App:** [http://localhost:8000/app](http://localhost:8000/app)
- **API Docs:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **Health Check:** `curl http://localhost:8000/api/v1/health`

---

## Local Deployment (Docker Compose — One Command)

Replicate the online version on your own system with a single command. This starts PostgreSQL and the FastAPI app together, serving the same `/app` frontend as the cloud deployment.

```bash
# Using Docker Compose directly
docker compose up --build

# Or using the setup script (installs deps, initializes schema, starts everything)
# Windows PowerShell:
.\scripts\setup-local.ps1

# Git Bash / WSL / Linux:
./scripts/setup-local.sh
```

After startup:
- **API:** `http://localhost:8000`
- **Frontend:** `http://localhost:8000/app`
- **API Docs:** `http://localhost:8000/docs`
- **Health:** `http://localhost:8000/api/v1/health`

Configure via `.env` — copy `.env.example` to `.env` and set at minimum:
- `GEMINI_API_KEY` (required for LLM agents and Gemini Live interviews)
- `DATABASE_URL=postgresql://openhire:openhire@localhost:5432/openhire` (or leave empty for in-memory fallback)

---

## Replicating the Online Version Locally

The same codebase runs both online and locally. To match the cloud deployment on your system:

| Step | What to do |
|------|-----------|
| 1 | `cp .env.example .env` |
| 2 | Start Docker Postgres: `docker compose -f docker-compose.postgres.yml up -d` |
| 3 | Set `DATABASE_URL=postgresql://openhire:openhire@localhost:5432/openhire` in `.env` |
| 4 | Set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=<your key>` in `.env` |
| 5 | Keep `TTS_PROVIDER=edge` (free neural TTS, no key needed) |
| 6 | `python -m uvicorn api.app:app --reload` |
| 7 | Open `http://localhost:8000/app` |

### Production-equivalent local config

To make local behave like the deployed production version:

```env
ENVIRONMENT=production
DEBUG=false
AUTH_ENABLED=true
JWT_SECRET_KEY=<run: python -c "import secrets; print(secrets.token_urlsafe(32))">
CORS_ALLOW_ORIGINS=http://localhost:8000
```

`core/lifespan.py` enforces these at startup — `ENVIRONMENT=production` with a placeholder JWT secret or wildcard CORS will refuse to boot.

### Optional cloud services you do NOT need locally

NVIDIA NIM, Groq, OpenAI, Azure Speech, LinkedIn OIDC, and BYOK encryption are all optional. Only one LLM provider (Gemini) and the Docker Postgres link are required for feature parity. Everything else degrades gracefully to mock/in-memory.

---

### Test the realtime interview panel locally

The local demo bypasses sign-in, applications, matching, recruiter setup, and post-interview evaluation. It creates an in-memory sample interview so the Gemini Live conversation, microphone, captions, interruption, and reconnect UI can be exercised directly.

Set these values in `.env`:

```env
ENVIRONMENT=development
AUTH_ENABLED=false
DATABASE_URL=
GEMINI_LIVE_ENABLED=true
GEMINI_API_KEY=your_gemini_api_key
```

Start the API and open:

```text
http://localhost:8000/app/interview.html?demo=1
```

The demo endpoint is unavailable in staging and production.

---

## Configuration Architecture

Settings are split across two modules (see `.env.example`):

- **`core/config.py`** (`AppSettings`) — service-level config: environment, CORS, auth, JWT, database URL, BYOK, LinkedIn OIDC
- **`config/settings.py`** — provider/domain config: LLM, embeddings, audio, vector store, interview limits

---

## How It Works

```
Recruiter Creates Job ──> Candidate Applies & Submits Resume
                                   │
                                   ▼
                    Adaptive Voice Interview (STT/TTS)
                                   │
                                   ▼
                    Multi-Agent Evaluation Pipeline
          (Technical · Behavioral · Resume Audit · Integrity · Bias)
                                   │
                                   ▼
                    Explainable Candidate Leaderboard
```

1. **Job & Rubric Setup:** Recruiters define job requirements and competencies. The system drafts an evidence-bound evaluation rubric.
2. **Resume Matching:** Candidate resumes (.pdf/.docx) are parsed via OCR/text extraction and scored against the approved rubric.
3. **Adaptive Voice Interview:** Shortlisted candidates take a conversational voice interview powered by an adaptive state machine.
4. **Multi-Agent Evaluation:** Sealed transcripts are scored across 12 specialized LLM agents, generating an evidence-backed candidate report.

---

## Multi-Agent System

| Agent | Focus |
|:---|:---|
| **JDAnalyzerAgent** | Extracts skills, competencies, and requirements from job postings |
| **ResumeParserAgent** | Extracts candidate experience, education, and skills from resumes |
| **ResumeMatcherAgent** | Matches resumes to approved rubrics with grounded evidence spans |
| **InterviewerAgent** | Generates dynamic, non-repetitive interview questions and assesses answers |
| **TechnicalEvaluatorAgent** | Evaluates candidate technical proficiency from interview transcripts |
| **BehavioralEvaluatorAgent** | Assesses leadership, teamwork, and communication competencies |
| **ResumeAuditorAgent** | Cross-verifies verbal interview claims against resume entries |
| **IntegrityAgent** | Identifies inconsistencies, conversational contradictions, and red flags |
| **BiasCheckerAgent** | Audits evaluation rationales for demographic or scoring bias |
| **ScoringAgent** | Aggregates competency scores using configurable role weights |
| **ReportGeneratorAgent** | Compiles explainable, citation-backed candidate reports |
| **LeaderboardAgent** | Ranks candidates while surfacing incomplete profiles for human review |

---

## Tech Stack

- **Backend:** Python 3.13, FastAPI, Uvicorn
- **AI & Agents:** LangGraph, Pydantic v2
- **LLM Providers:** NVIDIA NIM, Groq, OpenAI, Google Gemini, Mock Provider
- **Speech (Voice):** Edge-TTS, Azure Speech SDK, WebSockets
- **Database:** PostgreSQL (asyncpg) or zero-dependency In-Memory stubs
- **Frontend:** Vanilla HTML5, CSS3, ES6 JavaScript (served at `/app`)

---

## Running Tests

OpenHire includes a comprehensive test suite of **911 automated tests**:

```bash
# Run all tests
pytest tests/ -v

# Run specific test suite
pytest tests/test_voice_layer.py -v
```

---

## Contributing & Community

Contributions are welcome! Please check:

- **[CONTRIBUTING.md](CONTRIBUTING.md)** — Workflow, test conventions, and PR guidelines.
- **[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)** — Community standards and candidate data protection.
- **[SECURITY.md](SECURITY.md)** — Vulnerability disclosure policy.
- **[OpenBox Bug Board](pages/openbox.html)** — Platform-wide issue tracker.

> **Note:** AI tools are welcome for contributing — just review and verify the output before submitting. Unreviewed AI-generated PR/issue spam will be closed. See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Star History

<a href="https://www.star-history.com/?repos=devsidd2006%2Fopenhire&type=date&legend=bottom-right">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=devsidd2006/openhire&type=date&theme=dark&legend=bottom-right" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=devsidd2006/openhire&type=date&legend=bottom-right" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=devsidd2006/openhire&type=date&legend=bottom-right" />
 </picture>
</a>

---

## License

Distributed under the [MIT License](LICENSE).
