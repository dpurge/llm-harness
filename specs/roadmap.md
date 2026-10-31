---
version: 17
status: approved
updated: 2026-10-30
---

# Roadmap

## Now

Nothing in flight.

## Next

Nothing queued.

## Later

- `command-execution` — run_command through a CommandExecutor interface, permissions backend first; needs `tool-approval`
- `memory` — episodic, semantic and procedural memory with remember/recall tools, keyed by `user_id`; needs `tool-approval`, `storage-config` and `embeddings`
- `command-execution-docker` — Docker backend for the executor, after a bounded spike
- `storage-backends-remote` — Postgres (relational) and qdrant (remote vector) as optional uv extras behind the `storage-config` interfaces, after a bounded spike
- `a2ui-nicegui` — A2UI v1.0 in NiceGUI: widgets ported from TypeScript compiled with esbuild, custom HTML and SVG components, bundles shipped prebuilt in the wheel

## Out of scope

Nothing deferred or rejected.
