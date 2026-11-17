# Architecture

Baseline: 2026-11-12, adapted from the OpenBGA Environmental LIMS architecture of 2026-11-08. Product policy:
`docs/product.md`; installed versions: manifests/lockfiles. Working assumption: one repository
organization per deployment, staff-only access, modest volume and online operation. No shared
tenancy or offline sync.

## Stack

Use a Django monolith with server-rendered pages and PostgreSQL. Python is a good fit for exact
decimal data handling and integrations with scanners and printers; Django supplies mature forms,
authentication, transactions, migrations and an ORM in one conventional application.

| Area | Selected baseline | Reason |
| --- | --- | --- |
| Runtime/framework | Python 3.14.10; Django 5.2.19 LTS | Supported combination; Django security support through April 2028 |
| Database | PostgreSQL 17; Psycopg 3.3.9 | Exact numerics, constraints, row locks and managed backups |
| Identity | Django sessions/auth/groups + django-allauth 65.22.0 | Standard account lifecycle, rate limits and MFA capability |
| UI | Django templates/forms, Bootstrap 5.3.8, HTMX 2.0.11, DataTables 3.1.5 | Mature components and progressive enhancement without a separate SPA |
| Serving | Gunicorn 26.2.0, WhiteNoise 6.12.0 | One web service; locally served versioned static assets |
| Documents | WeasyPrint 70.0; python-barcode 0.16.1 | Label and manifest PDFs; standard Code 128 SVG generation |
| Tooling | uv lockfile; Ruff; pytest-django; Playwright | Reproducible environment and focused server/browser verification |

