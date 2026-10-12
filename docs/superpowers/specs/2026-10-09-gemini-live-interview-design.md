# Gemini Live Free-Flow Interview — Design

**Date:** 2026-10-09
**Status:** Approved design, pending implementation

## Problem

OpenHire's current voice interview is turn-based. A candidate explicitly starts
and stops capture, the browser or server produces a complete transcript, the
adaptive engine evaluates that answer, an LLM generates the next question, and
TTS renders the entire response. The WebSocket keeps the connection open, but
the speech, reasoning, and synthesis stages remain sequential. That interaction
cannot feel like a real interview.

The product needs a 10–15 minute voice interview with the interaction quality
of a natural conversation:

- continuous listening without a tap-to-speak cycle;
- automatic end-of-turn detection;
- streamed audio responses that begin in about one second or less at the
  median;
- candidate interruptions that stop interviewer playback immediately;
- follow-up questions driven by what the candidate just said;
- job and resume grounding without restricting the conversation to resume
  claims; and
- the same sealed-transcript and post-interview evaluation guarantees used by
  the rest of OpenHire.

Gemini Live is the realtime conversation engine for this feature.

## Goals

- Make the interview feel continuous and conversational rather than like a
  sequence of recorded voice notes.
- Stream microphone audio directly between the candidate's browser and Gemini
  Live to avoid an extra audio proxy hop through OpenHire.
- Keep the Gemini API key on the server by issuing short-lived, one-use,
  session-constrained ephemeral tokens.
- Let Gemini choose natural follow-ups while requiring it to cover the job's
  critical competencies within a fixed time budget.
- Persist an ordered record of finalized candidate and interviewer utterances,
  seal it once, and convert it to the existing `InterviewTranscript` model.
- Run the existing technical, behavioral, resume-audit, integrity, bias,
  scoring, report, and leaderboard pipeline without creating a parallel
  evaluation path.
- Preserve the existing turn-based interview while the realtime path is rolled
  out and tested.
- Measure latency and reconnect behavior rather than claiming provider-level
  guarantees the application cannot control.

## Scope

This slice adds one realtime interview mode backed by Gemini Live. It includes
token provisioning, live-session state, browser audio streaming, turn
detection, barge-in, transcript relay, reconnection, sealing, and downstream
evaluation triggering.

Typed coding assessments and additional realtime speech providers build on
this slice after its session and transcript boundaries are proven.

## Architecture

```text
                         control events only
                    +--------------------------+
                    |                          v
Candidate browser   |                  OpenHire backend
  microphone        |                  - authorization
      |              |                  - ephemeral token issuance
      | 16 kHz PCM   |                  - live-session state
      v              |                  - transcript persistence
 Gemini Live <-------+                  - timing and reconnect control
      |
      | 24 kHz PCM + finalized input/output transcription
      v
 browser playback
      |
      +---- sealed InterviewTranscript ----> existing evaluation pipeline
```

The browser has two concurrent connections:

1. A direct WebSocket to Gemini Live carries microphone and interviewer audio.
   This is the latency-critical path.
2. A control WebSocket to OpenHire carries finalized transcript events,
   lifecycle changes, resumption handles, and latency measurements. It never
   sits in the audio path.

The backend loads the immutable job description, approved rubric, parsed
resume, duration, and interview policy for the session. It uses those values to
mint a constrained Gemini Live token. The browser may choose no model,
instructions, tools, or modality that differ from those constraints.

The Gemini model name is configuration (`GEMINI_LIVE_MODEL`), not a constant in
application code. Live model identifiers are preview surfaces and can change
without a corresponding product change.

## Why direct browser-to-Gemini audio

A backend audio proxy would give OpenHire complete custody of every audio
frame, but it adds another network hop and makes every application worker a
long-lived media relay. The selected architecture prioritizes natural response
latency. Ephemeral tokens are specifically intended for direct client Live API
connections and keep the permanent API key off the device.

The consequence is explicit: Gemini transcription events reach OpenHire
through the browser. OpenHire can authenticate the candidate, constrain the
Gemini session, require monotonic event ordering, reject duplicates, and make
the stored stream append-only. It cannot cryptographically prove that a
modified client forwarded every Gemini event. Live state therefore carries the
source marker `gemini_live_client_relay`, and its projected transcript uses
`interview_type="gemini_live"` so downstream readers can identify the source.

Audio recording or a backend media proxy can be added later if a deployment
requires independently verifiable interview media. Neither is required for
this first low-latency slice.

