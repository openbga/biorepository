# UI before/after comparison

Full-page screenshots of every page for each demo role, at 1280 and 1440 pixels wide (768 for
the pages the navigation links), in light and dark, for pull-request review. Forms that open in
the dialog are captured open, from the button that opens them.

The crawl starts at the home page signed in as each role, follows the navigation and the record
pages its lists link to (one record per route shape), and records each dialog opener. The "after"
run replays the URLs the "before" run found, so both sides show the same records.

## Run

From the repository root, with demo data loaded (see CONTRIBUTING.md):

```sh
set -a; . ./.env; set +a          # DATABASE_URL, DEMO_PASSWORD, …
uv run --group e2e python scripts/ui_compare/capture.py before --ref develop
uv run --group e2e python scripts/ui_compare/capture.py after
open .local/shots/index.html
```

- `before --ref REF` exports `REF` with `git archive` into `.local/ui-compare/before-tree/`,
  serves it on 127.0.0.1:8027 against the same database, captures it and stops the server.
  Without `--ref`, `before` captures the server at `UI_COMPARE_BASE`.
- `after` captures `UI_COMPARE_BASE` (default `http://127.0.0.1:8020`, the development server).
  `--recrawl` crawls again instead of replaying the before URLs (new pages appear only then).
- `--roles demo_admin,demo_viewer` limits the roles; `--workers N` sets processes per role.

The :8027 server gets a scrubbed environment (PATH, HOME and the `.env` settings without the
demo passwords), since a DEBUG error page prints the process environment. A page or dialog that
answers with a server error (5xx, including a fragment it loads) is never screenshotted: the run
records only its URL and status as a gap.

Images go to the ignored `.local/shots/<side>/<role>/<page-key>--<width>-<theme>.png`, with a
`manifest.json` per side listing the pages, dialogs and any capture gaps (the command exits
non-zero when there are gaps). The viewer, `.local/shots/index.html`, pairs before and after by
role, page key, width and theme; rebuild it with `python3 scripts/ui_compare/build.py`.
