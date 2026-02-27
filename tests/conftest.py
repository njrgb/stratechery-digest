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
def mock_finance() -> list[dict]:
    return json.loads((FIXTURES / "mock_finance.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def openai_client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY environment variable not set")
    return OpenAI(api_key=api_key)
