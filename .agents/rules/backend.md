---
paths:
  - "*/models.py"
  - "*/views*.py"
  - "*/*_views.py"
  - "*/services*.py"
  - "*/forms*.py"
  - "*/*_forms.py"
  - "*/queries.py"
  - "*/lookups.py"
  - "*/migrations/**"
  - "accounts/**"
  - "core/**"
  - "config/**"
---

# Backend rules

The principles are in `AGENTS.md` (rules 2–4, 7, 9): exact records and provenance, authorization
on every read and write, attributed audits, no homemade layers. This file names the code that
implements them. Reuse it; don't write a second version.

- **Who may act.** Use `accounts/roles.py`: `require_role(user, *roles)` / `has_role` for the
  roles (admin, manager, technician, viewer; admin implies the others), `require_staff`,
  `staff_role_required` for views, `require_disposal_authority` (the explicit
  `accounts.approve_disposal` permission) and `require_request_authority` (the explicit
  `accounts.approve_distribution` permission plus an operating role). Superuser status alone
  grants neither. Check inside the service, not only in the view. A hidden button, model save
  hook or admin access is never the gate.
- **No self-approval.** Nobody approves their own disposal request or decides a specimen
  request they created or submitted. The same independence applies to excursion and deviation
  review and to reinstating a withdrawal. Reuse the checks in `specimens/services.py`,
  `distribution/services.py`, `equipment/services.py` and `quality/` and test the refusal.
- **Transition and audit commit together.** Decorate the service with `@transaction.atomic`,
  lock with `select_for_update()` (storage mutations lock the `StorageGate` row first through
  `storage.services.lock_gate`), recheck the state, change it, then call
  `core.audit.record_event(actor=, action=, obj=, before=, after=, reason=)`. Use
  `core.audit.snapshot(obj)` for before/after, never on account or credential models.
- **Append-only events.** Custody, move, check-out/in, thaw, disposal, equipment, consent and
  shipment events and the amount ledger are never updated or deleted: corrections add a record
  with a reason. Protect a new event table with `core.immutability.protect_history("<table>")`
  in its migration. Specimens are never deleted; status changes carry a reason. Specimen IDs
  stay opaque (no location, participant, study or date).
- **Position occupancy.** One item per position is the unique `storage.Occupancy.position`
  constraint, rechecked under the storage lock. Never decide "free" in one transaction and
  claim in another.
- **Concurrent edits.** Configuration edit forms use `core/concurrency.py`:
  `protect_form(form, instance)` adds the original-state token, and
  `check_original_state(instance, expected)` rejects a stale save while keeping the input.
  Records with a counter use `expected_revision`. Recheck after locking and before writing.
- **Exact quantities.** Parse amounts with `specimens.services.parse_quantity(value, unit, …)`
  and store `DecimalField` with an explicit unit, keeping the entered string where the model
  has one. Temperatures and levels are `Decimal` with their unit. Never `float()` or `round()`
  a measured value.
- **Remote select lookups.** Register in `core/lookups.py`:
  `register(Lookup("<name>", staff(lambda request: …), …))`. The form's `ModelChoiceField`
  queryset still validates. `tests/test_lookups.py` covers every lookup's authorization.
- **Per-user presentation** is `core/preferences.py` (`grid:<grid>:v<n>`, page size and sort).
  It is presentation only and never widens a queryset.
- **Dates and clocks.** `core.due.due_state` decides overdue and soon for dated obligations.
  Time out of storage and thaw counts are derived from custody and thaw records in
  `specimens/services.py`; don't recompute them in a view or template.
- **Migrations.** Additive and backward compatible while old and new code overlap. Add nullable
  or defaulted fields and backfill in a separate step. Never edit a merged migration, and keep
  `makemigrations --check` clean.
- **Structure.** Domain logic lives in each app's `services.py`, reads in `queries.py`, list
  filtering in `lists.py` / `*_lists.py` over `core/grids.py`. No generic service or repository
  base classes, and no workflow DSL.
- **Tests** go in `<app>/tests/` (cross-cutting ones in `tests/`). Cover the allowed and the
  refused role, a role-less or inactive user, the audit event and the stale-edit path. The gate
  suites are `*/tests/test_permissions.py`, `test_concurrency.py`, `test_history_guards.py`,
  `specimens/tests/test_custody.py`, `test_disposal.py`, `test_quantity.py` and
  `distribution/tests/test_gate.py`. Never loosen one to make a change pass. Synthetic data only:
  coded participants and `example.invalid` addresses.
