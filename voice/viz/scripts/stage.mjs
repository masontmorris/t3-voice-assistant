// Copy a loop archive into public/archives/<name> so compositions can load it with staticFile().
// Usage: node scripts/stage.mjs <archive dir> [name]
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { basename, join, resolve } from "node:path";

const [source, name = basename(resolve(source ?? ""))] = process.argv.slice(2);
if (!source || !existsSync(join(source, "manifest.json"))) {
  console.error("usage: node scripts/stage.mjs <archive dir> [name]");
  process.exit(1);
}
const manifest = JSON.parse(readFileSync(join(source, "manifest.json"), "utf8"));
if (manifest.schema !== "voice.loop.archive/1") {
  console.error(`${source}: schema ${manifest.schema} is not voice.loop.archive/1`);
  process.exit(1);
}
const target = join("public", "archives", name);
rmSync(target, { recursive: true, force: true });
mkdirSync(join(target, "stems"), { recursive: true });
for (const file of ["manifest.json", "timeline.jsonl", "waveform.json", ...manifest.stems.map((s) => s.file)]) {
  cpSync(join(source, file), join(target, file));
}
console.log(`staged ${source} as public/archives/${name}`);
