/**
 * Browser-independent audio helpers for the realtime interview transport.
 * PCM returned by this module is signed 16-bit, little-endian audio.
 */

export function resampleLinear(samples, sourceRate, targetRate = 16000) {
  if (!(samples instanceof Float32Array)) {
    throw new TypeError('samples must be a Float32Array');
  }
  if (!Number.isFinite(sourceRate) || sourceRate <= 0 ||
      !Number.isFinite(targetRate) || targetRate <= 0) {
    throw new RangeError('sample rates must be positive numbers');
  }
  if (sourceRate === targetRate || samples.length === 0) return samples;

  const outputLength = Math.max(1, Math.round(samples.length * targetRate / sourceRate));
  const output = new Float32Array(outputLength);
  const sourceStep = sourceRate / targetRate;

  for (let index = 0; index < outputLength; index += 1) {
    const sourcePosition = index * sourceStep;
    const leftIndex = Math.min(Math.floor(sourcePosition), samples.length - 1);
    const rightIndex = Math.min(leftIndex + 1, samples.length - 1);
    const fraction = sourcePosition - leftIndex;
    output[index] = samples[leftIndex] +
      ((samples[rightIndex] - samples[leftIndex]) * fraction);
  }
  return output;
}

export function floatToPcm16(samples) {
  if (!(samples instanceof Float32Array)) {
    throw new TypeError('samples must be a Float32Array');
  }

  const output = new ArrayBuffer(samples.length * 2);
  const view = new DataView(output);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    const integer = sample < 0
      ? Math.round(sample * 32768)
      : Math.round(sample * 32767);
    view.setInt16(index * 2, integer, true);
  }
  return output;
}

function asUint8Array(value) {
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) {
    return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  }
  throw new TypeError('audio data must be an ArrayBuffer or typed array');
}

export function arrayBufferToBase64(value) {
  const bytes = asUint8Array(value);
  if (typeof Buffer !== 'undefined') {
    return Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).toString('base64');
  }

  // Convert in chunks to avoid exceeding the argument/string limits on long
  // realtime sessions. Individual audio frames remain intentionally small.
  const chunkSize = 0x8000;
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    const chunk = bytes.subarray(offset, Math.min(offset + chunkSize, bytes.length));
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

export function base64ToInt16(encoded) {
  if (typeof encoded !== 'string') throw new TypeError('encoded audio must be a string');
  const bytes = typeof Buffer !== 'undefined'
    ? new Uint8Array(Buffer.from(encoded, 'base64'))
    : Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0));
  if (bytes.byteLength % 2 !== 0) {
    throw new RangeError('PCM16 audio must contain an even number of bytes');
  }

  const output = new Int16Array(bytes.byteLength / 2);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let index = 0; index < output.length; index += 1) {
    output[index] = view.getInt16(index * 2, true);
  }
  return output;
}

export class PcmFrameBuffer {
  constructor(frameSamples) {
    if (!Number.isInteger(frameSamples) || frameSamples <= 0) {
      throw new RangeError('frameSamples must be a positive integer');
    }
    this.frameSamples = frameSamples;
    this.pending = new Int16Array(frameSamples);
    this.bufferedSamples = 0;
  }

  push(samples) {
    if (!(samples instanceof Int16Array)) {
      throw new TypeError('samples must be an Int16Array');
    }

    const frames = [];
    let sourceOffset = 0;
    while (sourceOffset < samples.length) {
      const copyLength = Math.min(
        this.frameSamples - this.bufferedSamples,
        samples.length - sourceOffset,
      );
      this.pending.set(samples.subarray(sourceOffset, sourceOffset + copyLength), this.bufferedSamples);
      this.bufferedSamples += copyLength;
      sourceOffset += copyLength;

      if (this.bufferedSamples === this.frameSamples) {
        frames.push(this.pending);
        this.pending = new Int16Array(this.frameSamples);
        this.bufferedSamples = 0;
      }
    }
    return frames;
  }

  flush() {
    if (this.bufferedSamples === 0) return new Int16Array(0);
    const remainder = this.pending.slice(0, this.bufferedSamples);
    this.pending.fill(0);
    this.bufferedSamples = 0;
    return remainder;
  }

  clear() {
    this.pending.fill(0);
    this.bufferedSamples = 0;
  }
}

export class RmsSpeechGate {
  constructor({ threshold = 0.02, speechFrames = 2, silenceFrames = 35 } = {}) {
    if (!Number.isFinite(threshold) || threshold < 0) {
      throw new RangeError('threshold must be a non-negative number');
    }
    if (!Number.isInteger(speechFrames) || speechFrames <= 0 ||
        !Number.isInteger(silenceFrames) || silenceFrames <= 0) {
      throw new RangeError('speechFrames and silenceFrames must be positive integers');
    }
    this.threshold = threshold;
    this.speechFrames = speechFrames;
    this.silenceFrames = silenceFrames;
    this.isSpeaking = false;
    this.aboveThresholdFrames = 0;
    this.silentFrames = 0;
  }

  push(frame) {
    if (!(frame instanceof Float32Array)) {
      throw new TypeError('frame must be a Float32Array');
    }

    let squareSum = 0;
    for (let index = 0; index < frame.length; index += 1) {
      squareSum += frame[index] * frame[index];
    }
    const rms = frame.length === 0 ? 0 : Math.sqrt(squareSum / frame.length);

    if (rms >= this.threshold) {
      this.silentFrames = 0;
      if (this.isSpeaking) return null;
      this.aboveThresholdFrames += 1;
      if (this.aboveThresholdFrames >= this.speechFrames) {
        this.isSpeaking = true;
        this.aboveThresholdFrames = 0;
        return 'speech-start';
      }
      return null;
    }

    this.aboveThresholdFrames = 0;
    if (!this.isSpeaking) return null;
    this.silentFrames += 1;
    if (this.silentFrames >= this.silenceFrames) {
      this.isSpeaking = false;
      this.silentFrames = 0;
      return 'speech-end';
    }
    return null;
  }

  reset() {
    this.isSpeaking = false;
    this.aboveThresholdFrames = 0;
    this.silentFrames = 0;
  }
}
