"""Freshly extract the pinned SDK and replay both model records offline."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parent
MODELS = {'gpt-4-0613': 'max_tokens', 'gpt-6-astra': 'max_completion_tokens'}
VARIANTS = ('upstream', 'patched')
CASES = ['captured_null_structured', 'captured_control_plain', 'valid_structured', 'refusal',
         'content_filter', 'length', 'http_error', 'tool_then_null', 'handoff_then_null',
         'policy_off_recovery', 'plain_text', 'plain_empty_default',
         'nonempty_invalid', 'whitespace', 'invisible', 'unknown_empty']
ORIGINAL_REPLAY_SHA256 = '5dae85abf47e5918d038685cef380e3efd2b8347e64a94c221e4598da248be3f'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(path.read_bytes())


def write_json(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def source_files(path):
    return {str(item.relative_to(path)): sha(item.read_bytes())
            for item in sorted((path / 'src').rglob('*.py'))}


def source_identity(files):
    return sha(json.dumps(files, ensure_ascii=False, separators=(',', ':')).encode())


def request_contract(request):
    model = request['model']
    require(model in MODELS, f'Unsupported captured model: {model}')
    budget_field = MODELS[model]
    present = set(request) & {'max_tokens', 'max_completion_tokens'}
    require(present == {budget_field}, f'Wrong captured budget field for {model}.')
    require(type(request[budget_field]) is int and request[budget_field] == 1500,
            f'Wrong captured token budget for {model}.')
    return {'model': model, 'budget_field': budget_field, 'budget': request[budget_field]}


def verify_records(records_dir, model):
    qualification_bytes = (records_dir / 'qualification.json').read_bytes()
    qualification = json.loads(qualification_bytes)
    rows = qualification.get('results', [])
    require(qualification.get('qualified') is True and qualification.get('requests_attempted') == 2,
            f'{model}: both live qualification records must pass; this program never retries live.')
    require([row['arm'] for row in rows] == ['null', 'control'], f'{model}: wrong qualification arms.')
    contracts = []
    artifacts = {}
    for row in rows:
        arm = row['arm']
        request_bytes = (records_dir / f'{arm}.request.json').read_bytes()
        response_bytes = (records_dir / f'{arm}.response.raw.json').read_bytes()
        require(sha(request_bytes) == row['request_sha256'], f'{model}/{arm}: request hash mismatch.')
        require(sha(response_bytes) == row['raw_response_sha256'], f'{model}/{arm}: response hash mismatch.')
        require(row.get('passed') is True and row.get('http_status') == 200,
                f'{model}/{arm}: qualification failed.')
        contract = request_contract(json.loads(request_bytes))
        require(contract['model'] == model, f'{model}/{arm}: wrong requested model.')
        response = json.loads(response_bytes)
        require(len(response['choices']) == 1, f'{model}/{arm}: wrong choice count.')
        choice = response['choices'][0]
        message = choice['message']
        require(choice['finish_reason'] == 'stop' and message.get('role') == 'assistant',
                f'{model}/{arm}: response is not a normal assistant stop.')
        require(message.get('refusal') is None and not message.get('tool_calls') and not message.get('function_call'),
                f'{model}/{arm}: refusal or action in response.')
        content = message.get('content')
        require(type(content) is str and ((content == '') if arm == 'null' else len(content) > 0),
                f'{model}/{arm}: visible content qualification failed.')
        contracts.append(contract)
        artifacts[arm] = {'request_sha256': sha(request_bytes), 'response_sha256': sha(response_bytes)}
    require(contracts[0] == contracts[1], f'{model}: request contracts differ by arm.')
    return {**contracts[0], 'qualification_sha256': sha(qualification_bytes), 'artifacts': artifacts}


def extract_archive(archive, destination):
    destination.mkdir(parents=True)
    with tarfile.open(archive, 'r:gz') as source:
        source.extractall(destination, filter='data')


def child_environment(source):
    environment = os.environ.copy()
    for key in list(environment):
        if any(term in key.upper() for term in ('API_KEY', 'TOKEN', 'SECRET')):
            del environment[key]
    environment['PYTHONPATH'] = str(source / 'src')
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'records/replay')
    args = parser.parse_args()
    output = args.output_dir.resolve()
    require(not output.exists(), 'Refusing to overwrite an existing output directory.')
    implementation = load(ROOT / 'artifacts/implementation.json')
    archive = ROOT / 'vendor/sdk-upstream.tar.gz'
    sdk_patch = ROOT / 'artifacts/upstream.patch'
    require(sha(archive.read_bytes()) == implementation['source_archive_sha256'], 'Source archive hash mismatch.')
    require(sha(sdk_patch.read_bytes()) == implementation['patch_sha256'], 'SDK patch hash mismatch.')
    contracts = {model: verify_records(ROOT / 'records' / model, model) for model in MODELS}
    replay_hash = sha((ROOT / 'replay.py').read_bytes())
    wrapper_hash = sha(Path(__file__).read_bytes())
    commands = []
    for model in MODELS:
        for variant in VARIANTS:
            command = [sys.executable, '-B', str(ROOT / 'replay.py'),
                       '--variant', variant, '--sdk-source', str(output / 'sdk' / variant),
                       '--records-dir', str(ROOT / 'records' / model),
                       '--expected-commit', implementation['upstream_commit'],
                       '--output', str(output / model / f'replay-{variant}.json')]
            if variant == 'patched':
                command += ['--compare', str(output / model / 'replay-upstream.json')]
            commands.append({'model': model, 'variant': variant, 'command': command})
    output.mkdir(parents=True, exist_ok=False)
    plan = {
        'time_utc': datetime.now(timezone.utc).isoformat(),
        'stage': 'OFFLINE RECORD-REPLAY AND LABELED SYNTHETIC CONTROLS',
        'sdk_commit': implementation['upstream_commit'],
        'source_archive_sha256': implementation['source_archive_sha256'],
        'patch_sha256': implementation['patch_sha256'],
        'source_identities': {variant: implementation[f'{variant}_source_identity_sha256'] for variant in VARIANTS},
        'replay_sha256': replay_hash, 'runner_sha256': wrapper_hash,
        'original_replay_sha256': ORIGINAL_REPLAY_SHA256,
        'harness_change': 'Captured records directory, model, and token budget are parameters. Synthetic response model metadata follows the captured request. Receipt schema and paths are updated. Case predicates and the SDK patch are unchanged.',
        'harness_changes': [
            'Required --records-dir replaces the hardcoded artifacts directory for qualification, requests, and responses.',
            'run_case receives the captured request objects; control uses control.request.json and every other case uses null.request.json.',
            'OpenAIChatCompletionsModel takes the captured requested model in both SDK variants.',
            'max_tokens uses ModelSettings(max_tokens=1500); max_completion_tokens uses ModelSettings(extra_args={max_completion_tokens: 1500}). Exactly one captured budget field is required.',
            'Synthetic completion model metadata takes the captured requested model; synthetic case content and schedules are unchanged.',
            'Report schema is agents-terminal-empty-replay/v1 and adds records_dir and requested_model.',
            'Fixture paths support records outside the package. Output defaults inside records_dir and opens exclusively to reject an existing receipt.',
            'Source-file maps, local output schema adapter, all expected outcomes, all assertion predicates, and streaming labels are unchanged.',
        ],
        'original_predicate_ast_sha256': {
            'expected_outcomes': 'a55ee1cf27fc806432b20f4ff39e7cabf636c42bb1bfc86de5b78ce1cbf66582',
            'checks': '359347ef13b2f4909555156357ef1fe529f9497f56e5993db7eb9b9c2470844e',
        },
        'models': contracts, 'cases': CASES, 'modes': ['nonstreaming', 'streaming'],
        'cases_per_process': 32, 'processes': commands, 'live_requests': 0,
        'streaming': 'Synthetic SSE derived from fixtures; not captured live streaming.',
        'python': platform.python_version(), 'platform': platform.platform(),
        'dependencies': {d.metadata['Name']: d.version for d in importlib.metadata.distributions()},
    }
    write_json(output / 'replay-plan.json', plan)
    plan_hash = sha((output / 'replay-plan.json').read_bytes())
    maps = {}
    for variant in VARIANTS:
        source = output / 'sdk' / variant
        extract_archive(archive, source)
        require(source_identity(source_files(source)) == implementation['upstream_source_identity_sha256'],
                f'{variant}: fresh archive source identity mismatch.')
        if variant == 'patched':
            subprocess.run(['git', 'apply', '--check', str(sdk_patch)], cwd=source, check=True)
            subprocess.run(['git', 'apply', str(sdk_patch)], cwd=source, check=True)
        maps[variant] = source_files(source)
        require(source_identity(maps[variant]) == implementation[f'{variant}_source_identity_sha256'],
                f'{variant}: source identity mismatch after patch application.')
    write_json(output / 'replay-preflight.json', {
        'time_utc': datetime.now(timezone.utc).isoformat(), 'plan_sha256': plan_hash,
        'replay_sha256': replay_hash, 'runner_sha256': wrapper_hash,
        'source_identities': {variant: source_identity(maps[variant]) for variant in VARIANTS},
        'source_files': maps, 'live_requests': 0,
    })
    for process_plan in commands:
        model, variant, command = (process_plan[key] for key in ('model', 'variant', 'command'))
        destination = output / model
        destination.mkdir(exist_ok=True)
        require(sha((output / 'replay-plan.json').read_bytes()) == plan_hash, 'Replay plan changed.')
        require(sha((ROOT / 'replay.py').read_bytes()) == replay_hash, 'Replay harness changed.')
        require(sha(Path(__file__).read_bytes()) == wrapper_hash, 'Replay runner changed.')
        require(verify_records(ROOT / 'records' / model, model) == contracts[model], 'Live records changed.')
        environment = child_environment(output / 'sdk' / variant)
        with (destination / f'replay-{variant}.log').open('xb') as log:
            process = subprocess.Popen(command, env=environment, cwd=ROOT,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in process.stdout:
                log.write(line)
                log.flush()
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
            code = process.wait()
        receipt = {'command': command, 'exit_code': code, 'plan_sha256': plan_hash,
                   'PYTHONPATH': environment['PYTHONPATH'],
                   'harness_unchanged': sha((ROOT / 'replay.py').read_bytes()) == replay_hash}
        write_json(destination / f'replay-{variant}-command.json', receipt)
        if code:
            raise SystemExit(code)
        report = load(destination / f'replay-{variant}.json')
        require(report['sdk_source_files'] == maps[variant], f'{model}/{variant}: executed source map differs.')
        require(report['sdk_source_identity_sha256'] == implementation[f'{variant}_source_identity_sha256'],
                f'{model}/{variant}: executed source identity differs.')
        require(report['status'] == 'PASS' and report['passed'] == report['total'] == 32,
                f'{model}/{variant}: replay did not pass every planned case.')
        require([(row['case'], row['mode']) for row in report['cases']]
                == [(case, mode) for case in CASES for mode in ('nonstreaming', 'streaming')],
                f'{model}/{variant}: replay case set changed.')
        for row in report['cases']:
            for request in row['requests']:
                require(request_contract(request['body']) == {key: contracts[model][key]
                        for key in ('model', 'budget_field', 'budget')}, 'Replay request parameters differ.')
    require(sha((output / 'replay-plan.json').read_bytes()) == plan_hash, 'Replay plan changed.')
    print('PASS: 128 offline replay/control cases across four processes; pinned source maps and request budgets verified.')


if __name__ == '__main__':
    main()
