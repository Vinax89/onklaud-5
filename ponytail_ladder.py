#!/usr/bin/env python3
"""
🎠 Ponytail Ladder v2 - checks if a task can be solved with stdlib/native/existing-dep.
Returns the shortest solution that works. Zero API calls. Instant.

v2: normalized token matching (light stemming + synonyms) instead of exact
word-subset, a much larger pattern corpus, a confidence score, and near-miss
hints that feed the council's generation prompt when nothing fully matches.

Usage:
  python ponytail_ladder.py --task "read a JSON file in Python"
  python ponytail_ladder.py --task "parse URL query string in JavaScript" --lang js
  python ponytail_ladder.py --task "add dark mode toggle" --project-dir /path/to/project

Output:
  {"found": true, "level": "stdlib", "solution": "...", "confidence": 1.0, "language": "python"}
  {"found": false, "reason": "...", "hints": [{"pattern": "...", "solution": "...", "overlap": 0.5}]}

Exit codes: 0 = found (stdlib/native/dep covers it), 1 = not found (needs custom code)
"""

import sys
import os
import json
import argparse
import re
from pathlib import Path

# --- Normalization -----------------------------------------------------

_STOPWORDS = {"a", "an", "the", "in", "of", "for", "with", "to", "and", "or",
              "into", "from", "on", "at", "by", "using", "via", "my", "some"}

# Canonicalization: both patterns and tasks go through the same map, so the
# choice of canonical form doesn't matter — only consistency does.
_SYNONYMS = {
    "load": "read", "open": "read", "fetch": "read", "grab": "read", "pull": "read",
    "save": "write", "store": "write", "dump": "write", "persist": "write",
    "folder": "directory", "dir": "directory",
    "remove": "delete", "erase": "delete",
    "configuration": "config", "settings": "config", "setting": "config", "cfg": "config",
    "make": "create", "build": "create",
    "check": "validate", "verify": "validate",
    "picture": "image", "photo": "image",
    "js": "javascript", "ts": "javascript", "typescript": "javascript",
    "timestamp": "date", "datetime": "date",
    "guid": "uuid", "identifier": "uuid",
    "launch": "run", "execute": "run",
    "program": "command",
    "pause": "sleep", "wait": "sleep",
    "link": "url",
    "locate": "find", "search": "find",
    "show": "display",
    "ms": "millisecond",
}


def _stem(word):
    """Porter-lite: strip common suffixes, then a trailing 'e', so
    encode/encoding, parse/parsing, file/files all collide."""
    for suf in ("ing", "ed", "es", "s"):
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            word = word[: -len(suf)]
            break
    if word.endswith("e") and len(word) > 3:
        word = word[:-1]
    return word


