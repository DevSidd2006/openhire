# Stage 1 Project Flow

The end-to-end path of one Stage 1 interview, step by step. Each step names
its owning role(s) — see that role's file for the exact interface shape.

1. **Recruiter creates a job with fixed questions.**
   Frontend's job-creation form calls `POST /jobs`.
   See [frontend.md](frontend.md#depends-on), [backend.md](backend.md#provides).

2. **Candidate opens the interview link.**
   Frontend's interview page calls `GET /interviews/:link_token`.
   See [frontend.md](frontend.md#depends-on), [backend.md](backend.md#provides).

3. **Candidate answers via voice; audio is sent to the backend.**
   Frontend captures audio and calls
   `POST /interviews/:interview_id/answers` once per question.
   See [frontend.md](frontend.md#depends-on).

4. **Backend calls STT and stores the transcript.**
   Backend orchestrates the STT call and writes to the `transcripts` table.
   See [backend.md](backend.md#owns), [database.md](database.md#provides).

5. **Backend calls the scoring LLM against the transcript and job.**
   Backend calls `POST /interviews/:interview_id/complete`, which invokes
   Multi-Agent's `scoreTranscript` contract.
   See [backend.md](backend.md#depends-on), [multi-agent.md](multi-agent.md#provides).

6. **Score and report are persisted.**
   The `scoreTranscript` result is written to the `scores` table.
   See [database.md](database.md#provides).

7. **Recruiter views the score and report.**
   Frontend's results view calls `GET /interviews/:interview_id/results`.
   See [frontend.md](frontend.md#depends-on), [backend.md](backend.md#provides).

Cutting across all 7 steps: the Fullstack role owns the environment
configuration and provider credentials that make every call above actually
reach a running service. See [fullstack.md](fullstack.md#provides).
