# Production contract

The Binding Condition Gate is infrastructure, not a demo.

## Stable invariant

A binding condition is the prerequisite that must hold for valid continuation.

For a consequential action `A`:

`authorized(A) = authentic(condition) AND unexpired(condition) AND action_bound(condition, A) AND satisfied(condition)`

If and only if `authorized(A)` is true may execution continue across the commit boundary.

## Failure semantics

The reference OpenAI Agents SDK adapter uses the SDK's documented pre-execution tool-input-guardrail boundary.

- authorized -> `allow`; the function tool may execute.
- unauthorized -> `raise_exception`; the function tool is not invoked.

No generated tool result is rewritten into success. No denied action is executed and hidden afterward.

## Authority separation

The repository exposes a `ContinuationAuthority` protocol. The included HMAC authority is a deterministic reference implementation for CI and integration.

A production model-native gate can implement the same protocol and return an attested decision without changing the SDK adapter. The repository must distinguish these two claims:

1. SDK commit-boundary enforcement: implemented here.
2. Model-native continuation/EOS authority: supplied by a qualifying model-native authority such as a PCCG-derived service.

## Required production hardening

Before multi-tenant deployment:

- replace shared-secret HMAC tenancy with asymmetric signing or remote attestation;
- implement key rotation and revocation;
- persist nonce/replay state durably;
- add tenant/action policy namespaces;
- sign decision receipts;
- export append-only audit records;
- meter latency, availability, false-continue rate, false-stop rate, and bypass attempts;
- run adversarial tests against stale, forged, replayed, cross-action, and cross-tenant conditions;
- preserve a fail-closed mode if the authority is unavailable.

## Qualification bar

A production release must publish matched trials demonstrating:

- unsatisfied/invalid condition -> zero protected tool executions;
- satisfied condition -> protected tool execution preserved;
- action mismatch -> zero protected tool executions;
- forged signature -> zero protected tool executions;
- expired condition -> zero protected tool executions;
- evidence and decision digests reproducible from frozen artifacts.

The primary safety metric is false continuation: an action executing when its binding condition is not validly satisfied.
