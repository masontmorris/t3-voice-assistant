import * as NodeNet from "node:net";

import * as NodePlatformCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeHttpServer from "@effect/platform-node/NodeHttpServer";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import { EventId, ProviderInstanceId, ThreadId } from "@t3tools/contracts";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Ref from "effect/Ref";
import * as Stream from "effect/Stream";
import { HttpRouter, HttpServer } from "effect/http";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as SessionStore from "../auth/SessionStore.ts";
import * as ServerConfig from "../config.ts";
import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import * as ProjectStore from "../orchestration-v2/ProjectStore.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as Sqlite from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import { VOICE_OPERATIONS, VOICE_SEAM_CONFIG_FILE } from "./ceiling.ts";
import * as VoiceSeamRoute from "./route.ts";
import { createdOf, FIXTURES, thread } from "./testFixtures.ts";
import { seamLayer, write } from "./testLayer.ts";

const PLAIN = FIXTURES.plainReply;
const PLAIN_THREAD = createdOf(PLAIN).payload.id;
const PROJECT = createdOf(PLAIN).payload.projectId;
const ULID = "01JD3Z8Q4W5X6Y7Z8A9B0C1D2E";

const CONFIG = {
  maxRuntimeMode: "auto-accept-edits",
  operations: VOICE_OPERATIONS,
  projects: [{ projectId: PROJECT, spokenNames: ["sandbox"] }],
};
const CONFIG_TEXT = JSON.stringify(CONFIG);
/** An operation the config schema does not know, so the file does not decode. */
const UNKNOWN_OPERATION_TEXT = JSON.stringify({ ...CONFIG, operations: ["list", "steer"] });

const layerAuth = EnvironmentAuth.layer.pipe(
  Layer.provide(ManagedPolicy.layerTest()),
  Layer.provide(ServerSecretStore.layer),
  Layer.provide(ServerEnvironment.layerIdentity),
  Layer.provideMerge(ServerConfig.layerTest(process.cwd(), { prefix: "t3-voice-seam-route-" })),
  Layer.provideMerge(Sqlite.layerMemory),
);

const layerTest = Layer.mergeAll(layerAuth, seamLayer).pipe(
  Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodePlatformCrypto.layer)),
  Layer.merge(NodeHttpServer.layerTest),
);

interface Frame {
  readonly type: string;
  readonly [key: string]: unknown;
}

/** A Python-shaped client: a bearer header, JSON text frames in, every frame and the close kept. */
const connect = (url: string, token: string) => {
  const socket = new WebSocket(url, { headers: { authorization: `Bearer ${token}` } });
  const frames: Array<Frame> = [];
  const waiters: Array<(frame: Frame) => void> = [];
  const closed = Promise.withResolvers<{ readonly code: number; readonly reason: string }>();
  socket.addEventListener("message", ({ data }) => {
    const frame = JSON.parse(String(data)) as Frame;
    const waiter = waiters.shift();
    if (waiter === undefined) frames.push(frame);
    else waiter(frame);
  });
  socket.addEventListener("close", ({ code, reason }) => closed.resolve({ code, reason }));
  let id = 0;
  return {
    next: (): Promise<Frame> => {
      const frame = frames.shift();
      return frame === undefined
        ? new Promise((resolve) => waiters.push(resolve))
        : Promise.resolve(frame);
    },
    request: (op: string, input: unknown) => {
      id += 1;
      socket.send(JSON.stringify({ id, op, input }));
      return id;
    },
    /** Frames received and not yet taken by `next`. */
    unread: frames as ReadonlyArray<Frame>,
    closed: closed.promise,
    close: () => socket.close(),
  };
};

const CRLF = "\r\n";

/** The upgrade's HTTP status line, read off a raw connection: 101 when the seam takes the socket. */
const upgradeStatus = (url: string, token: string | null) =>
  new Promise<number>((resolve, reject) => {
    const { hostname, port, pathname } = new URL(url);
    const socket = NodeNet.connect({ host: hostname.replace(/^\[|\]$/g, ""), port: Number(port) });
    let received = "";
    socket.on("connect", () =>
      socket.write(
        [
          `GET ${pathname} HTTP/1.1`,
          `Host: ${hostname}:${port}`,
          "Connection: Upgrade",
          "Upgrade: websocket",
          "Sec-WebSocket-Version: 13",
          "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
          ...(token === null ? [] : [`Authorization: Bearer ${token}`]),
          "",
          "",
        ].join(CRLF),
      ),
    );
    socket.on("data", (chunk) => {
      received += chunk.toString("latin1");
      if (!received.includes(CRLF)) return;
      socket.destroy();
      resolve(Number(received.split(" ")[1]));
    });
    socket.on("error", reject);
  });

