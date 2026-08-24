# Multi-Agent System — Stage 1

## Mission

In Stage 1 there is no real multi-agent split yet — that arrives in Stage 3
(orchestrator, technical evaluator, integrity agent, scoring agent, report
agent). This role's Stage 1 job is to own the prompt design and I/O contract
for the two Stage 1 LLM calls (question delivery, transcript scoring), built
so it becomes a natural seed for the Stage 3 split rather than something
that gets rewritten from scratch.

## Owns

- The prompt/template used to deliver a question to the candidate
- The prompt/template and output parsing used to score a completed
  transcript against a job's questions
- The `buildDeliveryPrompt` and `scoreTranscript` function contracts (design
  and prompt content; Backend owns calling them over HTTP or in-process)

## Depends On

Multi-Agent is a leaf role in Stage 1 — no other role file should have a
"Depends On" entry naming Multi-Agent, other than for the two contracts
listed under Provides.

## Provides

- `buildDeliveryPrompt(question: string): string` — returns the prompt or
  script text used to deliver one question to the candidate (feeds
  whatever TTS/conversation layer Backend or Frontend uses). Stage 1 keeps
  this to straight question delivery — no adaptive follow-ups.
- `scoreTranscript(job: {title: string, questions: string[]}, transcript: {question: string, answer_text: string}[]): {overall_score: number, report_text: string}`
  — the single Stage 1 scoring LLM call. Takes the full job context and the
  full transcript, returns one overall score (0–100) and a free-text report.
  This single-call, single-score shape is deliberately simple — Stage 2
  moves this to per-competency rubric scoring, which is why the input shape
  already carries the full job and full transcript rather than pre-chunked
  competency slices.

## Stage 1 Tech Stack

A single LLM provider call per contract (e.g. one API call for
`buildDeliveryPrompt`'s templating if it's LLM-driven, one for
`scoreTranscript`). No agent framework, no orchestration layer, no vector DB
— those are Stage 3.

## Definition of Done

- [ ] `buildDeliveryPrompt` produces a usable prompt/script for every
      question in a Stage 1 job's fixed question list.
- [ ] `scoreTranscript` returns a valid `{overall_score, report_text}` for a
      complete Stage 1 transcript, with `overall_score` in range 0–100 and
      `report_text` non-empty.
- [ ] The two contracts' input/output shapes match exactly what Backend's
      role file names under its "Depends On" section.
