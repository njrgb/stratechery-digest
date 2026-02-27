"""
Reviewer Agent (Editor-in-Chief)

Audits a newsletter draft summary against the raw article text and verified
YFinance data before it is emailed. Returns a binary PASS/FAIL verdict and,
on FAIL, an actionable critique for the Summarizer to use in a revision.

Integration point: call audit() after summarize_article() / summarize_community()
returns the draft and before send_summary_email() is called.
"""

import json
from openai import OpenAI
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class ReviewInput(BaseModel):
    raw_article: str
    yfinance_data: list[dict]
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
to a senior technology professional. You have five non-negotiable rules.
Return FAIL if ANY rule is violated. Return PASS only when zero violations exist.

RULE 1 — PRIVATE COMPANY FINANCIALS
This rule applies ONLY to companies NOT listed in the "Verified YFinance Data"
block. Companies in that block (e.g. GOOGL, META) are verified public companies
— never apply Rule 1 to them.

For private companies (e.g. OpenAI, Stripe, Anthropic, SpaceX, Figma), work
through this decision tree in order and stop at the first match:

  STEP 1 — Is it a stock market metric?
  The following metrics exist ONLY for publicly traded companies. Flag them
  unconditionally, regardless of any attribution:
    • Market capitalisation / market cap
    • Stock price / share price
    • P/E ratio / price-to-earnings
    • Enterprise value / EV
  → VIOLATION. Flag it.

  STEP 2 — Is it a qualitative statement with no specific number?
  Analytical observations about cost trajectories, business models, or
  competitive dynamics that cite no specific figure are NOT financial metrics.
  Do NOT flag these under any circumstances:
    ✓ "OpenAI's costs scale roughly in parallel with revenue"
    ✓ "OpenAI's cost structure shows no margin leverage"
    ✓ "revenue and compute scaling in parallel"
    ✓ "Google subsidising Gemini through Search"
  → NOT A VIOLATION. Do not flag it.

  STEP 3 — Does the draft name a specific person or publication as the source?
  Operational metrics (revenue, ARR, compute costs, growth figures) are
  acceptable when the draft explicitly names the source person or publication:
    ✓ "Per figures published by OpenAI CFO Sarah Frier, revenue grew 3X..."
    ✓ "Per figures published by OpenAI CFO Sarah Frier, revenue and compute
       have tracked each other at 3X per year: from $2B ARR..."
    ✓ "According to OpenAI's CFO, ARR reached $20B+"
    ✓ "Per Bloomberg, Stripe's ARR exceeded $10B"
  → NOT A VIOLATION. Do not flag it.

  STEP 4 — A specific figure with no named source.
  A concrete number cited with no named person or publication is a violation:
    ✗ "OpenAI's revenue reached $20B+ ARR" (no attribution)
    ✗ "Compute grew 9.5X from 2023 to 2025" (no attribution)
    ✗ "OpenAI: Market cap $300B | ARR $20B+" (stock metric + no attribution)
  → VIOLATION. Flag it.

Correction: Remove the unattributed figure or attribute it to a named source.

RULE 2 — BANNED PHRASES
The following phrases are banned. Flag every occurrence with the exact quoted text:
  • "In the ever-evolving landscape"
  • "delve into" / "delves into" / "delving into"  ← always banned, regardless of context
  • "the author argues" / "the piece explores" / "the article discusses" / "the author discusses"

CRITICAL: The last category bans ONLY anonymous, unattributed references to the author or piece.
Named attributions using a specific person's name are NEVER a Rule 2 violation:
  ✓ "Thompson argues..." — NOT banned (named person)
  ✓ "Thompson concludes..." — NOT banned (named person)
  ✓ "Thompson contends..." — NOT banned (named person)
  ✓ "Seufert believes..." — NOT banned (named person)
  ✗ "the author argues..." — BANNED (anonymous, no name)
  ✗ "the piece explores..." — BANNED (refers to article, not person)
  ✗ "the article discusses..." — BANNED (refers to article, not person)

