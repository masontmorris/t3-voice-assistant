import assert from "node:assert/strict";
import { test } from "node:test";

async function openPage(t, search = "") {
  const elements = new Map();
  const worklets = new Map();
  const callbacks = new Map();
  const destination = {};
  const gain = { gain: { value: 1 }, connect(target) { this.target = target; }, disconnect() { this.target = null; } };
  function bind(name, value) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { value, configurable: true });
    t.after(() => {
      if (previous) Object.defineProperty(globalThis, name, previous);
      else delete globalThis[name];
    });
  }
  function element() {
    return {
      children: [], classList: { remove() {} }, style: {},
      addEventListener(event, fn) { callbacks.set(this, fn); },
      prepend(line) { this.children.unshift(line); },
    };
  }
  bind("document", {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    createElement: element,
  });
  bind("window", {
    location: { host: "voice.test", protocol: "https:", search },
    localStorage: { getItem: () => "test-secret" },
    setInterval: () => 1,
    clearInterval() {},
  });
  bind("navigator", { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [] }) } });
  bind("AudioContext", class {
    sampleRate = 48000;
    audioWorklet = { addModule: async () => {} };
    destination = destination;
    async resume() {}
    async suspend() {}
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createGain() { return gain; }
  });
  bind("AudioWorkletNode", class {
    port = { postMessage() {} };
    constructor(context, name) { worklets.set(name, this); }
    connect(target) { this.target = target; }
    disconnect() { this.target = null; }
  });
  let socketCreated;
  bind("WebSocket", class {
    constructor() { socketCreated(this); }
    send() {}
    close() {}
    control(message) { this.onmessage({ data: JSON.stringify(message) }); }
  });
  await import(new URL(`./app.js?${t.name}`, import.meta.url));
  async function start() {
    const created = new Promise((resolve) => { socketCreated = resolve; });
    callbacks.get(elements.get("start"))();
    return await created;
  }
  async function close(socket, code, reason) {
    socket.onclose({ code, reason });
    await Promise.resolve();
  }
  return { start, close, elements, worklets, gain, destination };
}

test("a delayed close from an old socket leaves the current call alone", async (t) => {
  const page = await openPage(t);
  const first = await page.start();
  first.onopen();
  await page.close(first, 1000, "");
  const second = await page.start();
  second.onopen();
  first.onclose({ code: 1006, reason: "" });
  await Promise.resolve();
  assert.equal(page.elements.get("status").textContent, "connected");
  assert.equal(page.elements.get("stop").disabled, false);
});

test("the call shows wrong secret and the server's busy reason", async (t) => {
  const page = await openPage(t);
  const first = await page.start();
  await page.close(first, 1008, "bad or missing auth");
  assert.equal(page.elements.get("status").textContent, "wrong secret");
  const second = await page.start();
  await page.close(second, 1013, "another client is connected");
  assert.equal(page.elements.get("status").textContent, "another client is connected");
});

test("capture connects to the destination through a muted gain and disconnects on close", async (t) => {
  const page = await openPage(t);
  const socket = await page.start();
  assert.equal(page.worklets.get("voice-capture").target, page.gain);
  assert.equal(page.gain.gain.value, 0);
  assert.equal(page.gain.target, page.destination);
  await page.close(socket, 1000, "");
  assert.equal(page.gain.target, null);
});

test("pong updates the RTT status without adding a log line", async (t) => {
  const page = await openPage(t);
  const socket = await page.start();
  socket.control({ type: "ready", mode: "vad" });
  socket.control({ type: "pong", t: performance.now() });
  assert.match(page.elements.get("rtt").textContent, /^\d+ ms$/);
  assert.deepEqual(page.elements.get("log").children.map((line) => line.textContent), ["connected, vad mode"]);
});

test("debug events are absent by default", async (t) => {
  await openPage(t);
  assert.equal(window.voiceDebug, undefined);
});

test("debug events are available with the debug query parameter", async (t) => {
  await openPage(t, "?debug");
  assert.equal(window.voiceDebug.events.at(-1).status, "secret saved, press Start");
});
