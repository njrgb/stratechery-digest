import sys
import io
import os
import re
import json
import time
import base64
import argparse
import feedparser
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import yfinance as yf
import markdown as md
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from openai import OpenAI
from html.parser import HTMLParser
from src.agents.reviewer import ReviewInput, audit
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

GMAIL_SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
]

STRATECHERY_FEED = os.environ.get('STRATECHERY_FEED', '')


def _call_with_retry(fn, max_attempts=3):
    """Call fn(), retrying on transient errors with exponential backoff."""
    from openai import AuthenticationError, PermissionDeniedError
    for attempt in range(max_attempts):
        try:
            return fn()
        except (AuthenticationError, PermissionDeniedError):
            raise  # never retry auth errors — they won't resolve
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            wait = 10 * (2 ** attempt)  # 10s, 20s
            print(f"  API call failed (attempt {attempt + 1}/{max_attempts}): {e}. Retrying in {wait}s...")
            time.sleep(wait)


# Note: {{ and }} are escaped braces in format strings
EXTRACT_PROMPT = """List all publicly traded companies mentioned in this article.
Return valid JSON only, no other text: {{"companies": [{{"name": "Full Company Name", "ticker": "TICK", "segments": ["Division1", "Division2"]}}]}}
- "segments": list the specific business divisions, products, or units of this company that are discussed in the article (e.g. ["Xbox", "Gaming"] for MSFT if those divisions are central to the piece). Empty list if no specific division is discussed.
Only include companies with known stock exchange tickers. Skip private companies and uncertain cases.

Article title: {title}
Article (first 2000 chars): {text}"""

SUMMARIZE_PROMPT = """You are summarizing a Stratechery article for a senior tech/product professional.
Assume full familiarity with the tech industry. No generic filler like "this piece explores" or "the article discusses".
Be direct, specific, and opinionated.

CRITICAL RULE: Make Ben Thompson's thesis and reasoning chain explicit throughout. Don't just describe what happened — show WHY Thompson interprets it the way he does and what conclusion he draws. Use "Thompson argues...", "Thompson's view is...", "Thompson concludes..." to attribute his specific analytical claims. The reader should finish each section understanding not just the facts but Thompson's actual position and the logic behind it.
Use plain prose for narrative and analysis. Use bullet points when listing discrete items (advantages, risks, players, features) — wherever a list is genuinely cleaner than a sentence.

CRITICAL RULE: Always use specific names. Never write "the opposing view", "the article", "critics", or "some argue".
Instead, name the actual person, publication, or company — e.g. "Citrini Research argues...", "The Wall Street Journal reported...", "Andreessen claims...".

CRITICAL RULE: Always anchor claims with specific metrics, timeframes, and indicators mentioned in the article. Never write vague descriptors like "declining performance" or "faced challenges" when the article provides concrete data. Use the actual figures — e.g. "three consecutive years of declining Xbox hardware revenue" not "hardware struggles", "down 40% year-over-year" not "significant drop".
If a name isn't clear from the text, use the closest specific descriptor available (e.g. "the Citrini Research note" not "the piece").

Article title: {title}
Article text:
{text}

{financial_block}

Article type: {article_type}
If this is a Daily Update: write compact sections (1-2 focused paragraphs per topic). You may omit MARKET CONTEXT if market structure isn't central to the piece.
If this is a Weekly: write full-length sections. Include MARKET CONTEXT with concrete size/share data.

The article title may indicate multiple topics (comma-separated) or a single deep-dive. Detect this from the title and structure accordingly.

Respond in EXACTLY this format, keeping all headers as written:

TOPICS COVERED
- [one line per topic]

[For each topic, one section with the topic name in caps as the header:]

STANDARD TOPIC FORMAT (use when no debate/rebuttal is involved):
[TOPIC NAME IN CAPS]
[2-3 paragraphs of direct analysis]

DEBATE/REBUTTAL TOPIC FORMAT (use when the section responds to or debates another source):
[TOPIC NAME IN CAPS]
[1-2 sentences: what this section is about]

Core debate: [The central claim being contested, in one sentence]

| Claim / Example | [Opposing source name] argues | Stratechery counters |
|---|---|---|
| [specific point] | [their position] | [Stratechery's response] |
[add one row per distinct point — aim for 3-5 rows]

Conclusion: [Stratechery's overall verdict or sentiment in 2-3 sentences]

IMPLICATIONS
[2-3 bullet points: what this means for builders, investors, or operators in the relevant space. Be specific and actionable — e.g. "If you're building developer tooling, Thompson's framing suggests commoditization pressure from below is more near-term than API lock-in".]

MARKET CONTEXT
[For each distinct market discussed: estimated size, key players with rough market share percentages, and market structure. Be specific with numbers. Flag if approximate. Omit for Daily Updates unless market structure is central to the article.]

FINANCIALS
[For each company, use the financial data provided. One entry per company:
Name (TICKER) — Market cap: $X.XB | [Most recent quarter] revenue: $XM (+X% YoY) | Net income: $XM
If relevant divisions are listed, add on a new line using this exact markdown link format: "Segment data: [View {{division names}} breakdown →](the provided URL)"
If no financial data was provided for a company, omit it entirely. Do not invent figures.]"""


