# Odoo Field Reference

The single source of truth for **every Odoo model and field the bot touches** —
what it reads, filters on (`domain`), sorts by (`order`), or writes. Keep this in
sync with `cogs/timetracking/odoo/client.py` and `.../inbox.py` whenever a call
changes. (A copy lives on the wiki as **Odoo-Field-Reference**; update both.)

## The rule that prevents the recurring 500 error

The error `Cannot convert <model>.<field> to SQL because it is not stored` means
a **computed / non-stored** field was used where Odoo needs real SQL. To avoid it:

| Where the field is used | Computed (non-stored) field allowed? |
| --- | --- |
| `fields` (reading, for labels/values) | ✅ Always fine |
| `domain` (filtering) | ⚠️ Only if the field has a search method — **prefer a stored field** |
| `order` (sorting) | ❌ **Never** — this becomes SQL `ORDER BY` and always fails |

Practical habit: **filter and sort on the stored `name`; read `display_name`
only for display.** `display_name` is computed on `res.partner` (and others) in
Odoo 19.

### How to check whether a field is stored
In Odoo: **Settings → Technical → Database Structure → Fields**, find the model +
field, and look at **Stored** (checkbox) — if unchecked, treat it as read-only
for our purposes. In code, `client.field_exists(model, field)` confirms a field
merely *exists* (used for the Studio shift field), not whether it's stored.

---

## Fields by model

Legend — **Use:** R = read (`fields`), F = filter (`domain`), O = order, W = write.

### `res.partner` — customers
| Field | Use | Stored | Notes |
| --- | --- | --- | --- |
| `id` | R/F | ✅ | |
| `name` | F/O/W | ✅ | **Filter/sort on this.** `name_create` writes it. |
| `display_name` | R | ❌ computed | Labels only — never `order`/`domain`. |
| `company_type` | R | ✅ | company vs person (dedup label). |
| `customer_rank` | F | ✅ | `> 0` selects customers. |
| `active` | F | ✅ | archived-partner handling. |

### `hr.employee`
| Field | Use | Stored | Notes |
| --- | --- | --- | --- |
| `id` | R/F | ✅ | |
| `name` | R | ✅ | shown on the clock. |
| `active` | R/F | ✅ | drives local archive. |
| `display_name` | R | ❌ computed | autocomplete label only. |
| `private_phone` | R/W | ✅ | env `ODOO_EMPLOYEE_PHONE_FIELD`. |
| `private_street` | R/W | ✅ | env `ODOO_EMPLOYEE_STREET_FIELD`. |
| `private_street2` | R/W | ✅ | env `ODOO_EMPLOYEE_STREET2_FIELD`. |
| `private_city` | R/W | ✅ | env `ODOO_EMPLOYEE_CITY_FIELD`. |
| `private_state_id` | R/W | ✅ | Many2one → `res.country.state`; env `ODOO_EMPLOYEE_STATE_FIELD`. |
| `private_zip` | R/W | ✅ | env `ODOO_EMPLOYEE_ZIP_FIELD`. |

The address/phone field names differ across Odoo configs, so they're
**env-overridable** (`_employee_field_map()` in `inbox.py`); defaults are the
standard Odoo 19 private-address fields.

### `hr.attendance` — a clock-in/out (a "shift" / punch)
| Field | Use | Stored | Notes |
| --- | --- | --- | --- |
| `id` | R | ✅ | stored locally as `punch_clock.odooId`. |
| `employee_id` | R/W | ✅ | Many2one → `hr.employee`. |
| `check_in` | R/W | ✅ | UTC datetime string. |
| `check_out` | R/W | ✅ | UTC datetime string. |

### `project.task`
| Field | Use | Stored | Notes |
| --- | --- | --- | --- |
| `id` | R/F | ✅ | |
| `name` | F | ✅ | **Filter task title on this.** |
| `display_name` | R | ❌ computed | picker label only. |
| `project_id` | R/F | ✅ | Many2one → `project.project`. |
| `partner_id` | R/F | ✅ | Many2one → `res.partner`. Filter via `partner_id.name`. |
| `planned_date_begin` | R/O | ✅ * | *Exists only with **project planning** enabled. Used to rank tasks by planned-start nearness; `search_tasks_for_partner` **falls back to unsorted** if absent. |
| `date_deadline` | R/F/O | ✅ | Field-Service task search window. |
| `is_closed` | F | ✅ | excludes done/closed tasks. |
| `company_id` | R | ✅ | copied onto the timesheet line. |

