# OpenBGA Biorepository: product scope

OpenBGA Biorepository is a free, open-source biorepository management system: where every specimen is, what
it is, who handled it and what happened to it, from registration through storage, custody,
disposal and distribution to another institution. This document says what the product must do,
why, and in what order. It does not prescribe architecture; [workflows](workflows.md) describes
what is built in detail. Domain claims cite public sources, listed under Evidence and limits so
they stay checkable after cloning. Researched 2026-11-12.

## Working assumptions

The product is built to the assumptions below. None is a rule of any standard; each is a choice a
repository should check against its own policy before it holds real records. Items marked
"expensive to change" shape the data model.

| Assumption | Consequence |
|---|---|
| One repository organization per deployment | No tenant layer. A site level exists in the storage tree; permissions are not site-scoped |
| Human specimens first; nothing in the model prevents animal or other material | Specimen types, containers and units are catalogues the repository manages, not code |
| No donor identifiers are stored. A participant is a coded ID with no name, contact, record number or birth date (expensive to change) | No identity tables. Free-text fields warn against identifying text. Re-identification, if any, happens outside the system |
| Collection and processing times are stored exactly, with time zone | Suits synthetic data and evaluation. Whether real dates must be coarsened or shifted is the repository's data-governance decision; changing precision is a schema change |
| Staff-only access | No requester or investigator accounts and no billing. Requests are entered and decided by staff; nothing is emailed |
| No regulatory claim | See "What this product claims". Electronic signatures are not implemented |
| Roles: administrator, repository manager, technician, viewer (expensive to change) | Admin implies the other three. Few sources describe biorepository roles, so the list is a starting point |
| Viewers read everything staff can read, export CSV and print labels, including consent records and agreement documents; they change nothing | A repository that treats agreements as confidential would narrow this |
| Disposal approval is a permission granted per person (`accounts.approve_disposal`), held with an operating role; nobody approves their own request; no role, admin status or superuser status implies it | Second-person approval is checked on the server, under the specimen's lock |
| Request decisions are a permission granted per person (`accounts.approve_distribution`), held with an operating role; nobody decides a request they created or submitted | The decision records the committee's own reference and any conflict of interest it noted; the software does not judge merit |
| Thresholds belong to the repository | No limit on time out of storage, thaw count, hold age or time away. Temperature limits, LN2 low levels and service due dates exist only where the repository enters them |

### Defaults the workflows are built to

These defaults fill gaps the sources leave to the repository. They are visible in the
application's page help and in [workflows](workflows.md).

