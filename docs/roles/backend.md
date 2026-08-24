# Backend — Stage 1

## Mission

Own the REST API layer that ties the frontend, speech-to-text, the scoring
LLM call, and the database together: job CRUD, interview session lifecycle,
STT orchestration, and triggering scoring. Stage 1 is one candidate at a
time, synchronous, no auth.

## Owns

- Job CRUD endpoints
- Interview session lifecycle (link tokens, status transitions)
- STT orchestration (calling the cloud STT provider on submitted audio)
- Triggering the Stage 1 scoring call and persisting its result

## Depends On

- **Database:** `jobs(id, title, questions jsonb, created_at)` — read/write
  from job CRUD endpoints.
- **Database:** `candidates(id, name, email, created_at)` — created when a
  candidate first opens an interview link.
- **Database:** `interviews(id, job_id, candidate_id, link_token, status, created_at)`
  — read/write across the interview lifecycle.
- **Database:** `transcripts(id, interview_id, question_index, answer_text, audio_url, created_at)`
  — written after each STT call.
- **Database:** `scores(id, interview_id, overall_score, report_text, created_at)`
  — written after the scoring call.
- **Multi-Agent:** `buildDeliveryPrompt(question: string): string` — called
  when preparing to present a question to the candidate.
- **Multi-Agent:** `scoreTranscript(job, transcript): {overall_score, report_text}`
  — called from `POST /interviews/:interview_id/complete`; its return value
  is written directly into the `scores` table.

## Provides

- `POST /jobs` — body `{title: string, questions: string[]}` →
  `{job_id: string}`
- `GET /jobs/:job_id` — →
  `{job_id, title, questions, interview_link}`
- `GET /interviews/:link_token` — →
  `{interview_id, job_title, questions}`
- `POST /interviews/:interview_id/answers` — body
  `{question_index: number, audio: blob}` → `{transcript_id: string}`
  (calls STT internally, writes to `transcripts`)
- `POST /interviews/:interview_id/complete` — no body →
  `{status: "scored"}` (calls `scoreTranscript`, writes to `scores`)
- `GET /interviews/:interview_id/results` — →
  `{overall_score: number, report_text: string}`

## Stage 1 Tech Stack

Node or Python API layer (team's choice, consistent across the service).
Cloud STT provider (e.g. Whisper API) called synchronously per answer.
Postgres via the Database role's schema.

## Definition of Done

- [ ] All 6 endpoints listed above are implemented and match the shapes in
      this file exactly.
- [ ] An audio answer submitted via `POST /interviews/:interview_id/answers`
      results in a row in `transcripts` with non-empty `answer_text`.
- [ ] `POST /interviews/:interview_id/complete` results in a row in `scores`
      that `GET /interviews/:interview_id/results` correctly returns.