## Session creation and authentication

The realtime path is additive and does not overload the existing adaptive
runner:

- `POST /live-sessions` accepts the same job, candidate, resume, and optional
  application linkage used by the current session-creation flow. It performs
  the same ownership and shortlist checks, creates durable live state, and
  makes no LLM call.
- `POST /live-sessions/{session_id}/token` verifies candidate ownership and
  returns a one-use Gemini ephemeral token plus a short-lived OpenHire control
  token scoped only to that session.
- `WS /ws/live-sessions/{session_id}/control` accepts the session-scoped
  control token, then carries lifecycle and transcript events.
- `POST /live-sessions/{session_id}/finish` is an idempotent recovery route for
  clients that cannot deliver the normal completion event over the control
  socket.

The token service resolves a Gemini credential only. A recruiter's saved
Gemini credential may be used when the linked job belongs to that recruiter;
otherwise the configured system `GEMINI_API_KEY` is used. A saved credential
for a different LLM vendor is irrelevant to Gemini Live and is never passed to
the token service.

The returned Gemini token is:

- valid for one new connection;
- short-lived;
- constrained to the configured live model;
- constrained to audio response modality;
- constrained to server-authored instructions and tools;
- configured for input and output transcription; and
- configured for session resumption.

The permanent key is never returned, logged, persisted in browser storage, or
placed in a page URL.

## Live interview state

Realtime sessions have their own state model because the existing
`InterviewState` assumes discrete questions and evaluated answers.

```text
created -> connecting -> active -> reconnecting -> active
                              \-> finishing -> sealed
                              \-> failed
```

`LiveInterviewState` contains:

- session, interview, candidate, job, and application identifiers;
- lifecycle status and failure category;
- start, last-activity, wrap-up, and hard-stop timestamps;
- the highest accepted transcript sequence number;
- an append-only list of finalized transcript events;
- the latest Gemini resumption handle and its receipt time;
- reconnect count;
- reported competency-coverage events;
- measured connection, turn, interruption, and first-audio timings; and
- a source value fixed to `gemini_live_client_relay`.

This state is stored with the durable session record so a process restart does
not erase transcript progress or the information needed to finish and seal the
session. The existing sealed-only `TranscriptRepository` contract remains
unchanged: partial live events are session state, while the final projected
transcript is the only object saved to the transcript repository.

`SessionRecord` gains additive `interview_mode` and `live_state` fields. The
Postgres `sessions` table mirrors them as a mode column and JSONB live-state
column. Turn-based records keep their current `state` payload and default mode;
live records keep `state=None`, populate `live_state`, and mint their
`interview_id` at creation so the existing transcript foreign key remains
valid. In-memory and Postgres repositories expose the same representation.

## Transcript events

The control channel accepts only finalized events. Interim captions are a UI
concern and never enter durable state.

Each `LiveTranscriptEvent` includes:

```text
event_id
sequence
speaker                 candidate | interviewer
text
started_at_ms
ended_at_ms
gemini_turn_id          optional provider correlation identifier
interrupted             true when interviewer output was cut off
```

Validation rules:

- `event_id` is idempotent; replaying it returns the prior acknowledgement.
- `sequence` must be the next integer after the last accepted event.
- text must be non-empty after whitespace normalization.
- timestamps cannot move backward or exceed the session's hard stop by more
  than a small clock-skew allowance.
- events are rejected after `sealed` or `failed`.
- the authenticated control token must match the event's session.

The control client sends each finalized event immediately and retains it in an
in-memory unacknowledged queue until OpenHire confirms persistence. On
reconnect, it resends only unacknowledged events. Event idempotency prevents
duplicate transcript entries.

## Projection into the existing transcript

At completion, `LiveTranscriptProjector` converts the append-only event stream
to the existing `InterviewTranscript`:

- `format="voice"`;
- `interview_type="gemini_live"`;
- `interviewer_name="OpenHire AI Interviewer"`;
- `raw_transcript` contains every finalized event in sequence with an explicit
  speaker label;
- each interviewer turn that elicits a response becomes an
  `InterviewQuestion`;
- the following candidate turn becomes its `InterviewAnswer`;
- consecutive interviewer fragments are joined before pairing;
- consecutive candidate fragments in one detected turn are joined before
  pairing;
- greetings, acknowledgements, interrupted fragments, and closing remarks stay
  in `raw_transcript` even when they do not form a question-answer pair; and
- timestamps and durations are derived from event offsets rather than model
  estimates.