| Area | Default |
|---|---|
| Specimen identifier | `VP-000001` style from a database sequence; never reused, gaps are normal |
| Processing times | Optional. Processing cannot start before collection, cannot finish before it started, and a completion needs a start; no time may be in the future, and registration cannot precede collection or the completion of processing |
| Thaw history | Recorded when observed, never inferred from a check-out; a history unknown at registration stays marked unknown |
| Holds | Three scopes: disposal (blocks disposal only), use (blocks use and dispatch, not disposal) and all-use (blocks both) |
| Check-in | Into any free position; a checked-out specimen does not keep its slot reserved |
| Consent scope | Categories the repository defines as data (the demonstration uses general research, genetic research including whole genome sequencing, commercial use, and sharing outside the repository). Each consent record answers permitted or refused per category the form asks; a category the form does not ask permits nothing. Free-text restrictions are shown to reviewers and pickers, never parsed |
| Which consent governs | The participant's current record for the study governs all their material in it, including material collected under an earlier form |
| Consent basis per study | Either individual consent records, or a recorded study-level determination (for example a waiver) that covers material without a record. A recorded refusal or withdrawal blocks in both cases |
| Material without a consent basis | No participant, no collection, or a collection no study governs: cannot be requested, allocated or sent. There is no override; a wrong record is corrected with a reason |
| Who records consent and withdrawal | Administrator and repository manager |
| Withdrawal escalation | A manager can append a destruction instruction to an active no-further-use withdrawal without reinstating consent or releasing holds. Disposal still requires ordinary requests and separate approval; nothing is disposed of, recalled or contacted automatically. Whoever recorded the withdrawal, the escalation or the new consent cannot reinstate it; any new consent used for reinstatement must be recorded after the escalation |
| Withdrawal kinds | No further contact; no further use; no further use and destroy. The last two place a use hold on the participant's specimens in custody and release their allocations; destroy offers ordinary disposal requests, each approved by another person. Nothing is deleted or anonymized, and material already distributed is listed for recorded follow-up, not recalled |
| Agreements | Every shipment needs a valid executed agreement (MTA or DUA) with the request's institution. Valid from its effective date through the day before it expires or is terminated; dates are shown and nothing ends automatically |
| Allocation | Whole containers; send part of a specimen by deriving an aliquot first. Allocation reserves the specimen, not its position. Remaining stock is shown, not gated |
| Shipping declarations | Classification, refrigerant, dry-ice weight, emergency contact and the packer's training confirmation are recorded as the shipper declares them; nothing classifies a shipment or blocks on them. A second person at packing is optional and must differ from the packer |
| Manifest contents | Accession, type, container, quantity and unit, study, frozen at dispatch. Participant codes only when chosen for the shipment and the agreement's data level is coded or limited; collection dates never |
| Returns from a shipment | Accepted only from the original shipment; the specimen comes back under a use hold until a manager reviews it and releases or keeps the hold |
| Temperature reading assessment | Compare the exact reading with the lower and upper bounds saved with it. Equality is within the recorded limits. When only one bound exists, use that bound. When both are absent, show “No limits recorded” in the register, equipment record, excursion and CSV. Staff can filter readings by this assessment |
| Excursion review | A technician, manager or administrator opens and submits an excursion; a manager or administrator other than the person who submitted it closes or returns it |
| Holds from excursions | Operators explicitly select up to 200 affected specimens and a disposal, use or all-use hold, with a reason. The affected list is computed while open or awaiting review and frozen at closure. Holds keep the excursion reference in their reason and are released individually by a manager from the specimen record. Closing or reopening an excursion never places or releases holds automatically. This is a working product default |
| Inventory audits | One active box of at most 400 positions per audit. Operators submit all observations together; each position is explicitly empty or has an observed identifier. Unknown identifiers remain discrepancies. Completion requires an explanation for discrepancies and an unchanged storage snapshot. Audits never change inventory; corrections use a new audit linked to the previous one. These are working defaults, not repository policy |
| Scanner CSV observations | UTF-8 CSV (optional BOM), at most 128 KiB, with `box,position,state,accession` columns and exactly one row per snapshotted position. Each row names the same box and an exact position label; state is `empty` or `accession`. Load for review before completing the audit. No device-specific formats or scanner qualification are implied. The upload is not retained; only the reviewed final observations are saved. This interchange format is a working product default |
| Deviations | Operators report and update an open deviation, with optional excursion and inventory-audit links. Submission requires investigation, corrective action and effectiveness narratives; explain an unknown cause or why action is unnecessary. A manager other than the creator and latest submitter closes or returns the report with a reason; managers may reopen closed reports. Every change retains prior values and attribution. Text-only evidence, no automatic holds, suitability decisions, deadlines or notifications. These are working product defaults |
| Training | Managers and administrators assign training to active staff accounts and record completion, trainer/provider and evidence. Optional competency assessments and renewal dates are staff declarations. All staff read the register. Corrections and cancellation require a reason and retain earlier evidence; a new assignment records renewal. No fixed curriculum, renewal interval, qualification claim, independent approval or permission gate. These are working product defaults |
| Labels | Code 128, one specimen ID per label, nothing else printed. Stock 50.8 × 25.4 mm unless the deployment sets `SPECIMEN_LABEL_STOCK`; not yet read with a physical scanner |
| Operational bounds | 1,000 rows and 2 MiB per import, 200 labels per print, 10,000 rows per export, 1 to 50 children per split, 200 specimens per allocation, pick or shipment, 10 MiB per attachment. Product limits, not standards |

## What this product claims

OpenBGA Biorepository supports a repository's own records and quality system. It does not make a repository
compliant, accredited or certified, and it does not classify shipments, decide consent or judge a
request's merit. Do not write UI copy, README text or reports that claim compliance. Say
"supports" and name the record or control.

The audit trail records who, when, the old and new value and why, as 21 CFR 11.10(e) describes for
audit trails; no conformance with 21 CFR Part 11, HIPAA, CAP or ISO 20387 is claimed. Restricted
standards (ISO 20387, CAP checklists, IATA Dangerous Goods Regulations) are not reproduced here or
in code.

