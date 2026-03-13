"""
LLM-as-a-Judge evaluation suite for the Reviewer Agent.

Architecture:
  Actor    → summarize.py / lenny.py (produces draft_summary)
  Critic   → src/agents/reviewer.py  (audits draft, returns critique)
  Judge    → this file (GPT-4o-mini, temperature=0; grades the Critic)

Each test case is loaded from tests/fixtures/eval_dataset.json.
The judge scores three criteria (binary 1/0) per test:
  - Defect Catch Rate    : Did the reviewer correctly identify PASS or FAIL?
  - False Positive Rate  : Did the reviewer avoid hallucinating errors?
  - Critique Actionability: For FAIL cases, does the critique quote offending
                            text, name the rule, and give a correction?

All three must score 1 for a test to pass.
"""

import json
from pathlib import Path

import pytest
from openai import OpenAI

from src.agents.reviewer import ReviewInput, ReviewResult, audit

# ---------------------------------------------------------------------------
# Load eval dataset at module level (required for @pytest.mark.parametrize)
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures"
_eval_cases: list[dict] = json.loads(
    (_FIXTURES / "eval_dataset.json").read_text(encoding="utf-8")
)


# ---------------------------------------------------------------------------
# Judge logic
# ---------------------------------------------------------------------------

_JUDGE_PROMPT = """\
You are an impartial evaluation judge assessing a newsletter Reviewer Agent.

TEST SCENARIO: {scenario_id} — {description}
EXPECTED VERDICT: {expected_action}
EXPECTED CONCEPT IN CRITIQUE (if FAIL): "{expected_keyword}"
REVIEWER'S ACTUAL VERDICT: {actual_action}
REVIEWER'S CRITIQUE:
{critique_text}

Score each criterion as 1 (satisfactory) or 0 (unsatisfactory):

DEFECT_CATCH_RATE:
- If expected_action is FAIL: score 1 only if (a) reviewer outputted FAIL, AND
  (b) the critique addresses the concept of "{expected_keyword}" — it does not
  need to use the exact word, but must clearly identify that defect category.
  Important: the keyword may appear anywhere in the critique — inside a quoted
  passage from the draft, in the rule identification, or in the correction
  instruction (e.g. "The raw article attributes this to Eric Seufert...").
  Read the full critique carefully, including all quoted text and correction
  instructions, before scoring this criterion.

  Keyword-to-concept mapping (use this to decide if the concept is addressed):
  • "GUEST"   → satisfied by any of: "GUEST CONTEXT", "Rule 5", vague person
                 reference, missing full name / title / company
  • "BANNED"  → satisfied by: "Rule 2", banned phrase identified (delve, ever-evolving,
                 the author argues, the article discusses, the piece explores)
  • "LIST"    → satisfied by: "Rule 3", list too long, too many bullet points
  • "private" → satisfied by: "Rule 1", private company financial, unattributed figure
                 for a private company (OpenAI, Stripe, Anthropic, etc.)
  • "Seufert" → satisfied by: misattribution involving Eric Seufert, "Rule 4"
  • ""        → for PASS cases only; ignore this field and check only the verdict.

- If expected_action is PASS: score 1 only if reviewer outputted PASS.

FALSE_POSITIVE_RATE:
- If expected_action is PASS: score 1 only if the critique is empty or contains
  no identified violations.
- If expected_action is FAIL: score 1 if the reviewer identified real defects
  from the draft rather than fabricating issues that are not present.
  Score 0 if the reviewer invents violations that do not exist in the draft.

CRITIQUE_ACTIONABILITY:
- If expected_action is FAIL: score 1 only if the critique (a) quotes specific
  offending text from the draft, (b) names the specific rule violated, and
  (c) provides a concrete correction instruction. Score 0 if vague.
- If expected_action is PASS: automatically score 1 (nothing to be actionable about).

Return JSON only — no other text:
{{"defect_catch_rate": 0 or 1, "false_positive_rate": 0 or 1, \
"critique_actionability": 0 or 1, "reasoning": "one concise sentence explaining your scores"}}\
"""


def _judge(
    *,
    test_case: dict,
    result: ReviewResult,
    client: OpenAI,
) -> dict:
    """Call GPT-4o-mini at temperature=0 to grade the Reviewer's output."""
    critique_text = result.critique if result.critique else "(empty)"
    prompt = _JUDGE_PROMPT.format(
        scenario_id=test_case["scenario_id"],
        description=test_case["description"],
        expected_action=test_case["expected_action"],
        expected_keyword=test_case["expected_critique_keyword"],
        actual_action=result.action,
        critique_text=critique_text,
    )
    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(response.choices[0].message.content)


# ---------------------------------------------------------------------------
# Parametrized eval test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "test_case",
    _eval_cases,
    ids=[tc["scenario_id"] for tc in _eval_cases],
)
def test_reviewer_eval(
    test_case: dict,
    mock_article: str,
    mock_financial_block: str,
    openai_client: OpenAI,
) -> None:
    # --- Step 1: Run the Reviewer Agent ---
    review_input = ReviewInput(
        raw_article=mock_article,
        financial_block=mock_financial_block,
        draft_summary=test_case["draft_summary"],
        newsletter_type="stratechery",
    )
    result = audit(review_input, openai_client)

    # --- Step 2: Run the LLM Judge ---
    scores = _judge(test_case=test_case, result=result, client=openai_client)

    scenario = test_case["scenario_id"]
    reasoning = scores.get("reasoning", "")

    # --- Step 3: Assert all three criteria ---
    assert scores["defect_catch_rate"] == 1, (
        f"[{scenario}] DEFECT_CATCH_RATE failed.\n"
        f"  Expected verdict: {test_case['expected_action']}\n"
        f"  Reviewer verdict: {result.action}\n"
        f"  Expected concept: '{test_case['expected_critique_keyword']}'\n"
        f"  Reviewer critique: {result.critique[:300]}\n"
        f"  Judge reasoning: {reasoning}"
    )
    assert scores["false_positive_rate"] == 1, (
        f"[{scenario}] FALSE_POSITIVE_RATE failed — reviewer hallucinated errors.\n"
        f"  Reviewer critique: {result.critique[:300]}\n"
        f"  Judge reasoning: {reasoning}"
    )
    assert scores["critique_actionability"] == 1, (
        f"[{scenario}] CRITIQUE_ACTIONABILITY failed — critique is not actionable.\n"
        f"  Reviewer critique: {result.critique[:300]}\n"
        f"  Judge reasoning: {reasoning}"
    )
