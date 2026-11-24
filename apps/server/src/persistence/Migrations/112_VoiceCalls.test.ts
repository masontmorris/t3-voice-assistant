import { assert, it } from "@effect/vitest";
import * as NodeSqliteClient from "@t3tools/shared/nodeSqliteClient";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Migrator from "effect/sql/Migrator";
import * as SqlClient from "effect/sql/SqlClient";

import { runMigrations } from "../Migrations.ts";
import VoiceCalls from "./112_VoiceCalls.ts";

const layer = it.layer(NodeSqliteClient.layer({ filename: ":memory:" }));

layer("112_VoiceCalls", (it) => {
  it.effect(
    "moves a call recorded as a turn item into its own table, after the item before it, its lines apart",
    () =>
      Effect.gen(function* () {
        const sql = yield* SqlClient.SqlClient;
        yield* runMigrations({ toMigrationInclusive: 111 });
        const lines = [{ speaker: "caller", text: "Private words", relay: false }];
        const item = (id: string, ordinal: number, type: string, payload: object) => sql`
          INSERT INTO orchestration_v2_projection_turn_items (
            turn_item_id, thread_id, run_id, ordinal, type, status, updated_at, payload_json
          ) VALUES (
            ${id}, 'thread-1', NULL, ${ordinal}, ${type}, 'completed', '2026-11-19T00:00:00.000Z',
            ${JSON.stringify({ id, threadId: "thread-1", ordinal, type, ...payload })}
          )`;
        yield* item("message", 1, "assistant_message", { text: "Hi" });
        yield* item("call", 2, "voice_call", {
          startedAt: "2026-11-19T00:00:00.000Z",
          callId: "call-1",
          lines,
          truncated: false,
        });
        yield* sql`INSERT INTO orchestration_events (
            event_id, aggregate_kind, stream_id, stream_version, event_type,
            occurred_at, actor_kind, payload_json, metadata_json, application_event_version
          ) VALUES (
            'event-call', 'thread', 'thread-1', 0, 'turn-item.updated',
            '2026-11-19T00:00:00.000Z', 'server', '{"type":"voice_call"}', '{}', 2
          )`;

        yield* runMigrations({ toMigrationInclusive: 112 });

        const calls = yield* sql<{
          readonly voice_call_id: string;
          readonly ordinal: number;
          readonly started_at: string;
          readonly payload_json: string;
          readonly lines_json: string;
        }>`SELECT voice_call_id, ordinal, started_at, payload_json, lines_json
          FROM orchestration_v2_projection_voice_calls`;
        assert.strictEqual(calls.length, 1);
        const [call] = calls;
        const header = JSON.parse(call!.payload_json);
        assert.deepStrictEqual(
          [call!.voice_call_id, call!.ordinal, call!.started_at, header.ordinal, header.lineCount],
          ["call", 1, "2026-11-19T00:00:00.000Z", 1, 1],
        );
        assert.notProperty(header, "lines");
        assert.deepStrictEqual(JSON.parse(call!.lines_json), lines);
        const items = yield* sql<{ readonly turn_item_id: string }>`
          SELECT turn_item_id FROM orchestration_v2_projection_turn_items`;
        assert.deepStrictEqual(
          items.map((row) => row.turn_item_id),
          ["message"],
        );
        assert.lengthOf(
          yield* sql`SELECT 1 FROM orchestration_events WHERE event_id = 'event-call'`,
          0,
        );
      }),
  );
});

it.layer(NodeSqliteClient.layer({ filename: ":memory:" }))(
  "112_VoiceCalls on a fresh database",
  (it) => {
    it.effect("creates an empty table", () =>
      Effect.gen(function* () {
        const sql = yield* SqlClient.SqlClient;
        yield* runMigrations();
        assert.lengthOf(yield* sql`SELECT * FROM orchestration_v2_projection_voice_calls`, 0);
      }),
    );
  },
);

it.layer(NodeSqliteClient.layer({ filename: ":memory:" }))(
  "112_VoiceCalls after a fork database ran it as 106",
  (it) => {
    it.effect("refuses to start until that history row goes, then keeps the calls", () =>
      Effect.gen(function* () {
        const sql = yield* SqlClient.SqlClient;
        yield* runMigrations({ toMigrationInclusive: 105 });
        yield* Migrator.make({})({ loader: Migrator.fromRecord({ "106_VoiceCalls": VoiceCalls }) });
        yield* sql`INSERT INTO orchestration_v2_projection_voice_calls
          VALUES ('call-1', 'thread-1', 0, '2026-11-19T00:00:00.000Z', '{}', '[]')`;
        assert.isTrue(Exit.isFailure(yield* Effect.exit(runMigrations())));
        yield* sql`DELETE FROM effect_sql_migrations WHERE migration_id = 106 AND name = 'VoiceCalls'`;
        yield* runMigrations();
        assert.lengthOf(yield* sql`SELECT * FROM orchestration_v2_projection_voice_calls`, 1);
      }),
    );
  },
);
