"""Quality gate v2 — offline, zero API calls.

v1 scored by keyword presence ("try" anywhere = error handling, `print(` =
dead code) which made 10/10 unreachable for real Python and gameable by
keyword-stuffed prose. v2 extracts code blocks and runs real static analysis
(ast.parse + AST lints for Python, node --check for JS when node is on PATH).
Prose-only gates remain for architecture reviews.

Usage: python quality_gate.py '<text_to_score>' [domain]   (or import score_output)
"""
import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile


class Gate:
    def __init__(self, name, severity, check_fn, domain="all"):
        self.name = name
        self.severity = severity
        self.check = check_fn
        self.domain = domain


GATE_REGISTRY = []


def gate(name, severity, domain="all"):
    def decorator(fn):
        GATE_REGISTRY.append(Gate(name, severity, fn, domain))
        return fn
    return decorator


class GateResult:
    def __init__(self, name, passed, issues):
        self.name = name
        self.passed = passed
        self.issues = issues

    def to_dict(self):
        return {"name": self.name, "passed": self.passed, "issues": self.issues}


# --- Code extraction ---------------------------------------------------

_PY_LANGS = {"python", "py", ""}
_JS_LANGS = {"js", "javascript", "ts", "typescript", "jsx", "tsx", "mjs"}
_CODE_STARTS = ("def ", "class ", "import ", "from ", "#!", "async def",
                "function ", "const ", "let ", "var ", "//", "export ")


def extract_code_blocks(text):
    """Return [(lang, code)]. Fenced blocks if present; else the whole text
    when it plainly looks like source code."""
    blocks = re.findall(r"```(\w*)[ \t]*\n(.*?)```", text, re.DOTALL)
    if blocks:
        return [(lang.lower(), code) for lang, code in blocks]
    stripped = text.strip()
    if stripped.startswith(_CODE_STARTS) or re.search(r"^\s*(def|class|import|from)\s", stripped, re.M):
        lang = "js" if re.search(r"\b(function|const|=>|console\.)\b", stripped) and \
                       not re.search(r"^\s*(def|import|from)\s", stripped, re.M) else "python"
        return [(lang, text)]
    return []


def _python_asts(output, ctx):
    """Parse python blocks once per scoring run; cache in ctx."""
    if "_py" not in ctx:
        trees = []
        for lang, code in extract_code_blocks(output):
            if lang in _PY_LANGS:
                try:
                    trees.append((code, ast.parse(code), None))
                except SyntaxError as e:
                    trees.append((code, None, e))
        ctx["_py"] = trees
    return ctx["_py"]


def _js_blocks(output):
    return [code for lang, code in extract_code_blocks(output) if lang in _JS_LANGS]


# --- BLOCKER gates (3x weight) ----------------------------------------

@gate("SyntaxValid", "blocker", "coding")
def chk_syntax(output, ctx):
    issues = []
    for _code, tree, err in _python_asts(output, ctx):
        if err is not None:
            issues.append(f"Python syntax error: {err.msg} (line {err.lineno})")
    node = shutil.which("node")
    if node:
        for code in _js_blocks(output):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                f.write(code)
                path = f.name
            try:
                r = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=10)
                if r.returncode != 0:
                    issues.append(f"JS syntax error: {(r.stderr or '').strip()[:150]}")
            except Exception:
                pass
    return GateResult("SyntaxValid", len(issues) == 0, issues)


@gate("ErrorHandling", "blocker", "coding")
def chk_error_handling(output, ctx):
    """Code that does I/O or async work must have real error handling — an
    actual try statement in the AST, not the word 'try' somewhere in prose."""
    issues = []
    RISKY = ("open(", "urlopen", "requests.", "subprocess", "socket", "connect(")
    for code, tree, _err in _python_asts(output, ctx):
        if tree is None:
            continue
        risky = any(tok in code for tok in RISKY) or "await " in code
        has_try = any(isinstance(n, ast.Try) for n in ast.walk(tree))
        raises = any(isinstance(n, ast.Raise) for n in ast.walk(tree))
        if risky and not has_try and not raises:
            issues.append("I/O or async code without try/except or raise")
    for code in _js_blocks(output):
        if ("await " in code or "fetch(" in code) and "try" not in code and ".catch" not in code:
            issues.append("JS async code without try/catch or .catch")
    return GateResult("ErrorHandling", len(issues) == 0, issues)


