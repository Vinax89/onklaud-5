#!/usr/bin/env python3
"""
Onklaud 5 - Benchmark Suite v4
==============================
Measures what the pipeline actually does. Two task sets, reported separately:

- PATTERN_COVERAGE_TASKS: phrased close to the ladder's own pattern keys.
  This measures corpus coverage — an upper bound, NOT real-world performance.
- HOLDOUT_TASKS: independently phrased (the way people actually describe
  tasks). This is the honest hit-rate number.

v3 printed hardcoded "estimates" (~50x speedup, 60-95% compression) as if
measured, and its composite grade could not go below 'A'. All of that is gone:
every number below is computed in this run, and the grade can fail.
"""

import sys
import json
import time
import subprocess
from pathlib import Path
from datetime import datetime

MY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MY_DIR.parent

# === Set 1: pattern-coverage tasks (phrased near the corpus keys) ===
# Upper bound: verifies the corpus matches its own vocabulary.
PATTERN_COVERAGE_TASKS = [
    # (task, category, language, expected_solvable)
    ("Read and parse a JSON configuration file", "IO", "python", True),
    ("Make an HTTP GET request with timeout and retry", "Network", "python", True),
    ("Generate a random UUID v4", "Crypto", "python", True),
    ("Walk a directory recursively finding all .py files", "Filesystem", "python", True),
    ("Parse command-line arguments with --verbose flag", "CLI", "python", True),
    ("Create a ZIP archive from a list of files", "Compression", "python", True),
    ("Run an external command and capture stdout/stderr", "System", "python", True),
    ("Log messages with timestamps to stderr", "Logging", "python", True),
    ("Hash a string with SHA256", "Crypto", "python", True),
    ("Base64 encode binary data", "Encoding", "python", True),
    ("Sleep/delay execution for N seconds", "Utility", "python", True),
    ("Get environment variable with default value", "Config", "python", True),
    ("Find files matching a regex pattern", "Filesystem", "python", True),
    ("Merge two dictionaries with priority", "Data", "python", True),
    ("Benchmark execution time of a function", "Profiling", "python", True),
    ("Read a file synchronously", "IO", "js", True),
    ("Parse a URL and extract query parameters", "Web", "js", True),
    ("Create an HTTP server on port 3000", "Server", "js", True),
    ("Generate a random integer between 1 and 100", "Utility", "js", True),
    ("Deep clone an object", "Data", "js", True),
    ("Format a date as ISO string", "Date", "js", True),
    ("Create a temporary directory", "Filesystem", "js", True),
    ("Set a timeout that can be cancelled", "Async", "js", True),
    ("Read all lines from a file", "IO", "js", True),
    ("Check if a path exists (file or directory)", "Filesystem", "js", True),
    ("Add a dark mode toggle to the page", "CSS", "native", True),
    ("Create a responsive 3-column grid layout", "CSS", "native", True),
    ("Add a tooltip on hover", "HTML", "native", True),
    ("Create an expandable FAQ section", "HTML", "native", True),
    ("Add a number input with min/max validation", "HTML", "native", True),
    ("Implement a custom LRU cache with TTL expiry", "Algorithm", "python", False),
    ("Build a WebSocket chat server with rooms", "Network", "python", False),
    ("Create a state machine for order processing", "Design", "python", False),
    ("Implement a token bucket rate limiter", "Algorithm", "python", False),
    ("Build a recursive directory watcher with debounce", "Filesystem", "python", False),
]

