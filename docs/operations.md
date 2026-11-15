# Operations: backup, restore and recovery

Runbook for operators of a self-hosted deployment. It covers what to protect and how to prove
a restore. Settings and setup steps live in the [README](../README.md); design rationale in
[architecture](architecture.md). Commands use placeholder credentials; keep real ones in your
shell or secret manager, never in Git, tickets or logs.

## What is protected

All state is in PostgreSQL: accounts, the repository record, announcements, the audit trail,
storage, specimens, custody, equipment, distribution records, inventory audits and deviations. Uploaded files (equipment and
agreement attachments) are stored once in the `attachment` table as a binary column with their
SHA-256 hash and size, so there is no file storage to back up separately. The container
filesystem is disposable.

Treat every dump as sensitive. It holds repository records and account data, including each user's
authenticator-app (TOTP) secret and recovery codes: the installed django-allauth stores them
unencrypted unless the deployment configures its encryption hook, which this project does not.
Encrypt backups and restrict who can read them.

`SECRET_KEY` is not stored in the database; keep it in your secret manager. Changing it signs
users out and invalidates outstanding password-reset and email-confirmation links, sign-in codes
and CSRF tokens. It does not make MFA enrolments unusable, because allauth does not derive MFA
secrets from it. Keep the same key when recovering unless you intend those effects.

**Immutability protection, exactly.** Core migration 0002 creates the trigger function
`prevent_record_mutation()` and attaches it to `core_auditevent` as the `audit_immutable` trigger.
Each domain app attaches the same function in its own migrations through
`core.immutability.protect_history`: append-only event tables and `attachment` get an
`<table>_immutable` trigger that refuses `UPDATE` and `DELETE`, and records that change state but
are never deleted (specimens, position occupancy, studies, shipments and similar) get a
`<table>_undeletable` trigger that refuses `DELETE`. Triggers do not block `INSERT`,
so `pg_restore` works. They do not block `TRUNCATE`, `DROP TABLE` or schema changes, and the table
owner or a superuser can disable them (`ALTER TABLE ... DISABLE TRIGGER`,
`session_replication_role`). Runtime and migration use the same database role today. Do not
repair history in place; restore into a new database and compare instead. This is not tamper
evidence against database administrators.

## Decisions the operator must make

The software does not choose these. Record them before real data.

