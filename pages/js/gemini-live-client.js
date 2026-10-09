import { arrayBufferToBase64 } from './live-audio-core.js';

const NOOP = () => {};

/** A small adapter around Gemini Live's raw WebSocket protocol. */
export class GeminiLiveClient {
  constructor({
    WebSocketImpl = globalThis.WebSocket,
    onAudio = NOOP,
    onInputTranscript = NOOP,
    onOutputTranscript = NOOP,
    onInterrupted = NOOP,
    onResumptionHandle = NOOP,
    onGoAway = NOOP,
    onToolCall = NOOP,
    onState = NOOP,
  } = {}) {
    if (!WebSocketImpl) throw new Error('WebSocket is unavailable');
    this.WebSocketImpl = WebSocketImpl;
    this.callbacks = {
      onAudio,
      onInputTranscript,
      onOutputTranscript,
      onInterrupted,
      onResumptionHandle,
      onGoAway,
      onToolCall,
      onState,
    };
    this.socket = null;
    this.outputText = '';
  }

  async connect({
    websocketUrl,
    token,
    model,
    resumeHandle = null,
    enableSessionResumption = true,
  }) {
    if (!websocketUrl || !token || !model) {
      throw new Error('Gemini Live connection details are incomplete');
    }
    if (this.socket && this.socket.readyState !== (this.WebSocketImpl.CLOSED ?? 3)) {
      this.close();
    }

    this.outputText = '';
    this.callbacks.onState('connecting');
    // The ephemeral token is used only here. Do not expose this URL through
    // callbacks, logs, DOM state, or error text.
    const separator = websocketUrl.includes('?') ? '&' : '?';
    const authenticatedUrl = `${websocketUrl}${separator}access_token=${encodeURIComponent(token)}`;
    const socket = new this.WebSocketImpl(authenticatedUrl);
    this.socket = socket;

    await new Promise((resolve, reject) => {
      let settled = false;
      const rejectConnection = (detail = '') => {
        if (settled) return;
        settled = true;
        reject(new Error(
          detail ? `Gemini Live connection failed: ${detail}` : 'Gemini Live connection failed',
        ));
      };

      socket.onerror = () => {
        this.callbacks.onState('error');
        // WebSocket errors are intentionally opaque in browsers. The close
        // event follows with Gemini's protocol code/reason and rejects there.
      };
      socket.onclose = (event) => {
        if (this.socket === socket) this.callbacks.onState('closed');
        rejectConnection(event?.reason || (event?.code ? `WebSocket closed (${event.code})` : ''));
      };
      socket.onmessage = async (event) => {
        let message;
        try {
          let payload = event.data;
          if (payload instanceof Blob) payload = await payload.text();
          else if (payload instanceof ArrayBuffer) payload = new TextDecoder().decode(payload);
          if (typeof payload !== 'string') throw new TypeError('unsupported WebSocket frame');
          message = JSON.parse(payload);
        } catch (error) {
          rejectConnection(`Invalid server message: ${error.message}`);
          return;
        }

        if (message.setupComplete && !settled) {
          settled = true;
          this.callbacks.onState('connected');
          resolve();
        }
        this.#handle(message);
      };
      socket.onopen = () => {
        try {
          const setup = {
            model: model.startsWith('models/') ? model : `models/${model}`,
            generationConfig: { responseModalities: ['AUDIO'] },
          };
          if (enableSessionResumption) {
            setup.sessionResumption = resumeHandle ? { handle: resumeHandle } : {};
          }
          this.#send({ setup });
        } catch (error) {
          rejectConnection(`Could not send setup: ${error.message}`);
        }
      };
    });
  }

  sendPcm16(audio) {
    this.#send({
      realtimeInput: {
        audio: {
          data: arrayBufferToBase64(audio),
          mimeType: 'audio/pcm;rate=16000',
        },
      },
    });
  }

  sendPrivateText(text) {
    this.#send({
      clientContent: {
        turns: [{ role: 'user', parts: [{ text }] }],
        turnComplete: true,
      },
    });
  }

  sendToolResponse(id, name, response) {
    this.#send({
      toolResponse: {
        functionResponses: [{ id, name, response }],
      },
    });
  }

  endAudioStream() {
    this.#send({ realtimeInput: { audioStreamEnd: true } });
  }

  close() {
    const socket = this.socket;
    this.socket = null;
    this.outputText = '';
    if (socket && socket.readyState !== (this.WebSocketImpl.CLOSED ?? 3)) {
      socket.close(1000, 'client complete');
    }
  }

  #send(message) {
    const openState = this.WebSocketImpl.OPEN ?? 1;
    if (!this.socket || this.socket.readyState !== openState) {
      throw new Error('Gemini Live socket is not open');
    }
    this.socket.send(JSON.stringify(message));
  }

  #handle(message) {
    const content = message.serverContent;
    if (content?.inputTranscription?.text) {
      this.callbacks.onInputTranscript(content.inputTranscription.text, true);
    }
    if (content?.outputTranscription?.text) {
      this.outputText += content.outputTranscription.text;
    }
    for (const part of content?.modelTurn?.parts || []) {
      if (part.inlineData?.data) {
        this.callbacks.onAudio(part.inlineData.data, part.inlineData.mimeType);
      }
    }

    if (content?.interrupted) {
      if (this.outputText) this.callbacks.onOutputTranscript(this.outputText, true);
      this.outputText = '';
      this.callbacks.onInterrupted();
    } else if (content?.turnComplete) {
      if (this.outputText) this.callbacks.onOutputTranscript(this.outputText, false);
      this.outputText = '';
    }

    if (
      message.sessionResumptionUpdate?.resumable &&
      message.sessionResumptionUpdate.newHandle
    ) {
      this.callbacks.onResumptionHandle(message.sessionResumptionUpdate.newHandle);
    }
    if (message.goAway) {
      this.callbacks.onGoAway(message.goAway.timeLeft);
    }
    for (const call of message.toolCall?.functionCalls || []) {
      this.callbacks.onToolCall(call);
    }
  }
}
