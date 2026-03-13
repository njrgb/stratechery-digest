"""
Reviewer Agent (Editor-in-Chief)

Audits a newsletter draft summary against the raw article text and verified
YFinance data before it is emailed. Returns a binary PASS/FAIL verdict and,
on FAIL, an actionable critique for the Summarizer to use in a revision.

Integration point: call audit() after summarize_article() / summarize_community()
returns the draft and before send_summary_email() is called.
"""

import json
import re
from openai import OpenAI
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class ReviewInput(BaseModel):
    raw_article: str
    financial_block: str  # formatted YFinance string passed to the summarizer
    draft_summary: str
    newsletter_type: str  # "stratechery" or "lenny"


class ReviewResult(BaseModel):
    action: str    # "PASS" or "FAIL"
    critique: str  # empty string on PASS; numbered violation list on FAIL


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_UNIVERSAL_RULES = """\
You are an Editor-in-Chief auditing a newsletter summary before it is emailed
to a senior technology professional. You have two non-negotiable rules.
Return FAIL if ANY rule is violated. Return PASS only when zero violations exist.

RULE 2 — BANNED ANONYMOUS ARTICLE REFERENCES
To check for a violation, identify the grammatical subject of the sentence.
Do NOT look at the verb. Only the subject matters.

  Subject is "the author", "the article", or "the piece" → VIOLATION
  Subject is a named person (any name, e.g. Thompson, Seufert, Altman) → NOT a violation

  VIOLATIONS (subject is anonymous/article):
    ✗ "the author argues..." — subject is "the author"
    ✗ "the piece explores..." — subject is "the piece"
    ✗ "the article discusses..." — subject is "the article"

  NOT VIOLATIONS (subject is a named person — verb is irrelevant):
    ✓ "Thompson argues..." — subject is Thompson (named person)
    ✓ "Thompson discusses..." — subject is Thompson (named person)
    ✓ "Thompson explores..." — subject is Thompson (named person)
    ✓ "Seufert concludes..." — subject is Seufert (named person)

Correction for 2: Replace the anonymous subject with the named author + verb
  (e.g. for Stratechery: "Thompson argues...", "Thompson examines...").

RULE 4 — POV ATTRIBUTION
A violation occurs ONLY when a claim is explicitly attributed to a specific
source in the raw article, and the draft credits a DIFFERENT person or source.

Step 1 — Enumerate: Scan the draft for sentences that credit a specific named
person with a fact, statistic, or claim using phrases like "[Person] argues...",
"[Person] believes...", "[Person] notes...", "[Person] claims...".

Step 2 — Verify each against the raw article using this decision tree:

  (a) THIRD-PARTY CLAIM STOLEN BY ANOTHER: The raw article attributes the claim
      to a specific named third party (Person B), but the draft credits Person A.
      → VIOLATION. Correct by crediting Person B.

  (b) REPORTED FACT MISATTRIBUTED TO AUTHOR: The raw article reports a
      specific fact using "sources say", "according to [Company/Executive]",
      "the company said", or similar — explicitly crediting a source OTHER than
      the newsletter author. But the draft credits the newsletter author
      (e.g. Thompson) with asserting that fact as his own claim.
      → VIOLATION. Correct by using "According to sources cited in the article"
      or quoting the actual source.
      NOTE: "According to sources" and "According to anonymous sources" are
      equivalent — do NOT flag one as a correction of the other.

  (c) AUTHOR'S OWN ANALYSIS: The claim represents the newsletter author's
      analytical framing, interpretation, conclusion, or strategic view —
      the kind of commentary the author weaves throughout their article.
      For Stratechery, the entire article is Thompson's analysis. His
      interpretations, framings, and conclusions are attributable to him even
      when the article text does not literally say "I argue" before each sentence.
      → NOT a violation.

      CRITICAL: Rule 4(c) applies when the draft attributes Thompson's OWN
      analytical conclusions to Thompson. It does NOT mean every fact Thompson
      quotes or cites should be re-credited to Thompson. If the draft says
      "According to [Named Third Party]" and the raw article confirms that
      third party as the actual source (e.g. their published post is being
      quoted), this is NEVER a violation — even if Thompson is the one
      presenting or quoting their data.

  (d) CORRECTLY ATTRIBUTED: The raw article clearly attributes the claim to the
      same person named in the draft → NOT a violation.

Pay special attention to section headers that assign ownership of an argument
to a named person — verify that the arguments in that section actually came
from that person in the raw article.

Example violation (wrong person): the raw article attributes a personalised-
advertising thesis to Eric Seufert, but the draft credits it to Sam Altman.
Correction: replace Sam Altman with Eric Seufert.

Example violation (reported fact): the raw article says "sources familiar with
the matter say revenue grew 3x", but the draft says "Thompson argues revenue
grew 3x". Correction: "According to sources cited in the article, revenue grew 3x."

Example NOT a violation: the draft says "Thompson concludes that Anthropic can
negotiate from a position of strength." This is Thompson's analytical conclusion
from reading the article's events — attributable to him even without a literal
"I conclude" in the raw text.

Correction: Quote the offending sentence, name who is wrongly credited in the
draft, name who actually made this claim per the raw article, and provide the
corrected sentence with the correct attribution.\
"""

