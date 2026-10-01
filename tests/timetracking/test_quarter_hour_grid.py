"""Consolidated quarter-hour grid: every caller agrees with the pre-refactor formulas."""
import pytest

from cogs.timetracking.odoo import sync
from cogs.timetracking.odoo.inbox import _quarter_hour_minutes
from cogs.timetracking.views import round_quarter_hours
from cogs.timetracking.cog import _hours_to_minutes

SPANS = [None, -1, -0.1, 0, 0.01, 0.12, 0.13, 0.24, 0.25, 0.3, 1.0, 3.2167, 7.9, 23.9, 24.0, 30.0]


def _old_grid(h):
    return int(round((h or 0) * 4) / 4 * 60)


def test_sync_grid_matches_old_formula():
    for h in SPANS:
        assert sync.quarter_hour_minutes(h) == _old_grid(h), h


def test_inbox_wrapper_clamps_to_schema_range():
    for h in SPANS:
        assert _quarter_hour_minutes(h) == max(0, min(1440, _old_grid(h))), h


def test_views_round_quarter_matches_old_and_floors_negatives():
    for h in [x for x in SPANS if x is not None and x >= 0]:
        assert round_quarter_hours(h) == ((round(h * 4) / 4) or 0.25), h
    assert round_quarter_hours(-1) == 0.25


def test_cog_hours_to_minutes_matches_old_and_rejects_out_of_range():
    for h in [x for x in SPANS if x is not None and 0 <= x <= 24]:
        assert _hours_to_minutes(h) == _old_grid(h), h
    for bad in (30.0, -1):
        with pytest.raises(ValueError):
            _hours_to_minutes(bad)
