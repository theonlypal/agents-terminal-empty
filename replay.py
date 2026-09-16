"""Offline, transport-level A/B replay; never imports or invokes the live qualifier.

Run once per checkout in separate processes with PYTHONPATH=<checkout>/src.
The local AgentOutputSchema is identical in both arms. The experimental adapter
omits only its provider-side response_format so the captured unconstrained
completion is not misrepresented as a structured-output model response.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import copy
from dataclasses import dataclass
from functools import partial
import hashlib
import json
from pathlib import Path
import platform
import socket
import subprocess
from typing import Any
from unittest.mock import patch

import httpx2
from openai import AsyncOpenAI
from pydantic import BaseModel

import agents
from agents import Agent, ModelSettings, RunConfig, Runner
from agents.agent_output import AgentOutputSchema
from agents.decorators import tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.tracing import TracingProcessor, set_trace_processors, set_tracing_disabled


ROOT = Path(__file__).resolve().parent
PIN = 'fbf59a40e9da5adb88d370fefaeaae0478376d4a'
SCHEMA = 'agents-terminal-empty-replay/v1'


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json')
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    raise TypeError(f'Unexpected receipt value: {type(value).__name__}')


class Answer(BaseModel):
    answer: str


class ReplayRequestLimit(RuntimeError):
    """Raised by the mock transport after the fixed response schedule is spent."""


class LocalSchemaChatCompletionsModel(OpenAIChatCompletionsModel):
    async def _fetch_response(
        self, system_instructions, input, model_settings, tools, output_schema,
        handoffs, span, tracing, stream=False, prompt=None,
    ):
        # Shared experimental adapter, not a public provider capability claim.
        return await super()._fetch_response(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=None,
            handoffs=handoffs,
            span=span,
            tracing=tracing,
            stream=stream,
            prompt=prompt,
        )


class MemorySession:
    """Local Session protocol implementation; no database, key, or network I/O."""

    session_id = 'offline-replay-session'
    session_settings = None

    def __init__(self):
        self.items: list[Any] = []

    async def get_items(self, limit=None):
        if limit is not None and limit <= 0:
            return []
        return copy.deepcopy(self.items if limit is None else self.items[-limit:])

    async def add_items(self, items):
        self.items.extend(copy.deepcopy(items))

    async def pop_item(self):
        return self.items.pop() if self.items else None

    async def clear_session(self):
        self.items.clear()


class CaptureProcessor(TracingProcessor):
    def __init__(self):
        self.traces: list[dict[str, Any]] = []
        self.spans: list[dict[str, Any]] = []

    def on_trace_start(self, trace):
        pass

    def on_trace_end(self, trace):
        value = trace.export()
        if value is not None:
            self.traces.append(value)

    def on_span_start(self, span):
        pass

    def on_span_end(self, span):
        value = span.export()
        if value is not None:
            self.spans.append(value)

    def shutdown(self):
        pass

    def force_flush(self):
        pass


@dataclass(frozen=True)
class Fixture:
    body: bytes
    kind: str
    status: int = 200
    source_path: str | None = None


def make_synthetic_completion(
    content: str | None = None,
    *,
    model: str,
    finish_reason: str = 'stop',
    refusal: str | None = None,
    tool_name: str | None = None,
) -> Fixture:
    message: dict[str, Any] = {'role': 'assistant', 'content': content, 'refusal': refusal}
    if tool_name:
        message['tool_calls'] = [{
            'id': f'call_{tool_name}', 'type': 'function',
            'function': {'name': tool_name, 'arguments': '{}'},
        }]
    return Fixture(encoded({
        'id': 'synthetic-chat-completion', 'object': 'chat.completion', 'created': 0,
        'model': model,
        'choices': [{'index': 0, 'message': message, 'finish_reason': finish_reason}],
        'usage': {'prompt_tokens': 5, 'completion_tokens': int(bool(content)),
                  'total_tokens': 5 + int(bool(content))},
    }), 'synthetic compatibility fixture; not live inference')


def synthetic_sse(fixture: Fixture) -> bytes:
    """Deterministic SSE representation, explicitly NOT captured live streaming."""
    envelope = json.loads(fixture.body)
    choice = envelope['choices'][0]
    message = choice['message']
    delta = {key: copy.deepcopy(value) for key, value in message.items()
             if key in ('role', 'content', 'refusal', 'tool_calls') and value is not None}
    for index, item in enumerate(delta.get('tool_calls', [])):
        item['index'] = index
    base = {key: envelope[key] for key in ('id', 'created', 'model')}
    base['object'] = 'chat.completion.chunk'
    chunks = [
        {**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
        {**base, 'choices': [{'index': 0, 'delta': {},
                              'finish_reason': choice['finish_reason']}]},
        {**base, 'choices': [], 'usage': envelope.get('usage')},
    ]
    return b''.join(b'data: ' + encoded(chunk) + b'\n\n' for chunk in chunks) + b'data: [DONE]\n\n'


class ReplayTransport:
    def __init__(self, fixtures: list[Fixture], streamed: bool):
        self.fixtures = fixtures
        self.streamed = streamed
        self.requests: list[dict[str, Any]] = []
        self.served: list[Fixture] = []
        self.limit_count = 0

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        record: dict[str, Any] = {
            'number': len(self.requests) + 1,
            'method': request.method, 'url': str(request.url),
            'body': body, 'body_sha256': digest(request.content),
        }
        self.requests.append(record)
        if len(self.served) >= len(self.fixtures):
            self.limit_count += 1
            record['blocked'] = 'ReplayRequestLimit'
            raise ReplayRequestLimit('Fixed offline replay schedule exhausted; no network fallback.')
        fixture = self.fixtures[len(self.served)]
        self.served.append(fixture)
        wire = synthetic_sse(fixture) if self.streamed and fixture.status == 200 else fixture.body
        record.update({
            'status': fixture.status, 'source_fixture_sha256': digest(fixture.body),
            'response_sha256': digest(wire), 'response_bytes': len(wire),
            'source_path': fixture.source_path,
            'fixture_kind': 'synthetic SSE; not live streaming' if self.streamed else fixture.kind,
            'exact_source_bytes': wire == fixture.body,
        })
        return httpx2.Response(
            fixture.status, content=wire,
            headers={'content-type': 'text/event-stream' if self.streamed and fixture.status == 200
                     else 'application/json', 'x-request-id': f'offline-request-{len(self.served)}'},
            request=request,
        )


def exception_record(error: Exception | None) -> dict[str, Any] | None:
    if error is None:
        return None
    chain = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append({'type': type(current).__name__, 'message': str(current)})
        current = current.__cause__ or current.__context__
    return {**chain[0], 'chain': chain}


def message_texts(raw_item: Any) -> list[str]:
    item = jsonable(raw_item)
    if not isinstance(item, dict) or item.get('role') != 'assistant':
        return []
    content = item.get('content')
    if isinstance(content, str):
        return [content]
    return [part['text'] for part in content or []
            if isinstance(part, dict) and part.get('type') == 'output_text']


async def run_case(
    name: str, streamed: bool, variant: str, captured: dict[str, Fixture],
    request_sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    request_source = request_sources['control' if name == 'captured_control_plain' else 'null']
    synthetic_completion = partial(make_synthetic_completion, model=request_source['model'])
    structured = name not in ('captured_control_plain', 'plain_text', 'plain_empty_default')
    requested = name not in ('policy_off_recovery', 'plain_empty_default')
    enabled = variant == 'patched' and requested
    fixtures = [captured['null']]
    expected_error: str | None = None
    expected_output: Any = None
    expected_calls = 1
    tool_counter = 0
    handoff_counter = 0
    terminal_case = name in ('captured_null_structured', 'tool_then_null', 'handoff_then_null')
    if name == 'captured_control_plain':
        fixtures = [captured['control']]
        expected_output = json.loads(fixtures[0].body)['choices'][0]['message']['content']
    elif name == 'valid_structured':
        fixtures = [synthetic_completion('{"answer":"synthetic validated JSON"}')]
        expected_output = {'answer': 'synthetic validated JSON'}
    elif name in ('nonempty_invalid', 'whitespace', 'invisible'):
        text = {'nonempty_invalid': 'not JSON', 'whitespace': ' ', 'invisible': '\u200b'}[name]
        fixtures = [synthetic_completion(text)]
        expected_error = 'ModelBehaviorError'
    elif name == 'unknown_empty':
        fixtures = [synthetic_completion(None)]
        expected_calls = 2
        expected_error = 'ReplayRequestLimit'
    elif name == 'refusal':
        fixtures = [synthetic_completion(refusal='Synthetic provider refusal.')]
        expected_error = 'ModelRefusalError'
    elif name == 'content_filter':
        fixtures = [synthetic_completion(finish_reason='content_filter')]
        expected_error = 'ModelRefusalError'
    elif name == 'length':
        fixtures = [synthetic_completion('', finish_reason='length')]
        expected_error = 'ModelBehaviorError'
    elif name == 'http_error':
        fixtures = [Fixture(encoded({'error': {'message': 'Synthetic HTTP error.',
                                              'type': 'invalid_request_error', 'code': 'bad_request'}}),
                            'synthetic HTTP error; not live inference', status=400)]
        expected_error = 'BadRequestError'
    elif name == 'policy_off_recovery':
        fixtures = [synthetic_completion(''), synthetic_completion('{"answer":"synthetic recovery"}')]
        expected_calls = 2
        expected_output = {'answer': 'synthetic recovery'}
    elif name == 'plain_text':
        fixtures = [synthetic_completion('Synthetic plain answer.')]
        expected_output = 'Synthetic plain answer.'
    elif name == 'plain_empty_default':
        fixtures = [synthetic_completion('')]
        expected_output = ''
    elif name not in ('captured_null_structured', 'tool_then_null', 'handoff_then_null'):
        raise ValueError(name)

    if terminal_case:
        expected_calls = 1 if name == 'captured_null_structured' else 2
        if not enabled:
            expected_calls += 1
            expected_error = 'ReplayRequestLimit'

    processor = CaptureProcessor()
    set_trace_processors([processor])  # Replace remote exporters before any run starts.
    set_tracing_disabled(False)
    session = MemorySession()
    schema = AgentOutputSchema(Answer)

    @tool
    def counted_tool() -> str:
        """A deterministic synthetic local side effect used only by the replay."""
        nonlocal tool_counter
        tool_counter += 1
        return 'synthetic tool result'

    async def on_handoff(context):
        nonlocal handoff_counter
        handoff_counter += 1

    if name == 'tool_then_null':
        fixtures = [synthetic_completion(tool_name='counted_tool', finish_reason='tool_calls'), captured['null']]
    if name == 'handoff_then_null':
        fixtures = [synthetic_completion(tool_name='transfer_to_delegate', finish_reason='tool_calls'), captured['null']]

    transport = ReplayTransport(fixtures, streamed)
    client = AsyncOpenAI(
        api_key='offline-placeholder-not-a-credential', base_url='https://replay.invalid/v1',
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport), trust_env=False),
    )
    model = LocalSchemaChatCompletionsModel(model=request_source['model'], openai_client=client)
    budget_fields = set(request_source) & {'max_tokens', 'max_completion_tokens'}
    if len(budget_fields) != 1:
        raise ValueError('Each captured request must contain exactly one token-budget field.')
    budget_field = budget_fields.pop()
    settings = (ModelSettings(max_tokens=request_source[budget_field]) if budget_field == 'max_tokens'
                else ModelSettings(extra_args={budget_field: request_source[budget_field]}))
    agent = Agent(
        name='replay', model=model, instructions=request_source['messages'][0]['content'],
        output_type=schema if structured else None, model_settings=settings,
        tools=[counted_tool] if name == 'tool_then_null' else [],
    )
    if name == 'handoff_then_null':
        from agents import handoff
        delegate = Agent(name='delegate', model=model, output_type=schema, model_settings=settings,
                         instructions=request_source['messages'][0]['content'])
        agent.handoffs = [handoff(delegate, on_handoff=on_handoff)]

    config_kwargs: dict[str, Any] = {'workflow_name': f'offline-{name}-{streamed}'}
    if variant == 'patched':
        config_kwargs['complete_on_empty_stop'] = enabled
    config = RunConfig(**config_kwargs)
    result: Any = None
    error: Exception | None = None
    events: list[dict[str, Any]] = []
    try:
        kwargs = {'input': request_source['messages'][1]['content'], 'run_config': config,
                  'session': session, 'max_turns': 5}
        if streamed:
            result = Runner.run_streamed(agent, **kwargs)
            async for event in result.stream_events():
                entry = {'type': event.type, 'name': getattr(event, 'name', None)}
                if hasattr(event, 'data'):
                    entry['raw_event'] = jsonable(event.data)
                events.append(entry)
        else:
            result = await Runner.run(agent, **kwargs)
    except Exception as caught:
        error = caught
    finally:
        await client.close()
        processor.force_flush()

    item_owner = result if result is not None else getattr(error, 'run_data', None)
    items_available = item_owner is not None and hasattr(item_owner, 'new_items')
    items = list(getattr(item_owner, 'new_items', []) or [])
    raw_items = [{'type': item.type, 'raw_item': jsonable(item.raw_item)} for item in items]
    actual_texts = [text for item in raw_items for text in message_texts(item['raw_item'])]
    provider_texts = []
    for fixture in transport.served:
        envelope = json.loads(fixture.body)
        for choice in envelope.get('choices', []):
            content = choice.get('message', {}).get('content')
            if isinstance(content, str):
                provider_texts.append(content)
    synthesized = list((Counter(actual_texts) - Counter(provider_texts)).elements())
    session_texts = [text for item in session.items for text in message_texts(item)]
    session_synthesized = list((Counter(session_texts) - Counter(provider_texts)).elements())
    final_output = jsonable(getattr(result, 'final_output', None))
    completion_reason = getattr(result, 'completion_reason', None)
    captured_error = exception_record(error)
    error_types = [entry['type'] for entry in (captured_error or {}).get('chain', [])]
    spans = jsonable(processor.spans)
    terminal_spans = [span for span in spans
                      if span.get('span_data', {}).get('name') == 'completed_without_output']
    terminal_expected = terminal_case and enabled
    first_body = transport.requests[0]['body'] if transport.requests else {}
    checks = [
        {'name': 'expected_request_count', 'passed': len(transport.requests) == expected_calls},
        {'name': 'expected_error', 'passed': (error is None if expected_error is None
                                            else expected_error in error_types)},
        {'name': 'expected_final_output', 'passed': final_output == expected_output},
        {'name': 'no_observed_synthesized_assistant_answer',
         'passed': not synthesized and not session_synthesized},
        {'name': 'no_provider_structured_output_instruction',
         'passed': all('response_format' not in row['body'] for row in transport.requests)},
        {'name': 'original_first_prompt_preserved', 'passed': first_body.get('messages') == request_source['messages']},
        {'name': 'tool_executed_exactly_once_when_selected', 'passed': tool_counter == int(name == 'tool_then_null')},
        {'name': 'handoff_executed_exactly_once_when_selected', 'passed': handoff_counter == int(name == 'handoff_then_null')},
        {'name': 'terminal_reason', 'passed': (completion_reason == 'completed_without_output') == terminal_expected},
        {'name': 'terminal_trace', 'passed': len(terminal_spans) == int(terminal_expected)},
        {'name': 'fixed_transport_cap', 'passed': transport.limit_count == int(expected_error == 'ReplayRequestLimit')},
    ]
    if name == 'captured_null_structured' and not streamed:
        checks.append({'name': 'exact_captured_null_response_bytes',
                       'passed': transport.requests[0].get('response_sha256') == digest(captured['null'].body)})
    if terminal_expected:
        checks.append({'name': 'no_assistant_message_added_for_empty_tail',
                       'passed': items_available and actual_texts == [] and session_texts == []})
    if name in ('tool_then_null', 'handoff_then_null'):
        call_name = 'counted_tool' if name == 'tool_then_null' else 'transfer_to_delegate'
        call_id = f'call_{call_name}'
        checks.append({'name': 'one_persisted_call_output_pair', 'passed':
                       sum(item.get('type') == 'function_call' and item.get('call_id') == call_id
                           for item in session.items) == 1
                       and sum(item.get('type') == 'function_call_output' and item.get('call_id') == call_id
                               for item in session.items) == 1})
    model_responses = getattr(item_owner, 'raw_responses', None)
    raw_responses = None if model_responses is None else [{
        'output': jsonable(response.output), 'response_id': response.response_id,
        'request_id': response.request_id,
        'empty_output_stop_reason': getattr(response, 'empty_output_stop_reason', None),
        'usage': {'requests': response.usage.requests, 'input_tokens': response.usage.input_tokens,
                  'output_tokens': response.usage.output_tokens, 'total_tokens': response.usage.total_tokens},
    } for response in model_responses]
    return {
        'case': name, 'mode': 'streaming' if streamed else 'nonstreaming',
        'fixture_kind': 'synthetic SSE; not live streaming' if streamed else fixtures[0].kind,
        'source_fixture_sha256': digest(fixtures[0].body),
        'source_fixture_path': fixtures[0].source_path,
        'policy_requested': requested, 'policy_enabled': enabled,
        'local_output_schema': schema.json_schema() if structured else None,
        'request_count': len(transport.requests), 'replayed_response_count': len(transport.served),
        'network_requests': 0, 'requests': transport.requests,
        'final_output': final_output, 'completion_reason': completion_reason,
        'exception': captured_error, 'tool_counter': tool_counter, 'handoff_counter': handoff_counter,
        'new_items': raw_items if items_available else None, 'new_items_available': items_available,
        'new_items_unavailable_reason': None if items_available else 'Nonstreaming run raised before returning a result; exception exposes no new_items.',
        'session_items': jsonable(session.items), 'raw_responses': raw_responses,
        'provider_answer_texts': provider_texts, 'actual_assistant_texts': actual_texts,
        'synthesized_answer_count': len(synthesized) if items_available else None,
        'synthesized_answer_texts': synthesized if items_available else None,
        'session_synthesized_answer_count': len(session_synthesized),
        'events': events, 'traces': jsonable(processor.traces), 'spans': spans,
        'checks': checks, 'passed': all(check['passed'] for check in checks),
    }


async def main(args) -> int:
    checkout = args.sdk_source.resolve()
    imported = Path(agents.__file__).resolve()
    if imported != checkout / 'src/agents/__init__.py':
        raise RuntimeError(f'Wrong SDK import: {imported}; expected {checkout}/src/agents/__init__.py')
    is_checkout = (checkout / '.git').exists()
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip() if is_checkout else args.expected_commit
    if commit != args.expected_commit:
        raise RuntimeError(f'Unexpected SDK pin: {commit}')
    diff = subprocess.check_output(['git', 'diff', '--binary', 'HEAD'], cwd=checkout) if is_checkout else b''
    status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=checkout, text=True) if is_checkout else 'source archive; verified by packet verifier'
    if args.variant == 'upstream' and is_checkout and status.strip():
        raise RuntimeError('The upstream checkout must remain unmodified.')
    source_files = {str(path.relative_to(checkout)): digest(path.read_bytes())
                    for path in sorted((checkout / 'src').rglob('*.py'))}
    captured = {}
    request_sources = {}
    records_dir = args.records_dir.resolve()
    qualification_path = records_dir / 'qualification.json'
    qualification_bytes = qualification_path.read_bytes()
    qualification = json.loads(qualification_bytes)
    qualified_arms = {row['arm']: row for row in qualification['results']}
    if qualification.get('qualified') is not True or set(qualified_arms) != {'null', 'control'}:
        raise RuntimeError('Both existing live qualification records are required; never retry live here.')
    for name in ('null', 'control'):
        path = records_dir / f'{name}.response.raw.json'
        captured[name] = Fixture(path.read_bytes(), 'captured live nonstreaming response; offline replay',
                                 source_path=str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path))
        if digest(captured[name].body) != qualified_arms[name]['raw_response_sha256']:
            raise RuntimeError(f'{name} response bytes differ from the existing qualification receipt.')
        request_bytes = (records_dir / f'{name}.request.json').read_bytes()
        if digest(request_bytes) != qualified_arms[name]['request_sha256']:
            raise RuntimeError(f'{name} request bytes differ from the existing qualification receipt.')
        request_sources[name] = json.loads(request_bytes)
    null = json.loads(captured['null'].body)
    choice = null['choices'][0]
    if len(null['choices']) != 1 or choice['finish_reason'] != 'stop' or choice['message']['content'] != '':
        raise RuntimeError('The captured null fixture no longer matches the qualified empty-stop envelope.')
    cases = ['captured_null_structured', 'captured_control_plain', 'valid_structured', 'refusal',
             'content_filter', 'length', 'http_error', 'tool_then_null', 'handoff_then_null',
             'policy_off_recovery', 'plain_text', 'plain_empty_default',
             'nonempty_invalid', 'whitespace', 'invisible', 'unknown_empty']
    rows = []
    def reject_socket(*args, **kwargs):
        raise RuntimeError('Network sockets and DNS are forbidden in this offline replay.')
    with patch.object(socket.socket, 'connect', reject_socket), \
         patch.object(socket.socket, 'connect_ex', reject_socket), \
         patch.object(socket, 'getaddrinfo', reject_socket):
        for name in cases:
            for streamed in (False, True):
                row = await run_case(name, streamed, args.variant, captured, request_sources)
                rows.append(row)
                failed = [check['name'] for check in row['checks'] if not check['passed']]
                print(json.dumps({'case': name, 'streamed': streamed, 'passed': row['passed'],
                                  'failed_checks': failed}), flush=True)
    report = {
        'schema': SCHEMA, 'variant': args.variant, 'sdk_source_path': str(checkout),
        'records_dir': str(records_dir), 'requested_model': request_sources['null']['model'],
        'sdk_commit': commit, 'sdk_diff_sha256': digest(diff), 'sdk_status': status,
        'sdk_source_files': source_files, 'sdk_source_identity_sha256': digest(encoded(source_files)),
        'replay_script_sha256': digest(Path(__file__).read_bytes()), 'python': platform.python_version(),
        'qualification_receipt_sha256': digest(qualification_bytes),
        'no_live_api_calls': True, 'network_blocked': True,
        'adapter_contract': 'Same experimental subclass in both arms; provider output_schema=None; local AgentOutputSchema retained.',
        'fixtures': {name: {'path': fixture.source_path, 'sha256': digest(fixture.body),
                            'bytes': len(fixture.body)} for name, fixture in captured.items()},
        'cases': rows, 'passed': sum(row['passed'] for row in rows), 'total': len(rows),
        'status': 'PASS' if all(row['passed'] for row in rows) else 'FAIL',
    }
    if args.compare is not None:
        peer = json.loads(args.compare.read_bytes())
        peer_cases = {(row['case'], row['mode']): row for row in peer['cases']}
        comparison_checks = [
            {'name': 'same_harness_bytes', 'passed': peer['replay_script_sha256'] == report['replay_script_sha256']},
            {'name': 'same_source_fixture_bytes', 'passed': peer['fixtures'] == report['fixtures']},
            {'name': 'same_case_set', 'passed': set(peer_cases) == {(row['case'], row['mode']) for row in rows}},
        ]
        for row in rows:
            other = peer_cases.get((row['case'], row['mode']))
            request_pairs = list(zip(row['requests'], other['requests'])) if other else []
            comparison_checks.append({
                'name': f'{row["case"]}/{row["mode"]}:identical_common_requests_and_responses',
                'passed': bool(request_pairs) and all(
                    left['body_sha256'] == right['body_sha256']
                    and (left.get('response_sha256') == right.get('response_sha256')
                         if 'response_sha256' in left and 'response_sha256' in right else True)
                    for left, right in request_pairs),
            })
        report['comparison'] = {'peer_path': str(args.compare), 'peer_sha256': digest(args.compare.read_bytes()),
                                'checks': comparison_checks,
                                'status': 'PASS' if all(check['passed'] for check in comparison_checks) else 'FAIL'}
        if report['comparison']['status'] != 'PASS':
            report['status'] = 'FAIL'
    destination = args.output or records_dir / f'replay-{args.variant}.json'
    with destination.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'{report["status"]}: {report["passed"]}/{report["total"]}; {destination}')
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=('upstream', 'patched'), required=True)
    parser.add_argument('--sdk-source', type=Path, required=True)
    parser.add_argument('--records-dir', type=Path, required=True)
    parser.add_argument('--expected-commit', default=PIN)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--compare', type=Path, help='Compare A/B request wire hashes against the other process receipt.')
    raise SystemExit(asyncio.run(main(parser.parse_args())))

