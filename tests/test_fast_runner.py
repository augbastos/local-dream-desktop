"""Checks for discovery coverage and failure propagation in the CI runner."""
from contextlib import redirect_stdout
import io
import subprocess
import unittest
from unittest.mock import patch

from tools import test_fast


class FastRunnerTests(unittest.TestCase):
    def fixture_failure(self):
        self.fail("intentional runner fixture failure")

    def test_nested_suites_preserve_every_id_once(self):
        cases = [FastRunnerTests("test_nested_suites_preserve_every_id_once"),
                 FastRunnerTests("test_child_failure_is_reported")]
        suite = unittest.TestSuite([cases[0], unittest.TestSuite([cases[1]])])
        self.assertEqual(test_fast.test_groups(suite),
                         {__name__: [case.id() for case in cases]})

    def test_child_failure_is_reported(self):
        module, count, elapsed, result = test_fast.run_module(
            (__name__, ["tests.test_fast_runner.FastRunnerTests.fixture_failure"]))
        self.assertEqual((module, count), (__name__, 1))
        self.assertGreater(elapsed, 0)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("intentional runner fixture failure", result.stdout)

    def test_main_collects_all_results_and_fails_on_nonzero_exit(self):
        groups = {"first": ["one"], "second": ["two"]}

        def run(item):
            module, ids = item
            return module, len(ids), 0.1, subprocess.CompletedProcess(
                [], int(module == "first"), stdout="fixture output\n")

        with patch.object(unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite()), \
                patch.object(test_fast, "test_groups", return_value=groups), \
                patch.object(test_fast, "run_module", side_effect=run) as child, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(test_fast.main(["--jobs", "2"]), 1)
        self.assertEqual(child.call_count, 2)
        self.assertIn("first: 1 tests", output.getvalue())
        self.assertIn("second: 1 tests", output.getvalue())