class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []
        self._skip_tags = {'script', 'style', 'head'}
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip = True
        if tag in ('p', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'blockquote'):
            self.chunks.append('\n')

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)

    def get_text(self):
        text = ''.join(self.chunks)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]+', ' ', text)
        return text.strip()


def strip_html(html):
    parser = HTMLStripper()
    parser.feed(html)
    return parser.get_text()


def fetch_latest(entry_index=0):
    feed = feedparser.parse(STRATECHERY_FEED)
    if not feed.entries:
        raise RuntimeError("No entries found in feed")
    if entry_index >= len(feed.entries):
        raise RuntimeError(f"Feed only has {len(feed.entries)} entries; index {entry_index} is out of range")
    entry = feed.entries[entry_index]
    title = entry.get('title', 'Untitled')
    published = entry.get('published', '')
    link = entry.get('link', '')
    raw_html = entry.get('content', [{}])[0].get('value', '') or entry.get('summary', '')
    text = strip_html(raw_html)
    return title, published, link, text


def extract_companies(title, text, client):
    try:
        response = _call_with_retry(lambda: client.chat.completions.create(
            model="gpt-4o",
            max_tokens=400,
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": EXTRACT_PROMPT.format(title=title, text=text[:2000])
            }]
        ))
        data = json.loads(response.choices[0].message.content)
        return data.get('companies', [])
    except Exception as e:
        print(f"  (Company extraction failed: {e})")
        return []


def fmt(n):
    if n is None:
        return "N/A"
    n = float(n)
    if abs(n) >= 1e9:
        return f"${n/1e9:.1f}B"
    if abs(n) >= 1e6:
        return f"${n/1e6:.0f}M"
    return f"${n:,.0f}"


def fetch_financials(companies):
    results = []
    for co in companies:
        try:
            t = yf.Ticker(co['ticker'])
            info = t.info
            market_cap = info.get('marketCap')

            quarterly = t.quarterly_income_stmt
            quarters = []
            yoy_growth = None

            if quarterly is not None and not quarterly.empty:
                # Try both possible row name formats across yfinance versions
                rev_row = next((r for r in ['Total Revenue', 'TotalRevenue'] if r in quarterly.index), None)
                net_row = next((r for r in ['Net Income', 'NetIncome'] if r in quarterly.index), None)

                for col in quarterly.columns[:2]:
                    rev = quarterly.loc[rev_row, col] if rev_row else None
                    net = quarterly.loc[net_row, col] if net_row else None
                    quarters.append({
                        'period': str(col)[:7],
                        'revenue': rev,
                        'net_income': net
                    })

                # YoY: most recent quarter vs same quarter one year prior (5th column)
                if rev_row and len(quarterly.columns) >= 5:
                    curr = quarterly.loc[rev_row].iloc[0]
                    prior = quarterly.loc[rev_row].iloc[4]
                    if prior and float(prior) != 0:
                        yoy_growth = (float(curr) - float(prior)) / abs(float(prior)) * 100

            results.append({
                'name': co['name'],
                'ticker': co['ticker'],
                'market_cap': market_cap,
                'quarters': quarters,
                'yoy_growth': yoy_growth,
                'segments': co.get('segments', []),
            })
        except Exception as e:
            print(f"  (Skipping {co.get('ticker', '?')}: {e})")

    return results


