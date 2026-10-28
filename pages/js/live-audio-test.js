import { GeminiLiveClient } from './gemini-live-client.js?v=6';
import { base64ToInt16, floatToPcm16, resampleLinear } from './live-audio-core.js';

const INPUT_RATE = 16000;
const OUTPUT_RATE = 24000;
const elements = {
  connect: document.getElementById('connectBtn'), mute: document.getElementById('muteBtn'),
  prompt: document.getElementById('promptBtn'), disconnect: document.getElementById('disconnectBtn'),
  clear: document.getElementById('clearBtn'), state: document.getElementById('state'),
  hint: document.getElementById('hint'), transcript: document.getElementById('transcript'),
  orb: document.getElementById('orb'),
};

let client = null;
let stream = null;
let context = null;
let source = null;
let worklet = null;
let silent = null;
let muted = false;
let playbackCursor = 0;
const playing = new Set();

function setState(state, hint = '') {
  elements.state.textContent = state;
  elements.hint.textContent = hint;
}

function setLevel(level) {
  elements.orb.style.setProperty('--level', String(Math.max(0, Math.min(1, level))));
}

function levelOf(samples) {
  if (!samples.length) return 0;
  let sum = 0;
  for (const sample of samples) sum += sample * sample;
  return Math.min(1, Math.sqrt(sum / samples.length) * 3.5);
}

function addTranscript(who, text, interrupted = false) {
  const clean = String(text || '').trim();
  if (!clean) return;
  elements.transcript.querySelector('.empty')?.remove();
  const line = document.createElement('div');
  line.className = `line ${who}`;
  const label = document.createElement('b');
  label.textContent = who === 'user' ? 'You' : 'Gemini';
  const body = document.createElement('span');
  body.textContent = interrupted ? `${clean} … (interrupted)` : clean;
  line.append(label, body);
  elements.transcript.append(line);
  elements.transcript.scrollTop = elements.transcript.scrollHeight;
}

function stopPlayback() {
  for (const node of playing) { try { node.stop(); } catch (_) {} }
  playing.clear();
  playbackCursor = context?.currentTime || 0;
  setLevel(0);
}

function playAudio(encoded) {
  const pcm = base64ToInt16(encoded);
  const buffer = context.createBuffer(1, pcm.length, OUTPUT_RATE);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768;
  const node = context.createBufferSource();
  node.buffer = buffer;
  node.connect(context.destination);
  const when = Math.max(context.currentTime + .01, playbackCursor);
  playbackCursor = when + buffer.duration;
  playing.add(node);
  node.onended = () => { playing.delete(node); if (!playing.size) { setLevel(0); setState('Listening', 'Speak naturally. Interrupt Gemini whenever you want.'); } };
  setLevel(levelOf(channel));
  setState('Gemini speaking', 'Start talking to test barge-in.');
  node.start(when);
}

async function connect() {
  elements.connect.disabled = true;
  setState('Connecting', 'Requesting a short-lived Gemini Live token…');
  try {
    const response = await fetch('/live-audio-test/token', { method: 'POST' });
    const issued = await response.json();
    if (!response.ok) throw new Error(issued.detail || issued.error || 'Token request failed');
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation:true, noiseSuppression:true, autoGainControl:true, channelCount:1 } });
    const AudioContextImpl = window.AudioContext || window.webkitAudioContext;
    context = new AudioContextImpl();
    await context.audioWorklet.addModule('js/live-audio-worklet.js');
    source = context.createMediaStreamSource(stream);
    worklet = new AudioWorkletNode(context, 'openhire-live-audio');
    silent = context.createGain(); silent.gain.value = 0;
    source.connect(worklet); worklet.connect(silent); silent.connect(context.destination);
    client = new GeminiLiveClient({
      onAudio: playAudio,
      onInputTranscript: (text) => addTranscript('user', text),
      onOutputTranscript: (text, interrupted) => addTranscript('ai', text, interrupted),
      onInterrupted: () => { stopPlayback(); setState('Interrupted', 'Gemini stopped. Keep speaking.'); },
      onState: (state) => { if (state === 'closed') disconnect('Connection closed'); },
    });
    await client.connect({
      websocketUrl: issued.websocket_url,
      token: issued.gemini_token,
      model: issued.model,
      enableSessionResumption: false,
    });
    worklet.port.onmessage = ({ data }) => {
      if (!(data instanceof Float32Array) || muted || !client) return;
      setLevel(levelOf(data));
      const resampled = resampleLinear(data, context.sampleRate, INPUT_RATE);
      try { client.sendPcm16(new Int16Array(floatToPcm16(resampled))); } catch (_) {}
    };
    elements.mute.disabled = false; elements.prompt.disabled = false; elements.disconnect.disabled = false;
    setState('Listening', 'Speak naturally. Interrupt Gemini whenever you want.');
  } catch (error) {
    disconnect('Connection failed', false);
    elements.state.classList.add('error');
    elements.hint.textContent = error.message;
  }
}

function disconnect(label = 'Disconnected', resetError = true) {
  stopPlayback();
  client?.close(); client = null;
  worklet?.port.postMessage({ type:'stop' });
  for (const track of stream?.getTracks?.() || []) track.stop();
  try { source?.disconnect(); worklet?.disconnect(); silent?.disconnect(); } catch (_) {}
  context?.close?.();
  stream = context = source = worklet = silent = null;
  muted = false;
  elements.connect.disabled = false; elements.mute.disabled = true; elements.prompt.disabled = true; elements.disconnect.disabled = true;
  elements.mute.textContent = 'Mute';
  if (resetError) elements.state.classList.remove('error');
  setState(label, label === 'Disconnected' ? 'Connect again whenever you are ready.' : elements.hint.textContent);
}

elements.connect.onclick = connect;
elements.disconnect.onclick = () => disconnect();
elements.mute.onclick = () => { muted = !muted; worklet?.port.postMessage({type:'set-muted', muted}); elements.mute.textContent = muted ? 'Unmute' : 'Mute'; setState(muted ? 'Muted' : 'Listening', muted ? 'Microphone audio is paused.' : 'Speak naturally.'); };
elements.prompt.onclick = () => client?.sendPrivateText('Start a casual conversation with one short, interesting question.');
elements.clear.onclick = () => { elements.transcript.innerHTML = '<div class="empty">Conversation transcript will appear here.</div>'; };
window.addEventListener('pagehide', () => disconnect(), { once:true });
