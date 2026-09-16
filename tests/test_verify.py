"""Adversarial checks on temporary fixtures; never modify captured evidence."""

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'verify.py'
SPEC = importlib.util.spec_from_file_location('packet_verify', MODULE)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class VerifierTests(unittest.TestCase):
    def captured_case(self, variant):
        root = MODULE.parent
        model = 'gpt-4-0613'
        report = verify.load(root, f'records/replay/{model}/replay-{variant}.json')
        captured = {arm: (root / f'records/{model}/{arm}.response.raw.json').read_bytes()
                    for arm in ('null', 'control')}
        return report['cases'][0], model, captured

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON key'):
            verify.decode(b'{"passed":false,"passed":true}')

    def test_wrong_budget_field_rejected(self):
        request = verify.expected_request('gpt-6-astra', 'null')
        request['max_tokens'] = request.pop('max_completion_tokens')
        with self.assertRaisesRegex(ValueError, 'token budget'):
            verify.check_budget(request, 'gpt-6-astra')

    def test_bool_budget_rejected(self):
        request = verify.expected_request('gpt-4-0613', 'null')
        request['max_tokens'] = True
        with self.assertRaises(ValueError):
            verify.check_budget(request, 'gpt-4-0613')

    def test_usage_count_mutation_rejected(self):
        with self.assertRaisesRegex(ValueError, 'total mismatch'):
            verify.check_usage({'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 9})

    def test_custom_captured_control_is_used(self):
        body = verify.synthetic('gpt-6-astra', 'A different response.\nΚαλημέρα!')
        contract = verify.case_contract('gpt-6-astra', 'captured_control_plain', 'patched',
                                        {'null': verify.synthetic('gpt-6-astra', ''), 'control': body})
        self.assertEqual(contract['output'], 'A different response.\nΚαλημέρα!')

    def test_patch_context_mutation_rejected(self):
        patch = b'diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n'
        result, _ = verify.apply_unified_patch({'a.py': b'old\n'}, patch)
        self.assertEqual(result['a.py'], b'new\n')
        with self.assertRaisesRegex(ValueError, 'context mismatch'):
            verify.apply_unified_patch({'a.py': b'forged\n'}, patch)

    def test_patch_hunk_count_mutation_rejected(self):
        patch = b'diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1 @@\n-old\n+new\n'
        with self.assertRaisesRegex(ValueError, 'count mismatch'):
            verify.apply_unified_patch({'a.py': b'old\n'}, patch)

    def test_manifest_requires_complete_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'one.txt').write_bytes(b'one')
            (root / 'SHA256SUMS').write_text(verify.sha(b'one') + '  one.txt\n')
            self.assertEqual(verify.verify_manifest(root), 1)
            (root / 'unlisted.json').write_text(json.dumps({'passed': True}))
            with self.assertRaisesRegex(ValueError, 'coverage'):
                verify.verify_manifest(root)

    def test_manifest_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'SHA256SUMS').write_text('0' * 64 + '  ../outside\n')
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                verify.verify_manifest(root)

    def test_removed_check_rejected(self):
        with self.assertRaisesRegex(ValueError, 'missing or failed'):
            verify.check_named([{'name': 'one', 'passed': True}], ['one', 'two'], 'fixture')

    def test_unavailable_output_cannot_be_reported_as_zero(self):
        row, model, captured = self.captured_case('upstream')
        verify.verify_case(row, model, 'upstream', captured)
        self.assertIs(row['synthesized_answer_count'], None)
        row['synthesized_answer_count'] = 0
        with self.assertRaisesRegex(ValueError, 'unavailable count mismatch'):
            verify.verify_case(row, model, 'upstream', captured)

    def test_forged_terminal_answer_rejected(self):
        row, model, captured = self.captured_case('patched')
        row['new_items'].append({'raw_item': {'role': 'assistant', 'content': 'Invented answer.'}})
        with self.assertRaisesRegex(ValueError, 'invented answer'):
            verify.verify_case(row, model, 'patched', captured)

    def test_unblocked_upstream_request_rejected(self):
        row, model, captured = self.captured_case('upstream')
        del row['requests'][1]['blocked']
        with self.assertRaisesRegex(ValueError, 'not blocked'):
            verify.verify_case(row, model, 'upstream', captured)

    def test_request_body_mutation_rejected_without_trusting_receipt(self):
        row, model, captured = self.captured_case('patched')
        row['requests'][0]['body']['temperature'] = 1
        with self.assertRaisesRegex(ValueError, 'request wire'):
            verify.verify_case(row, model, 'patched', captured)

    def test_records_verify_after_relocation_without_sdk_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = MODULE.parent
            for name in ('protocol.json', 'qualify.py', 'replay.py', 'run_replay.py',
                         'artifacts/implementation.json', 'artifacts/upstream.patch',
                         'vendor/sdk-upstream.tar.gz'):
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, destination)
            shutil.copytree(source / 'records', root / 'records', ignore=shutil.ignore_patterns('sdk'))
            self.assertFalse((root / 'records/replay/sdk').exists())
            qualifications = verify.verify_live(root)
            implementation, maps, _ = verify.verify_implementation(root)
            reports = verify.verify_replays(root, qualifications, implementation, maps)
            self.assertEqual(sum(report['total'] for report in reports.values()), 128)


if __name__ == '__main__':
    unittest.main()
