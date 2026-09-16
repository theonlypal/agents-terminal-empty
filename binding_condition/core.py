from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, replace
from typing import Protocol


class BindingConditionError(ValueError):
    """Raised when a binding-condition envelope cannot be trusted."""


@dataclass(frozen=True)
class BindingConditionEnvelope:
    """Signed prerequisite state presented at a continuation boundary.

    The envelope deliberately contains no prompt or model output. It carries only
    the minimum state needed to decide whether a named action is licensed.
    """

    subject: str
    action: str
    satisfied: bool
    evidence_digest: str
    issued_at: int
    expires_at: int
    nonce: str
    version: str = "bci-1"
    signature: str = ""

    def payload(self) -> dict[str, object]:
        return {
            "action": self.action,
            "evidence_digest": self.evidence_digest,
            "expires_at": self.expires_at,
            "issued_at": self.issued_at,
            "nonce": self.nonce,
            "satisfied": self.satisfied,
            "subject": self.subject,
            "version": self.version,
        }

    def canonical_payload(self) -> bytes:
        return json.dumps(
            self.payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def payload_digest(self) -> str:
        return hashlib.sha256(self.canonical_payload()).hexdigest()

    def sign(self, secret: bytes) -> "BindingConditionEnvelope":
        if not secret:
            raise BindingConditionError("signing secret must not be empty")
        signature = hmac.new(secret, self.canonical_payload(), hashlib.sha256).hexdigest()
        return replace(self, signature=signature)

    def verify(self, secret: bytes, *, now: int | None = None) -> None:
        if not secret:
            raise BindingConditionError("verification secret must not be empty")
        if not self.signature:
            raise BindingConditionError("missing signature")
        if self.expires_at <= self.issued_at:
            raise BindingConditionError("expires_at must be greater than issued_at")

        expected = hmac.new(secret, self.canonical_payload(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise BindingConditionError("invalid signature")

        current = int(time.time()) if now is None else int(now)
        if current < self.issued_at:
            raise BindingConditionError("condition is not active yet")
        if current >= self.expires_at:
            raise BindingConditionError("condition has expired")


@dataclass(frozen=True)
class BindingDecision:
    allowed: bool
    reason: str
    action: str
    condition_digest: str
    authority: str

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "allowed": self.allowed,
            "authority": self.authority,
            "condition_digest": self.condition_digest,
            "reason": self.reason,
        }

    def digest(self) -> str:
        encoded = json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ContinuationAuthority(Protocol):
    """Stable interface for any continuation authority.

    The authority can be deterministic, remote, or model-native. The OpenAI SDK
    adapter does not care how the decision is produced; it only consumes the
    decision at the documented pre-execution boundary.
    """

    def decide(self, condition: BindingConditionEnvelope, *, action: str) -> BindingDecision:
        ...


@dataclass(frozen=True)
class HMACContinuationAuthority:
    """Deterministic reference authority for integration and CI.

    This is intentionally boring infrastructure. It is not the model-native
    authority claim. A PCCG-style native gate can implement the same
    ContinuationAuthority protocol without changing callers.
    """

    secret: bytes
    name: str = "hmac-reference"

    def decide(self, condition: BindingConditionEnvelope, *, action: str) -> BindingDecision:
        digest = condition.payload_digest()

        if condition.action != action:
            return BindingDecision(
                allowed=False,
                reason="action_mismatch",
                action=action,
                condition_digest=digest,
                authority=self.name,
            )

        try:
            condition.verify(self.secret)
        except BindingConditionError as exc:
            return BindingDecision(
                allowed=False,
                reason=f"invalid_condition:{exc}",
                action=action,
                condition_digest=digest,
                authority=self.name,
            )

        if not condition.satisfied:
            return BindingDecision(
                allowed=False,
                reason="binding_condition_unsatisfied",
                action=action,
                condition_digest=digest,
                authority=self.name,
            )

        return BindingDecision(
            allowed=True,
            reason="binding_condition_satisfied",
            action=action,
            condition_digest=digest,
            authority=self.name,
        )
