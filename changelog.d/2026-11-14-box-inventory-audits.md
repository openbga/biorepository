---
date: 2026-11-14
type: added
impact: high
title: Audit a box against a saved inventory snapshot
area: Inventory audits
link: quality:audit-list
pr: 3
---
Under Inventory audits, staff compare a box's physical contents with a frozen snapshot and record matches, missing specimens, unexpected identifiers and mismatches. Completing or cancelling an audit keeps an attributed record that cannot change, and an audit never moves specimens or changes their status or holds.

## Technical notes

Adds migration `quality.0001_initial` (two tables protected against update and delete). An audit covers one box of up to 400 positions; a correction starts a new linked audit. Viewers can read audits.