Do NOT flag any sentence that attributes a view to a named individual.

Correction: Replace with specific, factual, direct language.

RULE 3 — LIST LENGTH
Any single bulleted or numbered list with more than 5 items is bloated.
Correction: State the exact item count and instruct the Summarizer to compress
the list to a maximum of 3 items by removing the least informative entries.

RULE 4 — POV ATTRIBUTION
Cross-reference every attributed argument or thesis in the draft against the
raw article. If the draft credits Person A with an argument that the raw article
attributes to a different person, that is a misattribution violation.

Pay special attention to:
  • Section headers that assign ownership of an argument to a person
    (e.g. "SAM ALTMAN'S VISION FOR PERSONALISED ADVERTISING" or
    "ERIC SEUFERT ON AD MODELS") — verify in the raw article that the arguments
    in that section actually came from the named person.
  • Sentences using "[Person] has argued...", "[Person] believes...",
    "[Person] contends..." — cross-check who actually said this in the article.

Example violation: the raw article attributes a personalised-advertising thesis
to Eric Seufert (a mobile growth analyst), but the draft credits the same
thesis to Sam Altman under a section titled "SAM ALTMAN'S VISION FOR
PERSONALISED ADVERTISING".

Example correction for the above: "Sam Altman has argued that personalised,
conversion-optimised advertising is a superior business model..." misattributes
this argument to Sam Altman. Per the raw article, this thesis was stated by
Eric Seufert, a mobile growth analyst. Replace with: "Eric Seufert, a mobile
growth analyst, has argued that personalised, conversion-optimised advertising..."

Correction: Quote the offending sentence, explicitly name who is wrongly
credited in the draft, explicitly name who actually made this argument per the
raw article, and provide the corrected sentence with the correct attribution.

RULE 5 — GUEST CONTEXT
Any person introduced in the draft must have their full name, current title, and
current company stated on first mention.

VIOLATIONS — vague references that omit the person's name:
  ✗ "A senior OpenAI leader published five principles..."
  ✗ "an executive outlined the company's approach..."
  ✗ "a senior leader", "the guest", "the interviewee", "an analyst"

ACCEPTABLE — proper introductions with full name + title + company:
  ✓ "Fidji Simo, CEO of Applications at OpenAI, published..."
  ✓ "Eric Seufert, a mobile growth analyst, has argued..."
  ✓ "Sarah Frier, CFO of OpenAI, published data showing..."

Do NOT flag a person who is introduced with their full name and a title or role,
even if the title is brief. "Fidji Simo, CEO of Applications at OpenAI" is a
correct introduction — do not flag it.

EXCEPTION — Source article limitation:
If the raw article itself never names the person (only refers to them by a
description such as "a senior AI researcher"), then the draft using that same
description is NOT a violation. Only flag vague references when the raw article
provides the person's full name and the draft omits it.

Correction: Quote the vague reference and instruct the Summarizer to replace it
with the person's full name, title, and company as given in the raw article.\
"""

_SOURCE_CONTEXT: dict[str, str] = {
    "stratechery": (
        "This is a Stratechery summary focused on macro tech strategy, business "
        "models, and market dynamics. Rigorously verify all financial figures "
        "against the YFinance data block — Stratechery frequently analyses public "
        "company financials and any figure not in the verified data is suspect."
    ),
    "lenny": (
        "This is a Lenny's Newsletter summary focused on tactical product management, "
        "growth metrics, and team operations. Flag any stock or valuation metrics "
        "for private SaaS companies. Lenny's rarely discusses live stock data; "
        "financial figures are more likely to be hallucinated or misattributed."
    ),
}

_OUTPUT_FORMAT = """\
Respond in this exact JSON format. Do not add any fields beyond what is listed.
The "critique" value must always be a plain JSON string — never an array or object.

{
  "action": "PASS" or "FAIL",
  "critique": "If FAIL: a single string containing numbered violations. Each violation must: (1) quote the exact offending text FROM THE DRAFT SUMMARY ONLY, (2) name the rule number and title violated, (3) give a concrete correction instruction. Do not quote text from the raw article or YFinance data. If PASS: empty string."
}\
"""


