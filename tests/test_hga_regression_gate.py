# Copyright 2026 Project contributors
# ruff: noqa: INP001, PT009, PT027, FBT003
"""A baseline exception cannot conceal new failures or a changed test inventory."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RegressionGateTests(unittest.TestCase):
    """Exercise fail-closed comparison of the copied upstream tests."""

    def setUp(self) -> None:
        path = ROOT / "scripts/ha_hga_regression_check.py"
        self.assertTrue(path.is_file(), "A real baseline comparison is required")
        spec = importlib.util.spec_from_file_location("hga_regression_check", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_only_identical_nonempty_results_are_accepted(self) -> None:
        baseline = {("suite", "a"): "passed", ("suite", "b"): "failure"}
        self.module.compare_results(baseline, dict(baseline))
        for altered in (
            {},
            {("suite", "a"): "passed"},
            {**baseline, ("suite", "c"): "failure"},
            {**baseline, ("suite", "a"): "failure"},
            {**baseline, ("suite", "b"): "skipped"},
        ):
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                self.module.compare_results(baseline, altered)
        with self.assertRaises(ValueError):
            self.module.compare_results({}, {})

    def test_report_rejects_errors_duplicate_ids_and_no_tests(self) -> None:
        for content in (
            "<testsuites/>",
            '<testsuite><testcase classname="s" name="a"><error/></testcase></testsuite>',
            '<testsuite><testcase classname="s" name="a"/><testcase classname="s" name="a"/></testsuite>',
        ):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.xml"
                path.write_text(content)
                with self.assertRaises(ValueError):
                    self.module.read_results(path)

    def test_source_change_disables_baseline_failure_tolerance(self) -> None:
        self.module.require_unchanged_source("abc", "abc", "abc", False)
        for values in (
            ("abc", "def", "def", False),
            ("abc", "abc", "def", False),
            ("abc", "abc", "abc", True),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.module.require_unchanged_source(*values)


if __name__ == "__main__":
    unittest.main()
