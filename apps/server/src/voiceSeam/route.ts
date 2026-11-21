import * as NodeHttpServerRequest from "@effect/platform-node/NodeHttpServerRequest";
import { AuthOrchestrationReadScope, type ThreadId } from "@t3tools/contracts";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Queue from "effect/Queue";
import * as Schema from "effect/Schema";
import * as Scope from "effect/Scope";
import * as Semaphore from "effect/Semaphore";
import * as Stream from "effect/Stream";
import { HttpRouter, HttpServerRequest, HttpServerResponse } from "effect/http";
import * as Socket from "effect/socket/Socket";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as SessionStore from "../auth/SessionStore.ts";
import * as ServerConfig from "../config.ts";
import { readConfig, VOICE_SEAM_CONFIG_FILE } from "./ceiling.ts";
import {
  ClientEnvelope,
  EventFrame,
  HelloFrame,
  REQUESTS,
  type RequestInput,
  type RequestOutput,
  ResultErrorFrame,
  resultFrame,
  WatchEndedFrame,
  type WireOp,
} from "./protocol.ts";
import * as VoiceSeam from "./VoiceSeam.ts";
import type * as Watch from "./watch.ts";

/** Outside `/api`, so no upstream route collides with it. */
export const VOICE_SEAM_PATH = "/voice-seam";
const VOICE_SUBJECT = "voice";
const MAX_IN_FLIGHT = 4;
const OUTBOUND_CAPACITY = 256;
/** Bytes `ws` may hold for one client before the seam gives up on it. */
const MAX_BUFFERED_BYTES = 1024 * 1024;

type Handlers = {
  readonly [Op in WireOp]: (
    input: RequestInput<Op>,
    /** Completes once this request's result frame is queued, so a watch's events follow it. */
    replied: Effect.Effect<void>,
  ) => Effect.Effect<RequestOutput<Op>, VoiceSeam.VoiceSeamRefusal>;
};

const isWireOp = (op: string): op is WireOp => Object.hasOwn(REQUESTS, op);

const decodeEnvelope = Schema.decodeUnknownEffect(Schema.fromJsonString(ClientEnvelope));
const encodeHello = Schema.encodeSync(Schema.fromJsonString(HelloFrame));
const encodeEvent = Schema.encodeSync(Schema.fromJsonString(EventFrame));
const encodeWatchEnded = Schema.encodeSync(Schema.fromJsonString(WatchEndedFrame));
const encodeRefusal = Schema.encodeSync(Schema.fromJsonString(ResultErrorFrame));

const codec = <Op extends WireOp>(op: Op) => ({
  decodeInput: Schema.decodeUnknownEffect(REQUESTS[op].input),
  encodeResult: Schema.encodeUnknownEffect(Schema.fromJsonString(resultFrame(op))),
});
/** Each op's input decoder and result encoder, compiled once. */
const CODECS: { readonly [Op in WireOp]: ReturnType<typeof codec<Op>> } = {
  list_threads: codec("list_threads"),
  read_thread: codec("read_thread"),
  watch: codec("watch"),
  unwatch: codec("unwatch"),
  launch: codec("launch"),
  send: codec("send"),
  interrupt: codec("interrupt"),
  respond: codec("respond"),
};

const close = (code: number, reason: string) => new Socket.CloseEvent(code, reason);
const revoked = close(1008, "The voice session was revoked.");
const stoppedReading = close(1013, "The voice seam client stopped reading.");

/**
 * Reads how many bytes `ws` holds for this connection: its send queue and the TCP socket's buffer.
 * Effect's socket keeps the `ws` instance private, and `ws` sets it on the TCP socket under a
 * symbol it does not export, so this finds it by the symbol's description.
 */
