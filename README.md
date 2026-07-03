# Onklaud 5 — A Cross-Model Code Review Pipeline

<p align="center">
  <strong>Generate, review, revise: multiple cheap models checking each other's work,<br>
  wrapped in offline pre-resolution and real static-analysis gates.</strong>
</p>

<p align="center">
  <img src="pipeline-diagram.png" alt="Onklaud 5 Pipeline Architecture" width="800">
</p>

<p align="center">
  <img src="demo.gif" alt="Onklaud 5 Terminal Demo" width="800">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue">
  <img src="https://img.shields.io/badge/license-BSL%201.1-green">
  <img src="https://img.shields.io/badge/benchmarks-reproducible-orange">
</p>

---

## What It Is

Onklaud 5 is not a model. It's a pipeline that orchestrates independent models
(Kimi K2.7 Code + GLM 5.2 via one OpenRouter key) through a structured council:

1. **Ponytail Ladder** ($0, offline) — resolves tasks that stdlib/native
   platform features already solve, before any API call. Misses become hints
   for the generator.
2. **GLM pre-design** — architecture sketch before code is written.
3. **Kimi generation** — the draft.
4. **Dual review (parallel)** — Kimi and GLM review the same draft
   simultaneously. Different training lineages, different blind spots.
5. **Kimi revision** — critiques feed back into a revised draft (up to
   3 rounds, with measured compression of older critiques).
6. **GLM arbitration** — synthesizes a final answer if rounds exhaust.
7. **Quality gate** ($0, offline) — AST-based static analysis: syntax,
   bare excepts, mutable defaults, real error-handling checks. 10/10 is
   achievable by clean code and not by keyword-stuffing.

Every failure the council catches is stored in **immune memory** and injected
into future generation prompts — the read side actually runs, so the pipeline
genuinely accumulates knowledge across runs.

Why an ensemble? The same reasoning as random forests over decision trees:
independent perspectives catch what any single reviewer misses. Whether that
beats a frontier model on external benchmarks is **an open question we have
not measured yet** — see [Honest Benchmarks](#honest-benchmarks).

---

## Quick Start

### You Need ONE API Key

OpenRouter gives you Kimi K2.7 + GLM 5.2. One key.

```bash
git clone https://github.com/Vinax89/onklaud-5.git
cd onklaud-5
pip install -r requirements.txt
cp .env.example .env
# Edit .env: OPENROUTER_API_KEY=sk-or-v1-your-key-here
python test_pipeline.py   # offline suite; expect 0 failed
```

### Solve a task (full pipeline)

```bash
python council.py solve --prompt "build an HTTP client with retry logic"
```

The council generates the draft, dual-reviews it, revises it with the
critiques, and only emits it once the gate passes (or arbitration concludes).

### Review an existing draft

```bash
python council.py dual --type code --prompt "..." --draft-file file.py
python council.py loop --type code --prompt "..." --draft-file file.py
```

### Free operations (0 API cost)

```bash
python ponytail_ladder.py --task "read a JSON config file" --json
python pre_check.py --task "write an HTTP retry function" --json
python fast_gate.py path/to/file.py --syntax-only
python council.py gate --text "..." --domain coding
```

---

## Honest Benchmarks

Run them yourself: `python benchmark_full.py` and
`python research_paper_benchmark.py`. Two ladder numbers are reported
separately and must not be conflated:

| Metric | Value (2026-07-03 run) | What it means |
|--------|------------------------|---------------|
| Ponytail **pattern coverage** | 83.3% | Tasks phrased near the corpus's own keys. **Upper bound.** |
| Ponytail **holdout** | 60.0% | Independently phrased tasks. **The realistic number.** |
| False positives on unsolvable tasks | 0 (holdout) | The ladder doesn't claim stdlib covers Raft. |
| Syntax gate | 100% | `ast.parse`/`node --check` on every file. Deterministic. |
| Context compression | 54.6% | Measured by running `compress_critiques` on a 3-round log. |
| Pipeline integration | 0 failed | Offline test suite (`test_pipeline.py` + 3 unit suites). |

**What we have NOT measured:** performance on HumanEval, SWE-bench, or any
external benchmark, and therefore any comparison against frontier models.
Earlier versions of this repo claimed parity with frontier models and shipped
a comparison PDF with unsourced numbers; both have been removed. The composite
grade in `benchmark_full.py` is computed from measured values only and can
fail.

Per-run token usage and real dollar cost are logged to `scores.jsonl`
(`python council.py status` summarizes them).

---

## Review Pull Requests (SARIF + GitHub Action)

Dual-review any git diff and emit SARIF for GitHub code scanning:

```bash
python council.py review-diff --base origin/main --sarif onklaud-review.sarif
```

Or wire it into a workflow so findings appear as native PR annotations:

```yaml
permissions:
  security-events: write
steps:
  - uses: actions/checkout@v4
    with: { fetch-depth: 0 }
  - uses: Vinax89/onklaud-5@main
    with:
      openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
  - uses: github/codeql-action/upload-sarif@v3
    with:
      sarif_file: onklaud-review.sarif
```

Installable as a package too — `pip install .` gives you the `onklaud` CLI and
a library API:

```python
from council import review
result = review(draft_text, "check for race conditions")  # dict, no exit()
```

## Commands Reference

```bash
python council.py solve --prompt "..."                    # full generate+review+revise pipeline
python council.py review-diff --base origin/main --sarif out.sarif
python council.py dual  --type code --prompt "..." --draft-file f.py
python council.py loop  --type code --prompt "..." --draft-file f.py
python council.py gate  --text "..." --domain coding
python council.py status                                  # keys, history, measured cost
python ponytail_ladder.py --task "..." --json
python pre_check.py --task "..." --json
python benchmark_full.py                                  # reproducible benchmark, honest grade
```

Configuration lives in [`nadirclaw/config.yaml`](nadirclaw/config.yaml) —
models, pricing, gate threshold, and max revision rounds are read from there
(swap in any OpenRouter-compatible model).

## Models

| Model | Provider | Role | Input $/1M | Output $/1M | Context |
|-------|----------|------|-----------|-------------|---------|
| Kimi K2.7 Code | Moonshot AI | Generation + review + revision | $0.95 | $4.00 | 262K |
| GLM 5.2 | Z.AI / Tsinghua | Pre-design + review + arbitration | $1.40 | $4.40 | 1M |

Third-party benchmark scores for these models are published by their
providers; we don't reproduce them here because we haven't verified them.

---

## Development

```bash
pip install -r requirements.txt ruff
ruff check .
python test_council_unit.py
python test_quality_gate_unit.py
python test_ponytail_unit.py
python test_pipeline.py
```

CI runs the same on Ubuntu + Windows, Python 3.10 and 3.13.

## License

**Business Source License 1.1** — [LICENSE](LICENSE)

- Free for non-production, academic, personal use — unlimited
- Free for production if: revenue < $2M OR team < 25 people
- **Converts to MIT on 2030-06-22**

## Acknowledgments

- **Kimi K2.7 Code** by Moonshot AI
- **GLM 5.2** by Z.AI / Tsinghua University (open weights, MIT)
- **Ponytail** by Dietrich Gebert