@gate("EdgeCases", "blocker", "coding")
def chk_edge_cases(output, ctx):
    """Pass when code actually guards against bad input (raise / except /
    None checks / emptiness checks), or when prose discusses edge cases."""
    trees = _python_asts(output, ctx)
    for code, tree, _err in trees:
        if tree is None:
            continue
        guarded = any(isinstance(n, (ast.Raise, ast.Try, ast.Assert)) for n in ast.walk(tree))
        if guarded or re.search(r"\bis (not )?None\b|\bif not \w", code):
            return GateResult("EdgeCases", True, [])
    if not trees and not _js_blocks(output):
        # Prose: give credit for discussing edge conditions
        found = sum(1 for kw in ("null", "empty", "invalid", "edge", "boundary", "none")
                    if kw in output.lower())
        if found >= 1:
            return GateResult("EdgeCases", True, [])
        return GateResult("EdgeCases", False, ["No edge case handling or discussion found"])
    return GateResult("EdgeCases", False, ["Code has no input guards (raise/except/None checks)"])


@gate("ExcellenceThreshold", "blocker")
def chk_excellence_threshold(output, ctx):
    issues = []
    if len(output) < 100:
        issues.append(f"Output too short ({len(output)} chars) - insufficient depth")
    if output.count("\n") < 3:
        issues.append("Output lacks structure - use paragraphs/sections")
    return GateResult("ExcellenceThreshold", len(issues) == 0, issues)


@gate("FailureModes", "blocker", "architecture")
def chk_failure_modes(output, ctx):
    issues = []
    fm_kw = ["failure", "failover", "retry", "circuit breaker", "dead letter", "dlq",
             "timeout", "degraded", "fallback", "redundancy", "graceful"]
    found = sum(1 for kw in fm_kw if kw in output.lower())
    if found < 2:
        issues.append("Architecture should address failure modes (retry, circuit breaker, graceful degradation)")
    return GateResult("FailureModes", found >= 2, issues)


# --- CRITICAL gates (2x weight) ----------------------------------------

@gate("BareExcept", "critical", "coding")
def chk_bare_except(output, ctx):
    issues = []
    for _code, tree, _err in _python_asts(output, ctx):
        if tree is None:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.ExceptHandler) and n.type is None:
                issues.append(f"Bare 'except:' at line {n.lineno} swallows all errors incl. KeyboardInterrupt")
    return GateResult("BareExcept", len(issues) == 0, issues)


@gate("MutableDefaults", "critical", "coding")
def chk_mutable_defaults(output, ctx):
    issues = []
    for _code, tree, _err in _python_asts(output, ctx):
        if tree is None:
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in n.args.defaults:
                    if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                        issues.append(f"Mutable default argument in {n.name}() at line {n.lineno}")
    return GateResult("MutableDefaults", len(issues) == 0, issues)


@gate("DebugArtifacts", "critical", "coding")
def chk_debug_artifacts(output, ctx):
    issues = []
    for code, _tree, _err in _python_asts(output, ctx):
        for pat in ("breakpoint()", "pdb.set_trace()"):
            if pat in code:
                issues.append(f"Debug artifact left in code: {pat}")
    for code in _js_blocks(output):
        for pat in ("debugger;", "console.log("):
            if pat in code:
                issues.append(f"Debug artifact left in code: {pat}")
                break
    return GateResult("DebugArtifacts", len(issues) == 0, issues)