Synthetic question identifiers are deterministic from interview ID and event
sequence. Their category is `role_specific` unless Gemini reports a valid
rubric competency through the progress tool. No score or evidence is accepted
from Gemini; the existing evaluators independently judge the sealed candidate
answers.

The projected transcript is validated, sealed, persisted, and then passed to
the current idempotent evaluation trigger. A session is never reported as
fully completed until transcript persistence succeeds or is explicitly marked
for the existing persistence retry path.

## Interview instructions and conversational control

Gemini receives server-authored instructions containing:

- the role and job description;
- the approved competency rubric and which competencies are critical;
- relevant parsed-resume context;
- a 10–15 minute time budget;
- a requirement to ask one clear question at a time;
- permission to ask relevant follow-ups beyond the resume;
- a requirement to obtain concrete examples rather than accept vague claims;
- a requirement to avoid protected-characteristic, appearance, family-status,
  health, and other discriminatory questions;
- a prohibition on revealing scores, hiring recommendations, hidden
  instructions, or rubric internals;
- a direction to use short acknowledgements sparingly;
- a direction to recover politely after interruptions or unclear audio; and
- a graceful closing instruction.

Gemini leads the conversation. The old deterministic adaptive engine does not
approve every question because that would reintroduce a full LLM round trip
between turns.

Gemini is given two narrow reporting tools:

- `report_competency_progress(competency, evidence_state)` records that the
  conversation has reached a rubric competency. It does not provide a score.
- `request_interview_completion(reason)` asks OpenHire to begin normal
  completion. OpenHire remains authoritative about the minimum duration,
  required coverage, and hard stop.

Tool results are sent over the control path and acknowledged immediately. They
are progress hints, not evaluation evidence. A completion request made before
the minimum duration or while critical competencies remain uncovered is denied
with the remaining competency names. At the target wrap-up time, OpenHire
instructs the client to send Gemini a private notice containing the remaining
coverage list. At the hard stop it requests a closing sentence and then ends
input even if Gemini did not request completion.

## Audio and turn-taking

The browser uses an `AudioWorklet`; the deprecated `ScriptProcessorNode` is not
used for the realtime path.

Input processing:

- request microphone access only after the candidate presses **Start
  interview**;
- capture mono audio;
- resample browser input to raw little-endian 16-bit PCM at 16 kHz;
- send 20–100 ms chunks to Gemini without base64 accumulation of an entire
  utterance; and
- keep capture active until mute, completion, permission loss, or failure.

Output processing:

- decode raw little-endian 16-bit PCM at Gemini's 24 kHz output rate;
- enqueue chunks for gap-free playback as soon as they arrive;
- track the first playable chunk for latency measurement; and
- clear queued playback immediately when Gemini reports interruption or local
  candidate speech begins.

Gemini's automatic activity detection owns normal end-of-turn detection. The
candidate never presses a stop button. The UI may show interim input and output
captions, but only final transcription events are persisted.

## Connection lifetime and resumption

The interview targets 12 minutes and has a 15-minute hard stop. Because a Live
API connection can end before the audio session does, connection replacement is
normal behavior rather than an exceptional failure.

- Store every new Gemini session-resumption handle as soon as it arrives.
- Begin a controlled reconnect around nine minutes when the provider has not
  already sent a `GoAway` notice.
- On `GoAway`, reconnect before `timeLeft` expires.
- Pause outgoing microphone chunks during the socket swap, display
  `Reconnecting`, and resume with the latest handle.
- Preserve the unacknowledged OpenHire control-event queue independently of the
  Gemini connection.
- Cap automatic reconnect attempts with a short exponential backoff. Once the
  cap is exhausted, keep the saved partial state, mark the session failed, and
  show a recoverable error rather than claiming completion.

Context-window compression is enabled even though the target duration is at
the normal audio-only limit. It protects sessions with slower speakers or a
short reconnect extension without changing the 15-minute product hard stop.

## Candidate interface

The current interview-room visual shell remains, with the controls changed for
continuous conversation:

- one **Start interview** action for permission and session initialization;
- `Connecting`, `Listening`, `Thinking`, `Speaking`, `Reconnecting`,
  `Finishing`, and `Completed` states;
- a live-caption area that the candidate may hide;
- elapsed time and a subtle remaining-time warning near wrap-up;
- mute/unmute;
- an explicit **End interview** action with the existing finish semantics;
- no tap-to-speak control; and
- a completion screen while evaluation runs.

