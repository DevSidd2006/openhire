# Security Policy

OpenHire handles resumes, interview transcripts, and hiring decisions about
real people. Security reports are welcome and taken seriously.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** A public
report tells everyone how to exploit a running deployment before it can be
fixed.

Instead, use one of:

- **GitHub private vulnerability reporting** — the *Report a vulnerability*
  button under this repository's **Security** tab. Preferred: it keeps the
  discussion attached to the project and private until a fix ships.
- **Email the maintainer** — kushwahasiddhartha31@gmail.com, with
  "OpenHire security" in the subject.

Please include: what the problem is, the steps to reproduce it, what an
attacker could achieve, and which version or commit you tested. A minimal
proof of concept helps enormously; a working weaponised exploit is not
needed.

You will get an acknowledgement within a few days. This is a
student-maintained project, not a company with an on-call rotation, so
please be patient — and if a report goes unanswered for two weeks, feel
free to nudge.

If you would like credit in the fix, say so and how you want to be named.

## Scope

In scope: anything in this repository, and any deployment of it that you
are authorised to test.

**Please do not test against the hosted instance without asking first.** It
carries other students' real resumes and interview data. Run a local copy —
it needs no API key and no database (see
[CONTRIBUTING.md](CONTRIBUTING.md)) — and test that instead.

Out of scope: findings against third parties this project talks to (Render,
the LLM providers), which belong in their own disclosure programmes.

## What we consider a vulnerability

Anything that lets someone read or change data they do not own, in
particular:

- Reading another user's resume, transcript, evaluation, or account details.
- Acting as another user — forged or replayed tokens, missing ownership
  checks on an endpoint, privilege escalation to a recruiter or admin.
- Leaking a credential: an API key, a database URL, or a JWT secret
  appearing in a response, a log line, an error body, or the repository.
- Injection into an LLM prompt that makes the system fabricate or alter
  evaluation evidence about a candidate. This project's core promise is
  that scores are evidence-backed, so a way to forge that evidence is a
  security bug, not a quirk.

## Deploying OpenHire safely

If you run your own instance:

- **Set `JWT_SECRET_KEY`** to a real random secret
  (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`). The
  in-repo default is a public placeholder, and the service refuses to start
  in production while it is still in use.
- **Set `AUTH_ENABLED=true`.** It defaults to `false`, which serves every
  request as an anonymous principal.
- **Set `DATABASE_URL`.** Without it, all data lives in memory and is lost
  on restart.
- **Set `CORS_ALLOW_ORIGINS`** to your exact frontend origin. A wildcard is
  rejected outright in production.
- **Keep `ENVIRONMENT=production`**, which also disables the interactive API
  docs.
- **Rotate any credential that has ever been committed**, even if a later
  commit removed it — git history keeps it.