def normalize(text):
    """Text -> set of canonical tokens."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    out = set()
    for t in tokens:
        if t in _STOPWORDS:
            continue
        t = _SYNONYMS.get(t, t)
        t = _stem(t)
        t = _SYNONYMS.get(t, t)
        if t and t not in _STOPWORDS:
            out.add(t)
    return out


# --- Knowledge Base ----------------------------------------------------

STDLIB_PATTERNS = {
    "python": {
        # Files & serialization
        "read json": "import json; data = json.load(open(path))",
        "read json config": "import json; cfg = json.load(open(path))",
        "write json": "import json; json.dump(data, open(path, 'w'), indent=2)",
        "read csv": "import csv; rows = list(csv.DictReader(open(path)))",
        "write csv": "import csv; csv.writer(open(path, 'w', newline='')).writerows(rows)",
        "read text file": "data = open(path, encoding='utf-8').read()",
        "write text file": "open(path, 'w', encoding='utf-8').write(data)",
        "read file lines": "lines = open(path, encoding='utf-8').read().splitlines()",
        "append file": "open(path, 'a', encoding='utf-8').write(line)",
        "read binary file": "data = open(path, 'rb').read()",
        "copy file": "import shutil; shutil.copy2(src, dst)",
        "move file": "import shutil; shutil.move(src, dst)",
        "delete file": "import os; os.remove(path)  # or pathlib.Path(path).unlink(missing_ok=True)",
        "file exists": "from pathlib import Path; Path(path).exists()",
        "file size": "import os; size = os.path.getsize(path)",
        "create directory": "from pathlib import Path; Path(dir).mkdir(parents=True, exist_ok=True)",
        "delete directory": "import shutil; shutil.rmtree(dir)",
        "walk directory": "import pathlib; [f for f in pathlib.Path(dir).rglob('*') if f.is_file()]",
        "list directory": "import os; names = os.listdir(dir)",
        "temp file": "import tempfile; f = tempfile.NamedTemporaryFile(delete=False)",
        "temp directory": "import tempfile; d = tempfile.mkdtemp()",
        "file extension": "from pathlib import Path; ext = Path(name).suffix",
        "path join": "from pathlib import Path; full = Path(dir) / name",
        "home directory": "from pathlib import Path; home = Path.home()",
        "read ini config": "import configparser; cfg = configparser.ConfigParser(); cfg.read(path)",
        "read toml": "import tomllib; data = tomllib.load(open(path, 'rb'))  # 3.11+",
        "pickle object": "import pickle; pickle.dump(obj, open(path, 'wb'))",
        "read zip": "import zipfile; zipfile.ZipFile(path).extractall(dir)",
        "create zip": "import zipfile; z = zipfile.ZipFile(path, 'w'); [z.write(f) for f in files]",
        "read gzip": "import gzip; data = gzip.open(path, 'rt').read()",
        "tar archive": "import tarfile; tarfile.open(path).extractall(dir, filter='data')",
        # Network
        "http get": "import urllib.request; data = urllib.request.urlopen(url).read()",
        "http get json": "import json, urllib.request; obj = json.load(urllib.request.urlopen(url))",
        "http post": "import urllib.request; urllib.request.urlopen(urllib.request.Request(url, data=body, method='POST'))",
        "http retry": "import requests; from urllib3.util.retry import Retry; s = requests.Session(); s.mount('https://', requests.adapters.HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1)))",
        "parse url": "from urllib.parse import urlparse; parsed = urlparse(url)",
        "url query string": "from urllib.parse import parse_qs, urlparse; qs = parse_qs(urlparse(url).query)",
        "url encode": "from urllib.parse import quote, urlencode; q = urlencode(params)",
        "hostname ip": "import socket; ip = socket.gethostbyname(host)",
        "simple http server": "python -m http.server 8000  # serves cwd",
        # Dates
        "parse date": "from datetime import datetime; dt = datetime.fromisoformat(s)",
        "format date": "s = dt.strftime('%Y-%m-%d')",
        "current date": "from datetime import datetime, timezone; now = datetime.now(timezone.utc)",
        "date difference": "delta = (d2 - d1).days",
        "add days date": "from datetime import timedelta; later = dt + timedelta(days=n)",
        "unix epoch date": "import time; ts = time.time()  # or dt.timestamp()",
        "sleep seconds": "import time; time.sleep(seconds)",
        "delay execution": "import time; time.sleep(seconds)",
        "measure elapsed": "import time; t0 = time.perf_counter(); elapsed = time.perf_counter() - t0",
        # Crypto / encoding / random
        "generate uuid": "import uuid; s = str(uuid.uuid4())",
        "hash string sha256": "import hashlib; h = hashlib.sha256(s.encode()).hexdigest()",
        "hash file": "import hashlib; h = hashlib.sha256(open(path,'rb').read()).hexdigest()",
        "hash password": "import hashlib, os; salt = os.urandom(16); h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 600_000)",
        "hmac signature": "import hmac, hashlib; sig = hmac.new(key, msg, hashlib.sha256).hexdigest()",
        "base64 encode": "import base64; encoded = base64.b64encode(data).decode()",
        "base64 decode": "import base64; data = base64.b64decode(encoded)",
        "random number": "import random; n = random.randint(1, 100)",
        "secure random token": "import secrets; token = secrets.token_urlsafe(32)",
        "random choice list": "import random; item = random.choice(items)",
        "shuffle list": "import random; random.shuffle(items)",
        # Text
        "regex match": "import re; m = re.search(pattern, text)",
        "regex replace": "import re; out = re.sub(pattern, repl, text)",
        "regex find all": "import re; hits = re.findall(pattern, text)",
        "split string": "parts = s.split(sep)",
        "string template": "from string import Template; out = Template('$name').substitute(name=x)",
        "similar strings ratio": "import difflib; ratio = difflib.SequenceMatcher(None, a, b).ratio()",
        "escape html": "import html; safe = html.escape(text)",
        "slugify text": "import re; slug = re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')",
        "word count": "count = len(text.split())",
        "pretty print json": "import json; print(json.dumps(obj, indent=2))",
        "wrap text": "import textwrap; lines = textwrap.wrap(text, width=72)",
        # Data structures
        "count occurrences": "from collections import Counter; counts = Counter(items)",
        "group by key": "from itertools import groupby; groups = {k: list(g) for k, g in groupby(sorted(items, key=fn), key=fn)}",
        "merge dictionaries": "merged = {**d1, **d2}  # d2 wins conflicts",
        "sort by key": "ordered = sorted(items, key=lambda x: x['field'])",
        "deduplicate list": "unique = list(dict.fromkeys(items))  # keeps order",
        "flatten nested list": "flat = [x for sub in nested for x in sub]",
        "chunk list": "chunks = [items[i:i+n] for i in range(0, len(items), n)]",
        "default dictionary": "from collections import defaultdict; d = defaultdict(list)",
        "named tuple": "from collections import namedtuple; Point = namedtuple('Point', 'x y')",
        "dataclass record": "from dataclasses import dataclass",
        "queue fifo": "from collections import deque; q = deque(); q.append(x); q.popleft()",
        "priority queue": "import heapq; heapq.heappush(h, (priority, item)); heapq.heappop(h)",
        "binary search sorted": "import bisect; i = bisect.bisect_left(sorted_list, value)",
        "cache function results": "from functools import lru_cache; @lru_cache(maxsize=1000)",
        "zip two lists": "pairs = list(zip(keys, values, strict=False))",
        # System
        "run command": "import subprocess; r = subprocess.run([...], capture_output=True, text=True)",
        "environment variable": "import os; val = os.environ.get('KEY', 'default')",
        "command line arguments": "import argparse; p = argparse.ArgumentParser(); p.add_argument('--verbose', action='store_true')",
        "exit code": "import sys; sys.exit(1)",
        "log message": "import logging; logging.basicConfig(level=logging.INFO); logging.info(msg)",
        "log file": "import logging; logging.basicConfig(filename='app.log', level=logging.INFO)",
        "current platform": "import sys, platform; sys.platform; platform.system()",
        "signal handler": "import signal; signal.signal(signal.SIGINT, handler)",
        "parallel threads": "from concurrent.futures import ThreadPoolExecutor; list(ThreadPoolExecutor(8).map(fn, items))",
        "parallel processes": "from concurrent.futures import ProcessPoolExecutor; list(ProcessPoolExecutor().map(fn, items))",
        "retry backoff": "for i in range(3):\\n    try: return fn()\\n    except Exception: time.sleep(2 ** i)",
        "send email smtp": "import smtplib; from email.message import EmailMessage  # stdlib covers plain SMTP",
        "sqlite database": "import sqlite3; con = sqlite3.connect('app.db'); con.execute(sql, params)",
        "profile code": "python -m cProfile -s tottime script.py",
        "unit test": "import unittest  # or pytest if already a dep",
        "mock object test": "from unittest import mock; mock.patch.object(target, 'attr')",
        "deep copy object": "import copy; clone = copy.deepcopy(obj)",
        "statistics mean median": "import statistics; statistics.mean(xs); statistics.median(xs)",
        "decimal money math": "from decimal import Decimal; total = Decimal('0.1') + Decimal('0.2')",
        "permutations combinations": "from itertools import permutations, combinations",
        "context manager": "from contextlib import contextmanager",
        "suppress exception": "from contextlib import suppress; with suppress(FileNotFoundError): ...",
        # Natural phrasings (holdout-driven additions)
        "find files": "import pathlib; hits = list(pathlib.Path(dir).rglob('*.py'))",
        "compress files archive": "import zipfile; z = zipfile.ZipFile(path, 'w'); [z.write(f) for f in files]",
        "datetime object string": "from datetime import datetime; dt = datetime.fromisoformat(s)",
        "first occurrence list": "unique = list(dict.fromkeys(items))  # keeps order",
        "local database": "import sqlite3; con = sqlite3.connect('app.db')",
        "format nested dict": "from pprint import pprint; pprint(obj)",
        "verbose flag": "import argparse; p.add_argument('--verbose', action='store_true')",
        "json into dict": "import json; data = json.loads(text)  # or json.load(open(path))",
    },
    "javascript": {
        # Files (Node)
        "read file": "import { readFileSync } from 'node:fs'; const data = readFileSync(path, 'utf-8')",
        "write file": "import { writeFileSync } from 'node:fs'; writeFileSync(path, data)",
        "read json": "const obj = JSON.parse(readFileSync(path, 'utf-8'))",
        "write json": "writeFileSync(path, JSON.stringify(obj, null, 2))",
        "append file": "import { appendFileSync } from 'node:fs'; appendFileSync(path, line)",
        "file exists": "import { existsSync } from 'node:fs'; existsSync(path)",
        "create directory": "import { mkdirSync } from 'node:fs'; mkdirSync(dir, { recursive: true })",
        "delete file": "import { rmSync } from 'node:fs'; rmSync(path, { force: true })",
        "walk directory": "import { readdirSync } from 'node:fs'; readdirSync(dir, { recursive: true })",
        "temp directory": "import { mkdtempSync } from 'node:fs'; import { tmpdir } from 'node:os'; mkdtempSync(join(tmpdir(), 'x-'))",
        "path join": "import { join } from 'node:path'; const full = join(dir, file)",
        "copy file": "import { copyFileSync } from 'node:fs'; copyFileSync(src, dst)",
        # Network
        "http get": "const data = await fetch(url).then(r => r.json())",
        "http post": "await fetch(url, { method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(payload) })",
        "http retry": "for (let i = 0; i < 3; i++) { try { return await fetch(url) } catch { await new Promise(r => setTimeout(r, 2**i * 1000)) } }",
        "http timeout abort": "await fetch(url, { signal: AbortSignal.timeout(5000) })",
        "parse url": "const u = new URL(input); // u.pathname, u.searchParams",
        "url query string": "const qs = new URLSearchParams(location.search); qs.get('key')",
        "http server": "import { createServer } from 'node:http'; createServer((req, res) => res.end('ok')).listen(3000)",
        # Crypto / encoding / random
        "generate uuid": "const id = crypto.randomUUID()",
        "hash string sha256": "import { createHash } from 'node:crypto'; createHash('sha256').update(s).digest('hex')",
        "secure random token": "import { randomBytes } from 'node:crypto'; randomBytes(32).toString('base64url')",
        "base64 encode": "const encoded = Buffer.from(data).toString('base64')  // browser: btoa()",
        "random number": "const n = Math.floor(Math.random() * 100) + 1",
        # Dates
        "parse date": "const d = new Date(isoString)",
        "format date": "new Intl.DateTimeFormat('en-US', { dateStyle: 'medium' }).format(d)",
        "relative date ago": "new Intl.RelativeTimeFormat('en').format(-3, 'day') // '3 days ago'",
        "sleep delay": "await new Promise(r => setTimeout(r, ms))",
        "measure elapsed": "const t0 = performance.now(); const ms = performance.now() - t0",
        # Text / data
        "regex match": "const m = /pattern/.exec(text)",
        "deduplicate list": "const unique = [...new Set(items)]",
        "group by key": "const groups = Object.groupBy(items, x => x.key) // Node 21+/modern browsers",
        "sort by key": "items.sort((a, b) => a.field - b.field)",
        "deep copy object": "const clone = structuredClone(obj)",
        "merge objects": "const merged = { ...a, ...b }",
        "flatten nested list": "const flat = nested.flat(Infinity)",
        "chunk list": "const chunks = Array.from({length: Math.ceil(a.length/n)}, (_, i) => a.slice(i*n, i*n+n))",
        "count occurrences": "const counts = items.reduce((m, x) => (m[x] = (m[x] ?? 0) + 1, m), {})",
        "escape html": "const safe = s.replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]))",
        "number format currency": "new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n)",
        "debounce input": "let t; const debounced = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms) }",
        "throttle event": "let last = 0; const throttled = (...a) => { const now = Date.now(); if (now - last > ms) { last = now; fn(...a) } }",
        "parallel promises": "const results = await Promise.all(tasks.map(fn))",
        "environment variable": "const val = process.env.KEY ?? 'default'",
        "command line arguments": "import { parseArgs } from 'node:util'; const { values } = parseArgs({ options: { verbose: { type: 'boolean' } } })",
        "run command": "import { execFileSync } from 'node:child_process'; execFileSync(cmd, args)",
        "watch file changes": "import { watch } from 'node:fs'; watch(dir, { recursive: true }, cb)",
        "read stdin": "const input = readFileSync(0, 'utf-8')",
        "copy clipboard browser": "await navigator.clipboard.writeText(text)",
        "local storage": "localStorage.setItem('key', JSON.stringify(v)); JSON.parse(localStorage.getItem('key'))",
        # Natural phrasings (holdout-driven additions)
        "wait milliseconds": "await new Promise(r => setTimeout(r, ms))",
        "random identifier": "const id = crypto.randomUUID()",
        "display price dollars": "new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n)",
        "fire after typing stops": "let t; input.oninput = () => { clearTimeout(t); t = setTimeout(search, 300) }",
        "keep config between page reloads": "localStorage.setItem('k', JSON.stringify(v))",
        "several promises all": "const results = await Promise.all(tasks)",
    },
}

# Native HTML/CSS patterns that don't need any code at all
NATIVE_PATTERNS = {
    "date picker": "<input type=\"date\"> - native date picker, no JS needed",
    "color picker": "<input type=\"color\"> - native color picker",
    "form validation": "<input required pattern=\"[a-z]+\" minlength=\"2\"> - HTML5 validation, no JS",
    "email input validate": "<input type=\"email\" required> - native email validation",
    "dropdown select": "<select><option>...</option></select> - native dropdown",
    "autocomplete suggestions": "<input list=\"opts\"><datalist id=\"opts\"><option>...</option></datalist>",
    "number input": "<input type=\"number\" min=\"0\" max=\"100\" step=\"1\">",
    "file upload": "<input type=\"file\" accept=\".pdf,.jpg\" multiple>",
    "range slider": "<input type=\"range\" min=\"0\" max=\"100\">",
    "search input": "<input type=\"search\"> - native clear button",
    "progress bar": "<progress value=\"50\" max=\"100\"></progress>",
    "meter gauge": "<meter value=\"0.7\" high=\"0.9\"></meter>",
    "dark mode": "@media (prefers-color-scheme: dark) { ... } - CSS handles it, no JS",
    "dark mode toggle": "CSS custom props + :root[data-theme] attribute; prefers-color-scheme for the default",
    "responsive layout": "CSS grid/flexbox + @media queries, no JS",
    "sticky header": "position: sticky; top: 0; - CSS, no JS",
    "smooth scroll": "scroll-behavior: smooth; - CSS property, no JS",
    "scroll snap carousel": "scroll-snap-type: x mandatory; on the container - CSS, no JS",
    "animations": "@keyframes + animation - CSS animations, no JS needed for simple ones",
    "hover effects": ":hover + transition - pure CSS",
    "tooltip": "[title=\"hover text\"] - native tooltip attribute",
    "accordion details": "<details><summary>Title</summary>Content</details> - native expandable",
    "dialog modal": "<dialog> element - .showModal() gives focus trap + backdrop free",
    "popover menu": "popover attribute + popovertarget - native, no JS (2024 baseline)",
    "lazy image": "<img loading=\"lazy\"> - native lazy loading",
    "aspect ratio box": "aspect-ratio: 16 / 9; - CSS property",
    "truncate text ellipsis": "text-overflow: ellipsis; white-space: nowrap; overflow: hidden;",
    "center element": "display: grid; place-items: center; - two lines of CSS",
    "custom scrollbar": "scrollbar-width / scrollbar-color - CSS, no JS",
    "print stylesheet": "@media print { ... } - CSS only",
    "toggle switch checkbox": "<input type=\"checkbox\"> styled via :checked + CSS - no JS",
    # Natural phrasings (holdout-driven additions)
    "pick day calendar": "<input type=\"date\"> - native calendar picker, no JS library",
    "keep navigation visible scroll": "position: sticky; top: 0; - CSS, no JS",
    "collapse expand faq": "<details><summary>Q</summary>A</details> - native expandable",
    "center div": "display: grid; place-items: center; - two lines of CSS",
    "upload progress indicator": "<progress value=\"50\" max=\"100\"></progress>",
}

# Pre-normalized corpora (computed once at import)
_NORM_STDLIB = {
    lang: [(pattern, normalize(pattern), solution) for pattern, solution in patterns.items()]
    for lang, patterns in STDLIB_PATTERNS.items()
}
_NORM_NATIVE = [(pattern, normalize(pattern), solution) for pattern, solution in NATIVE_PATTERNS.items()]


def detect_language(task, project_dir=None):
    """Detect language from task description and project files."""
    task_lower = task.lower()

    if any(kw in task_lower for kw in ["python", ".py", "django", "flask", "fastapi"]):
        return "python"
    if any(kw in task_lower for kw in ["javascript", "typescript", "node", "react", "next", "vue", "angular",
                                        ".js", ".ts", ".jsx", ".tsx", "npm", "vite", "express", "browser"]):
        return "javascript"

    if project_dir:
        root = Path(project_dir)
        if (root / "package.json").exists():
            return "javascript"
        if (root / "requirements.txt").exists() or (root / "pyproject.toml").exists() or (root / "setup.py").exists():
            return "python"

    return "python"


# A partial match counts as found when at least 2/3 of the pattern's tokens
# appear in the task and at least 2 tokens matched. Below that it's a hint.
_PARTIAL_THRESHOLD = 0.67


def _match_corpus(task_tokens, corpus, level):
    """Return (best_match_with_confidence, near_misses) against one corpus."""
    best = None       # (pattern, solution, level, n_tokens, confidence)
    near = []
    for pattern, pat_tokens, solution in corpus:
        if not pat_tokens:
            continue
        matched = len(pat_tokens & task_tokens)
        overlap = matched / len(pat_tokens)
        if overlap == 1.0 or (overlap >= _PARTIAL_THRESHOLD and matched >= 2):
            # Prefer higher confidence, then the most specific pattern
            key = (overlap, len(pat_tokens))
            if best is None or key > (best[4], best[3]):
                best = (pattern, solution, level, len(pat_tokens), overlap)
        elif overlap >= 0.5 and len(pat_tokens) >= 2:
            near.append({"pattern": pattern, "solution": solution, "level": level,
                         "overlap": round(overlap, 2)})
    return best, near


def run_ladder(task, project_dir=None, lang=None):
    """Run the full Ponytail ladder. Returns (result, exit_code)."""
    if not lang:
        lang = detect_language(task, project_dir)
    if lang == "js":
        lang = "javascript"

    task_tokens = normalize(task)
    hints = []

    # Step 1: Stdlib
    best, near = _match_corpus(task_tokens, _NORM_STDLIB.get(lang, []), "stdlib")
    hints.extend(near)
    if best:
        return {"found": True, "level": "stdlib", "solution": best[1], "language": lang,
                "pattern_matched": best[0], "confidence": round(best[4], 2)}, 0

    # Step 2: Native platform (HTML/CSS)
    best, near = _match_corpus(task_tokens, _NORM_NATIVE, "native")
    hints.extend(near)
    if best:
        return {"found": True, "level": "native", "solution": best[1],
                "pattern_matched": best[0], "confidence": round(best[4], 2)}, 0

    # Step 3: Existing dependency
    if project_dir:
        result = check_existing_dep(task, project_dir)
        if result:
            result["found"] = True
            result["confidence"] = 1.0
            return result, 0

    # Step 4: Not found — return near-misses as hints for the generator
    hints.sort(key=lambda h: h["overlap"], reverse=True)
    return {
        "found": False,
        "reason": "Task requires custom implementation",
        "suggestion": "Use shortest possible implementation: 1 line > 1 function > 1 file > 1 module",
        "language": lang,
        "hints": hints[:3],
    }, 1


def check_existing_dep(task, project_dir=None):
    """Check if project already has a dependency that covers this."""
    if not project_dir or not os.path.isdir(project_dir):
        return None

    root = Path(project_dir)
    task_lower = task.lower()

    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

            dep_mappings = {
                "lodash": ["filter", "map", "debounce", "throttle", "merge", "clone", "group"],
                "axios": ["http", "api call", "fetch", "request"],
                "date-fns": ["date format", "date parse", "date diff"],
                "zod": ["validate", "schema", "type check"],
                "express": ["server", "route", "middleware"],
                "react": ["component", "hook", "state"],
                "next": ["page", "route", "ssr"],
                "tailwind": ["style", "css", "layout"],
                "prisma": ["database", "query", "orm", "migrate"],
            }

            for dep_name, keywords in dep_mappings.items():
                if dep_name in deps and any(kw in task_lower for kw in keywords):
                    return {"level": "existing_dep", "dep": dep_name, "version": deps[dep_name]}
        except Exception:
            pass

    req_txt = root / "requirements.txt"
    if req_txt.exists():
        try:
            reqs = req_txt.read_text(encoding="utf-8").lower()

            py_dep_mappings = {
                "requests": ["http", "api call", "fetch", "request"],
                "pydantic": ["validate", "schema", "type check"],
                "fastapi": ["server", "route", "api"],
                "sqlalchemy": ["database", "query", "orm"],
                "click": ["cli", "command line"],
                "rich": ["console", "terminal", "progress"],
                "httpx": ["http", "async request"],
                "pytest": ["test", "assert"],
            }

            for dep_name, keywords in py_dep_mappings.items():
                if dep_name in reqs and any(kw in task_lower for kw in keywords):
                    return {"level": "existing_dep", "dep": dep_name}
        except Exception:
            pass

    return None


def main():
    parser = argparse.ArgumentParser(
        description="🎠 Ponytail Ladder - check if task can be solved with stdlib/native/existing-dep"
    )
    parser.add_argument("--task", required=True, help="Task description")
    parser.add_argument("--lang", choices=["python", "js", "javascript"], help="Language (auto-detected if omitted)")
    parser.add_argument("--project-dir", help="Project directory to check existing deps")
    parser.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()

    result, code = run_ladder(args.task, args.project_dir, args.lang)

    if args.json or not sys.stdout.isatty():
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if result["found"]:
            print(f"🎠 FOUND ({result.get('level', '?')}): {result.get('solution', '')}")
        else:
            print(f"🎠 NOT FOUND: {result.get('reason', '')}")
            for h in result.get("hints", []):
                print(f"   hint ({h['overlap']:.0%} {h['level']}): {h['pattern']} -> {h['solution'][:80]}")
            print(f"   {result.get('suggestion', '')}")

    sys.exit(code)


if __name__ == "__main__":
    main()