def format_financial_block(financials):
    if not financials:
        return ""
    lines = ["FINANCIAL DATA (sourced from Yahoo Finance — use this in the FINANCIALS section):"]
    for co in financials:
        entry = f"\n{co['name']} ({co['ticker']})"
        entry += f"\n  Market cap: {fmt(co['market_cap'])}"
        for q in co['quarters'][:2]:
            entry += f"\n  {q['period']}: Revenue {fmt(q['revenue'])}, Net income {fmt(q['net_income'])}"
        if co['yoy_growth'] is not None:
            sign = "+" if co['yoy_growth'] >= 0 else ""
            entry += f"\n  YoY revenue growth (vs same quarter last year): {sign}{co['yoy_growth']:.1f}%"
        if co.get('segments'):
            segs = ', '.join(co['segments'])
            ticker = co['ticker']
            entry += f"\n  Relevant divisions: {segs}"
            entry += f"\n  Segment-level data URL: https://finance.yahoo.com/quote/{ticker}/financials/"
        lines.append(entry)
    return '\n'.join(lines)


def summarize(title, text, financial_block, client, article_type='Weekly'):
    response = _call_with_retry(lambda: client.chat.completions.create(
        model="gpt-4o",
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": SUMMARIZE_PROMPT.format(
                title=title,
                text=text,
                financial_block=financial_block,
                article_type=article_type
            )
        }]
    ))
    return response.choices[0].message.content


def print_output(title, published, link, summary):
    divider = "━" * 60
    print(f"\n{divider}")
    print(f"STRATECHERY  |  {published[:16]}")
    print(f"\n{title}\n")
    print(divider)
    print(summary)
    print(f"\n{divider}")
    print(f"Read: {link[:80]}")
    print(divider)


def get_gmail_service():
    creds = None
    token_json_str = os.environ.get('GMAIL_TOKEN_JSON')
    if token_json_str:
        creds = Credentials.from_authorized_user_info(json.loads(token_json_str), GMAIL_SCOPES)
    elif os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', GMAIL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('gmail', 'v1', credentials=creds)


def _prepare_html_summary(summary):
    """Convert the plain-text summary format into clean markdown for HTML rendering."""
    lines = summary.split('\n')
    out = []
    skip_topics = False
    current_section = None

    for line in lines:
        stripped = line.strip()

        # Detect ALL-CAPS section headers (no punctuation except spaces/apostrophes/hyphens)
        is_header = bool(
            stripped
            and stripped == stripped.upper()
            and len(stripped) > 3
            and not stripped.startswith(('|', '-', '+', '#'))
            and not any(c in stripped for c in '.,;:?!()')
        )

        if is_header:
            current_section = stripped
            if stripped == 'TOPICS COVERED':
                skip_topics = True
                continue
            skip_topics = False
            out.append(f'\n## {stripped}\n')
            continue

        # Skip the TOPICS COVERED bullet list (redundant with email title)
        if skip_topics:
            if stripped.startswith('-') or stripped == '':
                continue
            skip_topics = False

        # Bullet individual entries in FINANCIALS and MARKET CONTEXT
        if current_section in ('FINANCIALS', 'MARKET CONTEXT'):
            if stripped and not stripped.startswith(('-', '|', '+')):
                out.append(f'- {stripped}')
                continue

        out.append(line)

    text = '\n'.join(out)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text



