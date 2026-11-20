import * as NodeAssert from "node:assert/strict";
import * as NodeTest from "node:test";

async function openPage(t, search = "") {
  const elements = new Map();
  const worklets = new Map();
  const callbacks = new Map();
  const destination = {};
  const gain = {
    gain: { value: 1 },
    connect(target) {
      this.target = target;
    },
    disconnect() {
      this.target = null;
    },
  };
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
      children: [],
      classList: { remove() {} },
      style: {},
      addEventListener(event, fn) {
        callbacks.set(this, fn);
      },
      prepend(line) {
        this.children.unshift(line);
      },
      replaceChildren(...items) {
        this.children = items;
      },
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
  bind(
    "AudioContext",
    class {
      sampleRate = 48000;
      audioWorklet = { addModule: async () => {} };
      destination = destination;
      async resume() {}
      async suspend() {}
      createMediaStreamSource() {
        return { connect() {}, disconnect() {} };
      }
      createGain() {
        return gain;
      }
    },
  );
  bind(
    "AudioWorkletNode",
    class {
      posted = [];
      port = {
        postMessage: (message) => {
          this.posted.push(message);
          if (message.type === "drain") this.port.onmessage({ data: { type: "recorded" } });
        },
      };
      constructor(context, name) {
        worklets.set(name, this);
      }
      connect(target) {
        this.target = target;
      }
      disconnect() {
        this.target = null;
      }
    },
  );
  let socketCreated;
  bind(
    "WebSocket",
    class {
      static OPEN = 1;
      readyState = 1;
      sent = [];
      constructor() {
        socketCreated(this);
      }
      send(data) {
        this.sent.push(data);
      }
      close() {
        this.readyState = 3;
      }
      control(message) {
        this.onmessage({ data: JSON.stringify(message) });
      }
    },
  );
  await import(new URL(`./app.js?${t.name}`, import.meta.url));
  async function start() {
    const created = new Promise((resolve) => {
      socketCreated = resolve;
    });
    callbacks.get(elements.get("start"))();
    return await created;
  }
  async function close(socket, code, reason) {
    socket.readyState = 3;
    socket.onclose({ code, reason });
    await Promise.resolve();
  }
  return { start, close, elements, worklets, gain, destination, callbacks };
}

NodeTest.test("a delayed close from an old socket leaves the current call alone", async (t) => {
  const page = await openPage(t);
  const first = await page.start();
  first.onopen();
  await page.close(first, 1000, "");
  const second = await page.start();
  second.onopen();
  first.onclose({ code: 1006, reason: "" });
  await Promise.resolve();
  NodeAssert.equal(page.elements.get("status").textContent, "connected");
  NodeAssert.equal(page.elements.get("stop").disabled, false);
});

NodeTest.test("the call shows wrong secret and the server's busy reason", async (t) => {
  const page = await openPage(t);
  const first = await page.start();
  await page.close(first, 1008, "bad or missing auth");
  NodeAssert.equal(page.elements.get("status").textContent, "wrong secret");
  const second = await page.start();
  await page.close(second, 1013, "another client is connected");
  NodeAssert.equal(page.elements.get("status").textContent, "another client is connected");
});

NodeTest.test(
  "capture connects to the destination through a muted gain and disconnects on close",
  async (t) => {
    const page = await openPage(t);
    const socket = await page.start();
    NodeAssert.equal(page.worklets.get("voice-capture").target, page.gain);
    NodeAssert.equal(page.gain.gain.value, 0);
    NodeAssert.equal(page.gain.target, page.destination);
    await page.close(socket, 1000, "");
    NodeAssert.equal(page.gain.target, null);
  },
);

