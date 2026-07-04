# Onklaud 5 - Quick Start Guide

Get Onklaud 5 running in 5 minutes.

## Step 1: Get an OpenRouter API Key

1. Go to https://openrouter.ai/
2. Sign up (free, no credit card required for trial credits)
3. Go to https://openrouter.ai/keys
4. Create a new key
5. Copy it (starts with `sk-or-v1-`)

## Step 2: Install

```bash
git clone https://github.com/Vinax89/onklaud-5.git
cd onklaud-5
pip install -r requirements.txt
```

Python 3.10+ required.

## Step 3: Configure

```bash
cp .env.example .env
```

Edit `.env` and paste your key:
```
OPENROUTER_API_KEY=sk-or-v1-your-actual-key-here
```

## Step 4: Verify

```bash
# Offline test suite (no API cost)
python test_pipeline.py
# Expected: 0 failed (the exact total varies with file count; API-key tests warn and skip)

# Check council status
python council.py status
```

## Step 5: Solve a Task (the full pipeline)

```bash
# The council generates, reviews, revises, and gates its own answer
python council.py solve --prompt "build an HTTP client with retry logic"

# Review an existing draft instead
python council.py loop --type code \
  --prompt "Review this for bugs and edge cases" \
  --draft-file path/to/your/code.py
```

## Step 6: Try Ponytail (0 cost)

```bash
# These are FREE - no API calls
python ponytail_ladder.py --task "read a JSON config file" --json
python ponytail_ladder.py --task "give every record a unique identifier" --json
python ponytail_ladder.py --task "dark mode toggle" --json
```

## Step 7: Immune Pre-Check

```bash
# Before coding, check against past failures (builds up as the council runs)
python pre_check.py --task "write an HTTP retry function" --json
```

## Cost Overview

| Task | API Calls | Cost |
|------|-----------|------|
| Ponytail check | 0 | $0 |
| Pre-check / gates | 0 | $0 |
| Dual review (code) | 2 calls (Kimi + GLM, parallel) | ~$0.005 |
| Full solve pipeline | 4-8 calls | ~$0.01-0.03 |

Real per-run token usage and cost are logged to `scores.jsonl` and shown in
`python council.py status` — quote those, not this table.

## Troubleshooting

**"OPENROUTER_API_KEY not set"**
- Did you copy .env.example to .env?
- Check the key starts with `sk-or-v1-`

**"OpenRouter unreachable"**
- Check your internet connection
- Verify your key at https://openrouter.ai/keys
- Free tier has rate limits; wait a minute and retry

**"No draft provided"**
- Use `--draft-file path/to/file` or pipe via stdin (loop mode)
- solve mode needs only `--prompt`

**Windows encoding errors**
- All scripts use UTF-8. If you see encoding errors, run:
  `chcp 65001` in your terminal before running Python