## The journey

The synthetic demonstration (`seed_demo --full`) walks the journey below. Staff can
create inventory audits for its seeded boxes.

```
storage unit (LN2 vapor-phase tank or freezer) -> racks -> boxes with a grid -> positions
-> specimen registered (type, container, amount, collection time, coded participant)
-> label printed -> stored in a position -> checked out (time out of storage runs)
-> split into aliquots, each stored -> parent checked in, observed thaw recorded
-> box moved to another rack -> specimen disposed with second-person approval
-> study, consent record -> request decided by a second person -> allocation, pick list
-> shipment packed and dispatched with a frozen manifest -> receipt recorded -> return reviewed
-> every step visible in custody history and the audit trail
```

Acceptance outcomes:

1. A manager defines storage: site, room, unit (with storage class, technology and nominal
   temperature), racks, towers, canisters or shelves, boxes, and positions from a container type
   with a grid and a numbering scheme. A manager can save an active layout as a template and apply it to a new unit.
   Within the template size limits, all four levels, top-level slot numbers and box grid versions
   carry over. The manager can review and edit the unique code suffixes before saving.
   Staff can open the complete register of active and retired units from their storage class,
   including units beyond the first 100 shown on the class record. Staff can review the full
   change history for sites, rooms, units, racks and shelves, boxes, storage classes, container
   types and templates. The first History page shows the latest changes. Each page lists up to
   50 entries in chronological order. Staff can follow older pages without losing their place
   in Moves on a box or rack record.
2. Occupancy is visible per unit, rack and box. From a storage list or a location record, staff
   can select a positive Free count to see available positions in that room, unit, rack, shelf or
   box. Selecting a position opens its cell in the box grid. A retired
   box retains its grid and recorded block reasons. Every position is shown as unavailable.
   The Racks and shelves register shows the full path within the unit to each listed location. Boxes and
   Free positions show the path to the rack or shelf holding each box. These paths match the
   CSV exports. Sorting Boxes by container type orders the rows by grid code and numeric
   version across pages and in the CSV.
3. A technician registers a specimen. The system assigns an opaque ID. Type, container, amount
   with unit, collection and processing times, and an optional coded participant and collection
   are recorded.
4. Labels print as a PDF with a barcode; scanning or typing an ID in the top bar opens the record.
5. A specimen is placed in a free position. Two people cannot store into the same position.
6. A checked-out specimen is split into aliquots or derivatives. Each child is a specimen with a
   parent link and its own ID, amount and position. The parent's amount is reduced by a ledger
   entry, not edited.
7. Check-out and check-in are recorded events with who, when and why. Time out of storage is
   computed, never typed. A thaw is recorded when it is observed; an unknown thaw history stays
   marked unknown.
8. A box or rack moves with all its contents in one recorded event. Staff can page through
   all moves recorded for that container, with the original route, item count, actor and reason.
9. Disposal records reason, method, who and when, needs approval by a different person holding
   that permission, frees the position and keeps the electronic history. A hold blocks disposal.
   Staff can download the seven disposal request register columns across all matching pages in
   the selected order, including requests they made or those awaiting their approval.
10. Specimens import in bulk from CSV with a validation pass before anything is committed.
    Administrators, managers and technicians can download the six import register columns for
    all files matching their search, in the selected order. Each validation has its own row.
    From a committed import, they can preview and download labels for every registered specimen
    in file order, in groups of up to 200 per PDF.
11. Lists filter, sort, paginate and export to CSV. Storage, specimen and distribution lists and
    catalogues show Export CSV for valid filters with zero to 10,000 matches. Larger selections
    show the row limit so staff can refine their filters, and invalid filters show errors. The
    export uses the matching rows across all pages at download time in the selected order. Each
    successful export records the number of data rows in the file, including zero. If a storage
    or distribution selection grows past the limit before download, staff return to the originating
    list with their filters and sort order so they can narrow the selection and retry.
12. Every change to a specimen, position, status or storage definition is traceable: who, when,
    old value, new value, reason. Controlled fields require a reason to change.
