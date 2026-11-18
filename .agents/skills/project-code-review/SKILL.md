---
name: project-code-review
description: The pre-PR quality gate for OpenBGA Biorepository. It reviews the current diff against AGENTS.md (exact Decimal quantities with units, server-side authorization on every read, mutation and lookup, transitions committed with their audit events, no self-approval of disposal or self-decision of specimen requests, append-only custody, move and disposal events, position occupancy by database constraint rechecked under lock), the frontend rules and ui-standards, migrations, tests and page help, fixes what it finds in place, runs the CI checks plus the affected pytest and browser journeys, captures before/after screenshots for UI changes, and ends with a summary table and notes for reviewers. Use it whenever you finish a change and before you commit, push or open a pull request, and whenever someone asks to review, check or verify the work on a branch.
---

# Pre-PR review and gate

This is a gate, not a report: fix what you find, rerun what you changed, and only report what
can't be fixed safely. Keep the review to the diff. Untouched legacy code is out of scope
unless the change makes it wrong.

## 0. Scope the diff

```sh
git fetch origin develop -q
git diff --name-only origin/develop...HEAD   # committed on this branch
git status --short                            # plus uncommitted and untracked work
```

Sort the files into groups. Each check below says which group it applies to:

| Group | Paths |
| --- | --- |
| Python domain and views | `*/models.py`, `*/services.py`, `*/views*.py`, `*/*_views.py`, `*/forms.py`, `*/*_forms.py`, `*/queries.py`, `core/*.py`, `accounts/*.py`, other app `*.py` |
| Migrations | `*/migrations/*.py` |
| Templates and front end | `templates/**`, `*/templates/**`, `static/css/**`, `static/js/**` |
| Printed output | `specimens/labels.py`, `specimens/label.css`, `specimens/templates/specimens/label_preview.html`, `distribution/documents.py`, `distribution/templates/distribution/pdf/**` |
| Tests | `tests/**`, `*/tests/**`, `e2e/**` |
| Docs and help | `docs/**`, `docs/pages/**`, `changelog.d/**` |

If the diff is empty, say "Nothing to review" and stop. Ignore `.local/`. Don't edit
`static/vendor/**`, and don't run Git commands that write unless you own the branch.

## 1. Domain and authorization (Python changes)

Read each changed function together with its callers, against `.agents/rules/backend.md`, which
names the helpers to use (`accounts/roles.py`, `core.audit.record_event`, `core/concurrency.py`,
`specimens.services.parse_quantity`, `core/lookups.py`). Code that reimplements one of them is
a finding. Fix violations in place:

| # | Check | What to look for | Fix |
| --- | --- | --- | --- |
| 1 | Exact quantities (rule 3) | `float(`, `round(` or arithmetic on amounts, volumes, concentrations, temperatures or levels; a `FloatField`; formatting that drops the entered digits; a quantity without its unit | `Decimal` / `DecimalField` with an explicit unit; keep the entered string |
| 2 | Every read authorized (rule 4) | A view, export, download, label, PDF, HTMX fragment or `core/lookups.py` queryset that doesn't check the role, or that exposes records a role may not see | Check the role on the server; scope the queryset; add a test for a refused role and a role-less or inactive user |
| 3 | Every mutation authorized | A POST handler or service without a role or permission check, relying on a hidden button, `admin`, superuser status or a model `save()` hook | Check in the service; test the forbidden role |
| 4 | Transition and audit committed together | A state change and its audit event or history in separate transactions; a check done before `select_for_update` and not rechecked | One `transaction.atomic()`: lock, recheck, change, record the event |
| 5 | No self-approval | Approving a disposal the actor requested; deciding a specimen request the actor created or submitted; closing an excursion or deviation the actor submitted; reinstating a withdrawal the actor recorded | Reuse `require_disposal_authority` / `require_request_authority` and the services' independence checks; test the refusal |
| 6 | Append-only events | Editing or deleting a custody, move, check-out/in, thaw, disposal, equipment, consent or shipment event, an amount-ledger entry or a dispatched shipment's items; deleting a specimen | Add a correction record with a reason; status changes carry a reason |
| 7 | Position occupancy | Taking a position without the database constraint or without rechecking under lock; reading "free" and writing later without a lock | The storage services' occupancy path; test two claims on one position |
| 8 | Distribution and disposal gates | Allocation, pick, pack or dispatch that skips consent, holds, study status or agreement validity; disposal that skips the approval or a disposal or all-use hold | Call the existing gate; test the refused path |
| 9 | Concurrency | Read-modify-write without a revision check or lock | `select_for_update` or the existing `expected_revision` / `check_original_state` pattern; a clear stale-edit message |
| 10 | No invented policy | New thresholds, retention rules, acceptance limits, regulatory or compliance wording | Make it configuration or remove it; flag it for the owner |
| 11 | Synthetic data and secrets | Real donor, patient or participant data (names, record numbers, birth dates, contact details), keys, dumps or runtime output in code, fixtures, tests or logs | Coded participants and `example.invalid` only |

