#!/usr/bin/env python3
"""Fixture tests for quality gate v2 — real static analysis, achievable 10/10."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quality_gate import score_output, extract_code_blocks  # noqa: E402

CLEAN_PY = '''def load_config(path):
    """Load config; raises ValueError on unreadable input."""
    # handles missing files, empty paths and boundary conditions
    if not path:
        raise ValueError("path is required")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise ValueError(f"unreadable config: {e}") from e

print(load_config("settings.ini"))
'''

BARE_EXCEPT_PY = '''def risky(path):
    """Reads a file, swallowing every possible error."""
    # long enough and structured enough to pass the excellence threshold
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except:
        return None
'''

SYNTAX_ERROR_PY = '''def broken(
    this is not valid python at all
'''


class GateV2Tests(unittest.TestCase):
    def test_clean_python_scores_10(self):
        """The flagship claim: 10/10 must be achievable by real, clean code."""
        r = score_output(CLEAN_PY, "coding")
        self.assertEqual(r["score"], 10, f"issues: {r['issues']}")
        self.assertTrue(r["passed"])

    def test_print_no_longer_blocks(self):
        """v1 flagged any `print(` as dead code, making Python unpassable."""
        r = score_output(CLEAN_PY, "coding")
        self.assertNotIn("print", " ".join(r["issues"]).lower())

    def test_bare_except_fails(self):
        r = score_output(BARE_EXCEPT_PY, "coding")
        self.assertLess(r["score"], 10)
        self.assertTrue(any("bare" in i.lower() for i in r["issues"]))

    def test_syntax_error_fails(self):
        r = score_output(f"```python\n{SYNTAX_ERROR_PY}\n```\nsome explanation\n", "coding")
        self.assertFalse(r["passed"])
        self.assertTrue(any("syntax error" in i.lower() for i in r["issues"]))

    def test_mutable_default_fails(self):
        code = 'def f(items=[]):\n    """Accumulate; raises on None."""\n    if items is None:\n        raise ValueError("no")\n    return items\n'
        r = score_output(code, "coding")
        self.assertTrue(any("mutable default" in i.lower() for i in r["issues"]))

    def test_keyword_stuffing_no_longer_passes(self):
        """v1 passed prose containing the words 'try' and 'null'. v2 requires
        actual guards in actual code."""
        code = ("def io_task(p):\n"
                "    # try to be careful about null and empty edge cases\n"
                "    data = open(p).read()\n"
                "    data2 = open(p).read()\n"
                "    return data + data2\n")
        r = score_output(code, "coding")
        self.assertFalse(r["passed"])

    def test_extract_fenced_blocks(self):
        text = "Here:\n```python\nx = 1\n```\nand\n```js\nconst y = 2;\n```"
        blocks = extract_code_blocks(text)
        self.assertEqual([lang for lang, _ in blocks], ["python", "js"])

    def test_prose_only_architecture_unaffected(self):
        prose = ("The design uses retries with exponential backoff and a circuit breaker "
                 "for graceful degradation on timeout.\nIt scales horizontally with "
                 "partitioned shards behind a load balancer to avoid bottlenecks.\n"
                 "The main tradeoff is cost; however, the failover redundancy justifies it.\n"
                 "Edge conditions like empty queues are handled by the dead letter queue.")
        r = score_output(prose, "architecture")
        self.assertEqual(r["score"], 10, f"issues: {r['issues']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