_SOURCE_CONTEXT: dict[str, str] = {
    "stratechery": (
        "This is a Stratechery summary focused on macro tech strategy, business models, and market dynamics."
    ),
    "lenny": (
        "This is a Lenny's Newsletter summary focused on tactical product management, growth metrics, and team operations."
    ),
}

_OUTPUT_FORMAT = """\nRespond in this exact JSON format. Do not add any fields beyond what is listed.

IMPORTANT: Check for ALL violations across Rules 2 and 4 before responding.
Rules 1 (currency), 3 (list length), and 5 (guest context) are handled separately —
do not check or report them.

{
  "rule2_violations": [
    "Each entry: quote the exact offending text from the draft, name Rule 2, give a correction."
  ],
  "rule4_violations": [
    {
      "offending_quote": "exact offending sentence from the draft",
      "person_credited_in_draft": "the name as written in the draft",
      "person_credited_in_raw_article": "the name the raw article actually credits for this claim",
      "correction": "the corrected sentence with proper attribution"
    }
  ]
}

For rule4_violations: ONLY include an entry when person_credited_in_draft is a DIFFERENT
person from person_credited_in_raw_article.
If both the draft and the raw article credit the same person — e.g. draft says
"According to Sarah Frier" and raw article says "from a post by Sarah Frier" — do NOT
include it. Same person is never a violation regardless of phrasing differences.\n"""


def _build_system_prompt(newsletter_type: str) -> str:
    context = _SOURCE_CONTEXT.get(newsletter_type, _SOURCE_CONTEXT["stratechery"])
    return f"{_UNIVERSAL_RULES}\n\nSOURCE-SPECIFIC CONTEXT:\n{context}\n\n{_OUTPUT_FORMAT}"



# ---------------------------------------------------------------------------
# Rule 1 - deterministic currency figure check
# ---------------------------------------------------------------------------

_CURRENCY_RE = re.compile(
    r'\$[\d,]+(?:\.\d+)?\s*(?:trillion|billion|million|thousand|T|B|M|K)\b'
    r'|\$[\d,]+(?:\.\d+)?',
    re.IGNORECASE,
)

_SCALE_MAP = {
    'trillion': 'trillion', 't': 'trillion',
    'billion': 'billion',   'b': 'billion',
    'million': 'million',   'm': 'million',
    'thousand': 'thousand', 'k': 'thousand',
}



_SCALE_TO_BILLIONS = {
    'trillion': 1e3,   # 1 trillion = 1000 billion
    'billion': 1.0,
    'million': 1e-3,   # 1 million = 0.001 billion
    'thousand': 1e-6,  # 1 thousand = 0.000001 billion
}


def _to_billions(value, scale):
    """Convert a (value, scale) pair to canonical billions for cross-scale comparison."""
    return value * _SCALE_TO_BILLIONS.get(scale, 1e-9)  # unscaled: treat as dollars -> billions


