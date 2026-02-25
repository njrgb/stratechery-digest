# Newsletter Summarizer

Automatically fetches, summarizes, and emails daily newsletter digests using GPT-4o and the Gmail API. Runs on a GitHub Actions schedule every weekday morning.

## What it does

| Script | Newsletter | Delivery |
|---|---|---|
| `summarize.py` | Stratechery (via RSS) | Summarizes the latest paid article with financial data, market context, and implications |
| `lenny.py` | Lenny's Newsletter (via Gmail) | Summarizes paid articles and Community Wisdom emails; skips "How I AI" podcast emails |

Summaries are sent as formatted HTML emails to a configured recipient address.

## Prerequisites

- Python 3.11+
- An [OpenAI API key](https://platform.openai.com/api-keys) with GPT-4o access
- A Stratechery paid subscription (RSS feed URL)
- A Google Cloud project with the Gmail API enabled
- A Gmail account to send from (OAuth2)

## One-time setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set up Gmail OAuth

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Gmail API**
2. Create an **OAuth 2.0 Client ID** (Desktop app) and download it as `credentials.json` into this folder
3. Set the OAuth consent screen to **Production** (prevents 7-day token expiry)
4. Run the auth script — it will open a browser to authorize Gmail send + read access:

```bash
python auth.py
```

This saves a `token.json` file. Keep it secret — it contains your refresh token.

### 3. Configure GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions**, add:

| Secret | Value |
|---|---|
| `OPENAI_API_KEY` | Your OpenAI API key |
| `STRATECHERY_FEED` | Your private Stratechery RSS feed URL |
| `TO_EMAIL` | Email address to deliver summaries to |
| `GMAIL_TOKEN_JSON` | Full contents of `token.json` (paste as-is) |

## Running locally

Set environment variables (PowerShell):

```powershell
$env:OPENAI_API_KEY     = "sk-proj-..."
$env:STRATECHERY_FEED   = "https://stratechery.passport.online/feed/rss/..."
$env:TO_EMAIL           = "you@gmail.com"
# token.json must exist locally from running auth.py
```

Then:

```powershell
# List recent articles in the feed
python summarize.py --list

# Summarize the latest article (default)
python summarize.py

# Summarize a specific article by index
python summarize.py --index 2

# Run the Lenny summarizer
python lenny.py
```

The `--index` flag bypasses the freshness check, so you can re-run on older articles for testing.

## Automation

The GitHub Actions workflow (`.github/workflows/summarize.yml`) runs both scripts at **1pm UTC (8am ET)** on weekdays. It can also be triggered manually from the Actions tab.

Each script skips content older than 25 hours to avoid re-sending on manual triggers.

## Design decisions

**Why Gmail OAuth2 instead of Zapier, SMTP, or a dedicated email address**
A few approaches were on the table:

- *Zapier / Make / n8n* — no-code tools that can connect Gmail to OpenAI. Rejected because they charge per task, heavily constrain what the prompt and logic can do, and can't run arbitrary code (fetching live financials from Yahoo Finance, custom HTML rendering, multi-step pipelines). Every customization becomes a workaround.
- *Dedicated Gmail account + app password + SMTP/IMAP* — simpler credential model, but Google has deprecated "less secure app access" for most accounts, so this no longer reliably works. IMAP is also being phased out in favour of the Gmail API, and storing a plain password as a secret is less secure than a scoped OAuth token.
- *Third-party sending services (Mailgun, SendGrid)* — good for production apps sending at scale, but require a verified domain and additional service setup. Overkill for a personal pipeline.

I landed on **Gmail OAuth2** because it's the officially supported path, works without app passwords, and a single refresh token covers both sending (delivering summaries) and reading (fetching Lenny emails from the inbox). The token lives as a GitHub Secret and is never stored in code.

**RSS for Stratechery, Gmail API for Lenny's**
Stratechery provides a private paid RSS feed, so fetching it is a simple URL call — no inbox access needed. Lenny's Newsletter doesn't have an equivalent paid feed, so I read it directly from Gmail using the Gmail API with `gmail.readonly` scope.

**Time-based deduplication instead of a database**
The simplest way to avoid re-sending an article is to check whether it was published within the last 25 hours (not 24 — the extra hour buffers against GitHub Actions scheduling jitter). This is stateless, requires no persistence layer, and works naturally with ephemeral CI runners. A database or state file would add complexity with no real benefit at this scale.

**Standalone scripts instead of a shared module**
`lenny.py` duplicates some utilities from `summarize.py` (HTML stripping, Gmail service setup, email sending) rather than importing them. This is intentional: `summarize.py` has module-level side effects (`sys.stdout` reassignment, environment variable reads at import time) that would cause problems if imported as a library. Keeping the scripts self-contained avoids a fragile import dependency.

**Two-step pipeline for Stratechery financials**
Rather than asking GPT to recall financial figures from training data (which would be stale or hallucinated), I run a lightweight extraction pass first to identify ticker symbols, fetch live data from Yahoo Finance, and inject it into the summarization prompt. This keeps the financials grounded in real numbers.

**Classify Lenny emails by sender address, not subject**
Lenny sends three distinct email types from three distinct substack addresses (`lenny@`, `lenny+community-wisdom@`, `lenny+how-i-ai@`). Routing on sender address is more reliable than parsing subject lines, which can vary in format.

**GitHub Actions for scheduling**
Free, requires no server or cloud infrastructure, and integrates naturally with the existing repo. The main trade-off is that scheduled runs can be delayed by a few minutes under load — the 25-hour freshness window accounts for this.

## File structure

```
summarize.py          # Stratechery summarizer
lenny.py              # Lenny's Newsletter summarizer
auth.py               # One-time OAuth setup script
requirements.txt      # Python dependencies
credentials.json      # Google OAuth client config (not committed)
token.json            # Gmail refresh token (not committed)
.github/
  workflows/
    summarize.yml     # GitHub Actions workflow
```
