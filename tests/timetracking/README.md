# Timetracking verification scripts

Standalone sanity checks for the timecard cog. Each file is a plain script (not
pytest): it seeds a throwaway SQLite DB in a temp dir, drives the code under test with
fakes for Discord/Odoo, asserts the outcome, prints `OK: …` per case, and ends with
`… VERIFICATION PASSED`. A failed assertion exits non-zero.

They live in the repo (not the session scratchpad) so they survive across sessions.

## Running

Run from the **repo root** (the scripts do `sys.path.insert(0, os.getcwd())`), using the
project venv:

```bash
# one script
discord-bot-venv/bin/python tests/timetracking/verify_editworktime_rules.py

# all of them
tests/timetracking/run_all.sh
```

Needs the bot's deps (py-cord, aiosqlite, pytz, openpyxl …) — the committed
`discord-bot-venv/` already has them; any venv with the bot installed works.

Each script ends with `os._exit(0)` on purpose: aiosqlite's connection threads are
non-daemon here, so a normal return can hang the interpreter at shutdown. A trailing
`RuntimeError: Event loop is closed` from those background threads is harmless.

## What's covered

| Script | Covers |
| --- | --- |
| `verify_resync_queue_unpushed.py` | `/resync` `_queue_unpushed`: full sweep + targeted id, parent-punch legacy skip, dedup, and the no-project / zero-hour / already-synced skips. |
| `verify_editworktime_rules.py` | `/editworktime`: local-only non-legacy push gate (legacy punch never pushes), customer-change blocked while Odoo is connected, and the project/task link rules (project-only clears task; project+task must agree; task-only validates against the current project; customer follows the work item). |

## Adding a new one

Name it `verify_<feature>.py`, follow the same shape (temp DB + fakes + `OK:` lines +
a final `… VERIFICATION PASSED`, then `sys.stdout.flush(); os._exit(0)`), and add a row
above. Keep them dependency-light and self-contained.