### `project.project`
| Field | Use | Stored | Notes |
| --- | --- | --- | --- |
| `id` | R/F | ✅ | |
| `active` | F | ✅ | |
| `partner_id` | R/F | ✅ | filter via `partner_id.name`. |
| `display_name` | R | ❌ computed | label only. |
| `company_id` | R | ✅ | |

### `account.analytic.line` — worktime / timesheet line
| Field | Use | Stored | Notes |
| --- | --- | --- | --- |
| `id` | R | ✅ | stored as `work_time.odooId`. |
| `name` | W | ✅ | description text. |
| `date` | R/W | ✅ | work date (`YYYY-MM-DD`). |
| `unit_amount` | R/W | ✅ | hours. |
| `product_uom_id` | W | ✅ | `4` = Hours. |
| `employee_id` | W | ✅ | Many2one → `hr.employee`. |
| `company_id` | W | ✅ | from the task/project. |
| `validated_status` | W | ✅ | set to `draft`. |
| `project_id` | R/W | ✅ | Many2one → `project.project`. |
| `task_id` | R/W | ✅ | Many2one → `project.task`. |
| `partner_id` | R | ✅ | read during inbound reconcile. |
| **shift field** (`x_studio_shift`) | R/W | ✅ custom | See below. |

### `mail.tracking.value` + `mail.message` — chatter (rename history)
Used by `get_partner_name_history()` so `/synccustomers` can match a local
customer against a partner's **former** name (renamed since). All stored.
| Model.Field | Use | Stored | Notes |
| --- | --- | --- | --- |
| `mail.tracking.value.field_id` | F | ✅ | m2o → `ir.model.fields`; filter `field_id.name = 'name'`. |
| `mail.tracking.value.old_value_char` | R | ✅ | the previous name text. |
| `mail.tracking.value.mail_message_id` | R/F | ✅ | m2o → `mail.message`; filter `mail_message_id.model = 'res.partner'`. |
| `mail.message.model` | F | ✅ | restrict to `res.partner`. |
| `mail.message.res_id` | R | ✅ | the partner id the change belongs to. |

### `ir.model.fields` — introspection only
| Field | Use | Stored | Notes |
| --- | --- | --- | --- |
| `model` | F | ✅ | `field_exists()` probe. |
| `name` | F | ✅ | the field to check for. |
| `id` | R | ✅ | |

---

## Custom / Studio fields

| Field | Model | Env override | Notes |
| --- | --- | --- | --- |
| `x_studio_shift` | `account.analytic.line` | `ODOO_SHIFT_FIELD` | **Studio** Many2one → `hr.attendance` linking a timesheet line to its shift. Requires Odoo Studio. Presence is probed at startup (`check_shift_field`); **deletion/reassignment support is gated on it**. Written only when present. |

## Config-driven ids (not fields, but referenced in domains/logic)

| Env var | Meaning |
| --- | --- |
| `ODOO_OFFICE_PROJECT_ID` | `project.project` id for Office work → punchType "Office". |
| `ODOO_FIELD_SERVICE_PROJECT_ID` | `project.project` id for Field Service → "Service"; anything else → "Construction". |
| `ODOO_BOT_UID` | the bot's Odoo user id; inbound webhooks whose `write_uid` matches are treated as our own echo and skipped. |

---

## Where each call lives

- **`cogs/timetracking/odoo/client.py`** — all outbound reads/writes (the tables
  above come from here). Top-of-file comment restates the stored-vs-computed rule.
- **`cogs/timetracking/odoo/inbox.py`** — inbound reconcile reads
  (`read_record` field lists for `res.partner`, `hr.employee`, `hr.attendance`,
  `account.analytic.line`) and the employee field map.
- **`cogs/timetracking/odoo/sync.py`** — orchestration only; no field literals of
  its own (it calls the client).

When you add or change any of these, **update this file and the wiki copy**, and
double-check the stored/computed column for anything new in `order` or `domain`.