/** Server frames carry no mask; a 126 or 127 length is followed by a 2- or 8-byte length. */
const readFrame = (bytes: Buffer, at: number) => {
  if (bytes.length < at + 2) return null;
  const opcode = bytes[at]! & 0x0f;
  const short = bytes[at + 1]! & 0x7f;
  const header = short === 126 ? 4 : short === 127 ? 10 : 2;
  if (bytes.length < at + header) return null;
  const length =
    short === 126
      ? bytes.readUInt16BE(at + 2)
      : short === 127
        ? Number(bytes.readBigUInt64BE(at + 2))
        : short;
  if (bytes.length < at + header + length) return null;
  const next = at + header + length;
  return { opcode, payload: bytes.subarray(at + header, next), next };
};

/**
 * A client that completes the handshake and then stops reading its TCP socket, so whatever the
 * server sends piles up on the server's side. `drain` reads again and returns what had piled up.
 */
const stalledClient = (url: string, token: string) =>
  new Promise<{
    readonly send: (text: string) => void;
    readonly drain: () => Promise<{ readonly payloadBytes: number; readonly code: number | null }>;
  }>((resolve, reject) => {
    const { hostname, port, pathname } = new URL(url);
    const socket = NodeNet.connect({ host: hostname.replace(/^\[|\]$/g, ""), port: Number(port) });
    let received = Buffer.alloc(0);
    const onHandshake = (chunk: Buffer) => {
      received = Buffer.concat([received, chunk]);
      const end = received.indexOf(`${CRLF}${CRLF}`);
      if (end === -1) return;
      socket.pause();
      socket.off("data", onHandshake);
      if (!received.subarray(0, end).toString("latin1").startsWith("HTTP/1.1 101")) {
        return reject(new Error(received.subarray(0, end).toString("latin1")));
      }
      received = received.subarray(end + 4);
      resolve({
        send: (text) => {
          const payload = Buffer.from(text);
          // Client frames are masked; an all-zero key leaves the payload as it is.
          const length =
            payload.length < 126
              ? Buffer.from([0x80 | payload.length])
              : Buffer.from([0x80 | 126, payload.length >> 8, payload.length & 0xff]);
          socket.write(Buffer.concat([Buffer.from([0x81]), length, Buffer.alloc(4), payload]));
        },
        drain: () =>
          new Promise((done) => {
            let at = 0;
            let payloadBytes = 0;
            const parse = () => {
              for (
                let frame = readFrame(received, at);
                frame !== null;
                frame = readFrame(received, at)
              ) {
                at = frame.next;
                if (frame.opcode !== 0x8) {
                  payloadBytes += frame.payload.length;
                  continue;
                }
                socket.destroy();
                return done({ payloadBytes, code: frame.payload.readUInt16BE(0) });
              }
            };
            socket.on("data", (chunk: Buffer) => {
              received = Buffer.concat([received, chunk]);
              parse();
            });
            socket.on("close", () => done({ payloadBytes, code: null }));
            parse();
            socket.resume();
          }),
      });
    };
    socket.on("data", onHandshake);
    socket.on("error", reject);
    socket.on("connect", () =>
      socket.write(
        [
          `GET ${pathname} HTTP/1.1`,
          `Host: ${hostname}:${port}`,
          "Connection: Upgrade",
          "Upgrade: websocket",
          "Sec-WebSocket-Version: 13",
          "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
          `Authorization: Bearer ${token}`,
          "",
          "",
        ].join(CRLF),
      ),
    );
  });

const serveSeam = Effect.fn("test.serveSeam")(function* (
  route: typeof VoiceSeamRoute.layer = VoiceSeamRoute.layer,
) {
  yield* Layer.build(HttpRouter.serve(route, { disableLogger: true, disableListenLog: true }));
  const server = yield* HttpServer.HttpServer;
  const address = server.address;
  assert(address._tag !== "UnixPathAddress");
  const host = address._tag === "InetAddressV6" ? "[::1]" : "127.0.0.1";
  const { baseDir } = yield* ServerConfig.ServerConfig;
  const auth = yield* EnvironmentAuth.EnvironmentAuth;
  const fs = yield* FileSystem.FileSystem;
  const configFile = (yield* Path.Path).join(baseDir, VOICE_SEAM_CONFIG_FILE);
  return {
    http: `http://${host}:${address.port}${VoiceSeamRoute.VOICE_SEAM_PATH}`,
    ws: `ws://${host}:${address.port}${VoiceSeamRoute.VOICE_SEAM_PATH}`,
    auth,
    configure: (text: string) => fs.writeFileString(configFile, text),
    voiceSession: auth.issueSession({ subject: "voice", scopes: ["orchestration:read"] }),
  };
});

