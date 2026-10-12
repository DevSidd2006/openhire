let initialized = false;

export function initializeLegacyInterview() {
  if (initialized) return;
  initialized = true;

  const urlParams = new URLSearchParams(window.location.search);
  const sessionId = urlParams.get('session_id');
  let ws = null, stream = null, audioCtx = null, node = null, source = null;
  let pcmChunks = [], capturing = false, sourceRate = 48000;
  const TARGET_RATE = 16000;

  function downsampleTo16k(input, inputRate) {
    if (inputRate === TARGET_RATE) return input;
    const ratio = inputRate / TARGET_RATE;
    const outLength = Math.floor(input.length / ratio);
    const out = new Float32Array(outLength);
    for (let i = 0; i < outLength; i++) {
      const start = Math.floor(i * ratio);
      const end = Math.min(Math.floor((i + 1) * ratio), input.length);
      let sum = 0, n = 0;
      for (let j = start; j < end; j++) { sum += input[j]; n++; }
      out[i] = n ? sum / n : 0;
    }
    return out;
  }

  function encodeWav(float32, sampleRate) {
    const bytesPerSample = 2;
    const dataSize = float32.length * bytesPerSample;
    const buf = new ArrayBuffer(44 + dataSize);
    const view = new DataView(buf);
    const w = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    w(0, "RIFF");
    view.setUint32(4, 36 + dataSize, true);
    w(8, "WAVE");
    w(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * bytesPerSample, true);
    view.setUint16(32, bytesPerSample, true);
    view.setUint16(34, 16, true);
    w(36, "data");
    view.setUint32(40, dataSize, true);
    let off = 44;
    for (let i = 0; i < float32.length; i++, off += 2) {
      const s = Math.max(-1, Math.min(1, float32[i]));
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return buf;
  }

  function b64(buf) {
    let s = "", bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }

  let lastAudioBlobUrl = null;

  // Toggles the expanding-wave animation (see .avatar.speaking in <style>)
  // around whichever avatar is currently producing sound/speech.
  function setSpeaking(who, on) {
    document.querySelector('.avatar.' + who).classList.toggle('speaking', on);
  }

  function playAudioEl(url) {
    const audio = new Audio(url);
    audio.addEventListener('ended', () => setSpeaking('ai', false));
    audio.addEventListener('pause', () => setSpeaking('ai', false));
    return audio;
  }

  function play(b64audio, format) {
    if (!b64audio) return;
    const bin = atob(b64audio);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const blob = new Blob([arr], { type: format === "mp3" ? "audio/mpeg" : "audio/wav" });
    lastAudioBlobUrl = URL.createObjectURL(blob);
    setSpeaking('ai', true);
    playAudioEl(lastAudioBlobUrl).play().catch(() => {
      // The browser blocked this specific playback (e.g. it decided the page
      // hasn't "really" been interacted with recently enough) - show a
      // visible, clickable way to hear it instead of failing silently.
      console.log('[voice] Autoplay blocked by browser - showing replay button');
      setSpeaking('ai', false);
      document.getElementById('replayBtn').style.display = 'inline-flex';
    });
  }

  document.getElementById('replayBtn').onclick = () => {
    if (!lastAudioBlobUrl) return;
    setSpeaking('ai', true);
    playAudioEl(lastAudioBlobUrl).play().catch((e) => {
      console.error('[voice] Replay failed:', e);
      setSpeaking('ai', false);
    });
    document.getElementById('replayBtn').style.display = 'none';
  };

  function updateQuestion(text) {
    document.getElementById('questionBar').textContent = text || 'Loading question...';
  }

  function addTranscriptEntry(speaker, text) {
    if (!text) return;
    const log = document.getElementById('transcriptLog');
    const entry = document.createElement('div');
    entry.style.marginBottom = '1rem';
    const label = document.createElement('div');
    label.textContent = speaker;
    label.style.cssText = 'font-size:11px; font-weight:500; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:0.25rem; font-family:var(--font-sans); color:' + (speaker === 'Interviewer' ? 'var(--ink)' : 'var(--success-text)');
    const body = document.createElement('div');
    body.textContent = text;
    body.style.cssText = 'font-size:13px; font-family:var(--font-sans); color:var(--ink-3); line-height:1.4;';
    entry.appendChild(label);
    entry.appendChild(body);
    log.appendChild(entry);
    // #transcriptLog itself doesn't scroll - #transcriptPanel (its parent)
    // is the actual overflow:auto container. requestAnimationFrame ensures
    // the new entry has been laid out before we measure scrollHeight.
    const panel = document.getElementById('transcriptPanel');
    requestAnimationFrame(() => { panel.scrollTop = panel.scrollHeight; });
  }

  function updateAIStatus(text) {
    document.getElementById('aiStatus').textContent = text;
  }

  function updateUserStatus(text) {
    document.getElementById('userStatus').textContent = text;
  }

  function connectToSession(id) {
    if (!id) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const backendHost = window.OPENHIRE_API_HOST || location.host;
    const wsUrl = `${proto}://${backendHost}/ws/sessions/${encodeURIComponent(id)}`;

    console.log('[voice] Connecting to:', wsUrl);
    ws = new WebSocket(wsUrl);

    ws.onopen = async () => {
      console.log('[voice] Connected');
      updateAIStatus('Listening');
      ws.send(JSON.stringify({ type: "speak_question" }));
    };

    ws.onclose = () => {
      console.log('[voice] Disconnected');
      updateAIStatus('Disconnected');
    };

    ws.onerror = (err) => {
      console.error('[voice] Error:', err);
      updateAIStatus('Connection error');
    };

    ws.onmessage = async (event) => {
      try {
        const msg = JSON.parse(event.data);

        if (msg.type === "error") {
          console.error('[voice] Server error:', msg.error);
          // Voice (TTS) can fail independently of the question itself being
          // ready - fall back to the question's text over REST so the page
          // never gets stuck on "Loading interview question..." just because
          // audio failed.
          try {
            const state = await apiRequest(`/sessions/${encodeURIComponent(sessionId)}`);
            if (state.current_question) {
              updateQuestion(state.current_question.question_text);
              updateAIStatus('Listening (audio unavailable)');
              updateUserStatus('Mic on');
              addTranscriptEntry('Interviewer', state.current_question.question_text);
            }
          } catch (fetchErr) {
            console.error('[voice] Fallback question fetch failed:', fetchErr);
          }
          return;
        }

        if (msg.transcript) {
          updateUserStatus('Responded');
          addTranscriptEntry('You', msg.transcript);
        }

        if (msg.next_question) {
          updateQuestion(msg.next_question.question_text);
          updateAIStatus('Listening');
          updateUserStatus('Mic on');
          addTranscriptEntry('Interviewer', msg.next_question.question_text);
        } else if (msg.next_question_text) {
          // The initial speak_question reply (and any reconnect replay) never
          // advances the interview, so next_question above is always null -
          // the question text only ever arrives via this field.
          updateQuestion(msg.next_question_text);
          updateAIStatus('Listening');
          updateUserStatus('Mic on');
          addTranscriptEntry('Interviewer', msg.next_question_text);
        }

        if (msg.status === "sealed") {
          updateQuestion('Interview completed. Finalizing evaluation...');
          updateAIStatus('Completed');
          setTimeout(() => {
            const appId = new URLSearchParams(window.location.search).get('app_id') || '';
            window.location.href = `report.html?session_id=${encodeURIComponent(sessionId)}&app_id=${encodeURIComponent(appId)}`;
          }, 1500);
          return;
        }

        play(msg.question_audio_base64, msg.audio_format);
      } catch (e) {
        console.error('[voice] Parse error:', e);
      }
    };
  }

  // Browser-native speech recognition (Web Speech API) is the primary
  // capture path: it transcribes in the browser and sends plain text over
  // the "answer_text" message, which the server already supports
  // (api/routes/voice.py) exactly like a server-transcribed audio answer -
  // same submit_text_answer()/InterviewSessionRunner.submit_answer() path,
  // same evaluation. Falls back to raw audio capture (server-side STT) only
  // when the browser has no SpeechRecognition (e.g. Firefox).
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition = null;
  let recognizedText = '';

  async function startRecording() {
    if (!ws || ws.readyState !== WebSocket.OPEN || capturing) return;
    if (SpeechRec) return startRecordingBrowserStt();
    return startRecordingServerAudio();
  }

  function stopRecording() {
    if (!capturing) return;
    if (recognition) return stopRecordingBrowserStt();
    return stopRecordingServerAudio();
  }

  function startRecordingBrowserStt() {
    capturing = true;
    updateMicButton();
    recognizedText = '';
    recognition = new SpeechRec();
    recognition.lang = 'en-US';
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.onresult = (event) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        if (event.results[i].isFinal) {
          recognizedText += (recognizedText ? ' ' : '') + event.results[i][0].transcript;
        }
      }
    };
    recognition.onerror = (e) => {
      console.error('[voice] SpeechRecognition error:', e.error);
    };
    recognition.onend = () => {
      recognition = null;
      const text = recognizedText.trim();
      if (!text) {
        updateUserStatus('Mic on');
        return;
      }
      updateAIStatus('Evaluating');
      updateUserStatus('Sending...');
      ws.send(JSON.stringify({
        type: "answer_text",
        transcript: text,
        utterance_id: crypto.randomUUID(),
      }));
    };
    try {
      recognition.start();
    } catch (e) {
      console.error('[voice] SpeechRecognition failed to start:', e);
      capturing = false;
      updateMicButton();
      recognition = null;
      updateUserStatus('Mic denied');
      return;
    }
    updateUserStatus('Speaking');
    updateAIStatus('Listening');
  }

  function stopRecordingBrowserStt() {
    capturing = false;
    updateMicButton();
    // recognition.onend fires asynchronously and sends the transcript.
    recognition.stop();
  }

  async function startRecordingServerAudio() {
    try {
      stream = stream || await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (e) {
      console.error('[voice] Microphone denied:', e);
      updateUserStatus('Mic denied');
      return;
    }

    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") await audioCtx.resume();
    sourceRate = audioCtx.sampleRate;

    pcmChunks = [];
    capturing = true;
    updateMicButton();
    source = audioCtx.createMediaStreamSource(stream);
    node = audioCtx.createScriptProcessor(4096, 1, 1);
    node.onaudioprocess = (e) => {
      if (!capturing) return;
      pcmChunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    };
    source.connect(node);
    const mute = audioCtx.createGain();
    mute.gain.value = 0;
    node.connect(mute);
    mute.connect(audioCtx.destination);

    updateUserStatus('Speaking');
    updateAIStatus('Listening');
  }

  function stopRecordingServerAudio() {
    capturing = false;
    updateMicButton();
    try { source && source.disconnect(); node && node.disconnect(); } catch (e) {}

    const total = pcmChunks.reduce((n, c) => n + c.length, 0);
    if (!total) {
      updateUserStatus('Mic on');
      return;
    }

    const merged = new Float32Array(total);
    let off = 0;
    for (const c of pcmChunks) { merged.set(c, off); off += c.length; }

    const wav = encodeWav(downsampleTo16k(merged, sourceRate), TARGET_RATE);
    updateAIStatus('Evaluating');
    updateUserStatus('Sending...');

    ws.send(JSON.stringify({
      type: "answer",
      audio_base64: b64(wav),
      audio_format: "wav",
      utterance_id: crypto.randomUUID(),
    }));
  }

  // Mic on/off toggle (replaces press-and-hold, which was too easy to
  // interrupt accidentally - a stray mouseleave/touch-cancel would abort
  // SpeechRecognition mid-sentence).
  function updateMicButton() {
    const btn = document.getElementById('micBtn');
    btn.classList.toggle('recording', capturing);
    btn.textContent = capturing ? 'Tap to stop' : 'Tap to speak';
    setSpeaking('user', capturing);
  }

  document.getElementById('micBtn').onclick = () => {
    if (capturing) {
      stopRecording();
    } else {
      startRecording();
    }
  };

  // Load session on page load
  document.getElementById('startBtn').onclick = async () => {
    document.getElementById('startOverlay').style.display = 'none';
    // Ask for the mic up front, inside this click, rather than waiting for
    // the candidate's first press-and-hold - getUserMedia is what actually
    // triggers the browser's permission prompt (SpeechRecognition draws on
    // the same microphone permission but won't prompt proactively on its
    // own). The stream itself isn't needed yet, so it's stopped immediately;
    // startRecordingServerAudio() (the no-SpeechRecognition fallback path)
    // re-acquires its own stream when actually recording.
    try {
      const probe = await navigator.mediaDevices.getUserMedia({ audio: true });
      probe.getTracks().forEach(t => t.stop());
    } catch (e) {
      console.error('[voice] Microphone permission denied:', e);
      updateUserStatus('Mic denied');
    }
    if (sessionId) {
      connectToSession(sessionId);
    } else {
      updateQuestion('No session ID specified.');
    }
  };

  window.addEventListener('load', () => {
    if (!sessionId) {
      document.getElementById('startOverlay').style.display = 'none';
      updateQuestion('No session ID specified.');
    }
  });

  // Override end interview button
  document.getElementById('endBtn').onclick = async () => {
    const appId = new URLSearchParams(window.location.search).get('app_id') || '';
    try {
      await apiRequest(`/sessions/${encodeURIComponent(sessionId)}/finish`, { method: 'POST' });
    } catch (e) {
      console.error('Error finishing session:', e);
    }
    setTimeout(() => {
      window.location.href = `report.html?session_id=${encodeURIComponent(sessionId)}&app_id=${encodeURIComponent(appId)}`;
    }, 500);
  };
}
