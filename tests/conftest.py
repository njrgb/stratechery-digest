"""
Shared pytest fixtures for the reviewer eval suite.
All fixtures are session-scoped so fixture data is loaded once per test run.
"""

import json
import os
from pathlib import Path

import pytest
from openai import OpenAI

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def mock_article() -> str:
    return (FIXTURES / "mock_article.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def mock_financial_block() -> str:
    """Format mock_finance.json into a text block matching the summarizer's financial_block format."""
    data = json.loads((FIXTURES / "mock_finance.json").read_text(encoding="utf-8"))
    lines = []
    for co in data:
        mc = co.get("market_cap_usd", 0)
        rev = co.get("revenue_ttm_usd", 0)
        # Format using T for trillions, B for billions (matching LLM summarizer output)
        def _fmt(n):
            if n >= 1e12:
                return f"${n/1e12:.2f}T"
            if n >= 1e9:
                return f"${n/1e9:.1f}B"
            if n >= 1e6:
                return f"${n/1e6:.0f}M"
            return f"${n:,.0f}"
        name = co["name"]
        ticker = co["ticker"]
        rev_growth = co.get("revenue_growth_yoy_pct", 0)
        op_margin = co.get("operating_margin_pct", 0)
        lines.append(
            f"{name} ({ticker}): Market cap {_fmt(mc)} | Revenue TTM {_fmt(rev)} | "
            f"Revenue growth {rev_growth:.1f}% YoY | Operating margin {op_margin:.1f}%"
        )
    return "\n".join(lines)


@pytest.fixture(scope="session")
def openai_client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY environment variable not set")
    return OpenAI(api_key=api_key)