def _extract_currency_figures(text):
    """Extract deduplicated currency figures from text, returning list of (raw_string, value, scale)."""
    from src.agents.reviewer import _CURRENCY_RE, _normalize_figure
    seen = set()
    results = []
    for match in _CURRENCY_RE.finditer(text):
        raw = match.group()
        normalized = _normalize_figure(raw)
        if normalized in seen:
            continue
        seen.add(normalized)
        results.append((raw, normalized[0], normalized[1]))
    return results


def _find_figure_context(value, scale, raw_article, financial_block):
    """
    Find a source sentence for a currency figure (value, scale) in the article or financial block.
    Returns (source_type, excerpt) where source_type is 'article', 'finance', or None.
    """
    from src.agents.reviewer import _build_search_pattern
    pattern = _build_search_pattern(value, scale)

    for source_type, text in [('article', raw_article), ('finance', financial_block)]:
        if not text:
            continue
        match = pattern.search(text)
        if match:
            # Extract the surrounding sentence
            start = max(0, match.start() - 120)
            end = min(len(text), match.end() + 120)
            excerpt = text[start:end]
            # Trim to sentence boundaries if possible
            left = excerpt.rfind('.', 0, match.start() - start)
            if left >= 0:
                excerpt = excerpt[left + 1:]
            right = excerpt.find('.', match.end() - start - (match.start() - start - left - 1 if left >= 0 else 0))
            if right >= 0:
                excerpt = excerpt[:right + 1]
            excerpt = excerpt.strip()
            if len(excerpt) > 160:
                excerpt = excerpt[:157] + '...'
            return (source_type, excerpt)
    return (None, None)


def _build_verify_facts_html(summary, raw_article, financial_block):
    """Build an HTML 'Verify Facts' section listing all currency figures with their source context."""
    figures = _extract_currency_figures(summary)
    if not figures:
        return ''

    rows = []
    for raw, value, scale in figures:
        source_type, excerpt = _find_figure_context(value, scale, raw_article, financial_block)
        if source_type == 'article':
            source_html = f'<span class="vf-article">&ldquo;{excerpt}&rdquo;</span>'
        elif source_type == 'finance':
            source_html = '<em class="vf-finance">from Yahoo Finance</em>'
        else:
            source_html = '<strong class="vf-missing">Not found in source</strong>'
        rows.append(f'<tr><td class="vf-figure">{raw}</td><td>{source_html}</td></tr>')

    rows_html = '\n'.join(rows)
    return f"""<div class="verify-facts">
<p class="verify-header">VERIFY FACTS</p>
<p class="verify-desc">Currency figures cited in this summary and their sources.</p>
<table>
<tr><th>Figure</th><th>Source</th></tr>
{rows_html}
</table>
</div>"""


