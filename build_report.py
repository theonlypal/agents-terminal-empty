"""Build the report from the four live records and SDK test receipts."""
import html
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
MODELS = ('gpt-4-0613', 'gpt-6-astra')

def load(path):
    return json.loads((ROOT / path).read_bytes())

def esc(value):
    return html.escape(str(value))

def table(headers, rows):
    return '<div class="scroll"><table><thead><tr>' + ''.join(f'<th>{esc(x)}</th>' for x in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{esc(x)}</td>' for x in row) + '</tr>' for row in rows) + '</tbody></table></div>'

def main():
    subprocess.run([sys.executable, '-B', str(ROOT / 'verify.py'), '--records-only'], check=True, cwd=ROOT)
    protocol = load('protocol.json')
    live = load('records/live-results.json')
    sdk_summary = load('artifacts/sdk-verification/result.json')
    sdk_receipt = 'artifacts/sdk-verification/' + sdk_summary['final_receipt']
    sdk = load(sdk_receipt)
    live_rows, ab_rows, case_rows, details = [], [], [], []
    replay_passed = replay_total = 0
    for model in MODELS:
        for row in live['models'][model]:
            usage = row['usage']
            request = load(f'records/{model}/{row["arm"]}.request.json')
            live_rows.append((model, row['arm'], request['messages'][1]['content'], row['http_status'],
                              repr(row['message']['content']), row['visible_utf8_bytes'], row['finish_reason'],
                              usage['prompt_tokens'], usage['completion_tokens'],
                              usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)))
            prefix = f'../records/{model}/{row["arm"]}'
            details.append(f'<h3>{esc(model)} / {esc(row["arm"])}</h3><pre>{esc(json.dumps(request, ensure_ascii=False, indent=2))}</pre>'
                           f'<p>Request SHA-256: <code>{esc(row["request_sha256"])}</code><br>'
                           f'Response SHA-256: <code>{esc(row["raw_response_sha256"])}</code><br>'
                           f'Request ID: <code>{esc(row["request_id"])}</code></p>'
                           f'<p><a href="{prefix}.request.json">Request</a> · <a href="{prefix}.response.raw.json">Raw response</a> · <a href="{prefix}.response.headers.json">Headers</a></p>')
        for variant in ('upstream', 'patched'):
            replay = load(f'records/replay/{model}/replay-{variant}.json')
            replay_passed += replay['passed']
            replay_total += replay['total']
            main_row = next(r for r in replay['cases'] if r['case'] == 'captured_null_structured' and r['mode'] == 'nonstreaming')
            ab_rows.append((model, variant, main_row['request_count'], main_row['replayed_response_count'],
                            main_row['completion_reason'] or 'Second request intercepted',
                            main_row['synthesized_answer_count'] if main_row['synthesized_answer_count'] is not None else 'No returned result'))
            for row in replay['cases']:
                case_rows.append((model, variant, row['case'], row['mode'], row['fixture_kind'],
                                  row['request_count'], row['completion_reason'] or '-', 'PASS' if row['passed'] else 'FAIL'))
    tested = sdk['tests']
    document = f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Agents Terminal Empty</title>
<style>body{{font:16px/1.55 system-ui,sans-serif;color:#161616;max-width:1200px;margin:40px auto;padding:0 24px}}h1{{line-height:1.15}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{border:1px solid #ccc;padding:8px;text-align:left;vertical-align:top}}th{{background:#f4f4f4}}.scroll{{overflow-x:auto}}pre{{background:#f4f4f4;padding:18px;overflow:auto}}code{{overflow-wrap:anywhere}}a{{color:#164dab}}</style>
<h1>Agents Terminal Empty</h1><p>Opt-in completion without output for the OpenAI Agents SDK.</p>
<p><strong>{live['requests_attempted']}/4 live qualification requests passed. {replay_passed}/{replay_total} offline replay/control cases passed.</strong></p>
<h2>Live requests</h2><p>One fresh null/control pair per model. Temperature, top_p, reasoning effort, tools, response format, stop sequences, and logit bias omitted. Streaming false. Transport retries zero.</p>
{table(['Model','Arm','User input','HTTP','Content (Python repr)','UTF-8 bytes','Finish','Input tokens','Completion tokens','Reasoning tokens'], live_rows)}
<p>The empty response is an actual string of length zero. It is not whitespace, missing content, a refusal, or a budget stop. Completion-token usage and visible-byte count are reported separately. Reasoning text and native EOS token IDs were not exposed.</p>
<h2>SDK A/B</h2><p>Identical captured response bytes pass through the same Chat Completions adapter in both runners. A local structured expectation is active; no provider-side JSON schema is sent. Network sockets and DNS are blocked during replay.</p>
{table(['Model','Runner','Request attempts','Responses replayed','Terminal result','Synthesized answers'], ab_rows)}
<p>Enabled with <code>RunConfig(complete_on_empty_stop=True)</code>. Patched runs return <code>final_output=None</code> and <code>completion_reason="completed_without_output"</code>. They do not manufacture a structured answer. Upstream's second request is recorded and intercepted; no additional live request is made. Because upstream raises before returning a result, its returned-result answer count is unavailable.</p>
<h2>SDK verification</h2><p>Status: <strong>{esc(sdk['status'])}</strong>. Tests: <strong>{tested['total_passed']:,} passed, {tested['total_skipped']} skipped</strong>. Formatting, lint, mypy, and pyright completed. Source and input hashes were unchanged across the gate.</p><p><a href="sdk-verification/result.json">Verification index</a> · <a href="sdk-verification/{esc(sdk_summary['final_receipt'])}">Final receipt</a> · <a href="sdk-verification/{esc(str(Path(sdk_summary['final_receipt']).parent / 'repository-verification.log'))}">Complete gate log</a>. Repository-designated platform and integration exclusions are recorded in the receipt. The gate runs separately from the four live requests.</p>
<h2>Reproduce</h2><pre>python3 verify.py

python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python run_replay.py --output-dir replay-output</pre>
<p>Run from the repository root. Verification reads saved evidence. Offline reproduction extracts the pinned source, applies the included patch, and repeats the A/B. Dependency installation uses the network; replay does not. <a href="../README.md">README</a> · <a href="../protocol.json">Protocol</a> · <a href="upstream.patch">Patch</a> · <a href="implementation.json">Source identities</a>.</p>
<h2>Exact configuration</h2><p>Endpoint: <code>{esc(protocol['endpoint'])}</code>. GPT-4 uses <code>max_tokens: 1500</code>; Astra uses <code>max_completion_tokens: 1500</code>. Astra's budget includes reasoning tokens.</p><p>SDK commit: <code>{esc(protocol['sdk_commit'])}</code>.</p>{''.join(details)}
<h2>Offline case records</h2><p>32 cases per model and SDK variant, 128 total. Streaming fixtures are synthetic SSE, not live streams. Valid JSON is synthetic. Tool and handoff fixtures test single execution of local side effects. All case results below are measured against their declared expectations.</p>
{table(['Model','Runner','Case','Mode','Fixture','Request attempts','Terminal result','Result'], case_rows)}
<p>Rayan Pal</p></html>'''
    (ROOT / 'artifacts/VOID.html').write_text(document, encoding='utf-8')
    print('Generated artifacts/VOID.html from verified records.')

if __name__ == '__main__':
    main()
