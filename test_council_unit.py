#!/usr/bin/env python3
"""Unit tests for council.py internals — no network, no API key needed."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import council  # noqa: E402


def _fake_response(payload):
    body = json.dumps(payload).encode("utf-8")
    resp = mock.Mock()
    resp.read.return_value = body
    return resp


class CallOpenRouterTests(unittest.TestCase):
    def setUp(self):
        council.OR_KEY = "primary-key"
        council.OR_KEY_BACKUP = "backup-key"

    def test_backup_key_used_when_primary_fails(self):
        """A 401 on the primary key must fail over to the backup key."""
        seen_keys = []

        def fake_urlopen(req, timeout=60):
            key = req.headers["Authorization"].split()[-1]
            seen_keys.append(key)
            if key == "primary-key":
                from urllib.error import HTTPError
                raise HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
            return _fake_response({
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })

        with mock.patch.object(council, "urlopen", fake_urlopen):
            out = council.call_openrouter("test/model", "hi", retries=1)
        self.assertEqual(out, "ok")
        self.assertIn("backup-key", seen_keys)

    def test_non_retryable_401_fails_fast(self):
        """401 must not be retried with backoff on the same key."""
        calls = []

        def fake_urlopen(req, timeout=60):
            calls.append(req.headers["Authorization"].split()[-1])
            from urllib.error import HTTPError
            raise HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

        council.OR_KEY_BACKUP = ""
        with mock.patch.object(council, "urlopen", fake_urlopen), \
             mock.patch.object(council.time, "sleep") as sleeper:
            out = council.call_openrouter("test/model", "hi", retries=2)
        self.assertIsNone(out)
        self.assertEqual(len(calls), 1)          # exactly one attempt
        sleeper.assert_not_called()              # and no backoff sleeps

    def test_usage_tracked_on_success(self):
        def fake_urlopen(req, timeout=60):
            return _fake_response({
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 2000},
            })

        before = council.USAGE["api_calls"]
        with mock.patch.object(council, "urlopen", fake_urlopen):
            council.call_openrouter(council.KIMI_MODEL, "hi")
        self.assertEqual(council.USAGE["api_calls"], before + 1)
        self.assertGreater(council.USAGE["cost_usd"], 0)


class QualityGateTests(unittest.TestCase):
    def test_large_draft_no_crash(self):
        """60k+ char drafts used to crash the Windows argv limit — now in-process."""
        big = ("word " * 20000) + "\n\n\nstructured enough\n"
        result = council.run_quality_gate(big, "general")
        self.assertIn("score", result)
        self.assertNotIn("_degraded", result)

    def test_degraded_gate_never_fabricates_pass(self):
        with mock.patch.dict(sys.modules, {"quality_gate": None}):
            result = council.run_quality_gate("text", "general")
        self.assertFalse(result["passed"])
        self.assertTrue(result.get("degraded"))
        self.assertEqual(result["score"], 0)


class DegradedResultTests(unittest.TestCase):
    def test_no_fabricated_score(self):
        r = council.degraded_result("test")
        self.assertFalse(r["passed"])
        self.assertTrue(r["degraded"])
        self.assertEqual(r["final_score"], 0)


class StateIsolationTests(unittest.TestCase):
    def test_different_prompts_different_dirs(self):
        council.init_state("task one")
        first = council.ROUND_FILE
        council.init_state("task two")
        second = council.ROUND_FILE
        self.assertNotEqual(first, second)

    def test_same_prompt_same_dir(self):
        council.init_state("same task")
        first = council.ROUND_FILE
        council.init_state("same task")
        self.assertEqual(first, council.ROUND_FILE)


class ParseReviewTests(unittest.TestCase):
    def test_truncated_array_salvage(self):
        """']}' closer variant: truncation right after a complete array element."""
        raw = '{"passed": false, "score": 4, "critique": "meh", "issues": ["a", "b"'
        result = council.parse_review(raw)
        self.assertEqual(result.get("score"), 4)

    def test_fenced_json(self):
        raw = 'Here you go:\n```json\n{"passed": true, "score": 10, "critique": "x", "issues": []}\n```'
        self.assertEqual(council.parse_review(raw)["score"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