def send_email(title, published, link, summary, article_type='Weekly', subject_prefix='', raw_article='', financial_block=''):
    to_email = os.environ.get('TO_EMAIL')
    if not to_email:
        raise RuntimeError("TO_EMAIL environment variable not set")

    html_body = md.markdown(_prepare_html_summary(summary), extensions=['tables'])
    verify_facts_html = _build_verify_facts_html(summary, raw_article, financial_block)
    html = f"""<!DOCTYPE html>
<html><head><style>
  body {{ font-family: Georgia, serif; max-width: 700px; margin: 40px auto; color: #222; line-height: 1.7; }}
  h1 {{ font-size: 1.3em; margin-bottom: 0.2em; }}
  h2, h3 {{ font-size: 1em; font-weight: bold; letter-spacing: 0.06em; border-bottom: 2px solid #333; padding-bottom: 4px; margin-top: 2em; color: #111; }}
  p {{ margin: 0.8em 0; }}
  ul {{ margin: 0.5em 0 0.8em 1.4em; padding: 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 0.92em; }}
  th, td {{ border: 1px solid #ccc; padding: 8px 12px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; font-weight: bold; }}
  .meta {{ color: #888; font-size: 0.88em; margin-bottom: 1.5em; }}
  .footer {{ margin-top: 2em; font-size: 0.85em; color: #888; border-top: 1px solid #eee; padding-top: 1em; }}
  .verify-facts {{ margin-top: 2em; font-size: 0.85em; color: #555; }}
  .verify-header {{ font-weight: bold; letter-spacing: 0.06em; border-bottom: 2px solid #333; padding-bottom: 4px; color: #111; display: block; margin-bottom: 0.5em; }}
  .verify-desc {{ color: #888; margin: 0 0 0.8em 0; }}
  .vf-figure {{ white-space: nowrap; font-family: monospace; font-size: 0.95em; padding-right: 1em; }}
  .vf-missing {{ color: #c00; }}
  .vf-finance {{ color: #666; }}
  .vf-article {{ color: #444; }}
</style></head>
<body>
<p class="meta">Stratechery &nbsp;·&nbsp; {published[:16]} &nbsp;·&nbsp; {article_type}</p>
<h1>{title}</h1>
{html_body}
{verify_facts_html}
<p class="footer"><a href="{link}">Read original →</a></p>
</body></html>"""

    msg = MIMEMultipart('alternative')
    short_title = title.split(',')[0].strip()
    msg['Subject'] = f"{subject_prefix}[TL;DR] Stratechery: {short_title}"
    msg['From'] = 'me'
    msg['To'] = to_email
    msg.attach(MIMEText(summary, 'plain', 'utf-8'))
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = get_gmail_service()
    service.users().messages().send(userId='me', body={'raw': raw}).execute()
    print(f"Email sent to {to_email}")


