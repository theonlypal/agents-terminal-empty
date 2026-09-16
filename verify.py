#!/usr/bin/env python3
"""Read-only verification of saved records. Standard library; no SDK or network calls."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile

ROOT = Path(__file__).resolve().parent
PIN = 'fbf59a40e9da5adb88d370fefaeaae0478376d4a'
SYSTEM = 'You are the concept the user names. Embody it completely. Output only what the concept itself would say or express. If the concept is silent by nature, be silent.'
MODELS = {'gpt-4-0613': 'max_tokens', 'gpt-6-astra': 'max_completion_tokens'}
ARMS = {'null': 'Be the void.', 'control': 'Say hello.'}
VARIANTS = ('upstream', 'patched')
MODES = ('nonstreaming', 'streaming')
CASES = ('captured_null_structured', 'captured_control_plain', 'valid_structured',
         'refusal', 'content_filter', 'length', 'http_error', 'tool_then_null',
         'handoff_then_null', 'policy_off_recovery', 'plain_text', 'plain_empty_default',
         'nonempty_invalid', 'whitespace', 'invisible', 'unknown_empty')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, f'Duplicate JSON key: {key}')
        value[key] = item
    return value


def decode(raw):
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda value: require(False, f'Invalid JSON number: {value}'))


def load(root, name):
    return decode((root / name).read_bytes())


def safe_relative(name):
    path = PurePosixPath(name)
    require(name and not path.is_absolute() and '..' not in path.parts and '\\' not in name,
            f'Unsafe relative path: {name}')
    return path.as_posix()


def timestamp(value):
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, 'Timestamp has no timezone.')
    return result


def expected_request(model, arm):
    return {'model': model, 'messages': [
        {'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': ARMS[arm]}],
        MODELS[model]: 1500, 'stream': False}


def check_budget(body, model):
    field = MODELS[model]
    require(body.get('model') == model and set(body) & {'max_tokens', 'max_completion_tokens'} == {field}
            and type(body[field]) is int and body[field] == 1500,
            f'{model}: wrong token budget or model.')


def check_usage(usage):
    require(isinstance(usage, dict), 'Missing provider usage.')
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        require(type(usage.get(key)) is int and usage[key] >= 0, f'Invalid usage: {key}')
    require(usage['total_tokens'] == usage['prompt_tokens'] + usage['completion_tokens'],
            'Provider usage total mismatch.')
    for key, value in usage.items():
        if isinstance(value, dict):
            require(all(type(count) is int and count >= 0 for count in value.values()),
                    f'Invalid detailed usage: {key}')


def verify_live(root):
    protocol = load(root, 'protocol.json')
    require(protocol['schema'] == 'agents-terminal-empty-protocol/v1'
            and protocol['endpoint'] == 'https://api.openai.com/v1/chat/completions'
            and protocol['system'] == SYSTEM and protocol['stream'] is False
            and protocol['maximum_live_requests'] == 4 and protocol['transport_retries'] == 0
            and protocol['timeout_seconds'] == 180
            and protocol['sdk_commit'] == PIN, 'Protocol differs from the frozen experiment.')
    require(protocol['models'] == [{'id': model, 'budget_field': field, 'budget': 1500}
                                   for model, field in MODELS.items()]
            and protocol['arms'] == [{'name': name, 'input': value} for name, value in ARMS.items()],
            'Protocol models, budgets, or arms differ.')
    plan = load(root, 'records/live-plan.json')
    results = load(root, 'records/live-results.json')
    for receipt in (plan, results):
        require(receipt['stage'] == 'LIVE INFERENCE'
                and receipt['protocol_sha256'] == sha((root / 'protocol.json').read_bytes())
                and receipt['qualifier_sha256'] == sha((root / 'qualify.py').read_bytes()),
                'Live protocol or qualifier binding mismatch.')
    require(plan['maximum_live_requests'] == results['requests_attempted'] == 4
            and plan['transport_retries'] == 0 and results['qualified'] is True,
            'Exactly four successful scheduled attempts are required.')
    expected = [(model, arm) for model in MODELS for arm in ARMS]
    require([(row['model'], row['arm']) for row in plan['requests']] == expected,
            'Live plan has missing, duplicate, or reordered requests.')
    require(set(results['models']) == set(MODELS), 'Live results model set differs.')
    planned = {(row['model'], row['arm']): row for row in plan['requests']}
    previous = timestamp(plan['started_at'])
    qualifications = {}
    ids = set()
    for model in MODELS:
        qualification = load(root, f'records/{model}/qualification.json')
        require(qualification['qualified'] is True and qualification['requests_attempted'] == 2
                and qualification['stage'] == 'LIVE INFERENCE', f'{model}: unqualified pair.')
        rows = qualification['results']
        require(rows == results['models'][model] and [row['arm'] for row in rows] == list(ARMS),
                f'{model}: qualification and aggregate records differ.')
        for row in rows:
            arm = row['arm']
            label = f'{model}/{arm}'
            raw_request = (root / f'records/{model}/{arm}.request.json').read_bytes()
            request = decode(raw_request)
            check_budget(request, model)
            require(request == expected_request(model, arm) == planned[model, arm]['request'],
                    f'{label}: request differs from the exact protocol/plan.')
            require(sha(raw_request) == row['request_sha256'] == planned[model, arm]['request_sha256']
                    == sha(encoded(planned[model, arm]['request'])), f'{label}: request hash mismatch.')
            current = timestamp(row['started_at'])
            require(current >= previous, f'{label}: record predates plan or prior request.')
            previous = current
            raw_response = (root / f'records/{model}/{arm}.response.raw.json').read_bytes()
            require(sha(raw_response) == row['raw_response_sha256'], f'{label}: response hash mismatch.')
            response = decode(raw_response)
            headers = load(root, f'records/{model}/{arm}.response.headers.json')
            require(row['stage'] == 'LIVE INFERENCE' and row['requested_model'] == model
                    and row['passed'] is True and type(row['http_status']) is int
                    and 200 <= row['http_status'] < 300, f'{label}: failed request.')
            request_id = headers['x-request-id']
            require(type(request_id) is str and bool(request_id) and request_id == row['request_id']
                    and request_id not in ids, f'{label}: missing, duplicate, or mismatched request ID.')
            ids.add(request_id)
            require(response['object'] == 'chat.completion'
                    and response['model'] == row['returned_model'] == model
                    and type(response['id']) is str and bool(response['id'])
                    and response['id'] == row['response_id'], f'{label}: response identity mismatch.')
            require(len(response['choices']) == 1 and response['choices'][0]['index'] == 0,
                    f'{label}: unexpected choices.')
            choice = response['choices'][0]
            message = choice['message']
            require(message == row['message'] and message.get('role') == 'assistant',
                    f'{label}: message differs.')
            other = {key: value for key, value in message.items()
                     if key not in {'role', 'content', 'refusal', 'annotations'}
                     and value not in (None, [], {})}
            require(message.get('refusal') is None and not other
                    and row['no_refusal_or_action'] is True, f'{label}: refusal or action output.')
            content = message.get('content')
            require(type(content) is str and row['content_key_present'] is True
                    and row['content_is_string'] is True, f'{label}: content is not an actual string.')
            require((content == '') if arm == 'null' else len(content) > 0,
                    f'{label}: content qualification failed.')
            require(choice['finish_reason'] == row['finish_reason'] and
                    (arm != 'null' or choice['finish_reason'] == 'stop'), f'{label}: stop reason mismatch.')
            require(type(row['visible_utf8_bytes']) is int and len(content.encode()) == row['visible_utf8_bytes']
                    and content.encode().hex() == row['content_utf8_hex'], f'{label}: content bytes differ.')
            require(response['usage'] == row['usage'], f'{label}: usage receipt differs.')
            check_usage(response['usage'])
        qualifications[model] = qualification
    return qualifications


def apply_unified_patch(files, patch):
    """Apply the pinned text diff in memory, checking every context byte and hunk count."""
    result = dict(files)
    lines = patch.splitlines(keepends=True)
    index = 0
    changes = []
    while index < len(lines):
        match = re.fullmatch(rb'diff --git a/(.+) b/(.+)\n', lines[index])
        require(match is not None and match[1] == match[2], 'Unsupported patch file header.')
        name = safe_relative(match[2].decode())
        index += 1
        while index < len(lines) and not lines[index].startswith(b'--- '):
            require(not lines[index].startswith((b'diff --git ', b'Binary')), 'Missing text patch body.')
            index += 1
        require(index + 1 < len(lines), 'Truncated patch headers.')
        old_name = lines[index][4:].rstrip(b'\n')
        new_name = lines[index + 1][4:].rstrip(b'\n')
        require(new_name == b'b/' + name.encode() and old_name in (b'/dev/null', b'a/' + name.encode()),
                'Patch path mismatch.')
        require((name not in result) if old_name == b'/dev/null' else name in result,
                f'Patch source existence mismatch: {name}')
        source = result.get(name, b'').splitlines(keepends=True)
        output, cursor, added, deleted = [], 0, 0, 0
        index += 2
        while index < len(lines) and not lines[index].startswith(b'diff --git '):
            hunk = re.match(rb'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', lines[index])
            require(hunk is not None, f'Unsupported patch hunk: {name}')
            old_start, old_count = int(hunk[1]), int(hunk[2] or 1)
            new_start, new_count = int(hunk[3]), int(hunk[4] or 1)
            start = old_start - 1 if old_count else old_start
            require(cursor <= start <= len(source), f'Patch hunk position mismatch: {name}')
            output.extend(source[cursor:start])
            cursor = start
            require(len(output) == (new_start - 1 if new_count else new_start), f'New hunk position mismatch: {name}')
            consumed, produced = 0, 0
            index += 1
            while index < len(lines) and not lines[index].startswith((b'@@ ', b'diff --git ')):
                line = lines[index]
                require(line[:1] in (b' ', b'+', b'-'), 'Unsupported patch line.')
                payload = line[1:]
                if line[:1] in (b' ', b'-'):
                    require(cursor < len(source) and source[cursor] == payload,
                            f'Patch context mismatch: {name}:{cursor + 1}')
                    cursor += 1
                    consumed += 1
                if line[:1] in (b' ', b'+'):
                    output.append(payload)
                    produced += 1
                added += int(line[:1] == b'+')
                deleted += int(line[:1] == b'-')
                index += 1
            require((consumed, produced) == (old_count, new_count), f'Patch hunk count mismatch: {name}')
        output.extend(source[cursor:])
        result[name] = b''.join(output)
        changes.append({'path': name, 'added': added, 'deleted': deleted})
    require(len({item['path'] for item in changes}) == len(changes), 'Duplicate patched file.')
    return result, changes


def source_map(files):
    return {name: sha(value) for name, value in sorted(files.items())
            if name.startswith('src/') and name.endswith('.py')}


def source_identity(files):
    return sha(encoded(dict(sorted(files.items()))))


def verify_implementation(root):
    implementation = load(root, 'artifacts/implementation.json')
    archive = root / 'vendor/sdk-upstream.tar.gz'
    patch = (root / 'artifacts/upstream.patch').read_bytes()
    require(implementation['upstream_commit'] == PIN
            and implementation['upstream_repository'] == 'https://github.com/openai/openai-agents-python',
            'SDK pin differs.')
    require(sha(archive.read_bytes()) == implementation['source_archive_sha256']
            and sha(patch) == implementation['patch_sha256'], 'SDK archive/patch hash mismatch.')
    files = {}
    with tarfile.open(archive, 'r:gz') as handle:
        for member in handle.getmembers():
            name = safe_relative(member.name)
            if member.isdir():
                continue
            if member.issym():
                safe_relative(member.linkname)
                require(not name.startswith('src/'), 'Source symlink unsupported in identity map.')
                continue
            require(member.isfile() and name not in files, f'Unsupported or duplicate archive member: {name}')
            files[name] = handle.extractfile(member).read()
    patched, changes = apply_unified_patch(files, patch)
    require(changes == implementation['paths'] and len(changes) == implementation['changed_files']
            and sum(row['added'] for row in changes) == implementation['added_lines']
            and sum(row['deleted'] for row in changes) == implementation['deleted_lines'],
            'Patch statistics differ from the implementation receipt.')
    maps = {variant: source_map(values) for variant, values in zip(VARIANTS, (files, patched))}
    for variant in VARIANTS:
        require(source_identity(maps[variant]) == implementation[f'{variant}_source_identity_sha256'],
                f'{variant}: source identity differs from archive plus patch.')
    return implementation, maps, patched


def synthetic(model, content=None, reason='stop', refusal=None, tool=None):
    message = {'role': 'assistant', 'content': content, 'refusal': refusal}
    if tool:
        message['tool_calls'] = [{'id': f'call_{tool}', 'type': 'function',
                                 'function': {'name': tool, 'arguments': '{}'}}]
    return encoded({'id': 'synthetic-chat-completion', 'object': 'chat.completion', 'created': 0,
                    'model': model, 'choices': [{'index': 0, 'message': message, 'finish_reason': reason}],
                    'usage': {'prompt_tokens': 5, 'completion_tokens': int(bool(content)),
                              'total_tokens': 5 + int(bool(content))}})


def sse(body):
    envelope = decode(body)
    choice = envelope['choices'][0]
    delta = {key: value for key, value in choice['message'].items()
             if key in ('role', 'content', 'refusal', 'tool_calls') and value is not None}
    for index, item in enumerate(delta.get('tool_calls', [])):
        item['index'] = index
    base = {key: envelope[key] for key in ('id', 'created', 'model')}
    base['object'] = 'chat.completion.chunk'
    chunks = [{**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
              {**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': choice['finish_reason']}]},
              {**base, 'choices': [], 'usage': envelope.get('usage')}]
    return b''.join(b'data: ' + encoded(chunk) + b'\n\n' for chunk in chunks) + b'data: [DONE]\n\n'


def case_contract(model, name, variant, captured):
    terminal = name in ('captured_null_structured', 'tool_then_null', 'handoff_then_null')
    expected = {'fixtures': [(captured['null'], 200, 'null')], 'error': None,
                'output': None, 'terminal': terminal and variant == 'patched'}
    if name == 'captured_control_plain':
        expected['fixtures'] = [(captured['control'], 200, 'control')]
        expected['output'] = decode(captured['control'])['choices'][0]['message']['content']
    elif name == 'valid_structured':
        expected['fixtures'] = [(synthetic(model, '{"answer":"synthetic validated JSON"}'), 200, None)]
        expected['output'] = {'answer': 'synthetic validated JSON'}
    elif name in ('nonempty_invalid', 'whitespace', 'invisible'):
        content = {'nonempty_invalid': 'not JSON', 'whitespace': ' ', 'invisible': '\u200b'}[name]
        expected['fixtures'] = [(synthetic(model, content), 200, None)]
        expected['error'] = 'ModelBehaviorError'
    elif name == 'unknown_empty':
        expected['fixtures'] = [(synthetic(model), 200, None)]
        expected['error'] = 'ReplayRequestLimit'
    elif name in ('refusal', 'content_filter', 'length'):
        options = {'refusal': {'refusal': 'Synthetic provider refusal.'},
                   'content_filter': {'reason': 'content_filter'}, 'length': {'content': '', 'reason': 'length'}}[name]
        expected['fixtures'] = [(synthetic(model, **options), 200, None)]
        expected['error'] = 'ModelBehaviorError' if name == 'length' else 'ModelRefusalError'
    elif name == 'http_error':
        expected['fixtures'] = [(encoded({'error': {'message': 'Synthetic HTTP error.',
            'type': 'invalid_request_error', 'code': 'bad_request'}}), 400, None)]
        expected['error'] = 'BadRequestError'
    elif name == 'policy_off_recovery':
        expected['fixtures'] = [(synthetic(model, ''), 200, None),
                               (synthetic(model, '{"answer":"synthetic recovery"}'), 200, None)]
        expected['output'] = {'answer': 'synthetic recovery'}
    elif name in ('plain_text', 'plain_empty_default'):
        expected['output'] = 'Synthetic plain answer.' if name == 'plain_text' else ''
        expected['fixtures'] = [(synthetic(model, expected['output']), 200, None)]
    elif name in ('tool_then_null', 'handoff_then_null'):
        tool = 'counted_tool' if name == 'tool_then_null' else 'transfer_to_delegate'
        expected['fixtures'].insert(0, (synthetic(model, reason='tool_calls', tool=tool), 200, None))
    if terminal and variant == 'upstream':
        expected['error'] = 'ReplayRequestLimit'
    expected['requests'] = len(expected['fixtures']) + int(expected['error'] == 'ReplayRequestLimit')
    return expected


def assistant_texts(items):
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get('role') != 'assistant':
            continue
        content = item.get('content')
        if isinstance(content, str):
            result.append(content)
        else:
            result.extend(part['text'] for part in content or []
                          if isinstance(part, dict) and part.get('type') == 'output_text')
    return result


def check_named(checks, expected_names, label):
    require(isinstance(checks, list) and len(checks) == len(expected_names)
            and {check['name'] for check in checks} == set(expected_names)
            and all(check['passed'] is True for check in checks), f'{label}: missing or failed checks.')


def verify_case(row, model, variant, captured):
    name, mode = row['case'], row['mode']
    label = f'{model}/{variant}/{name}/{mode}'
    expected = case_contract(model, name, variant, captured)
    streamed = mode == 'streaming'
    requested = name not in ('policy_off_recovery', 'plain_empty_default')
    require(row['passed'] is True and row['network_requests'] == 0
            and row['policy_requested'] is requested and row['policy_enabled'] is (requested and variant == 'patched'),
            f'{label}: policy or network record differs.')
    require(row['request_count'] == len(row['requests']) == expected['requests']
            and row['replayed_response_count'] == len(expected['fixtures']), f'{label}: transport counts differ.')
    schema = None if name in ('captured_control_plain', 'plain_text', 'plain_empty_default') else {
        'properties': {'answer': {'title': 'Answer', 'type': 'string'}}, 'required': ['answer'],
        'title': 'Answer', 'type': 'object', 'additionalProperties': False}
    require(row['local_output_schema'] == schema, f'{label}: local output schema differs.')
    arm = 'control' if name == 'captured_control_plain' else 'null'
    require(row['requests'][0]['body']['messages'] == expected_request(model, arm)['messages'],
            f'{label}: first prompt differs.')
    provider_texts = []
    for index, request in enumerate(row['requests']):
        body = request['body']
        check_budget(body, model)
        require(request['number'] == index + 1 and request['method'] == 'POST'
                and request['url'] == 'https://replay.invalid/v1/chat/completions'
                and request['body_sha256'] == sha(encoded(body))
                and body.get('stream', False) is streamed and 'response_format' not in body,
                f'{label}: request wire or adapter contract differs.')
        if index == len(expected['fixtures']):
            require(request.get('blocked') == 'ReplayRequestLimit'
                    and 'response_sha256' not in request, f'{label}: extra request was not blocked.')
            continue
        fixture, status, captured_arm = expected['fixtures'][index]
        wire = sse(fixture) if streamed and status == 200 else fixture
        require('blocked' not in request and request['status'] == status
                and request['source_fixture_sha256'] == sha(fixture)
                and request['response_sha256'] == sha(wire) and request['response_bytes'] == len(wire)
                and request['exact_source_bytes'] is (wire == fixture), f'{label}: replay fixture bytes differ.')
        path = f'records/{model}/{captured_arm}.response.raw.json' if captured_arm else None
        require(request['source_path'] == path, f'{label}: source fixture path differs.')
        if streamed:
            require(request['fixture_kind'] == 'synthetic SSE; not live streaming', f'{label}: unlabeled stream.')
        for choice in decode(fixture).get('choices', []):
            content = choice.get('message', {}).get('content')
            if isinstance(content, str):
                provider_texts.append(content)
    require(row['source_fixture_sha256'] == sha(expected['fixtures'][0][0])
            and row['source_fixture_path'] == row['requests'][0]['source_path'], f'{label}: initial fixture differs.')
    errors = [item['type'] for item in (row['exception'] or {}).get('chain', [])]
    require((row['exception'] is None) if expected['error'] is None else expected['error'] in errors,
            f'{label}: exception differs.')
    require(row['final_output'] == expected['output']
            and row['completion_reason'] == ('completed_without_output' if expected['terminal'] else None),
            f'{label}: final output or completion reason differs.')
    available = row['new_items_available']
    require(type(available) is bool and (isinstance(row['new_items'], list) if available else row['new_items'] is None),
            f'{label}: item availability mismatch.')
    actual = assistant_texts([item['raw_item'] for item in row['new_items'] or []])
    session = assistant_texts(row['session_items'])
    invented = list((Counter(actual) - Counter(provider_texts)).elements())
    session_invented = list((Counter(session) - Counter(provider_texts)).elements())
    require(row['provider_answer_texts'] == provider_texts and row['actual_assistant_texts'] == actual
            and row['synthesized_answer_count'] == (len(invented) if available else None)
            and row['synthesized_answer_texts'] == (invented if available else None)
            and row['session_synthesized_answer_count'] == len(session_invented)
            and not invented and not session_invented, f'{label}: invented answer or unavailable count mismatch.')
    require(type(row['session_synthesized_answer_count']) is int
            and (type(row['synthesized_answer_count']) is int if available else row['synthesized_answer_count'] is None),
            f'{label}: invalid answer count type.')
    require((row['new_items_unavailable_reason'] is None) if available else bool(row['new_items_unavailable_reason']),
            f'{label}: unavailable output lacks explanation.')
    require(row['tool_counter'] == int(name == 'tool_then_null')
            and row['handoff_counter'] == int(name == 'handoff_then_null'), f'{label}: side effect count differs.')
    terminal_spans = [span for span in row['spans']
                      if span.get('span_data', {}).get('name') == 'completed_without_output']
    require(len(terminal_spans) == int(expected['terminal']), f'{label}: terminal trace differs.')
    checks = ['expected_request_count', 'expected_error', 'expected_final_output',
              'no_observed_synthesized_assistant_answer', 'no_provider_structured_output_instruction',
              'original_first_prompt_preserved', 'tool_executed_exactly_once_when_selected',
              'handoff_executed_exactly_once_when_selected', 'terminal_reason', 'terminal_trace', 'fixed_transport_cap']
    if name == 'captured_null_structured' and not streamed:
        checks.append('exact_captured_null_response_bytes')
    if expected['terminal']:
        checks.append('no_assistant_message_added_for_empty_tail')
        require(available and not actual and not session and row['final_output'] is None
                and row['synthesized_answer_count'] == row['session_synthesized_answer_count'] == 0,
                f'{label}: terminal result added an answer.')
        require(terminal_spans[0]['span_data']['data'] == {'provider_finish_reason': 'stop'},
                f'{label}: terminal trace provider reason differs.')
        raw = row['raw_responses'][-1]
        usage = decode(captured['null'])['usage']
        require(raw['empty_output_stop_reason'] == 'stop' and raw['output'] == []
                and raw['usage'] == {'requests': 1, 'input_tokens': usage['prompt_tokens'],
                                     'output_tokens': usage['completion_tokens'], 'total_tokens': usage['total_tokens']},
                f'{label}: terminal provider metadata differs.')
    if name in ('tool_then_null', 'handoff_then_null'):
        checks.append('one_persisted_call_output_pair')
        call_id = 'call_counted_tool' if name == 'tool_then_null' else 'call_transfer_to_delegate'
        require(all(sum(item.get('type') == kind and item.get('call_id') == call_id
                        for item in row['session_items']) == 1
                    for kind in ('function_call', 'function_call_output')), f'{label}: persisted action pair differs.')
    check_named(row['checks'], checks, label)


def verify_replays(root, qualifications, implementation, maps):
    base = 'records/replay'
    plan = load(root, f'{base}/replay-plan.json')
    preflight = load(root, f'{base}/replay-preflight.json')
    replay_hash = sha((root / 'replay.py').read_bytes())
    runner_hash = sha((root / 'run_replay.py').read_bytes())
    plan_hash = sha((root / base / 'replay-plan.json').read_bytes())
    identities = {variant: source_identity(maps[variant]) for variant in VARIANTS}
    require(plan['sdk_commit'] == PIN and plan['source_archive_sha256'] == implementation['source_archive_sha256']
            and plan['patch_sha256'] == implementation['patch_sha256']
            and preflight['source_files'] == maps, 'Replay SDK preflight differs.')
    for receipt in (plan, preflight):
        require(receipt['replay_sha256'] == replay_hash and receipt['runner_sha256'] == runner_hash
                and receipt['source_identities'] == identities and receipt['live_requests'] == 0,
                'Replay preflight script/source binding mismatch.')
    require(preflight['plan_sha256'] == plan_hash and timestamp(preflight['time_utc']) >= timestamp(plan['time_utc']),
            'Replay preflight plan binding/time mismatch.')
    require(plan['cases'] == list(CASES) and plan['modes'] == list(MODES)
            and plan['cases_per_process'] == 32 and set(plan['models']) == set(MODELS), 'Replay plan matrix differs.')
    require([(row['model'], row['variant']) for row in plan['processes']]
            == [(model, variant) for model in MODELS for variant in VARIANTS], 'Replay process schedule differs.')
    process_plans = {(row['model'], row['variant']): row for row in plan['processes']}
    reports = {}
    for model in MODELS:
        captured = {arm: (root / f'records/{model}/{arm}.response.raw.json').read_bytes() for arm in ARMS}
        qualification_hash = sha((root / f'records/{model}/qualification.json').read_bytes())
        artifacts = {row['arm']: {'request_sha256': row['request_sha256'],
                                'response_sha256': row['raw_response_sha256']}
                     for row in qualifications[model]['results']}
        require(plan['models'][model] == {'model': model, 'budget_field': MODELS[model], 'budget': 1500,
                'qualification_sha256': qualification_hash, 'artifacts': artifacts}, f'{model}: replay plan input differs.')
        for variant in VARIANTS:
            prefix = f'{base}/{model}/replay-{variant}'
            report = load(root, prefix + '.json')
            command = load(root, prefix + '-command.json')
            require(command['exit_code'] == 0 and command['harness_unchanged'] is True
                    and command['plan_sha256'] == plan_hash
                    and command['command'] == process_plans[model, variant]['command'], 'Replay command receipt differs.')
            require(report['schema'] == 'agents-terminal-empty-replay/v1' and report['variant'] == variant
                    and report['requested_model'] == model and report['sdk_commit'] == PIN
                    and report['replay_script_sha256'] == replay_hash
                    and report['qualification_receipt_sha256'] == qualification_hash
                    and report['sdk_source_files'] == maps[variant]
                    and report['sdk_source_identity_sha256'] == identities[variant], 'Replay source/input identity differs.')
            require(report['status'] == 'PASS' and report['passed'] == report['total'] == 32
                    and report['no_live_api_calls'] is True and report['network_blocked'] is True, 'Replay result differs.')
            require(report['fixtures'] == {arm: {'path': f'records/{model}/{arm}.response.raw.json',
                    'sha256': sha(raw), 'bytes': len(raw)} for arm, raw in captured.items()}, 'Replay fixture map differs.')
            require([(row['case'], row['mode']) for row in report['cases']]
                    == [(case, mode) for case in CASES for mode in MODES], 'Replay case set/order differs.')
            for row in report['cases']:
                require(all(timestamp(span['started_at']) >= timestamp(preflight['time_utc'])
                            and timestamp(span['ended_at']) >= timestamp(span['started_at'])
                            for span in row['spans']), 'Replay trace predates preflight or start.')
                verify_case(row, model, variant, captured)
            reports[model, variant] = report
        upstream, patched = (reports[model, variant] for variant in VARIANTS)
        comparison = patched['comparison']
        require(comparison['status'] == 'PASS' and comparison['peer_sha256']
                == sha((root / f'{base}/{model}/replay-upstream.json').read_bytes()), 'A/B peer binding differs.')
        check_named(comparison['checks'], ['same_harness_bytes', 'same_source_fixture_bytes', 'same_case_set'] +
                    [f'{case}/{mode}:identical_common_requests_and_responses' for case in CASES for mode in MODES],
                    f'{model}: A/B comparison')
        for left, right in zip(upstream['cases'], patched['cases']):
            require(left['local_output_schema'] == right['local_output_schema'], 'A/B local schemas differ.')
            for a, b in zip(left['requests'], right['requests']):
                require(a['body'] == b['body'] and a['body_sha256'] == b['body_sha256'], 'A/B request wires differ.')
                if 'response_sha256' in a and 'response_sha256' in b:
                    require(a['response_sha256'] == b['response_sha256'], 'A/B served bytes differ.')
    return reports


def parse_test_summaries(log):
    summaries = []
    for line in log.splitlines():
        if line.startswith('=') and re.search(r'\b\d+ passed\b', line):
            numbers = {key: int(number) for number, key in
                       re.findall(r'(\d+) (passed|skipped|failed|deselected|errors?)\b', line)}
            summaries.append({**numbers, 'raw_summary': line})
    if len(summaries) != 2:
        return {'parsed': False, 'summaries': summaries}
    return {'parsed': True, 'parallel': summaries[0], 'serial': summaries[1],
            'total_passed': sum(row.get('passed', 0) for row in summaries),
            'total_skipped': sum(row.get('skipped', 0) for row in summaries)}


def verify_sdk_attempt(root, base, implementation, patched, runner):
    report = load(base, 'verification.json')
    provenance = load(base, 'provenance.json')
    expected_sources = {name: sha(raw) for name, raw in sorted(patched.items())}
    require(load(base, 'source-files-before.json') == load(base, 'source-files-after.json') == expected_sources,
            'SDK gate source files differ from archive plus patch.')
    expected_inputs = {name: sha((root / name).read_bytes()) for name in
                       ('vendor/sdk-upstream.tar.gz', 'artifacts/upstream.patch', 'artifacts/implementation.json')}
    require(load(base, 'inputs-before.json') == load(base, 'inputs-after.json') == expected_inputs,
            'SDK gate package inputs differ.')
    require(provenance['upstream_commit'] == PIN and provenance['archive_sha256'] == implementation['source_archive_sha256']
            and provenance['patch_sha256'] == implementation['patch_sha256']
            and provenance['source_file_count'] == len(expected_sources)
            and provenance['patch_applied_unchanged'] is True
            and provenance['live_provider_integration_requested'] is False, 'SDK gate provenance differs.')
    for variant in VARIANTS:
        require(provenance[f'{variant}_source_identity_sha256'] == implementation[f'{variant}_source_identity_sha256'],
                'SDK gate source identity differs.')
    environment = load(base, 'environment.json')
    require(environment['verification_runner_sha256'] == sha(runner.read_bytes())
            and environment['credentials_inherited'] is False
            and environment['settings']['UV_FROZEN'] == environment['settings']['UV_OFFLINE'] == '1',
            'SDK gate runner/environment binding differs.')
    for name in ('patch-check', 'patch-apply', 'environment-setup', 'repository-verification'):
        command = load(base, f'{name}-command.json')
        require(type(command['exit_code']) is int and command['log'] == f'{name}.log'
                and command['log_sha256'] == sha((base / command['log']).read_bytes())
                and timestamp(command['finished_utc']) >= timestamp(command['started_utc']),
                f'SDK gate command/log differs: {name}')
        if name in ('patch-check', 'patch-apply'):
            require(command['exit_code'] == 0, 'SDK patch application failed.')
        if name == 'environment-setup':
            require(command == report['setup'], 'SDK setup receipt differs.')
        if name == 'repository-verification':
            require(command == report['gate'] and command['command'] ==
                    ['bash', '.agents/skills/code-change-verification/scripts/run.sh'], 'SDK gate command differs.')
    log = (base / 'repository-verification.log').read_text()
    checks = {name: f'make {name} passed in ' in log for name in ('format', 'lint', 'typecheck', 'tests')}
    mypy = re.search(r'Success: no issues found in (\d+) source files', log)
    counts = parse_test_summaries(log)
    passed = report['gate']['exit_code'] == 0 and all(checks.values()) and counts['parsed']
    require(report['schema'] == 'fresh-patched-sdk-verification/v1'
            and report['status'] == ('PASS' if passed else 'FAIL')
            and report['checks'] == checks and report['tests'] == counts
            and report['mypy_checked_files'] == (int(mypy[1]) if mypy else None)
            and report['pyright_zero_diagnostics'] is ('0 errors, 0 warnings, 0 informations' in log)
            and report['input_files_unchanged'] is True and report['source_files_unchanged'] is True
            and report['changed_source_paths'] == []
            and report['source_identity_after'] == implementation['patched_source_identity_sha256'],
            'SDK gate outcome or recomputed counts differ.')
    return report


def verify_sdk_gate(root, implementation, patched):
    base = root / 'artifacts/sdk-verification'
    if not base.exists():
        return None
    aggregate = load(base, 'result.json')
    require(aggregate['schema'] == 'sdk-verification-attempts/v1'
            and aggregate['final_status'] == 'PASS' and aggregate['sdk_or_patch_changes'] is False,
            'SDK aggregate outcome differs.')
    attempts = aggregate['attempts']
    require(attempts and [row['attempt'] for row in attempts] == list(range(1, len(attempts) + 1)),
            'SDK attempt sequence differs.')
    receipts = {}
    for attempt in attempts:
        relative = safe_relative(attempt['receipt'])
        path = base / relative
        require(path.resolve().is_relative_to(base.resolve())
                and attempt['receipt_sha256'] == sha(path.read_bytes()), 'SDK attempt receipt hash differs.')
        runner = root / 'validate_sdk.py'
        if attempt['attempt'] < len(attempts):
            runner = base / f'runner-attempt-{attempt["attempt"]}.py'
        report = verify_sdk_attempt(root, path.parent, implementation, patched, runner)
        require(report['status'] == attempt['status'] and report['gate']['exit_code'] == attempt['gate_exit_code'],
                'SDK attempt outcome differs.')
        receipts[relative] = report
    require(aggregate['final_receipt'] == attempts[-1]['receipt']
            and aggregate['final_receipt_sha256'] == attempts[-1]['receipt_sha256'], 'SDK final receipt selection differs.')
    final = receipts[aggregate['final_receipt']]
    require(final['status'] == 'PASS' and final['pyright_zero_diagnostics'] is True
            and final['mypy_checked_files'] is not None and final['tests']['parsed'] is True,
            'Final SDK gate did not pass.')
    for phase in ('parallel', 'serial'):
        require(not any(final['tests'][phase].get(key, 0) for key in ('failed', 'error', 'errors')),
                'Final SDK tests failed.')
    return final


def manifest_excluded(relative):
    parts = PurePosixPath(relative).parts
    return (relative == 'SHA256SUMS' or relative.startswith('records/replay/sdk/')
            or any(part in {'.git', '.venv', '__pycache__', '.pytest_cache'} for part in parts)
            or relative.endswith('.pyc'))


def verify_manifest(root):
    entries = {}
    for line in (root / 'SHA256SUMS').read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        require(match is not None, 'Invalid manifest line.')
        expected, relative = match.groups()
        require(safe_relative(relative) == relative and relative not in entries
                and not manifest_excluded(relative), f'Invalid or duplicate manifest path: {relative}')
        path = root / relative
        require(path.resolve().is_relative_to(root.resolve()) and path.is_file() and not path.is_symlink(),
                f'Manifest path escapes package or is missing: {relative}')
        require(sha(path.read_bytes()) == expected, f'Hash mismatch: {relative}')
        entries[relative] = expected
    actual = {path.relative_to(root).as_posix() for path in root.rglob('*')
              if path.is_file() and not manifest_excluded(path.relative_to(root).as_posix())}
    require(set(entries) == actual, 'Manifest coverage differs: ' + repr(sorted(set(entries) ^ actual)))
    return len(entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records-only', action='store_true', help='Check records before generating SHA256SUMS.')
    args = parser.parse_args()
    try:
        count = None if args.records_only else verify_manifest(ROOT)
        qualifications = verify_live(ROOT)
        implementation, maps, patched = verify_implementation(ROOT)
        verify_replays(ROOT, qualifications, implementation, maps)
        gate = verify_sdk_gate(ROOT, implementation, patched)
    except (OSError, ValueError, KeyError, TypeError, IndexError, tarfile.TarError) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    prefix = '' if count is None else f'{count} file hashes; '
    print(f'PASS: {prefix}4 live records; 128 offline replay/control cases; identical A/B request construction.')
    if gate:
        print(f'SDK gate records: {gate["tests"]["total_passed"]} passed; {gate["tests"]["total_skipped"]} skipped; format/lint/type checks passed.')
    else:
        print('No full SDK gate receipt is present.')
    print('Saved-record verification only. No SDK execution or fresh inference.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