@gate("DeadCode", "critical", "coding")
def chk_dead_code(output, ctx):
    issues = []
    for _lang, code in extract_code_blocks(output):
        if "TODO" in code or "FIXME" in code:
            issues.append("Contains TODO/FIXME - either implement or remove")
            break
    return GateResult("DeadCode", len(issues) == 0, issues)


@gate("DRY", "critical", "coding")
def chk_dry(output, ctx):
    issues = []
    for _lang, code in extract_code_blocks(output):
        lines = code.split("\n")
        significant = [l.strip() for l in lines
                       if len(l.strip()) > 40 and not re.match(r'^[{}()\[\];,.\s]+$', l.strip())]
        if len(significant) > 10:
            seen = {}
            for i, line in enumerate(significant):
                normalized = re.sub(r'\s+', '', line)
                if normalized in seen:
                    issues.append(f"Potential duplication: line {seen[normalized]+1} and {i+1}")
                    break
                seen[normalized] = i
    return GateResult("DRY", len(issues) == 0, issues)


@gate("TypeSafety", "critical", "coding")
def chk_type_safety(output, ctx):
    issues = []
    for code in _js_blocks(output):
        if len(re.findall(r":\s*any\b", code)) >= 2:
            issues.append("Multiple ': any' annotations - prefer stricter TS types")
    return GateResult("TypeSafety", len(issues) == 0, issues)


@gate("ScalingStrategy", "critical", "architecture")
def chk_scaling(output, ctx):
    issues = []
    scale_kw = ["scale", "shard", "partition", "replica", "horizontal", "vertical",
                "throughput", "bottleneck", "load balanc"]
    found = sum(1 for kw in scale_kw if kw in output.lower())
    if found < 2:
        issues.append("Architecture should discuss scaling strategy")
    return GateResult("ScalingStrategy", found >= 2, issues)


@gate("Tradeoffs", "critical", "architecture")
def chk_tradeoffs(output, ctx):
    issues = []
    trade_kw = ["tradeoff", "trade-off", "cost", "latency vs", "consistency vs",
                "however", "downside", "limitation", "caveat"]
    found = sum(1 for kw in trade_kw if kw in output.lower())
    if found < 1:
        issues.append("Architecture should mention tradeoffs (every decision has a cost)")
    return GateResult("Tradeoffs", found >= 1, issues)


# --- WARNING gates (1x weight) -----------------------------------------

@gate("Clarity", "warning")
def chk_clarity(output, ctx):
    issues = []
    if "```" in output and output.count("```") % 2 != 0:
        issues.append("Unclosed code block")
    if "http://" in output:
        issues.append("Uses HTTP (prefer HTTPS)")
    return GateResult("Clarity", len(issues) == 0, issues)


SEVERITY_WEIGHT = {"blocker": 3, "critical": 2, "warning": 1}


def score_output(output, domain="general"):
    ctx = {"domain": domain}
    applicable = [g for g in GATE_REGISTRY if g.domain in (domain, "all")]
    results = []
    total_weight = 0
    weighted_sum = 0
    for g in applicable:
        result = g.check(output, ctx)
        results.append(result)
        w = SEVERITY_WEIGHT.get(g.severity, 1)
        total_weight += w
        if result.passed:
            weighted_sum += w
    if total_weight == 0:
        return {"score": 10, "passed": True, "gates": [], "issues": []}
    raw_score = (weighted_sum / total_weight) * 10
    score = round(raw_score)
    passed = score >= 10
    all_issues = []
    for r in results:
        all_issues.extend(r.issues)
    return {"score": score, "passed": passed, "gates": [r.to_dict() for r in results], "issues": all_issues}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"score": 0, "passed": False, "error": "Usage: python quality_gate.py '<text_to_score>' [domain]"}))
        sys.exit(1)
    domain = sys.argv[2] if len(sys.argv) > 2 else "general"
    result = score_output(sys.argv[1], domain)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["passed"] else 1)