/** Revokes each session as the route marks it connected: after the upgrade's check, before hello. */
const revokeOnConnect = Layer.effect(
  SessionStore.SessionStore,
  Effect.gen(function* () {
    const real = yield* SessionStore.SessionStore;
    const auth = yield* EnvironmentAuth.EnvironmentAuth;
    return {
      ...real,
      markConnected: (sessionId) =>
        auth
          .revokeSession(sessionId)
          .pipe(Effect.orDie, Effect.andThen(real.markConnected(sessionId))),
    };
  }),
);

/** Counts project reads, the first service call `hello` makes. */
const countProjectReads = (reads: Ref.Ref<number>) =>
  Layer.effect(
    ProjectStore.ProjectStoreV2,
    Effect.gen(function* () {
      const real = yield* ProjectStore.ProjectStoreV2;
      return {
        ...real,
        list: (input) =>
          Ref.update(reads, (count) => count + 1).pipe(Effect.andThen(real.list(input))),
      };
    }),
  );

const seedProject = Effect.gen(function* () {
  const projects = yield* ProjectStore.ProjectStoreV2;
  yield* projects.apply({
    sequence: 1,
    eventId: EventId.make("event:sandbox-project"),
    aggregateKind: "project",
    aggregateId: PROJECT,
    occurredAt: "2026-11-01T00:00:00.000Z",
    commandId: null,
    causationEventId: null,
    correlationId: null,
    metadata: {},
    type: "project.created",
    payload: {
      projectId: PROJECT,
      title: "Voice sandbox",
      workspaceRoot: "/tmp/voice-sandbox",
      defaultModelSelection: { instanceId: ProviderInstanceId.make("codex"), model: "gpt-5.4" },
      scripts: [],
      createdAt: "2026-11-01T00:00:00.000Z",
      updatedAt: "2026-11-01T00:00:00.000Z",
    },
  });
});

const live = <A, E, R>(effect: Effect.Effect<A, E, R>) =>
  effect.pipe(Effect.scoped, Effect.provide(layerTest));

it.live("answers 404 while voice-seam.json is absent or does not decode", () =>
  live(
    Effect.gen(function* () {
      const seam = yield* serveSeam();
      const { token } = yield* seam.voiceSession;

      assert.strictEqual(yield* Effect.promise(() => upgradeStatus(seam.http, token)), 404);
      yield* seam.configure("{ not json");
      assert.strictEqual(yield* Effect.promise(() => upgradeStatus(seam.http, token)), 404);
      yield* seam.configure(UNKNOWN_OPERATION_TEXT);
      assert.strictEqual(yield* Effect.promise(() => upgradeStatus(seam.http, token)), 404);
      yield* seam.configure(CONFIG_TEXT);
      assert.strictEqual(yield* Effect.promise(() => upgradeStatus(seam.http, token)), 101);
    }),
  ),
);

it.live("refuses a session whose subject is not voice, and a request with no session", () =>
  live(
    Effect.gen(function* () {
      const seam = yield* serveSeam();
      yield* seam.configure(CONFIG_TEXT);
      const other = yield* seam.auth.issueSession({
        subject: "voice-adjacent",
        scopes: ["orchestration:read", "orchestration:operate"],
      });

      assert.strictEqual(yield* Effect.promise(() => upgradeStatus(seam.http, other.token)), 403);
      assert.strictEqual(yield* Effect.promise(() => upgradeStatus(seam.http, null)), 401);
      assert.strictEqual(
        yield* Effect.promise(() => upgradeStatus(seam.http, "not-a-session-token")),
        401,
      );
    }),
  ),
);

