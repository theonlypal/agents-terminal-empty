# Binding Condition Gate: boring infrastructure

This branch turns the Binding Condition Framework into a small, inspectable commit-boundary primitive.

## Contract

A binding condition is the prerequisite that must hold for valid continuation.

At a consequential tool boundary:

- satisfied, authentic, unexpired condition -> execution may continue;
- unsatisfied, invalid, expired, or action-mismatched condition -> execution halts before the function tool is invoked.

The integration is deliberately boring. It does not delete an answer after generation and it does not claim that an SDK-side tripwire is model-native EOS. Instead, it defines a stable `ContinuationAuthority` interface so a model-native authority can be substituted behind the same boundary.

## Why this boundary

OpenAI Agents SDK commit `d59fdb8a789a54aff77ce61e503a04797355fc03` documents that tool input guardrails run before `FunctionTool` execution and can halt execution. The adapter binds the condition decision at that public boundary.

## Security properties

The reference condition envelope is canonicalized and HMAC-SHA256 authenticated, action-bound, time-bounded, nonce-carrying, and evidence-digest carrying. The reference authority fails closed on invalid signatures, expiry, action mismatch, or `satisfied=false`.

HMAC is the CI/reference authority, not the final multi-tenant production trust model. Production deployment should use an asymmetric signer or remote attestation service with key rotation, replay protection, durable nonce storage, audit logging, and explicit tenant isolation.

## Model-native authority

`ContinuationAuthority` is intentionally abstract. PCCG-style native continuation control can implement the same interface by returning an attested decision produced at the model continuation boundary. The SDK adapter then enforces that decision before the real-world tool call.

The separation is:

`world/evidence -> verifier -> binding condition -> continuation authority -> commit boundary -> action`

## Reproduction

CI installs the exact pinned OpenAI Agents SDK commit, compiles the package, and runs the matched allow/deny tests.

The repository must never claim a stronger property than the evidence demonstrates. In particular, provider `finish_reason="stop"` is not proof that a closed model emitted a specific native EOS token.
