# Onklaud 5 v4 - Design Spec

**Date:** 2026-07-03
**Goal:** A cross-model generate/review/revise pipeline with $0 offline layers.
**Philosophy:** Ponytail resolves what stdlib already solves (measured: ~60%
holdout, ~83% pattern coverage — run `benchmark_full.py` for current numbers).
GLM at 3 touchpoints. Prior-round critiques compressed (measured ~55% on a
3-round log).

## Architecture v4

```
USER REQUEST
      │
      ▼
🎠 PONYTAIL LADDER (step 0 - 0 tokens)
  stdlib → native → existing dep → shortest
  Resolved here = done at $0. Misses become generator hints.
      │
      ▼ (only if the ladder misses)
🔮 GLM PRE-DESIGN (touchpoint 1)
  Architecture sketch before Kimi codes
      │
      ▼
⚡ KIMI K2.7 CODE - generation (immune-memory hints injected)
      │
      ▼
⚡+🔮 DUAL REVIEW (touchpoint 2 - Kimi+GLM review IN PARALLEL)
  Scores averaged. Different blind spots.
      │
      ▼ (fail → ⚡ KIMI REVISES with compressed critiques, ≤ max_rounds)
🔮 GLM ARBITRATION (touchpoint 3 - final synthesis on exhaustion)
      │
      ▼
GATE (AST-based static analysis) → 🔨 VERIFY
```

## Components

| Component | Role | Tokens | Cost |
|-----------|------|--------|------|
| 🎠 ponytail_ladder.py | stemmed+synonym matching, ~200 patterns, confidence + hints | 0 | $0 |
| 🔮 GLM 5.2 | pre-design + dual review + arbitration | ≤64000 | $1.40/$4.40/M |
| ⚡ Kimi K2.7 | generation + review + revision | ≤64000 | $0.95/$4.00/M |
| 🔮 pre_check.py | immune memory scan; hints injected into generation | 0 | $0 |
| 🚦 quality_gate.py | AST static analysis (syntax, bare except, mutable defaults, error handling) | 0 | $0 |
| 🔨 verify.py | type-check + tests (trusted repos only) | 0 | $0 |
| 🗜️ compress_critiques | prior-round critique compression, measured per run | 0 | $0 |

## Config

`nadirclaw/config.yaml` is the source of truth: model ids, pricing, gate
threshold, max revision rounds. council.py reads it at startup (falls back to
built-in defaults without PyYAML).

## Rules

1. 🎠 Ponytail Ladder FIRST - always, before any code
2. 🔮 GLM at pre-design + dual review + arbitration
3. Pipeline trace: `[🎠→⚡K(X/10)→🔮G→gate(X/10)]`
4. Gate threshold from config (default 10) — achievable by clean code, enforced by AST checks
5. DeepSeek = emergency fallback only
6. No fabricated numbers anywhere: if it isn't measured in a run, it isn't claimed
7. Degraded ≠ passed: API/gate failures report degraded with score 0, never a fake 7