Specimen identifiers stay opaque: no location, participant, study or date encoded in them.

## 2. Migrations

- Additive and backward compatible: new nullable or defaulted columns and new tables. No dropped
  or renamed columns in the same release as the code that stops using them. Never edit a
  merged migration.
- `uv run python manage.py makemigrations --check --dry-run` must say "No changes detected".
- A data migration is reversible or says why it isn't, and never rewrites events or history.

## 3. Templates and front end (follow the build-page skill and `.agents/rules/frontend.md`)

| # | Check | Fix |
| --- | --- | --- |
| 12 | Built from `{% load ui %}` components, no copied component markup, no `{% include %}` of a registered component | Use the tag; add a parameter to the component if needed |
| 13 | Tokens only: no inline `style`, hex, rgb or hsl, raw px or rem, untokened Bootstrap colour utility; muted is `.vr-muted`; warm only for time out of storage | Use a `--vr-*` token or class |
| 14 | Icons only via `{% icon %}` or `vrUi.icon`, Bootstrap Icons symbols | Copy the symbol into `static/img/icons.svg` |
| 15 | Controls through form widgets (no raw select, datalist, date, number or file input); large model choices use `RemoteSelect` with an authorized lookup | Render the form field; register the lookup |
| 16 | Every form opens in the dialog: new POST routes are in `FORM_SURFACES` or `NOT_FORM_PAGES` (`core/form_surfaces.py`) | Register the route |
| 17 | Page type rules: one primary, at most three secondary plus More, identifier row links, list states, breadcrumbs only where due | ui-standards §2–§6 |
| 18 | Must-see facts (status, holds, out of storage, pending approval) in `page_meta` or a notice, never colour alone | Move them up; add a word or icon cue |
| 19 | Panel spacing owned by the panel (no padding or margin utilities inside panel bodies) | `panel_section`, `table=True` |
| 20 | Wording (ui-standards §9), sentence case, the same term as the menu and title; codes shown in words | Rename |

Printed output (labels, pick lists, manifests): any new sentence on a printed document is listed
for the owner's approval in the notes for reviewers. Labels carry only the specimen ID.

## 4. Tests and page help

- New or changed behaviour has a test next to the app's existing tests: authorization (the
  allowed role and a refused one, and a role-less or inactive user on lookups), transitions and
  their audit events, exact quantities, position occupancy and the stale-edit path where
  relevant. Fix the code, not the test, unless the test is provably wrong. A test that guards a
  gate or evidence (disposal approval, distribution gates, custody, position uniqueness, audit,
  holds, consent and withdrawal) is never loosened to make it pass; read markup through stable
  `data-*` hooks instead.
- Any page, field, action, filter, permission or rule change has its help updated in the same
  change: run the `document-page` skill. New POST-only, download or JSON routes go in
  `NON_PAGE_ROUTES` in `tests/test_page_docs.py`.
