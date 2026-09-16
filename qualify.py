"""Run the frozen four-request qualification once. No automatic retries."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import httpx2

ROOT = Path(__file__).resolve().parent

def sha(data):
    return hashlib.sha256(data).hexdigest()

def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credential-file', type=Path)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'records')
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'live-plan.json').exists():
        raise RuntimeError('This record already started; choose a new output directory for an authorized new run.')
    protocol_raw = (ROOT / 'protocol.json').read_bytes()
    protocol = json.loads(protocol_raw)
    requests = []
    for model in protocol['models']:
        for arm in protocol['arms']:
            request = {'model': model['id'], 'messages': [
                {'role': 'system', 'content': protocol['system']},
                {'role': 'user', 'content': arm['input']}],
                model['budget_field']: model['budget'], 'stream': protocol['stream']}
            requests.append({'model': model['id'], 'arm': arm['name'], 'request': request,
                             'request_sha256': sha(encoded(request))})
    if len(requests) != protocol['maximum_live_requests'] or protocol['transport_retries'] != 0:
        raise RuntimeError('Invalid frozen request count or retry setting.')
    key = os.environ.get('OPENAI_API_KEY')
    if args.credential_file:
        matches = re.findall(r'sk-[A-Za-z0-9_-]+', args.credential_file.read_text())
        if len(set(matches)) != 1:
            raise RuntimeError('Credential file must contain exactly one API key.')
        key = matches[0]
        del matches
    if not key:
        raise RuntimeError('Set OPENAI_API_KEY or supply --credential-file.')
    plan = {'stage': 'LIVE INFERENCE', 'started_at': datetime.now(timezone.utc).isoformat(),
            'protocol_sha256': sha(protocol_raw), 'qualifier_sha256': sha(Path(__file__).read_bytes()),
            'maximum_live_requests': 4, 'transport_retries': 0, 'requests': requests}
    with (output / 'live-plan.json').open('x') as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    outcomes = {m['id']: [] for m in protocol['models']}
    with httpx2.Client(timeout=protocol['timeout_seconds'], trust_env=False,
                       transport=httpx2.HTTPTransport(retries=0)) as client:
        for scheduled in requests:
            model, arm = scheduled['model'], scheduled['arm']
            directory = output / model
            directory.mkdir(exist_ok=True)
            raw_request = encoded(scheduled['request'])
            (directory / f'{arm}.request.json').write_bytes(raw_request)
            row = {'stage': 'LIVE INFERENCE', 'arm': arm, 'requested_model': model,
                   'started_at': datetime.now(timezone.utc).isoformat(),
                   'request_sha256': sha(raw_request)}
            print(f'LIVE {model} {arm}', flush=True)
            try:
                response = client.post(protocol['endpoint'], content=raw_request,
                                       headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
                raw = response.content
                (directory / f'{arm}.response.raw.json').write_bytes(raw)
                headers = {k: response.headers[k] for k in ('x-request-id', 'date', 'content-type', 'openai-processing-ms') if k in response.headers}
                save(directory / f'{arm}.response.headers.json', headers)
                row.update({'http_status': response.status_code, 'request_id': headers.get('x-request-id'),
                            'raw_response_sha256': sha(raw)})
                data = response.json()
                choices = data.get('choices', [])
                message = choices[0].get('message', {}) if choices else {}
                content = message.get('content')
                reason = choices[0].get('finish_reason') if choices else None
                other = {k: v for k, v in message.items()
                         if k not in {'role', 'content', 'refusal', 'annotations'} and v not in (None, [], {})}
                no_actions = not message.get('refusal') and not other
                common = response.is_success and len(choices) == 1 and message.get('role') == 'assistant' and no_actions
                passed = (common and type(content) is str and content == '' and reason == 'stop') if arm == 'null' else (common and type(content) is str and len(content) > 0)
                row.update({'returned_model': data.get('model'), 'response_id': data.get('id'),
                            'usage': data.get('usage'), 'message': message, 'finish_reason': reason,
                            'content_key_present': 'content' in message, 'content_is_string': type(content) is str,
                            'visible_utf8_bytes': len(content.encode('utf-8')) if type(content) is str else None,
                            'content_utf8_hex': content.encode('utf-8').hex() if type(content) is str else None,
                            'no_refusal_or_action': no_actions, 'passed': bool(passed)})
            except Exception as error:
                row.update({'passed': False, 'exception': type(error).__name__,
                            'error': str(error).replace(key, '[REDACTED]')})
            outcomes[model].append(row)
            save(directory / 'qualification.json', {'stage': 'LIVE INFERENCE',
                 'qualified': len(outcomes[model]) == 2 and all(r['passed'] for r in outcomes[model]),
                 'requests_attempted': len(outcomes[model]), 'results': outcomes[model]})
            print(json.dumps(row, ensure_ascii=False), flush=True)
    qualified = all(len(rows) == 2 and all(r['passed'] for r in rows) for rows in outcomes.values())
    save(output / 'live-results.json', {'stage': 'LIVE INFERENCE', 'qualified': qualified,
         'requests_attempted': sum(len(rows) for rows in outcomes.values()), 'models': outcomes,
         'protocol_sha256': sha(protocol_raw), 'qualifier_sha256': sha(Path(__file__).read_bytes())})
    print('QUALIFIED' if qualified else 'NOT QUALIFIED; NO ADDITIONAL LIVE CALLS', flush=True)
    return 0 if qualified else 2

if __name__ == '__main__':
    sys.exit(main())
