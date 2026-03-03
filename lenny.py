import sys
import io
import os
import re
import json
import time
import base64
import markdown as md
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from openai import OpenAI
from google.auth.transport.requests import Request
from src.agents.reviewer import ReviewInput, audit
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

GMAIL_SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
]

# Sender classification
LENNY_SENDERS = {
    'lenny@substack.com': 'article',
    'lenny+community-wisdom@substack.com': 'community',
    'lenny+how-i-ai@substack.com': 'skip',
}

ARTICLE_PROMPT = """You are summarizing a Lenny's Newsletter article for a senior product/tech professional.
Assume full familiarity with the tech industry. No filler phrases like "the author discusses" or "this piece explores".
Be direct, specific, and opinionated.
Use plain prose for narrative and analysis. Use bullet points when listing discrete items (advantages, risks, steps, frameworks).

CRITICAL RULE: Always use specific names. Never write vague references like "the guest" or "some argue". Use the actual person, company, or framework name.

CRITICAL RULE: Always anchor claims with specific metrics, timeframes, and indicators mentioned in the article. Never write vague descriptors like "strong growth" or "many users" when the article provides concrete data. Use the actual figures — e.g. "grew DAUs 3x in 18 months" not "rapid growth", "used by 40% of Fortune 500 companies" not "widely adopted".

Start with a single sentence introducing the guest: their name, current role, and company.
If there is no guest (Lenny wrote it himself), skip the intro line.

If the article includes a "Key Takeaways" section, integrate those points into the relevant topic sections — do not repeat them as a standalone list.

Article title: {title}
Article text:
{text}

Respond in EXACTLY this format, keeping all headers as written:

TOPICS COVERED
- [one line per topic]

[For each topic, one section with the topic name in caps as the header:]

[TOPIC NAME IN CAPS]
[2-3 paragraphs of direct analysis]

FRAMEWORKS OR MODELS
[Only include if the guest presents a named framework or model — e.g. "the CIRCLES method", "the four traps of growth". State the name, then list its components as bullet points. If no named framework is presented, omit this section entirely.]

IMPLICATIONS
[2-3 bullet points: what product managers, founders, or growth leaders should take away. Be specific and actionable.]"""

COMMUNITY_PROMPT = """This is a Lenny's Newsletter Community Wisdom email. The text may contain markdown links in [text](url) format — preserve these links in your output.

Extract and format the following:

1. A markdown table with these exact columns:
   | Question | Best Response | Thread Link | # Other Responses |

   - "Question" is the question posted by the community member
   - "Best Response" is the top/highlighted answer shown
   - "Thread Link": if a URL is present in the email for this question's thread, format it as [View thread](url). Otherwise use "—"
   - "# Other Responses" is the count of other replies shown in the email. If not shown, use "—"
   - Include all questions present in the email

2. After the table, a TOP FINDS section — copy the content from the "Top Finds" section of the email verbatim, formatted as a bulleted list. Preserve any [text](url) markdown links. If there is no Top Finds section, omit this section entirely.

Return only these two sections, no other commentary.

Email text:
{text}"""


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


def strip_html_with_links(html):
    """Strip HTML but convert <a href> tags to markdown [text](url) format."""
    class LinkPreservingStripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.chunks = []
            self._skip_tags = {'script', 'style', 'head'}
            self._skip = False
            self._in_link = False
            self._link_href = ''
            self._link_text = []

        def handle_starttag(self, tag, attrs):
            if tag in self._skip_tags:
                self._skip = True
            if tag in ('p', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'blockquote'):
                self.chunks.append('\n')
            if tag == 'a' and not self._skip:
                attrs_dict = dict(attrs)
                href = attrs_dict.get('href', '')
                if href and not href.startswith('mailto:') and not href.startswith('#'):
                    self._in_link = True
                    self._link_href = href
                    self._link_text = []

        def handle_endtag(self, tag):
            if tag in self._skip_tags:
                self._skip = False
            if tag == 'a' and self._in_link:
                text = ''.join(self._link_text).strip()
                if text and self._link_href:
                    self.chunks.append(f'[{text}]({self._link_href})')
                elif text:
                    self.chunks.append(text)
                self._in_link = False
                self._link_href = ''
                self._link_text = []

        def handle_data(self, data):
            if not self._skip:
                if self._in_link:
                    self._link_text.append(data)
                else:
                    self.chunks.append(data)

        def get_text(self):
            text = ''.join(self.chunks)
            text = re.sub(r'\n{3,}', '\n\n', text)
            text = re.sub(r'[ \t]+', ' ', text)
            return text.strip()

    parser = LinkPreservingStripper()
    parser.feed(html)
    return parser.get_text()


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


