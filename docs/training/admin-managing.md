# Admin Video — Managing Timecards

> Reporting has its **own** video — see [admin-reporting.md](admin-reporting.md). This
> one is about running the system day to day: setup, approvals, corrections, customers,
> groups, and keeping Odoo in sync.

## At a glance
- **Audience:** timecard admins / managers (holds the **timecard-admin** role, or a server
  Administrator).
- **Length target:** 10–14 minutes (or split into "Setup" and "Daily" if you prefer).
- **Have on screen before recording:** a **test/demo server** with a sanitized database —
  never real pay or customer data. Have the managed channels present (log, timecard-log,
  timecard-admin, timecard-reports) and at least one demo employee with some punches.
- **Goal:** by the end, an admin can onboard an employee, approve and correct time, manage
  customers and groups, and fix sync issues — and knows the guardrails around pay.

## Outline
1. Who this is for + the channels (where results show, where pay is allowed).
2. One-time setup: roles, categories, Odoo projects.
3. Onboarding an employee: add → link to Odoo → create their clock.
4. Customers: sync from Odoo / add / link.
5. Daily: approving non-standard punches.
6. Daily: viewing a timecard (ids, durations, totals, Odoo links).
7. Corrections: punches and worktime.
8. Abandoned worktimes (0h) and how to resolve them.
9. Keeping Odoo in sync: resync + reconcile.
10. Groups (needed for reports).
11. Pay & leave — and the pay guardrail.
12. Where to practice + where to find every command.

## Script

| # | On screen / action | Narration |
| --- | --- | --- |
| 1 | Admin looking at the four managed channels. | "This video is for managers running the timecard system. First, the channels. Admin command results show up **publicly** in the **timecard-admin** and **timecard-log** channels, and **privately to you** anywhere else, just to keep other channels clean. Anything involving **pay** is locked to the admin-facing channels — more on that at the end." |
| 2 | Run `/configureroles`. | "Setup is a one-time thing. **slash configure-roles** sets two roles: the **timecard-admin** role — who can manage everything — and the shop **timeclock** role, whose members' punches are trusted and auto-approved." |
| 3 | Run `/configurecategories`, then `/configureprojects`. | "**slash configure-categories** manages the clock channels and the disabled-clocks category. **slash configure-projects** tells the bot which Odoo projects are **Field Service** and **Office**, so logged work lands in the right place." |
| 4 | Run `/addemployee` with the options. | "To onboard someone: **slash add-employee** — name, contact, address, pay rate, and pay type. You can link them to their Odoo employee right here, or do it after." |
| 5 | Run `/linkemployee @user`. | "If you didn't link at creation, **slash link-employee** ties a Discord user to their Odoo employee. Until they're linked, their time just queues up safely and syncs once linked." |
| 6 | Run `/createclock @user`. | "Then **slash create-clock** posts that person's time-clock message in a channel. That's the clock your employees use in the first two videos. That's onboarding." |
| 7 | Run `/synccustomers`; mention `/addcustomer`. | "Customers power the job picker. **slash sync-customers** pulls and links customers from Odoo; **slash add-customer** adds one manually. If a local customer isn't tied to Odoo yet, **slash link-customer** connects them." |
| 8 | Switch to the **timecard-admin** channel showing a pending approval prompt. | "Now the daily stuff. When someone whose account isn't a trusted clocker punches, the bot posts an **approval** here with **Approve** and **Edit** buttons for the clock-in and the clock-out." |
| 9 | Click **Approve clock-in**. | "**Approve** confirms the punch. If the time's wrong, **Edit** opens a box with the current time and lets you set the correct one — it fixes the database and, if it's linked, pushes the correction to Odoo." |
| 10 | Run `/viewtimecard employee:@demo`. | "To see anyone's week, **slash view-timecard**. You get weekly totals, each punch with its **length**, and the jobs underneath. When Odoo's connected, the job and customer are **clickable** straight into Odoo." |
| 11 | Point to the `#id` values in the view. | "See these **#id** numbers? That's how you target a specific punch or work entry in the edit commands. Grab the id from here, then edit it." |
| 12 | Run `/editpunch` (show options); mention `/addpunch`, `/deletepunch`. | "Corrections. For punches: **slash edit-punch** fixes a clock-in or clock-out time, **slash add-punch** creates a missing one, and **slash delete-punch** removes a bad one — it shows you the linked work first." |
| 13 | Run `/editworktime` (show options); mention add/delete/reassign. | "For work entries: **slash edit-worktime** changes the type, hours, customer, or the Odoo **task** (Service) or **project** (Construction). You can also **move** a work entry to a different shift. There's **add-worktime**, **delete-worktime**, and **reassign-worktime** too. Note: when Odoo's connected, the customer follows the task/project — you pick the work item, not the customer." |
| 14 | Run `/abandonedworktime`. | "Sometimes a job gets left at zero hours — usually an old one where someone clocked out before ending their work. **slash abandoned-worktime** lists those. By default it shows only the ones **not yet in Odoo**, since those are the ones that need you." |
| 15 | Show the two fixes. | "Two ways to clear one: **edit-worktime** with the real hours to log it properly, or **abandoned-worktime push** with its id to send it to Odoo as a flagged zero-hour placeholder — handy for old ones you won't reconstruct but don't want to delete." |
| 16 | Run `/resync`; mention targeting. | "If a change didn't make it to Odoo, **slash resync** force-pushes everything pending, retries failures, and sweeps anything that never got sent. You can target one punch or work entry by id if you only want that." |
| 17 | Run `/reconcileattendance`. | "**slash reconcile-attendance** finds and cleans up duplicate or orphaned attendances in Odoo that failed syncs can leave behind. Run it if attendance looks doubled up." |
| 18 | Run `/creategroup` then `/addtogroup`. | "Groups are how reports get scoped. **slash create-group** makes a group, **slash add-to-group** adds employees. You'll set these up before running the group reports in the reporting video." |
| 19 | Move to a **pay** channel; run `/setpay`; mention pay commands. | "Pay and leave. **slash set-pay** records a pay rate with an effective date, **slash pay-history** shows the history, and there are banked-hours and leave and bonus commands. These build the payroll numbers." |
| 20 | Emphasize the guardrail. | "The guardrail: **pay commands only work in the pay channels** — log, timecard-log, timecard-admin, and timecard-reports — and the employee picker never shows pay. That's deliberate: compensation data must not leak into a general channel. Keep pay work in those channels." |
| 21 | Talking-head / calm screen. | "Two closing tips. **Practice on a test server** with sanitized data, not production. And the wiki's **Commands Reference** lists every command and option — these videos cover the common paths, not all of them. That's managing the system. The next video is reporting." |

