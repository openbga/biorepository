---
date: 2026-11-15
type: added
impact: high
title: Record and review deviation reports
area: Deviations
link: quality:deviation-list
pr: 9
---
Under Deviations, staff report an issue, record the investigation, corrective action and effectiveness evidence, and submit it for review by a manager other than its creator and latest submitter, who closes or returns it. Every correction, return and reopening keeps the earlier values and reason, and optional links to an excursion or inventory audit change no specimen or hold.

## Technical notes

Adds migration `quality.0002_deviations` (two tables with history guards; additive). The review sequence is a product default, not repository policy.
