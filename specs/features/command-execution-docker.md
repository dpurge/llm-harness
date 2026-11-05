---
title: Command execution in Docker
kind: feature
status: done
version: 1
updated: 2026-11-05
branch: main
---

## Problem / Motivation

`run_command` runs programs directly on the host through `PermissionsExecutor`. This feature adds a second backend that runs each command in a Docker container. It reuses the roadmap item `command-execution-docker` (`specs/roadmap.md`, line 19).

The backend is chosen by a new `[tools]` key. The default is the current behavior, so nothing changes unless the user opts in.

Decided by the author (2026-11-05): 

1. Verification gap: the author's agent can run real `docker run` only on macOS (Rancher Desktop, moby). The author said: write the code so that it should also work on Windows and Linux and do not assume a POSIX system on every machine. Linux and Windows runs stay UNVERIFIED by the agent until the user runs them (criterion 14).

Open assumption (not yet accepted by the author):

2. No startup probe: the backend does not check Docker at startup. Each call fails on its own with a clear error (docker not found, daemon down, image missing), returned as the tool observation. This is not decided.

## Acceptance Criteria

1. `ToolsConfig` accepts `command_backend` with the values `"permissions"` or `"docker"`. The default is `"permissions"` and then behavior is unchanged. Any other value is rejected (`extra="forbid"` stays).
2. `ToolsConfig` accepts `command_image` (str). It is required when `command_backend = "docker"`; config load fails with a clear error if it is missing.
3. The image is never pulled: `docker run` gets `--pull never` (docker run pulls a missing image by default). A missing image returns a clear error as the tool observation.
4. Sample invocation: `tool run_command {"argv": ["python", "-c", "print(1)"], "cwd": "<workspace>"}` runs as `docker run --rm --pull never --name <unique> -v <workspace>:/work -w /work --entrypoint= <image> python -c "print(1)"`. `--entrypoint=` makes argv[0] the program that runs, so the name check and the approval prompt describe what runs, whatever ENTRYPOINT the image has; an image that needs its ENTRYPOINT for setup does not work.
5. With no `cwd`, nothing is mounted (no `-v`).
6. The container has no `--network` flag, so it has outbound network access.
7. No CPU or memory limits are set.
8. `command_allow` and `command_deny` match `argv[0]` by name only. The built-in deny list still applies. Every call still needs approval.
9. On timeout, cancellation and any other exit before the client has exited, the backend runs `docker rm -f <name>` for that container, bounded by 10 s; a failed cleanup is ignored. After a timeout the container is gone.
10. Output capture reuses `HEAD_BYTES`/`TAIL_BYTES` (8192) and the timeout bounds 1..120.
11. A subagent's approval request reaches the human in the CLI, TUI and GUI, with `agent: <id>` shown. Piped stdin still means denied. A subagent still cannot delegate.
12. The argv builder and host-path to `-v` mapping is one pure function. Unit tests feed it Windows-style (`C:\Users\x\proj`) and POSIX paths on any host.
13. All unit tests run offline with a fake docker runner. A real-docker integration test is skipped when docker is absent, and the skip reason is stated.
14. Manual check for the user (UNVERIFIED by the agent): run the sample invocation on Linux and on Windows against a local image.

## Approach

Memory consulted: docker|command|subprocess|windows — 1 hit docker (memory.md:56, zsh glob gotcha, not relevant); command entries :54 (Process.wait hang, _close_pipes), :55 (venv python symlink), :57 (asyncio subprocess under uvloop) relevant to the executor; windows 0 hits

Decisions (user-confirmed, 2026-11-05):

