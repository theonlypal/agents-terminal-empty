from __future__ import annotations

import time
from types import SimpleNamespace

from binding_condition.core import BindingConditionEnvelope, HMACContinuationAuthority
from binding_condition.openai_agents import BindingConditionRuntime, binding_condition_gate


SECRET = b"test-secret"
NOW = int(time.time())


def envelope(*, satisfied: bool = True, action: str = "send_email") -> BindingConditionEnvelope:
    return BindingConditionEnvelope(
        subject="user-123",
        action=action,
        satisfied=satisfied,
        evidence_digest="sha256:abc123",
        issued_at=NOW - 1,
        expires_at=NOW + 300,
        nonce="n-1",
    ).sign(SECRET)


def test_signed_satisfied_condition_allows() -> None:
    authority = HMACContinuationAuthority(SECRET)
    decision = authority.decide(envelope(), action="send_email")
    assert decision.allowed is True
    assert decision.reason == "binding_condition_satisfied"


def test_unsatisfied_condition_denies() -> None:
    authority = HMACContinuationAuthority(SECRET)
    decision = authority.decide(envelope(satisfied=False), action="send_email")
    assert decision.allowed is False
    assert decision.reason == "binding_condition_unsatisfied"


def test_action_mismatch_denies() -> None:
    authority = HMACContinuationAuthority(SECRET)
    decision = authority.decide(envelope(action="send_email"), action="wire_money")
    assert decision.allowed is False
    assert decision.reason == "action_mismatch"


def test_tampered_condition_denies() -> None:
    authority = HMACContinuationAuthority(SECRET)
    signed = envelope()
    tampered = BindingConditionEnvelope(
        subject=signed.subject,
        action=signed.action,
        satisfied=False,
        evidence_digest=signed.evidence_digest,
        issued_at=signed.issued_at,
        expires_at=signed.expires_at,
        nonce=signed.nonce,
        version=signed.version,
        signature=signed.signature,
    )
    decision = authority.decide(tampered, action="send_email")
    assert decision.allowed is False
    assert decision.reason.startswith("invalid_condition:")


def test_openai_tool_guardrail_allows_licensed_call() -> None:
    runtime = BindingConditionRuntime(
        condition=envelope(),
        authority=HMACContinuationAuthority(SECRET),
    )
    fake_data = SimpleNamespace(
        context=SimpleNamespace(
            context=runtime,
            qualified_tool_name="send_email",
        )
    )
    result = binding_condition_gate.guardrail_function(fake_data)
    assert result.behavior["type"] == "allow"
    assert result.output_info["binding_condition"]["allowed"] is True


def test_openai_tool_guardrail_halts_unlicensed_call() -> None:
    runtime = BindingConditionRuntime(
        condition=envelope(satisfied=False),
        authority=HMACContinuationAuthority(SECRET),
    )
    fake_data = SimpleNamespace(
        context=SimpleNamespace(
            context=runtime,
            qualified_tool_name="send_email",
        )
    )
    result = binding_condition_gate.guardrail_function(fake_data)
    assert result.behavior["type"] == "raise_exception"
    assert result.output_info["binding_condition"]["allowed"] is False