| Decision | Notes |
| --- | --- |
| Recovery point and time objectives | Render point-in-time recovery covers 3 days on Hobby workspaces and 7 on Pro or higher; Free databases have none ([Render backups](https://render.com/docs/postgresql-backups), checked 2026-11-09). |
| Off-provider destination and encryption | Render keeps logical exports seven days only. Store your own copies encrypted (for example `age` or `gpg`) at a location you control. |
| Retention period and responsible person | The repository's policy and any rules that apply to it decide; the software infers none. |
| Restore-test cadence | Suggested minimum: before go-live and after any major version or migration change. |

## Take a backup

Use PostgreSQL client tools of the same major version as the server (17).
`SOURCE_URL` is the database's external connection string; the Render dashboard shows it, and
your IP must be on the database allow list (the Blueprint default is empty, so none).
If you cannot connect externally, use the dashboard **Recovery → Create export**
instead, which yields a `.dir.tar.gz` directory-format archive for `pg_restore`.

```sh
export SOURCE_URL='postgresql://USER:PASSWORD@HOST/vapor'   # placeholder
pg_dump --format=directory --jobs=4 --no-owner --file=vapor-phase-$(date -u +%Y%m%dT%H%MZ).dump "$SOURCE_URL"
```

Encrypt and copy the result off-provider, then record its date and size. Also capture a
verification summary (see below) beside it so a later restore can be compared.

## Restore into an isolated database

Never restore over the live database. Create an empty, separate PostgreSQL 17 database (a
second Render database, or the local Compose database on port 55434) and an app environment
that points only at it.

```sh
export TARGET_URL='postgresql://vapor:vapor@127.0.0.1:55434/vapor_restore_check'   # placeholder
psql 'postgresql://vapor:vapor@127.0.0.1:55434/postgres' -c 'CREATE DATABASE vapor_restore_check'
pg_restore --exit-on-error --no-owner --no-privileges --jobs=4 --dbname="$TARGET_URL" vapor-phase-YYYYMMDDTHHMMZ.dump
```

- `pg_restore` targets an empty database; `--clean` drops objects first and is not needed here.
- `--single-transaction` gives all-or-nothing behaviour but cannot be combined with `--jobs`.
- Roles named in the dump may not exist on the target; `--no-owner` makes you the owner.
  The runtime role then needs ordinary table privileges on the restored objects.
- Source of semantics: [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html).

For a Render point-in-time recovery, the platform creates a **new** database instance; do
not delete the original until you have verified the new one.

## Verify the restore

Counts only compare meaningfully at a consistent boundary. `pg_dump` itself reads one
snapshot, but queries you run separately do not share it, and the live database keeps changing.
Either suspend the web service (writes quiet) while you take the dump and run the source
summary, or skip the source comparison and rely on the self-consistency checks below, which
need no live comparison. Run the restored-database checks with the app configured as in
production (`DEBUG=false`) and `DATABASE_URL` set to the restored database.

Source summary, taken with writes quiet; save the output beside the dump. Run the same file with
`psql --no-psqlrc --file=summary.sql "$URL"` on the source and later on the restored database:

```sql
-- summary.sql: read-only. Row counts of every application table, then the history triggers.
\pset footer off
\pset tuples_only on
select table_name, (xpath('/row/c/text()', query_to_xml(
         format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text
from information_schema.tables
where table_schema = 'public' and table_type = 'BASE TABLE' order by 1;
select tgrelid::regclass::text, tgname, tgenabled from pg_trigger where not tgisinternal order by 1, 2;
```

1. `python manage.py migrate --check` exits successfully (no unapplied migrations) and
   `python manage.py check --deploy` reports only the HSTS warnings described under HTTPS policy.
2. The summary on the restored database equals the saved source summary: same row counts, and
   every `*_immutable` and `*_undeletable` trigger present and enabled (`O`). If you also compare
   constraint or index definitions, expect PostgreSQL to print `IN`-list checks differently after a
   restore (`ANY ((ARRAY['a'::character varying])::text[])` becomes
   `ANY (ARRAY[('a'::character varying)::text])`); the meaning is the same.
3. Stored files are intact; this must return 0:
   `select count(*) from attachment where encode(sha256(data), 'hex') <> sha256 or octet_length(data) <> size`
4. Start the app against the restored database, check `/ready/`, sign in as an administrator
   and open the dashboard, a specimen's history, the record retention page and the
   announcements list.
5. On a scratch copy only, inside a transaction you roll back, an
   `UPDATE core_auditevent SET reason = reason` and a `DELETE FROM specimens_specimen` must both
   fail with "Historical records cannot be updated or deleted".

Record the result (date, backup file, checks passed) in your own change log. A backup that
has not passed these checks is untested.

## Recover production

Pick the situation first; they are different procedures.

**Bad release, database healthy.** Redeploy the previous code if it is compatible with the
current schema. Project policy is backward-compatible migrations, so the previous release usually runs
against a newer schema, but check the migrations added since it (a dropped or renamed column
breaks it). Do not restore the
database: that would discard records entered since the backup.

**Data loss or corruption.** Recover into a new database; never overwrite or discard the
current one.

1. Stop writes (suspend the web service) and keep the damaged database untouched.
2. Restore a backup or point-in-time recovery into a new database and verify it as above.
3. Records entered after the backup exist only in the damaged database. Export or
   reconcile them with the repository's staff before choosing which database becomes production.
4. Point `DATABASE_URL` at the chosen database and deploy; pre-deploy runs
   `migrate --noinput`, a no-op if the schema is current.
5. Keep the other database until the repository signs off.

A migration that must be reverted needs a deliberately written reverse or a staged fix, not a
blind restore. Take a backup before each deploy that adds migrations.

## Sign-in limits behind a proxy

Failed sign-ins, password resets and sign-in code requests are limited per account and per
client address. The address is the connecting peer unless `ALLAUTH_TRUSTED_PROXY_COUNT` says how
many trusted proxies append to `X-Forwarded-For`. Behind a hosting platform's proxy the peer is
the proxy for every visitor, so with the default of `0` all visitors share one per-address limit:
repeated failed sign-ins by anyone can block sign-in for everyone until the window passes.

Set `ALLAUTH_TRUSTED_PROXY_COUNT` to the number of proxies you control or trust between the
visitor and the application. Verify it before relying on it: sign in from a known address and
confirm that address is the entry that many places from the end of the `X-Forwarded-For` header
your service receives. A count that is too high lets a visitor forge the address and escape the
limit; if the header has fewer entries than the count, the request fails. The Blueprint does not
set a value because the platform's proxy chain must be confirmed on the running service.

Some platforms instead put the visitor's address in a header of their own and replace whatever a
client sends in it. If yours documents such a header, set `ALLAUTH_TRUSTED_CLIENT_IP_HEADER` to
its name; it is then used instead of the proxy count. Use it only for a header the platform
guarantees: a header that a client can set lets anyone choose the address they are counted under.
On Render, Cloudflare writes `CF-Connecting-IP` on every request that reaches a public web service,
so a caller cannot choose its value
([Render, on trusted proxy headers](https://render.com/articles/host-pocketbase-on-render), checked
2026-11-13; on that date the edge refused a request that supplied the header itself). The public
demonstration sets `ALLAUTH_TRUSTED_CLIENT_IP_HEADER=CF-Connecting-IP`.

Limit counters are kept in each worker process's memory. With several Gunicorn workers or
instances the effective limit is multiplied accordingly; configure a shared Django cache if you
need exact limits.

## Browser security headers

Every response carries a Content Security Policy that allows scripts, styles, images, fonts and
connections from the deployment's own origin only, with no inline scripts or styles, and a
Permissions-Policy that turns off camera, microphone, geolocation, payment and USB access. The
application's own pages need nothing more. If a deployment adds approved sources of its own, set
`CONTENT_SECURITY_POLICY` to the complete replacement policy; it is sent as given, so keep
`frame-ancestors 'none'` and avoid `'unsafe-inline'` and `'unsafe-eval'`.

## Request size

The application refuses a request that declares a body larger than `MAX_REQUEST_BODY_BYTES`
(12 MiB by default; the largest upload a form accepts is 10 MiB) before reading it. This checks
the declared length only. Keep a body limit on the proxy or load balancer in front of the
application as well, because a request without a declared length is not covered here.

## HTTPS policy

With `DEBUG=false` the application redirects HTTP to HTTPS, marks its cookies secure and sends
`Strict-Transport-Security` for one hour. The short default limits the damage of a mistake.
Once every host that serves the deployment works over HTTPS only, raise
`SECURE_HSTS_SECONDS` (one year is `31536000`). Set `SECURE_HSTS_INCLUDE_SUBDOMAINS=true` only if
every subdomain of the host is served over HTTPS as well; browsers remember the policy for the
whole period and it cannot be withdrawn early.

## Logs

Each request writes one line to standard output (retained by the platform) from
`core/request_logging.py`, through the `vapor.request` logger: request ID, method, matched URL route pattern, status and duration.
The raw path is never logged because URL segments carry reset and confirmation tokens and record
identifiers; a request shows `route=accounts/confirm-email/<key>/`, or `<unmatched>` for paths
that match nothing. The ID is generated by the server, never taken from a client header, and is
returned in the `X-Request-ID` response header. Quote it to find related errors.

Django's own error and security records are rewritten to the same ID, status, route and exception
class. Tracebacks keep their frames but exception messages are withheld, because Django and
libraries can embed request details in them. Query strings, bodies, cookies, headers, usernames
and local variables are not logged. Frame lines show source code text, never values.

Limit: Gunicorn's access log and the development server's own request line (both log the raw path)
are off, and Gunicorn's error log stays on for worker-level problems. Messages that application code logs itself are not rewritten, so
do not log submitted values. Credentials, `DATABASE_URL`, `SECRET_KEY` and SMTP passwords belong only
in environment settings or the provider's secret store.

## Public demonstration

A public demonstration lets anyone sign in with shared accounts, so it must return to a known
state by itself. This is for a deployment that holds only the seed's synthetic records. Do not
do any of it where a repository keeps its records.

`seed_demo --announce-accounts` publishes the shared accounts on the sign-in page and stops them
changing their own credentials (see the README). `reset_demo` then erases every record and runs
the seed again:

```sh
ALLOW_DEMO_RESET=true python manage.py reset_demo --full --announce-accounts
```

Use `--full` for a public demonstration, as `render.demo.yaml` does. The plain seed holds the
four accounts, the storage structure of one tank and one freezer, and one short specimen journey:
enough to start from, too little to judge the product by. `--full` adds a believable working
repository (about 350 specimens from 70 coded participants in seven storage units, aliquot
families, check-outs in progress for up to a month, holds, disposal requests in every state,
bulk imports, corrections, every unit's equipment record with about three months of service
events, LN2 level checks, temperature readings and excursions, and distribution: studies,
consent records and a withdrawal, agreements, requests, shipments, a receipt and a return), so a visitor opens the dashboard queues, filters and box maps onto
real-looking records instead of an empty room. It is made through the same services as real
work, in 30 seconds to a minute and a half (tens of thousands of queries; the time follows the database's
latency and load). Every record is invented, and times are relative
to the reset, so the demonstration always looks like the day after the last reset. Without
`--full` a reset takes about three seconds.

It asks for confirmation unless `--noinput` is given, and refuses to run unless all of these hold:

| Check | Why |
| --- | --- |
| `ALLOW_DEMO_RESET=true` in the job's environment | The deployment opts in; a repository's deployment never sets it |
| The database's repository is the seed's "Example Biorepository", or the database is empty | A database set up with `setup_repository` is never erased |
| No migration is pending and none is unknown to the release | A job from another release than the web service would seed the wrong schema |

The erase and the seed are one transaction. Requests that arrive meanwhile wait, usually a
minute or so, then see the fresh demonstration; everyone is signed out. If the seed fails, the
transaction rolls back and nothing changes. The erase empties every application table, including
accounts, sessions, audit history, specimens, custody, equipment, distribution
history, inventory audits and deviations, and restarts identifiers such as specimen numbers. The database triggers that keep history rows from being updated or
deleted do not apply to this whole-table erase; that is deliberate and is the reason for the
checks above.

Signed-in visitors can attach files to equipment and distribution records (PDF, PNG, JPEG, TXT
or CSV, up to 10 MiB each), and other visitors can download them until the next reset. Set
`ATTACHMENT_STORAGE_LIMIT_BYTES` so the total stays bounded between resets; the public
demonstration sets 256 MiB.

On Render the schedule is `render.demo.yaml`, a Blueprint separate from `render.yaml`. The deploy
button and a repository's Blueprint never create it. In the workspace that already runs a
demonstration from `render.yaml`:

1. Deploy the release to the web service first; its pre-deploy step migrates.
2. Create a new Blueprint from the same repository and branch with **Blueprint Path**
   `render.demo.yaml`. It adds one paid cron job, `vapor-phase-demo-reset`, and refers to the
   existing web service and database by name. Render asks once for `DEMO_PASSWORD`, which will be
   public, and `DEMO_ADMIN_PASSWORD`, which must differ and stay private.
3. Use **Trigger Run** on the job to reset straight away, or wait for the schedule (daily at
   08:00 UTC). Change the time in the file's `schedule` and `DEMO_RESET_NOTE` together.

Both files set auto-deploy off, the right default for a repository: deploy the job and the web
service together for every release, or the job refuses to run until they match.

The public demonstration at <https://vapor-phase.onrender.com/> instead shows the current
`develop` branch. Its web service and reset job both track `develop` with Render's **After CI
Checks Pass** auto-deploy (`checksPass`), set on the two services (2026-11-13) rather than in the
files. Render waits for the `checks` workflow, which cancels runs superseded by a newer push, so
several pushes in a row end in one deploy of the last commit and a commit that fails its checks is
not deployed. Keep branch and trigger equal on both services. Syncing a Blueprint from
`render.yaml` onto the demonstration would turn the web service's auto-deploy off again, so keep
automatic sync off there. A deploy migrates but does not reseed: changed seed data appears at the
next reset.

The job is billed for the time it runs, with a monthly minimum;
check [Render pricing](https://render.com/pricing).

Sources, checked 2026-11-12: a Blueprint file may sit at a custom path and one repository may back
several independent Blueprints ([Render Blueprints](https://render.com/docs/infrastructure-as-code));
cron schedules use UTC, at most one run is active at a time and billing is by running time with a
monthly minimum ([cron jobs](https://render.com/docs/cronjobs)); a Blueprint may reference a
service that exists in the workspace but is not in the file
([Blueprint reference](https://render.com/docs/blueprint-spec)); `checksPass` deploys a commit
only after its CI checks pass ([deploys](https://render.com/docs/deploys)). On 2026-11-13
`render blueprints validate` (CLI 2.36.0, creates nothing) accepted `render.yaml`, planning one web
service and one database. It rejects `render.demo.yaml` in a workspace where those two do not exist
yet ("env var depends on non-existent DB" and "non-existent service"); validate it only in the
workspace that already runs `render.yaml`.
