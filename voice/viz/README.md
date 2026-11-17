# Loop visualization

A Remotion project that turns a loop archive (`docs/voice/loop-archive.md`) into video. See `docs/voice/loop-viz.md`.

```bash
npm i
node scripts/stage.mjs ../archive/example example          # copy an archive into public/archives/
npx remotion studio                                          # preview
npx remotion render LoopReplay out/example.mp4 --props='{"archive":"example"}'
npx remotion render Blooper out/blooper.mp4 --props='{"edit":"example"}'
```

`public/archives/` and every edit except `public/edits/example.json` are gitignored, because a real run holds real voices and real words.
