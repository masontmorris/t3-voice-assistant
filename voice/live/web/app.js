import { micToServerPCM16, replyToDeviceFloat } from "./resample.js";

// The browser half of the live voice call. Opens the microphone, sends 16 kHz PCM16 like client.py, and
// plays the reply through a jitter buffer that ducks, unducks and drops on barge-in the same way.

const SECRET_KEY = "voice-secret";
const PING_MS = 2000;

const els = {
  secret: document.getElementById("secret"),
  save: document.getElementById("save"),
  start: document.getElementById("start"),
  stop: document.getElementById("stop"),
  modeVad: document.getElementById("mode-vad"),
  modePtt: document.getElementById("mode-ptt"),
  talk: document.getElementById("talk"),
  status: document.getElementById("status"),
  rtt: document.getElementById("rtt"),
  meter: document.getElementById("meter-fill"),
  log: document.getElementById("log"),
};

// Small hook for scripted runs: window.voiceDebug.events holds every control message and state change.
window.voiceDebug = { events: [] };

const state = {
  ws: null,
  context: null,
  micSource: null,
  capture: null,
  playback: null,
  micStream: null,
  mode: "vad",
  running: false,
  replyRate: 0,
  pttDown: false,
  pingAt: 0,
  pingTimer: 0,
  rtts: [],
  status: "idle",
  heard: [],
  said: [],
};

function note(kind, detail) {
  window.voiceDebug.events.push({ at: Date.now(), kind, ...detail });
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
  state.playback = new AudioWorkletNode(state.context, "voice-playback");
  state.playback.port.postMessage({ type: "config", deviceRate });
  state.capture.port.onmessage = (event) => onMic(event.data, deviceRate);
  state.playback.port.onmessage = (event) => {
    if (event.data.type === "drained") note("playback", { drained: true });
    else note("playback", { [event.data.type]: true });
  };
  state.micSource.connect(state.capture);
  state.playback.connect(state.context.destination);
  state.replyRate = 0;
  state.heard = [];
  state.said = [];

  const ws = new WebSocket(wsUrl());
  state.ws = ws;
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    ws.send(`auth ${phrase}`);
    ws.send(JSON.stringify({ type: "hello", mode: state.mode }));
    setStatus("connected");
    state.pingTimer = window.setInterval(() => {
      state.pingAt = performance.now();
      ws.send(JSON.stringify({ type: "ping", t: state.pingAt }));
    }, PING_MS);
  };
  ws.onmessage = (event) => {
    if (typeof event.data === "string") onControl(JSON.parse(event.data));
    else {
      note("wire", { bytes: event.data.byteLength });
      try {
        onReply(event.data, deviceRate);
      } catch (error) {
        note("onreply-err", { message: String((error && error.stack) || error).slice(0, 300) });
      }
    }
  };
  ws.onclose = (event) => {
    note("close", { code: event.code, reason: event.reason });
    if (state.running) void stop(`closed ${event.code}`);
  };
  ws.onerror = () => note("error", {});
  state.running = true;
  els.start.disabled = true;
  els.stop.disabled = false;
  els.talk.hidden = state.mode !== "ptt";
  note("mic", { deviceRate });
}

function onMic(message, deviceRate) {
  if (!state.running || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.ws.send(micToServerPCM16(message.pcm, deviceRate));
  const db = 20 * Math.log10(Math.max(1e-5, message.level));
  els.meter.style.width = `${Math.max(0, Math.min(100, ((db + 60) / 60) * 100))}%`;
}

let feedNoted = false;
function onReply(data, deviceRate) {
  if (state.replyRate <= 0) return;
  state.playback.port.postMessage({
    type: "feed",
    pcm: replyToDeviceFloat(new Uint8Array(data), state.replyRate, deviceRate),
  });
  if (!feedNoted) {
    feedNoted = true;
    note("feed", { bytes: data.byteLength });
  }
}

function onControl(message) {
  note("control", { text: describeControl(message) });
  if (message.type === "pong") {
    const rtt = performance.now() - message.t;
    state.rtts.push(rtt);
    if (state.rtts.length > 20) state.rtts.shift();
    const median = [...state.rtts].sort((a, b) => a - b)[Math.floor(state.rtts.length / 2)];
    els.rtt.textContent = `${Math.round(median)} ms`;
    note("rtt", { median_ms: Math.round(median) });
  } else if (message.type === "ready") {
    setStatus("listening");
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
    if (message.transcript) state.heard.push(message.transcript);
    for (const reply of message.replies || []) state.said.push(reply);
    setStatus(message.cancelled ? "continued" : "listening");
    note("turn_done", { turn: message.turn, transcript: message.transcript || null });
  }
}

function describeControl(message) {
  if (message.type === "turn_done") return `turn ${message.turn} done: ${message.transcript || "(no transcript)"}`;
  if (message.type === "reply_start") return `reply at ${message.rate} Hz`;
  if (message.type === "ready") return `connected, ${message.mode} mode`;
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
  note("stop", { why: why || "button" });
  if (state.pingTimer) window.clearInterval(state.pingTimer);
  state.pingTimer = 0;
  if (state.ws) {
    try {
      state.ws.close();
    } catch {
      // Already closed on the server's side.
    }
    state.ws = null;
  }
  for (const node of [state.micSource, state.capture, state.playback]) {
    if (node) {
      try {
        node.disconnect();
      } catch {
        // Already torn down.
      }
    }
  }
  state.micSource = state.capture = state.playback = null;
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
