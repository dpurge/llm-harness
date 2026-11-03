---
version: 26
status: approved
updated: 2026-11-03
---

# Roadmap

## Now

Nothing in progress.

## Next

Nothing queued.

## Later

- `command-execution-docker` — Docker backend for the executor, after a bounded spike
- `pgvector-index` — an optional ANN (HNSW) index per collection for the pgvector backend, with a configuration key, a recall comparison against exact search and the 2000-dimension limit; after a bounded spike
- `memory-reindex-all` — `/memory reindex --all`: re-embed every note into the current vector store, for after `[storage.vector]` changes backend or server (the SQLite markers in `memory_vectors` say "indexed" and `/memory reindex` skips those notes)
- `a2ui-nicegui` — A2UI v1.0 in NiceGUI: widgets ported from TypeScript compiled with esbuild, custom HTML and SVG components, bundles shipped prebuilt in the wheel

## Out of scope

Nothing deferred or rejected.
