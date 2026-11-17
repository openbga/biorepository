---
date: 2026-11-14
type: added
impact: medium
title: Load scanner CSV observations into an inventory audit
area: Inventory audits
link: quality:audit-list
pr: 5
---
An open inventory audit can now load a box's observations from a scanner CSV file. The grid fills only after the whole file passes validation, and you review and edit it before completing the audit; a rejected file keeps what you had entered.

## Technical notes

No migration. The file uses the project's normalized UTF-8 CSV format, one box at a time, with every position listed; a scanner's own export format needs converting first. The file itself is not stored.
