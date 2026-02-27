# Project: Newsletter Summarizer Pipeline

## What This Project Does
Automatically fetches, summarizes, audits, and emails daily newsletter digests:
- **Stratechery** (via RSS feed) → `summarize.py`
- **Lenny's Newsletter** (via Gmail API) → `lenny.py`

Runs on GitHub Actions daily at 8am ET (weekdays). Can be triggered manually via `workflow_dispatch`.

## File Map

| File | Role |
|------|------|
| `summarize.py` | Stratechery: fetch RSS → GPT-4o summary → audit → send email |
| `lenny.py` | Lenny's: fetch Gmail → classify → GPT-4o summary → audit (articles only) → send email |
| `auth.py` | One-time local OAuth flow to generate `token.json` |
| `src/agents/reviewer.py` | Reviewer Agent (GPT-4o, temp=0) — audits draft summaries against 5 rules |
| `tests/test_reviewer.py` | LLM-as-a-Judge eval suite (GPT-4o-mini) — 6 parametrized test cases |
| `tests/conftest.py` | Pytest fixtures: `mock_article`, `mock_finance`, `openai_client` |
| `tests/fixtures/eval_dataset.json` | 6 eval scenarios (TC-01 through TC-06) |
| `tests/fixtures/mock_article.txt` | Raw article used as `raw_article` in all eval tests |
| `tests/fixtures/mock_finance.json` | YFinance data used as `yfinance_data` in all eval tests |
| `.github/workflows/summarize.yml` | GitHub Actions: runs both scripts daily |

## Required Secrets (GitHub + local .env)

| Variable | Used By |
|----------|---------|
| `OPENAI_API_KEY` | Both scripts + tests |
| `STRATECHERY_FEED` | `summarize.py` only (RSS URL with auth token) |
| `TO_EMAIL` | Both scripts (recipient email address) |
| `GMAIL_TOKEN_JSON` | Both scripts (OAuth token JSON as a string) |

**Local testing (PowerShell):**
```powershell
$env:OPENAI_API_KEY = "sk-..."
$env:STRATECHERY_FEED = "https://..."
$env:TO_EMAIL = "you@example.com"
$env:GMAIL_TOKEN_JSON = (Get-Content token.json -Raw)
```

## Running Locally

```bash
# List recent Stratechery articles (requires STRATECHERY_FEED)
python summarize.py --list

# Summarize a specific article by index (0 = most recent)
python summarize.py --index 2

# Run Lenny (processes emails from last 25 hours)
python lenny.py

# Run the reviewer eval suite (requires OPENAI_API_KEY)
pytest tests/test_reviewer.py -v
```

## Lenny Email Classification

| Sender | Action |
|--------|--------|
| `lenny+how-i-ai@substack.com` | Skip entirely |
| `lenny+community-wisdom@substack.com` | Community Wisdom format (table extraction) |
| `lenny@substack.com` | Normal article format |

Community wisdom emails skip the audit entirely — the reviewer rules don't apply to table output.

## Audit Loop (both scripts)

1. Generate draft summary (GPT-4o)
2. Run `audit()` from `src/agents/reviewer.py`
3. If FAIL: revise summary using critique, then re-audit
4. If still FAIL after revision: prefix subject with `⚠ Audit failed: `
5. Send email

`yfinance_data` field mapping: `fetch_financials()` returns `{market_cap}` but `ReviewInput` expects `{market_cap_usd}` — mapped inline before calling `audit()`.

## Reviewer Agent — 5 Rules

### Rule 1 — Private Company Financials
4-step decision tree (evaluate in order, stop at first match):
1. **Stock market metric** (market cap, stock price, P/E, EV) for a private company → VIOLATION always
2. **Qualitative statement with no specific number** → NOT a violation
3. **Specific figure with a named source** (person or publication) → NOT a violation
4. **Specific figure with no named source** → VIOLATION

Companies in the YFinance block (e.g. GOOGL, META) are verified public — Rule 1 never applies to them.

### Rule 2 — Banned Phrases
Only three categories:
- `"In the ever-evolving landscape"`
- `"delve into"` / `"delves into"` / `"delving into"` — always banned regardless of context
- `"the author argues"` / `"the piece explores"` / `"the article discusses"` / `"the author discusses"`

**Critical carve-out:** The third category bans only anonymous references. Named attributions are NEVER banned:
- ✓ `"Thompson argues..."` — NOT banned (named person)
- ✓ `"Seufert concludes..."` — NOT banned (named person)
- ✗ `"the author argues..."` — BANNED

### Rule 3 — List Length
Any single list with more than 5 items → VIOLATION. Instruct compression to max 3 items.

### Rule 4 — POV Attribution
Cross-reference every attributed argument against the raw article. Flag misattribution (wrong person credited). Correction must name who is wrongly credited AND who actually said it per the raw article.

### Rule 5 — Guest Context
Any person introduced must have full name + title + company on first mention.
- ✗ `"A senior OpenAI leader..."` — VIOLATION
- ✓ `"Fidji Simo, CEO of Applications at OpenAI..."` — correct

**Exception:** If the raw article itself only uses a description (not a name), the draft may use the same description — not a violation.

## Eval Suite Structure (tests/test_reviewer.py)

| TC | Rule Tested | Expected | Keyword |
|----|-------------|----------|---------|
| TC-01 | Rule 1 (private financials) | FAIL | `private` |
| TC-02 | Rule 2 (banned phrases) | FAIL | `BANNED` |
| TC-03 | Rule 3 (list length) | FAIL | `LIST` |
| TC-04 | Rule 4 (POV misattribution) | FAIL | `Seufert` |
| TC-05 | Rule 5 (guest context) | FAIL | `GUEST` |
| TC-06 | Clean pass (no violations) | PASS | _(empty)_ |

Judge scores 3 binary criteria per test: `defect_catch_rate`, `false_positive_rate`, `critique_actionability`. All three must be 1 for the test to pass.

## Gmail OAuth Notes
- `auth.py` generates `token.json` locally via browser OAuth
- Required scopes: `gmail.send` + `gmail.readonly`
- After re-running `auth.py`, copy new `token.json` contents into the `GMAIL_TOKEN_JSON` GitHub Secret
