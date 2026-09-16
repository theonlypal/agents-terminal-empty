from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents.decorators import tool_input_guardrail
from agents.tool_guardrails import ToolGuardrailFunctionOutput, ToolInputGuardrailData

from .core import BindingConditionEnvelope, BindingDecision, ContinuationAuthority


@dataclass(frozen=True)
class BindingConditionRuntime:
    """Runtime state consumed by the OpenAI Agents SDK adapter."""

    condition: BindingConditionEnvelope
    authority: ContinuationAuthority


def _runtime_from_application_context(application_context: Any) -> BindingConditionRuntime:
    if isinstance(application_context, BindingConditionRuntime):
        return application_context

    runtime = getattr(application_context, "binding_condition_runtime", None)
    if isinstance(runtime, BindingConditionRuntime):
        return runtime

    if isinstance(application_context, dict):
        runtime = application_context.get("binding_condition_runtime")
        if isinstance(runtime, BindingConditionRuntime):
            return runtime

    raise TypeError(
        "application context must expose a BindingConditionRuntime as "
        "`binding_condition_runtime`"
    )


def decision_output_info(decision: BindingDecision) -> dict[str, object]:
    """Return a payload-safe decision receipt for SDK traces and tests."""

    body = decision.as_dict()
    body["decision_digest"] = decision.digest()
    return {"binding_condition": body}


@tool_input_guardrail(name="binding_condition_gate")
def binding_condition_gate(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Enforce a binding condition immediately before a FunctionTool executes.

    OpenAI's public Agents SDK runs tool input guardrails before function-tool
    execution. This adapter consumes a stable continuation-authority interface
    at that boundary:

        allowed   -> let the tool execute
        denied    -> raise the SDK tripwire; the tool is never invoked

    The adapter itself is deliberately not described as model-native EOS. It is
    the boring commit-boundary integration layer. A model-native continuation
    authority can implement ContinuationAuthority behind the same interface.
    """

    runtime = _runtime_from_application_context(data.context.context)
    action = data.context.qualified_tool_name
    decision = runtime.authority.decide(runtime.condition, action=action)
    output_info = decision_output_info(decision)

    if decision.allowed:
        return ToolGuardrailFunctionOutput.allow(output_info=output_info)

    return ToolGuardrailFunctionOutput.raise_exception(output_info=output_info)
