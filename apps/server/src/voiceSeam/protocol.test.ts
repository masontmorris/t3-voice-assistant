import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";

import { seamJsonSchema } from "./protocol.ts";

const decodeJson = Schema.decodeUnknownEffect(Schema.fromJsonString(Schema.Unknown));

// Compared as JSON values, so the formatter's layout of the committed file does not matter.
it.effect("voice/t3/seam.schema.json is the schema protocol.ts generates", () =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const path = yield* Path.Path;
    const golden = path.join(import.meta.dirname, "../../../../voice/t3/seam.schema.json");
    const generated = seamJsonSchema();
    const committed = yield* decodeJson(yield* fs.readFileString(golden));

    assert.deepStrictEqual(
      committed,
      generated,
      "Stale. From the repo root, run node voice/t3/write_seam_schema.ts, then vp fmt voice/t3/seam.schema.json.",
    );
  }).pipe(Effect.provide(NodeServices.layer)),
);
