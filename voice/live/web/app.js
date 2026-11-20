import {
  bytesToBase64,
  micIndexAt,
  micToServerPCM16,
  replyToDeviceFloat,
} from "./resample.js";

// The browser half of the live voice call. Opens the microphone, sends 16 kHz PCM16 like client.py, and
// plays the reply through a jitter buffer that ducks, unducks and drops on barge-in the same way. When the server
// offers a recording, it also sends back the audio the output worklet really played (see browser_recording.py).

const SECRET_KEY = "voice-secret";
const PING_MS = 2000;
const SERVER_RATE = 16000;
// Sent microphone blocks kept for placing played audio on the microphone stream; played runs are under 100 ms old.
const MIC_BLOCKS_KEPT = 64;
const DRAIN_WAIT_MS = 300;

const els = {
  secret: document.getElementById("secret"),
  save: document.getElementById("save"),
  start: document.getElementById("start"),
  stop: document.getElementById("stop"),
  modeVad: document.getElementById("mode-vad"),
  modePtt: document.getElementById("mode-ptt"),
  talk: document.getElementById("talk"),
  summariser: document.getElementById("summariser"),
  status: document.getElementById("status"),
  rtt: document.getElementById("rtt"),
  meter: document.getElementById("meter-fill"),
  log: document.getElementById("log"),
};

const debug = new URLSearchParams(window.location.search).has("debug") ? { events: [] } : null;
if (debug) window.voiceDebug = debug;

const state = {
  ws: null,
  context: null,
  micSource: null,
  capture: null,
  captureGain: null,
  playback: null,
  micStream: null,
  mode: "vad",
  running: false,
  replyRate: 0,
  pttDown: false,
  pingTimer: 0,
  rtts: [],
  status: "idle",
  record: false,
  micBlocks: [],
  micSent: 0,
  drained: null,
};

function note(kind, detail) {
  if (debug) {
    debug.events.push({ kind, ...detail });
    if (debug.events.length > 100) debug.events.shift();
  }
  if (kind === "control") {
    const line = document.createElement("div");
    line.textContent = detail.text;
    els.log.prepend(line);
    while (els.log.children.length > 30) els.log.lastChild.remove();
  }
}

function setStatus(value) {
  state.status = value;
  els.status.textContent = value;
  note("status", { status: value });
}

function wsUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/`;
}

function secret() {
  const typed = els.secret.value.trim();
  if (typed) {
    window.localStorage.setItem(SECRET_KEY, typed);
    return typed;
  }
  return window.localStorage.getItem(SECRET_KEY) || "";
}

async function start() {
  if (state.running) return;
  const phrase = secret();
  if (!phrase) {
    setStatus("paste the secret first");
    els.secret.focus();
    return;
  }
  state.running = true;
  els.start.disabled = true;
  note("start", { mode: state.mode });
  // Both halves start inside the tap's gesture task: iOS drops microphone and audio requests made after
  // an await.
  const streamPromise = navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  state.context = state.context || new AudioContext();
  const resumePromise = state.context.resume();
  state.micStream = await streamPromise;
  await resumePromise;
  const deviceRate = state.context.sampleRate;
  await state.context.audioWorklet.addModule("capture-worklet.js");
  await state.context.audioWorklet.addModule("playback-worklet.js");
  state.micSource = state.context.createMediaStreamSource(state.micStream);
  state.capture = new AudioWorkletNode(state.context, "voice-capture");
  state.captureGain = state.context.createGain();
  state.captureGain.gain.value = 0;
  state.playback = new AudioWorkletNode(state.context, "voice-playback");
  state.playback.port.postMessage({ type: "config", deviceRate });
  state.capture.port.onmessage = (event) => onMic(event.data, deviceRate);
  state.playback.port.onmessage = (event) => {
    const { type, frames } = event.data;
    if (type === "drained") {
      note("drained", { frames });
      if (state.status === "speaking") setStatus("listening");
    } else if (type === "played") {
      onPlayed(event.data, deviceRate);
    } else if (type === "recorded" && state.drained) {
      state.drained();
    }
  };
  state.micSource.connect(state.capture);
  // WebKit needs a path from the capture worklet to the destination to process it.
  state.capture.connect(state.captureGain);
  state.captureGain.connect(state.context.destination);
  state.playback.connect(state.context.destination);
  state.replyRate = 0;
  state.record = false;
  state.micBlocks = [];
  state.micSent = 0;

  const ws = new WebSocket(wsUrl());
  state.ws = ws;
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    ws.send(`auth ${phrase}`);
    ws.send(JSON.stringify({ type: "hello", mode: state.mode, record: true }));
    setStatus("connected");
    state.pingTimer = window.setInterval(() => {
      ws.send(JSON.stringify({ type: "ping", t: performance.now() }));
    }, PING_MS);
  };
  ws.onmessage = (event) => {
    if (typeof event.data === "string") onControl(JSON.parse(event.data));
    else onReply(event.data, deviceRate);
  };
  ws.onclose = (event) => {
    if (state.ws !== ws) return;
    note("close", { code: event.code, reason: event.reason });
    void stop(event.code === 1008 ? "wrong secret" : event.reason || `closed ${event.code}`);
  };
  ws.onerror = () => note("error", {});
  els.stop.disabled = false;
  note("mic", { deviceRate });
}

function onMic(message, deviceRate) {
  if (!state.running || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  const bytes = micToServerPCM16(message.pcm, deviceRate);
  state.ws.send(bytes);
  const samples = bytes.length / 2;
  state.micBlocks.push({
    frame: message.frame,
    frames: message.pcm.length,
    before: state.micSent,
    samples,
  });
  if (state.micBlocks.length > MIC_BLOCKS_KEPT) state.micBlocks.shift();
  state.micSent += samples;
  const db = 20 * Math.log10(Math.max(1e-5, message.level));
  els.meter.style.width = `${Math.max(0, Math.min(100, ((db + 60) / 60) * 100))}%`;
}

function onPlayed(message, deviceRate) {
  const ws = state.ws;
  if (!state.record || !ws || ws.readyState !== WebSocket.OPEN) return;
  const at = micIndexAt(state.micBlocks, message.frame);
  if (at === null) return;
  const pcm = bytesToBase64(micToServerPCM16(message.pcm, deviceRate));
  ws.send(JSON.stringify({ type: "played", t: at / SERVER_RATE, pcm }));
}

// Asks the output worklet for the run it still holds, then says goodbye, so the server can tell a hangup from a
// connection that dropped.
async function finishRecording(ws) {
  if (!state.record || ws.readyState !== WebSocket.OPEN) return;
  if (state.playback) {
    const drained = new Promise((resolve) => {
      state.drained = resolve;
    });
    state.playback.port.postMessage({ type: "drain" });
    await Promise.race([drained, new Promise((resolve) => setTimeout(resolve, DRAIN_WAIT_MS))]);
    state.drained = null;
  }
  ws.send(JSON.stringify({ type: "bye" }));
}

function onReply(data, deviceRate) {
  if (state.replyRate <= 0) return;
  state.playback.port.postMessage({
    type: "feed",
    pcm: replyToDeviceFloat(new Uint8Array(data), state.replyRate, deviceRate),
  });
}

function onControl(message) {
  if (message.type === "pong") {
    const rtt = performance.now() - message.t;
    state.rtts.push(rtt);
    if (state.rtts.length > 20) state.rtts.shift();
    const median = [...state.rtts].sort((a, b) => a - b)[Math.floor(state.rtts.length / 2)];
    els.rtt.textContent = `${Math.round(median)} ms`;
    return;
  }
  note("control", { text: describeControl(message) });
  if (message.type === "ready") {
    state.record = !!message.record;
    state.playback?.port.postMessage({ type: "record", on: state.record });
    els.talk.hidden = state.mode !== "ptt";
    setStatus("listening");
    showSummarisers(message);
  } else if (message.type === "summariser") {
    showSummarisers(message);
  } else if (message.type === "turn_start") {
    setStatus("listening");
  } else if (message.type === "turn_end") {
    setStatus("thinking");
  } else if (message.type === "reply_start") {
    if (message.rate !== state.replyRate) {
      state.playback.port.postMessage({ type: "flush" });
      state.replyRate = message.rate;
    }
    setStatus("speaking");
  } else if (message.type === "reply_end") {
    state.playback.port.postMessage({ type: "feed", pcm: new Float32Array(0), end: true });
  } else if (message.type === "duck") {
    state.playback.port.postMessage({ type: "duck" });
  } else if (message.type === "unduck") {
    state.playback.port.postMessage({ type: "unduck" });
  } else if (message.type === "barge_in") {
    state.playback.port.postMessage({ type: "flush" });
    setStatus("listening");
  } else if (message.type === "turn_done") {
    if (state.status !== "speaking") setStatus("listening");
    note("turn_done", { turn: message.turn });
  }
}

// The server says which models can write thread summaries and which one is in use; the choice is the server's, so
// it holds for every device and survives a reload.
function showSummarisers(message) {
  const options = message.summarisers || [];
  els.summariser.replaceChildren(
    ...options.map((option) => {
      const el = document.createElement("option");
      el.value = option.id;
      el.textContent = option.label;
      return el;
    }),
  );
  els.summariser.value = message.summariser || "";
  els.summariser.disabled = options.length < 2;
}

function describeControl(message) {
  if (message.type === "turn_done") return `turn ${message.turn} done`;
  if (message.type === "reply_start") return `reply at ${message.rate} Hz`;
  if (message.type === "ready") return `connected, ${message.mode} mode`;
  if (message.type === "summariser")
    return message.ok ? `summaries by ${message.summariser}` : "that model is not offered";
  return message.type;
}

function setPtt(down) {
  if (!state.running || state.mode !== "ptt" || state.pttDown === down) return;
  state.pttDown = down;
  state.ws.send(JSON.stringify({ type: "ptt", down }));
  els.talk.classList.toggle("down", down);
  note("ptt", { down });
}

async function stop(why) {
  // Runs for a failed start too, so a start that throws still releases the microphone and says why.
  state.running = false;
  state.pttDown = false;
  els.talk.classList.remove("down");
  note("stop", { why: why || "button" });
  if (state.pingTimer) window.clearInterval(state.pingTimer);
  state.pingTimer = 0;
  if (state.ws) {
    await finishRecording(state.ws);
    try {
      state.ws.close();
    } catch {
      // Already closed on the server's side.
    }
    state.ws = null;
  }
  for (const node of [state.micSource, state.capture, state.captureGain, state.playback]) {
    if (node) {
      try {
        node.disconnect();
      } catch {
        // Already torn down.
      }
    }
  }
  state.micSource = state.capture = state.captureGain = state.playback = null;
  if (state.micStream) for (const track of state.micStream.getTracks()) track.stop();
  state.micStream = null;
  if (state.context) await state.context.suspend();
  els.meter.style.width = "0%";
  els.start.disabled = false;
  els.stop.disabled = true;
  els.talk.hidden = true;
  setStatus(why || "stopped");
}

function setMode(mode) {
  if (state.running) return;
  state.mode = mode;
  els.modeVad.classList.toggle("on", mode === "vad");
  els.modePtt.classList.toggle("on", mode === "ptt");
  note("mode", { mode });
}

els.start.addEventListener("click", () => void start().catch((error) => void stop(String(error))));
els.stop.addEventListener("click", () => void stop("button"));
els.save.addEventListener("click", () => {
  secret();
  els.secret.value = "";
  setStatus(secret() ? "secret saved" : "idle");
});
els.summariser.addEventListener("change", () => {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.ws.send(JSON.stringify({ type: "summariser", id: els.summariser.value }));
});
els.modeVad.addEventListener("click", () => setMode("vad"));
els.modePtt.addEventListener("click", () => setMode("ptt"));
els.talk.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  setPtt(true);
});
for (const end of ["pointerup", "pointerleave", "pointercancel"]) {
  els.talk.addEventListener(end, () => setPtt(false));
}
els.secret.value = "";
if (window.localStorage.getItem(SECRET_KEY)) setStatus("secret saved, press Start");