13. Sign-in, optional two-factor authentication, roles, announcements on the sign-in page,
    in-application page help, light and dark themes and the audit trail work as they do in
    [OpenBGA Environmental LIMS](https://github.com/openbga/environmental-lims), whose platform layer OpenBGA Biorepository shares.
14. Equipment (tanks, freezers, monitors, supply dewars) is tied to its storage unit, with service
    events and the due dates the repository enters, an LN2 level and fill log, temperature
    readings entered or imported, and excursions reviewed by a second person that capture the
    specimens in the unit when closed. Operators can place manual holds on selected affected
    specimens, with an attributed reason and excursion history. All staff can export the seven
    temperature-reading import register columns across pages for the selected file name search,
    validation state and sort.
15. A study governs collections; consent records and withdrawals are recorded per coded participant
    and study; a request is decided by a second person; allocation, picking, packing and dispatch
    each pass one gate that quotes the recorded fact behind any refusal; the manifest is frozen at
    dispatch; receipt and returns are recorded, and returned material is held for review.
16. Staff can audit a box against a frozen expected inventory, record observed contents and
    discrepancies, and retain completed or cancelled audits without changing custody or stock.
    Observations can be entered manually or loaded from the normalized scanner CSV format for
    one box, then reviewed before completion. Staff can export the six register columns for
    all matching audits, including each frozen box code and full creation reason.
17. Staff can report a deviation, record investigation, corrective action and effectiveness
    evidence, and submit it for a separate manager's review. Corrections, returns and reopening
    keep the earlier record and reason without changing inventory or holds. Staff can export the
    six deviation register columns for a search and status in the selected sort order. The
    individual report holds the investigation, review and history.
18. Managers assign staff training with an optional deadline, record completion and assessment
    evidence, and retain corrections and cancellation. Staff can find assignments and renewal
    dates that need attention and export the filtered register in its displayed order. The CSV
    includes register columns only. Completion evidence, assessment and history remain on the
    individual record. Training does not change operational permissions.

The demonstration uses clearly fake names such as "Example Biorepository" and participant codes
like "P-000123".

## Domain rules that must be right

These are the rules where a plausible but wrong implementation misleads a repository. "Project
rule" means no source read states it; it is a design choice.

| Topic | Rule | Basis |
|---|---|---|
| Identifier | Specimen ID is system-assigned, unique, and carries no location, participant, study or date meaning; each aliquot or derivative gets its own ID and records its parent | NCI C.6.1.2, C.6.2.1, C.6.2.2; ISBER I3.2 |
| Labels | Human- and machine-readable, and lead back to the inventory record | NCI C.6.2.2 |
| Location | Location is a hierarchy down to the position and is updated when the event is recorded. Whole boxes and racks move as one event | NCI C.6.2.4; ISBER I3.1 |
| Audit trail | Computer-generated, time-stamped; records create, modify and delete with who, when, old and new value and why; a change never obscures the earlier entry; kept as long as the record | 21 CFR 11.10(e); NCI C.6.2.6; ISBER I3.6 |
| Occupancy | One specimen per position, enforced by the database and rechecked under lock | Project rule |
| Vapor phase | LN2 vapor-phase storage is described as at or below −150 °C, warmer with distance from the liquid; the top of a vessel should stay below −140 °C and the gradient should be measured. Storage class and nominal temperature are recorded as stated and never used to refuse a record | ISBER 5th ed. (LN2 vapor-phase passages); IARC TP44 glossary; NCI C.2.8.2 |
| Quantities | Exact decimal with an explicit unit; the amount is a ledger of additions and removals; no implied conversion between units | Project rule |
| Time | Events store when they occurred and when they were recorded; future times and times before the item's latest event are rejected | Project rule |
| Retrieval and thaws | Record the date and time of retrieval; record thaw and refreeze episodes and storage deviations | NCI C.2.8.1, C.2.9; ISBER J3.8 |
| Disposal | Never deletes the specimen or its history; approval is a separate person's decision and the act is recorded separately | Project rule, following the audit-trail rule above |
| Consent tracking | Consent, or the reason it was not required, is tracked for each specimen and is retrievable when specimens are retrieved; the system can tell when a participant revoked consent | NCI B.5.1, B.5.4.1, B.8.1.3 |
| Refusal | A person who was asked for broad consent and refused cannot have that refusal waived for storage or secondary research use; a recorded refusal therefore blocks even under a study-level determination | 45 CFR 46.116(e)(1), (f)(1) |
| Withdrawal | Options include no further contact and no further use (remainder disposed); the system records the withdrawal and supports disposal. Analysis of material already distributed is generally not prohibited, so the product lists it for follow-up and recalls nothing | ISBER C2.2.5; NCI B.5.7 |
| Request review | Requests get scientific and/or administrative review for consistency with consent, ethics approval, agreements and availability; conflicts of interest are documented. Neither source states a self-approval rule; nobody deciding their own request is a project rule | ISBER K2.2; NCI B.8, C.1.2.2 |
| Agreement before shipment | A material transfer agreement and a requisition from inventory exist before any shipment | NCI C.2.10.2.1; ISBER K3 |
| Shipment classification | Category A or B assignment rests on the source's medical history or professional judgment, so the software records the shipper's declaration and never classifies. The emergency contact, dry-ice net mass and hazmat training are recorded as declared | 49 CFR 173.134(a)(1), 173.199(a)(7), 173.217, 172.704 |
| Manifest and verification | A manifest lists specimen IDs, types, amounts with units and the study; the recipient verifies against it and reports discrepancies, condition and logger output | ISBER L4.1, L4.5; NCI C.2.10.2.2 |
| Dates on manifests | All date elements except year are identifiers under HIPAA Safe Harbor, and a limited data set (which keeps dates) needs a data use agreement, so collection dates stay off the manifest | 45 CFR 164.514(b)(2)(i), (e) |
| Retrieval of requested material | Confirm every requisitioned specimen was retrieved and document missing ones (recorded as "not found") | ISBER J7.2 |
| Returns | Restocking returned, unused specimens is not recommended but must be tracked | NCI C.6.2.3 |
| Depletion | Depleting remaining stock "may require" approval; the product shows stock and does not gate it | ISBER J7.2; NCI B.8.1.4 |
| Excursions | Storage deviations and out-of-range events are recorded, documented and available for reporting. No source read requires a second person to sign off an excursion; supervisory or QA review of deviations is the closest, so the second-person close is a project default | NCI C.2.8.1, C.2.8.2, C.3.3; ISBER J3.5 |
| LN2 log | LN2 level, consumption and storage temperature are critical parameters; record the level before and after a manual fill; consumption is derived from the levels | ISBER H2.2 |
| Maintenance and calibration | Maintenance records carry description, date and who did it; calibration records name the reference device; intervals are set by the repository, so the product enforces none | ISBER H12.1, D3.6.2.2 |
| Training | Retain staff training and competency assessment evidence; the repository defines its curriculum, assessment and schedule | NCI C.1.2 and C.3.2 recommend periodic training, assessment documentation and training records; this workflow is a project choice |
| Deviations | Retain deviation and corrective-action records as part of the repository's quality system. The software records staff assessments; its second-person review and required narratives are product choices | NCI C.3.1–C.3.2 recommends QMS corrective actions and deviation/CAPA records; it does not prescribe this workflow |

### Why this shapes the design

- **Never overwrite.** Corrections add a record with a reason. History is the product's main trust
  feature, and the database refuses updates and deletes on history tables.
- **Records, not judgments.** Consent, classification, merit and specimen quality are decided by
  people and committees. The product records what they decided and refuses steps the records do
  not support.
- **Second person on irreversible steps.** Disposal approval, request decisions, excursion close
  and consent reinstatement are made by someone other than the person who started them, checked on
  the server.
- **Time is computed.** Time out of storage, time away and thaw counts come from recorded events,
  so they cannot be typed in or drift.

## Milestones

| Milestone | Scope | Status |
|---|---|---|
| 0. Platform | Sign-in with optional two-factor authentication, announcements, four staff roles, shell and themes, audit trail, page help, tests, CI, container and Render Blueprint, presentation page | Built |
| 1. Storage | Storage tree, container types and grids, templates, occupancy views, free-position search, blocked positions, box and rack moves | Built |
| 2. Specimens | Registration, aliquots and derivatives, placement, check-out and check-in, freeze-thaw, holds, disposal with approval, custody history | Built |
| 3. Throughput | Labels, scan-to-lookup, CSV import, search, exports, full synthetic demo, browser journey | Built |
| 4. Governance and distribution | Studies and collections, consent records and withdrawal with escalation to destruction while retaining holds, requests with approval, allocation and pick lists, shipments and manifests, receipt, returns, agreements | Built |
| 5. Equipment and quality | Equipment records, service events and due dates, LN2 level and fill log, temperature readings, reviewed excursions and explicitly selected manual holds from excursions; box inventory audits with manual entry and normalized scanner CSV import; deviations with investigation, corrective action and separate manager review; staff training assignments, completion and assessment evidence, explicit due dates and retained corrections | Built |
| Later | Monitoring ingest and alarms, requester accounts, billing, site-scoped permissions | Later, each when a repository needs it |
| Exploring | Standards exports (SPREC, MIABIS), electronic signatures, spatial storage maps | Not committed |

No further milestone is currently committed. Later and exploring items need a demonstrated
repository need or the dependencies described below.

The public [roadmap](../website/about/roadmap/index.html) carries the same order without dates.
Change it in the same increment when an area ships or the order changes.

### Future research: spatial storage maps

Explore a visual way to find stored material through buildings, floors and rooms, with cabinets,
freezers and tanks placed in their physical layout. Drill down to the existing internal storage
structure, boxes and positions, and locate a specimen in that context. This is an eventual
research direction, not a prioritized milestone or a delivery-date commitment.

Research how spatial layouts should relate to the existing storage records before choosing a
data model or interaction design. The current model has separate sites, rooms and storage units;
internal shelves, racks, towers and canisters use a typed hierarchy, followed by boxes and
positions. Buildings, floors and spatial coordinates are not implemented. A future map should
complement, not replace, the existing inventory, authorization and custody-history controls.

Research notes (2026-11-16): [ISBER Best Practices, fifth edition, I3.1, p. 133](https://cdn.ymaws.com/www.isber.org/resource/resmgr/best_practices/isberbestpracticeseng-5thedi.pdf)
recommends unambiguous physical storage locations, including building context, and retained
movement history. It does not prescribe separate building/floor tables or a map interface.

- **Working preference to evaluate:** optional structured buildings and floors above rooms, when
  multiple buildings or repeated room names make them useful. Keep simple single-building sites
  usable without invented floor/building records. Treat map placement as optional presentation
  data, separate from the authoritative storage and specimen relationships.
- **Alternatives:** room-level building/floor labels are a smaller initial change but weaker for
  consistent naming and shared floor plans. A general typed location tree accommodates irregular
  facilities but adds parent-type, cycle, same-site and move validation. Compare these against
  explicit building/floor records before committing to a schema; do not generalize the whole
  specimen-storage model merely to draw a map.
- **Migration questions:** preserve existing room, unit, box, position and specimen IDs and all
  historical events; introduce optional associations before verified backfill. Do not infer a
  building or floor from a room code. Review room-code uniqueness (currently per site),
  cross-site consistency, imports, exports, breadcrumbs and existing links. Distinguish correcting
  a map or room label from a real physical move, which must retain its reason and attribution.
  Plan compatibility and rollback before making associations required.

### Decisions a repository makes before real use

The product leaves these to the repository that runs it: whether real collection dates are
stored exactly; which roles exist and who may approve disposal and decide requests; whether
viewers may read agreements; any time-out-of-storage or thaw limits (recorded outside the product
today); label stock, adhesion at storage temperature and scanner verification; hosting, backup and
recovery ownership; MFA enforcement and account recovery; and its own validation of the system
for its intended use.

## Project decisions

| Date | Decision | Status |
|---|---|---|
| 2026-11-12 | Build OpenBGA Biorepository on the OpenBGA Environmental LIMS platform and pattern, sharing its platform layer so fixes can move between the two | Confirmed |
| 2026-11-12 | License: AGPL-3.0-only, as OpenBGA Environmental LIMS, with the same LICENSE, NOTICE and footer "Source code" arrangement | Confirmed |
| 2026-11-12 | Coded participants only; specimen IDs in `VP-000001` style; disposal approval is a permission granted per person | Working default |
| 2026-11-12 | Distribution defaults: structured consent scope categories, current record governs, the three withdrawal kinds, request decisions by a per-person permission, an agreement for every shipment, the manifest contents above, returns held for review, no override of a blocked step | Working default |
| 2026-11-12 | Equipment defaults: the repository enters every limit, low level and due date; second-person excursion close | Working default |
| 2026-11-15 | Deviation defaults: open, awaiting review and closed states; investigation/action/effectiveness narratives before submission; review by a manager other than the creator and latest submitter; retained history on return and reopening | Working default |
| 2026-11-15 | Training defaults: manager-managed assignments and evidence, optional competency assessment and dates, staff read access, retained corrections/cancellation; no automatic qualification or access gates | Working default |
| 2026-11-17 | Withdrawal escalation: retain an active no-use withdrawal and its holds while appending a destruction instruction; ordinary disposal approval remains separate; reinstatement excludes both instruction recorders | Working default |

## Evidence and limits

- Rechecked NCI B.5.7, pp. 16–17, directly on 2026-11-17 for withdrawal escalation. This
  voluntary guidance recommends documenting discontinuation actions, stopping further
  distribution when withdrawal applies to stored material, and respecting requests to destroy
  unused specimens whenever possible. It leaves the repository's response procedures to the
  repository; the append-only escalation and separate-person reinstatement checks are product
  choices. See the January 2026 primary source below.

- Rechecked NCI C.1.2 and C.3.2 directly on 2026-11-15 for training records. These recommend
  periodic training and documentation of training and competency assessments. Assignment states,
  manager access and optional renewal dates are product choices, not mandatory intervals.
- Rechecked NCI C.3.1–C.3.2 directly on 2026-11-15 for the deviation register. These are voluntary
  quality-management recommendations; the review sequence and required fields above are product choices.
- Read directly on 2026-11-12: NCI Best Practices for Biospecimen Resources, 4th edition (January
  2026), the sections cited above; ISBER Best Practices: Recommendations for Repositories, 5th
  edition (2023), the sections cited above; IARC Technical Publication No. 44 (2017), glossary and
  section 3.3; and the federal rules below in the eCFR (text as of 2026-11-10).
- The ISBER text was read in a full-text copy, not from ISBER's own site; obtain it from ISBER.
- NCI and ISBER are voluntary guidance. Their section numbers are cited so a reader can check the
  wording; the product's defaults are choices consistent with them, not requirements they impose.
- Not read: IATA Dangerous Goods Regulations (paywalled), PHMSA guidance pages, current CAP
  biorepository checklist items, ISO 20387, CLSI documents, HHS's interpretation of HIPAA
  "reliance" after revocation, state law, FDA 21 CFR 50 and 56, NIH data-sharing policy and the
  GDPR. Nothing here should be read as reflecting them. Regulatory statements are US federal.
- Not legal advice. A repository confirms its own obligations with its institution.

### Primary sources

| Claim area | Source |
|---|---|
| Identifiers, labels, lineage, location, audit trail, consent tracking, retrieval, shipping, returns | [NCI Best Practices for Biospecimen Resources, 4th ed., January 2026](https://dctd.cancer.gov/data-tools-biospecimens/biospecimens-biobanks/resources/best-practices/biospecimen-resources/appendices/2026-4th-edition-best-practices.pdf) ([landing page](https://dctd.cancer.gov/data-tools-biospecimens/biospecimens-biobanks/resources/best-practices/biospecimen-resources)) |
| Inventory system, withdrawal options, access review, agreements, manifests, retrieval, LN2 and equipment records | ISBER Best Practices: Recommendations for Repositories, 5th ed. (2023), International Society for Biological and Environmental Repositories |
| Vapor-phase temperature range | [IARC Technical Publication No. 44 (2017)](https://publications.iarc.who.int/Book-And-Report-Series/Iarc-Technical-Publications/Common-Minimum-Technical-Standards-And-Protocols-For-Biobanks-Dedicated-To-Cancer-Research-2017) |
| Audit trails | [21 CFR 11.10](https://www.ecfr.gov/current/title-21/chapter-I/subchapter-A/part-11) |
| Broad consent and refusal | [45 CFR 46.116](https://www.ecfr.gov/current/title-45/part-46/section-46.116) |
| De-identification, limited data sets, data use agreements | [45 CFR 164.514](https://www.ecfr.gov/current/title-45/part-164/section-164.514) |
| Shipment classification, Category B packaging, dry ice | [49 CFR 173.134](https://www.ecfr.gov/current/title-49/part-173/section-173.134), [173.199](https://www.ecfr.gov/current/title-49/part-173/section-173.199), [173.217](https://www.ecfr.gov/current/title-49/part-173/section-173.217) |
| Hazmat employee training | [49 CFR 172.704](https://www.ecfr.gov/current/title-49/part-172/section-172.704) |
