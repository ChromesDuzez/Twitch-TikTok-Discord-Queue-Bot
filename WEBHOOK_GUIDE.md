# Webhook Integration Guide

## Overview

The bot exposes HTTP endpoints that let **Odoo notify the bot that a record
changed**. Odoo POSTs a tiny pointer (just the record `_id`); the bot then
**pulls that record from Odoo** over the authenticated API and reconciles it
into its authoritative SQLite store, refreshing any affected Discord message.

SQLite stays the source of truth. Outbound sync (bot → Odoo) is handled
separately by the sync worker and needs no webhook.

## Why we don't cryptographically sign the messages

A common way to secure a webhook is to have the sender attach a cryptographic
signature to each message (often called *HMAC signing*) that the receiver verifies.
We don't need that here, because the message Odoo sends never contains any data the
bot trusts. It's just a pointer — a record id — and the bot turns around and fetches
that record from Odoo itself over the authenticated connection. So:

- **The record type comes from the web address, not the message body.** There's a
  separate address per record type, and the only thing read from the message body
  is the id number. A faked or garbage type value in the body can't do anything
  because the bot never reads it.
- **A forged message can't slip in bad data** — the worst it can do is make the bot
  re-fetch a record from Odoo and confirm it already matches, which changes nothing.
  Re-sending the same message is harmless for the same reason.
- The only real risk left is someone flooding the address with requests to overload
  it (a denial-of-service attempt), and a shared secret **token** handles that.
  Signing would also mean maintaining extra code on the Odoo side — unnecessary here.

## Authentication

### Token (required)

The bot checks a shared secret token on every request. The comparison takes the
same amount of time whether the token is right or wrong, so an attacker can't guess
it character-by-character by timing how fast the bot replies. Provide it either way:

- URL query: `...?token=<WEBHOOK_TOKEN>`  ← works with Odoo's no-code webhook
- Header: `X-Webhook-Token: <WEBHOOK_TOKEN>`