def is_fresh(published_str, max_hours=8):
    """Return True if article was published within the last max_hours."""
    try:
        age = datetime.now(timezone.utc) - parsedate_to_datetime(published_str)
        return age.total_seconds() < max_hours * 3600
    except Exception:
        return False  # unparseable date — skip rather than risk duplicate


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Summarize a Stratechery article')
    parser.add_argument('--index', type=int, default=0,
                        help='Feed entry index to summarize (0=latest, 1=second latest, etc.)')
    parser.add_argument('--list', action='store_true',
                        help='List available articles in the feed and exit')
    parser.add_argument('--dry-run', action='store_true',
                        help='Run summarize+audit loop but skip sending the email')
    args = parser.parse_args()

    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable not set")
    client = OpenAI(api_key=api_key)

    if args.list:
        feed = feedparser.parse(STRATECHERY_FEED)
        print(f"\n{'#':<4} {'Published':<18} Title")
        print("-" * 80)
        for i, e in enumerate(feed.entries[:10]):
            pub = e.get('published', '')[:16]
            print(f"{i:<4} {pub:<18} {e.get('title', 'Untitled')}")
        sys.exit(0)

    print("Fetching Stratechery article...")
    title, published, link, text = fetch_latest(args.index)
    print(f"Found: {title} ({len(text)} chars)")

    if args.index == 0 and not is_fresh(published):
        print("Article is more than 23 hours old — already sent. Exiting.")
        sys.exit(0)

    # Detect article type: Daily Updates are typically shorter and often titled as such
    article_type = 'Daily Update' if ('daily' in title.lower() or len(text) < 6000) else 'Weekly'
    print(f"Detected article type: {article_type}")

    print("Extracting public companies...")
    companies = extract_companies(title, text, client)
    print(f"Companies identified: {[c['ticker'] for c in companies] or 'none'}")
    for co in companies:
        if co.get('segments'):
            print(f"  {co['ticker']} segments: {co['segments']}")

    financial_block = ""
    financials = []
    if companies:
        print("Fetching financials from Yahoo Finance...")
        financials = fetch_financials(companies)
        financial_block = format_financial_block(financials)

    print("Summarizing...")
    summary = summarize(title, text, financial_block, client, article_type)
    print_output(title, published, link, summary)

    # --- Audit loop ---
    print("Auditing summary...")
    result = audit(ReviewInput(
        raw_article=text,
        financial_block=financial_block,
        draft_summary=summary,
        newsletter_type='stratechery',
    ), client)

    subject_prefix = ''
    if result.action == 'FAIL':
        print(f"  Audit FAIL — revising. Violations:\n{result.critique[:400]}")
        revision_system = SUMMARIZE_PROMPT.format(
            title=title,
            text=text,
            financial_block=financial_block,
            article_type=article_type,
        )
        revision_user = (
            f"The draft summary below failed an editorial audit. Revise it to fix every "
            f"violation listed — keep all other content unchanged.\n\n"
            f"VIOLATIONS TO FIX:\n{result.critique}\n\n"
            f"RULE 1 VALID FIXES (private company financials):\n"
            f"  • Remove the unattributed figure entirely, OR\n"
            f"  • Add a named attribution: e.g. 'per Bloomberg', 'according to [Name, Title]'\n"
            f"  Do NOT rephrase the figure or add vague hedges like 'reportedly' — those still fail.\n\n"
            f"RULE 3 VALID FIXES (list length):\n"
            f"  • Remove the least informative items until the list has 3 or fewer entries.\n"
            f"  Do NOT split one long list into two shorter lists — that still fails.\n\n"
            f"RULE 4 VALID FIXES (POV attribution):\n"
            f"  - Find the exact claim in the RAW ARTICLE. Check who the article attributes it to.\n"
            f"  - If the raw article names a DIFFERENT person: replace the credited person\n"
            f"    in the draft with whoever the raw article actually names.\n"
            f"  - If the raw article does NOT name anyone for the claim: attribute to Thompson\n"
            f"    ('Thompson argues...') or remove the third-party attribution entirely.\n"
            f"  Do NOT use hedges like reportedly - attribute clearly to a named person.\n"
            f"  - After fixing the listed violations, scan the ENTIRE revised draft for ALL\n"
            f"    attributed claims (any sentence crediting a named person with an argument or\n"
            f"    position) and verify each one against the raw article. Fix any additional\n"
            f"    misattributions you find — do not stop at the violations listed above.\n\n"
            f"RULE 5 VALID FIXES (guest context):\n"
            f"  • Find the person's full name, title, and company in the RAW ARTICLE (provided in\n"
            f"    your system prompt) and use them on first mention.\n"
            f"  • Any ordering is acceptable: 'Name, Title at Company' or 'Company's Title, Name'.\n"
            f"  • CRITICAL: Adding a first name alone is NOT sufficient. You must include all three:\n"
            f"    full name + title + company. E.g. expanding 'Booty' to 'Matt Booty' still fails —\n"
            f"    the correct fix is 'Matt Booty, Head of Xbox Game Studios at Microsoft'.\n"
            f"  • Scan the ENTIRE revised draft for any person mentioned by last name only or without\n"
            f"    title/company, and apply the full introduction to each one.\n"
            f"  • If the raw article itself only uses a description (e.g. 'a senior researcher') and\n"
            f"    never gives a name, using that same description is NOT a violation — leave it as-is.\n\n"
            f"---\nDRAFT TO REVISE:\n{summary}"
        )
        revision_response = _call_with_retry(lambda: client.chat.completions.create(
            model='gpt-4o',
            max_tokens=4000,
            messages=[
                {'role': 'system', 'content': revision_system},
                {'role': 'user', 'content': revision_user},
            ],
        ))
        summary = revision_response.choices[0].message.content

        result2 = audit(ReviewInput(
            raw_article=text,
            financial_block=financial_block,
            draft_summary=summary,
            newsletter_type='stratechery',
        ), client)
        if result2.action == 'FAIL':
            print(f"  Audit still FAIL after revision. Remaining violations:\n{result2.critique}")
            subject_prefix = '⚠ Audit failed: '
        else:
            print("  Audit PASS after revision.")
    else:
        print("  Audit PASS.")

    if args.dry_run:
        print("Dry run — skipping email send.")
    else:
        print("Sending email...")
        send_email(title, published, link, summary, article_type, subject_prefix=subject_prefix, raw_article=text, financial_block=financial_block)
