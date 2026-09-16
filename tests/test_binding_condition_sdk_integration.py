from __future__ import annotations

import asyncio
import time

import pytest
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from agents import Agent, Runner, ToolInputGuardrailTripwireTriggered, function_tool
from agents.testing import ScriptedModel

from binding_condition.core import BindingConditionEnvelope, HMACContinuationAuthority
from binding_condition.openai_agents import BindingConditionRuntime, binding_condition_gate


SECRET = b"integration-secret"


def _condition(*, satisfied: bool) -> BindingConditionEnvelope:
    now = int(time.time())
    return BindingConditionEnvelope(
        subject="integration-user",
        action="protected_action",
        satisfied=satisfied,
        evidence_digest="sha256:integration-evidence",
        issued_at=now - 1,
        expires_at=now + 300,
        nonce=f"integration-{int(satisfied)}",
    ).sign(SECRET)


def _tool_call() -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id="fc_1",
        call_id="call_1",
        type="function_call",
        name="protected_action",
        arguments='{"value":"commit"}',
    )


def _final_message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="msg_1",
        type="message",
        role="assistant",
        content=[ResponseOutputText(text=text, type="output_text", annotations=[], logprobs=[])],
        status="completed",
    )


def _agent(executions: list[str]) -> Agent:
    @function_tool
    def protected_action(value: str) -> str:
        executions.append(value)
        return "executed"

    protected_action.tool_input_guardrails = [binding_condition_gate]

    model = ScriptedModel()
    model.extend([
        [_tool_call()],
        [_final_message("finished")],
    ])

    return Agent(name="binding-condition-integration", model=model, tools=[protected_action])


def test_unsatisfied_condition_prevents_real_function_tool_execution() -> None:
    async def scenario() -> None:
        executions: list[str] = []
        agent = _agent(executions)
        runtime = BindingConditionRuntime(
            condition=_condition(satisfied=False),
            authority=HMACContinuationAuthority(SECRET),
        )

        with pytest.raises(ToolInputGuardrailTripwireTriggered) as exc_info:
            await Runner.run(agent, "execute", context=runtime)

        assert executions == []
        assert exc_info.value.output.output_info["binding_condition"]["allowed"] is False
        assert (
            exc_info.value.output.output_info["binding_condition"]["reason"]
            == "binding_condition_unsatisfied"
        )

    asyncio.run(scenario())


def test_satisfied_condition_preserves_real_function_tool_execution() -> None:
    async def scenario() -> None:
        executions: list[str] = []
        agent = _agent(executions)
        runtime = BindingConditionRuntime(
            condition=_condition(satisfied=True),
            authority=HMACContinuationAuthority(SECRET),
        )

        result = await Runner.run(agent, "execute", context=runtime)

        assert executions == ["commit"]
        assert result.final_output == "finished"

    asyncio.run(scenario())
