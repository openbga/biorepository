---
date: 2026-11-15
type: fixed
impact: low
title: Demonstration data no longer fails to load after a daylight-saving change
pr: 10
---
Loading the full demonstration data near midnight after an autumn clock change could stop with an import error, because two temperature readings fell on the same local morning. Each demonstration reading now falls on its own local date.

## Technical notes

Affects only `seed_demo` and the demonstration reset; no migration and no change to real records.