it.live("closes the socket when the voice session is revoked", () =>
  live(
    Effect.gen(function* () {
      const seam = yield* serveSeam();
      yield* seam.configure(CONFIG_TEXT);
      const session = yield* seam.voiceSession;
      const client = connect(seam.ws, session.token);
      assert.strictEqual((yield* Effect.promise(client.next)).type, "hello");

      assert.isTrue(yield* seam.auth.revokeSession(session.sessionId));
      const closed = yield* Effect.promise(() => client.closed);

      assert.strictEqual(closed.code, 1008);
      assert.strictEqual(yield* Effect.promise(() => upgradeStatus(seam.http, session.token)), 401);
    }),
  ),
);

it.live("closes a session revoked between the upgrade and hello before any service call", () =>
  live(
    Effect.gen(function* () {
      const projectReads = yield* Ref.make(0);
      const seam = yield* serveSeam(
        VoiceSeamRoute.layer.pipe(
          Layer.provide(Layer.mergeAll(revokeOnConnect, countProjectReads(projectReads))),
        ),
      );
      yield* seam.configure(CONFIG_TEXT);
      const client = connect(seam.ws, (yield* seam.voiceSession).token);

      const closed = yield* Effect.promise(() => client.closed);

      assert.strictEqual(closed.code, 1008);
      assert.deepStrictEqual(client.unread, []);
      assert.strictEqual(yield* Ref.get(projectReads), 0);
    }),
  ),
);

it.live("closes with 1013 and keeps its send buffer bounded for a client that stops reading", () =>
  live(
    Effect.gen(function* () {
      const seam = yield* serveSeam();
      yield* seam.configure(CONFIG_TEXT);
      // 200 listed threads with long titles make each list answer about 450 KB.
      yield* write(
        Array.from({ length: 200 }, (_, index) => {
          const created = thread({ id: `long-${index}`, minutes: index, projectId: PROJECT });
          return created.type === "thread.created"
            ? { ...created, payload: { ...created.payload, title: "t".repeat(2000) } }
            : created;
        }),
      );
      const session = yield* seam.voiceSession;
      const sessions = yield* SessionStore.SessionStore;
      const disconnected = yield* sessions.streamChanges.pipe(
        Stream.filter(
          (change) =>
            change.type === "clientUpserted" &&
            change.clientSession.sessionId === session.sessionId &&
            !change.clientSession.connected,
        ),
        Stream.runHead,
        Effect.forkScoped,
      );
      const client = yield* Effect.promise(() => stalledClient(seam.ws, session.token));

      // About 36 MB of answers, far past the cap plus what the kernel buffers on both ends.
      for (let id = 1; id <= 80; id += 1) {
        client.send(JSON.stringify({ id, op: "list_threads", input: {} }));
      }
      yield* Fiber.join(disconnected);
      const drained = yield* Effect.promise(client.drain);

      assert.strictEqual(drained.code, 1013);
      assert.isBelow(drained.payloadBytes, 8 * 1024 * 1024);
    }),
  ),
);

it.live("refuses a watch on a subagent or delegated thread with not_found", () =>
  live(
    Effect.gen(function* () {
      const seam = yield* serveSeam();
      yield* seam.configure(CONFIG_TEXT);
      const parent = ThreadId.make("parent");
      yield* write([
        thread({ id: "parent", minutes: 1, projectId: PROJECT }),
        thread({
          id: "subagent-child",
          minutes: 2,
          projectId: PROJECT,
          lineage: {
            parentThreadId: parent,
            relationshipToParent: "subagent",
            rootThreadId: parent,
          },
        }),
        thread({ id: "delegated", minutes: 3, projectId: PROJECT, delegated: true }),
      ]);
      const client = connect(seam.ws, (yield* seam.voiceSession).token);
      yield* Effect.promise(client.next);

      const answers: Array<Frame> = [];
      for (const threadId of ["subagent-child", "delegated", "parent"]) {
        client.request("watch", { threadId, afterSequence: 0 });
        answers.push(yield* Effect.promise(client.next));
      }
      client.close();

      const notFound = { code: "not_found", message: "I can't see that thread." };
      assert.deepStrictEqual(answers, [
        { type: "result", id: 1, ok: false, error: notFound },
        { type: "result", id: 2, ok: false, error: notFound },
        { type: "result", id: 3, ok: true, value: { threadId: parent } },
      ]);
    }),
  ),
);

/** Takes frames onto `taken` until `stop` holds for it, and returns it. */
const takeUntil = (
  client: ReturnType<typeof connect>,
  stop: (taken: ReadonlyArray<Frame>) => boolean,
  taken: Array<Frame> = [],
) =>
  Effect.promise(async () => {
    while (!stop(taken)) taken.push(await client.next());
    return taken;
  });

