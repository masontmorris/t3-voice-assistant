import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import * as Config from "effect/Config";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";

import { seamJsonSchema } from "./protocol.ts";

const WRITE = Config.Boolean("VOICE_SEAM_SCHEMA_WRITE").pipe(Config.withDefault(false));
const decodeJson = Schema.decodeUnknownEffect(Schema.fromJsonString(Schema.Unknown));
const pretty = (value: unknown) => `${JSON.stringify(value, null, 2)}\n`;

// Compared as JSON values, so the formatter's layout of the committed file does not matter.
it.effect("voice/t3/seam.schema.json is the schema protocol.ts generates", () =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const path = yield* Path.Path;
    const golden = path.join(import.meta.dirname, "../../../../voice/t3/seam.schema.json");
    const generated = seamJsonSchema();
    if (yield* WRITE) {
      yield* fs.writeFileString(golden, pretty(generated));
    }
    const committed = yield* decodeJson(yield* fs.readFileString(golden));

    assert.deepStrictEqual(
      committed,
      generated,
      "Stale. Run VOICE_SEAM_SCHEMA_WRITE=1 vp test run src/voiceSeam/protocol.test.ts in apps/server, then vp fmt.",
    );
  }).pipe(Effect.provide(NodeServices.layer)),
);
