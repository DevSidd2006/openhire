import test from 'node:test';
import assert from 'node:assert/strict';

import { GeminiLiveClient } from '../../pages/js/gemini-live-client.js';

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.sent = [];
    this.closeArgs = null;
    FakeWebSocket.instances.push(this);
  }

  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }

  receive(message) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  send(message) {
    this.sent.push(message);
  }

  fail() {
    this.onerror?.({});
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code: 1006, reason: '', wasClean: false });
  }

  close(code, reason) {
    this.readyState = FakeWebSocket.CLOSED;
    this.closeArgs = [code, reason];
    this.onclose?.({ code, reason, wasClean: code === 1000 });
  }
}

function resetSockets() {
  FakeWebSocket.instances.length = 0;
}

function sentMessages(socket) {
  return socket.sent.map((value) => JSON.parse(value));
}

test('connect waits for setupComplete and does not expose the token through state', async () => {
  resetSockets();
  const states = [];
  const client = new GeminiLiveClient({
    WebSocketImpl: FakeWebSocket,
    onState: (state) => states.push(state),
  });
  let connected = false;

  const pending = client.connect({
    websocketUrl: 'wss://generativelanguage.example/ws',
    token: 'secret-token',
    model: 'gemini-live-model',
    resumeHandle: 'resume-1',
  }).then(() => { connected = true; });
  const socket = FakeWebSocket.instances[0];

  assert.equal(connected, false);
  socket.open();
  assert.deepEqual(sentMessages(socket), [{
    setup: {
      model: 'models/gemini-live-model',
      generationConfig: { responseModalities: ['AUDIO'] },
      sessionResumption: { handle: 'resume-1' },
    },
  }]);
  assert.equal(connected, false);

  socket.receive({ setupComplete: {} });
  await pending;
  assert.equal(connected, true);
  assert.deepEqual(states, ['connecting', 'connected']);
  assert.equal(JSON.stringify(states).includes('secret-token'), false);
});

test('sends small PCM messages, private text, tool responses, and audio end', async () => {
  resetSockets();
  const client = new GeminiLiveClient({ WebSocketImpl: FakeWebSocket });
  const pending = client.connect({
    websocketUrl: 'wss://generativelanguage.example/ws',
    token: 'token',
    model: 'model',
  });
  const socket = FakeWebSocket.instances[0];
  socket.open();
  socket.receive({ setupComplete: {} });
  await pending;

  socket.sent.length = 0;
  client.sendPcm16(new Int16Array([1, -1]));
  client.sendPrivateText('Please begin wrapping up.');
  client.sendToolResponse('call-1', 'report_competency_progress', { accepted: true });
  client.endAudioStream();

  assert.deepEqual(sentMessages(socket), [
    { realtimeInput: { audio: { data: 'AQD//w==', mimeType: 'audio/pcm;rate=16000' } } },
    { clientContent: {
      turns: [{ role: 'user', parts: [{ text: 'Please begin wrapping up.' }] }],
      turnComplete: true,
    } },
    { toolResponse: { functionResponses: [{
      id: 'call-1',
      name: 'report_competency_progress',
      response: { accepted: true },
    }] } },
    { realtimeInput: { audioStreamEnd: true } },
  ]);
});

test('emits final transcriptions, audio, interruption, resumption and goAway events', async () => {
  resetSockets();
  const events = [];
  const client = new GeminiLiveClient({
    WebSocketImpl: FakeWebSocket,
    onAudio: (data, mimeType) => events.push(['audio', data, mimeType]),
    onInputTranscript: (text, final) => events.push(['input', text, final]),
    onOutputTranscript: (text, interrupted) => events.push(['output', text, interrupted]),
    onInterrupted: () => events.push(['interrupted']),
    onResumptionHandle: (handle) => events.push(['resume', handle]),
    onGoAway: (timeLeft) => events.push(['go-away', timeLeft]),
  });
  const pending = client.connect({ websocketUrl: 'wss://example/ws', token: 't', model: 'm' });
  const socket = FakeWebSocket.instances[0];
  socket.open();
  socket.receive({ setupComplete: {} });
  await pending;

  socket.receive({
    serverContent: {
      inputTranscription: { text: 'candidate answer' },
      outputTranscription: { text: 'First ' },
      modelTurn: { parts: [{ inlineData: { data: 'AAA=', mimeType: 'audio/pcm;rate=24000' } }] },
    },
    sessionResumptionUpdate: { resumable: true, newHandle: 'resume-2' },
    goAway: { timeLeft: '5s' },
  });
  socket.receive({ serverContent: { outputTranscription: { text: 'question.' }, turnComplete: true } });
  socket.receive({ serverContent: { outputTranscription: { text: 'Discard me' }, interrupted: true } });

  assert.deepEqual(events, [
    ['input', 'candidate answer', true],
    ['audio', 'AAA=', 'audio/pcm;rate=24000'],
    ['resume', 'resume-2'],
    ['go-away', '5s'],
    ['output', 'First question.', false],
    ['output', 'Discard me', true],
    ['interrupted'],
  ]);
  assert.equal(client.outputText, '');
});

test('forwards both approved reporting tool calls', async () => {
  resetSockets();
  const calls = [];
  const client = new GeminiLiveClient({
    WebSocketImpl: FakeWebSocket,
    onToolCall: (call) => calls.push(call),
  });
  const pending = client.connect({ websocketUrl: 'wss://example/ws', token: 't', model: 'm' });
  const socket = FakeWebSocket.instances[0];
  socket.open();
  socket.receive({ setupComplete: {} });
  await pending;

  socket.receive({ toolCall: { functionCalls: [
    { id: 'one', name: 'report_competency_progress', args: { competency: 'debugging' } },
    { id: 'two', name: 'request_interview_completion', args: { reason: 'covered' } },
  ] } });

  assert.deepEqual(calls.map((call) => call.name), [
    'report_competency_progress',
    'request_interview_completion',
  ]);
});

test('connection errors stay generic and never contain the token', async () => {
  resetSockets();
  const client = new GeminiLiveClient({ WebSocketImpl: FakeWebSocket });
  const pending = client.connect({
    websocketUrl: 'wss://example/ws',
    token: 'do-not-leak',
    model: 'm',
  });
  FakeWebSocket.instances[0].fail();

  await assert.rejects(pending, (error) => {
    assert.match(error.message, /^Gemini Live connection failed/);
    assert.equal(error.message.includes('do-not-leak'), false);
    return true;
  });
});
