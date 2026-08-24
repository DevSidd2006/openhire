# Frontend — Stage 1

## Mission

Build the three screens that make the Stage 1 prototype usable: the
recruiter's job-creation form, the candidate's voice interview page, and the
recruiter's results view. Stage 1 is single-candidate, single-language
(English), no auth — the UI should stay that simple and not build ahead of
the backend.

## Owns

- Recruiter job-creation form (title + fixed question list input)
- Candidate interview page: displays the current question, captures a voice
  answer, and submits it
- Recruiter results view: displays a candidate's score and report once
  scoring is complete

## Depends On

- **Backend:** `POST /jobs` — body `{title: string, questions: string[]}` →
  `{job_id: string}`. Used by the job-creation form on submit.
- **Backend:** `GET /jobs/:job_id` — →
  `{job_id, title, questions, interview_link}`. Used to show the recruiter
  the shareable interview link after job creation.
- **Backend:** `GET /interviews/:link_token` — →
  `{interview_id, job_title, questions}`. Used to load the candidate
  interview page when they open the link.
- **Backend:** `POST /interviews/:interview_id/answers` — body
  `{question_index: number, audio: blob}` → `{transcript_id: string}`. Used
  to submit each recorded answer.
- **Backend:** `POST /interviews/:interview_id/complete` — no body →
  `{status: "scored"}`. Called once the candidate has answered all
  questions.
- **Backend:** `GET /interviews/:interview_id/results` — →
  `{overall_score: number, report_text: string}`. Used by the recruiter
  results view.

## Provides

Frontend is a leaf role in Stage 1 — no other role file should have a
"Depends On" entry naming Frontend.

## Stage 1 Tech Stack

React web app. Voice capture uses the browser's `MediaRecorder` API; no
client-side STT — raw audio is sent to the backend.

## Definition of Done

- [ ] A recruiter can create a job with a title and a fixed list of
      questions and receives a working interview link.
- [ ] A candidate opening that link sees the questions one at a time and can
      record and submit a voice answer to each.
- [ ] After the candidate finishes, the recruiter's results view shows a
      score and report for that candidate.