NodeTest.test(
  "the picker lists the server's summary models and sends the one chosen",
  async (t) => {
    const page = await openPage(t);
    const socket = await page.start();
    socket.onopen();
    const picker = page.elements.get("summariser");
    const offered = [
      { id: "cloud-a", label: "A (cloud)" },
      { id: "local-b", label: "B (local)" },
    ];
    socket.control({ type: "ready", mode: "vad", summarisers: offered, summariser: "cloud-a" });
    NodeAssert.deepEqual(
      picker.children.map((option) => [option.value, option.textContent]),
      [
        ["cloud-a", "A (cloud)"],
        ["local-b", "B (local)"],
      ],
    );
    NodeAssert.equal(picker.value, "cloud-a");
    NodeAssert.equal(picker.disabled, false);
    picker.value = "local-b";
    page.callbacks.get(picker)();
    NodeAssert.deepEqual(JSON.parse(socket.sent.at(-1)), { type: "summariser", id: "local-b" });
    socket.control({ type: "summariser", ok: true, summarisers: offered, summariser: "local-b" });
    NodeAssert.equal(picker.value, "local-b");
    socket.control({
      type: "ready",
      mode: "vad",
      summarisers: offered.slice(0, 1),
      summariser: "cloud-a",
    });
    NodeAssert.equal(picker.disabled, true, "one model is nothing to choose between");
  },
);

NodeTest.test("pong updates the RTT status without adding a log line", async (t) => {
  const page = await openPage(t);
  const socket = await page.start();
  socket.control({ type: "ready", mode: "vad" });
  socket.control({ type: "pong", t: 0 });
  NodeAssert.match(page.elements.get("rtt").textContent, /^\d+ ms$/);
  NodeAssert.deepEqual(
    page.elements.get("log").children.map((line) => line.textContent),
    ["connected, vad mode"],
  );
});

NodeTest.test("debug events are absent by default", async (t) => {
  await openPage(t);
  NodeAssert.equal(window.voiceDebug, undefined);
});

NodeTest.test("debug events are available with the debug query parameter", async (t) => {
  await openPage(t, "?debug");
  NodeAssert.equal(window.voiceDebug.events.at(-1).status, "secret saved, press Start");
});

async function callWithRecording(t, record) {
  const page = await openPage(t);
  const socket = await page.start();
  socket.onopen();
  socket.control({ type: "ready", mode: "vad", record });
  const mic = (frame) =>
    page.worklets.get("voice-capture").port.onmessage({
      data: { type: "mic", pcm: new Float32Array(2048), level: 0, frame },
    });
  const played = (frame, samples) =>
    page.worklets.get("voice-playback").port.onmessage({
      data: { type: "played", frame, pcm: new Float32Array(samples).fill(0.5) },
    });
  const sent = () => socket.sent.filter((data) => typeof data === "string" && data.startsWith("{")).map((text) => JSON.parse(text));
  return { page, socket, mic, played, sent };
}

NodeTest.test("hello asks for a recording and the page tells the output worklet the answer", async (t) => {
  const call = await callWithRecording(t, true);
  NodeAssert.equal(call.sent().find((message) => message.type === "hello").record, true);
  NodeAssert.deepEqual(call.page.worklets.get("voice-playback").posted.at(-1), { type: "record", on: true });
});

NodeTest.test("played audio goes back on the microphone stream's timeline", async (t) => {
  const call = await callWithRecording(t, true);
  call.mic(10000);
  call.mic(12048);
  call.played(12048 + 1024, 4096);
  const message = call.sent().find((sent) => sent.type === "played");
  NodeAssert.equal(message.t, (683 + 341.5) / 16000);
  NodeAssert.equal(Buffer.from(message.pcm, "base64").length, 1365 * 2, "16 kHz PCM16 of 4096 device samples");
});

NodeTest.test("nothing is sent back when the server records nothing", async (t) => {
  const call = await callWithRecording(t, false);
  call.mic(0);
  call.played(0, 2048);
  NodeAssert.deepEqual(
    call.sent().filter((message) => message.type === "played" || message.type === "bye"),
    [],
  );
});

NodeTest.test("a hangup drains the worklet, then says bye", async (t) => {
  const call = await callWithRecording(t, true);
  call.mic(0);
  call.page.callbacks.get(call.page.elements.get("stop"))();
  await new Promise((resolve) => setTimeout(resolve, 20));
  NodeAssert.deepEqual(call.page.worklets.get("voice-playback").posted.at(-1), { type: "drain" });
  NodeAssert.equal(call.sent().at(-1).type, "bye");
});

NodeTest.test("a connection the server closed sends no bye", async (t) => {
  const call = await callWithRecording(t, true);
  await call.page.close(call.socket, 1006, "");
  NodeAssert.equal(
    call.sent().some((message) => message.type === "bye"),
    false,
  );
});