def _normalize_figure(raw):
    """Parse a raw currency string like "$6B" or "$6 billion" into (value, scale)."""
    raw = raw.strip()
    num_part = raw.lstrip('$').replace(',', '').strip()
    m = re.match(r'^([\d.]+)\s*(\w+)?$', num_part, re.IGNORECASE)
    if not m:
        return (0.0, None)
    value = float(m.group(1))
    unit = m.group(2)
    scale = _SCALE_MAP.get(unit.lower(), None) if unit else None
    return (value, scale)


def _build_search_pattern(value, scale):
    """Build a regex that matches all surface forms of a normalized figure."""
    if value == int(value):
        num_str = str(int(value))
    else:
        num_str = str(value)
    escaped = re.escape(num_str)
    if scale:
        aliases = {
            'trillion': r'(?:trillion|T)',
            'billion':  r'(?:billion|B)',
            'million':  r'(?:million|M)',
            'thousand': r'(?:thousand|K)',
        }
        alias = aliases.get(scale, scale)
        return re.compile(r'\$' + escaped + r'(?:\.\d+)?\s*' + alias + r'\b', re.IGNORECASE)
    else:
        return re.compile(r'\$' + escaped + r'(?:\.\d+)?\b', re.IGNORECASE)


def _check_currency_figures(draft, raw_article, financial_block):
    """
    Extract all currency figures from the draft and flag any whose canonical value
    (normalized to billions) does not appear in the raw article or financial_block.
    Handles cross-scale equivalence ($2.31T = $2310B) and minor rounding differences
    (1% relative tolerance, ~$100M on a $10B figure).
    """
    # Build a list of canonical values (in billions) from source texts
    source_values = []
    for text in [raw_article, financial_block]:
        for m in _CURRENCY_RE.finditer(text):
            v, s = _normalize_figure(m.group())
            if v > 0:
                source_values.append(_to_billions(v, s))

    def _is_verified(b):
        return any(
            abs(b - sv) / max(abs(sv), 1e-9) < 0.015  # 1.5% relative tolerance
            for sv in source_values
        )

    violations = []
    seen_raws = set()

    for match in _CURRENCY_RE.finditer(draft):
        raw = match.group()
        v, s = _normalize_figure(raw)
        if v == 0:
            continue
        b = _to_billions(v, s)
        # Deduplicate by checking if we already verified/flagged a close value
        if any(abs(b - seen_b) / max(abs(seen_b), 1e-12) < 0.015 for seen_b in seen_raws):
            continue
        seen_raws.add(b)

        if _is_verified(b):
            continue  # verified

        violations.append(
            f'Rule 1 - UNVERIFIED FIGURE: "{raw}" not found in source article '
            f'or verified financial data. '
            f'Correction: Remove this figure or attribute it to a named source from the article.'
        )

    return violations


# ---------------------------------------------------------------------------
# Rule 3 — deterministic list-length check
# ---------------------------------------------------------------------------

_LIST_ITEM_RE = re.compile(r'^\s*(?:[-•*]|\d+\.)\s')

# Matches vague anonymous person references: "a senior leader", "an executive",
# "the guest", etc. -- up to 4 modifier words between the article and the role noun.
_VAGUE_PERSON_RE = re.compile(
    r'(a|an)\s+(?:\w+\s+){0,4}(executive|leader|analyst|researcher|officer|guest|interviewee)'
    r'|'
    r'the\s+(guest|interviewee)',
    re.IGNORECASE,
)


def _check_list_length(draft: str) -> list[str]:
    """Return one violation string per bulleted/numbered list with >5 items."""
    violations = []
    current: list[str] = []

    def _flush():
        if len(current) > 5:
            violations.append(
                f"Rule 3 — LIST LENGTH: list has {len(current)} items "
                f"(max 5). Compress to 3 items. "
                f"First item: \"{current[0][:80]}\". "
                f"Correction: remove the {len(current) - 3} least informative entries."
            )
        current.clear()

    for line in draft.split('\n'):
        if _LIST_ITEM_RE.match(line):
            current.append(line.strip())
        else:
            _flush()
    _flush()
    return violations