- New `[tools]` keys in `ToolsConfig` (`config.py`, `extra="forbid"`): `command_backend` (`"permissions"` | `"docker"`, default `"permissions"`) and `command_image` (str, configurable, required for docker).
- The container has outbound network access: there is no `--network` flag. No CPU or memory limits.
- `cwd` is mounted read-write at `/work`. By convention agents use a `.scratch/` folder inside the workspace for scratch files. There is no new tool and no new config; they use it through `run_command`.
- Allow/deny match `argv[0]` by name only. The host `resolve_program` (`command_policy.py:73-93`) does not describe what runs in the container.
- Selection point: `builtin_tools.py:156-165` (single call site; the `command_executor` parameter is test-only). It needs a new `build_tool_registry` parameter, a line in `cli.py:518-536` and the `ToolsConfig` key.
- Protocol (`command_executor.py:55-60`): `check(argv, cwd) -> str | None` and `async execute(request: CommandRequest) -> CommandResult`. `CommandRequest` has argv, cwd, timeout_seconds. `CommandResult` has exit_code, timed_out, seconds, program, output_bytes, head, tail, truncated, error. Reuse the capture and timeout bounds (`command_tool.py:103-127`, config 1..120).
- Spike (2026-11-05, rancher-desktop context, image `registry:2`, local, no pull): after SIGKILL of the `timeout ... docker run --rm --name N` wrapper, the container was still `Up 4 seconds`. `docker kill N` stopped it and `--rm` removed it. Caveat: the killed process was the `timeout` wrapper, not docker itself. Consequence: the backend names each container (`--name`, unique) and runs `docker rm -f <name>` on timeout, cancellation and any other exit before the client has exited. It must NOT rely on `os.killpg` (`command_executor.py:225`, POSIX-only). Why `docker rm -f` and not `docker kill` (verified 2026-11-05 on Docker 29.6.2): `docker kill` fails on a created-but-not-started container with "is not running", `docker rm -f` removes it.
- Entrypoint: `--entrypoint=` goes before the image, so argv[0] is the program that runs, whatever ENTRYPOINT the image has (verified on Docker 29.6.2: `docker run --rm --pull never --entrypoint= registry:2 echo hi` prints hi; docker stops option parsing at the image).
- Portability: argument building and path mapping use plain string checks, not `pathlib`/`os.path`, so the Windows form can be unit-tested on any host (criterion 12). No POSIX mode bits, no `killpg`, no `O_NOFOLLOW`. Docker is started with `asyncio.create_subprocess_exec` and an argv list, never a shell string.
- Subagents (user chose option b): the parent's approver is passed down. Change points: `SubagentRun.__init__` (`subagents.py:83-94`) gets `approver` and passes it to the child `Orchestrator` (`subagents.py:100-115`); `orchestrator.py:736-745` passes `self._approver`. `ApprovalRequest` already carries `agent_id` and all three front ends print `agent: <id>`. The CLI path is `cli.py:609-620`.
- Consequence: this applies to every tool a subagent calls that asks for approval (today `run_command`; the memory tools declare `prompts = False` and still run without a prompt, corrected 2026-11-05). It REVERSES the earlier decision in `specs/features/tool-approval.md:34` and `:108` and the statements in `specs/features/command-execution.md:51,80,109,301,360`. Those must be updated in step B4. Tests that pin denial and must change: `tests/test_command_wiring.py:288-310` and `tests/test_tool_approval.py:184-197`.
- Delegation is sequential today, so one prompt at a time. Parallel subagents are out of scope (TUI single slot `tui/app.py:147/453` and CLI shared stdin would break).

Ordered plan (each step testable offline with a fake docker runner):

1. Pure argv builder, path mapping and tests.
2. `DockerExecutor` (check by name, execute with injected runner, remove by name) and tests.
3. Config keys and wiring (`config.py`, `builtin_tools.py`, `cli.py`) and tests.
4. Subagent approver pass-down and update the two pinning tests.
5. Manual run on macOS against the local rancher-desktop context with a user-supplied image.

## Affected Areas

- `src/llm_harness/config.py` (`ToolsConfig` keys)
- `src/llm_harness/builtin_tools.py` (selection point)
- `src/llm_harness/cli.py` (wiring, approver path)
- `src/llm_harness/command_executor.py` (Protocol reference; new `DockerExecutor`, location not decided)
- `src/llm_harness/subagents.py`, `src/llm_harness/orchestrator.py` (approver pass-down)
- `tests/test_command_wiring.py`, `tests/test_tool_approval.py` (pinning tests)
- `specs/features/tool-approval.md`, `specs/features/command-execution.md` (statements to update in B4)

## Out of Scope

- Resource limits
- Image pull or build
- Per-language images
- Parallel subagents
- Changing `PermissionsExecutor`
- Docker daemon management
- New file read/write tools
- Auto-detecting a runtime other than the `docker` CLI
- Podman

## Implementation Notes

Status (2026-11-05): steps 1-5 of 5 done, nothing committed.