const bufferedBytesOf = (transport: object): (() => number) | undefined => {
  const key = Object.getOwnPropertySymbols(transport).find(
    (symbol) => symbol.description === "websocket",
  );
  const webSocket: unknown = key === undefined ? undefined : Reflect.get(transport, key);
  if (typeof webSocket !== "object" || webSocket === null || !("bufferedAmount" in webSocket)) {
    return undefined;
  }
  return () => Number(webSocket.bufferedAmount);
};

/**
 * One socket serves one authenticated voice caller under the ceiling read when it opened.
 * `hello` goes first, requests run up to four at a time, and each watch forwards from its own fiber.
 */
const runSocket = Effect.fn("VoiceSeam.route.runSocket")(function* (input: {
  readonly socket: Socket.Socket;
  /** The upgraded request's TCP socket, where `ws` keeps its WebSocket. */
  readonly transport: object;
  readonly seam: VoiceSeam.VoiceSeam["Service"];
  readonly caller: VoiceSeam.VoiceCaller;
  /** Completes when the caller's session is revoked or expires. */
  readonly invalidated: Effect.Effect<void, SessionStore.SessionCredentialVerificationError>;
  /** Whether the session is still unrevoked and unexpired, read once the watcher is up. */
  readonly stillActive: Effect.Effect<boolean, SessionStore.SessionCredentialInternalError>;
}) {
  const { seam, caller } = input;
  const scope = yield* Scope.Scope;
  const writer = yield* input.socket.writer;
  const outbound = yield* Queue.bounded<string>(OUTBOUND_CAPACITY);
  /** The close frame to send, or null once the client has closed. */
  const ending = yield* Deferred.make<Socket.CloseEvent | null>();
  const endWith = (event: Socket.CloseEvent) => Deferred.succeed(ending, event).pipe(Effect.asVoid);
  // Frames waiting for the writer. A full queue, or more than MAX_BUFFERED_BYTES in `ws`, closes
  // with 1013, and the client re-watches from its cursors.
  const send = (frame: string) =>
    Effect.suspend(() =>
      Queue.offerUnsafe(outbound, frame) ? Effect.void : endWith(stoppedReading),
    );
  const refuse = (id: number, refusal: VoiceSeam.VoiceSeamRefusal) =>
    send(
      encodeRefusal({
        type: "result",
        id,
        ok: false,
        error: {
          code: refusal.code,
          message: refusal.message,
          ...(refusal.reason === undefined ? {} : { reason: refusal.reason }),
          ...(refusal.commandId === undefined ? {} : { commandId: refusal.commandId }),
          ...(refusal.threadId === undefined ? {} : { threadId: refusal.threadId }),
        },
      }),
    );

  const watches = new Map<ThreadId, Fiber.Fiber<void>>();
  // Admission, replacement and unwatch wait on a stopping fiber between reading and writing
  // `watches`, so each runs alone, through its result frame.
  const watchLock = yield* Semaphore.make(1);
  const stopWatch = Effect.fn("VoiceSeam.route.stopWatch")(function* (threadId: ThreadId) {
    const fiber = watches.get(threadId);
    watches.delete(threadId);
    if (fiber !== undefined) yield* Fiber.interrupt(fiber);
  });
  const forward = (item: Watch.WatchItem) =>
    send(
      item.type === "watch_ended"
        ? encodeWatchEnded(item)
        : encodeEvent({ type: "event", event: item }),
    );

  const handlers: Handlers = {
    list_threads: (request) => seam.listThreads(caller, request),
    read_thread: (request) => seam.readThread(caller, request),
    watch: Effect.fn("VoiceSeam.route.watch")(function* (request, replied) {
      const items = yield* seam.watch(caller, request);
      const { threadId } = request;
      // A second watch on the same thread replaces the first; it does not take another slot.
      if (!watches.has(threadId) && watches.size >= caller.config.maxWatches) {
        return yield* VoiceSeam.VoiceSeamRefusal.tooManyWatches();
      }
      yield* stopWatch(threadId);
      const fiber: Fiber.Fiber<void> = yield* replied.pipe(
        Effect.andThen(Stream.runForEach(items, forward)),
        Effect.ensuring(
          Effect.sync(() => {
            if (watches.get(threadId) === fiber) watches.delete(threadId);
          }),
        ),
        Effect.forkIn(scope),
      );
      watches.set(threadId, fiber);
      return { threadId };
    }),
    unwatch: (request) =>
      stopWatch(request.threadId).pipe(Effect.as({ threadId: request.threadId })),
    launch: (request) => seam.launch(caller, request),
    send: (request) => seam.send(caller, request),
    interrupt: (request) => seam.interrupt(caller, request),
    respond: (request) => seam.respond(caller, request),
  };

  const dispatch = Effect.fn("VoiceSeam.route.dispatch")(function* <Op extends WireOp>(
    op: Op,
    id: number,
    raw: unknown,
  ) {
    const { decodeInput, encodeResult } = CODECS[op];
    const decoded = yield* decodeInput(raw).pipe(Effect.result);
    if (decoded._tag === "Failure") {
      yield* Effect.logWarning("voice seam request was invalid", { op, issue: decoded.failure });
      return yield* refuse(id, VoiceSeam.VoiceSeamRefusal.invalidRequest());
    }
    const replied = yield* Deferred.make<void>();
    const handler: Handlers[Op] = handlers[op];
    const answered = yield* handler(decoded.success, Deferred.await(replied)).pipe(
      Effect.flatMap((value) =>
        encodeResult({ type: "result", id, ok: true, value }).pipe(Effect.orDie),
      ),
      Effect.result,
    );
    if (answered._tag === "Failure") {
      const refusal = answered.failure;
      if (refusal.cause !== undefined) {
        yield* Effect.logWarning("voice seam request failed", { op, cause: refusal.cause });
      }
      return yield* refuse(id, refusal);
    }
    yield* send(answered.success);
    yield* Deferred.succeed(replied, undefined);
  });

  const receive = Effect.fn("VoiceSeam.route.receive")(function* (frame: string | Uint8Array) {
    const envelope =
      typeof frame === "string" ? yield* decodeEnvelope(frame).pipe(Effect.option) : Option.none();
    if (Option.isNone(envelope)) {
      // With no id there is no request to answer.
      return yield* endWith(close(1007, "Voice seam frames are JSON objects with a numeric id."));
    }
    const { id, op, input: raw } = envelope.value;
    if (!isWireOp(op)) return yield* refuse(id, VoiceSeam.VoiceSeamRefusal.invalidRequest());
    const request = dispatch(op, id, raw);
    return yield* op === "watch" || op === "unwatch" ? watchLock.withPermit(request) : request;
  });

  const permits = yield* Semaphore.make(MAX_IN_FLIGHT);
  const reader = yield* input.socket.reader;
  const bufferedBytes = bufferedBytesOf(input.transport);
  if (bufferedBytes === undefined) {
    yield* Effect.logWarning("voice seam could not find its WebSocket's send buffer");
    return yield* writer.write(close(1011, "The voice seam could not start.")).pipe(Effect.ignore);
  }
  // Effect's writer hands each frame to `ws` without waiting, so the bound is checked here.
  const writeLoop = Effect.gen(function* () {
    while (true) {
      const frame = yield* Queue.take(outbound);
      if (bufferedBytes() > MAX_BUFFERED_BYTES) return yield* endWith(stoppedReading);
      yield* writer.write(frame);
    }
  });
  yield* writeLoop.pipe(Effect.ignore, Effect.forkIn(scope));
  yield* input.invalidated.pipe(
    Effect.matchEffect({
      onFailure: (cause) =>
        Effect.logWarning("voice seam could not watch its session", { cause }).pipe(
          Effect.andThen(endWith(close(1011, "The voice seam lost its session check."))),
        ),
      onSuccess: () => endWith(revoked),
    }),
    Effect.forkIn(scope),
  );

  const readLoop = Effect.forever(
    reader.pull.pipe(
      Effect.flatMap((frames) =>
        Effect.forEach(
          frames,
          (frame) =>
            permits
              .take(1)
              .pipe(
                Effect.andThen(
                  receive(frame).pipe(Effect.ensuring(permits.release(1)), Effect.forkIn(scope)),
                ),
              ),
          { discard: true },
        ),
      ),
    ),
  );
  const serve = Effect.gen(function* () {
    // A revocation before the watcher subscribed is caught here, before any service call.
    const active = yield* input.stillActive.pipe(Effect.result);
    if (active._tag === "Failure") {
      yield* Effect.logWarning("voice seam could not check its session", { cause: active.failure });
      return yield* endWith(close(1011, "The voice seam lost its session check."));
    }
    if (!active.success) return yield* endWith(revoked);
    const hello = yield* seam.hello(caller).pipe(Effect.result);
    if (hello._tag === "Failure") {
      yield* Effect.logWarning("voice seam hello failed", { cause: hello.failure.cause });
      return yield* endWith(close(1011, "The voice seam could not start."));
    }
    yield* send(encodeHello(hello.success));
    // A pull fails once the client closes the socket, which ends this socket's watches with it.
    return yield* readLoop;
  });
  yield* serve.pipe(
    Effect.ignore,
    Effect.andThen(Deferred.succeed(ending, null)),
    Effect.forkIn(scope),
  );
  const ended = yield* Deferred.await(ending);
  if (ended !== null) yield* writer.write(ended).pipe(Effect.ignore);
});

