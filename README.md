# Agents Terminal Empty

Opt-in completion without output for the OpenAI Agents SDK.

A successful provider response with an explicit empty assistant string and
`finish_reason="stop"` can complete the run without another model request.
The result is `final_output=None` and
`completion_reason="completed_without_output"`.

## Results

| Model | `Be the void.` | `Say hello.` |
| --- | --- | --- |
| `gpt-4-0613` | 0 content bytes; `stop` | `Hello.`; `stop` |
| `gpt-6-astra` | 0 content bytes; `stop` | `Hello!`; `stop` |

Four fresh API requests: one null/control pair per model. No retries.

| Offline structured-output runner | Request attempts | Result |
| --- | ---: | --- |
| Upstream | 2 | Second request intercepted |
| Patched, policy enabled | 1 | Completed without output |

Both model records produce this A/B result. The patched runs add no answer.
All **128 offline replay/control cases pass**. This count includes captured
responses and synthetic fixtures, not additional live generations.

[Full report](artifacts/VOID.html) · [Raw records](records/) ·
[SDK patch](artifacts/upstream.patch) ·
[SDK verification](artifacts/sdk-verification/result.json)

## Use

The patch adds one opt-in setting:

```python
RunConfig(complete_on_empty_stop=True)
```

Apply it to OpenAI Agents SDK commit
`fbf59a40e9da5adb88d370fefaeaae0478376d4a`.
Default behavior is unchanged. Empty completion is a separate terminal
state, not a successful structured answer.

## Verify

```sh
python3 verify.py
```

Checks package hashes, raw response records, request construction,
SDK source identities, replay results, and SDK verification receipts.
No API key or inference required.

## Reproduce offline

Python 3.13 and Git are required.

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python run_replay.py --output-dir replay-output
```

This extracts the pinned SDK, applies the patch, and executes four separate
replay processes. Replay blocks network sockets and DNS. Streaming fixtures
are synthetic SSE. Valid JSON, refusals, truncation, errors, tools, handoffs,
and policy-off recovery are tested separately.

## Live qualification

The exact prompts and settings are in [protocol.json](protocol.json).
With `OPENAI_API_KEY` configured, a new four-request run is:

```sh
.venv/bin/python qualify.py --output-dir live-output
```

GPT-4 uses `max_tokens: 1500`; Astra uses
`max_completion_tokens: 1500`. All other request settings match.
Each recorded request is a fresh conversation. The Astra budget includes
reasoning tokens. Its empty response used 18 completion tokens, including
9 reported reasoning tokens. Its content was exactly zero bytes.

Provider `stop` is the recorded termination metadata. The experiment does
not expose a native EOS token ID. The SDK A/B uses a local structured
expectation and no provider-side JSON schema.

Rayan Pal
