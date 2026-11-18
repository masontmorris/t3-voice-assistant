// Regenerates seam.schema.json from the server's protocol: `node voice/t3/write_seam_schema.ts`,
// then `vp fmt voice/t3/seam.schema.json`. protocol.test.ts only compares the two.
import * as NodeFS from "node:fs";

import { seamJsonSchema } from "../../apps/server/src/voiceSeam/protocol.ts";

NodeFS.writeFileSync(
  new URL("seam.schema.json", import.meta.url),
  `${JSON.stringify(seamJsonSchema(), null, 2)}\n`,
);