def _build_system_prompt(newsletter_type: str) -> str:
    context = _SOURCE_CONTEXT.get(newsletter_type, _SOURCE_CONTEXT["stratechery"])
    return f"{_UNIVERSAL_RULES}\n\nSOURCE-SPECIFIC CONTEXT:\n{context}\n\n{_OUTPUT_FORMAT}"


def _format_yfinance(data: list[dict]) -> str:
    """Render yfinance data as a readable block for the model to cross-reference."""
    if not data:
        return "(No verified financial data provided — flag any financial metrics as unverified.)"
    lines = [
        "The following companies have verified public market data.",
        "Any company NOT listed here is private or unverified.\n",
    ]
    for co in data:
        lines.append(f"Company: {co.get('name', 'Unknown')} ({co.get('ticker', '?')})")
        if "market_cap_usd" in co:
            lines.append(f"  Market Cap: ${co['market_cap_usd']:,.0f}")
        if "price_usd" in co:
            lines.append(f"  Price: ${co['price_usd']:.2f}")
        if "revenue_ttm_usd" in co:
            lines.append(f"  Revenue (TTM): ${co['revenue_ttm_usd']:,.0f}")
        if "revenue_growth_yoy_pct" in co:
            lines.append(f"  Revenue Growth YoY: {co['revenue_growth_yoy_pct']:.1f}%")
        if "gross_margin_pct" in co:
            lines.append(f"  Gross Margin: {co['gross_margin_pct']:.1f}%")
        if "operating_margin_pct" in co:
            lines.append(f"  Operating Margin: {co['operating_margin_pct']:.1f}%")
        if "pe_ratio" in co:
            lines.append(f"  P/E Ratio: {co['pe_ratio']}")
        if "52w_high_usd" in co:
            lines.append(f"  52-Week High: ${co['52w_high_usd']:.2f}")
        if "52w_low_usd" in co:
            lines.append(f"  52-Week Low: ${co['52w_low_usd']:.2f}")
        if "segments" in co:
            lines.append(f"  Business Segments: {', '.join(co['segments'])}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def audit(review_input: ReviewInput, client: OpenAI) -> ReviewResult:
    """
    Audit a draft summary against the raw article and verified financial data.

    Returns a ReviewResult with action="PASS" or "FAIL". On FAIL, critique
    contains a numbered list of violations with exact correction instructions
    for the Summarizer to act on.
    """
    system_prompt = _build_system_prompt(review_input.newsletter_type)
    finance_block = _format_yfinance(review_input.yfinance_data)

    user_message = (
        f"DRAFT SUMMARY TO AUDIT:\n"
        f"(This is the ONLY document you are auditing. Flag violations here only.)\n"
        f"{review_input.draft_summary}\n\n"
        f"--- REFERENCE DOCUMENTS — DO NOT AUDIT THESE ---\n"
        f"VERIFIED YFINANCE DATA (use only to check Rule 1 claims in the draft above):\n"
        f"{finance_block}\n\n"
        f"RAW ARTICLE (use only to verify attributions for Rule 4 — do not flag its content):\n"
        f"{review_input.raw_article}\n\n"
        "Audit ONLY the DRAFT SUMMARY. Use the reference documents solely to "
        "cross-check claims made in the draft. Return your verdict in the required JSON format."
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
        raw_critique = result.get("critique", "")
        # Defensive: GPT occasionally returns critique as a list despite instructions
        if isinstance(raw_critique, list):
            raw_critique = "\n".join(str(item) for item in raw_critique)
        return ReviewResult(
            action=result.get("action", "FAIL"),
            critique=raw_critique,
        )
    except (json.JSONDecodeError, KeyError) as exc:
        return ReviewResult(
            action="FAIL",
            critique=f"Reviewer returned malformed output and could not be parsed: {exc}",
        )