# === Set 2: holdout tasks (independently phrased) ===
# Written the way people actually describe work, deliberately avoiding the
# corpus keys' vocabulary. This is the number that matters.
HOLDOUT_TASKS = [
    ("Turn a settings file written as JSON into a dict I can use", "IO", "python", True),
    ("Grab the contents of a webpage over HTTPS", "Network", "python", True),
    ("I need a unique identifier for each new record", "Crypto", "python", True),
    ("Locate every Python source file under a project tree", "Filesystem", "python", True),
    ("Let users pass a --verbose switch to my script", "CLI", "python", True),
    ("Bundle several files into one compressed archive", "Compression", "python", True),
    ("Launch another program and collect what it prints", "System", "python", True),
    ("Figure out how similar two strings are", "Text", "python", True),
    ("Turn '2026-07-03' into a datetime object", "Date", "python", True),
    ("Pause the program for three seconds", "Utility", "python", True),
    ("How many times does each word appear in this text", "Data", "python", True),
    ("Keep only the first occurrence of each item in a list", "Data", "python", True),
    ("Cache the results of an expensive function call", "Performance", "python", True),
    ("Store rows in a lightweight local database without a server", "Data", "python", True),
    ("Nicely formatted output of a nested dict for debugging", "Text", "python", True),
    ("Pull the text out of a file on disk in node", "IO", "js", True),
    ("What are the query parameters of this link", "Web", "js", True),
    ("Wait 500 ms inside an async function", "Async", "js", True),
    ("Copy an object so later mutations don't leak", "Data", "js", True),
    ("Show prices as US dollars", "I18n", "js", True),
    ("Only fire the search after the user stops typing", "UI", "js", True),
    ("Persist a small setting between page reloads", "Web", "js", True),
    ("Kick off several fetches and wait for all of them", "Async", "js", True),
    ("Run a shell command from a node script", "System", "js", True),
    ("Give every visitor a fresh random identifier", "Crypto", "js", True),
    ("Let the user pick a day from a calendar without a JS library", "HTML", "native", True),
    ("Keep the navigation bar visible while scrolling", "CSS", "native", True),
    ("Collapse and expand answers in a FAQ", "HTML", "native", True),
    ("Show how far along the upload is", "HTML", "native", True),
    ("Center a div horizontally and vertically", "CSS", "native", True),
    ("Implement a Raft consensus module", "Distributed", "python", False),
    ("Build a collaborative rich text editor with CRDTs", "Web", "js", False),
    ("Write a SQL query planner", "Database", "python", False),
    ("Train a small sentiment classifier", "ML", "python", False),
    ("Parse our proprietary binary telemetry format", "IO", "python", False),
]


def run_task_set(tasks, label):
    """Run the ladder over a task set. Every number here is measured."""
    results = {"label": label, "total": 0, "stdlib": 0, "native": 0, "existing_dep": 0,
               "not_found": 0, "hits_on_solvable": 0, "solvable_total": 0,
               "false_positives": 0, "time_ms": 0, "tasks": []}
    start = time.time()

    for task, category, lang, expected_solvable in tasks:
        cmd = [sys.executable, str(MY_DIR / "ponytail_ladder.py"), "--task", task, "--json"]
        if lang not in ("native", ""):
            cmd.extend(["--lang", lang])
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace", cwd=str(PROJECT_ROOT)
        )
        try:
            data = json.loads(r.stdout.strip()) if r.stdout and r.stdout.strip() else {"found": False}
        except json.JSONDecodeError:
            data = {"found": False}

        found = data.get("found", False)
        level = data.get("level", "not_found") if found else "not_found"
        results["total"] += 1
        results[level if level in ("stdlib", "native", "existing_dep") else "not_found"] += 1

        if expected_solvable:
            results["solvable_total"] += 1
            if found:
                results["hits_on_solvable"] += 1
        elif found:
            results["false_positives"] += 1

        results["tasks"].append({
            "task": task, "category": category, "language": lang,
            "expected_solvable": expected_solvable,
            "resolved": found, "level": level,
            "solution": data.get("solution", "")[:100],
        })

    results["time_ms"] = round((time.time() - start) * 1000, 1)
    results["hit_rate"] = round(100 * results["hits_on_solvable"] / max(results["solvable_total"], 1), 1)
    return results


def run_syntax_benchmark():
    """Syntax-check every onklaud-5 Python file. Deterministic."""
    py_files = list(MY_DIR.glob("*.py"))
    results = {"files": len(py_files), "passed": 0, "failed": 0, "time_ms": 0, "details": []}

    start = time.time()
    for f in py_files:
        r = subprocess.run(
            [sys.executable, str(MY_DIR / "fast_gate.py"), str(f), "--syntax-only"],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
            cwd=str(PROJECT_ROOT)
        )
        ok = "OK" in r.stdout
        results["passed" if ok else "failed"] += 1
        results["details"].append({"file": f.name, "passed": ok})

    results["time_ms"] = round((time.time() - start) * 1000, 1)
    return results


def run_council_metrics():
    """Collect council metrics without API calls."""
    r = subprocess.run(
        [sys.executable, str(MY_DIR / "council.py"), "status"],
        capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        cwd=str(PROJECT_ROOT)
    )
    return {"raw": r.stdout[:500], "council_available": "operational" in r.stdout.lower()}


