# Training videos — outlines, scripts & written guides

This folder holds the plan for the how-to videos and the **written guide that pairs
with each video** (same steps, for people who'd rather read). Each file is one video.

## The video set

| # | File | Audience | Status |
| --- | --- | --- | --- |
| E1 | [employee-1-clocking-in-out.md](employee-1-clocking-in-out.md) | All employees | Ready to record |
| E2 | [employee-2-logging-work.md](employee-2-logging-work.md) | All employees | Ready to record |
| E3 | [employee-3-checking-your-timecard.md](employee-3-checking-your-timecard.md) | All employees | Ready to record |
| A1 | [admin-managing.md](admin-managing.md) | Timecard admins / managers | Ready to record |
| A2 | [admin-reporting.md](admin-reporting.md) | Timecard admins / office | **Skeleton** — some reports still being built; film once they're done |

Keep the three employee videos **short** (3–6 min each) so they're easy to watch and
easy to re-shoot when a button or screen changes.

## How each file is laid out

1. **At a glance** — audience, length target, what to have on screen before recording.
2. **Outline** — the sections, in order.
3. **Script** — a scene table: *On screen / action* ↔ *Narration* (word-for-word).
4. **Written guide** — the same steps as a numbered list; this is the text that pairs
   with the finished video (hand it out, or paste into the wiki).

## Recording notes (apply to all videos)

- **Use a test/demo server, not production** — never show real employees' pay, real
  customer names, or anything sensitive. A sanitized test database is ideal (see the
  wiki *Testing* page).
- Show the **actual buttons/commands** on screen while narrating; the scripts call out
  exactly what to click.
- When a screen shows an **id** (like `#3887`), mention that ids are how admins find and
  edit a specific punch or worktime — employees don't need to memorize them.
- The bot's full command list lives in the wiki **Commands Reference**; these videos
  cover the *common* day-to-day paths, not every option.

## Source of truth

These guides describe the bot as it exists today. If a button label or command changes,
update the matching file here (and the wiki) so the written guide and the video stay in
sync. Reporting (A2) intentionally has **placeholders** for reports that aren't built
yet — fill them in as each report ships.