`WEBHOOK_TOKEN` is **auto-generated on first startup** and written to `.env` if
blank — you don't create it by hand. Rotate it anytime with the
`/regenwebhooktoken` slash command (the new value is **not** shown in Discord;
read it from the server's `.env`, then update your Odoo URLs).

### IP allowlist (optional, off by default)

A rough extra filter that only accepts webhooks coming from known IP addresses.
When enabled (`/webhookallowlist enable`), the bot only accepts webhooks from the
addresses in `WEBHOOK_IP_ALLOWLIST`, which it refreshes with Odoo's own address
every time it calls Odoo. When the traffic comes through Cloudflare, the bot reads
the sender's true address from the `CF-Connecting-IP` header Cloudflare adds
(otherwise it would only see Cloudflare's own address).

> ⚠️ Caveat: with Cloudflare-fronted Odoo hosting, the address you *connect to*
> (the front door) can differ from the address Odoo *sends from* (its outbound
> address). If enabling the allowlist blocks legitimate webhooks, that mismatch is
> why — just turn it off (the token is the real protection) or add Odoo's outbound
> address to `WEBHOOK_IP_ALLOWLIST`.

## Endpoints (one per model)

```
# create / update
POST /webhook/odoo/res.partner?token=<TOKEN>
POST /webhook/odoo/hr.attendance?token=<TOKEN>
POST /webhook/odoo/account.analytic.line?token=<TOKEN>
POST /webhook/odoo/hr.employee?token=<TOKEN>          # archive/reactivate only (no delete route)
# deletion (separate route so the action is unambiguous)
POST /webhook/odoo/hr.attendance/delete?token=<TOKEN>
POST /webhook/odoo/account.analytic.line/delete?token=<TOKEN>
POST /webhook/odoo/res.partner/delete?token=<TOKEN>
```

Headers: `Content-Type: application/json`.
Body (Odoo's native webhook sends exactly this):

```json
{ "_id": 123 }
```

`_action` and `write_uid` are accepted if present but optional. The bot returns
`200` as soon as the pointer is queued; the pull + reconcile happen
asynchronously (usually within seconds).

## Exposing the endpoint (Cloudflare Zero Trust tunnel — recommended)

You do **not** need to port-forward. Run the bot with a **Cloudflare Zero Trust
tunnel** (`cloudflared`), which dials out to Cloudflare and publishes a public
`https://` hostname that forwards to the bot's local `WEBHOOK_PORT`:

- No inbound ports opened on your network/firewall.
- **The connection is encrypted automatically** — Cloudflare handles the HTTPS
  encryption for you, so the `?token=` in the address is protected in transit.
- Cloudflare passes along each request's true sending address in the
  `CF-Connecting-IP` header, which is the address the optional IP allowlist checks.

Quick setup: in the Cloudflare Zero Trust dashboard, create a **Tunnel**, add a
**Public Hostname** (e.g. `hooks.yourdomain.com`) with service
`http://localhost:8080` (your `WEBHOOK_PORT`), and run the provided `cloudflared`
connector on the bot's host. Your Odoo URLs then look like
`https://hooks.yourdomain.com/webhook/odoo/hr.attendance?token=<TOKEN>`.

> The webhook auth still applies end-to-end: the token is required regardless of
> the tunnel. Optionally, Cloudflare Access policies can add another layer in
> front of the endpoint.

## Setting it up in Odoo (Enterprise 17+, incl. 19.0+e — no code)

Odoo's native webhook automation posts the pointer for you; there is nothing to
write or maintain on the Odoo side.

Create one automation rule per model:

1. Enable developer mode: **Settings → Developer Tools → Activate the developer mode**.
2. **Settings → Technical → Automation Rules → New**.
3. Set **Model**, **Trigger**, and **Watched Fields** per the table below.
4. **Actions To Do** → add **“Send Webhook Notification”**.
5. **URL**: the matching endpoint above, including `?token=<your WEBHOOK_TOKEN>`.
6. Save.

Odoo sends `{"_id": <record id>}` (with `_model`) to that URL on the trigger;
the bot authenticates the token, pulls the record, and reconciles.

### Which models, triggers, and fields

The bot only reads a few fields per model (it re-fetches the record, so the
payload itself is ignored). Scope each automation's **trigger to just those
fields** so unrelated edits — especially `res.partner`'s many custom fields —
don't fire needless webhooks.

| Model | Endpoint (`…/webhook/odoo/…`) | Watched fields (all the bot reads) | Used for |
| --- | --- | --- | --- |
| `hr.attendance` | `hr.attendance` | `check_in`, `check_out`, **`employee_id`** | Punch times → `punch_clock`; a create mirrors an Odoo-built shift into a local punch, and `employee_id` re-assigns it |
| `account.analytic.line` | `account.analytic.line` | `unit_amount`, **`project_id`**, **`task_id`**, **`partner_id`**, **`x_studio_shift`** | Worktime → `work_time`. Each round-trips: hours, category (from project), task, customer, and the shift/punch it belongs to. **Not `date`** — the worktime's timestamp is derived from its shift, so it's never watched or pulled back |
| `res.partner` | `res.partner` | `name` (the base **Name** field, *not* the computed "Complete Name" — it drives `display_name`), **`active`**, and `customer_rank` (catches a contact promoted to a customer) | Customer name → `customer`; `active` toggles local archive |
| `hr.employee` | `hr.employee` | **Watch** `active`, `name`, `private_phone`. The address (`private_street`, `private_street2`, `private_city`, `private_state_id`, `private_zip`) is **read on the pull** but usually **can't be watched** — it's a non-stored related field, so it syncs whenever a watched field changes | `active` archives/reactivates the linked employee (removes/keeps their Discord clock, history kept); `name`/phone/address are pulled **one-way Odoo → bot** to keep the weekly report current. No Domain needed — only employees linked via `odooId` are affected |

> **These watched-field lists exist because the bot now mirrors admin edits made
> *in Odoo* back to Discord** (see [Inbound reconcile](#what-round-trips-from-odoo)
> below). If a field the bot mirrors isn't in the rule's Watched Fields, an admin
> editing it in Odoo won't fire a webhook and the two systems drift — which is the
> whole failure mode these lists prevent. Add a field here only alongside code that
> actually reads it; a watched field with no matching reconcile just wastes fetches.

**Recommended trigger: “On Create and edit”, with Watched Fields set to the
columns above** — fires on creation and when a watched field is edited, nothing
else. Avoid *after last update*: it fires on **any** field change (a lot of
needless webhooks for `res.partner`). Since re-fetching a record that didn't
really change just makes the bot do nothing, limiting the watched fields is purely
an efficiency win — nothing breaks if a rule fires more than needed, and no delay
is needed.

### Record filters (the automation **Domain**)

Watched fields decide *when* a rule fires; the **Domain** decides *which records*
it fires for. Two of these models are shared across large parts of Odoo, so a
Domain isn't optional polish — it's what keeps the bot from being flooded with
pointers to records it will only fetch and discard. Set the Domain on the
Automation Rule (the **“Before Update Domain” / record filter** on the rule
form), and apply the **same Domain to that model's create/edit rule and its
`/delete` rule** so both stay scoped identically.

| Model | Domain (what to match) | Domain expression | Why it matters |
| --- | --- | --- | --- |
| `account.analytic.line` | Project **is set**, Journal Item **not set**, Financial Account **not set** | `[("project_id", "!=", False), ("move_line_id", "=", False), ("general_account_id", "=", False)]` | `account.analytic.line` backs timesheets **and** accounting cost/revenue postings, expenses, and other analytic entries. Only a genuine project **timesheet** has a project with no journal-item / financial-account linkage. Without this filter, every accounting posting fires the webhook; the bot pulls it, finds no `x_studio_shift` link, and skips it — many pointless round-trips. |
| `res.partner` | Is a **customer** — put this on **Apply on** (not Before Update Domain) and tick **Include archived** | `[("customer_rank", ">", 0)]` | `res.partner` holds contacts, vendors, companies, and portal users — the vast majority of which are not customers you clock work against. `customer_rank > 0` is Odoo's standard "this partner is a customer" flag and cuts the firing set down to just the records that map to a local `customer` row. It must sit on **Apply on** so it also filters *creates*; **Include archived** lets an archive (`active` → False) still match and fire the webhook. |
| `hr.attendance` | *(none required)* | `[]` | `hr.attendance` is a dedicated model — every record is a punch the bot may care about, so no Domain is needed. In a multi-company / multi-department setup you *may* narrow it, e.g. `[("employee_id.department_id", "=", <dept id>)]`. |

**About the analytic-line field names.** In the Domain editor the three leaves
read as **Project** (`project_id`), **Journal Item** (`move_line_id`), and
**Financial Account** (`general_account_id`). The exact technical names can vary
slightly by Odoo build; pick the fields whose labels are *Project*, *Journal
Item*, and *Financial Account* and the "is set / is not set" operators — that's
the combination that isolates timesheets. This mirrors the pre-existing rule that
was already working in the test database.

**Why the same Domain on the `/delete` rule.** If the delete automation is left
unfiltered, deleting *any* accounting analytic line (or *any* partner) POSTs to
the `/delete` endpoint. For a record the bot never tracked that just does nothing
(it's "already gone locally") — but for `hr.attendance` /
`account.analytic.line` it still posts an **admin approval prompt** in the
timecard channel before the bot works out it doesn't apply. Matching the Domain
keeps delete approvals scoped to real timesheets/attendances and avoids that
noise.

### What round-trips from Odoo

The bot doesn't just import new records — it mirrors **edits an admin makes in
Odoo** back into SQLite and re-renders the affected Discord clock. This matters
when more than one person touches Odoo: a well-meaning "let me just fix it here"
edit stays in sync instead of silently diverging. What's reconciled today:

| Odoo change | Effect in Discord |
| --- | --- |
| Edit `hr.attendance` check-in/check-out | Punch times updated, clock re-rendered |
| Re-assign `hr.attendance` to another employee | Punch moves to that employee (old + new clocks re-render). If the new employee isn't linked locally, the punch is left as-is and a warning is logged |
| **Create** an `hr.attendance` from scratch in Odoo | Mirrored into a new local punch (employee resolved via its `odooId`); an in-flight punch the bot just created is *adopted*, not duplicated |
| Edit an `account.analytic.line` (hours, project, task, customer) | The `work_time` row is re-synced field-for-field; category (`punchType`) is re-derived from the project. The line's `date` is **not** pulled back — `timeStarted` stays shift-derived |
| Move an `account.analytic.line` to a different `x_studio_shift` | The `work_time` is re-attached to that punch |
| **Clear** the `x_studio_shift` on a worktime | The `work_time` is **soft-detached** — kept but hidden from reports/clock and not attributed, until a shift is re-assigned (re-attaches the same row); only a real Odoo delete removes it |
| **Create** an `account.analytic.line` with a shift link | Mirrored into a new `work_time` on the linked punch |
| Rename / archive a `res.partner` | `customer` name / `archived` flag updated |
| Archive / unarchive an `hr.employee` (e.g. a temp worker who didn't work out) | Linked employee is archived (clock removed → can't punch in, history kept) or reactivated |
| Edit an `hr.employee`'s name / phone / address | Pulled one-way into the local employee row so the weekly report stays current (empty Odoo values never blank populated local data) |

Attribution is still **Discord-first** — the normal flow is that work is assigned
on the clock and pushed *out* to Odoo. This inbound mirroring is the safety net
for the cases where a change originates in Odoo instead. Every time a webhook
fires, the bot re-fetches the current record and applies it; if nothing actually
changed it simply does nothing, so a duplicate or unnecessary webhook does no harm.
(Employee **name/phone/address** are the exception to
Discord-first: HR owns those, so they're pulled **one-way from Odoo** — the bot
never pushes them back.)

> **Employee field-name overrides (optional).** The `hr.employee` demographic pull
> defaults to the standard Odoo 19 fields — `private_phone`, `private_street`,
> `private_street2`, `private_city`, `private_state_id`, `private_zip` (`name` is
> always `name`). If your Odoo uses different field names, override them without a
> code change via env vars: `ODOO_EMPLOYEE_PHONE_FIELD`,
> `ODOO_EMPLOYEE_STREET_FIELD`, `ODOO_EMPLOYEE_STREET2_FIELD`,
> `ODOO_EMPLOYEE_CITY_FIELD`, `ODOO_EMPLOYEE_STATE_FIELD`, `ODOO_EMPLOYEE_ZIP_FIELD`
> — and set the automation's Watched Fields to match whatever you point them at.

### Deletions & the shift field (requires Odoo Studio)

Deletion support and attributing Odoo-created timesheets rely on a **Studio
Many2one field `x_studio_shift`** on `account.analytic.line` → `hr.attendance`
(configurable via `ODOO_SHIFT_FIELD`). The bot sets it on lines it creates, and
reads it to map an Odoo line to the right punch. **If that field doesn't exist
(or Odoo Studio isn't enabled), the `/delete` routes return `503` and deletion
support stays off** until it does (the bot re-checks periodically).

To mirror deletions, add **“on deletion”** automations pointing at the `/delete`
endpoints for `hr.attendance`, `account.analytic.line`, and `res.partner`. And
for **archiving** a customer, add a `res.partner` update automation watching
`active` (Odoo archive sets `active = False`).

Because Discord is the source of truth, an Odoo-side deletion of an attendance or
timesheet posts an **admin approval** in the timecard channel: approve to delete
it locally, or reject and the bot **re-creates it in Odoo**. Customer deletions
don't need approval — a referenced customer is archived locally (hidden from
search, kept for reports); an unreferenced one is removed.

Odoo 19 groups triggers as: **Timing Conditions** (based on date field / after

Odoo 19 groups triggers as: **Timing Conditions** (based on date field / after
creation / after last update), **Values Updated**, **Custom** (On Create / On
Create and edit / on deletion / on UI change), **External** (on Webhook — not
used here).

## Response codes

| Code | Meaning |
| --- | --- |
| `200` | Pointer accepted and queued |
| `400` | Missing/invalid integer `_id`, or wrong Content-Type |
| `401` | Missing or wrong token |
| `403` | IP allowlist enabled and the source IP isn't allowed |
| `503` | Token not configured, TimeTracking cog not loaded, or (for `/delete`) deletion support disabled because the `x_studio_shift` field is missing |

## Testing (curl)

```bash
curl -X POST "https://your-domain.com/webhook/odoo/hr.attendance?token=YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"_id": 123}'
```

## Logging

Console lines are prefixed `[Webhook]`. Rejections log the source IP and reason
(bad token, IP not allowed). Successful timecard reconciliation is logged to the
timecard log channel; webhook/transport issues go to the general log channel.
