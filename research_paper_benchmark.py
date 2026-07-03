#!/usr/bin/env python3
"""Onklaud 5 Research Benchmarks — every number measured in this run.

Methodology notes (integrity):
- The "pattern coverage" task set is phrased close to the ladder's own pattern
  keys. It measures corpus coverage — an UPPER BOUND, not realistic usage.
- The "holdout" task set (shared with benchmark_full.py) is independently
  phrased and is the number to quote.
- Context compression is measured by running the pipeline's own
  compress_critiques() on a synthetic multi-round critique log — not asserted.
"""

import json
import sys
import subprocess
import time
import statistics
from pathlib import Path
from datetime import datetime

MY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MY_DIR.parent
sys.path.insert(0, str(MY_DIR))

from benchmark_full import HOLDOUT_TASKS, PATTERN_COVERAGE_TASKS  # noqa: E402


def _run_ladder_tasks(tasks):
    """(hit_rate over solvable, timings, per-task rows) for a task set."""
    hits = 0
    solvable = 0
    timings = []
    rows = []
    for task, category, lang, expected_solvable in tasks:
        cmd = [sys.executable, str(MY_DIR / "ponytail_ladder.py"), "--task", task, "--json"]
        if lang not in ("native", ""):
            cmd.extend(["--lang", lang])
        t0 = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                           encoding="utf-8", errors="replace", cwd=str(PROJECT_ROOT))
        elapsed = (time.perf_counter() - t0) * 1000
        timings.append(elapsed)
        try:
            data = json.loads(r.stdout.strip()) if r.stdout and r.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        found = data.get("found", False)
        if expected_solvable:
            solvable += 1
            if found:
                hits += 1
        rows.append({"task": task, "category": category, "language": lang,
                     "expected_solvable": expected_solvable, "found": found,
                     "level": data.get("level", "not_found"),
                     "confidence": data.get("confidence"),
                     "latency_ms": round(elapsed, 2)})
    rate = round(100 * hits / max(solvable, 1), 1)
    return {"hit_rate_pct": rate, "hits": hits, "solvable": solvable,
            "avg_latency_ms": round(statistics.mean(timings), 2),
            "p50_latency_ms": round(sorted(timings)[len(timings) // 2], 2),
            "results": rows}


def bench_ponytail():
    return {
        "pattern_coverage": _run_ladder_tasks(PATTERN_COVERAGE_TASKS),
        "holdout": _run_ladder_tasks(HOLDOUT_TASKS),
    }


def bench_syntax():
    py_files = list(MY_DIR.glob("*.py"))
    results = []
    passed = 0
    timings = []
    for f in py_files:
        t0 = time.perf_counter()
        r = subprocess.run(
            [sys.executable, str(MY_DIR / "fast_gate.py"), str(f), "--syntax-only"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace", cwd=str(PROJECT_ROOT))
        elapsed = (time.perf_counter() - t0) * 1000
        timings.append(elapsed)
        ok = "OK" in r.stdout
        if ok:
            passed += 1
        results.append({"file": f.name, "passed": ok, "latency_ms": round(elapsed, 2)})
    return {"total_files": len(py_files), "passed": passed,
            "pass_rate_pct": round(100 * passed / max(len(py_files), 1), 1),
            "avg_latency_ms": round(statistics.mean(timings), 2),
            "results": results}


def bench_precheck():
    tests = [
        ("retry", "Write an HTTP client with retry logic and circuit breaker"),
        ("type_safety", "Parse JSON response and cast to interface type"),
        ("cleanup", "Create a background worker that needs cleanup"),
        ("race_condition", "Implement a concurrent queue processor"),
        ("error_handling", "Write a function that fetches data from an API"),
        ("magic_numbers", "Configure API client with timeout settings"),
        ("validation", "Build a form validation system"),
        ("api_design", "Design a REST API endpoint"),
        ("unknown", "Write a simple hello world program"),
        ("retry", "Add retry with backoff to a database connection"),
    ]

    # Patterns actually on disk, not a hardcoded count
    immune_file = MY_DIR / "immune_memory.json"
    try:
        patterns_stored = len(json.loads(immune_file.read_text(encoding="utf-8")))
    except Exception:
        patterns_stored = 0

    results = []
    hits = 0
    timings = []
    for expected_category, task in tests:
        t0 = time.perf_counter()
        r = subprocess.run(
            [sys.executable, str(MY_DIR / "pre_check.py"), "--task", task, "--json"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace", cwd=str(PROJECT_ROOT))
        elapsed = (time.perf_counter() - t0) * 1000
        timings.append(elapsed)
        try:
            data = json.loads(r.stdout.strip()) if r.stdout and r.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        categories = [w["category"] for w in data.get("warnings", [])]
        matched = expected_category in categories
        if matched:
            hits += 1
        results.append({"task": task[:60], "expected": expected_category,
                        "matched": matched, "warnings": categories,
                        "latency_ms": round(elapsed, 2)})

    return {"total_tests": len(tests), "hits": hits,
            "detection_rate_pct": round(100 * hits / max(len(tests), 1), 1),
            "avg_latency_ms": round(statistics.mean(timings), 2),
            "patterns_stored": patterns_stored,
            "results": results,
            "note": "Detection depends on patterns actually stored from prior runs; "
                    "a fresh clone has 0 patterns and 0% detection by design."}


def bench_context_compression():
    """Measure the pipeline's OWN critique compression on a synthetic
    3-round log — the thing that actually runs in solve mode."""
    from council import compress_critiques
    critiques = [
        {"round": i,
         "critique": ("The implementation misses the connection-timeout edge case and "
                      "does not release the socket on failure. " * 12),
         "issues": [f"round-{i}-issue-{j}: unhandled timeout on retry path" for j in range(8)]}
        for i in range(1, 4)
    ]
    full_len = len(json.dumps(critiques, ensure_ascii=False))
    text, saved = compress_critiques(critiques)
    return {"uncompressed_chars": full_len,
            "compressed_chars": len(text),
            "reduction_pct": round(100 * saved / max(full_len, 1), 1),
            "note": "Measured on a synthetic 3-round critique log via council.compress_critiques."}


def bench_integration():
    t0 = time.perf_counter()
    r = subprocess.run(
        [sys.executable, str(MY_DIR / "test_pipeline.py")],
        capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace", cwd=str(PROJECT_ROOT))
    elapsed = (time.perf_counter() - t0) * 1000
    output = r.stdout + r.stderr
    passed = output.count("[PASS]")
    failed = output.count("[FAIL]")
    warnings = output.count("[WARN]")
    total = passed + failed + warnings
    return {"total_tests": total, "passed": passed, "failed": failed,
            "warnings": warnings,
            "pass_rate_pct": round(100 * passed / max(total, 1), 1),
            "total_time_ms": round(elapsed, 2)}


def generate_paper(pony, syntax, precheck, compression, integration):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cov = pony["pattern_coverage"]
    hold = pony["holdout"]

    paper = f"""
================================================================================
  ONKLAUD 5: A MULTI-MODEL VERIFICATION PIPELINE FOR CODE QUALITY
  Measured Benchmarks — Generated: {now}
================================================================================

ABSTRACT
--------
Onklaud 5 is a code-quality pipeline that orchestrates independent models
(Kimi K2.7 Code, GLM 5.2) plus rule-based offline layers (Ponytail ladder,
AST quality gate, immune memory) through generation, dual review, revision,
and arbitration. This report presents measured benchmarks of those layers.
All numbers come from this execution run.

INTEGRITY NOTE
--------------
Two ladder task sets are reported separately and must not be conflated:
- PATTERN COVERAGE: tasks phrased near the corpus's own keys. Upper bound.
- HOLDOUT: independently phrased tasks. This is the realistic number.
Earlier versions of this document reported only the coverage-style number;
that inflated the ladder's apparent capability.

1. RESULTS
----------

1.1 Ponytail Ladder
  Pattern coverage (upper bound): {cov['hit_rate_pct']}% ({cov['hits']}/{cov['solvable']}), p50 {cov['p50_latency_ms']}ms
  Holdout (realistic):            {hold['hit_rate_pct']}% ({hold['hits']}/{hold['solvable']}), p50 {hold['p50_latency_ms']}ms

  Resolved tasks cost 0 API calls and 0 tokens. Unresolved tasks proceed to
  model generation, with near-miss patterns passed along as hints.

1.2 Syntax Gate
  {syntax['passed']}/{syntax['total_files']} files pass ({syntax['pass_rate_pct']}%), {syntax['avg_latency_ms']}ms avg per file.

1.3 Immune Pre-Check
  Detection rate: {precheck['detection_rate_pct']}% ({precheck['hits']}/{precheck['total_tests']}) against {precheck['patterns_stored']} stored patterns.
  Note: {precheck['note']}

1.4 Context Compression (measured, replaces the former unimplemented
    "Headroom 60-95%" claim)
  Synthetic 3-round critique log: {compression['uncompressed_chars']} -> {compression['compressed_chars']} chars
  ({compression['reduction_pct']}% reduction). {compression['note']}

1.5 Pipeline Integration
  {integration['passed']}/{integration['total_tests']} tests pass ({integration['pass_rate_pct']}%),
  {integration['failed']} failed, {integration['warnings']} warnings (API-key-dependent tests skip without a key).

2. DISCUSSION
-------------
Cross-model review rests on ensemble reasoning: models with different training
lineages have different blind spots, so agreement raises confidence and
disagreement localizes risk. That argument is theoretical until measured:
this pipeline has NOT yet been evaluated on HumanEval, SWE-bench, or any
external benchmark, and no claim of parity with frontier models is made here.

3. LIMITATIONS AND FUTURE WORK
------------------------------
- The dual-review accuracy gain is unmeasured; an external-benchmark run
  (HumanEval subset with and without dual review) is the necessary next step.
- Third-party model scores are not cited in this document because we have not
  independently verified any of them.
- Holdout hit rate depends on corpus growth and is expected to move; re-run
  this script for current numbers rather than quoting stale ones.

================================================================================
  DATA (JSON): onklaud-5/research_benchmarks.json
  Generated:   {now}
================================================================================
"""
    return paper


def main():
    print("=" * 60)
    print("  ONKLAUD 5 RESEARCH BENCHMARKS -- ALL MEASURED")
    print("=" * 60)

    print("\n[1/5] Ponytail ladder (coverage + holdout sets)...")
    pony = bench_ponytail()
    print(f"  Coverage: {pony['pattern_coverage']['hit_rate_pct']}% | "
          f"Holdout: {pony['holdout']['hit_rate_pct']}%")

    print("\n[2/5] Syntax gate...")
    syntax = bench_syntax()
    print(f"  Pass rate: {syntax['pass_rate_pct']}%")

    print("\n[3/5] Immune pre-check...")
    precheck = bench_precheck()
    print(f"  Detection: {precheck['detection_rate_pct']}% ({precheck['patterns_stored']} patterns stored)")

    print("\n[4/5] Context compression (measured)...")
    compression = bench_context_compression()
    print(f"  Reduction: {compression['reduction_pct']}%")

    print("\n[5/5] Pipeline integration...")
    integ = bench_integration()
    print(f"  Pass rate: {integ['pass_rate_pct']}% ({integ['passed']}/{integ['total_tests']})")

    paper = generate_paper(pony, syntax, precheck, compression, integ)

    (MY_DIR / "RESEARCH_PAPER.txt").write_text(paper, encoding="utf-8")
    json.dump({
        "datetime": datetime.now().isoformat(),
        "ponytail_ladder": {
            "pattern_coverage": {k: v for k, v in pony["pattern_coverage"].items() if k != "results"},
            "holdout": {k: v for k, v in pony["holdout"].items() if k != "results"},
        },
        "ponytail_details": pony["holdout"]["results"],
        "syntax_gate": syntax,
        "immune_precheck": precheck,
        "context_compression": compression,
        "pipeline_integration": integ,
    }, open(MY_DIR / "research_benchmarks.json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    print(paper)
    print("\nReports saved: RESEARCH_PAPER.txt, research_benchmarks.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