Versions are those OpenBGA Environmental LIMS checked against publisher documentation and package registries
on 2026-11-08, except django-allauth and Psycopg, whose newer patch releases the fresh OpenBGA Biorepository
`uv.lock` of 2026-11-12 resolved (allauth 65.22.0 adds reauthentication-by-code pages, which have
help). `uv.lock` pins the resolved transitive dependencies and CI builds and checks the container. Use current compatible security patches.
PostgreSQL's managed minor patch is provider-controlled. SciPy is not carried over: no OpenBGA Biorepository
feature needs statistics. See [Django support](https://www.djangoproject.com/download/),
[Python compatibility](https://docs.djangoproject.com/en/5.2/faq/install/),
[allauth requirements](https://docs.allauth.org/en/latest/installation/requirements.html),
[Bootstrap](https://getbootstrap.com/docs/5.3/getting-started/introduction/),
[HTMX](https://htmx.org/docs/) and [WeasyPrint dependencies](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html).

A React/API split adds contracts and deployment/testing surface without a demonstrated need.
Use DataTables with its Bootstrap adapter and ColReorder for column presentation only. Keep
Django-rendered rows, full-queryset filtering/sort links and pagination; disable client sorting,
search and paging. Browser preferences store column IDs/order/visibility only, with keyboard
controls and reset. No Ajax row API, server preference model, or second table/query framework.

## Shared platform

OpenBGA Biorepository is the sibling of OpenBGA Environmental LIMS and deliberately shares its platform layer:
`config/`, `accounts/`, `core/`, root `templates/` and `static/`, the `e2e/` harness, platform
tests, `skills/document-page`, `docs/ui-standards.md`, CI and deployment files. Keep those files as
close to the Environmental LIMS originals as the rename allows, so fixes can move between the two products
as patches. The bootstrap of 2026-11-12 is the baseline for that comparison.

Naming (2026-11-16): the product is OpenBGA Biorepository, in `openbga/biorepository` (formerly
Vapor Phase, `openbga/vapor-phase`); its sibling is OpenBGA Environmental LIMS,
`openbga/environmental-lims` (formerly Smart Roots LIMS). The older names survive only as legacy
identifiers, for example the Render service, database and cron names, the demo hostname, the `vapor`
database and user (and the `vapor_*` CI and test databases), the `vapor.*` logger, the `vr-` prefix,
the `VP-` identifier examples and the Docker Compose project name `vapor-phase`. GitHub redirects the old repository URL, but not the GitHub Pages
site, which moved from `openbga.github.io/vapor-phase/` to `openbga.github.io/biorepository/`.

Names fixed before the first migration and deployment:

| Thing | Value |
|---|---|
| Distribution and image name | `biorepository` |
| Render service | `vapor-phase`, kept from before the 2026-11-16 rename so existing Blueprint instances and the public demonstration at `vapor-phase.onrender.com` stay the same resources |
| Database name and user (development) | `vapor` (compose: 127.0.0.1:55434) |
| Render database | `vapor-phase-db`, database and user `vapor` |
| Organization singleton (Environmental LIMS `Lab`) | `core.Repository`, exposed to templates as `repository` |
| Setup command (Environmental LIMS `setup_lab`) | `setup_repository` |
| URL prefix for organization pages (Environmental LIMS `lab/`) | `repository/` (URL names unchanged) |
| Roles (groups) | `admin`, `manager` (repository manager), `technician`, `viewer` (default list) |
| Disposal approval (Environmental LIMS `authorize_release`) | permission `accounts.approve_disposal`, granted per person |
| Request logger (Environmental LIMS `roots.request`) | `vapor.request` |
| CSS/JS prefix | `vr-` kept, so platform files stay comparable with Environmental LIMS |
| Theme | Re-skinned through the color tokens only; new mark and sign-in scene |
| Specimen ID | `VP-` and six or more digits from a PostgreSQL sequence |

Intentional divergences from Environmental LIMS (keep this list current):

- `accounts.User` has no client link; there is no portal. Portal layout, portal navigation,
  client announcements (`show_to_clients`) and client checks in roles, setup and admin are gone.
- The immutability trigger function `prevent_record_mutation()` lives in `core` (migration 0002,
  which also protects `core_auditevent`); each app attaches it to its own event tables in its own
  migration with `core.immutability.protect_history`. Environmental LIMS attached a domain table in the
  core migration.
- One `staff_role_required` decorator, in `accounts.roles` (Environmental LIMS had one per domain app).
- No role group receives disposal approval; `setup_repository` creates the four role groups only.
  Environmental LIMS gave release authority to the `approver` group.
- Navigation (`core/templatetags/navigation.py`) and form surfaces (`core/form_surfaces.py`) hold
  OpenBGA Biorepository routes only.
- `core/security_headers.py` has no PDF-preview framing exception (Environmental LIMS reporting).
- The `action_label` history filter is `core/templatetags/history.py` (Environmental LIMS
  `laboratory_extras`).
- Error pages choose the shell by `user.is_authenticated` (Environmental LIMS also chose the portal).
- The top bar is navy in both themes, set through the color tokens.
- Chart.js and Tom Select are not vendored, and `core/widgets.py` (the list-or-other and
  suggestion inputs) with its widget templates is not carried over: no OpenBGA Biorepository page uses them.
  `static/css/app.css` drops their styles and the Environmental LIMS domain styles (quote documents,
  requested-test pickers, container rows, the labels menu). The `.tomselect` branches in
  `static/js/app.js` and `static/js/form-dialog.js` stay, unused, so those files remain identical.
- Uploaded files are `core.Attachment` rows (`core/attachments.py`, migration `core` 0003): each
  file stored once and referenced by equipment and distribution event versions, with the optional
  `ATTACHMENT_STORAGE_LIMIT_BYTES` total cap. Environmental LIMS keeps attachment bytes on its
  `laboratory` records.
- `static/js/form-dialog.js` refreshes shared row-selection counts after restoring controls from
  their temporary loading state; `static/js/app.js` exposes the existing initializer for that call.
- `templates/includes/pagination.html` accepts a fixed page size without a rows-per-page control
  for the bounded excursion hold picker; the default pagination behavior is unchanged.
- The `checks` workflow's `application` job allows 60 minutes (Environmental LIMS: 30): the OpenBGA Biorepository
  suite is larger. Lower it if CI runs leave room.

## Application boundaries and integrity

Keep a few Django apps within one project and database. Views/forms handle transport and
validation; small explicit domain functions handle multi-record transitions in
`transaction.atomic`. Ordinary ORM queries need no repository wrapper. Use constraints and
selective row locks/version checks for concurrency. Do not encode approval policy in signals or
spread it across UI handlers.

### Domain applications

| App | Owns |
|---|---|
| `storage` | Sites, rooms, storage units and classes, container types and grids, racks, boxes, positions, occupancy, moves |
| `specimens` | Participants (coded), collections, specimen types, specimens and aliquots, quantity ledger, placement, custody events, check-out/in, holds, disposal |
| `equipment` | Equipment linked to storage units, service events, LN2 level and fill log, temperature readings and excursions |
| `quality` | Box inventory audits with immutable snapshots and observations; deviation reports, investigation, corrective action and independent review; staff training assignments and completion evidence with append-only history |
| `distribution` | Studies and their collections, consent records and withdrawals, institutions, investigators, requests and decisions, allocations and pick lists, shipments and manifests, receipts, returns, agreements |

Label PDFs live in `specimens`; pick lists and manifests in `distribution`. The `quality` app holds inventory audits, deviations and staff training records.

Integrity rules: append-only event tables protected by database triggers; one occupant per
position by a unique constraint, allocation under row lock; state change, checks and audit event
in one transaction; optimistic revision on edited records; exact numerics with units. `specimens`
depends on `storage`, never the reverse: storage exposes positions and containers, and learns what
occupies them only as opaque occupancy keys. `equipment` depends on `storage`, reads specimen
history through public queries, and places explicitly selected manual excursion holds through the
specimen service under the storage gate. Holds and their excursion provenance commit together;
equipment never changes specimen rows directly. `distribution` depends on `specimens` and `storage`
and changes specimens only through the specimens services (holds, check-out and check-in, dispatch, return).
Neither `storage` nor `specimens` depends on `equipment`, `distribution` or `quality`.
`quality` reads storage and specimen identities and takes the storage gate for consistent audit
snapshots and completion checks. It never changes custody or inventory. Its snapshots and
finalizations use the same append-only database protection as other history records.
Deviation transitions lock their own record and check its revision; they change no storage or
specimen state and need no storage gate. The report is undeletable, and every change adds a
protected event and field-level audit entry in the same transaction. Optional links to equipment
excursions and inventory audits provide context without changing their workflows.

Training transitions likewise lock only their own record, check its revision and append protected
history plus field-level audit in the transaction. Staff accounts are protected references;
training records neither grant roles nor gate operational workflows.

### Accounts and access

Use Django's user model extensibility from the first migration. Staff permissions use groups and
explicit capabilities: a role is a group, a capability is a permission, superuser grants nothing in
the application. Admin implies the operational roles but not disposal approval or request
decisions (`approve_disposal`, `approve_distribution`, granted per person). Nobody approves a
disposal they requested or decides a request they created or submitted; the services check that on
the server. Invitation-only access is
the initial assumption; never commit bootstrap credentials. Allauth supplies login, reset,
verification and MFA mechanisms; repository roles and record authorization remain application
policy. Use CSRF protection, secure sessions, HTTPS and explicit trusted hosts/origins.

### Records and history

Store Decimal values with declared scale/precision, original supplied text and units as distinct
data. Events store when they occurred and when they were recorded. Corrections retain actor,
timestamp, reason and prior linkage. Mutations write an attributed audit event
(`core.audit.record_event`, which refuses to run outside `transaction.atomic`) in the same
transaction; background/system actions have an explicit identity. History tables refuse UPDATE and
DELETE by trigger (see [Operations](operations.md)). Separate migration credentials where the host
allows. Application append-only records alone do not prove resistance to database administrators;
stronger tamper evidence is a future policy choice.

Use deployment `TIME_ZONE` for new forms and CSV input; `Repository.timezone` is a recorded setup
copy only. Never reinterpret old records.

Initially store bounded PDFs/attachments in PostgreSQL: each file once, as an immutable attachment
record with metadata and checksum that event versions reference; list queries must not fetch
the bytes. `ATTACHMENT_STORAGE_LIMIT_BYTES` optionally caps the total. A provisional 10 MiB per-file limit and bounded
request sizes keep this practical for a small deployment. Revisit before substantial volume; use
Django storage + private S3-compatible object storage then, with staged writes, reconciliation and
coordinated restore. Never put persistent files on Render's ephemeral filesystem.

PDF rendering accepts only application templates and escaped data, with local approved assets;
deny arbitrary network/file fetching. Start with bounded synchronous work and explicit retryable
failures. Add a standard durable queue only when measured job duration/concurrency requires it; do
not build an ad hoc scheduler.

## User experience

Bootstrap supplies standard controls; a coherent theme and task-oriented shell must deliver a
professional operational workspace (`docs/ui-standards.md`). Tables need readable density, aligned
numbers/units, useful columns, server-side sorting/filtering/pagination and clear identity/status.
Forms need logical groups, explicit units, inline errors, preserved input and keyboard focus.
HTMX preserves feedback/focus and CSRF; serve pinned assets locally. Django admin is restricted
maintenance tooling only.

Page help is versioned plain-text content selected from the authorized response's resolved route,
including distinct form variants. It introduces no record API or database state. The browser keeps
per-account panel visibility and width preferences, as it does table presentation preferences.
Route-coverage checks require help for every HTML page; downloads and transitions are documented
on their invoking pages. The document-page skill defines the authoring and maintenance standard.

## Deployment and recovery

One Docker web service on Render plus managed PostgreSQL in the same US region is the initial
shape. Docker pins Python, PDF system libraries and fonts; Gunicorn serves the app, WhiteNoise
serves collected static assets. No Redis, worker, separate frontend host or persistent disk.
The public presentation at `/about/` ships with the same service. Only its static HTML and
assets live under the [dedicated WhiteNoise root](https://whitenoise.readthedocs.io/en/stable/django.html#whitenoise-root);
the workspace stays at `/` behind sign-in. The same files are published to GitHub Pages
(`.github/workflows/pages.yml`), which adds the Plausible analytics snippet to the published
copy only; their links are relative so both addresses work. Build assets
and collectstatic during image build; migrate once in a serialized pre-deploy step. Use
backward-compatible migrations while old/new instances overlap; destructive changes need a staged
migration and verified restore path. Rollback of code does not undo a database migration.

Health checks distinguish process health (`/health/`) from database readiness (`/ready/`). Send
structured logs with request identifiers and errors, not record payloads or credentials. Configure
timeouts, connection limits and size limits against actual service capacity. `reset_demo` is the
deliberate synthetic demo reset: it runs only where `ALLOW_DEMO_RESET=true`, only against the
seed's demonstration repository ("Example Biorepository") or an empty database, and only when the
schema matches the release, and it erases and seeds in one transaction. Its schedule lives in
`render.demo.yaml`, a separate Blueprint that the deploy button never uses (see
[Operations](operations.md#public-demonstration)).

Managed database recovery plus periodic encrypted off-provider exports are the minimum planned
backup approach. Prove restore to a separate database and compare custody and audit history before
relying on it. Retention, RPO/RTO, region and the responsible operator remain decisions. Real-data
use needs an operational runbook and tested recovery, not merely a successful demo deployment.

The Render Blueprint (`render.yaml`) describes the web service, database, generated secret and
required inputs. The README button targets
`https://render.com/deploy?repo=https://github.com/openbga/biorepository/tree/main`. It names `main`
because Render otherwise deploys the repository's default branch, which is `develop`. Set `autoDeployTrigger: off` for distributed button instances.
`render blueprints validate render.yaml` needs an authenticated Render workspace; on 2026-11-13 it
accepted `render.yaml`. The public demonstration at <https://vapor-phase.onrender.com/> runs this
shape with the reset job of `render.demo.yaml` (see
[Operations](operations.md#public-demonstration)). See [button requirements](https://render.com/docs/deploy-to-render),
[CLI](https://render.com/docs/cli) and [Blueprint reference](https://render.com/docs/blueprint-spec).

The Blueprint creates paid web and PostgreSQL resources; check [Render pricing](https://render.com/pricing)
before deployment. Storage, outbound bandwidth, build usage and any paid workspace are charged
separately, and a deployment also needs an email provider, off-provider backup storage and a domain.
Free Postgres expires after 30 days and lacks managed recovery, so it is not a durable demo
baseline. Deployers review and approve their own paid resources.

## Verification

Keep work in complete vertical slices (storage, specimens, throughput, distribution, equipment). Shared schema, migrations,
routing and dependencies change in one place at a time.

The combined gate covers: locked install/lint, PostgreSQL tests for roles, custody/history and the
immutability triggers, position uniqueness and concurrency, disposal and distribution gates; migration from
empty and prior schema plus restore; and a browser journey through the first journey in
`docs/product.md`. Inspect professional layout, keyboard navigation and PDF label output. Verify the
container and Blueprint before deploying. Run the checks a change affects; don't add tests that
simply restate templates. Passing synthetic tests is not validation of a repository's process.
Commands are in [CONTRIBUTING](../CONTRIBUTING.md#verify-your-change).

## Open decisions

| Decision | Current assumption / who decides |
| --- | --- |
| Role list, disposal authority and request decisions | Default four roles plus `approve_disposal` and `approve_distribution`; each repository decides (see [product scope](product.md#working-assumptions)) |
| Labels: 2D codes | Code 128 only; a 2D symbology is a new dependency, discuss first |
| Attachments: object storage | PostgreSQL binary columns while bounded; revisit with volume |
| Hosting budget and demo access/reset policy | The deployer approves budget and defines access/reset policy |
| Email delivery, MFA enforcement and account recovery | Allauth mechanisms selected; operating policy and provider are the repository's choice |
| License | AGPL-3.0-only (decided 2026-11-12); canonical GNU LICENSE and dependency notices included |
