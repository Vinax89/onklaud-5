#!/usr/bin/env python3
"""Render research_benchmarks.json as a small self-contained HTML report.

All dynamic fields are html.escape()d — task text can contain arbitrary
user/LLM-supplied strings. Run research_paper_benchmark.py first.
"""
import html
import json
import sys
from datetime import datetime
from pathlib import Path

MY_DIR = Path(__file__).resolve().parent
DATA_FILE = MY_DIR / "research_benchmarks.json"
OUT_FILE = MY_DIR / "ONKLAUD_5_BENCHMARKS.html"


def esc(v):
    return html.escape(str(v))


def task_rows(rows):
    out = []
    for r in rows:
        status = "OK" if r.get("found") else ("miss" if r.get("expected_solvable") else "ok-miss")
        out.append(
            f"<tr><td>{esc(r.get('task', ''))}</td>"
            f"<td>{esc(r.get('language', ''))}</td>"
            f"<td>{esc(status)}</td>"
            f"<td>{esc(r.get('level', ''))}</td>"
            f"<td>{esc(r.get('latency_ms', ''))} ms</td></tr>"
        )
    return "\n".join(out)


def main():
    if not DATA_FILE.exists():
        print("Run research_paper_benchmark.py first (research_benchmarks.json missing)", file=sys.stderr)
        return 1
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    pony = data.get("ponytail_ladder", {})
    cov = pony.get("pattern_coverage", {})
    hold = pony.get("holdout", {})
    syntax = data.get("syntax_gate", {})
    pre = data.get("immune_precheck", {})
    comp = data.get("context_compression", {})
    integ = data.get("pipeline_integration", {})

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Onklaud 5 Benchmarks (measured)</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
td, th {{ border: 1px solid #ccc; padding: 4px 8px; text-align: left; font-size: 14px; }}
.k {{ font-weight: 600; }}
.note {{ background: #fff8e1; padding: 8px 12px; border-left: 4px solid #f0b429; }}
</style></head><body>
<h1>Onklaud 5 — Measured Benchmarks</h1>
<p>Generated {esc(datetime.now().strftime('%Y-%m-%d %H:%M'))} from {esc(data.get('datetime', '?'))} run data.
Every number below was computed by <code>research_paper_benchmark.py</code> in that run.</p>

<div class="note"><strong>Integrity note:</strong> "Pattern coverage" tasks are phrased near the
ladder's own pattern keys and represent an upper bound. The <strong>holdout</strong> number
(independently phrased tasks) is the realistic one.</div>

<h2>Summary</h2>
<table>
<tr><td class="k">Ponytail — pattern coverage (upper bound)</td><td>{esc(cov.get('hit_rate_pct', '?'))}%</td></tr>
<tr><td class="k">Ponytail — holdout (realistic)</td><td>{esc(hold.get('hit_rate_pct', '?'))}%</td></tr>
<tr><td class="k">Syntax gate</td><td>{esc(syntax.get('pass_rate_pct', '?'))}% ({esc(syntax.get('passed', '?'))}/{esc(syntax.get('total_files', '?'))})</td></tr>
<tr><td class="k">Immune pre-check detection</td><td>{esc(pre.get('detection_rate_pct', '?'))}% against {esc(pre.get('patterns_stored', '?'))} stored patterns</td></tr>
<tr><td class="k">Context compression (measured)</td><td>{esc(comp.get('reduction_pct', '?'))}%</td></tr>
<tr><td class="k">Pipeline integration</td><td>{esc(integ.get('pass_rate_pct', '?'))}% ({esc(integ.get('passed', '?'))}/{esc(integ.get('total_tests', '?'))})</td></tr>
</table>

<h2>Holdout tasks (task-by-task)</h2>
<table>
<tr><th>Task</th><th>Lang</th><th>Result</th><th>Level</th><th>Latency</th></tr>
{task_rows(data.get('ponytail_details', []))}
</table>

<p>Data: <code>research_benchmarks.json</code> · Paper: <code>RESEARCH_PAPER.txt</code></p>
</body></html>"""

    OUT_FILE.write_text(doc, encoding="utf-8")
    print(f"Saved: {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
