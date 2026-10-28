---
version: 12
status: approved
updated: 2026-10-28
---

# Roadmap

## Now

Nothing in flight.

## Next

Nothing queued.

## Later

- `agent-registry` — Agents declared in configuration with tags (the orchestrator, researcher, writer, reviewer and so on)
- `tool-registry` — Typed tools with tags, a registry, and the `tool_search` tool for the orchestrator (needs `agent-registry` and `storage-config`; vector search in `tool_search` uses `embeddings` when configured, lexical-only otherwise)
- `subagents` — Subagents with per-subagent tool lists and model tags (needs `agent-registry` and `tool-registry`)
- `tools-first-set` — Tools: web search, RAG query, memory access, pre-programmed web scrapers and API calls (needs `tool-registry`, `storage-config` and `embeddings`)
- `storage-backends-remote` — Postgres (relational) and qdrant (remote vector) as optional uv extras behind the `storage-config` interfaces, after a bounded spike
- `a2ui-nicegui` — A2UI v1.0 in NiceGUI: widgets ported from TypeScript compiled with esbuild, custom HTML and SVG components, bundles shipped prebuilt in the wheel

## Out of scope

Nothing deferred or rejected.
