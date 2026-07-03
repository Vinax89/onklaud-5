#!/usr/bin/env python3
"""
Onklaud 5 Council Orchestrator v3.1 -- 🎠 Ponytail Native + 🔮 GLM +30%

GLM now used at 3 stages (up from 1): pre-design → dual review → arbitration (+50% presence)

Modes:
  loop     -- Full pipeline: GLM pre-design → Kimi generate → dual review (Kimi+GLM) → revise → GLM arbitrate → gate
  review   -- Send draft to Kimi (code) or GLM (architecture), get score + critique
  dual     -- Dual review by both Kimi AND GLM, scores averaged
  gate     -- Run quality gate on text
  full     -- review + gate in one call (default)

Usage:
  # Loop mode (auto-redo, graceful degradation, immune memory)
  python onklaud-5/council.py loop --type code --prompt "fix the bug" --draft "..."
  echo "draft" | python onklaud-5/council.py loop --type code --prompt "..."

  # Legacy modes (kept for backward compat)
  python onklaud-5/council.py full --type code --prompt "..." --draft "..."
  python onklaud-5/council.py review --type architecture --prompt "..." --draft "..."
  python onklaud-5/council.py gate --text "..." --domain coding

Output:
  Default: pipeline trace line + final score on stdout
  --json:   full JSON object on stdout
  stderr:   debug/trace info

Exit codes: 0 = passed (score >= 10 or degraded), 1 = failed

Windows: Git Bash /tmp or %TEMP% for temp files. Uses ascii-safe stdout.
"""

import sys
import os
import json
import argparse
import hashlib
import re
import threading
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# --- Paths -----------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

# Temp directory: prefer /tmp on Unix/Git-Bash, fall back to system temp
TMP = Path(tempfile.gettempdir())
for cand in [
    Path("/tmp"),
    Path(os.environ.get("TMP", "")),
    Path(os.environ.get("TEMP", "")),
    Path(tempfile.gettempdir()),
]:
    try:
        if cand.is_dir() and os.access(str(cand), os.W_OK):
            TMP = cand
            break
    except Exception:
        continue

ROUND_FILE = TMP / "onklaud-round.txt"
ISSUES_FILE = TMP / "onklaud-issues.json"
DRAFT_FILE = TMP / "onklaud-draft.txt"


def init_state(prompt, state_dir=None):
    """Give each task its own state dir so concurrent runs don't clobber each other.

    Loop state must survive across CLI invocations (the caller revises the draft
    between rounds), so the dir is derived deterministically from cwd + prompt —
    the same task continues its rounds, a different task gets fresh state.
    """
    global ROUND_FILE, ISSUES_FILE
    if state_dir:
        base = Path(state_dir)
    else:
        digest = hashlib.sha256(f"{os.getcwd()}|{prompt or ''}".encode("utf-8")).hexdigest()[:12]
        base = TMP / f"onklaud-{digest}"
    base.mkdir(parents=True, exist_ok=True)
    ROUND_FILE = base / "round.txt"
    ISSUES_FILE = base / "issues.json"
SCORES_FILE = SCRIPT_DIR / "scores.jsonl"
IMMUNE_FILE = SCRIPT_DIR / "immune_memory.json"

# --- Environment -----------------------------------------------------
def load_env():
    """Load .env from project root."""
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key not in os.environ:
                    os.environ[key] = val

load_env()

OR_BASE = "https://openrouter.ai/api/v1/chat/completions"
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OR_KEY_BACKUP = os.environ.get("OPENROUTER_API_KEY_BACKUP", "")


# --- Config (nadirclaw/config.yaml is the source of truth) -----------
def load_config():
    """Load pipeline config. Falls back to built-in defaults when PyYAML or
    the file is unavailable, so the stdlib-only core keeps working."""
    cfg_file = SCRIPT_DIR / "nadirclaw" / "config.yaml"
    try:
        import yaml
        with open(cfg_file, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


CONFIG = load_config()


def _cfg_model(name_fragment, default_id):
    for m in CONFIG.get("models", []):
        if name_fragment in m.get("name", "") and m.get("model_id"):
            return m["model_id"]
    return default_id


KIMI_MODEL = _cfg_model("kimi", "moonshotai/kimi-k2.7-code")
GLM_MODEL = _cfg_model("glm", "z-ai/glm-5.2")

_STAGES = CONFIG.get("council", {}).get("stages", {})
GATE_THRESHOLD = _STAGES.get("quality_gate", {}).get("threshold", 10)
MAX_ROUNDS = _STAGES.get("revise", {}).get("max_rounds", 3)

# --- Improved Review Prompts -----------------------------------------

REVIEW_PROMPT_CODE = """Review this code. Be brutal and specific. Output ONLY valid JSON (no markdown, no backticks):
{{"passed": bool, "score": 0-10, "critique": "one paragraph explaining why this score and not higher", "issues": ["specific_issue1", "specific_issue2"]}}

Look for: bugs, logic errors, edge cases (null, empty, boundary), security issues (injection, auth, sanitization), race conditions, error handling gaps, type safety problems, performance pitfalls, and testability.

REQUEST: {prompt}

DRAFT: {draft}"""

REVIEW_PROMPT_ARCH = """Review this architecture/design. Be brutal and specific. Output ONLY valid JSON (no markdown, no backticks):
{{"passed": bool, "score": 0-10, "critique": "one paragraph explaining why this score and not higher", "issues": ["specific_issue1", "specific_issue2"]}}

Look for: scaling bottlenecks, failure modes (what breaks?), cost/operational problems, consistency issues, single points of failure, complexity smells, missing observability, data loss risks, and tradeoffs not addressed.

REQUEST: {prompt}

DRAFT: {draft}"""

GLM_ARBITRATE_PROMPT = """You are the final arbiter. Synthesize the absolute best answer combining the original draft with ALL critique feedback. Address every issue raised. Output ONLY the final improved response -- no markdown, no JSON wrapper, just the raw final answer.

ORIGINAL REQUEST: {prompt}

DRAFT: {draft}

CRITIQUES RECEIVED: {critiques}

FINAL SYNTHESIZED ANSWER:"""

GLM_PRE_DESIGN_PROMPT = """You are the architecture designer. BEFORE any code is written, sketch the optimal approach. Output ONLY valid JSON (no markdown, no backticks):
{{"approach": "one paragraph describing the optimal architecture/approach", "files_to_touch": ["file1", "file2"], "risks": ["risk1", "risk2"], "alternatives": ["simpler_approach_if_applicable"], "complexity": "low|medium|high", "stdlib_check": "can this be solved with stdlib/native APIs without new code?"}}

Analyze the request and design the solution. Consider: simplicity first, existing code patterns, stdlib/native coverage, minimal new code.

REQUEST: {prompt}

DRAFT: {draft}"""

KIMI_GENERATE_PROMPT = """You are the primary code generator. Produce the best possible answer to the request. Output ONLY the code/answer — no preamble, no explanations outside code comments.

REQUEST: {prompt}

ARCHITECTURE GUIDANCE (from pre-design):
{design}

KNOWN PAST FAILURE PATTERNS TO AVOID (from this pipeline's immune memory):
{immune}"""

KIMI_REVISE_PROMPT = """You are revising a draft that failed council review. Address EVERY critique below. Keep what works, fix what doesn't. Output ONLY the full revised draft — no preamble.

REQUEST: {prompt}

CURRENT DRAFT:
{draft}

CRITIQUES TO ADDRESS:
{critiques}

KNOWN PAST FAILURE PATTERNS TO AVOID:
{immune}"""


# --- API Client ------------------------------------------------------

# $ per 1K tokens (input, output) — used to log real cost per run
MODEL_PRICES = {
    m["model_id"]: (m.get("cost_per_1k_input", 0), m.get("cost_per_1k_output", 0))
    for m in CONFIG.get("models", []) if m.get("model_id")
} or {
    KIMI_MODEL: (0.00095, 0.004),
    GLM_MODEL: (0.0014, 0.0044),
}
USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "api_calls": 0}
_USAGE_LOCK = threading.Lock()

