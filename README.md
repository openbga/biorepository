# OpenBGA Biorepository: open-source biorepository management

[![checks](https://github.com/openbga/biorepository/actions/workflows/checks.yml/badge.svg)](https://github.com/openbga/biorepository/actions/workflows/checks.yml)

OpenBGA Biorepository is a biorepository management system for one repository organization per deployment,
and an open-source project of BGASoft Inc. It records where every specimen is, what it is, who
handled it and what happened to it, across freezers, liquid-nitrogen vapor-phase tanks, racks,
boxes and positions. Built with Django, PostgreSQL and Bootstrap/HTMX. AGPL-3.0-only.
Source: <https://github.com/openbga/biorepository>.

Status: the repository journey works end to end: storage, specimens, custody, disposal with
approval, labels, bulk import, equipment records, inventory audits, deviations, training, and
distribution from consent records and a request through shipment, receipt and return. Use synthetic data only.

This software supports a repository's own records and quality system; it does not certify
compliance or accreditation.

## What it covers

- **Storage:** sites, rooms, freezers and LN2 tanks (vapor or liquid phase), racks, towers,
  shelves and boxes, each box with a position grid from a container type. Occupancy is shown per
  unit, rack and box, free positions are searchable, positions can be blocked, and boxes and
  racks move with everything in them as one recorded event. Templates build a unit like an
  existing one.
- **Specimens:** registration with an opaque ID (`VP-000123` style) that carries no location or
  participant meaning, a coded participant (no names or record numbers), exact decimal amounts
  with units kept as a ledger, aliquots and derivatives with their lineage.
- **Custody:** check-out and check-in with time out of storage computed from the events, custody
  hand-overs, and freeze-thaw counts that record observed thaws and keep an unknown history marked
  unknown.
- **Holds and disposal:** disposal, use and all-use holds; disposal as a request, a decision by a
  different person who holds the approval permission, and a recorded act that frees the position
  and keeps the record.
- **Throughput:** Code 128 label PDFs, scan-to-lookup in the top bar, CSV import with a validation
  pass before anything is committed, filters, sorting, pagination and CSV export.
- **Equipment:** a register of the tanks, freezers, monitors and supply dewars that hold or watch
  the specimens, tied to their storage units; service events (maintenance, calibration, repair,
  mapping, qualification) with due dates the repository enters; an LN2 level and fill log;
  temperature readings entered or imported from CSV; and temperature excursions reviewed by a
  second person, with the specimens that were in the unit captured when one is closed. Operators
  can select affected specimens and place manual holds with a reason. Staff can filter and export
  readings with no recorded limits. The limits,
  low levels and due dates are the repository's own: the product sets none, and it does not
  monitor or raise alarms.
- **Inventory audits:** check one box against a saved inventory snapshot, enter observations
  manually or load a normalized scanner CSV for review, and retain discrepancies with an
  explanation. Completed and cancelled audits remain on record; an audit never changes specimen
  locations. CSV import uses the project format, one box at a time; device-specific formats
  require conversion.
- **Deviations:** report an issue, record investigation, corrective action and effectiveness
  evidence, then submit for a separate manager's review. Optional links connect a report to an
  excursion or inventory audit. Corrections, returns and reopening retain the earlier values and
  reasons. Staff can download the filtered deviation register as a CSV for review meetings.
  Review changes no specimen status or hold.
- **Training:** staff assignments with explicit due dates, completion evidence, trainer/provider,
  optional competency assessments and renewal dates. Managers record corrections and cancellation
  with retained history. Staff can filter the register and export matching rows to CSV. Records do
  not grant permissions or certify qualification.
- **Distribution:** studies that govern collections, consent records by scope category with
  withdrawal (which holds the participant's specimens and lists material already sent), escalation
  to destruction while retaining those holds, specimen
  requests decided by a different person who holds the request decision permission, allocation
  against what the records permit, pick lists, shipments with the shipper's declarations and
  manifests frozen at dispatch, receipt confirmation, returns held for a manager's review, and
  agreements (MTA, DUA). Specimens that leave are recorded as distributed, and their time away is
  kept apart from time out of storage. The software records what the shipper declares and what the
  repository's records say; it never classifies a shipment or decides consent, and nothing is
  emailed.
- **Platform:** sign-in with optional authenticator-app two-factor, four staff roles
  (administrator, repository manager, technician, viewer), announcements, an append-only
  audit trail with the values before and after and a reason, in-application help for every page,
  light and dark themes.

Corrections add a record with a reason. Events (custody, moves, check-outs, disposals) are
append-only and specimens are never deleted. [Workflow rules and limits](docs/workflows.md)
describes each of these in more detail, and what is not built: requester accounts, monitoring and
alarms come later (see [product scope](docs/product.md)).

![Dashboard with the work queues: check-outs in progress, specimens waiting for a position, holds and disposal requests](docs/images/dashboard.png)

![A cryobox as a position map: occupied, free and blocked positions](docs/images/box.png)

![A specimen record with its custody events and thaw records](docs/images/specimen.png)

![A liquid-nitrogen tank's equipment record: limits, service events, LN2 log and readings](docs/images/equipment.png)

![A specimen request with its progress panel: prepared, decided and allocated, with specimens picked and a shipment in preparation](docs/images/request.png)

Screens show the synthetic demo data.

## Hosted demo

Open the [OpenBGA Biorepository demo](https://vapor-phase.onrender.com/) on Render and sign in with one of
the demo accounts listed on its sign-in page. The [demo presentation](https://vapor-phase.onrender.com/about/)
is available without signing in. The demo is public and is erased and seeded again every day with
the full synthetic repository, so enter synthetic data only. See
[Public demonstration](docs/operations.md#public-demonstration) for how it is run.

![The sign-in page of a public demonstration, listing the shared demo accounts by role](docs/images/signin.png)

## Try it with Docker

Requires Docker with Compose. This builds the application image and runs it with a local
PostgreSQL database in development mode, reachable only from your machine. It is for evaluation,
not for real repository data.

```sh
git clone --branch main https://github.com/openbga/biorepository.git
cd biorepository
docker compose --profile app up --build -d
# Choose a private password (at least 12 characters) for the synthetic demo accounts.
read -s DEMO_PASSWORD; export DEMO_PASSWORD
# --full loads a synthetic working repository; without it you get the accounts and a short journey.
docker compose --profile app exec -e DEMO_PASSWORD web python manage.py seed_demo --full
```

Open <http://127.0.0.1:8000> and sign in as `demo_admin`, `demo_manager`, `demo_technician` or
`demo_viewer` with that password. Stop it with `docker compose --profile app down`; add
`--volumes` to delete the trial database.

## Local development

Requires Python 3.14, [uv](https://docs.astral.sh/uv/) and PostgreSQL 17. Docker Compose can run
the development database on port 55434. Commands below use the Unix shell:

```sh
uv sync --frozen
docker compose up -d db
export DEBUG=true
export DATABASE_URL=postgresql://vapor:vapor@127.0.0.1:55434/vapor
uv run python manage.py migrate
uv run python manage.py setup_repository --repository-name "Example Biorepository" \
  --admin-username owner --admin-email owner@example.invalid
# Supply a private, unique demo password (at least 12 characters) in the environment.
read -s DEMO_PASSWORD; export DEMO_PASSWORD
uv run python manage.py seed_demo
uv run python manage.py runserver 127.0.0.1:8000
```

Open <http://127.0.0.1:8000>. Seed accounts are `demo_admin`, `demo_manager`, `demo_technician`
and `demo_viewer`, using the supplied password. The organization is "Example Biorepository". The
seed is explicit and idempotent: it never resets existing passwords or deletes records. It
refuses an existing non-demo repository, so run `setup_repository` with a real name only on a
database you will not seed. No seed or reset runs during normal deployment.
Add `--full` for a synthetic working repository: about 350 specimens from 70 coded participants
in seven storage units and thirty boxes (from empty to full), aliquot families, check-outs in
progress for up to a month, holds, disposal requests in every state, bulk imports and
corrections, plus the equipment record of every unit with about three months of service events,
LN2 level checks, temperature readings and excursions, and distribution (studies, consent
records with a withdrawal, agreements, requests, a received shipment with a return and a packed
one), inventory audits, deviations and training records in several states, all made through the application's own services. It takes from about 30 seconds to a minute and a half, depending on the machine, and adds
nothing when run again.

For a public demonstration, `seed_demo --announce-accounts` also posts an announcement on the
sign-in page that lists the manager, technician and viewer accounts and `DEMO_PASSWORD`, where
anyone can read them. It leaves out `demo_admin`, which manages announcements and accounts, and
refuses to run while `DEMO_PASSWORD` opens that account: set a different `DEMO_ADMIN_PASSWORD` (at
least 12 characters) before the first seed. The listed accounts become shared demonstration
accounts: whoever signs in cannot change their password, email addresses or two-factor settings.
A database that is emptied and seeded again gets the same announcement back; on an existing
database the seed leaves an announcement an administrator changed or withdrew as it is.
`reset_demo` erases a demonstration database and seeds it again on a schedule (the public
demonstration uses `--full`); see
[Public demonstration](docs/operations.md#public-demonstration). It is never part of a
repository's deployment.

Disposal approval is the permission `accounts | approve_disposal`, and deciding specimen requests
is `accounts | approve_distribution`. The seed grants both directly to `demo_manager`; neither is
granted to any group or restored on rerun, so a visitor can create a request as the technician and
decide it as the manager. Administrator or superuser status alone grants neither.

`.env.example` documents settings; Django does not automatically load `.env`. Never commit secrets.

To check a change, see [Verify your change](CONTRIBUTING.md#verify-your-change).

## Set up your repository

On a fresh database, configure `DATABASE_URL`, `SECRET_KEY`, `TIME_ZONE` and the production host/
HTTPS settings from `.env.example`, then run:

```sh
uv run python manage.py migrate
uv run python manage.py setup_repository --repository-name "Your Repository Name" \
  --admin-username owner --admin-email owner@repository.example
```

The command prompts privately for the password, or accepts `SETUP_ADMIN_PASSWORD` from the
environment. It creates the repository record, the role groups and the first administrator.
Reruns retain passwords, existing roles and granted permissions. `--update-repository` explicitly
renames the repository; `--sync-timezone` records a changed deployment `TIME_ZONE`. Keep demo
seeding separate from a real repository database.

Sign in at `/accounts/login/`. The owner manages users and group membership at `/admin/`.
Signed-in users may optionally enrol an authenticator app from the account menu's
"Two-factor authentication" page (`/accounts/2fa/`). MFA is not enforced; the repository chooses
its enforcement and account-recovery policy. The first owner has the `admin` role;
`createsuperuser` alone does not assign a repository role.

| Role | Work it enables |
| --- | --- |
| `admin` | Configure the repository, post announcements, record the retention statement, and do manager, technician and viewer work |
| `manager` | Repository manager: define storage, block positions, manage catalogues, correct records and release holds, record consent and agreements, allocate specimens, as well as technician work |
| `technician` | Day-to-day work: register, store, check out and in, split, hold, request disposal, import, equipment entries, requests, picking and shipments |
| `viewer` | Read-only access, including exports and labels |

The role list is a default; see [product scope](docs/product.md#working-assumptions).

Disposal approval is not a role. It is the permission `accounts | approve_disposal`,
granted per person or group in `/admin/` to qualified staff. No role group carries it, the `admin`
role does not imply it, and nobody may approve their own disposal request (the server refuses it).
Deciding a specimen request is likewise the permission `accounts | approve_distribution`, granted
per person, and nobody decides a request they created or submitted.

Account administration requires a superuser and is separate from repository roles. To add a role
to an existing owner from the service shell, use `setup_repository --admin-username owner --role
admin` (or another role); this adds membership without removing other roles or restoring
permissions.

## Deploy to Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/openbga/biorepository/tree/main)

The Blueprint packages a Docker web service (`vapor-phase`) and PostgreSQL 17
(`vapor-phase-db`); these resource names predate the product's current name and are kept so
existing instances are unchanged. **It creates paid resources:** review and accept the current charges in your
Render account before deploying. For a private fork, authorize Render's GitHub App to access the
source. Distributed instances do not automatically deploy upstream changes.

The Blueprint generates `SECRET_KEY` and wires `DATABASE_URL`. The app derives its allowed host
and trusted HTTPS origin from Render's `RENDER_EXTERNAL_HOSTNAME`; no manual origin input is
needed for the Render domain. Set comma-separated `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`
for additional custom domains/origins. HTTPS and secure cookies are enabled when `DEBUG=false`.
Set SMTP settings/`EMAIL_BACKEND` for real reset and sign-in-code mail; the packaged default
disables mail delivery until configured. In the service shell, run
`python manage.py setup_repository --repository-name "Your Repository Name" --admin-username owner --admin-email owner@repository.example`
and follow the [repository setup steps](#set-up-your-repository). Never embed passwords in the
image or Blueprint.

Build collects static assets; pre-deploy runs migrations once. Health endpoints are `/health/`
(process) and `/ready/` (database). No user uploads should be written to the ephemeral filesystem.
Back up PostgreSQL, retain encrypted off-provider exports, and prove restoration before real use;
see [Operations](docs/operations.md). Use compatible staged migrations: redeploying old code
does not roll back a changed database.

Authenticated validation, without provisioning: `DO_NOT_TRACK=1 render blueprints validate
render.yaml` after `render login` and workspace selection.
See [Render's deploy button documentation](https://render.com/docs/deploy-to-render).

## Documentation

| Document | What it covers |
| --- | --- |
| [Workflow rules and limits](docs/workflows.md) | What each workflow does and the limits to know before relying on it |
| [Product scope](docs/product.md) | What the product must do, the domain rules behind it and their sources |
| [Architecture](docs/architecture.md) | Stack, integrity and authorization design, deployment shape |
| [Operations](docs/operations.md) | Backup, restore, recovery and logs for a self-hosted deployment; running a public demonstration |
| [UI standards](docs/ui-standards.md) | Layout, navigation, table and form conventions |
| [Third-party notices](THIRD_PARTY_NOTICES.md) | Licences of bundled and required components |

Inside the application, the documentation icon at the top right opens help for the current page.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the ground rules, setup and
checks, and the [Code of Conduct](CODE_OF_CONDUCT.md). Report security problems privately as
described in [SECURITY.md](SECURITY.md). [AGENTS.md](AGENTS.md) holds the working rules for AI
coding agents.

## Project and support

OpenBGA Biorepository is an open-source project of [BGASoft](https://bgasoft.com), a laboratory software company, and part of [OpenBGA](https://github.com/openbga).

**Community use.** The software is free under the AGPL-3.0-only licence and is provided as is. The project does not include a support commitment: issues and pull requests are welcome and are answered on a best-effort basis.

**Enterprise support.** BGASoft offers paid support and services for organizations that run this software. Are you looking for support? Contact [bgasoft.com](https://bgasoft.com). Please do not use GitHub issues for support requests of that kind.

## Licence

Copyright (C) 2026 BGASoft Inc. and OpenBGA contributors.

OpenBGA Biorepository is licensed under the GNU Affero General Public License version 3 only
(AGPL-3.0-only); see [LICENSE](LICENSE) and [NOTICE](NOTICE). If you run a modified version for
users over a network, AGPL section 13 requires offering them its source: set `SOURCE_CODE_URL` to
your published source so the "Source code" link in the application footer points to it.

OpenBGA, OpenBGA Biorepository and their logos are trademarks of BGASoft Inc. The licence
does not grant permission to use them (AGPL-3.0 section 7(e)), except as needed to describe the
origin of the software.

Dependency licenses remain their respective owners' terms; preserve notices when distributing
(see [Third-party notices](THIRD_PARTY_NOTICES.md)).
