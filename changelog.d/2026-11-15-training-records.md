---
date: 2026-11-15
type: added
impact: high
title: Keep staff training records with completion evidence
area: Training records
link: quality:training-list
pr: 12
---
Under Training records, managers assign a training topic with an optional procedure reference and due date, record completion with the trainer and evidence, and correct or cancel an assignment with a reason that stays in the history. Staff can search and filter assignments and renewal dates; a record grants no permissions.

## Technical notes

Adds migration `quality.0003_training` (additive; protects event history and prevents deleting records). No dependency changes.
