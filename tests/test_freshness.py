"""
Unit tests for lenny.py is_fresh() — the email age filter.

With two runs per day (6am PT + noon PT, 6h apart), max_hours=8 ensures:
  - Each run catches emails from its own window without re-processing
    articles already handled by the previous run
  - 2h overlap zone (4am–6am) is safe because neither newsletter
    publishes multiple articles within a 2-hour window

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
# Test matrix — calibrated for max_hours=8 (two runs, 6h apart)
# Each entry: (id, description, date_str, max_hours, expected)
# ---------------------------------------------------------------------------

_CASES = [
    # --- Happy path: clearly fresh, comfortably inside 8h window ---
    ("TC-F-01", "Very recent email (1h ago)",          hours_ago(1),   8, True),
    ("TC-F-02", "Mid-window email (4h ago)",           hours_ago(4),   8, True),
    ("TC-F-03", "Just inside 8h window (7.5h ago)",    hours_ago(7.5), 8, True),

    # --- Boundary: just outside the 8h window ---
    ("TC-F-04", "Just outside 8h window (8.5h ago)",   hours_ago(8.5), 8, False),

    # --- GH Actions delay scenario ---
    # Workflow fires 30min late; an email sent 7.5h ago is now 8h old.
    # 8h window still catches it (7.5 < 8).
    ("TC-F-05", "GH delayed 30min: email 7.5h old",    hours_ago(7.5), 8, True),

    # --- No duplicate zone ---
    # Runs are 6h apart. An article caught by the 6am run (e.g. sent at 3am)
    # is 9h old by the noon run → safely outside the 8h window, not re-sent.
    ("TC-F-06", "Prev run article now 9h old — not re-sent", hours_ago(9), 8, False),
    ("TC-F-07", "Prev run article now 12h old — not re-sent", hours_ago(12), 8, False),

    # --- Overlap zone (4am–6am): theoretical duplicate risk ---
    # An email sent 5h ago falls inside both the 6am and noon run windows.
    # Acceptable because neither newsletter publishes twice in a 2h span.
    ("TC-F-08", "Overlap zone: 5h old, inside both windows", hours_ago(5), 8, True),

    # --- Safely old — no window catches these ---
    ("TC-F-09", "Old email (24h ago)",  hours_ago(24), 8, False),
    ("TC-F-10", "Old email (48h ago)",  hours_ago(48), 8, False),

    # --- Error handling ---
    ("TC-F-11", "Malformed date string",   "not-a-date",  8, False),
    ("TC-F-12", "Empty date string",       "",            8, False),
    ("TC-F-13", "Partial date string",     "Mon, 02 Mar", 8, False),
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