# 4xx errors that will not succeed on retry (bad key, bad model id) — fail fast
NON_RETRYABLE = {400, 401, 403, 404}


def _track_usage(model, data):
    """Accumulate token usage + cost from an OpenRouter response."""
    u = data.get("usage") or {}
    pt = u.get("prompt_tokens", 0)
    ct = u.get("completion_tokens", 0)
    pin, pout = MODEL_PRICES.get(model, (0, 0))
    with _USAGE_LOCK:
        USAGE["prompt_tokens"] += pt
        USAGE["completion_tokens"] += ct
        USAGE["cost_usd"] += pt / 1000 * pin + ct / 1000 * pout
        USAGE["api_calls"] += 1


def call_openrouter(model, prompt, max_tokens=2000, api_key=None, retries=2, reasoning=None):
    """Call OpenRouter API with retry + backup-key failover. Returns response text or None."""
    keys = [k for k in ([api_key] if api_key else [OR_KEY, OR_KEY_BACKUP]) if k]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.1
    }
    # Enable reasoning for Kimi/GLM (they're reasoning models)
    if reasoning:
        payload["reasoning"] = reasoning
    elif "kimi" in model.lower() or "glm" in model.lower():
        payload["reasoning"] = {"effort": "high"}

    body = json.dumps(payload).encode("utf-8")

    last_error = None
    for key in keys:
        for attempt in range(retries + 1):
            try:
                req = Request(OR_BASE, data=body, headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/Vinax89/onklaud-5",
                    "X-Title": "Onklaud 5 Council"
                })
                resp = urlopen(req, timeout=60)
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                if content:
                    _track_usage(model, data)
                    return content
                last_error = "Empty response from model"
            except HTTPError as e:
                last_error = f"HTTP {e.code}: {e.reason}"
                if e.code in NON_RETRYABLE:
                    break  # retrying won't help; move to next key (or give up)
            except URLError as e:
                last_error = f"Network: {e.reason}"
            except Exception as e:
                last_error = str(e)[:200]
            if attempt < retries:
                time.sleep(2 ** attempt)  # exponential backoff

    print(f"  [api] OpenRouter unreachable after retries: {last_error}", file=sys.stderr)
    return None


# --- Review Parsing ---------------------------------------------------

