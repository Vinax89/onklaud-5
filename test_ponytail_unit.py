#!/usr/bin/env python3
"""Ponytail Ladder v2 tests — rephrased tasks must match; misses must hint."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ponytail_ladder import run_ladder, normalize  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_stemming_collides_variants(self):
        self.assertEqual(normalize("parsing dates"), normalize("parse date"))

    def test_synonyms_canonicalize(self):
        self.assertEqual(normalize("load settings"), normalize("read config"))


class LadderV2Tests(unittest.TestCase):
    def _found(self, task, lang=None):
        result, code = run_ladder(task, None, lang)
        return result, code

    def test_original_phrasing_still_matches(self):
        r, code = self._found("read a JSON file in Python")
        self.assertTrue(r["found"])
        self.assertEqual(r["level"], "stdlib")

    def test_rephrased_task_matches(self):
        """v1's exact word-subset match failed on any rephrasing."""
        r, _ = self._found("load settings from a JSON configuration file")
        self.assertTrue(r["found"], r)
        self.assertIn("json", r["pattern_matched"])

    def test_plural_and_gerund_match(self):
        r, _ = self._found("parsing dates in Python")
        self.assertTrue(r["found"], r)

    def test_js_coverage_improved(self):
        for task in ["deduplicate a list in JavaScript",
                     "deep copy an object in node",
                     "debounce a search input in the browser"]:
            r, _ = self._found(task)
            self.assertTrue(r["found"], f"{task} -> {r}")

    def test_confidence_reported(self):
        r, _ = self._found("generate a random UUID in Python")
        self.assertEqual(r.get("confidence"), 1.0)

    def test_miss_returns_hints(self):
        r, code = self._found("build a custom distributed json replication engine in Python")
        self.assertFalse(r["found"])
        self.assertEqual(code, 1)
        self.assertTrue(len(r.get("hints", [])) >= 1, r)

    def test_true_miss_stays_a_miss(self):
        r, code = self._found("orchestrate quantum flux capacitors")
        self.assertFalse(r["found"])
        self.assertEqual(code, 1)

    def test_native_still_matches(self):
        r, _ = self._found("add a dark mode toggle")
        self.assertTrue(r["found"])
        self.assertEqual(r["level"], "native")


if __name__ == "__main__":
    unittest.main(verbosity=2)
