---
version: 11
status: approved
updated: 2026-10-29
---

# Mission

## Problem

There is no hands-on way to find out whether LLMs reached through the Ollama, OpenAI and Vertex AI APIs can be applied to practical tasks at low cost. This project is an LLM coding-agent harness in Python that is used to experiment with those APIs. It is a personal project. Today the harness streams chat over Ollama, OpenAI-compatible APIs and Vertex AI through a plain line mode, a terminal UI and a NiceGUI web UI; a failing model is skipped in favour of the next suitable one and every call's tokens and cost are recorded; every message goes through an orchestrator that keeps a typed state instead of the chat history; the orchestrator can delegate to subagents declared in agents.yaml; real tools are not built yet.

## Users

The author, on their own hobby projects, as a single developer. The harness is a desktop, interactive application used by one person at a time: it is not a service in a container and not a batch runner. Two interfaces over one shared core: a terminal UI and a NiceGUI web UI.

## Value proposition

Success is that the author uses the harness on their own hobby projects and it works with acceptable reliability on those projects.

The rest of this section describes the goal. What exists today: streaming chat over the three APIs, models declared in configuration with tags and priority, the orchestrator model chosen by tag and priority, per-model prices, fallback to the next suitable model when one fails (with a persisted backoff), a record of tokens and cost per call with a usage line that shows the session total, display of the model's thinking text, and an orchestrator with a typed state (goal, sourced findings, plan, a simple environment block with date and time, user name, computer name and `user_id`) and a reply contract of a state patch plus one action, and a local storage layer (an SQLite relational store, a file store and a qdrant-edge vector store, chosen in `[storage]` in `config.toml`; only the usage ledger uses it so far), agents declared in `agents.yaml` with tags (the orchestrator agent's id and tags are used, and any other agent with examples is a subagent the orchestrator can delegate to; the State tab lists them), typed tools with tags in a registry, and the tool `tool_search`, which lets the orchestrator find the tools it may use (every request lists them by name and description), and delegation: the orchestrator hands a brief to one subagent, which runs its own short loop with its own tools and a model chosen by its model tags, and gets back a summary with sources; the chat history is not sent to the model. What does not exist yet: real tools (only the built-in tool_search exists), nested or parallel delegation, and external memory that uses `user_id` (the key exists in `config.toml`, nothing uses it yet).

Goal: several APIs and providers are supported so that the author can pick and choose models that are fast and cheap but can do the job. Models are chosen per task from configuration, using tags and priorities (today the orchestrator model is chosen this way, and the user can pick another model by id or in the UI). Today, if the chosen model fails, the next matching model by priority is tried, and tokens and price are tracked per call so the author can see what a call cost; the orchestrator model is chosen by the tag `orchestrator` and priority; a subagent's model is chosen the same way, from its agent's model tags and the model priorities.

Goal: one orchestrator model reads the user prompt, sends work to subagents and assembles the reply. Today the chat history is not sent to the model; the orchestrator's context tracks the user's current goal, the information collected so far with the source of each item, and the plan to reach the goal. The goal may change; the orchestrator tracks the current goal at all times and, if it has no clue what the goal is, asks the user questions. The orchestrator's context always includes the current date and time, the user's name and the computer name. Delegation to one subagent at a time (one level deep) works; still goals: nested or parallel delegation, and recognising the same user on another computer under a different name so that the orchestrator can reach memories about that user.

The author wants good control over context and wants to see it. Today the UIs show streaming thinking traces (reasoning text from models that provide it). The UIs also let the user inspect, at any moment, the orchestrator's current context (a State tab and a Trace tab): the goal, the collected information with sources, the plan, and what will be sent to the model next. Thinking traces are display-only and are never written back into the orchestrator state.

## Non-goals

- Not a production coding assistant.
- Not a replacement for existing general-purpose coding agents.
- No multi-user support and no authentication.
- Not a service in a container and not a batch runner.
- Sending the full chat history to the model: the orchestrator works from its own state instead.