def parse_review(raw):
    """Extract JSON from model response. Handles markdown-wrapped JSON."""
    if not raw:
        return {"passed": False, "score": 0, "critique": "Empty response", "issues": ["Empty response"]}

    # Try direct parse
    try:
        result = json.loads(raw)
        if "score" in result and "passed" in result:
            return result
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code blocks (json or generic)
    for pat in [r'```json\s*\n(.*?)\n\s*```', r'```\s*\n(.*?)\n\s*```']:
        m = re.search(pat, raw, re.DOTALL)
        if m:
            try:
                result = json.loads(m.group(1))
                if "score" in result and "passed" in result:
                    return result
            except json.JSONDecodeError:
                pass

    # Try finding JSON object with passed/score fields
    m = re.search(r'\{[^{}]*"passed"\s*:\s*(?:true|false)[^{}]*"score"\s*:\s*\d+[^{}]*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # Reverse: score then passed
    m = re.search(r'\{[^{}]*"score"\s*:\s*\d+[^{}]*"passed"\s*:\s*(?:true|false)[^{}]*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # Salvage: try to close truncated JSON (reasoning model cutoff)
    # Find the last complete JSON element before truncation
    salvage = raw.strip()
    if salvage.startswith("{") and not salvage.endswith("}"):
        # Try closing at last complete array bracket or comma
        for close_attempt in ["}]}", '"]}', ']}', "}", '"}']:
            candidate = salvage + close_attempt
            try:
                result = json.loads(candidate)
                if "score" in result:
                    return result
            except json.JSONDecodeError:
                continue

    return {"passed": False, "score": 0, "critique": "Failed to parse review response",
            "issues": [raw[:300]]}


# --- Quality Gate ----------------------------------------------------

def run_quality_gate(text, domain="general"):
    """Score text with the quality gate, in-process.

    In-process (not a subprocess) so arbitrarily large drafts work on every
    platform. On failure the result is marked degraded with passed=False —
    never a fabricated passing score.
    """
    # Council review types say "code"; the gate registry says "coding".
    # Without this mapping the coding gates silently never ran.
    domain = {"code": "coding"}.get(domain, domain)
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from quality_gate import score_output
        return score_output(text, domain)
    except Exception as e:
        return {"score": 0, "passed": False, "degraded": True, "gates": [],
                "issues": [f"Quality gate unavailable: {e}"]}


# --- Immune Memory ---------------------------------------------------

def _categorize_issues(issues_text):
    """Map issue text to a failure category using pre_check's taxonomy."""
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from pre_check import FAILURE_CATEGORIES
        tl = issues_text.lower()
        for cat, kws in FAILURE_CATEGORIES.items():
            if any(kw.lower() in tl for kw in kws):
                return cat
    except Exception:
        pass
    return "general"


def immune_hints(task_text, limit=3):
    """Retrieve the most relevant stored failure patterns for prompt injection.

    This is the read side of immune memory: without it the pipeline records
    failures but never learns from them.
    """
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from pre_check import check_task
        res = check_task(task_text)
        lines = []
        for w in res.get("warnings", [])[:limit]:
            lines.append(f"- [{w['category']}] seen {w['frequency']}x: {w['pattern_preview']}")
        for r in res.get("relevant", [])[: max(0, limit - len(lines))]:
            lines.append(f"- seen {r['frequency']}x: {r['pattern_preview']}")
        return "\n".join(lines) if lines else "None recorded."
    except Exception:
        return "None recorded."


def record_immune_memory(issues):
    """Save failure patterns to immune_memory.json for pattern learning."""
    if not issues:
        return
    try:
        pattern_text = json.dumps(issues, ensure_ascii=False, sort_keys=True)[:200]
        memories = []
        if IMMUNE_FILE.exists():
            try:
                memories = json.loads(IMMUNE_FILE.read_text(encoding="utf-8"))
                if not isinstance(memories, list):
                    memories = []
            except (json.JSONDecodeError, OSError):
                memories = []

        entry = None
        now = datetime.now(timezone.utc).isoformat()
        for mem in memories:
            if mem.get("pattern") == pattern_text:
                mem["frequency"] = mem.get("frequency", 1) + 1
                mem["last_seen"] = now
                entry = mem
                break

        if entry is None:
            entry = {
                "timestamp": now,
                "last_seen": now,
                "pattern": pattern_text,
                "issues": [str(i)[:200] for i in issues[:6]],
                "category": _categorize_issues(pattern_text),
                "frequency": 1
            }
            memories.append(entry)

        IMMUNE_FILE.write_text(json.dumps(memories, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  [immune] Pattern recorded (freq={entry['frequency']})", file=sys.stderr)
    except Exception as e:
        print(f"  [immune] Failed to record: {e}", file=sys.stderr)


# --- Score Tracking --------------------------------------------------

def record_score(result, review_type, prompt, degraded=False):
    """Append one JSON line to scores.jsonl for metrics tracking."""
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": review_type,
            "reviewer_model": result.get("reviewer_model", "unknown"),
            "review_score": result.get("review_score", 0),
            "gate_score": result.get("gate_score", 0),
            "final_score": result.get("final_score", 0),
            "passed": result.get("passed", False),
            "prompt_preview": (prompt or "")[:100].replace("\n", " "),
            "degraded": degraded,
            "round": result.get("round", 0),
            "usage": dict(USAGE),
        }
        SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SCORES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"  [scores] Recorded (score={entry['final_score']}, passed={entry['passed']})", file=sys.stderr)
    except Exception as e:
        print(f"  [scores] Failed to record: {e}", file=sys.stderr)


# --- Degraded Result -------------------------------------------------

def degraded_result(reason="OpenRouter API unreachable after retries"):
    """Return a graceful degradation result. passed=False, score 0 — a review
    that never happened must not contribute a fabricated score."""
    return {
        "passed": False,
        "score": 0,
        "degraded": True,
        "critique": f"UNCOUNCILED - {reason}",
        "issues": [reason],
        "reviewer_model": "none (degraded)",
        "review_score": 0,
        "gate_score": 0,
        "final_score": 0,
    }


# --- Review ----------------------------------------------------------

def do_review(draft, prompt, review_type="code"):
    """Send draft to reviewer model. Returns (result_dict, degraded_flag)."""
    model = KIMI_MODEL if review_type == "code" else GLM_MODEL
    tmpl = REVIEW_PROMPT_CODE if review_type == "code" else REVIEW_PROMPT_ARCH
    model_short = model.split("/")[-1]

    safe_prompt = prompt[:4000] if prompt else "No prompt provided"
    safe_draft = draft[:60000] if draft else "No draft provided"
    full_prompt = tmpl.format(prompt=safe_prompt, draft=safe_draft)

    print(f"  [review] Calling {model_short} ({review_type})...", file=sys.stderr)
    review_tokens = 64000 if review_type == "code" else 64000
    raw = call_openrouter(model, full_prompt, max_tokens=review_tokens)

    if raw is None:
        return degraded_result(f"OpenRouter unreachable for {review_type} review"), True

    result = parse_review(raw)
    result["reviewer_model"] = model
    result["_raw_response_len"] = len(raw)

    score = result.get("score", 0)
    passed = result.get("passed", False)
    print(f"  [review] {model_short} score: {score}/10, passed: {passed}", file=sys.stderr)

    if score < 10 or not passed:
        issues = result.get("issues", [])
        if issues:
            record_immune_memory(issues)

    return result, False


# --- GLM Pre-Design (NEW v3.1 - GLM +30%) ----------------------------

def do_glm_pre_design(draft, prompt):
    """GLM sketches architecture BEFORE Kimi generates code. First GLM touchpoint."""
    safe_prompt = prompt[:4000] if prompt else "No prompt provided"
    safe_draft = draft[:8000] if draft else "No draft provided"
    full_prompt = GLM_PRE_DESIGN_PROMPT.format(prompt=safe_prompt, draft=safe_draft)

    print("  [glm-pre] GLM designing architecture...", file=sys.stderr)
    raw = call_openrouter(GLM_MODEL, full_prompt, max_tokens=16000, reasoning={"effort": "medium"})

    if raw is None:
        return degraded_result("GLM pre-design API unreachable"), True

    result = parse_review(raw)
    result["reviewer_model"] = f"{GLM_MODEL} (pre-design)"
    result["_raw_response_len"] = len(raw)

    print(f"  [glm-pre] GLM pre-design complete ({len(raw)} chars)", file=sys.stderr)

    return result, False


# --- Dual Review (NEW v3.1 - Kimi + GLM both review) -----------------

def do_dual_review(draft, prompt):
    """Both Kimi AND GLM review the code concurrently. Scores averaged. Catches different blind spots."""
    print("  [dual] Starting dual review (Kimi + GLM, parallel)...", file=sys.stderr)

    model = GLM_MODEL
    safe_prompt = prompt[:4000] if prompt else "No prompt provided"
    safe_draft = draft[:60000] if draft else "No draft provided"
    full_prompt = REVIEW_PROMPT_CODE.format(prompt=safe_prompt, draft=safe_draft)

    # Kimi and GLM review the same draft at the same time — different
    # architectures, different blind spots, half the wall clock.
    with ThreadPoolExecutor(max_workers=2) as pool:
        kimi_future = pool.submit(do_review, draft, prompt, "code")
        glm_future = pool.submit(call_openrouter, model, full_prompt, 64000)
        kimi_result, kimi_degraded = kimi_future.result()
        raw = glm_future.result()

    glm_degraded = raw is None
    glm_result = parse_review(raw) if raw else degraded_result("GLM dual review API unreachable")
    glm_result["reviewer_model"] = model

    # Average scores
    kimi_score = kimi_result.get("score", 0)
    glm_score = glm_result.get("score", 0)
    avg_score = round((kimi_score + glm_score) / 2)

    # Combine critiques
    all_issues = (kimi_result.get("issues", []) +
                  glm_result.get("issues", []))
    combined_critique = (
        f"Kimi ({kimi_score}/10): {kimi_result.get('critique', 'N/A')}\n"
        f"GLM ({glm_score}/10): {glm_result.get('critique', 'N/A')}"
    )

    degraded = kimi_degraded and glm_degraded

    result = {
        "passed": kimi_result.get("passed") and glm_result.get("passed"),
        "score": avg_score,
        "degraded": degraded,
        "critique": combined_critique,
        "issues": all_issues,
        "reviewer_model": "dual (kimi-k2.7+glm-5.2)",
        "review_score": avg_score,
        "gate_score": 0,
        "final_score": avg_score,
        "kimi_review": {"score": kimi_score, "passed": kimi_result.get("passed")},
        "glm_review": {"score": glm_score, "passed": glm_result.get("passed")},
    }

    print(f"  [dual] Kimi({kimi_score}/10) + GLM({glm_score}/10) = {avg_score}/10 avg", file=sys.stderr)
    return result, degraded


# --- GLM Arbitration -------------------------------------------------

def compress_critiques(critiques, keep_last=1, max_chars=4000):
    """Compress prior-round critiques so multi-round prompts don't saturate context.

    The latest round stays verbatim; older rounds are reduced to their issue
    lists. Returns (text, chars_saved) — savings are measured, not asserted.
    """
    if not critiques:
        return "", 0
    full_len = len(json.dumps(critiques, ensure_ascii=False))
    parts = []
    for c in critiques[:-keep_last]:
        issues = "; ".join(str(i)[:120] for i in c.get("issues", [])[:6])
        parts.append(f"Round {c.get('round', '?')} issues: {issues}")
    for c in critiques[-keep_last:]:
        parts.append(
            f"Round {c.get('round', '?')} (latest): {c.get('critique', '')[:2000]}\n"
            f"Issues: {json.dumps(c.get('issues', []), ensure_ascii=False)[:1500]}"
        )
    text = "\n".join(parts)[:max_chars]
    saved = max(0, full_len - len(text))
    if saved:
        pct = 100 * saved // max(1, full_len)
        print(f"  [headroom] critiques compressed {full_len}->{len(text)} chars ({pct}% saved)", file=sys.stderr)
    return text, saved


def do_glm_arbitrate(draft, prompt, critiques_list):
    """GLM synthesizes the best answer from draft + all critiques."""
    critiques_text = "\n\n---\n\n".join(
        f"CRITIQUE {i+1} (score: {c.get('review_score', '?')}/10): {c.get('critique', '')}\n"
        f"Issues: {json.dumps(c.get('issues', []))}"
        for i, c in enumerate(critiques_list)
    )

    full_prompt = GLM_ARBITRATE_PROMPT.format(
        prompt=prompt[:2000] if prompt else "No prompt",
        draft=draft[:6000] if draft else "No draft",
        critiques=critiques_text[:6000]
    )

    print("  [arbitrate] GLM arbitrating (synthesizing best answer)...", file=sys.stderr)
    raw = call_openrouter(GLM_MODEL, full_prompt, max_tokens=64000)

    if raw is None:
        return degraded_result("GLM arbitration API unreachable"), True

    # Run quality gate on GLM's synthesized output
    print(f"  [arbitrate] GLM responded ({len(raw)} chars), running gate...", file=sys.stderr)
    gate_result = run_quality_gate(raw, "general")

    final_score = gate_result.get("score", 7)
    passed = final_score >= GATE_THRESHOLD

    print(f"  [arbitrate] Gate score: {final_score}/10, passed: {passed}", file=sys.stderr)

    result = {
        "passed": passed,
        "score": final_score,
        "degraded": False,
        "escalated": True,
        "glm_synthesized": raw,
        "critique": f"GLM arbitration synthesized {len(raw)} chars. Gate score: {final_score}/10",
        "issues": gate_result.get("issues", []),
        "reviewer_model": f"{GLM_MODEL} (arbitration)",
        "review_score": final_score,
        "gate_score": final_score,
        "final_score": final_score,
    }

    return result, False


# --- Round Tracking --------------------------------------------------

def read_round():
    """Read current loop round from ROUND_FILE. Default: 1."""
    try:
        if ROUND_FILE.exists():
            val = ROUND_FILE.read_text().strip()
            return max(1, int(val))
    except Exception:
        pass
    return 1


def write_round(round_num):
    """Write current loop round to ROUND_FILE."""
    try:
        ROUND_FILE.write_text(str(round_num))
    except Exception:
        pass


def clear_round_state():
    """Remove round and issues temp files on success."""
    for fn in [ROUND_FILE, ISSUES_FILE]:
        try:
            if fn.exists():
                fn.unlink()
        except Exception:
            pass


# --- Format Output ---------------------------------------------------

def format_trace(result, review_type, json_output=False):
    """Format output: pipeline trace (default) or full JSON (--json)."""
    if json_output:
        # Strip internal fields
        out = {k: v for k, v in result.items() if not k.startswith("_")}
        return json.dumps(out, indent=2, ensure_ascii=False)

    # Pipeline trace line
    review_score = result.get("review_score", "?")
    gate_score = result.get("gate_score", "?")
    final_score = result.get("final_score", "?")
    passed = "PASS" if result.get("passed") else "FAIL"
    degraded = " [DEGRADED]" if result.get("degraded") else ""
    escalated = " [ESCALATED]" if result.get("escalated") else ""
    round_info = f"Round {result.get('round', 1)}: " if result.get("round") else ""

    return f"{round_info}[🎠→⚡K({review_score}/10)→🔮G→gate({gate_score}/10)] = {final_score}/10 {passed}{degraded}{escalated}"


# --- Subcommands -----------------------------------------------------

def cmd_review(args):
    """Review mode: model review only."""
    draft = _read_draft(args)
    result, degraded = do_review(draft, args.prompt, args.type)
    result["type"] = args.type

    # Record score
    record_score(result, args.type, args.prompt, degraded)

    print(format_trace(result, args.type, args.json_output))
    sys.exit(0 if result.get("passed") else 1)


def cmd_gate(args):
    """Gate mode: quality gate only."""
    text = args.text or sys.stdin.read()
    result = run_quality_gate(text, args.domain)

    if args.json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        score = result.get("score", "?")
        passed = "PASS" if result.get("passed") else "FAIL"
        print(f"[gate ({args.domain})] = {score}/10 {passed}")

    sys.exit(0 if result.get("passed") else 1)


def cmd_full(args):
    """Full council: review + quality gate."""
    draft = _read_draft(args)
    review_type = args.type
    prompt = args.prompt or "Code/architecture task"

    # Phase 1: Review
    r, degraded = do_review(draft, prompt, review_type)

    # Phase 2: Quality gate
    domain = review_type if review_type in ("code", "architecture") else "general"
    g = run_quality_gate(draft, domain)

    # Combined score
    review_score = r.get("score", 0)
    gate_score = g.get("score", 0)
    combined = round((review_score + gate_score) / 2)

    result = {
        "passed": combined >= GATE_THRESHOLD and r.get("passed", False) and g.get("passed", False),
        "score": combined,
        "degraded": degraded,
        "critique": r.get("critique", ""),
        "issues": r.get("issues", []) + g.get("issues", []),
        "reviewer_model": r.get("reviewer_model", "unknown"),
        "review_score": review_score,
        "gate_score": gate_score,
        "final_score": combined,
        "type": review_type,
    }

    record_score(result, review_type, prompt, degraded)
    print(format_trace(result, review_type, args.json_output))
    sys.exit(0 if result["passed"] else 1)


def cmd_dual(args):
    """Dual review mode: Kimi + GLM both review, scores averaged."""
    draft = _read_draft(args)
    prompt = args.prompt or "Code/architecture task"

    r, degraded = do_dual_review(draft, prompt)

    if degraded and not r.get("passed"):
        print(format_trace(r, args.type, args.json_output))
        sys.exit(1)

    # Quality gate on draft
    domain = args.type if args.type in ("code", "architecture") else "general"
    g = run_quality_gate(draft, domain)
    gate_score = g.get("score", 0)
    combined = round((r.get("score", 0) + gate_score) / 2)

    result = {
        "passed": combined >= GATE_THRESHOLD and r.get("passed", False) and g.get("passed", False),
        "score": combined,
        "degraded": degraded,
        "critique": r.get("critique", ""),
        "issues": r.get("issues", []) + g.get("issues", []),
        "reviewer_model": r.get("reviewer_model", "dual"),
        "review_score": r.get("score", 0),
        "gate_score": gate_score,
        "final_score": combined,
        "type": args.type,
    }

    record_score(result, args.type, prompt, degraded)
    print(format_trace(result, args.type, args.json_output))
    sys.exit(0 if result["passed"] else 1)


def cmd_loop(args):
    """Loop mode v3.1: GLM pre-design → dual review → gate → GLM arbitration."""
    draft = _read_draft(args)
    review_type = args.type
    prompt = args.prompt or "Code/architecture task"
    domain = review_type if review_type in ("code", "architecture") else "general"

    # Read current round
    init_state(prompt, getattr(args, "state_dir", None))
    round_num = read_round()
    print(f"  [loop] Round {round_num}/{MAX_ROUNDS} - 🎠→🔮GLM(pre)→⚡Kimi+🔮GLM(dual review)→gate", file=sys.stderr)

    # Collect all critiques for potential escalation
    all_critiques = []

    # Check if we have previous critiques from a prior round
    if ISSUES_FILE.exists() and round_num > 1:
        try:
            prev = json.loads(ISSUES_FILE.read_text(encoding="utf-8"))
            if prev.get("critiques"):
                all_critiques = prev.get("critiques", [])
        except Exception:
            pass

    # If round > 3 (already exhausted), escalate immediately
    if round_num > MAX_ROUNDS:
        print("  [loop] Round > 3, escalating to GLM arbitration...", file=sys.stderr)
        r, degraded = do_glm_arbitrate(draft, prompt, all_critiques)
        r["round"] = round_num
        _finalize_loop(r, degraded, review_type, prompt, args)
        return

    # === STEP 1: GLM PRE-DESIGN (NEW - GLM +30%) ===
    pre_design, pre_degraded = do_glm_pre_design(draft, prompt)
    if not pre_degraded:
        print(f"  [loop] GLM pre-design: {pre_design.get('critique', 'N/A')[:120]}...", file=sys.stderr)

    # === STEP 2: DUAL REVIEW (Kimi + GLM both review - GLM +30%) ===
    r, degraded = do_dual_review(draft, prompt)

    if degraded and not r.get("passed"):
        r["round"] = round_num
        clear_round_state()
        _finalize_loop(r, True, review_type, prompt, args)
        return

    # === STEP 3: Quality gate ===
    g = run_quality_gate(draft, domain)
    gate_score = g.get("score", 0)
    review_score = r.get("score", 0)
    combined = round((review_score + gate_score) / 2)

    passed = combined >= GATE_THRESHOLD and r.get("passed", False) and g.get("passed", False)

    # Collect critique for escalation tracking
    all_critiques.append({
        "round": round_num,
        "review_score": review_score,
        "gate_score": gate_score,
        "critique": r.get("critique", ""),
        "issues": r.get("issues", []) + g.get("issues", []),
        "glm_pre_design": pre_design.get("critique", "") if not pre_degraded else "",
    })

    result = {
        "passed": passed,
        "score": combined,
        "degraded": False,
        "critique": r.get("critique", ""),
        "issues": r.get("issues", []) + g.get("issues", []),
        "reviewer_model": r.get("reviewer_model", "dual"),
        "review_score": review_score,
        "gate_score": gate_score,
        "final_score": combined,
        "type": review_type,
        "round": round_num,
        "glm_pre_design": pre_design if not pre_degraded else None,
    }

    if passed:
        print(f"  [loop] Round {round_num} PASSED ({combined}/10)", file=sys.stderr)
        clear_round_state()
        _finalize_loop(result, False, review_type, prompt, args)
        return

    # FAILED: GLM arbitration (third GLM touchpoint - +30% complete)
    if round_num >= MAX_ROUNDS:
        print(f"  [loop] Round {round_num} failed ({combined}/10), GLM arbitration (3rd touchpoint)...", file=sys.stderr)
        arb_result, arb_degraded = do_glm_arbitrate(draft, prompt, all_critiques)
        arb_result["round"] = round_num
        arb_result["all_prior_critiques"] = all_critiques
        clear_round_state()
        if hasattr(args, 'output') and args.output and 'glm_synthesized' in arb_result:
            _save_output(arb_result['glm_synthesized'], args.output)
        _finalize_loop(arb_result, arb_degraded, review_type, prompt, args)
    else:
        # Advance to next round
        next_round = round_num + 1
        write_round(next_round)

        combined_issues = r.get("issues", []) + g.get("issues", [])
        try:
            ISSUES_FILE.write_text(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "issues": combined_issues,
                "critiques": all_critiques,
                "current_round": round_num,
                "next_round": next_round,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  [issues] Saved {len(combined_issues)} issue(s) to {ISSUES_FILE}", file=sys.stderr)
        except Exception as e:
            print(f"  [issues] Failed to save: {e}", file=sys.stderr)

        print(f"  [loop] Round {round_num} FAILED ({combined}/10) - issues saved for revision", file=sys.stderr)
        result["next_round"] = next_round
        record_score(result, review_type, prompt, False)
        print(format_trace(result, review_type, args.json_output))
        sys.exit(1)


def cmd_solve(args):
    """Full generate-review-revise pipeline. This is the pipeline the README
    describes: ponytail -> GLM pre-design -> Kimi GENERATES -> dual review ->
    Kimi REVISES with critiques (up to MAX_ROUNDS) -> GLM arbitration -> gate.

    Unlike `loop`, no external caller is needed to improve the draft — the
    council does its own revision.
    """
    prompt = args.prompt
    review_type = args.type
    domain = review_type if review_type in ("code", "architecture") else "general"
    init_state(prompt, getattr(args, "state_dir", None))

    # === STEP 0: Ponytail ladder — resolve at $0 if a known solution exists ===
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from ponytail_ladder import run_ladder
        ladder, _ = run_ladder(prompt, getattr(args, "project_dir", None), None)
    except Exception:
        ladder = {"found": False}
    if ladder.get("found"):
        solution = ladder.get("solution") or f"use existing dep: {ladder.get('dep', '?')}"
        result = {
            "passed": True, "score": GATE_THRESHOLD, "degraded": False,
            "critique": f"Resolved by ponytail ladder at level '{ladder.get('level')}' — no API call needed.",
            "issues": [], "reviewer_model": "ponytail (offline)",
            "review_score": GATE_THRESHOLD, "gate_score": GATE_THRESHOLD,
            "final_score": GATE_THRESHOLD, "type": review_type, "solution": solution,
        }
        print(f"  [solve] ponytail hit ({ladder.get('level')}): {solution[:100]}", file=sys.stderr)
        if getattr(args, "output", None):
            _save_output(solution, args.output)
        record_score(result, review_type, prompt, False)
        print(format_trace(result, review_type, args.json_output))
        if args.json_output:
            pass
        else:
            print(solution)
        sys.exit(0)

    # Immune memory: inject known past failures into generation
    immune = immune_hints(prompt)

    # === STEP 1: GLM pre-design ===
    pre_design, pre_degraded = do_glm_pre_design("", prompt)
    design = ""
    if not pre_degraded:
        design = pre_design.get("approach") or pre_design.get("critique", "")
        print(f"  [solve] pre-design: {str(design)[:120]}", file=sys.stderr)

    # === STEP 2: Kimi generates the draft ===
    print("  [solve] Kimi generating draft...", file=sys.stderr)
    draft = call_openrouter(KIMI_MODEL, KIMI_GENERATE_PROMPT.format(
        prompt=prompt[:4000], design=str(design)[:3000], immune=immune), max_tokens=64000)
    if draft is None:
        r = degraded_result("Kimi generation API unreachable")
        r["type"] = review_type
        record_score(r, review_type, prompt, True)
        print(format_trace(r, review_type, args.json_output))
        sys.exit(1)

    # === STEPS 3-5: review -> revise loop ===
    all_critiques = []
    final = None
    for round_num in range(1, MAX_ROUNDS + 1):
        print(f"  [solve] Round {round_num}/{MAX_ROUNDS}: dual review...", file=sys.stderr)
        r, degraded = do_dual_review(draft, prompt)
        g = run_quality_gate(draft, domain)
        review_score = r.get("score", 0)
        gate_score = g.get("score", 0)
        combined = round((review_score + gate_score) / 2)
        passed = combined >= GATE_THRESHOLD and r.get("passed", False) and g.get("passed", False)

        all_critiques.append({
            "round": round_num,
            "review_score": review_score,
            "gate_score": gate_score,
            "critique": r.get("critique", ""),
            "issues": r.get("issues", []) + g.get("issues", []),
        })

        final = {
            "passed": passed,
            "score": combined,
            "degraded": degraded,
            "critique": r.get("critique", ""),
            "issues": r.get("issues", []) + g.get("issues", []),
            "reviewer_model": r.get("reviewer_model", "dual"),
            "review_score": review_score,
            "gate_score": gate_score,
            "final_score": combined,
            "type": review_type,
            "round": round_num,
        }

        if passed or (degraded and not r.get("passed")):
            break

        if round_num < MAX_ROUNDS:
            # Kimi revises with compressed critiques (headroom keeps prompts lean)
            critique_text, _ = compress_critiques(all_critiques)
            print(f"  [solve] Round {round_num} failed ({combined}/{GATE_THRESHOLD}) — Kimi revising...", file=sys.stderr)
            revised = call_openrouter(KIMI_MODEL, KIMI_REVISE_PROMPT.format(
                prompt=prompt[:4000], draft=draft[:60000],
                critiques=critique_text, immune=immune), max_tokens=64000)
            if revised:
                draft = revised

    # Exhausted without passing -> GLM arbitration synthesizes the best answer
    if final and not final["passed"] and not final.get("degraded"):
        print("  [solve] Rounds exhausted — GLM arbitration...", file=sys.stderr)
        arb, arb_degraded = do_glm_arbitrate(draft, prompt, all_critiques)
        arb["type"] = review_type
        arb["round"] = final["round"]
        if arb.get("glm_synthesized"):
            draft = arb["glm_synthesized"]
        final = arb

    clear_round_state()
    if getattr(args, "output", None):
        _save_output(draft, args.output)
    record_score(final, review_type, prompt, final.get("degraded", False))
    print(format_trace(final, review_type, args.json_output))
    if not args.json_output:
        print(draft)
    sys.exit(0 if final.get("passed") else 1)


# Global cache to prevent double-read of stdin
_DRAFT_CACHE = None

def _read_draft(args):
    """Read draft from --draft, --draft-file, stdin, or temp file. Caches stdin result."""
    global _DRAFT_CACHE
    if _DRAFT_CACHE is not None:
        return _DRAFT_CACHE
    if args.draft:
        _DRAFT_CACHE = args.draft
        return _DRAFT_CACHE
    if args.draft_file:
        try:
            _DRAFT_CACHE = Path(args.draft_file).read_text(encoding="utf-8", errors="replace")
            return _DRAFT_CACHE
        except Exception as e:
            print(f"Error reading draft file: {e}", file=sys.stderr)
            sys.exit(1)
    if DRAFT_FILE.exists():
        try:
            _DRAFT_CACHE = DRAFT_FILE.read_text(encoding="utf-8", errors="replace")
            return _DRAFT_CACHE
        except Exception as e:
            print(f"Warning: DRAFT_FILE exists but unreadable: {e}", file=sys.stderr)
    if not sys.stdin.isatty():
        _DRAFT_CACHE = sys.stdin.read()
        return _DRAFT_CACHE
    print("Error: no draft provided (use --draft, --draft-file, stdin, or /tmp/onklaud-draft.txt)", file=sys.stderr)
    sys.exit(1)


def _save_output(draft, output_path):
    """Save final draft to --output file."""
    if not output_path:
        return
    try:
        Path(output_path).write_text(draft, encoding="utf-8")
        print(f"  [output] Saved to {output_path}", file=sys.stderr)
    except Exception as e:
        print(f"  [output] Failed to save: {e}", file=sys.stderr)


def _finalize_loop(result, degraded, review_type, prompt, args):
    """Finalize loop mode: save output, record score, print result."""
    record_score(result, review_type, prompt, degraded)
    # Save final draft to --output file if specified (use original draft as content)
    if hasattr(args, 'output') and args.output:
        original_draft = _read_draft(args)
        _save_output(original_draft, args.output)
    print(format_trace(result, review_type, args.json_output))


# --- Main ------------------------------------------------------------

def main():
    # Force UTF-8 output on Windows (only if stdout is a tty/pipe, not a file)
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Onklaud 5 Council Orchestrator v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python council.py loop --type code --prompt "fix auth bug" --draft "..."
  echo "my draft" | python council.py loop --type code --prompt "..."
  python council.py loop --type code --prompt "..." --draft-file /tmp/draft.txt
  python council.py full --type architecture --prompt "design system" --draft "..."
  python council.py gate --text "some output" --domain coding --json
"""
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # --- solve (the full generate-review-revise pipeline) ---
    p_solve = sub.add_parser("solve", help="Generate + review + revise: the full council pipeline from a bare prompt")
    p_solve.add_argument("--prompt", required=True, help="The task to solve")
    p_solve.add_argument("--type", choices=["code", "architecture"], default="code")
    p_solve.add_argument("--project-dir", dest="project_dir",
                         help="Project dir for ponytail existing-dep detection")
    p_solve.add_argument("--json", dest="json_output", action="store_true")
    p_solve.add_argument("--output", help="Save the final draft to file")
    p_solve.add_argument("--state-dir", dest="state_dir")

    # --- loop ---
    p_loop = sub.add_parser("loop", help="Full pipeline: GLM pre-design -> dual review -> gate -> GLM arbitration")
    p_loop.add_argument("--type", choices=["code", "architecture"], default="code",
                        help="Review type: code (Kimi+GLM dual) or architecture (GLM)")
    p_loop.add_argument("--prompt", default="Code/architecture task",
                        help="The original user request/context")
    p_loop.add_argument("--draft", help="Draft text to review (takes priority)")
    p_loop.add_argument("--draft-file", help="Read draft from file path")
    p_loop.add_argument("--json", dest="json_output", action="store_true",
                        help="Output full JSON instead of pipeline trace")
    p_loop.add_argument("--output", help="Save final passed draft to file")
    p_loop.add_argument("--state-dir", dest="state_dir",
                        help="Directory for round/issues state (default: per-task dir derived from cwd+prompt)")

    # --- dual (NEW v3.1) ---
    p_dual = sub.add_parser("dual", help="Dual review: Kimi + GLM both review, scores averaged")
    p_dual.add_argument("--type", choices=["code", "architecture"], default="code")
    p_dual.add_argument("--prompt", default="Code/architecture task")
    p_dual.add_argument("--draft")
    p_dual.add_argument("--draft-file")
    p_dual.add_argument("--json", dest="json_output", action="store_true")

    # --- review (legacy) ---
    p_review = sub.add_parser("review", help="Model review only (legacy)")
    p_review.add_argument("--type", choices=["code", "architecture"], default="code")
    p_review.add_argument("--prompt", default="Code/architecture task")
    p_review.add_argument("--draft")
    p_review.add_argument("--draft-file")
    p_review.add_argument("--json", dest="json_output", action="store_true")

    # --- gate (legacy) ---
    p_gate = sub.add_parser("gate", help="Quality gate only (legacy)")
    p_gate.add_argument("--domain", default="general")
    p_gate.add_argument("--text")
    p_gate.add_argument("--json", dest="json_output", action="store_true")

    # --- full (legacy) ---
    p_full = sub.add_parser("full", help="Full council: review + gate (legacy)")
    p_full.add_argument("--type", choices=["code", "architecture"], default="code")
    p_full.add_argument("--prompt", default="Code/architecture task")
    p_full.add_argument("--draft")
    p_full.add_argument("--draft-file")
    p_full.add_argument("--json", dest="json_output", action="store_true")

    # --- status ---
    p_status = sub.add_parser("status", help="Health check: connectivity, scores, immune memory")

    # Set defaults
    for p in [p_loop, p_review, p_gate, p_full, p_status]:
        p.set_defaults(json_output=False, draft_file=None)

    args = parser.parse_args()

    # Validate API key for modes that need it
    modes_need_api = {"loop", "review", "full", "dual", "solve"}
    if args.mode in modes_need_api and not OR_KEY:
        # Allow loop to work in degraded mode even without key
        if args.mode in ("loop", "solve"):
            print("  [warn] No OPENROUTER_API_KEY set -- will use degraded mode", file=sys.stderr)
        else:
            print(json.dumps({"error": "OPENROUTER_API_KEY not set in .env", "passed": False, "score": 0}))
            sys.exit(1)

    if args.mode == "review":
        cmd_review(args)
    elif args.mode == "gate":
        cmd_gate(args)
    elif args.mode == "full":
        cmd_full(args)
    elif args.mode == "loop":
        cmd_loop(args)
    elif args.mode == "dual":
        cmd_dual(args)
    elif args.mode == "solve":
        cmd_solve(args)
    elif args.mode == "status":
        cmd_status(args)


def cmd_status(args):
    """Health check: connectivity, score history, immune memory."""

    print("=" * 50, file=sys.stderr)
    print("  Onklaud 5 Status", file=sys.stderr)
    print("=" * 50, file=sys.stderr)

    # 1. API key
    print(f"  OpenRouter key: {'LOADED' if OR_KEY else 'MISSING'}", file=sys.stderr)
    print(f"  Backup key:     {'LOADED' if OR_KEY_BACKUP else 'MISSING'}", file=sys.stderr)

    # 2. Quick connectivity test
    try:
        from urllib.request import urlopen
        urlopen("https://openrouter.ai/api/v1/models", timeout=10)
        print("  Connectivity:   REACHABLE", file=sys.stderr)
    except Exception as e:
        print(f"  Connectivity:   DOWN ({e})", file=sys.stderr)

    # 3. Score history
    scores = []
    mem = []
    try:
        scores = [json.loads(l) for l in open(SCORES_FILE) if l.strip()]
        if scores:
            passed = [s for s in scores if s.get("passed")]
            avg = sum(s["final_score"] for s in scores) / len(scores)
            last = scores[-1]
            total_cost = sum(s.get("usage", {}).get("cost_usd", 0) for s in scores)
            print(f"  Total runs:     {len(scores)}", file=sys.stderr)
            print(f"  Pass rate:      {len(passed)}/{len(scores)} ({100*len(passed)/len(scores):.0f}%)", file=sys.stderr)
            print(f"  Avg score:      {avg:.1f}/10", file=sys.stderr)
            print(f"  Measured cost:  ${total_cost:.4f} across recorded runs", file=sys.stderr)
            print(f"  Last run:       {last.get('reviewer_model','?').split('/')[-1]} ({last.get('final_score','?')}/10) {'PASS' if last.get('passed') else 'FAIL'}", file=sys.stderr)
        else:
            print("  Scores:         No runs yet", file=sys.stderr)
    except Exception:
        print("  Scores:         No data", file=sys.stderr)

    # 4. Immune memory
    try:
        mem = json.load(open(IMMUNE_FILE))
        if mem:
            print(f"  Immune patterns: {len(mem)} stored", file=sys.stderr)
            for m in mem[:3]:
                print(f"    [{m.get('frequency',1)}x] {m.get('pattern','')[:80]}...", file=sys.stderr)
        else:
            print("  Immune patterns: None (clean slate)", file=sys.stderr)
    except Exception:
        print("  Immune patterns: No data", file=sys.stderr)

    # 5. Temp files
    print(f"  Round file:     {'EXISTS' if ROUND_FILE.exists() else 'CLEAN'}", file=sys.stderr)
    print(f"  Issues file:    {'PENDING' if ISSUES_FILE.exists() else 'CLEAN'}", file=sys.stderr)
    print("=" * 50, file=sys.stderr)

    # JSON output for scripting
    print(json.dumps({
        "api_key": bool(OR_KEY),
        "scores_count": len(scores),
        "last_score": scores[-1]["final_score"] if scores else None,
        "last_passed": scores[-1]["passed"] if scores else None,
        "immune_patterns": len(mem),
        "status": "operational" if OR_KEY else "degraded"
    }))


if __name__ == "__main__":
    main()
