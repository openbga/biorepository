---
date: 2026-11-13
type: added
impact: high
title: First public release of OpenBGA Biorepository
---
A free, open-source biorepository management system for one repository organization per deployment. Staff record storage from sites and freezers down to box positions, specimens and aliquots, custody, holds, disposal with a separate approval, labels, CSV import, equipment records with temperature excursions, and distribution from consent and requests through shipment, receipt and return.

## Technical notes

Django and PostgreSQL with Bootstrap and HTMX, licensed AGPL-3.0-only. Run `migrate` before the first start; `seed_demo` loads synthetic demonstration records and must stay away from a real repository database. This software supports a repository's own records and does not certify compliance or accreditation.
