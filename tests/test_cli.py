import json
import subprocess
import sys
import unittest

class CommandLineTests(unittest.TestCase):
    def test_example_is_machine_readable(self):
        run = subprocess.run([sys.executable, "-m", "agent_trace_ledger", "examples/input.json"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIsInstance(json.loads(run.stdout), dict)

    def test_bad_json_has_clean_error(self):
        run = subprocess.run([sys.executable, "-m", "agent_trace_ledger", "-"], input="{broken", capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertNotIn("Traceback", run.stderr)

    def test_top_level_array_is_rejected(self):
        run = subprocess.run([sys.executable, "-m", "agent_trace_ledger", "-"], input="[]", capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
