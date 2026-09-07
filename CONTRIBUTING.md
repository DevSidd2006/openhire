# Contributing to OpenHire

Thanks for taking an interest. OpenHire is an AI interview platform that
handles real people's resumes and interview transcripts, so this guide
leans on two things: **run it in mock mode by default**, and **explain your
reasoning in the code**.

New here? Issues labelled `good first issue` are the intended entry point.

## Table of contents

- [Setting up](#setting-up)
- [Running the tests](#running-the-tests)
- [How this codebase is written](#how-this-codebase-is-written)
- [Making a change](#making-a-change)
- [Opening a pull request](#opening-a-pull-request)
- [Things to be careful with](#things-to-be-careful-with)

## Setting up

**You do not need an API key to develop OpenHire.** The default
`LLM_PROVIDER=mock` runs the whole pipeline against a deterministic local
provider, which is also what the test suite uses. Add a real provider key
only when you are specifically working on provider behaviour.

```bash
git clone https://github.com/DevSidd2006/OpenHire.git
cd OpenHire
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # the defaults are mock-mode and need no editing
python -m uvicorn api.app:app --reload
```

The server comes up on `http://localhost:8000`, with interactive API docs
at `/docs` and the web pages at `/app`. Starting the server calls no LLM,
speech, or vector provider — configuration is read and validated, and
providers are resolved lazily at first use.

### Do you need a database?

No. With `DATABASE_URL` unset, OpenHire uses the in-process repositories in
`repositories/memory.py` — everything works, and nothing survives a
restart. The startup log says so explicitly.

If you want durable data locally:

```bash
docker compose -f docker-compose.postgres.yml up -d
psql "$DATABASE_URL" -f repositories/postgres/schema.sql
```

`repositories/postgres/schema.sql` is the entire migration story: one
idempotent file, safe to re-run against an already-migrated database. There
is no migration tool in this project, and adding one is a discussion to
have in an issue first.

## Running the tests

```bash
pytest tests/ -v                     # everything
pytest tests/test_backend_jobs_candidates_applications.py -v
pytest tests/ -k "rubric" -v
```

The suite is hermetic: `tests/conftest.py` pins every provider to `mock`
and clears `DATABASE_URL` before any project module is imported, so a
`.env` configured for real Groq or a real database cannot redirect your
test run at a paid API. Please keep it that way — a test that needs a
different provider should inject a fake through an existing seam
(`tests/fakes.py`, `app.state.*_factory`), not change the environment.

CI runs the same suite on Python 3.11 and 3.12, so a change that only works
on a newer Python will fail there.

## How this codebase is written

Read a neighbouring file before writing a new one — the conventions are
consistent and mostly visible:

- **Comments explain *why*, not *what*.** The unusual thing about this
  codebase is the density of reasoning in its docstrings: benchmark numbers
  behind a model choice, why a stub is temporary, what a design deliberately
  does *not* do. If you make a non-obvious decision, write down what you
  considered and rejected. If you remove a constraint, say what changed.
- **Layers stay separate.** Route handlers are thin HTTP transport;
  business rules live in `services/`; storage lives behind the ABCs in
  `repositories/interfaces.py`. A handler should not reach into
  `request.app.state` — declare a dependency in `core/dependencies.py`
  instead, so it shows up in the signature and can be overridden in a test.
- **One replacement point.** Which repositories, providers, and dispatchers
  get built is decided in `core/container.py` and nowhere else.
- **Errors carry their own HTTP semantics.** Raise the right class from
  `core/errors.py` rather than assembling a response; client-safe text goes
  in `detail`, everything diagnostic in `internal_detail` (logged, never
  returned).
- **Never fabricate model output.** A schema-validation failure must surface
  as an error, never quietly become plausible-looking data. This is a
  hiring tool; invented evidence about a real candidate is the worst bug
  this project can have.

Tests are expected with behaviour changes, and they read as prose —
`test_production_with_the_default_jwt_secret_refuses_to_start` — with a
docstring saying why the behaviour matters when it is not obvious.

## Making a change

1. Open an issue first for anything beyond a bug fix, so the design gets
   discussed before you spend an evening on it.
2. Branch off `main`: `git checkout -b fix/short-description`.
3. Make the change, with tests.
4. Run `pytest tests/ -v` and make sure it is green.

## Opening a pull request

Describe **what changed and why**, how you verified it, and anything you
deliberately left out. Screenshots help for anything touching `pages/`.

Keep pull requests focused — a reviewer can hold one idea in their head at
a time. Unrelated cleanups belong in their own PR.

All PRs are reviewed by the maintainer (`CODEOWNERS.md`) and must pass CI.

## Things to be careful with

- **Never commit a secret.** Not in code, not in a doc, not in a test
  fixture, not "temporarily". `.env` is gitignored; keep real keys there.
  A credential that reaches a commit lives in git history even after it is
  deleted, and must be rotated rather than merely removed.
- **Never log a key, a token, a password, or a resume body.** The service
  logs no request or response bodies and no headers, on purpose.
- **Treat candidate data as personal data.** Resumes and transcripts belong
  to real people. Do not copy them into fixtures, issues, or screenshots.
- **Found a security problem?** Do not open a public issue — see
  [SECURITY.md](SECURITY.md).
