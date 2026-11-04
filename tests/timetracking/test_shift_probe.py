"""Shift-field probe cadence: frequent until found, then daily; snoozable."""
import os
from datetime import datetime, timedelta

from cogs.timetracking.odoo.client import OdooClient, _shift_probe_snoozed
from _helpers import asynctest

KEY = "ODOO_SHIFT_PROBE_SNOOZE_UNTIL"


def _client():
    return OdooClient("https://x.odoo.com/json/2", "db", "u", "k")   # .loaded is True


def _clear():
    os.environ.pop(KEY, None)


def test_snoozed_reads_env():
    try:
        _clear()
        assert _shift_probe_snoozed() is False
        os.environ[KEY] = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        assert _shift_probe_snoozed() is True
        os.environ[KEY] = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        assert _shift_probe_snoozed() is False          # expired
        os.environ[KEY] = "garbage"
        assert _shift_probe_snoozed() is False          # unparseable
    finally:
        _clear()


def test_interval_frequent_until_found_then_daily():
    try:
        _clear()
        c = _client()
        c.shift_field_available = False
        assert c._shift_probe_interval() == 300.0        # not found -> every 5 min
        c.shift_field_available = True
        assert c._shift_probe_interval() == 86400.0      # found -> daily
        c.shift_field_available = False
        os.environ[KEY] = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        assert c._shift_probe_interval() == 86400.0      # snoozed -> daily even though not found
    finally:
        _clear()


@asynctest
async def test_maybe_check_gates_on_schedule_and_reset():
    try:
        _clear()
        c = _client()
        calls = []

        async def _fake_check():
            calls.append(1)
            c.shift_field_available = False   # keep it "not found" -> 5-min interval

        c.check_shift_field = _fake_check
        await c.maybe_check_shift_field()     # starts due (next=0) -> probes
        assert len(calls) == 1
        await c.maybe_check_shift_field()     # not due yet (300s out) -> no probe
        assert len(calls) == 1
        c.reset_shift_probe_schedule()        # snooze/resume forces an immediate re-probe
        await c.maybe_check_shift_field()
        assert len(calls) == 2
    finally:
        _clear()


@asynctest
async def test_maybe_check_noop_when_not_loaded():
    c = OdooClient(None, None, None, None)    # .loaded is False
    called = []
    async def _fake_check(): called.append(1)
    c.check_shift_field = _fake_check
    await c.maybe_check_shift_field()
    assert called == []