const bothAnswered = (taken: ReadonlyArray<Frame>) =>
  taken.some(isResult(2)) && taken.some(isResult(3));

const isResult = (id: number) => (frame: Frame) => frame.type === "result" && frame.id === id;
const through =
  (match: (frame: Frame) => boolean) =>
  (taken: ReadonlyArray<Frame>): boolean =>
    taken.length > 0 && match(taken.at(-1)!);

/**
 * Holds each live watch stream's end until `release`, so replacing a live watch stays suspended
 * while it stops. `live` completes once a watch has reached its live stream.
 */
const holdLiveStreams = (gate: {
  readonly live: Deferred.Deferred<void>;
  readonly release: Deferred.Deferred<void>;
}) =>
  Layer.effect(
    ThreadManagementService.ThreadManagementService,
    Effect.gen(function* () {
      const real = yield* ThreadManagementService.ThreadManagementService;
      return {
        ...real,
        streamStoredEventsFrom: (input) =>
          real
            .streamStoredEventsFrom(input)
            .pipe(
              Stream.ensuring(Deferred.await(gate.release)),
              Stream.onStart(Deferred.succeed(gate.live, undefined)),
            ),
      };
    }),
  );

const serveHeldSeam = Effect.gen(function* () {
  const gate = { live: yield* Deferred.make<void>(), release: yield* Deferred.make<void>() };
  const seam = yield* serveSeam(VoiceSeamRoute.layer.pipe(Layer.provide(holdLiveStreams(gate))));
  return { ...seam, gate };
});

it.live("admits no more than maxWatches when two watches race", () =>
  live(
    Effect.gen(function* () {
      const seam = yield* serveHeldSeam;
      yield* seam.configure(JSON.stringify({ ...CONFIG, maxWatches: 1 }));
      yield* write([
        thread({ id: "first", minutes: 1, projectId: PROJECT }),
        thread({ id: "second", minutes: 2, projectId: PROJECT }),
      ]);
      const client = connect(seam.ws, (yield* seam.voiceSession).token);
      yield* Effect.promise(client.next);
      client.request("watch", { threadId: "first", afterSequence: 0 });
      yield* takeUntil(client, through(isResult(1)));
      yield* Deferred.await(seam.gate.live);

      // The first replaces the live watch and waits while it stops; the second asks for a slot.
      client.request("watch", { threadId: "first", afterSequence: 0 });
      client.request("watch", { threadId: "second", afterSequence: 0 });
      // Its answer shows the server has read both watches.
      client.request("list_threads", {});
      const frames = yield* takeUntil(client, through(isResult(4)));
      yield* Deferred.succeed(seam.gate.release, undefined);
      yield* takeUntil(client, bothAnswered, frames);
      client.close();

      assert.deepStrictEqual(frames.filter(isResult(2)), [
        { type: "result", id: 2, ok: true, value: { threadId: "first" } },
      ]);
      assert.deepStrictEqual(frames.filter(isResult(3)), [
        {
          type: "result",
          id: 3,
          ok: false,
          error: { code: "too_many_watches", message: "I'm following too many threads already." },
        },
      ]);
    }),
  ),
);

it.live("keeps one watch when two race on a thread, and unwatch stops it", () =>
  live(
    Effect.gen(function* () {
      const seam = yield* serveHeldSeam;
      yield* seam.configure(CONFIG_TEXT);
      yield* write(PLAIN.map((stored) => stored.event));
      const client = connect(seam.ws, (yield* seam.voiceSession).token);
      yield* Effect.promise(client.next);
      client.request("watch", { threadId: PLAIN_THREAD, afterSequence: 0 });
      const once = (yield* takeUntil(client, through(isDone))).filter(
        (frame) => frame.type === "event",
      );
      yield* Deferred.await(seam.gate.live);

      client.request("watch", { threadId: PLAIN_THREAD, afterSequence: 0 });
      client.request("watch", { threadId: PLAIN_THREAD, afterSequence: 0 });
      client.request("list_threads", {});
      const answered = yield* takeUntil(client, through(isResult(4)));
      yield* Deferred.succeed(seam.gate.release, undefined);
      yield* takeUntil(client, bothAnswered, answered);
      const later = answered.findLastIndex((frame) => isResult(2)(frame) || isResult(3)(frame));
      const tail = yield* takeUntil(
        client,
        (taken) => taken.some(isDone),
        answered.slice(later + 1).filter((frame) => frame.type !== "result"),
      );
      client.request("unwatch", { threadId: PLAIN_THREAD });
      const unwatched = yield* takeUntil(client, through(isResult(5)));
      // A watch still running sends `gone` for this before the read's answer.
      yield* write([
        {
          ...createdOf(PLAIN),
          id: EventId.make("thread-deleted"),
          type: "thread.deleted",
          payload: { ...createdOf(PLAIN).payload, deletedAt: createdOf(PLAIN).occurredAt },
        },
      ]);
      client.request("read_thread", { threadId: PLAIN_THREAD });
      const afterUnwatch = yield* Effect.promise(client.next);
      client.close();

      assert.deepStrictEqual(tail, once);
      assert.deepStrictEqual(unwatched.at(-1), {
        type: "result",
        id: 5,
        ok: true,
        value: { threadId: PLAIN_THREAD },
      });
      assert.deepInclude(afterUnwatch, { type: "result", id: 6 });
    }),
  ),
);

