# Timetracking test suite

Pytest tests for the timecard cog. They live in the repo (not the session
scratchpad) so they persist across sessions.

Each test seeds a throwaway SQLite DB in a temp dir and drives the code under test
with light fakes for Discord/Odoo — no network, no real credentials, all seed data
synthetic. Async tests are plain functions wrapped with `@asynctest`
(`asyncio.run` under the hood), so **no `pytest-asyncio` plugin is required** — just
`pytest`. Every test gets its DB from `db_ctx`, which closes it on exit.

## Running

From the **repo root**, with the project venv:

```bash
# everything
tests/timetracking/run_all.sh
#   or:
discord-bot-venv/bin/python -m pytest tests/timetracking

# one file / one test
discord-bot-venv/bin/python -m pytest tests/timetracking/test_editworktime_rules.py
discord-bot-venv/bin/python -m pytest tests/timetracking/test_editworktime_rules.py -k task_only
```

Install the test dependency once (into whatever venv runs the bot):

```bash
discord-bot-venv/bin/python -m pip install -r tests/requirements-dev.txt
```

## Layout

| File | Covers |
| --- | --- |
| `conftest.py` | Puts the repo root on `sys.path`; points `LOG_FILE` at a temp file. |
| `_helpers.py` | `@asynctest`, `db_ctx`/`new_db`, `add_punch`/`add_worktime`, `outbox()`. |
| `test_worktime_autoclose.py` | Odoo check-out ends a locally-open jobsite worktime (inbox `_reconcile_attendance` / `_close_open_worktimes`). |
| `test_worktime_finish_helpers.py` | `round_quarter_hours` + `finalize_worktime` (shared by End-Work-Now and the auto-close). |
| `test_quarter_hour_grid.py` | `sync.quarter_hour_minutes` and the inbox/views/cog wrappers all match the pre-refactor formulas. |
| `test_resync_queue_unpushed.py` | `/resync` `_queue_unpushed`: full sweep + targeted id, parent-punch legacy skip, dedup, and the no-project/zero-hour/already-synced skips. |
| `test_editworktime_rules.py` | `/editworktime`: local-only non-legacy push gate, customer-change block while Odoo is connected, and the project/task link rules. |

## Adding a test

Name the file `test_<feature>.py` and the functions `test_*`. For async, write
`async def` and decorate with `@asynctest`; get a DB with `async with db_ctx() as db:`
so it's always closed. Keep tests dependency-light and self-contained, and never put
real data (names, tokens, Odoo URLs, phone numbers) in them.
