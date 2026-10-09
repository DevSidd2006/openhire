class OpenHireLiveAudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.muted = false;
    this.stopped = false;
    this.port.onmessage = (event) => {
      if (event.data?.type === 'set-muted') {
        this.muted = Boolean(event.data.muted);
      } else if (event.data?.type === 'stop') {
        this.stopped = true;
      }
    };
  }

  process(inputs) {
    if (this.stopped) return false;
    const channel = inputs[0]?.[0];
    if (!this.muted && channel?.length) {
      // The Web Audio input buffer is reused after process returns. Copy it
      // before transferring ownership to the main thread.
      const copied = new Float32Array(channel);
      this.port.postMessage(copied, [copied.buffer]);
    }
    return true;
  }
}

registerProcessor('openhire-live-audio', OpenHireLiveAudioProcessor);
