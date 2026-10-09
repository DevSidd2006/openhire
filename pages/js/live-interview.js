import { GeminiLiveClient } from './gemini-live-client.js?v=6';
import {
  PcmFrameBuffer,
  RmsSpeechGate,
  base64ToInt16,
  floatToPcm16,
  resampleLinear,
} from './live-audio-core.js';
import { initializeLegacyInterview } from './legacy-interview.js';

const INPUT_RATE = 16000;
const OUTPUT_RATE = 24000;
const FRAME_SAMPLES = 1600;
const WRAP_AFTER_MS = 12 * 60 * 1000;
const HARD_STOP_AFTER_MS = 15 * 60 * 1000;
const CONTROL_TIMEOUT_MS = 5000;
const RECONNECT_DELAYS_MS = [250, 500, 1000];
const UI_STATES = new Set([
  'Connecting',
  'Listening',
  'Thinking',
  'Speaking',
  'Reconnecting',
  'Finishing',
  'Completed',
]);

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function makeEventId() {
  return globalThis.crypto?.randomUUID?.() ||
    `live-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function setAvatarSpeaking(who, active) {
  document.querySelector(`.avatar.${who}`)?.classList.toggle('speaking', active);
}

function audioLevel(samples) {
  if (!samples?.length) return 0;
  let squareSum = 0;
  for (let index = 0; index < samples.length; index += 1) {
    squareSum += samples[index] * samples[index];
  }
  return Math.min(1, Math.sqrt(squareSum / samples.length) * 3.2);
}

function setAvatarLevel(who, level) {
  const avatar = document.querySelector(`.avatar.${who}`);
  if (!avatar) return;
  const normalized = Math.max(0, Math.min(1, Number(level) || 0));
  avatar.style.setProperty('--audio-level', normalized.toFixed(3));
}

function formatElapsed(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

export class LiveInterviewController {
  constructor({ sessionId, applicationId = '', initialState }) {
    this.sessionId = sessionId;
    this.applicationId = applicationId;
    this.initialState = initialState;

    this.sequence = initialState.last_sequence || 0;
    this.unacknowledgedEvents = new Map();
    this.pendingToolCalls = new Map();
    this.remainingCompetencies = [];
    this.resumeHandle = null;
    this.pendingResumeHandle = null;

    this.controlSocket = null;
    this.controlToken = null;
    this.controlReady = false;
    this.controlReconnectPromise = null;
    this.gemini = null;
    this.geminiReconnectPromise = null;

    this.mediaStream = null;
    this.audioContext = null;
    this.mediaSource = null;
    this.workletNode = null;
    this.silentGain = null;
    this.speechGate = null;
    this.frameBuffer = new PcmFrameBuffer(FRAME_SAMPLES);
    this.audioSending = false;
    this.muted = false;

    this.scheduledSources = new Set();
    this.playbackCursor = 0;
    this.modelTurnStartedMs = null;
    this.candidateSpeechStartedMs = null;
    this.lastEventEndedMs = 0;
    this.turnEndedAt = null;

    this.startedEpochMs = null;
    this.wrapUpAt = initialState.wrap_up_at || null;
    this.hardStopAt = initialState.hard_stop_at || null;
    this.timerInterval = null;
    this.wrapTimer = null;
    this.hardStopTimer = null;
    this.reconnectTimer = null;
    this.scheduledReconnectDone = false;
    this.reconnectAfterSeconds = Number(initialState.reconnect_after_seconds) || 9 * 60;

    this.started = false;
    this.finishing = false;
    this.destroyed = false;
    this.captionsVisible = true;
    this.completionResolver = null;

    this.elements = {
      state: document.getElementById('interviewState'),
      aiStatus: document.getElementById('aiStatus'),
      userStatus: document.getElementById('userStatus'),
      mic: document.getElementById('micBtn'),
      captions: document.getElementById('captionsBtn'),
      elapsed: document.getElementById('elapsedTimer'),
      end: document.getElementById('endBtn'),
      start: document.getElementById('startBtn'),
      overlay: document.getElementById('startOverlay'),
      transcriptPanel: document.getElementById('transcriptPanel'),
      transcriptLog: document.getElementById('transcriptLog'),
      question: document.getElementById('questionBar'),
    };
  }

  bind() {
    this.elements.captions.hidden = false;
    this.elements.elapsed.hidden = false;
    this.elements.start.onclick = () => void this.start();
    this.elements.mic.onclick = () => this.setMuted(!this.muted);
    this.elements.captions.onclick = () => this.setCaptionsVisible(!this.captionsVisible);
    this.elements.end.onclick = () => void this.finish('candidate_ended');
    window.addEventListener('pagehide', () => this.destroy(), { once: true });

    const resumable = ['active', 'connecting', 'reconnecting'].includes(this.initialState.status);
    if (resumable) {
      this.elements.start.querySelector('span')?.remove();
      this.elements.start.textContent = 'Resume interview';
    }
    if (this.initialState.status === 'sealed') {
      this.elements.overlay.style.display = 'none';
      this.setState('Completed');
      this.elements.mic.disabled = true;
      this.elements.end.disabled = true;
      return;
    }
    this.setState('Connecting');
  }

  async start() {
    if (this.started || this.finishing || this.destroyed) return;
    this.started = true;
    this.elements.start.disabled = true;
    this.setState('Connecting');
    const resume = ['active', 'connecting', 'reconnecting'].includes(this.initialState.status);
    const setupStartedAt = performance.now();

    try {
      await this.#prepareAudio();
      const issued = await this.#issueToken(resume);
      this.controlToken = issued.control_token;
      await this.#openControlSocket(this.controlToken);
      await this.#connectGemini(issued, resume ? this.resumeHandle : null);
      this.#sendControl({ type: 'started' });
      this.#setCaptureActive(true);
      this.elements.overlay.style.display = 'none';
      this.setState('Listening');
      this.#recordMetric('connection_setup', performance.now() - setupStartedAt);

      if (!resume) {
        this.gemini.sendPrivateText(
          'Begin the interview now with a brief welcome and one clear opening question.',
        );
      }
      void this.#refreshServerState();
    } catch (error) {
      this.started = false;
      this.elements.start.disabled = false;
      this.#setCaptureActive(false);
      this.#releaseAudio();
      this.#showMessage(
        error?.name === 'NotAllowedError'
          ? 'Microphone access is required to start the interview.'
          : 'The realtime interview could not connect. Please try again.',
      );
      this.setState('Connecting');
    }
  }

  setMuted(muted) {
    this.muted = Boolean(muted);
    this.workletNode?.port.postMessage({ type: 'set-muted', muted: this.muted || !this.audioSending });
    this.elements.mic.setAttribute('aria-pressed', String(this.muted));
    this.elements.mic.innerHTML = this.muted
      ? '<i class="ph-bold ph-microphone-slash" aria-hidden="true"></i><span>Unmute</span>'
      : '<i class="ph-bold ph-microphone" aria-hidden="true"></i><span>Mute</span>';
    this.elements.userStatus.textContent = this.muted ? 'Muted' : 'Mic on';
    if (this.muted && this.speechGate?.isSpeaking) {
      this.speechGate.reset();
      this.#candidateSpeechEnded();
    }
  }

  async finish(reason, { graceMs = 1000 } = {}) {
    if (this.finishing || this.destroyed) return;
    this.finishing = true;
    this.setState('Finishing');
    this.elements.end.disabled = true;
    this.elements.mic.disabled = true;
    this.#setCaptureActive(false);
    this.#clearPlayback(false);

    try {
      if (this.gemini) {
        this.gemini.sendPrivateText('Close the interview politely in one short sentence.');
        if (graceMs > 0) await delay(graceMs);
      }
    } catch (_error) {
      // Completion remains server-authoritative if the provider is already gone.
    }

    try {
      let completed = null;
      if (this.controlReady) {
        const acknowledged = new Promise((resolve) => { this.completionResolver = resolve; });
        this.#sendControl({ type: 'complete', reason });
        completed = await Promise.race([acknowledged, delay(2000).then(() => null)]);
        this.completionResolver = null;
      }
      if (!completed) {
        const pendingEvents = [...this.unacknowledgedEvents.values()]
          .sort((a, b) => a.sequence - b.sequence);
        completed = await globalThis.apiRequest(
          `/live-sessions/${encodeURIComponent(this.sessionId)}/finish`,
          {
            method: 'POST',
            body: pendingEvents.length ? { pending_events: pendingEvents } : {},
          },
        );
      }
      this.setState('Completed');
      this.#showMessage('Interview completed. Finalizing evaluation…');
      this.destroy();
      setTimeout(() => {
        window.location.href = `report.html?session_id=${encodeURIComponent(this.sessionId)}` +
          `&app_id=${encodeURIComponent(this.applicationId)}`;
      }, 900);
    } catch (_error) {
      this.finishing = false;
      this.elements.end.disabled = false;
      this.#showMessage('We could not finalize the interview yet. Please try End interview again.');
      this.setState('Finishing');
    }
  }

  reconnectGemini(reason = 'provider_disconnect') {
    if (this.geminiReconnectPromise || this.finishing || this.destroyed) {
      return this.geminiReconnectPromise;
    }
    this.geminiReconnectPromise = this.#reconnectGemini(reason)
      .finally(() => { this.geminiReconnectPromise = null; });
    return this.geminiReconnectPromise;
  }

  reconnectControl() {
    if (this.controlReconnectPromise || this.finishing || this.destroyed) {
      return this.controlReconnectPromise;
    }
    this.controlReconnectPromise = this.#reconnectControl()
      .finally(() => { this.controlReconnectPromise = null; });
    return this.controlReconnectPromise;
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.#closeTransports();
    this.#releaseAudio();
  }

  setState(state) {
    if (!UI_STATES.has(state)) throw new Error(`Unknown interview state: ${state}`);
    this.elements.state.textContent = state;
    this.elements.aiStatus.textContent = state;
  }

  setCaptionsVisible(visible) {
    this.captionsVisible = Boolean(visible);
    this.elements.transcriptPanel.hidden = !this.captionsVisible;
    this.elements.question.style.display = this.captionsVisible ? 'block' : 'none';
    this.elements.captions.setAttribute('aria-pressed', String(this.captionsVisible));
    this.elements.captions.querySelector('span').textContent =
      this.captionsVisible ? 'Captions on' : 'Captions off';
  }

  async #prepareAudio() {
    if (this.audioContext && this.workletNode && this.mediaStream) {
      await this.audioContext.resume();
      return;
    }
    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    const AudioContextImpl = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextImpl || !window.AudioWorkletNode) {
      throw new Error('AudioWorklet is unavailable');
    }
    this.audioContext = new AudioContextImpl();
    await this.audioContext.resume();
    await this.audioContext.audioWorklet.addModule('js/live-audio-worklet.js');

    this.mediaSource = this.audioContext.createMediaStreamSource(this.mediaStream);
    this.workletNode = new AudioWorkletNode(this.audioContext, 'openhire-live-audio');
    this.silentGain = this.audioContext.createGain();
    this.silentGain.gain.value = 0;
    this.mediaSource.connect(this.workletNode);
    this.workletNode.connect(this.silentGain);
    this.silentGain.connect(this.audioContext.destination);

    const frameDurationMs = 128 / this.audioContext.sampleRate * 1000;
    this.speechGate = new RmsSpeechGate({
      threshold: 0.02,
      speechFrames: Math.max(1, Math.ceil(40 / frameDurationMs)),
      silenceFrames: Math.max(1, Math.ceil(700 / frameDurationMs)),
    });
    this.workletNode.port.onmessage = (event) => this.#handleInputFrame(event.data);
    this.setMuted(false);
    this.#setCaptureActive(false);
  }

  #handleInputFrame(frame) {
    if (!(frame instanceof Float32Array)) return;
    setAvatarLevel('user', this.muted ? 0 : audioLevel(frame));
    if (!this.audioSending || this.muted || !this.gemini) return;

    const activity = this.speechGate.push(frame);
    if (activity === 'speech-start') this.#candidateSpeechStarted();

    const resampled = resampleLinear(frame, this.audioContext.sampleRate, INPUT_RATE);
    const pcm = new Int16Array(floatToPcm16(resampled));
    for (const completeFrame of this.frameBuffer.push(pcm)) {
      try {
        this.gemini.sendPcm16(completeFrame);
      } catch (_error) {
        break;
      }
    }

    if (activity === 'speech-end') this.#candidateSpeechEnded();
  }

  #candidateSpeechStarted() {
    this.candidateSpeechStartedMs = this.#elapsedMs();
    this.elements.userStatus.textContent = 'Speaking';
    setAvatarSpeaking('user', true);
    this.#clearPlayback(true);
    this.setState('Listening');
  }

  #candidateSpeechEnded() {
    const remainder = this.frameBuffer.flush();
    if (remainder.length && this.audioSending && !this.muted) {
      try { this.gemini.sendPcm16(remainder); } catch (_error) {}
    }
    this.turnEndedAt = performance.now();
    this.elements.userStatus.textContent = this.muted ? 'Muted' : 'Mic on';
    setAvatarSpeaking('user', false);
    setAvatarLevel('user', 0);
    this.setState('Thinking');
  }

  async #issueToken(resume) {
    return globalThis.apiRequest(`/live-sessions/${encodeURIComponent(this.sessionId)}/token`, {
      method: 'POST',
      body: { resume },
    });
  }

  async #connectGemini(issued, resumeHandle) {
    const client = new GeminiLiveClient({
      onAudio: (data) => this.#scheduleAudio(data),
      onInputTranscript: (text) => this.#acceptTranscript('candidate', text, false),
      onOutputTranscript: (text, interrupted) =>
        this.#acceptTranscript('interviewer', text, interrupted),
      onInterrupted: () => {
        this.pendingToolCalls.clear();
        this.#clearPlayback(true);
      },
      onResumptionHandle: (handle) => {
        this.resumeHandle = handle;
        this.pendingResumeHandle = handle;
        this.#sendControlIfReady({ type: 'resumption_handle', handle });
      },
      onGoAway: () => void this.reconnectGemini('go_away'),
      onToolCall: (call) => this.#handleToolCall(call),
      onState: (state) => {
        if (state === 'closed' && this.started && !this.finishing && !this.destroyed) {
          void this.reconnectGemini('socket_closed');
        }
      },
    });
    this.gemini = client;
    try {
      await client.connect({
        websocketUrl: issued.websocket_url,
        token: issued.gemini_token,
        model: issued.model,
        resumeHandle,
      });
    } catch (error) {
      if (this.gemini === client) this.gemini = null;
      client.close();
      throw error;
    }
  }

  #scheduleAudio(encoded) {
    if (!this.audioContext || this.destroyed) return;
    const pcm = base64ToInt16(encoded);
    if (!pcm.length) return;

    const buffer = this.audioContext.createBuffer(1, pcm.length, OUTPUT_RATE);
    const channel = buffer.getChannelData(0);
    for (let index = 0; index < pcm.length; index += 1) channel[index] = pcm[index] / 32768;
    setAvatarLevel('ai', audioLevel(channel));

    const source = this.audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(this.audioContext.destination);
    const startAt = Math.max(this.audioContext.currentTime + 0.01, this.playbackCursor);
    this.playbackCursor = startAt + buffer.duration;
    this.scheduledSources.add(source);
    source.onended = () => {
      this.scheduledSources.delete(source);
      if (!this.scheduledSources.size) {
        setAvatarSpeaking('ai', false);
        setAvatarLevel('ai', 0);
        if (!this.speechGate?.isSpeaking && !this.finishing) this.setState('Listening');
      }
    };
    source.start(startAt);

    if (this.modelTurnStartedMs === null) this.modelTurnStartedMs = this.#elapsedMs();
    if (this.turnEndedAt !== null) {
      this.#recordMetric('turn_to_first_audio', performance.now() - this.turnEndedAt);
      this.turnEndedAt = null;
    }
    setAvatarSpeaking('ai', true);
    if (!this.finishing) this.setState('Speaking');
  }

  #clearPlayback(reportMetric) {
    if (!this.scheduledSources.size) return;
    const stopStartedAt = performance.now();
    for (const source of this.scheduledSources) {
      try { source.stop(); } catch (_error) {}
      try { source.disconnect(); } catch (_error) {}
    }
    this.scheduledSources.clear();
    this.playbackCursor = this.audioContext?.currentTime || 0;
    setAvatarSpeaking('ai', false);
    setAvatarLevel('ai', 0);
    if (reportMetric) {
      this.#recordMetric('interruption_stop', performance.now() - stopStartedAt);
    }
  }

  #acceptTranscript(speaker, text, interrupted) {
    const normalized = String(text || '').trim();
    if (!normalized) return;
    const now = this.#elapsedMs();
    const naturalStart = speaker === 'candidate'
      ? this.candidateSpeechStartedMs
      : this.modelTurnStartedMs;
    const startedAt = Math.max(this.lastEventEndedMs, naturalStart ?? now);
    const endedAt = Math.max(startedAt, now, this.lastEventEndedMs);
    const event = {
      event_id: makeEventId(),
      sequence: ++this.sequence,
      speaker,
      text: normalized,
      started_at_ms: Math.round(startedAt),
      ended_at_ms: Math.round(endedAt),
      gemini_turn_id: null,
      interrupted: Boolean(interrupted),
    };
    this.lastEventEndedMs = event.ended_at_ms;
    if (speaker === 'candidate') this.candidateSpeechStartedMs = null;
    else this.modelTurnStartedMs = null;

    this.unacknowledgedEvents.set(event.sequence, event);
    this.#renderTranscript(speaker, normalized, interrupted);
    this.#sendControlIfReady({ type: 'transcript_final', event });
  }

  #renderTranscript(speaker, text, interrupted = false) {
    const entry = document.createElement('div');
    entry.style.marginBottom = '1rem';
    const label = document.createElement('div');
    label.textContent = speaker === 'interviewer' ? 'Interviewer' : 'You';
    label.style.cssText = 'font-size:11px;font-weight:500;text-transform:uppercase;' +
      'letter-spacing:.05em;margin-bottom:.25rem;color:' +
      (speaker === 'interviewer' ? 'var(--ink)' : 'var(--success-text)');
    const body = document.createElement('div');
    body.textContent = interrupted ? `${text} …` : text;
    body.style.cssText = 'font-size:13px;color:var(--ink-3);line-height:1.4;';
    entry.append(label, body);
    this.elements.transcriptLog.appendChild(entry);
    this.elements.transcriptPanel.scrollTop = this.elements.transcriptPanel.scrollHeight;
    this.#showMessage(text);
  }

  #handleToolCall(call) {
    const callId = call.id || makeEventId();
    const args = call.args || call.arguments || {};
    let controlMessage;
    if (call.name === 'report_competency_progress') {
      controlMessage = {
        type: 'competency_progress',
        call_id: callId,
        progress: {
          competency: String(args.competency || ''),
          evidence_state: args.evidence_state,
        },
      };
    } else if (call.name === 'request_interview_completion') {
      controlMessage = {
        type: 'completion_request',
        call_id: callId,
        reason: String(args.reason || 'coverage_complete'),
      };
    } else {
      this.gemini?.sendToolResponse(callId, call.name, { accepted: false, reason: 'unsupported_tool' });
      return;
    }
    this.pendingToolCalls.set(callId, { call, controlMessage });
    this.#sendControlIfReady(controlMessage);
  }

  async #openControlSocket(token) {
    const generation = Symbol('control');
    this.controlGeneration = generation;
    this.controlReady = false;
    const socket = new WebSocket(globalThis.getWebSocketUrl(
      `/ws/live-sessions/${encodeURIComponent(this.sessionId)}/control`,
    ));
    this.controlSocket = socket;

    await new Promise((resolve, reject) => {
      let settled = false;
      let authenticated = false;
      const timeout = setTimeout(() => {
        if (!settled) {
          settled = true;
          socket.close();
          reject(new Error('Control authentication timed out'));
        }
      }, CONTROL_TIMEOUT_MS);
      const fail = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        reject(new Error('Control connection failed'));
      };

      socket.onopen = () => socket.send(JSON.stringify({ type: 'authenticate', token }));
      socket.onerror = fail;
      socket.onclose = () => {
        if (this.controlGeneration !== generation) return;
        this.controlReady = false;
        fail();
        if (authenticated && this.started && !this.finishing && !this.destroyed) {
          void this.reconnectControl();
        }
      };
      socket.onmessage = (event) => {
        let message;
        try { message = JSON.parse(event.data); } catch (_error) { return; }
        if (message.type === 'authenticated' && !settled) {
          settled = true;
          authenticated = true;
          clearTimeout(timeout);
          this.controlReady = true;
          this.#applyControlSnapshot(message);
          resolve();
          return;
        }
        this.#handleControlMessage(message);
      };
    });
  }

  #applyControlSnapshot(snapshot) {
    if (snapshot.wrap_up_at) this.wrapUpAt = snapshot.wrap_up_at;
    if (snapshot.hard_stop_at) this.hardStopAt = snapshot.hard_stop_at;
    if (snapshot.reconnect_after_seconds) {
      this.reconnectAfterSeconds = Number(snapshot.reconnect_after_seconds) || this.reconnectAfterSeconds;
    }
    if (Array.isArray(snapshot.remaining_competencies)) {
      this.remainingCompetencies = snapshot.remaining_competencies;
    }
    const nextSequence = Number(snapshot.next_sequence || 1);
    if (Number.isInteger(nextSequence) && nextSequence > 0) {
      this.sequence = Math.max(this.sequence, nextSequence - 1);
      for (const sequence of this.unacknowledgedEvents.keys()) {
        if (sequence < nextSequence) this.unacknowledgedEvents.delete(sequence);
      }
      for (const [sequence, event] of [...this.unacknowledgedEvents.entries()].sort((a, b) => a[0] - b[0])) {
        if (sequence >= nextSequence) this.#sendControl({ type: 'transcript_final', event });
      }
    }
    for (const pending of this.pendingToolCalls.values()) this.#sendControl(pending.controlMessage);
    if (this.pendingResumeHandle) {
      this.#sendControl({ type: 'resumption_handle', handle: this.pendingResumeHandle });
    }
    this.#configureTimers();
  }

  #handleControlMessage(message) {
    if (message.type === 'event_ack') {
      this.unacknowledgedEvents.delete(Number(message.sequence));
      return;
    }
    if (message.type === 'tool_ack') {
      const pending = this.pendingToolCalls.get(message.call_id);
      if (!pending) return;
      this.pendingToolCalls.delete(message.call_id);
      this.gemini?.sendToolResponse(message.call_id, pending.call.name, { accepted: true });
      return;
    }
    if (message.type === 'completion_decision') {
      const pending = this.pendingToolCalls.get(message.call_id);
      if (!pending) return;
      this.pendingToolCalls.delete(message.call_id);
      this.remainingCompetencies = message.remaining_competencies || [];
      this.gemini?.sendToolResponse(message.call_id, pending.call.name, {
        approved: Boolean(message.approved),
        reason: message.reason,
        remaining_competencies: this.remainingCompetencies,
      });
      if (message.approved) void this.finish(message.reason || 'model_requested', { graceMs: 3000 });
      return;
    }
    if (message.type === 'started_ack') {
      if (message.wrap_up_at) this.wrapUpAt = message.wrap_up_at;
      if (message.hard_stop_at) this.hardStopAt = message.hard_stop_at;
      this.#configureTimers();
      return;
    }
    if (message.type === 'resumption_handle_ack') {
      this.pendingResumeHandle = null;
      return;
    }
    if (message.type === 'completed') {
      this.completionResolver?.(message);
      return;
    }
    if (message.type === 'wrap_up') {
      this.remainingCompetencies = message.remaining_competencies || this.remainingCompetencies;
      this.#sendWrapUpNotice();
      return;
    }
    if (message.type === 'hard_stop') {
      void this.finish('hard_stop', { graceMs: 3000 });
      return;
    }
    if (message.type === 'error' && message.call_id) {
      const pending = this.pendingToolCalls.get(message.call_id);
      if (pending) {
        this.pendingToolCalls.delete(message.call_id);
        this.gemini?.sendToolResponse(message.call_id, pending.call.name, {
          accepted: false,
          reason: message.code || 'control_error',
        });
      }
    }
  }

  #sendControl(message) {
    if (!this.controlReady || !this.controlSocket || this.controlSocket.readyState !== WebSocket.OPEN) {
      throw new Error('Control socket is not ready');
    }
    this.controlSocket.send(JSON.stringify(message));
  }

  #sendControlIfReady(message) {
    if (!this.controlReady) return;
    try { this.#sendControl(message); } catch (_error) {}
  }

  async #reconnectControl() {
    this.controlReady = false;
    for (const waitMs of RECONNECT_DELAYS_MS) {
      if (this.finishing || this.destroyed) return;
      await delay(waitMs);
      try {
        await this.#openControlSocket(this.controlToken);
        return;
      } catch (_error) {}
    }
    this.#sendLifecycleFailure('control_reconnect_failed');
  }

  async #reconnectGemini(reason) {
    this.setState('Reconnecting');
    this.#setCaptureActive(false);
    this.#clearPlayback(false);
    const previousClient = this.gemini;
    this.gemini = null;
    previousClient?.close();
    this.#sendControlIfReady({ type: 'reconnecting', reason });
    const startedAt = performance.now();

    for (const waitMs of RECONNECT_DELAYS_MS) {
      if (this.finishing || this.destroyed) return;
      await delay(waitMs);
      try {
        const issued = await this.#issueToken(true);
        this.controlToken = issued.control_token;
        await this.#connectGemini(issued, this.resumeHandle);
        this.#sendControlIfReady({ type: 'started', reason: 'resumed' });
        this.#setCaptureActive(true);
        this.setState('Listening');
        this.#recordMetric('reconnect', performance.now() - startedAt);
        return;
      } catch (_error) {}
    }
    this.#sendLifecycleFailure('gemini_reconnect_failed');
  }

  #sendLifecycleFailure(reason) {
    this.#sendControlIfReady({ type: 'failed', reason });
    this.#showMessage('The interview connection could not be restored. Please use End interview.');
    this.elements.end.disabled = false;
    this.setState('Reconnecting');
  }

  #recordMetric(name, durationMs) {
    this.#sendControlIfReady({
      type: 'latency_metric',
      metric: { name, duration_ms: Math.max(0, durationMs), turn_id: null },
    });
  }

  #setCaptureActive(active) {
    this.audioSending = Boolean(active) && !this.finishing;
    this.workletNode?.port.postMessage({
      type: 'set-muted',
      muted: !this.audioSending || this.muted,
    });
    if (!this.audioSending) this.frameBuffer.clear();
  }

  #releaseAudio() {
    this.workletNode?.port.postMessage({ type: 'stop' });
    try { this.mediaSource?.disconnect(); } catch (_error) {}
    try { this.workletNode?.disconnect(); } catch (_error) {}
    try { this.silentGain?.disconnect(); } catch (_error) {}
    this.mediaStream?.getTracks().forEach((track) => track.stop());
    if (this.audioContext && this.audioContext.state !== 'closed') {
      void this.audioContext.close();
    }
    this.mediaStream = null;
    this.audioContext = null;
    this.mediaSource = null;
    this.workletNode = null;
    this.silentGain = null;
    this.speechGate = null;
    this.frameBuffer.clear();
  }

  async #refreshServerState() {
    try {
      const state = await globalThis.apiRequest(`/live-sessions/${encodeURIComponent(this.sessionId)}`);
      this.initialState = state;
      this.sequence = Math.max(this.sequence, state.last_sequence || 0);
      this.wrapUpAt = state.wrap_up_at || this.wrapUpAt;
      this.hardStopAt = state.hard_stop_at || this.hardStopAt;
      if (state.reconnect_after_seconds) {
        this.reconnectAfterSeconds = Number(state.reconnect_after_seconds) || this.reconnectAfterSeconds;
      }
      this.#configureTimers();
    } catch (_error) {}
  }

  #configureTimers() {
    const wrapEpoch = this.wrapUpAt ? Date.parse(this.wrapUpAt) : NaN;
    const hardEpoch = this.hardStopAt ? Date.parse(this.hardStopAt) : NaN;
    if (Number.isFinite(wrapEpoch)) this.startedEpochMs = wrapEpoch - WRAP_AFTER_MS;
    else if (Number.isFinite(hardEpoch)) this.startedEpochMs = hardEpoch - HARD_STOP_AFTER_MS;
    else this.startedEpochMs ??= Date.now();

    clearInterval(this.timerInterval);
    this.timerInterval = setInterval(() => {
      this.elements.elapsed.textContent = formatElapsed(Date.now() - this.startedEpochMs);
    }, 1000);
    this.elements.elapsed.textContent = formatElapsed(Date.now() - this.startedEpochMs);

    clearTimeout(this.wrapTimer);
    clearTimeout(this.hardStopTimer);
    clearTimeout(this.reconnectTimer);
    const effectiveWrap = Number.isFinite(wrapEpoch) ? wrapEpoch : this.startedEpochMs + WRAP_AFTER_MS;
    const effectiveHard = Number.isFinite(hardEpoch) ? hardEpoch : this.startedEpochMs + HARD_STOP_AFTER_MS;
    this.wrapTimer = setTimeout(() => this.#sendWrapUpNotice(), Math.max(0, effectiveWrap - Date.now()));
    this.hardStopTimer = setTimeout(
      () => void this.finish('hard_stop', { graceMs: 3000 }),
      Math.max(0, effectiveHard - Date.now()),
    );
    if (!this.scheduledReconnectDone) {
      const reconnectAfterMs = (this.reconnectAfterSeconds || 9 * 60) * 1000;
      const reconnectAt = this.startedEpochMs + reconnectAfterMs;
      this.reconnectTimer = setTimeout(() => {
        this.scheduledReconnectDone = true;
        void this.reconnectGemini('scheduled_refresh');
      }, Math.max(0, reconnectAt - Date.now()));
    }
  }

  #sendWrapUpNotice() {
    if (!this.gemini || this.finishing) return;
    const coverage = this.remainingCompetencies.length
      ? ` Remaining competencies: ${this.remainingCompetencies.join(', ')}.`
      : '';
    try {
      this.gemini.sendPrivateText(
        `Begin wrapping up the interview naturally.${coverage} Ask only what is essential, then close politely.`,
      );
    } catch (_error) {}
  }

  #elapsedMs() {
    return Math.max(0, Date.now() - (this.startedEpochMs || Date.now()));
  }

  #showMessage(text) {
    this.elements.question.textContent = text;
    this.elements.question.style.display = this.captionsVisible ? 'block' : 'none';
  }

  #closeTransports() {
    clearInterval(this.timerInterval);
    clearTimeout(this.wrapTimer);
    clearTimeout(this.hardStopTimer);
    clearTimeout(this.reconnectTimer);
    this.#clearPlayback(false);
    this.gemini?.close();
    this.gemini = null;
    this.controlReady = false;
    if (this.controlSocket && this.controlSocket.readyState < WebSocket.CLOSING) {
      this.controlSocket.close(1000, 'client complete');
    }
    this.controlSocket = null;
  }
}

async function bootstrapInterviewPage() {
  const params = new URLSearchParams(window.location.search);
  let sessionId = params.get('session_id');
  const applicationId = params.get('app_id') || '';
  const localDemoRequested = params.get('demo') === '1' ||
    ['localhost', '127.0.0.1'].includes(window.location.hostname);

  if (!sessionId && localDemoRequested) {
    try {
      const demo = await globalThis.apiRequest('/live-sessions/demo', { method: 'POST' });
      sessionId = demo.session_id;
      const nextUrl = new URL(window.location.href);
      nextUrl.searchParams.delete('demo');
      nextUrl.searchParams.set('session_id', sessionId);
      window.history.replaceState({}, '', nextUrl);
    } catch (error) {
      document.getElementById('startBtn').disabled = true;
      document.getElementById('questionBar').textContent =
        error?.data?.error === 'gemini_live_unconfigured'
          ? 'Add GEMINI_API_KEY to .env, enable Gemini Live, and restart the server.'
          : 'The local interview demo could not be created.';
      document.getElementById('questionBar').style.display = 'block';
      return;
    }
  }
  if (!sessionId) {
    document.getElementById('startOverlay').style.display = 'none';
    document.getElementById('questionBar').textContent = 'No session ID specified.';
    document.getElementById('questionBar').style.display = 'block';
    return;
  }

  try {
    const state = await globalThis.apiRequest(`/live-sessions/${encodeURIComponent(sessionId)}`);
    const controller = new LiveInterviewController({ sessionId, applicationId, initialState: state });
    globalThis.liveInterviewController = controller;
    controller.bind();
  } catch (error) {
    if (error?.status === 404) {
      initializeLegacyInterview();
      document.getElementById('interviewState').textContent = 'Live';
      return;
    }
    document.getElementById('startBtn').disabled = true;
    document.getElementById('questionBar').textContent =
      'The interview could not be loaded. Refresh the page to try again.';
    document.getElementById('questionBar').style.display = 'block';
  }
}

void bootstrapInterviewPage();