export const layer = Layer.unwrap(
  Effect.gen(function* () {
    const auth = yield* EnvironmentAuth.EnvironmentAuth;
    const sessions = yield* SessionStore.SessionStore;
    const seam = yield* VoiceSeam.VoiceSeam;
    const fs = yield* FileSystem.FileSystem;
    const path = yield* Path.Path;
    const { baseDir } = yield* ServerConfig.ServerConfig;
    const configFile = path.join(baseDir, VOICE_SEAM_CONFIG_FILE);
    return HttpRouter.add(
      "GET",
      VOICE_SEAM_PATH,
      Effect.gen(function* () {
        const request = yield* HttpServerRequest.HttpServerRequest;
        const session = yield* auth
          .authenticateWebSocketUpgrade(request, "rpc")
          .pipe(Effect.result);
        if (session._tag === "Failure") {
          return HttpServerResponse.empty({
            status: EnvironmentAuth.isServerAuthCredentialError(session.failure) ? 401 : 500,
          });
        }
        const { sessionId, subject, scopes, proofKeyThumbprint } = session.success;
        if (subject !== VOICE_SUBJECT || !scopes.includes(AuthOrchestrationReadScope)) {
          return HttpServerResponse.empty({ status: 403 });
        }
        const read = yield* readConfig(fs, configFile);
        if (read.kind === "invalid") {
          yield* Effect.logWarning("voice seam is off: voice-seam.json did not decode", {
            cause: read.cause,
          });
        }
        if (read.kind !== "on") return HttpServerResponse.empty({ status: 404 });
        const caller: VoiceSeam.VoiceCaller = {
          sessionId,
          namespace: "voice",
          config: read.config,
        };
        const socket = yield* request.upgrade;
        const transport = NodeHttpServerRequest.toIncomingMessage(request).socket;
        yield* Effect.acquireUseRelease(
          sessions.markConnected(sessionId),
          () =>
            runSocket({
              socket,
              transport,
              seam,
              caller,
              invalidated: sessions.awaitInvalidation(sessionId, proofKeyThumbprint),
              stillActive: sessions
                .listActive()
                .pipe(Effect.map((active) => active.some((row) => row.sessionId === sessionId))),
            }).pipe(Effect.scoped),
          () => sessions.markDisconnected(sessionId),
        );
        return HttpServerResponse.empty();
      }),
    );
  }),
).pipe(Layer.provide(VoiceSeam.layer));
