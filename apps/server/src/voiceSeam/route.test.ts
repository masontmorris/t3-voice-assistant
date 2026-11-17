import * as NodeNet from "node:net";

import * as NodePlatformCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeHttpServer from "@effect/platform-node/NodeHttpServer";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import { EventId, ProviderInstanceId, ThreadId } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Ref from "effect/Ref";
import { HttpRouter, HttpServer } from "effect/http";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as SessionStore from "../auth/SessionStore.ts";
import * as ServerConfig from "../config.ts";
import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import * as ProjectStore from "../orchestration-v2/ProjectStore.ts";
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
