---
title: "Embeddings: Embedder interface over embedding models declared in models.yaml"
kind: feature
status: done
version: 1
updated: 2026-10-28
branch: main
---

## Problem / Motivation

Roadmap item `embeddings`. No embedding code exists in `src/llm_harness/`. `ChatProvider` (`src/llm_harness/providers/base.py`) has only `stream_chat` and `aclose`, and `ModelConfig` (`src/llm_harness/config.py`) has no way to mark a model as an embedding model safely. The first consumers (`tool-registry`, `tools-first-set`) need to turn text into vectors for the vector store built by `storage-config`.

An entry with the tag `embedding` already loads today (tags are free-form), but nothing stops it from also carrying `chat`, `default` or `orchestrator`, and both chat selectors in the UIs and the CLI `--model` option would list or accept it. This feature adds the config rules, a selector, an `Embedder`, an `embed` method on the three provider adapters, and hides embedding models from chat. Design source: `specs/tech-stack.md`, bullet "Embeddings (planned, not built)".

## Acceptance Criteria

1. `ModelConfig` accepts an optional `dimensions` (a positive integer). `dimensions: 0` or a negative value is rejected. Absent, it is `None` and behaviour is unchanged for every existing entry.
2. An entry tagged `embedding` together with any of `chat`, `default` or `orchestrator` is rejected at config load. The error names the entry id and the conflicting tag (test: entry `bad` tagged `[embedding, chat]` gives an error containing `bad` and `chat`). An entry tagged only `[embedding]` loads.
3. `select_embedding_model(config, model_id: str | None)` has the same shape as `select_orchestrator_model`.
   - With `model_id` given, it returns that entry if it has the tag `embedding`, otherwise raises `ConfigError` naming the id.
   - With `None`, it returns the top-priority entry tagged `embedding`.
   - Two embedding entries at the same top priority raise `ConfigError` (a tie is an error).
   - No embedding entry raises `ConfigError` with a message that says no model has the tag `embedding`.
4. `chat_models(config)` returns the entries NOT tagged `embedding`. The GUI model selector and the TUI model selector list only `chat_models(config)`. A config with `bge-m3-local` tagged `[embedding]` and one chat model shows only the chat model in both (tests for both).
5. `llm-harness --model <id>` where `<id>` is an embedding model fails before any chat starts with `error: ...` on stderr and exit code 1. The message names the id and the tag `embedding`. An unknown id keeps its current error.
6. `EmbeddingError` is a typed exception carrying an `ErrorKind` (the existing enum in `src/llm_harness/types.py`) and a redacted message. A message never contains an API key, token or full request URL with credentials (test with a key placed in the faked SDK exception text).
7. `Embedder` wraps a provider and one `ModelConfig`. It exposes `model_id`, `dimension` and `async embed(texts: list[str]) -> list[list[float]]`.
   - `embed` returns one vector per input text, in input order.
   - An empty list returns `[]` without any provider call (ASSUMPTION: chosen default; test asserts zero calls).
   - A failing provider call raises `EmbeddingError` with the matching kind; there is no fallback to another embedding model and no result type.
8. `dimensions` is passed to the provider call only when set on the entry. Test per adapter with a faked SDK: unset gives a call without the argument, set to 256 gives a call with it. The SDK argument names come from the installed SDKs (see Approach).
9. Each adapter has `embed(model, texts, dimensions)`: Ollama (`providers/ollama.py`), OpenAI-compatible (`providers/openai_compat.py`) and Vertex (`providers/vertex.py`). Each maps SDK errors to the same `ErrorKind` values its chat path uses. Contract tests use faked SDKs; none touch the network.
10. Tests use a fake embedder in `tests/fakes.py`. The opt-in live test (marker `live`, deselected by default) embeds two short texts with `bge-m3` on local Ollama and expects 2 vectors of 1024 dimensions with L2 norm about 1.0. It skips with a printed reason when Ollama or the model is absent.
11. Existing behaviour is unchanged: with no embedding entry configured, chat, the orchestrator and the ledger behave exactly as today, and the existing suite stays green.
12. README, CHANGELOG, `specs/tech-stack.md` (planned to built) and `specs/roadmap.md` are updated. The full suite and ruff pass; every skip is explained.

## Approach

Memory consulted: grep '[Ee]mbed' (0 hits), '\[gotcha\]' (29 hits), '[Oo]llama' (2 hits) in specs/memory.md — none relevant to embeddings (two Ollama entries concern structured output; qdrant-edge gotchas at memory.md:32-36 are relevant only to a later consumer)