def _check_guest_context(draft: str) -> list[str]:
    """Return one violation string per vague anonymous person reference."""
    violations = []
    for match in _VAGUE_PERSON_RE.finditer(draft):
        # Skip appositive descriptions that follow a proper name + comma
        # e.g. "Eric Seufert, a mobile growth analyst" -- comma signals appositive
        pre = draft[max(0, match.start() - 10):match.start()]
        if re.search(r',\s*$', pre):
            continue
        violations.append(
            f"Rule 5 — GUEST CONTEXT: vague reference \"{match.group()}\" found. "
            f"Replace with the person's full name, title, and company from the raw article."
        )
    return violations



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _is_same_person(a: str, b: str) -> bool:
    """True if two person references appear to name the same individual."""
    a_norm, b_norm = a.lower().strip(), b.lower().strip()
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    a_last = a_norm.split()[-1] if a_norm else ""
    b_last = b_norm.split()[-1] if b_norm else ""
    if a_last and a_last == b_last:
        return True
    return a_norm in b_norm or b_norm in a_norm


def audit(review_input: ReviewInput, client: OpenAI) -> ReviewResult:
    """
    Audit a draft summary against the raw article and verified financial data.

    Returns a ReviewResult with action="PASS" or "FAIL". On FAIL, critique
    contains a numbered list of violations with exact correction instructions
    for the Summarizer to act on.
    """
    # Rules 1, 3, and 5 are checked deterministically before calling the LLM
    currency_violations = _check_currency_figures(
        review_input.draft_summary,
        review_input.raw_article,
        review_input.financial_block,
    )
    list_violations = _check_list_length(review_input.draft_summary)
    guest_violations = _check_guest_context(review_input.draft_summary)

    system_prompt = _build_system_prompt(review_input.newsletter_type)

    user_message = (
        f"DRAFT SUMMARY TO AUDIT:\n"
        f"(This is the ONLY document you are auditing. Flag violations here only.)\n"
        f"{review_input.draft_summary}\n\n\n--- REFERENCE DOCUMENT — DO NOT AUDIT THIS ---\n"
        f"RAW ARTICLE (use only to verify attributions for Rule 4 — do not flag its content):\n"
        f"{review_input.raw_article}\n\n\nAudit ONLY the DRAFT SUMMARY. Use the raw article solely to "
        "cross-check attributions for Rule 4. Return your verdict in the required JSON format."
    )
    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )

    try:
        result = json.loads(response.choices[0].message.content)

        rule2_raw = result.get("rule2_violations", [])
        if isinstance(rule2_raw, str):
            rule2_raw = [rule2_raw]
        rule2_items = [str(v).strip() for v in rule2_raw if str(v).strip()]

        rule4_items = []
        for v in result.get("rule4_violations", []):
            draft_person = v.get("person_credited_in_draft", "")
            raw_person = v.get("person_credited_in_raw_article", "")
            if _is_same_person(draft_person, raw_person):
                continue  # deterministic false-positive filter
            # If the LLM says the raw article credits the newsletter author
            # but the draft credits a named third party, the draft is MORE
            # correct (it names the actual source). Filter it out.
            _author_tokens = {"thompson", "ben thompson"}
            if raw_person.lower().strip() in _author_tokens:
                continue
            quote = v.get("offending_quote", "")
            correction = v.get("correction", "")
            rule4_items.append(
                f'Rule 4 — POV ATTRIBUTION: "{quote}". '
                f'Draft credits {draft_person}; raw article credits {raw_person}. '
                f'Correction: {correction}'
            )

        llm_items = rule2_items + rule4_items
        llm_action = "FAIL" if llm_items else "PASS"
        llm_critique = "\n".join(llm_items)
    except (json.JSONDecodeError, KeyError) as exc:
        llm_action = "FAIL"
        llm_critique = f"Reviewer returned malformed output and could not be parsed: {exc}"

    # Merge all deterministic violations with LLM results
    det_violations = currency_violations + list_violations + guest_violations
    if not det_violations:
        return ReviewResult(action=llm_action, critique=llm_critique)

    # Combine: deterministic hits first, then any LLM violations (stripping existing numbers)
    _num_re = re.compile(r'^\d+\.\s*')
    llm_items = [_num_re.sub('', s).strip() for s in llm_critique.split('\n') if s.strip()] if llm_critique else []
    all_items = det_violations + llm_items
    combined = "\n".join(f"{i}. {v}" for i, v in enumerate(all_items, 1))
    return ReviewResult(action="FAIL", critique=combined)
