# Admin Video — Reporting  ⚠️ SKELETON / WORK IN PROGRESS

> **Status:** outline + draft script for the reports that exist today, with **placeholders**
> for reports still being built. Don't record the whole thing yet — film the "available
> now" sections when you're ready, and fill in each placeholder as that report ships.
> Managing the system is a separate video: [admin-managing.md](admin-managing.md).

## At a glance
- **Audience:** timecard admins / office staff who pull reports (timecard-admin role or
  Administrator).
- **Length target:** TBD (likely 8–12 min once all reports exist; consider splitting
  payroll vs. billing/audit).
- **Have on screen before recording:** a **test/demo server** with sanitized data, at least
  one **employee group**, pay rates set, and a **pay calendar** generated so the payroll
  reports have periods to run against. **Never show real pay or customer data.**
- **Goal:** by the end, an admin can produce each report, knows what it's for, and knows
  where it posts.

## Reporting ground rules (cover once, up front)
- Reports come out as **Excel workbooks** and post to the **timecard-reports** channel
  (some can be DM'd).
- Most reports are **group-scoped** — you pick an employee group, so set those up first
  (see the managing video: `/creategroup`, `/addtogroup`).
- Reports are **pay-sensitive**: they only run in the pay channels (log, timecard-log,
  timecard-admin, timecard-reports). Keep them there.
- Weeks end on **Saturday**; payroll runs on the **pay calendar**'s periods.

## Prerequisites checklist (show on screen)
- [ ] Employee **groups** created and populated.
- [ ] **Pay rates** set for everyone being costed (`/setpay`).
- [ ] **Pay calendar** generated (`/genpaycalendar` / `/addpayperiod`, view with `/paycalendar`).
- [ ] Leave / bonuses entered if the period includes them (`/addleave`, `/addbonus`).

---

## Part 1 — Reports available now

### 1a. Weekly timecard report — `/timecardreport`
- **What it is:** a multi-sheet Excel workbook, one sheet per employee, for a week ending
  on a Saturday, scoped to a chosen group. Unapproved punches are flagged.
- **Draft script beat:** "Pick the group and the week-ending Saturday; the bot builds the
  workbook and posts it to the reports channel. Each employee gets a tab; anything
  unapproved is flagged so you can chase it before payroll."

| # | On screen / action | Narration |
| --- | --- | --- |
| 1 | Run `/timecardreport` (group + week). | "For the weekly timecards, run **slash timecard-report**, choose the **group** and the **Saturday** that ends the week." |
| 2 | Show the posted workbook in the reports channel. | "It posts an Excel workbook here — one tab per employee, with unapproved punches flagged." |

### 1b. Payroll — `/payrollweekly` and `/payrollmonthly`
- **What it is:** payroll **distribution** for a pay period (weekly) or a month, splitting
  each person's gross across categories using the lunch + standard/OT rules, including
  paid leave and bonuses.
- **Draft script beat:** "Run it against a pay period from the calendar; you get the gross
  and how it distributes across Construction, Service, Office, Shop, and Bonus."

| # | On screen / action | Narration |
| --- | --- | --- |
| 1 | Run `/payrollweekly` (period). | "**slash payroll-weekly** builds the distribution for a pay period…" |
| 2 | Run `/payrollmonthly` (month). | "…and **slash payroll-monthly** rolls up every run in a month." |

### 1c. Hipp invoice — `/hippinvoice`
- **What it is:** the Hipp billing workbook (bills at the current pay rate, shows the base
  pay-rate column, per-employee catch-all).
- **Draft script beat:** "Pick the group/period; the bot produces the Hipp invoice
  workbook ready to send."

### 1d. Custom range — `/timecardrange`
- **What it is:** a timecard report over any custom date range (up to a year). Also
  available to employees for themselves; admins can run it for reporting.

---

## Part 2 — Placeholders (record when built)

> Leave these in the outline so the video has a home for them. **Do not script in detail
> until the report ships** — then replace the stub with a scene table like Part 1.

### 2a. Insurance audit — Summary  — ⬜ TODO (not built yet)
- **Intended:** the audit summary over a date range (split by year, with the OT
  %-allocation section) plus per-employee week-by-week detail sheets.
- **When built:** add purpose, the command + options, what each sheet shows, and where it
  posts. _(Weekly and Monthly audit pieces may already exist — confirm and fold in here.)_

### 2b. PTO accrual / balances  — ⬜ TODO (optional, not built yet)
- **Intended:** show accrued vs. used PTO (hourly auto-accrual 1h/40, salaried tracked),
  and balances.
- **When built:** document the command, how balances are read, and caveats.

### 2c. Anything else we add  — ⬜ TODO
- Reserve a slot for future reports (e.g., cost summaries). Add a short section each time
  one ships so this video stays complete.

---

## Written guide (pairs with the video) — living document

**Before you run anything:** groups populated, pay rates set, pay calendar generated,
leave/bonuses entered. Run reports only in the pay channels; they post to
**timecard-reports** (or DM where noted). Weeks end Saturday.

**Available now**
- **/timecardreport** — weekly, per-employee tabs, group-scoped; flags unapproved punches.
- **/payrollweekly** — payroll distribution for a pay period.
- **/payrollmonthly** — monthly roll-up of the period runs.
- **/hippinvoice** — Hipp billing workbook.
- **/timecardrange** — custom date range (up to a year).

**Coming (fill in as they ship)**
- ⬜ Insurance audit **Summary** (+ per-employee detail).
- ⬜ PTO accrual / balances.
- ⬜ Future reports.

> Maintenance: when a placeholder ships, (1) replace its stub above with a real scene
> table, (2) add it to this list, (3) re-record that section, and (4) update the wiki
> Commands Reference.