An embedding model is configured like any other model: an entry in `~/.config/llm-harness/models.yaml` with the tag `embedding`, using a provider declared in `config.toml`. First model: bge-m3 through the local Ollama provider (`id` `bge-m3-local`, `model` `bge-m3`, `tags` [embedding], price 0/0). No `[embeddings]` table is added. No fallback between embedding models, because vectors from two models are not comparable.

Human decisions (2026-10-28):

1. No non-test call site now. `Embedder`, the adapters' `embed` and `select_embedding_model` are intentionally unused outside tests until the first consumer (`tool-registry` or `tools-first-set`). This is an explicit, human-approved exception to the "no uncalled code" rule; the reason is that the consumers are separate roadmap items that need this interface first.
2. Embedding models are hidden from chat through a helper `chat_models(config)` (entries NOT tagged `embedding`), used by the GUI selector, the TUI selector and the CLI `--model` check. These ARE called from non-test code.
3. `embed` raises a typed `EmbeddingError` (existing `ErrorKind`, redacted message); no fallback; no result type.

SDK signatures are recorded in `specs/tech-stack.md` (Embeddings bullet). The engineer must re-verify each with the `api-verification` skill against the installed version before use. Vertex acceptance of `output_dimensionality` is UNVERIFIED.

Ordered plan; each step is small, runnable and has its own tests; the human approves between steps:

1. **Config.** `dimensions` field; the tag-combination rule as a per-entry model validator (pattern: `ProviderConfig._check_rules` in `config.py`); `select_embedding_model`; `chat_models`; tests in `tests/test_config.py`. Visible check: a scratch `models.yaml` with `[embedding, chat]` fails naming the id.
2. **Chat filter.** Use `chat_models` in the GUI and TUI selectors and in the CLI `--model` check; tests in `tests/test_gui.py`, `tests/test_tui.py`, `tests/test_cli.py`.
3. **`EmbeddingError`, `Embedder`, Ollama `embed`.** `EmbeddingError` and `Embedder` (reports model id and dimension; wraps provider and `ModelConfig`; `embed(texts) -> list[list[float]]`); Ollama `embed`; a fake embedder in `tests/fakes.py`; Ollama contract test with a faked SDK.
4. **OpenAI-compatible and Vertex `embed`** with faked SDKs.
5. **Opt-in live test** for bge-m3 on local Ollama (skips with a printed reason; expects 1024-dimension vectors, norm about 1.0).
6. **Documentation:** README, CHANGELOG, `specs/tech-stack.md` bullet from planned to built, `specs/roadmap.md`.

## Affected Areas

Planned:

- `src/llm_harness/config.py` (`ModelConfig.dimensions`, validator, `select_embedding_model`, `chat_models`)
- `src/llm_harness/types.py` (only if `ErrorKind` needs a value; otherwise untouched)
- `src/llm_harness/embeddings.py` (new: `EmbeddingError`, `Embedder`; module name is a proposal)
- `src/llm_harness/providers/ollama.py`, `openai_compat.py`, `vertex.py`, and `providers/base.py` (embed method)
- `src/llm_harness/gui/app.py`, `src/llm_harness/tui/app.py`, `src/llm_harness/cli.py` (chat filter)
- `tests/fakes.py`, `tests/test_config.py`, `tests/test_gui.py`, `tests/test_tui.py`, `tests/test_cli.py`, adapter tests, new embedder tests, `tests/test_live_ollama.py`
- `README.md`, `CHANGELOG.md`, `specs/tech-stack.md`, `specs/roadmap.md`

## Out of Scope

- The in-process bge-m3 ONNX backend (downloaded on first use): deferred, not rejected; its candidate runtime and rules stay in `specs/tech-stack.md`.
- Any non-test caller of `Embedder`, `embed` or `select_embedding_model`: human-approved exception, they stay unused outside tests until `tool-registry` / `tools-first-set`.
- Storing the embedding model id per vector collection and refusing a model mismatch: moves to the first consumer feature (the vector store does not store a model id today).
- Fallback between embedding models, and a result type for `embed`.
- Chunking, batching limits, retries and caching of embeddings.
- An `[embeddings]` config table.
- Remote or paid embedding models beyond what the three existing providers already reach.

## Implementation Notes

As built, step by step (the plan itself was unchanged):