## Written guide (pairs with the video)

**Access & channels**
- You need the **timecard-admin** role (or be a server Administrator).
- Command results post **publicly** in **timecard-admin** / **timecard-log**, and
  **privately** to you elsewhere.
- **Pay** commands only run in the pay channels (log, timecard-log, timecard-admin,
  timecard-reports) and never reveal pay in autocomplete.

**One-time setup**
1. **/configureroles** — set the timecard-admin role and the shop timeclock role (trusted
   clockers, auto-approved).
2. **/configurecategories** — manage clock channels / the disabled-clocks category.
3. **/configureprojects** — set the Odoo Field Service and Office project ids.

**Onboard an employee**
1. **/addemployee** — name, contact, address, pay rate, pay type (optionally link Odoo here).
2. **/linkemployee @user** — link to their Odoo employee if not done above (time safely
   queues until linked).
3. **/createclock @user** — post their time-clock message.

**Customers**
- **/synccustomers** — pull & link customers from Odoo.
- **/addcustomer** — add one manually. **/linkcustomer** — tie a local customer to Odoo;
  **/unlinkedcustomers** lists any not yet linked.

**Approvals (daily)**
- Non-standard punches post an **Approve** / **Edit** prompt in the admin channel.
- **Approve** confirms; **Edit** corrects the time (updates the DB and pushes to Odoo if
  linked).

**View a timecard**
- **/viewtimecard employee:@name** — weekly totals, per-punch durations, nested jobs, and
  Odoo links. The **#id** on each row is what the edit commands target.

**Corrections**
- Punches: **/addpunch**, **/editpunch**, **/deletepunch**.
- Work entries: **/addworktime**, **/editworktime** (type / hours / customer / task /
  project / move), **/deleteworktime**, **/reassignworktime**.
- With Odoo connected, the customer **follows** the chosen task/project.

**Abandoned (0-hour) worktimes**
- **/abandonedworktime** — lists 0h entries on finished shifts; defaults to the ones **not
  yet in Odoo** (`show:synced` / `show:all` to widen).
- Resolve each by **/editworktime <id> hours:<n>** (log real hours) or
  **/abandonedworktime push:<id>** (send as a flagged 0h placeholder).

**Keep Odoo in sync**
- **/resync** — force-push pending, retry failures, sweep never-sent items (optionally
  target one `worktime:`/`punch:`).
- **/reconcileattendance** — find/clean duplicate or orphaned Odoo attendances.

**Groups (for reports)**
- **/creategroup**, then **/addtogroup** — build the employee groups the reports are
  scoped to.

**Pay & leave** *(pay channels only)*
- **/setpay**, **/payhistory**, banked-hours (**/bankadjust**, **/bankbalance**), leave
  (**/addleave**), bonuses (**/addbonus**), and the pay calendar commands.

**Practice & reference**
- Rehearse on a **test server** with sanitized data.
- The wiki **Commands Reference** has every command and option.
