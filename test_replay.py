"""Offline harness contract checks; no SDK execution and no provider requests."""

import ast
import hashlib
from pathlib import Path
import unittest

from run_replay import child_environment, request_contract

ROOT = Path(__file__).resolve().parent


def predicate_identities(source):
    tree = ast.parse(source)
    case = next(node for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run_case')
    def assignment(node, name):
        return (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))
    start = next(index for index, node in enumerate(case.body) if assignment(node, 'structured'))
    end = next(index for index, node in enumerate(case.body) if assignment(node, 'processor'))
    checks = [node for node in case.body if assignment(node, 'checks') or (
        isinstance(node, ast.If) and any(
            isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
            and isinstance(item.func.value, ast.Name) and item.func.value.id == 'checks'
            and item.func.attr == 'append' for item in ast.walk(node)))]
    return {label: hashlib.sha256(ast.dump(ast.Module(body=nodes, type_ignores=[]),
                                         include_attributes=False).encode()).hexdigest()
            for label, nodes in [('expected_outcomes', case.body[start:end]), ('checks', checks)]}


class ReplayContractTests(unittest.TestCase):
    def test_original_case_predicates_unchanged(self):
        # Fingerprints from the source harness identified in replay-plan.json.
        self.assertEqual(predicate_identities((ROOT / 'replay.py').read_text()), {
            'expected_outcomes': 'a55ee1cf27fc806432b20f4ff39e7cabf636c42bb1bfc86de5b78ce1cbf66582',
            'checks': '359347ef13b2f4909555156357ef1fe529f9497f56e5993db7eb9b9c2470844e',
        })

    def test_captured_budget_contracts(self):
        for model, field in [('gpt-4-0613', 'max_tokens'), ('gpt-6-astra', 'max_completion_tokens')]:
            with self.subTest(model=model):
                self.assertEqual(request_contract({'model': model, field: 1500}),
                                 {'model': model, 'budget_field': field, 'budget': 1500})

    def test_incompatible_or_ambiguous_budgets_fail(self):
        requests = [
            {'model': 'gpt-4-0613', 'max_completion_tokens': 1500},
            {'model': 'gpt-6-astra', 'max_tokens': 1500},
            {'model': 'gpt-6-astra', 'max_tokens': 1500, 'max_completion_tokens': 1500},
            {'model': 'gpt-4-0613', 'max_tokens': 1501},
            {'model': 'gpt-4-0613'},
        ]
        for request in requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                request_contract(request)

    def test_children_do_not_receive_credential_variables(self):
        environment = child_environment(ROOT)
        self.assertFalse(any(any(term in key.upper() for term in ('API_KEY', 'TOKEN', 'SECRET'))
                             for key in environment))
        self.assertEqual(environment['PYTHONPATH'], str(ROOT / 'src'))


if __name__ == '__main__':
    unittest.main()