1. Config (`src/llm_harness/config.py`, `tests/test_config.py`): `ModelConfig.dimensions: PositiveInt | None`; `EMBEDDING_EXCLUSIVE_TAGS` and the `_check_embedding_tags` validator (error text "model '<id>' is tagged 'embedding' and cannot also be tagged '<tag>'"); `select_embedding_model`; `chat_models`. +19 tests (1592 -> 1611 passing).
2. Chat filter: the GUI selector (`gui/app.py`) and the TUI selector (`tui/app.py`) list `chat_models(config)`. The `--model` check lives in `config.py` as `_lookup_chat_model`, used by `select_model` and `select_orchestrator_model` when an id is given (message "model '<id>' is tagged 'embedding' and cannot be used for chat"); `cli.py` is unchanged, it already prints `error: ...` and exits 1. DIVERGENCE from Approach item 2 and criterion 5's wording: the CLI check does not call `chat_models`; it uses the same tag test inside `config.py`, so there is one place to maintain. +6 tests (1617).
3. `src/llm_harness/embeddings.py`: `EmbeddingError(kind, message)` and `Embedder` (`model_id`; `dimension` is the entry's `dimensions` when set, otherwise learned from the first vector and `None` before any call; a mismatch raises). `EmbeddingProvider` Protocol in `providers/base.py`; `OllamaProvider.embed`; `FakeEmbedder` in `tests/fakes.py`; `tests/test_embeddings.py` (8) and Ollama adapter tests (15). +23 (1640). Real call against local Ollama: 2 vectors, 1024 dimensions, norm 1.0.
4. OpenAI-compatible `embed` (sends `encoding_format="float"` explicitly because the SDK defaults to base64, which compatible servers need not support; sorts data by `index`) and Vertex `embed` (`EmbedContentConfig(output_dimensionality=...)` only when set). Vertex chat error mapping was refactored into `_error_kind` and `_SDK_ERRORS`, shared by chat and embed; chat behaviour is unchanged (contract tests pass). +41 tests (1681).
5. Opt-in live test `tests/test_live_ollama.py::test_live_ollama_embeds_two_texts` (marker `live`, skips with a printed reason; expects 2 vectors of `dimensions` or 1024, norm within 0.01 of 1.0). The dimension rule in the test is wider than criterion 10's fixed 1024: it accepts the entry's `dimensions` when set. 1681 passed, 5 deselected.
6. Config documentation, requested by the human ("make sure config files are well documented with examples"): `EXAMPLE_CONFIG_TOML` and `EXAMPLE_MODELS_YAML` were rewritten with explanatory comments for every key and a commented-out `bge-m3-local` embedding entry between marker comments. Tests load both examples with the real loader, load the embedding block when uncommented (with and without `dimensions`), and fail when a field of `ModelConfig`, `Price` or `ProviderConfig` is left undocumented in the examples. +21 tests (1702). A review found the first draft of the `tags` comments wrong (it said `default` is the starting model and orchestrator models are tried first on failure); corrected to match README and `routing.py` (start = highest-priority `orchestrator`; fallback = other `orchestrator` models, then `default` models). README Configuration blocks now equal the constants exactly (checked by string comparison); new README `### Embedding models`; README `## Tests` has the live command.

Human-approved exception (decision 1 in Approach): `Embedder`, the adapters' `embed` and `select_embedding_model` have no non-test caller until the first consumer feature.

## Validation

Tester's B3 run, `uv run pytest -rs -q`: 1702 passed, 0 skipped, 0 failed, 5 deselected, 1 warning (google-genai DeprecationWarning about aiohttp ClientSession inheritance; third-party, present at baseline). Baseline before the feature: 1592 passed, 0 skipped, 4 deselected. `uv run ruff check` pass; `uv run ruff format --check` 102 files formatted (no type checker is configured).

Live: `-m live tests/test_live_ollama.py -k embed` against the real user config: 1 skipped with reason "no model has the tag 'embedding'" (the user's `models.yaml` has no embedding entry, so unverified there). Against a scratch home with a `bge-m3-local` entry on localhost:11434: 1 passed.

Real artifact run (scratch HOME):
- `--model bge-m3-local` -> `error: model 'bge-m3-local' is tagged 'embedding' and cannot be used for chat`, exit 1.
- `models.yaml` with tags [embedding, chat] -> `error: invalid models.yaml: 1: Value error, model 'bge-m3-local' is tagged 'embedding' and cannot also be tagged 'chat'`, exit 1.
- Real `Embedder` through the Ollama adapter: 2 vectors, lengths [1024, 1024], norm 1.0, dimension 1024.

Criterion-to-test map (test names confirmed by grep at closeout):
- 1: `test_dimensions_absent_is_none`, `test_dimensions_valid_value_loads`, `test_dimensions_zero_or_negative_is_a_config_error` (`tests/test_config.py`).
- 2: `test_embedding_with_a_chat_tag_is_a_config_error_naming_id_and_tag`, `test_embedding_with_several_chat_tags_names_each`, `test_embedding_tag_alone_loads`, `test_embedding_with_unrelated_tag_loads`.
- 3: `test_embedding_picks_highest_priority_tagged_model`, `test_embedding_explicit_id_returns_that_entry`, `test_embedding_explicit_id_without_the_tag_is_error_naming_id`, `test_embedding_unknown_id_lists_valid_ids`, `test_embedding_tie_at_top_priority_names_the_ids`, `test_embedding_none_tagged_is_error_saying_no_model_has_the_tag`.
- 4: `test_chat_models_excludes_embedding_and_keeps_file_order` (`tests/test_config.py`); `test_selector_hides_embedding_models` in `tests/test_gui.py` and `tests/test_tui.py`.
- 5: `test_model_flag_naming_an_embedding_model_fails_before_any_chat`, `test_plain_model_flag_naming_an_embedding_model_fails_without_a_provider`, `test_model_flag_naming_a_chat_model_still_works_with_an_embedding_entry` (`tests/test_cli.py`).
- 6, 7: `tests/test_embeddings.py` (8 tests); the redaction tests are `test_embed_error_message_is_redacted` in each adapter test file.
- 8, 9: `test_embed_*` in `tests/test_ollama_adapter.py` (8 named), `tests/test_openai_compat_adapter.py` (9) and `tests/test_vertex_adapter.py` (9), for example `test_embed_without_dimensions_sends_no_dimensions_argument` and `test_embed_with_dimensions_sends_them` (Vertex: `test_embed_without_dimensions_sends_no_config`, `test_embed_with_dimensions_sends_output_dimensionality`).
- 10: `FakeEmbedder` (`tests/fakes.py:121`); the live test `test_live_ollama_embeds_two_texts`.
- 11: the existing suite stays green (1702 passed).
- 12: documentation, see Documentation Review.

Call-site grep (tester): every new name has a non-test use except `Embedder`, the adapters' `embed` and `select_embedding_model` (the approved exception). At closeout I re-checked `chat_models` (used in `gui/app.py`, `tui/app.py`) by grep.

Unverified:
- Vertex `output_dimensionality` acceptance by the real service.
- The exception types raised by the real OpenAI-compatible and Vertex embed calls (tests inject the classes the chat path already maps).
- Ollama with `dimensions` set was tested only with a fake client.
- The live test against the user's own config (no embedding entry there).
- The GUI and TUI selectors were verified by tests only, not by hand.

## Documentation Review

- `CHANGELOG.md` (the only changelog; no Artifacts table), under `## [Unreleased]`:
  - Added: "Embedding models in `models.yaml`: the tag `embedding`, an optional `dimensions`, an `Embedder` with `embed` on the Ollama, OpenAI-compatible and Vertex adapters, and an opt-in live test (`uv run pytest -m live tests/test_live_ollama.py -k embed -rs`); nothing calls them yet." (line 31)
  - Changed: line 41, starting "Embedding models are hidden from the model selectors and rejected by `--model`; an entry tagged `embedding` together with `chat`, `default` or `orchestrator` is a config error; the example config files ..." (read from the file; the line is longer than quoted here).
- `README.md`: Configuration blocks synced to the example constants, new `### Embedding models`, Tests section has the live command.
- Constitution drift: `specs/tech-stack.md` lines for the model registry bullet, the Embeddings bullet and the Infrastructure bullet said planned; updated with the human's approval to built (version 17). The deferred ONNX backend and the model-id-per-collection storage stay not built.
- `specs/roadmap.md`: the item moved to `## Now` at B2 and was removed at B5.
CI review: not applicable (no `.github`, Dockerfile, Makefile, task runner or release workflow exists; the only build file is `pyproject.toml` (uv_build, version 0.1.0), and the feature adds no dependency, entry point or build step; the `live` pytest marker already existed). Verified at closeout: `ls -a` shows no `.github`, Dockerfile or Makefile, and `find` for `*.yml`, `*.yaml`, Dockerfile, Makefile and justfile (depth 3, excluding `.venv` and `.git`) returned nothing. `git diff pyproject.toml` shows one added line, `qdrant-edge-py>=0.9.0`, which belongs to the `storage-config` feature, not this one.

## Documentation Updates

- `README.md`: Configuration blocks, `### Embedding models`, Tests (live command).
- `CHANGELOG.md`: 2 lines (one Added, one Changed).
- `specs/tech-stack.md`: planned to built, version 17; pointer only, see that file for the change.
- `src/llm_harness/config.py`: `EXAMPLE_CONFIG_TOML` and `EXAMPLE_MODELS_YAML` documented with comments.