def composite_grade(holdout, coverage, syntax):
    """Grade from measured numbers only. Weighted toward the holdout set
    because that's the realistic one. Can and should fail on regressions."""
    syntax_rate = 100 * syntax["passed"] / max(syntax["files"], 1)
    score = 0.5 * holdout["hit_rate"] + 0.2 * coverage["hit_rate"] + 0.3 * syntax_rate
    # False positives (claiming a stdlib one-liner covers Raft) are worse than misses
    score -= 5 * (holdout["false_positives"] + coverage["false_positives"])
    for letter, floor in (("A", 85), ("B", 70), ("C", 55), ("D", 40)):
        if score >= floor:
            return letter, round(score, 1)
    return "F", round(score, 1)


def generate_text_report(coverage, holdout, syntax, council, grade, grade_score):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "=" * 70,
        "  ONKLAUD 5 v4 - BENCHMARK REPORT (all numbers measured this run)",
        f"  Generated: {now}",
        "=" * 70,
        "",
        "  PONYTAIL - PATTERN COVERAGE (phrased near corpus keys; upper bound)",
        f"    Hit rate on solvable tasks: {coverage['hit_rate']}% "
        f"({coverage['hits_on_solvable']}/{coverage['solvable_total']})",
        f"    False positives on unsolvable tasks: {coverage['false_positives']}",
        f"    Time: {coverage['time_ms']}ms for {coverage['total']} tasks",
        "",
        "  PONYTAIL - HOLDOUT (independently phrased; the realistic number)",
        f"    Hit rate on solvable tasks: {holdout['hit_rate']}% "
        f"({holdout['hits_on_solvable']}/{holdout['solvable_total']})",
        f"    False positives on unsolvable tasks: {holdout['false_positives']}",
        f"    Time: {holdout['time_ms']}ms for {holdout['total']} tasks",
        "",
        "  SYNTAX GATE",
        f"    {syntax['passed']}/{syntax['files']} files pass ({syntax['time_ms']}ms)",
        "",
        "  COUNCIL",
        f"    Status: {'OPERATIONAL' if council.get('council_available') else 'DEGRADED (no API key)'}",
        "",
        f"  COMPOSITE GRADE: {grade} ({grade_score}/100)",
        "    Formula: 0.5*holdout + 0.2*coverage + 0.3*syntax - 5*false_positives",
        "",
        "  TASK-BY-TASK (holdout set)",
    ]
    for t in holdout["tasks"]:
        status = "OK " if t["resolved"] else ("-- " if t["expected_solvable"] else "ok-miss")
        lines.append(f"    [{status}] {t['task'][:55]:<55s} {t['level']}")
    lines.append("")
    lines.append("  Report generated by benchmark_full.py")
    return "\n".join(lines)


def main():
    print("Onklaud 5 Benchmark Suite v4 - running...")
    print("=" * 50)

    print(f"\n[1/4] Pattern-coverage set ({len(PATTERN_COVERAGE_TASKS)} tasks)...")
    coverage = run_task_set(PATTERN_COVERAGE_TASKS, "pattern_coverage")
    print(f"  Hit rate: {coverage['hit_rate']}% (upper bound)")

    print(f"\n[2/4] Holdout set ({len(HOLDOUT_TASKS)} independently phrased tasks)...")
    holdout = run_task_set(HOLDOUT_TASKS, "holdout")
    print(f"  Hit rate: {holdout['hit_rate']}% (realistic)")

    print("\n[3/4] Syntax check benchmark...")
    syntax = run_syntax_benchmark()
    print(f"  Pass: {syntax['passed']}/{syntax['files']} ({syntax['time_ms']}ms)")

    print("\n[4/4] Council metrics...")
    council = run_council_metrics()
    print(f"  Council: {'OPERATIONAL' if council.get('council_available') else 'DEGRADED'}")

    grade, grade_score = composite_grade(holdout, coverage, syntax)

    report = generate_text_report(coverage, holdout, syntax, council, grade, grade_score)

    report_path = MY_DIR / "BENCHMARK_REPORT.txt"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport saved: {report_path}")

    json_path = MY_DIR / "benchmark_results.json"
    json.dump({
        "datetime": datetime.now().isoformat(),
        "onklaud_version": "4",
        "pattern_coverage": {k: v for k, v in coverage.items() if k != "tasks"},
        "holdout": {k: v for k, v in holdout.items() if k != "tasks"},
        "holdout_tasks": holdout["tasks"],
        "coverage_tasks": coverage["tasks"],
        "syntax": syntax,
        "grade": grade,
        "grade_score": grade_score,
    }, open(json_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"JSON saved: {json_path}")

    print("\n" + report)
    return 0 if grade != "F" else 1


if __name__ == "__main__":
    sys.exit(main())