def classify_sender(from_header):
    """Return 'article', 'community', or 'skip' based on sender address."""
    from_header = from_header.lower()
    for addr, kind in LENNY_SENDERS.items():
        if addr in from_header:
            return kind
    return None  # not a Lenny email


def decode_email_body(payload):
    """Recursively extract and decode the HTML (or plain text) body from a Gmail message payload."""
    mime_type = payload.get('mimeType', '')
    body_data = payload.get('body', {}).get('data', '')

    if mime_type == 'text/html' and body_data:
        return base64.urlsafe_b64decode(body_data).decode('utf-8', errors='replace')

    if mime_type == 'text/plain' and body_data:
        # Keep as fallback if no HTML found
        return base64.urlsafe_b64decode(body_data).decode('utf-8', errors='replace')

    # Multipart: recurse into parts, prefer HTML
    parts = payload.get('parts', [])
    html_body = None
    plain_body = None
    for part in parts:
        result = decode_email_body(part)
        if result:
            part_type = part.get('mimeType', '')
            if 'html' in part_type:
                html_body = result
            elif 'plain' in part_type and plain_body is None:
                plain_body = result

    return html_body or plain_body or ''


def is_fresh(date_str, max_hours=8):
    """Return True if email was received within the last max_hours."""
    try:
        age = datetime.now(timezone.utc) - parsedate_to_datetime(date_str)
        return age.total_seconds() < max_hours * 3600
    except Exception:
        return False  # unparseable date — skip rather than risk duplicate


def fetch_lenny_emails(service):
    """Return list of recent Lenny emails as dicts with keys: from, subject, date, text, email_type."""
    query = (
        'from:(lenny@substack.com OR lenny+community-wisdom@substack.com '
        'OR lenny+how-i-ai@substack.com) newer_than:2d'
    )
    result = service.users().messages().list(userId='me', q=query).execute()
    messages = result.get('messages', [])

    emails = []
    for msg_ref in messages:
        msg = service.users().messages().get(
            userId='me', id=msg_ref['id'], format='full'
        ).execute()

        headers = {h['name'].lower(): h['value'] for h in msg['payload']['headers']}
        from_addr = headers.get('from', '')
        subject = headers.get('subject', '(no subject)')
        date_str = headers.get('date', '')

        email_type = classify_sender(from_addr)
        if email_type is None:
            continue  # not a recognised Lenny sender
        if not is_fresh(date_str):
            print(f"  Skipping (too old): {subject}")
            continue

        raw_body = decode_email_body(msg['payload'])
        is_html = '<' in raw_body

        if email_type == 'community':
            # Preserve links as markdown for community emails so GPT can include them in the table
            text = strip_html_with_links(raw_body) if is_html else raw_body
        else:
            text = strip_html(raw_body) if is_html else raw_body

        emails.append({
            'from': from_addr,
            'subject': subject,
            'date': date_str,
            'text': text,
            'email_type': email_type,
        })

    return emails


def summarize_article(title, text, client):
    response = _call_with_retry(lambda: client.chat.completions.create(
        model='gpt-4o',
        max_tokens=4000,
        messages=[{
            'role': 'user',
            'content': ARTICLE_PROMPT.format(title=title, text=text)
        }]
    ))
    return response.choices[0].message.content