Baseline before changes (offline, `uv run pytest -q -p no:cacheprovider`): 4349 passed, 0 skipped, 0 failed, 310 live tests deselected. `ruff check src tests`: 9 I001 import-order errors. `ruff format --check`: 2 files would be reformatted. All of these are pre-existing, all in files already modified before this feature (the author's uncommitted Windows-compat work), and not touched by this feature.

Step 1 done (pure argv builder and path mapping):

- New `src/llm_harness/docker_command.py`:
  - `new_container_name()` returns `llm-harness-cmd-` + uuid4 hex.
  - `mount_source(cwd)` works on text only. It accepts a POSIX absolute path or a Windows drive path (`X:\` or `X:/`). It rejects UNC (`\\` and `//`), relative paths (including `C:proj`) and any colon after the drive-letter position. It returns the source unchanged.
  - `docker_run_args(*, image, name, argv, cwd)` returns the arguments after `docker`: `run --rm --name <name> [-v <source>:/work -w /work] <image> <argv...>`. It raises ValueError for an empty image, an image starting with `-`, an empty name, an empty argv or an empty `argv[0]`.
  - No `--network`, CPU, memory, `-i` or `-t` flag. No os, pathlib or process calls, so it behaves the same on every host.
- New `tests/test_docker_command.py`: 25 tests, offline, no `posix_only`. 25 passed, 0 skipped, 0 failed. `ruff check` and `ruff format` are clean on both new files.
- `grep -rn docker_command src` outside the module: no hits. The functions have no non-test call site yet, intentionally, until steps 2 and 3.
- `tests/test_command_executor.py` and `tests/test_command_policy.py`: 154 passed.

Known low points (not requested, not changed): a path starting with `//` is rejected as UNC even on POSIX; a newline or NUL in cwd is not checked.

UNVERIFIED: the Windows path form against real Docker Desktop, and any run on Linux or Windows (criterion 14).

Step 1 amendment: `docker_run_args` now returns `run --rm --pull never --name <name> ...`, because `docker run` pulls a missing image by default (criterion 3). Test: `test_image_is_never_pulled`.

Step 2 done (`DockerExecutor`):

- New `src/llm_harness/docker_executor.py`: `DockerExecutor(image, allow, deny, *, docker="docker", spawn=asyncio.create_subprocess_exec, clock=time.monotonic)`.
- `check` matches `argv[0]` by name only: casefolded basename `fnmatch`, splitting on `/` and `\`. An entry with a separator must equal `argv[0]`. The built-in `DEFAULT_COMMAND_DENY` always applies, deny wins, an empty allow accepts the rest. It refuses a cwd that is not an existing directory and a cwd that `mount_source` rejects.
- `execute` re-runs `check`, then spawns `docker run ...` with stdin DEVNULL, stderr merged and the FULL host environment (the container does not inherit it; the client needs `DOCKER_HOST` etc.). It reuses `_Capture`, `_drain` and `_close_pipes` imported from `command_executor.py` (unchanged; imports of private names). Capture is 8192/8192 head+tail.
- Cleanup: `docker kill <name>` through the same `spawn` seam, only when the client has not exited (`proc.returncode is None`: timeout, cancellation, other errors). It is bounded by `KILL_WAIT_SECONDS = 10` and a failed kill is ignored. Then `proc.kill()`, close pipes, shielded wait. After a normal exit or a non-zero client exit no kill is issued (the container is gone through `--rm`; the user chose "restrict" on 2026-11-05).
- Errors: `FileNotFoundError` on spawn gives a `CommandResult` error "docker was not found (looked for 'docker'); install Docker or fix PATH"; any other `OSError` gives "could not start docker: ...". No startup probe.
- Medium confidence (code review): `proc.returncode` is set only once asyncio observed the exit, so a client that died just before the check may cause one harmless extra kill.
- New `tests/test_docker_executor.py`: offline, no `posix_only`. A fake spawn asserts the exact `docker run` argv, kill on timeout and cancellation, and no kill on normal or non-zero exit. A real `sys.executable` child behind the spawn seam proves head/tail truncation and a timeout that kills the client without hanging. One real-docker test is skipped unless env `LLM_HARNESS_DOCKER_TEST_IMAGE` is set and `docker version` works.

Step 3 done (config keys and wiring):

- `ToolsConfig.command_backend` (`Literal["permissions","docker"]`, default `"permissions"`, `config.py:533`) and `command_image` (`str | None`, default `None`, `config.py:535`). Validator `_command_image_rules` (`config.py:563`): under docker the image is required, non-empty, has no whitespace and must not start with `-`; under permissions an image is accepted and unused.
- Both keys are documented in the example config template (`config.py:1079-1080`: outbound network, read-write `/work`, never pulled).
- `build_tool_registry(command_backend=..., command_image=...)` (`builtin_tools.py:115-116`; docker branch `:166-174`). An injected `command_executor` still wins. Docker with a missing image raises ConfigError `invalid config.toml: [tools] command_backend = "docker" needs command_image, a local image name`.
- `cli.py:536-537` passes both values. It is the only `build_tool_registry` caller in `src` (TUI and GUI get the registry from it).
- Tests: `tests/test_config.py` (default, valid, docker without and with empty image, unsafe images `-v`, `--privileged`, space, tab, newline, invalid backends, template documents both keys, unknown key still rejected) and `tests/test_command_wiring.py` (default `PermissionsExecutor`, docker gives `DockerExecutor` with image and allow/deny, injected wins, missing image raises, docker ignored while `run_command` is off, CLI `config.toml` reaches the registry). The wiring tests read private attributes `_executor`, `_image`, `_allow`, `_deny`.

Verification (coordinator re-ran, 2026-11-05): `uv run pytest tests/test_docker_command.py tests/test_docker_executor.py tests/test_command_executor.py tests/test_command_policy.py tests/test_command_tool.py tests/test_command_wiring.py tests/test_config.py tests/test_cli.py tests/test_cli_orchestrator.py -q -p no:cacheprovider -rs`: 855 passed, 1 skipped, 0 failed (the skip is the real-docker test, env not set; unverified). Ruff check and format are clean on every file this feature created or edited. Pre-existing findings were left alone (`tests/test_config.py` one format hunk at the `bin/git` tuples, 9 I001 in other test files). The full suite was not re-run after the changes (baseline 4349 passed); it runs in validation. Call sites (grep): every new key and `DockerExecutor` have a non-test use (`cli.py:536-537`, `builtin_tools.py:166-174`, `config.py:563`).

UNVERIFIED (steps 2-3): any real `docker run` or `docker kill`, `--pull never` with a missing image, the Windows `docker.exe` spawn and `proc.kill()`, and any run on Linux or Windows.

Step 4 done (subagent approver pass-down):

- `src/llm_harness/subagents.py`: `SubagentRun.__init__` takes `approver: Approver | None = None` (imported from `llm_harness.approval`) and passes it to the child `Orchestrator`. The child keeps `can_delegate=False`.
- `src/llm_harness/orchestrator.py`: `_delegate` passes `approver=self._approver` into `SubagentRun` (the only non-test caller). Source change: 4 lines in 2 files.
- Behavior: a subagent's approval requests now reach the human through the parent's approver. The request's `agent_id` is the subagent's id, and the prompt text is the validated arguments built by the existing approval code (the subagent cannot alter it). The CLI, TUI and GUI prompts print `agent: <id>` (`cli.py:225`, `tui/app.py:444`, `gui/app.py:318`; checked by grep, not by driving a real prompt).
- Piped or non-TTY CLI still has approver None, so subagent calls are denied there with the existing "needs the user's approval ... none is available here" text. This is pinned by unit tests with a None approver; there is no CLI-level test for subagent calls under piped stdin.
- Applies to every tool a subagent calls that asks for approval (today `run_command`). The memory tools are `write` risk but declare `prompts = False` (`memory_tools.py:166,189,221`, `tools.py:58-60`), so they still run without a prompt in a subagent; an earlier version of this note said they would prompt, which was wrong (corrected 2026-11-05). This reverses `tool-approval.md:34` and `:108`; those statements are updated in B4.
- Delegation is sequential, so one prompt at a time; no queue added.
- Tests: `tests/test_tool_approval.py` replaced `test_a_child_subagents_write_tool_is_denied` with three tests (approved child write tool runs and the request names the child with the exact arguments text; denied by the human is not run; no approver is still denied) plus `test_the_parents_approver_gets_the_delegated_childs_tool_call` (orchestrator, delegate, child tool call; the parent's approver instance is called with the child's agent_id; the child's own `delegate` gets "delegate is not available to subagents"). `tests/test_command_wiring.py` replaced `test_a_subagent_that_names_run_command_gets_no_approval_and_nothing_runs` with three tests (approved runs and names the subagent with exact JSON arguments; denied does not run; no approver does not run). Other denial-pinning tests concern the top-level orchestrator and needed no change.
- The engineer reported that removing `approver=self._approver` temporarily made the delegation test fail (`tests/test_tool_approval.py:261`) and restoring it made it pass (reported, not repeated by the coordinator).
- Verification (coordinator re-ran, 2026-11-05): `uv run pytest tests/test_subagents.py tests/test_tool_approval.py tests/test_command_wiring.py tests/test_orchestrator.py tests/test_docker_executor.py -q -p no:cacheprovider -rs`: 200 passed, 1 skipped, 0 failed (the skip is the real-docker test, env not set). Ruff check and format clean on the four edited files. The full suite was not re-run (baseline 4349 passed).

UNVERIFIED (step 4): a real CLI, TUI or GUI approval prompt for a subagent.

Step 5 done (manual macOS run), coordinator ran these himself on 2026-11-05 on macOS, Rancher Desktop, `DOCKER_CONTEXT=rancher-desktop`, local image `registry:2` (Alpine, has `echo`, `touch`, `ls`, `sleep`), nothing pulled:

- `LLM_HARNESS_DOCKER_TEST_IMAGE=registry:2 uv run pytest tests/test_docker_executor.py`: 35 passed, 0 skipped (the real-docker `echo hello` test ran and passed).
- A scratch script (not committed, in the session scratchpad) drove the real `DockerExecutor`:
  - `sleep 30` with timeout 3 returned `timed_out=True`, `exit_code=None` in 3.1 s; `docker ps -a --filter name=llm-harness-cmd` listed no container afterwards (`docker kill` by name works for real).
  - An image that does not exist returned exit 125 with output "No such image: llm-harness-no-such-image:0"; nothing was pulled (`--pull never` holds).
  - `touch a.txt` with a temporary host directory as cwd exited 0 and the file exists on the host; `ls /work` in a second container listed it (read-write mount works).
  - The `sh` program was refused by the built-in deny list as designed (container commands in the deny list stay refused by name).

UNVERIFIED (step 5): Linux and Windows (the author runs these: criterion 14), outbound network from the container (no `--network` flag is passed; no network command was run), a real CLI/TUI/GUI approval prompt for a subagent, the Windows `docker.exe` spawn and `proc.kill()`.

What is next: B3 validation (full suite, call-site grep, review), then B4 documentation (README, CHANGELOG, tool-approval.md:34 and :108, command-execution.md:51,80,109,301,360, tech-stack.md).

Review fixes (2026-11-05):

Security review (reviewer, read-only) found:

- M1: a timeout landing between container create and start left the container running, because `docker kill` fails on a created container.
- M2: an image ENTRYPOINT receives argv as arguments, so the name check and the prompt could describe a program that never runs.
- M3: cwd can be any host directory, mounted read-write into a container that runs as the image's user, often root; on rootful Linux Docker a call with cwd `/etc` can write there as root, which the host backend cannot.
- M4: under docker, path entries in `command_allow`/`command_deny` match only the exact text, so a deny of `/usr/bin/python3` is bypassed by `python3`; the host backend blocks every spelling.

Decisions by the author (asked and answered, 2026-11-05):

- M1 fixed with `docker rm -f <name>`: `_kill_container` renamed `_remove_container`; the cleanup client is reaped with a bounded 2 s `_REAP_WAIT_SECONDS` if it times out; tests renamed, plus a test that a failing `docker rm` exit still returns `timed_out`.
- M2 fixed with `--entrypoint=` before the image. Test: after the mount flags, immediately before the image, argv[0] right after the image.
- M3 documented only (comments in `config.py`).
- M4 documented only (comments in `config.py`: use plain program names; inside the container the list guards against mistakes and is not a sandbox).

Coordinator re-ran after the fixes: 7-file subset `uv run pytest tests/test_docker_command.py tests/test_docker_executor.py tests/test_command_executor.py tests/test_command_wiring.py tests/test_config.py tests/test_tool_approval.py tests/test_subagents.py`: 590 passed, 1 skipped (real-docker test, env not set), 0 failed. Ruff check and format clean on `docker_executor.py`, `docker_command.py`, `config.py`, `tests/test_docker_command.py`, `tests/test_docker_executor.py`. With `DOCKER_CONTEXT=rancher-desktop LLM_HARNESS_DOCKER_TEST_IMAGE=registry:2`: `tests/test_docker_executor.py` 36 passed, 0 skipped. Real executor: five consecutive 1 s timeouts each returned `timed_out` in 1.1 s with no `llm-harness-cmd` container left; `echo hi` with `--entrypoint=` printed hi; `touch b.txt` created the file on the host. The start race is intermittent: five clean runs make the fix plausible, they do not prove it.

Known limitations the review raised, not fixed:

- L1: the built-in deny list does not cover container shells and wrappers (`ash`, `busybox sh -c`, `rbash`), and interpreters (`python -c`) are allowed by design; inside the container the list guards against mistakes only, the human reading argv is the boundary.
- L2: no cleanup when the harness is killed hard (SIGTERM, closed terminal, `os._exit`): a running container survives, only task cancellation cleans up.
- L3: cancellation inside the spawn call itself issues no cleanup (about a millisecond window).
- L5: on Linux with rootful Docker, files written to /work are owned by root on the host; SELinux hosts may need `:z` on the mount; `--pull never` needs Docker 20.10 or later; a Windows drive root `C:\` gives source `C:\:/work` (unverified with Docker Desktop); a `docker.cmd` shim is not found; the GUI needs the Proactor event loop on Windows.
- L6: the `run_command` tool description and the approval prompt still say "on this machine" and do not show the backend or image.
- L7: Docker's own error text can echo `DOCKER_HOST` into the observation (redaction is heuristic), and `proxies` in `~/.docker/config.json` are injected into containers by the client.

## Validation

Baseline before the feature: 4349 passed, 0 skipped, 0 failed, 310 live deselected (`uv run pytest -q -rs -p no:cacheprovider`).

First full run after steps 1-5 (tester, before the review fixes): 4441 passed, 1 skipped, 0 failed, 310 deselected, 1 warning (unrelated google.genai DeprecationWarning) in 170 s: +93 tests, none lost. The skip is the opt-in real-docker test, which passed when run with env `LLM_HARNESS_DOCKER_TEST_IMAGE=registry:2`, `DOCKER_CONTEXT=rancher-desktop` (35 passed, 0 skipped at that time).

Lint: ruff check clean on 12 files; ruff format --check clean except the pre-existing hunk in `tests/test_config.py` (the `bin/git` tuples); 9 pre-existing I001 in other test files untouched.

Call sites (grep, tester): `DockerExecutor` builtin_tools.py:174; `docker_run_args` docker_executor.py (call in `execute`); `new_container_name` docker_executor.py; `mount_source` docker_executor.py and docker_command.py; `command_backend` cli.py:536, builtin_tools.py:166, config.py; `command_image` cli.py:537, builtin_tools.py:174, config.py; `KILL_WAIT_SECONDS` docker_executor.py; `SubagentRun` `approver` forwarded in subagents.py and passed at orchestrator.py:745 from the front ends (cli.py:621, tui/app.py:211, gui/app.py:253).

Portability scan of `docker_command.py` and `docker_executor.py`: no killpg, SIGKILL, start_new_session, fcntl, termios, geteuid, O_NOFOLLOW, fchmod or st_mode; one `cwd.startswith("/")` in `docker_command.py`, the explicit POSIX branch beside the Windows drive-letter check.

Security review: see Implementation Notes (reviewer, read-only; findings M1-M4 handled as above).

Full suite after the review fixes (coordinator, 2026-11-05, `uv run pytest -q -rs -p no:cacheprovider`, exit 0, 169 s): 4443 passed, 1 skipped, 0 failed, 310 live deselected, 1 warning (unrelated google.genai DeprecationWarning): +94 tests against the 4349 baseline, none lost. The one skip is `tests/test_docker_executor.py:415`, the opt-in real-docker test (env `LLM_HARNESS_DOCKER_TEST_IMAGE` not set); with the env set it passed (36 passed, 0 skipped in that file).

Real runs by the author on Linux and Windows (criterion 14) are not done.

Closeout facts (B5, 2026-11-05):

- Skip explained: the 1 skipped test is the opt-in real-docker test in `tests/test_docker_executor.py`; it runs only when env `LLM_HARNESS_DOCKER_TEST_IMAGE` names a local image, and it passed with the env set (36 passed, 0 skipped in that file).
- Counts present: 4443 passed, 1 skipped, 0 failed (full suite above).
- Non-test call sites: already listed above (Call sites).
- CI review recorded in Documentation Review (no CI workflow in the repository).
- The `## Now` line in `specs/roadmap.md` is removed.

## Documentation Review

- Changelog: one only, `CHANGELOG.md` (`specs/tech-stack.md` has no `### Artifacts` table: `grep -n "Artifacts" specs/tech-stack.md` finds no match, so the single implicit artifact applies).
  - `{CHANGELOG.md, Added, "Docker backend for `run_command`: `[tools] command_backend = \"docker\"` with `command_image` (a local image, never pulled) runs each command in its own container with the working directory mounted read-write at `/work`; the default `command_backend = \"permissions\"` is unchanged."}`
  - `{CHANGELOG.md, Changed, "Subagents can now run tools that need approval: each request asks you, with the subagent's id (`agent: <id>`), through the same prompt as the orchestrator's; before, such calls were always denied. Piped `--plain` still denies them."}`
- README (`README.md`): `### Tools` config block and the `### Command execution (`run_command`)` section covered `run_command`. Stale and fixed: "A subagent has no approver either, so a `run_command` call from a subagent is never run" (now: asks through the same approver); "A Docker backend is a later roadmap item" (now points to the new `#### Docker backend` subsection); in `## Known limits` "there is no Docker backend yet". Not changed, still true: `### Agent memory` says memory write tools run with no prompt, also inside a subagent (they skip the prompt through `tool_prompts`, `orchestrator.py` `_approve`).
- Specs: `specs/features/tool-approval.md` (lines 12, 34, 104, 108) and `specs/features/command-execution.md` (lines 14, 22, 51, 80, 88, 109, 126, 137, 144, 301, 360) now carry a dated note "Superseded by command-execution-docker (2026-11-05)". The original sentences are kept. Left alone as history: `tool-approval.md` line 153 (the changelog entry proposed then) and the command-execution-docker line 57 that records this reversal.
- Constitution drift (proposed, NOT applied, awaiting the author's approval): `specs/tech-stack.md` line 53 ("no approver means deny (subagents, piped `--plain`)"; "(roadmap: `command-execution-docker`; docker, API and scraper tools)"), line 107 ("subagents get no approver, so they cannot run it"; "a Docker backend is the roadmap item `command-execution-docker`"), line 72 (the Layout list has no `docker_command.py`, `docker_executor.py`), and a new decision entry for the `[tools]` keys `command_backend` and `command_image`. `specs/roadmap.md`: the `## Now` line is removed in B5.
- CI review: not applicable (the repository has no CI workflow, Dockerfile, Makefile, task-runner or release script: `ls -a` shows no `.github`, no `scripts/`, no `Makefile`, no `Dockerfile`; `pyproject.toml` has only `[project.scripts]`, `[build-system]` and `[tool.pytest.ini_options]`). `git diff -- pyproject.toml uv.lock` is empty: no dependency was added. `LLM_HARNESS_DOCKER_TEST_IMAGE` is opt-in, so the real-docker test skips with no env set.
  - `{.github/*, needs change: no, does not exist}`, `{pyproject.toml, no, unchanged}`, `{uv.lock, no, unchanged}`.

## Documentation Updates

- `README.md`: new `#### Docker backend` subsection under `### Command execution (`run_command`)`; the `### Tools` config block gains the `command_backend` and `command_image` lines; the subagent sentence now says a subagent's `run_command` call asks through the same approver; `## Known limits` no longer says there is no Docker backend.
- `CHANGELOG.md`: two entries under `## [Unreleased]`, Added (Docker backend for `run_command`) and Changed (subagents can run tools that need approval); quoted in Documentation Review.
- `specs/features/tool-approval.md` and `specs/features/command-execution.md`: dated notes "Superseded by command-execution-docker (2026-11-05)" at the lines listed in Documentation Review; original sentences kept.
- `specs/tech-stack.md`: constitution update to version 38 (`updated: 2026-11-05`, `status: approved`) with six author-approved edits (approver sentence, roadmap pointer, subagent sentence, Docker backend sentence, a `command-execution-docker` entry in the `run_command` decision, the Layout list); see that file, not duplicated here.
- `specs/roadmap.md`: the `## Now` line for this feature is removed, leaving "Nothing in progress." (no version bump, per the `## Now` lifecycle).
- `specs/memory.md`: four entries appended (docker cleanup with `docker rm -f`, `--pull never` and `--entrypoint=`, the opt-in real-docker test env, the zsh unquoted-variable gotcha).
