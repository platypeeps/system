# local-worldmonitor

Wrapper for running world-monitor locally. The checkout path is
`WORLDMONITOR_DIR`, exported or set in `$SYSTEM_TOOLS_CONFIG/worldmonitor/.env`
(copy `.env.example`); this folder holds only the entrypoint.

## Usage

```sh
./worldmonitor.sh dev              # Vite dev server on :3000 (full variant)
./worldmonitor.sh dev tech         # variant build: tech|finance|happy|commodity|energy
./worldmonitor.sh build            # production build (tsc + vite build)
./worldmonitor.sh preview          # serve the last production build
./worldmonitor.sh lint             # biome lint + safe-html check
```

## Gotchas

- Port `3000` conflicts with `local-grafana` — only one at a time. `DEV_PORT`
  overrides the dev-server port (non-integer or out-of-range values silently
  fall back to 3000).
- `build` is not just `vite build`: it also runs the vite-env secret check
  (`--strict-local`) and the blog/crawlable/content corpus prebuilds, so it is
  slow and can fail on missing local env.
- Variants are `VITE_VARIANT` builds of the same app, not separate projects.
