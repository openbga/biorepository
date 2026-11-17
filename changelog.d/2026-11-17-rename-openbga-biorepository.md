---
date: 2026-11-17
type: changed
impact: high
title: The application is now called OpenBGA Biorepository
pr: 15
---
The product is now named OpenBGA Biorepository. The new name appears in the top bar, page titles, sign-in, error pages, footer and page help; on phones the top bar wraps the name onto two lines.

## Technical notes

No migration and no change to records. The default `SOURCE_CODE_URL` now points to `https://github.com/openbga/biorepository`; a deployment that sets it should check it points to its published source. Hosting service, database and logger names are unchanged and need no action.
