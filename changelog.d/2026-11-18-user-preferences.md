---
date: 2026-11-18
type: changed
impact: medium
title: Lists remember your columns, rows per page and sort on any workstation
---
List column layouts, rows per page and sort are remembered for each user on the server, so they follow you to any workstation and the next person at a shared one starts from the defaults. On narrower screens, less important columns start hidden; Columns brings them back.

## Technical notes

Migration `core` 0004 adds the table `core_userpreference` (additive).