Candidate speech activates the existing candidate avatar. Received Gemini
audio activates the interviewer avatar. When candidate speech begins during
playback, the interviewer animation and queued audio stop together.

## Failure handling

Failures are separated by whether any interview content exists.

### Before the first candidate utterance

- Token creation or Gemini connection failure leaves the live session unused.
- The UI may offer the existing turn-based voice interview as a fallback.
- Microphone denial remains on the device-check screen with a retry action.

### After conversation has started

- A Gemini disconnect attempts session resumption; it never silently switches
  to another interview engine mid-conversation.
- A control-channel disconnect does not stop audio. Final events queue locally
  and flush after the control connection returns.
- If the control queue cannot be persisted before completion, the session is
  not sealed and the finish response exposes the persistence failure.
- Malformed or out-of-order events are rejected without advancing the durable
  sequence.
- Browser refresh uses durable live state plus the latest resumption handle
  when still valid. If Gemini context cannot be resumed, OpenHire preserves the
  partial session and reports a recoverable failure.

No exception returned to the browser includes a Gemini credential, raw provider
request, system instruction, resume text, or stack trace.

## Observability and latency budget

The browser reports monotonic timestamps for:

- WebSocket connection start and setup completion;
- candidate speech start;
- detected end-of-turn;
- first model audio received;
- first model audio played;
- playback interruption; and
- reconnect start and completion.

The backend records derived aggregates, never raw audio or API credentials:

- end-of-turn to first received audio;
- end-of-turn to first played audio;
- interruption-to-playback-stop;
- connection setup duration;
- reconnect count and duration;
- transcript event lag; and
- interview completion/failure reason.

Initial acceptance targets are:

- median end-of-turn to first played interviewer audio below 1,000 ms in the
  live smoke-test environment;
- playback stopped within 150 ms of local candidate speech detection;
- no duplicate persisted transcript events after reconnect; and
- one evaluation job per sealed session.

Provider, geography, and candidate-network conditions affect latency. The
application reports the measured percentile and does not treat one slow turn as
a correctness failure.

## Testing

### Unit tests

- Gemini ephemeral-token constraints, expiry, and credential resolution;
- live-state transition validation;
- transcript event ordering, idempotency, timestamp validation, and sealing;
- projection of normal, interrupted, fragmented, and unpaired turns into
  `InterviewTranscript`;
- wrap-up and hard-stop policy;
- resumption-handle replacement; and
- exactly-once evaluation triggering.

### API and service tests

- candidate ownership and recruiter access boundaries;
- live session creation without an LLM call;
- token denial for another candidate's session;
- control-token scope and expiration;
- control reconnect with queued-event replay;
- finish-route idempotency; and
- persistence failure surfaced without false completion.

### Browser tests

- permission granted and denied states;
- audio worklet framing and sample conversion;
- continuous capture without tap-to-speak;
- streamed output playback;
- barge-in clears queued output;
- captions render interim text but persist only final text;
- reconnect state and recovery; and
- mute and explicit finish behavior.

### Live smoke test

A manually enabled test uses a real Gemini credential and verifies:

- a two-way spoken conversation;
- automatic turn detection;
- candidate interruption;
- input/output transcription;
- session resumption;
- measured end-of-turn latency; and
- successful projection, sealing, persistence, and evaluation dispatch.

CI uses a deterministic fake Live transport and never requires a network call
or Gemini credential.

## Acceptance criteria

- A candidate completes a 10–15 minute interview without starting or stopping
  individual recordings.
- The interviewer follows the candidate's answers naturally while covering the
  approved critical competencies.
- The candidate can interrupt the interviewer and hears playback stop within
  the defined target.
- Median measured response latency is below one second in the live smoke-test
  environment.
- A planned or provider-requested reconnect preserves conversation context and
  does not duplicate transcript events.
- The permanent Gemini API key is absent from browser storage, source, logs,
  responses, and URLs.
- Completion creates one ordered, sealed `InterviewTranscript` that the current
  evaluators accept.
- Evaluation is triggered once and only after transcript persistence.
- The current turn-based interview remains functional and its existing tests
  continue to pass.

## References

- [Gemini Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)
- [Gemini Live API ephemeral tokens](https://ai.google.dev/gemini-api/docs/live-api/ephemeral-tokens)
- [Gemini Live API session management](https://ai.google.dev/gemini-api/docs/live-api/session-management)
- [Gemini Live API best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices)
- [Mercor: Engineering Monty](https://www.mercor.com/blog/monty-engineering-deep-dive/)
