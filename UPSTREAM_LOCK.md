# OpenAI upstream lock

Canonical upstream repository:

- https://github.com/openai/openai-agents-python
- License: MIT

Upstream `main` observed and pinned on 2026-09-16:

- commit: `d59fdb8a789a54aff77ce61e503a04797355fc03`
- tree: `3ebc087712a2be629c846dabfa98ba4a8c6bf7cc`
- commit URL: https://github.com/openai/openai-agents-python/commit/d59fdb8a789a54aff77ce61e503a04797355fc03

Existing `agents-terminal-empty` artifact baseline:

- this repository commit: `1dd698c4d9ffc58443c6b442d34d08fdda188d99`
- existing OpenAI Agents SDK patch base: `fbf59a40e9da5adb88d370fefaeaae0478376d4a`

## Binding Condition port

The next public object is a pinned mirror/port against the upstream commit above. The invariant is:

> A binding condition is the prerequisite that must hold for valid continuation.

At the documented pre-execution/terminal boundary:

- condition satisfied -> continuation is permitted;
- condition unsatisfied -> terminal non-continuation;
- no fabricated answer;
- no post-hoc deletion of an already-produced answer;
- matched licensed/unlicensed controls must be preserved;
- raw termination metadata, test commands, patch identity, and hashes must be reproducible.

Tracking issue: https://github.com/theonlypal/agents-terminal-empty/issues/1

This file pins provenance only. It does not claim that the current repository has already mirrored or patched upstream commit `d59fdb8a789a54aff77ce61e503a04797355fc03`.