- Any change users, deployers or integrators can observe has an entry in `changelog.d/` and a
  regenerated `CHANGELOG.md`: run the `changelog` skill. Nothing observable: the pull request
  says so.

## 5. Run the gate

From the repository root, with your own test database so parallel runs don't collide:

```sh
set -a; . ./.env; set +a
uv run ruff check . && uv run ruff format --check .
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py changelog --check
uv run python manage.py collectstatic --noinput -v0
TEST_DATABASE_NAME=test_vapor_<you> REQUIRE_WEASYPRINT=1 uv run pytest <affected paths> -q
```

Affected paths are the changed apps' `tests/`, the `tests/test_*.py` that cover what you changed
(`test_ui_standards`, `test_include_isolation`, `test_controls`, `test_icons`, `test_navigation`,
`test_form_dialog` and `test_page_docs` for any template change), and the full
`REQUIRE_WEASYPRINT=1 uv run pytest` when shared code (`core/`, `accounts/`,
`templates/includes/`, `static/css/app.css`, settings) or a migration changed. The gate tests
live in `*/tests/test_permissions.py`, `test_concurrency.py`, `test_history_guards.py`,
`specimens/tests/test_custody.py`, `test_disposal.py`, `test_outputs.py` and
`distribution/tests/test_gate.py`. Fix and rerun until green. Run each failing test once more by
itself before calling it flaky.

When templates, `static/js` or `static/css` changed, run the browser journeys that open those
pages:

```sh
# find the journeys that visit the changed pages: grep -ln "<url path or route>" e2e/test_*.py
TEST_DATABASE_NAME=test_vapor_<you> uv run --group e2e pytest -c e2e/pytest.ini \
  e2e/<affected journeys>.py e2e/test_theme.py e2e/test_keyboard_focus.py
```

## 6. PR evidence: before/after screenshots (any UI change)

Every PR that changes what a page looks like carries before/after screenshots. With demo data
and `DEMO_PASSWORD` in `.env`, and the development server on 127.0.0.1:8020:

```sh
set -a; . ./.env; set +a
uv run --group e2e python scripts/ui_compare/capture.py before --ref origin/develop
uv run --group e2e python scripts/ui_compare/capture.py after
open .local/shots/index.html
```

- `before --ref` exports the ref with `git archive` and serves it on :8027 against the same
  database, so both sides show the same records. `after` replays the before URLs against your
  server; `--recrawl` crawls again when the change adds pages. `--roles` limits the roles.
- Output goes to `.local/shots/` (git-ignored), every role at 1280 and 1440, light and dark.
  The run exits non-zero if a page failed, and each side's `manifest.json` lists the gaps.
  Usage details: `scripts/ui_compare/README.md`.
- Attach the relevant pairs (light and dark) to the PR description under "Before / after".
  Never commit the screenshots.

## 7. Summary and notes for reviewers

End with this table, then the notes for reviewers:

```markdown
## Review summary

| Area | Result | Notes |
| --- | --- | --- |
| Scope | N files (Python, templates, migrations, tests, docs) | |
| Domain and authorization | Pass / Fixed N | file:line, what changed |
| Migrations | None / Additive, check clean | |
| Templates and UI rules | Pass / Fixed N | |
| Tests added or updated | Yes / Not needed (why) | |
| Page help and changelog | Updated / Not needed (why) | |
| ruff, format, check, makemigrations, changelog, collectstatic | Pass | |
| pytest (paths) | N passed | failures and their owner if not yours |
| Browser journeys | N passed / Not run (no UI change) | |
| Screenshots | .local/shots/index.html / Not needed | |

### Notes for reviewers
- Decisions about policy or behaviour, migrations, dependency changes, new printed wording,
  failures outside this diff, anything not fixed and why.
```

Flag rather than auto-fix a change that would alter behaviour, a public URL, printed output or
stored data. Say so explicitly when the diff affects authorization, custody or audit history,
position occupancy, disposal or distribution gates, a migration or printed output: the PR
template asks reviewers to look there.
