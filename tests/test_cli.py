import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_trace_ledger


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *arguments, raw=None):
        return subprocess.run([sys.executable, '-m', 'agent_trace_ledger', *arguments], input=raw,
                              capture_output=True, text=not isinstance(raw, bytes))

    def test_otlp_file_and_pricing_are_machine_readable(self):
        run = self.run_cli('examples/otlp.json', '--rates', 'examples/rates.json')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['cost']['currency'], 'USD')

    def test_stdin_and_explicit_format(self):
        run = self.run_cli('-', '--format', 'normalized', raw='{"spans": []}')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['span_count'], 0)

    def test_bad_json_duplicate_keys_top_level_and_nonfinite_have_clean_errors(self):
        for text in ('{broken', '[]', '{"spans": [], "spans": []}', '{"spans": [], "x": NaN}'):
            with self.subTest(text=text):
                run = self.run_cli('-', raw=text)
                self.assertEqual(run.returncode, 2)
                self.assertIn('Invalid input:', run.stderr)
                self.assertNotIn('Traceback', run.stderr)
                self.assertEqual(run.stdout, '')

    def test_invalid_utf8_and_missing_file_have_clean_errors(self):
        run = self.run_cli('-', raw=b'\xff')
        self.assertEqual(run.returncode, 2)
        self.assertNotIn(b'Traceback', run.stderr)
        run = self.run_cli('does-not-exist.json')
        self.assertEqual(run.returncode, 2)
        self.assertNotIn('Traceback', run.stderr)

    def test_double_overflow_in_recognized_otlp_attribute_is_a_clean_input_error(self):
        data = json.loads(Path('examples/otlp.json').read_text())
        span = data['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        span['attributes'][0]['value'] = {'doubleValue': 10**400}
        run = self.run_cli('-', raw=json.dumps(data))
        self.assertEqual(run.returncode, 2)
        self.assertNotIn('Traceback', run.stderr)
        self.assertIn('finite double range', run.stderr)

    def test_failure_policy_preserves_report_and_distinguishes_input_error(self):
        for policy in ('graph', 'incomplete-cost', 'any'):
            with self.subTest(policy=policy):
                run = self.run_cli('examples/otlp.json', '--rates', 'examples/rates.json', '--fail-on', policy)
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertEqual(json.loads(run.stdout)['span_count'], 7)
        run = self.run_cli('examples/input.json', '--fail-on', 'graph')
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_limits_and_invalid_rates_use_input_error_status(self):
        for arguments in (('--max-spans', '1'), ('--max-findings', '0'), ('--rates', '-')):
            with self.subTest(arguments=arguments):
                run = self.run_cli('examples/otlp.json', *arguments)
                self.assertEqual(run.returncode, 2)
                self.assertNotIn('Traceback', run.stderr)

    def test_file_byte_limit_reads_only_one_byte_past_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'large.json'
            path.write_bytes(b' ' * 17)
            with patch.object(agent_trace_ledger, 'MAX_INPUT_BYTES', 16):
                with self.assertRaisesRegex(ValueError, 'exceeds 16 bytes'):
                    agent_trace_ledger._load_json(str(path))

    def test_version(self):
        run = self.run_cli('--version')
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout.strip(), '0.2.0')