def summarize_community(text, client):
    response = _call_with_retry(lambda: client.chat.completions.create(
        model='gpt-4o',
        max_tokens=4000,
        messages=[{
            'role': 'user',
            'content': COMMUNITY_PROMPT.format(text=text)
        }]
    ))
    return response.choices[0].message.content


def _prepare_html_summary(summary):
    """Convert plain-text summary (ALL-CAPS headers, bullets) to markdown for HTML rendering."""
    lines = summary.split('\n')
    out = []
    skip_topics = False
    current_section = None

    for line in lines:
        stripped = line.strip()

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

        if skip_topics:
            if stripped.startswith('-') or stripped == '':
                continue
            skip_topics = False

        out.append(line)

    text = '\n'.join(out)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def send_summary_email(subject, date_str, summary, service):
    to_email = os.environ.get('TO_EMAIL')
    if not to_email:
        raise RuntimeError('TO_EMAIL environment variable not set')

    html_body = md.markdown(_prepare_html_summary(summary), extensions=['tables'])
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
</style></head>
<body>
<p class="meta">Lenny's Newsletter &nbsp;·&nbsp; {date_str[:16]}</p>
{html_body}
</body></html>"""

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = 'me'
    msg['To'] = to_email
    msg.attach(MIMEText(summary, 'plain', 'utf-8'))
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId='me', body={'raw': raw}).execute()
    print(f"  Email sent: {subject}")


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY environment variable not set')
    client = OpenAI(api_key=api_key)

    print('Connecting to Gmail...')
    service = get_gmail_service()

    print('Fetching recent Lenny emails...')
    emails = fetch_lenny_emails(service)

    if not emails:
        print('No new Lenny emails found. Exiting.')
        sys.exit(0)

    print(f'Found {len(emails)} email(s) to process.')
    for email in emails:
        subject = email['subject']
        email_type = email['email_type']

        if email_type == 'skip':
            print(f'  Skipping (How I AI): {subject}')
            continue

        print(f'  Processing [{email_type}]: {subject}')

        if email_type == 'community':
            summary = summarize_community(email['text'], client)
            out_subject = f"[TL;DR] Lenny's Community Wisdom: {email['date'][:16]}"
        else:
            summary = summarize_article(subject, email['text'], client)
            short_subject = subject.split(',')[0].strip()
            out_subject = f"[TL;DR] Lenny's: {short_subject}"

            # --- Audit loop (article emails only) ---
            print(f"    Auditing...")
            result = audit(ReviewInput(
                raw_article=email['text'],
                yfinance_data=[],
                draft_summary=summary,
                newsletter_type='lenny',
            ), client)

            if result.action == 'FAIL':
                print(f"    Audit FAIL — revising. Violations:\n{result.critique[:300]}")
                revision_prompt = (
                    f"Revise this newsletter summary to fix all of the following violations "
                    f"before it is emailed. Apply every correction listed:\n\n"
                    f"{result.critique}\n\n"
                    f"---\nOriginal summary to revise:\n{summary}"
                )
                revision_response = _call_with_retry(lambda: client.chat.completions.create(
                    model='gpt-4o',
                    max_tokens=4000,
                    messages=[{'role': 'user', 'content': revision_prompt}]
                ))
                summary = revision_response.choices[0].message.content

                result2 = audit(ReviewInput(
                    raw_article=email['text'],
                    yfinance_data=[],
                    draft_summary=summary,
                    newsletter_type='lenny',
                ), client)
                if result2.action == 'FAIL':
                    print(f"    Audit still FAIL — flagging subject line.")
                    out_subject = f"⚠ Audit failed: {out_subject}"
                else:
                    print(f"    Audit PASS after revision.")
            else:
                print(f"    Audit PASS.")

        send_summary_email(out_subject, email['date'], summary, service)
