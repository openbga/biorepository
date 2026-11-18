## What changed and why

<!-- One coherent change. Link the issue if there is one. -->

## How it was verified

<!-- Commands you ran and what you checked by hand (journeys, roles, refused paths, light and dark). -->

## Before / after

<!-- Visible changes: before (develop) and after (this branch) screenshots of synthetic data, light and
dark, from scripts/ui_compare/capture.py. Delete this section when nothing visible changed. -->

## Checks

- [ ] This pull request targets `develop`
- [ ] Ran the `project-code-review` skill (or its checks by hand) and fixed what it found
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] `uv run python manage.py check`, `makemigrations --check --dry-run` and `changelog --check` pass
- [ ] `REQUIRE_WEASYPRINT=1 uv run pytest` passes on PostgreSQL (`collectstatic` run first), and the affected browser journeys pass
- [ ] Tests added or updated for changed behaviour, including refused, stale or locked paths and role boundaries where relevant
- [ ] Every read and mutation is authorized on the server; nobody approves their own disposal or decides their own specimen request
- [ ] Events are append-only: corrections add a record with a reason, and no specimen is deleted
- [ ] Page help under `docs/pages/` updated for any changed page, field, action, permission or rule
- [ ] An entry in `changelog.d/` and a regenerated `CHANGELOG.md` for any change users, deployers or integrators can observe (otherwise say "No changelog entry: nothing observable" above)
- [ ] Synthetic data only; no secrets, real donor or participant data, or compliance claims
- [ ] Migrations, if any, are backward compatible and no merged migration was edited

## Notes for reviewers

<!-- Anything that needs a decision, a migration, a dependency change or a follow-up. Flag changes to
authorization, custody or audit history, position occupancy, disposal or distribution gates, and any
new wording on a label, pick list or manifest. -->
