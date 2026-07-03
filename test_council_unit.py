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


class ConfigTests(unittest.TestCase):
    def test_models_come_from_config(self):
        """config.yaml is the source of truth — model ids must match it."""
        cfg = council.CONFIG
        if not cfg:
            self.skipTest("PyYAML not installed")
        ids = {m.get("model_id") for m in cfg.get("models", [])}
        self.assertIn(council.KIMI_MODEL, ids)
        self.assertIn(council.GLM_MODEL, ids)

    def test_gate_threshold_from_config(self):
        self.assertEqual(council.GATE_THRESHOLD,
                         council.CONFIG.get("council", {}).get("stages", {})
                         .get("quality_gate", {}).get("threshold", 10))


class GateDomainMappingTests(unittest.TestCase):
    def test_code_maps_to_coding_gates(self):
        """Council says 'code', the gate registry says 'coding' — the coding
        gates must actually run."""
        result = council.run_quality_gate("short", "code")
        names = {g["name"] for g in result.get("gates", [])}
        self.assertIn("DeadCode", names)


class CompressCritiquesTests(unittest.TestCase):
    def test_compression_saves_chars_and_keeps_latest(self):
        critiques = [
            {"round": i, "critique": "blah " * 200, "issues": [f"issue-{i}-{j}" for j in range(8)]}
            for i in range(1, 4)
        ]
        text, saved = council.compress_critiques(critiques)
        self.assertGreater(saved, 0)
        self.assertIn("(latest)", text)
        self.assertIn("Round 1 issues:", text)


class ImmuneMemoryTests(unittest.TestCase):
    def test_hints_never_raise(self):
        self.assertIsInstance(council.immune_hints("implement a retry loop"), str)

    def test_categorize(self):
        self.assertEqual(council._categorize_issues("missing retry backoff on 429"), "retry")


class SolveTests(unittest.TestCase):
    """End-to-end solve mode against a scripted fake OpenRouter."""

    # Passes every coding gate: try/except, edge-case words, no print/TODO, structured.
    GOOD_DRAFT = (
        "def load(path):\n"
        "    # handles null, empty and boundary edge cases; invalid input raises\n"
        "    try:\n"
        "        with open(path, encoding='utf-8') as f:\n"
        "            return f.read()\n"
        "    except OSError as e:\n"
        "        raise ValueError(f'unreadable: {e}') from e\n"
    )

    def _fake_call(self, calls):
        good_draft = self.GOOD_DRAFT

        def fake(model, prompt, max_tokens=2000, api_key=None, retries=2, reasoning=None):
            calls.append(prompt.split("\n", 1)[0][:60])
            if "architecture designer" in prompt:
                return json.dumps({"passed": True, "score": 8, "approach": "keep it simple",
                                   "critique": "design ok", "issues": []})
            if "primary code generator" in prompt:
                return "draft-v1 = broken"
            if "revising a draft" in prompt:
                return good_draft
            if "Review this code" in prompt:
                # Reviews of v1 fail; reviews of the revised draft pass
                if "draft-v1" in prompt:
                    return json.dumps({"passed": False, "score": 4,
                                       "critique": "broken", "issues": ["no error handling"]})
                return json.dumps({"passed": True, "score": 10, "critique": "solid", "issues": []})
            raise AssertionError(f"unexpected prompt: {prompt[:80]}")

        return fake

    def test_solve_generates_revises_and_passes(self):
        import argparse
        calls = []
        args = argparse.Namespace(prompt="build a robust file loader", type="code",
                                  project_dir=None, json_output=True, output=None,
                                  state_dir=None)
        with mock.patch.object(council, "call_openrouter", self._fake_call(calls)), \
             mock.patch.object(council, "record_score"):
            with self.assertRaises(SystemExit) as ctx:
                council.cmd_solve(args)
        self.assertEqual(ctx.exception.code, 0)
        # Generation happened, revision happened
        self.assertTrue(any("primary code generator" in c for c in calls))
        self.assertTrue(any("revising a draft" in c for c in calls))

    def test_solve_ponytail_hit_needs_no_api(self):
        import argparse
        args = argparse.Namespace(prompt="read a JSON config file in Python", type="code",
                                  project_dir=None, json_output=True, output=None,
                                  state_dir=None)
        def boom(*a, **kw):
            raise AssertionError("API must not be called on a ponytail hit")
        with mock.patch.object(council, "call_openrouter", boom), \
             mock.patch.object(council, "record_score"):
            with self.assertRaises(SystemExit) as ctx:
                council.cmd_solve(args)
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
