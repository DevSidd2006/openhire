import test from 'node:test';
import assert from 'node:assert/strict';

import {
  PcmFrameBuffer,
  RmsSpeechGate,
  arrayBufferToBase64,
  base64ToInt16,
  floatToPcm16,
  resampleLinear,
} from '../../pages/js/live-audio-core.js';

test('floatToPcm16 clamps samples and writes little-endian bytes', () => {
  const bytes = new Uint8Array(floatToPcm16(new Float32Array([-2, 0, 2])));
  assert.deepEqual([...bytes], [0, 128, 0, 0, 255, 127]);
});

test('resampleLinear returns stable endpoints at the target rate', () => {
  const source = new Float32Array([0, 0.5, 1, 0.5]);
  const output = resampleLinear(source, 4, 2);

  assert.equal(output.length, 2);
  assert.deepEqual([...output], [0, 1]);
  assert.equal(resampleLinear(source, 4, 4), source);
});

test('frame buffer emits 100 ms frames at 16 kHz and keeps the remainder', () => {
  const buffer = new PcmFrameBuffer(1600);
  assert.equal(buffer.push(new Int16Array(800)).length, 0);

  const frames = buffer.push(new Int16Array(1000).fill(7));
  assert.equal(frames.length, 1);
  assert.equal(frames[0].length, 1600);
  assert.equal(buffer.bufferedSamples, 200);
  assert.equal(buffer.flush().length, 200);
  assert.equal(buffer.bufferedSamples, 0);
});

test('base64 helpers preserve raw little-endian PCM', () => {
  const pcm = new Int16Array([-32768, -3, 0, 32767]);
  const encoded = arrayBufferToBase64(pcm.subarray(1, 3));
  assert.deepEqual([...base64ToInt16(encoded)], [-3, 0]);
});

test('speech gate reports sustained speech within 40 ms', () => {
  const gate = new RmsSpeechGate({
    threshold: 0.02,
    speechFrames: 2,
    silenceFrames: 10,
  });

  assert.equal(gate.push(new Float32Array(320).fill(0.1)), null);
  assert.equal(gate.push(new Float32Array(320).fill(0.1)), 'speech-start');
  assert.equal(gate.isSpeaking, true);
});

test('speech gate reports speech end after sustained silence and can restart', () => {
  const gate = new RmsSpeechGate({ speechFrames: 1, silenceFrames: 2 });

  assert.equal(gate.push(new Float32Array(320).fill(0.2)), 'speech-start');
  assert.equal(gate.push(new Float32Array(320)), null);
  assert.equal(gate.push(new Float32Array(320)), 'speech-end');
  assert.equal(gate.isSpeaking, false);
  assert.equal(gate.push(new Float32Array(320).fill(0.2)), 'speech-start');
});