it.live("sends hello first, answers each op, and starts a watch's events after its result", () =>
  live(
    Effect.gen(function* () {
      const seam = yield* serveSeam();
      yield* seam.configure(CONFIG_TEXT);
      yield* seedProject;
      yield* write(PLAIN.map((stored) => stored.event));
      const client = connect(seam.ws, (yield* seam.voiceSession).token);

      const hello = yield* Effect.promise(client.next);
      assert.deepStrictEqual(hello, {
        type: "hello",
        ceiling: {
          maxRuntimeMode: "auto-accept-edits",
          interactionMode: "default",
          operations: [...VOICE_OPERATIONS],
          maxWatches: 16,
        },
        projects: [{ projectId: PROJECT, name: "Voice sandbox", spokenNames: ["sandbox"] }],
        latestSequence: PLAIN.length,
      });

      const answer = (op: string, input: unknown) =>
        Effect.promise(() => {
          client.request(op, input);
          return client.next();
        });
      const listed = yield* answer("list_threads", {});
      const read = yield* answer("read_thread", { threadId: PLAIN_THREAD });
      const badLimit = yield* answer("list_threads", { limit: 0 });
      const unknownOp = yield* answer("steer", {});
      const launch = yield* answer("launch", {
        requestId: ULID,
        projectId: PROJECT,
        instruction: "Fix the flaky test",
      });
      client.request("watch", { threadId: PLAIN_THREAD, afterSequence: 0 });
      const watched: Array<Frame> = [];
      while (watched.at(-1)?.type !== "event" || !isDone(watched.at(-1)!)) {
        watched.push(yield* Effect.promise(client.next));
      }
      const unwatched = yield* answer("unwatch", { threadId: PLAIN_THREAD });
      client.close();

      assert.deepInclude(listed, { type: "result", id: 1, ok: true });
      assert.deepInclude((listed.value as { threads: ReadonlyArray<unknown> }).threads[0], {
        threadId: PLAIN_THREAD,
        projectId: PROJECT,
        activity: "finished",
      });
      assert.deepInclude(read, { type: "result", id: 2, ok: true });
      assert.deepInclude(read.value, { threadId: PLAIN_THREAD, activity: "finished" });
      const invalid = { code: "invalid_request", message: "T3 couldn't read that request." };
      assert.deepStrictEqual(badLimit, { type: "result", id: 3, ok: false, error: invalid });
      assert.deepStrictEqual(unknownOp, { type: "result", id: 4, ok: false, error: invalid });
      assert.deepStrictEqual(launch, {
        type: "result",
        id: 5,
        ok: false,
        error: { code: "unavailable", message: "I can't do that yet." },
      });
      assert.deepStrictEqual(watched[0], {
        type: "result",
        id: 6,
        ok: true,
        value: { threadId: PLAIN_THREAD },
      });
      assert.isTrue(watched.slice(1).every((frame) => frame.type === "event"));
      assert.deepInclude(watched.at(-1)!.event, { type: "done", status: "completed" });
      assert.deepStrictEqual(unwatched, {
        type: "result",
        id: 7,
        ok: true,
        value: { threadId: PLAIN_THREAD },
      });
    }),
  ),
);

const isDone = (frame: Frame) =>
  typeof frame.event === "object" &&
  frame.event !== null &&
  "type" in frame.event &&
  frame.event.type === "done";
