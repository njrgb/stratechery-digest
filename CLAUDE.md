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
| `src/agents/reviewer.py` | Reviewer Agent (GPT-4o, temp=0) — audits draft summaries against 5 rules; Rules 1, 3, 5 handled by deterministic code; Rule 4 uses structured scratchpad output with Python-level false-positive filters |
| `tests/test_reviewer.py` | LLM-as-a-Judge eval suite (GPT-4o) — 6 parametrized test cases |
| `tests/conftest.py` | Pytest fixtures: `mock_article`, `mock_financial_block`, `openai_client` |
| `tests/fixtures/eval_dataset.json` | 6 eval scenarios (TC-01 through TC-06) |
| `tests/fixtures/mock_article.txt` | Raw article used as `raw_article` in all eval tests |
| `tests/fixtures/mock_finance.json` | Financial block text used as `financial_block` in all eval tests |
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

# Dry run — summarize + audit loop but skip sending email
python summarize.py --dry-run --index 0

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

`audit()` accepts `financial_block: str` (pre-formatted text). `summarize.py` passes `financial_block=financial_block` directly; `lenny.py` passes `financial_block=''`.

`send_email()` in `summarize.py` accepts `raw_article` and `financial_block`, and appends a **VERIFY FACTS** table before the footer showing each currency figure with its source sentence (or "Not found in source" in bold red).

## Reviewer Agent — 5 Rules

### Rule 1 — Private Company Financials
**Handled by deterministic code (`_check_currency_figures`) — NOT by the LLM.**
Scans the summary for currency figures and cross-references them against `financial_block`. Cross-scale matching is supported ($2.31T = $2310B) with a 1.5% rounding tolerance.

Decision logic (evaluate in order, stop at first match):
1. **Stock market metric** (market cap, stock price, P/E, EV) for a private company → VIOLATION always
2. **Qualitative statement with no specific number** → NOT a violation
3. **Specific figure with a named source** (person or publication) → NOT a violation
4. **Specific figure with no named source** → VIOLATION

Companies in the financial block are verified public — Rule 1 never applies to them. This exemption extends to all divisions/brands of those companies (e.g. Xbox → MSFT, YouTube → GOOGL).

Rule 1 Step 3 — compound sentences: `"According to [person], $X... $Y... $Z"` covers ALL figures in that sentence. Do not treat later clauses as unattributed.

### Rule 2 — Banned Anonymous Article References
**Checked by the LLM.**
Banned phrases: `"the author argues"` / `"the piece explores"` / `"the article discusses"` / `"the author discusses"` — anonymous subject only.

Named attributions are NEVER banned:
- ✓ `"Thompson argues..."` — NOT banned (named person)
- ✓ `"Seufert concludes..."` — NOT banned (named person)
- ✗ `"the author argues..."` — BANNED

### Rule 3 — List Length
**Handled by deterministic regex (`_check_list_length`) — NOT by the LLM.**
Any single explicit bulleted (`-`, `•`, `*`) or numbered (`1.`, `2.`) list with **more than 5 items** → VIOLATION. Instruct compression to max 3 items.
- Exactly 5 items is NOT a violation (threshold is strictly >5)
- Inline comma-separated lists in prose are NOT counted

### Rule 4 — POV Attribution
**Checked by the LLM** using a structured scratchpad. A violation occurs ONLY when a claim is explicitly attributed to a specific source in the raw article and the draft credits a **different** person.

**Step 1 — Enumerate:** Scan the draft for sentences crediting a named person with a fact or claim.

**Step 2 — Verify each (decision tree):**
- **(a) Third-party claim stolen** — raw article attributes the claim to Person B, draft credits Person A → VIOLATION
- **(b) Reported fact misattributed to author** — raw article uses "sources say" / "according to [Company]" / "the company said" (crediting a source other than the author), but draft credits the newsletter author with asserting it as their own claim → VIOLATION. `"According to sources"` and `"According to anonymous sources"` are equivalent — do NOT flag one as a correction of the other.
- **(c) Author's own analysis** — the claim is the newsletter author's analytical framing, interpretation, or conclusion. For Stratechery, the entire article is Thompson's analysis; his interpretations are attributable to him even without a literal "I argue" in the raw text → NOT a violation. **CRITICAL:** Rule 4(c) does NOT mean every fact Thompson quotes should be re-credited to Thompson. If the draft correctly credits a named third-party source (e.g. "According to Sarah Frier") and the raw article confirms that person as the actual source, this is NEVER a violation.
- **(d) Correctly attributed** → NOT a violation

Correction must name who is wrongly credited AND who actually made the claim per the raw article.

**Structured output + Python filters:** The LLM returns `rule2_violations` (list of strings) and `rule4_violations` (list of objects with `offending_quote`, `person_credited_in_draft`, `person_credited_in_raw_article`, `correction`). Two deterministic filters run before any Rule 4 entry reaches the verdict:
1. **`_is_same_person()`** — discards entries where both person fields name the same individual (handles phrasing variants like "Sarah Frier" vs "OpenAI CFO Sarah Frier")
2. **Thompson-author filter** — discards entries where `person_credited_in_raw_article` is the newsletter author (Thompson/Ben Thompson) while the draft credits a named third party (the draft is more correct in that case)

### Rule 5 — Guest Context
**Handled by deterministic regex (`_check_guest_context`) — NOT by the LLM.**
Flags vague anonymous references used in place of a person's name (up to 4 modifier words between article and role noun):
- ✗ `"A senior OpenAI leader..."` — VIOLATION (matches `a ... leader`)
- ✗ `"an executive outlined..."` — VIOLATION
- ✗ `"the guest explained..."` — VIOLATION

Appositive descriptions after a named person are **not flagged** (comma-skip logic):
- ✓ `"Eric Seufert, a mobile growth analyst, argued..."` — NOT flagged (appositive after name)
- ✓ `"David Ellison, CEO of Skydance Media..."` — NOT flagged (named person)

Role nouns checked: `executive`, `leader`, `analyst`, `researcher`, `officer`, `guest`, `interviewee`

## Eval Suite Structure (tests/test_reviewer.py)

| TC | Rule Tested | Expected | Keyword |
|----|-------------|----------|---------|
| TC-01 | Rule 1 (private financials) | FAIL | `private` |
| TC-02 | Rule 2 (anonymous article references) | FAIL | `BANNED` |
| TC-03 | Rule 3 (list length) | FAIL | `LIST` |
| TC-04 | Rule 4 (POV misattribution) | FAIL | `Seufert` |
| TC-05 | Rule 5 (guest context) | FAIL | `GUEST` |
| TC-06 | Clean pass (no violations) | PASS | _(empty)_ |

Tests use `financial_block=mock_financial_block` (formatted text string, not a list of dicts).

Judge scores 3 binary criteria per test: `defect_catch_rate`, `false_positive_rate`, `critique_actionability`. All three must be 1 for the test to pass.

**Judge model:** GPT-4o (upgraded from gpt-4o-mini — mini was unreliable for concept-matching). Judge prompt includes a keyword-to-concept mapping table so it reliably recognises indirect references (e.g. "GUEST CONTEXT" → satisfies keyword "GUEST").

## Gmail OAuth Notes
- `auth.py` generates `token.json` locally via browser OAuth
- Required scopes: `gmail.send` + `gmail.readonly`
- After re-running `auth.py`, copy new `token.json` contents into the `GMAIL_TOKEN_JSON` GitHub Secret
