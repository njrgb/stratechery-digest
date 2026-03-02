"""
Unit tests for lenny.py is_fresh() — the email age filter.

These tests document the tradeoffs between different max_hours values:
  - Too small → emails sent near the previous run time get silently dropped
  - Too large → emails already processed yesterday get re-sent (duplicate)

Run with: python -m pytest tests/test_freshness.py -v
No API key required.
"""

import pytest
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime

from lenny import is_fresh


def hours_ago(n: float) -> str:
    """Return an RFC 2822 date string for a time n hours in the past."""
    dt = datetime.now(timezone.utc) - timedelta(hours=n)
    return format_datetime(dt)


# ---------------------------------------------------------------------------
# Test matrix
# Each entry: (id, description, date_str, max_hours, expected)
# ---------------------------------------------------------------------------

_CASES = [
    # --- Happy path: clearly fresh, any window catches these ---
    ("TC-F-01", "Very recent email (2h ago)",          hours_ago(2),    23, True),
    ("TC-F-02", "Mid-day email (12h ago)",             hours_ago(12),   23, True),
    ("TC-F-03", "Late yesterday, well inside window",  hours_ago(20),   23, True),

    # --- 23h window edge cases ---
    ("TC-F-04", "Just inside 23h window (22.5h ago)",  hours_ago(22.5), 23, True),
    ("TC-F-05", "Just outside 23h window (23.5h ago)", hours_ago(23.5), 23, False),  # currently DROPPED

    # --- GH Actions delay scenario ---
    # Workflow fires 30min late; email sent just before yesterday's run is now ~23.5h old.
    # 23h window drops it. 26h window saves it.
    ("TC-F-06", "GH delayed 30min: email 23.5h old, window=26h", hours_ago(23.5), 26, True),
    ("TC-F-07", "GH delayed 30min: email 24.5h old, window=26h", hours_ago(24.5), 26, True),

    # --- Duplicate risk zone ---
    # An email 25h old was already caught by yesterday's 26h window.
    # With a 26h window today it would be sent again → duplicate.
    # This test documents the risk; the expected value is True (it WOULD be re-sent).
    ("TC-F-08", "Duplicate risk: 25h old already processed yesterday, window=26h",
     hours_ago(25), 26, True),   # ← re-send risk; acceptable only if Lenny sends ≤1/day

    # --- Safely outside any reasonable window ---
    ("TC-F-09", "Just outside 26h window (27h ago)",   hours_ago(27),   26, False),
    ("TC-F-10", "Old email (48h ago)",                 hours_ago(48),   26, False),
    ("TC-F-11", "Very old email (72h ago)",            hours_ago(72),   26, False),

    # --- Error handling ---
    ("TC-F-12", "Malformed date string",   "not-a-date",  23, False),
    ("TC-F-13", "Empty date string",       "",            23, False),
    ("TC-F-14", "Partial date string",     "Mon, 02 Mar", 23, False),
]


@pytest.mark.parametrize(
    "tc_id, description, date_str, max_hours, expected",
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_is_fresh(tc_id, description, date_str, max_hours, expected):
    result = is_fresh(date_str, max_hours=max_hours)
    assert result == expected, (
        f"[{tc_id}] {description}\n"
        f"  max_hours={max_hours}, expected={expected}, got={result}"
    )
