# Workflow rules and limits

What the application does today, and the limits a repository should know before relying on it.
It covers the platform (sign-in, roles, announcements, the audit trail, the demonstration
tooling), storage, specimens, custody, disposal, bulk import and labels, equipment records
(service events, the LN2 log, temperature readings and excursions), and distribution (studies,
consent records and withdrawal, requests, allocation, shipments, receipt, returns and
agreements), as built. The help panel
inside the application describes every page, field and action; this is the overview.
[Product scope](product.md) explains why the rules exist. Items that product scope lists as
working assumptions or defaults are choices a repository should check against its own policy;
they are marked "default" below. Vapor Phase supports a repository's own records and quality system. It
does not make a repository compliant, accredited or certified.

## Time zones

Times are interpreted in the configured deployment `TIME_ZONE` and stored with time zone support.
Naive form timestamps and CSV times without an offset use `TIME_ZONE`; explicit UTC offsets keep
their instant. `Repository.timezone` is a recorded setup copy, not a parsing override;
`setup_repository --sync-timezone` aligns it with the setting.

## Setup, roles and the repository record

`setup_repository` creates the repository record (one per deployment), the four role groups and
the first administrator. It is safe to rerun: it creates what is missing and never resets a
password or removes a role. Explicit flags rename the repository, align the time zone or add roles
to a named existing account, and each change is audited against the named administrator.

The roles are `admin` (Administrator), `manager` (Repository manager), `technician` and `viewer`.
Admin implies the other three. The list is a default: few sources describe biorepository roles
(see product scope). Superuser status opens the Django administration site only;
it grants no role. There are no client or portal accounts.

| Work | admin | manager | technician | viewer |
| --- | :-: | :-: | :-: | :-: |
| Read storage, specimens, history, lists, exports, labels | yes | yes | yes | yes |
| Register, place, relocate, check out and in, hand over, split, record use and thaws, place holds, request and record disposal, import, move a box or rack | yes | yes | yes | no |
| Define storage (sites, units, racks, boxes, templates, grids), block positions, retire storage, manage specimen catalogues | yes | yes | no | no |
| Correct a specimen, a custody record, a thaw record or a recorded move; adjust or reverse an amount; release a hold | yes | yes | no | no |
| Register equipment, change its status, close, return or reopen an excursion | yes | yes | no | no |
| Record service events, LN2 entries and temperature readings, import readings, open, edit and submit an excursion, add notes | yes | yes | yes | no |
| Define studies, link collections, record consent and withdrawal, manage agreements, institutions and investigators, allocate specimens, release allocations, cancel, reopen or close a request, cancel a shipment, correct a receipt, review a return | yes | yes | no | no |
| Create, edit and submit a request, pick, put back, build, pack and dispatch a shipment, record notifications, receipts and returns, add notes | yes | yes | yes | no |
| Approve or reject a disposal | explicit permission, any role | | | |
| Approve, reject or return a specimen request | explicit permission and an operating role | | | |
| Announcements, record retention statement | admin (the statement also needs superuser) | no | no | no |

Every server-side read and write checks the role again; a hidden or disabled button is never the
protection.

## Disposal approval

Approving or rejecting a disposal needs the explicit permission `accounts | approve_disposal`,
which no role group carries: a superuser grants it to a named person or group in the Django
administration, and it is not implied by the admin role or by superuser status. The person must
also hold an operating role. Nobody approves their own request, whatever their permissions; the
service refuses it under the specimen's lock. See [Disposal](#disposal) for the workflow.

## Request decisions

Deciding a specimen request (approve, reject or return it for changes) needs the explicit
permission `accounts | approve_distribution`, granted per person in the Django administration like
disposal approval; no role group carries it and neither the admin role nor superuser status
substitutes for it. The person must also hold an operating role, and nobody decides a request they
created or submitted. The decision records a reason, the committee's own reference and any
conflict of interest the committee noted; the software does not judge scientific merit. A
rejection is final and, in the same step, releases any specimens still allocated to the request
(with their holds) and cancels its draft shipments. See [Distribution](#distribution).

## Audit trail and corrections

Every change to an account, group, announcement, storage definition, specimen or event writes an
attributed audit event with the values before and after and the reason, in the same transaction as
the change. Password and authenticator secret values are never recorded. History tables are
append-only: a database trigger raises an error on `UPDATE` or `DELETE`, so a correction adds a
record with a reason and the earlier value stays visible. The trigger does not stop `TRUNCATE`,
`DROP` or a database owner disabling it; see [operations](operations.md). Specimens and their
events are never deleted; the database refuses it.

## Storage

Storage is a hierarchy: **site → room → storage unit → (rack, tower, canister or shelf, up to four
levels) → box → position**. A unit has a storage class (cryogenic, ultra-low, refrigerated or
ambient by default; classes are data), a technology (LN2 vapor phase, LN2 liquid phase, mechanical
or ambient) and a nominal temperature in °C. Technology and temperature are recorded as stated.
They are never used to refuse a record: the product records where material is kept, it does not
judge whether that is cold enough.

**Defining storage.** Admin and manager create sites, rooms, units, nodes and boxes. A node
holds either other nodes (tower, canister and rack hold shelves; a shelf holds racks) or boxes.
A box takes its positions from a container type: rows, columns, a label scheme (letters and
numbers such as A1, or numbers only) and a fill order. A **template** is a versioned recipe of one
unit with its layout; applying it creates the unit, nodes, boxes and positions together or not at
all. Codes are upper-case ASCII and cannot change; names, a site's time zone and a unit's class or
temperature can, with a reason. A container type or template that already has a code gets a new
version rather than a rewrite, and a template version used to create a unit is frozen.

**Positions and occupancy.** A position holds one specimen. A database constraint enforces that,
and every placement re-checks it under a lock, so two people storing into the same position cannot
both succeed: the second is refused as occupied. A manager can **block** an empty position (for
example a cracked well) with a reason; a blocked position cannot receive a specimen and is not
counted as free. Occupancy is shown per unit, rack and box, and the free-position list finds room
by unit, rack or box.

**Moves.** A box moves to a node with box slots; a node moves, with everything under it, to the top
level of a unit or into another node. One move is one recorded event that lists every item carried
and the path before and after. Admin, manager and technician can move. Only the container's latest
move can be corrected, once, and only by admin or manager, by a new compensating move linked to the
wrong one; the wrong record stays. A move is a location update, not a check-out: material that
leaves storage is checked out.

**Retiring.** An empty box, a node with no active children or a unit with no active nodes can be
retired, with a reason and attribution. Retired storage stays on record and cannot receive anything.
Storage is retired, never deleted, in the application.

**Chronology.** Every storage and specimen event stores when it happened and when it was recorded.
The time of occurrence cannot be in the future and cannot be earlier than the latest event recorded
for that item. A container cannot be recorded as moved before the last recorded movement of anything
inside it, including items that have since left, and an item cannot be placed in a box before that
box's, or its rack's, last move.

**Concurrency.** Storage-changing operations take one repository-wide lock, always first, and
records carry a revision. A form opened before another person changed the same record is refused as
stale and nothing is saved. A box or rack move also checks a repository-wide storage revision, so
any placement or move elsewhere makes an open move form stale and it reloads. This is simple and
correct for one repository; it serializes writes.

## Specimens

**Identity.** The system assigns each specimen an ID such as `VP-000001` from a database sequence.
It is unique, never reused, and carries no meaning: no location, participant, study or date. Gaps
are normal. The ID is the only thing printed on a label.

**Catalogues.** Specimen types, container types (with an optional nominal capacity), quantity units
(case-sensitive codes such as `mL`, `uL`, `count`), participants and collections are data, managed
by admin and manager. A participant is a coded identifier such as `P-000123` and nothing else: no
name, contact, record number or birth date is stored, and free-text fields warn against typing
identifying text. A collection is a plain grouping until a study governs it (see
[Distribution](#distribution)). A catalogue entry that a specimen uses keeps the fields that would change its meaning (a unit's
code and dimension, for example).

**Registration.** Admin, manager and technician register a specimen with type, container, an exact
amount with its unit, collection time, optional processing start and completion, an optional
participant and collection, the thaw count before registration (a whole number, or unknown) and
optionally a storage position, all in one transaction. The amount is a decimal, never a binary
float; the text as supplied is kept beside it. The collection time cannot be in the future,
processing cannot start before collection or finish before it started, and the registration cannot
precede collection or the completion of processing. The registering person is the first custodian.
Registration is also how a received specimen enters the system unplaced, to wait for a position.

**Amounts.** The amount on hand is a ledger of additions and removals, never an edited number.
Entries are the initial amount, consumption (material used or lost, only while the specimen is in
hand), derivation into children, a manager's signed adjustment, a reversal and the final disposal
debit. An amount cannot go negative. A consume or adjust entry can be reversed once by a manager,
by a new entry at the current time. When the amount reaches zero the specimen's status becomes
**exhausted**; it keeps its position and is not disposed. Statuses are available, exhausted,
disposed and distributed (see Distributed below).

**Placement and check-out.** A specimen is placed in a free, active, unblocked position. Check-out
records who takes it, where it is going and why, frees its position and starts the clock. A
specimen has at most one open check-out. Check-in returns it to a free position, recorded with the
person returning it. Handing a specimen to another person without moving it is a custody transfer.
Recipients are active staff in any role. Each custody event keeps who handed over, who received and
the time it happened.

**Time out of storage** is computed from the events and never typed: the sum of closed check-out
periods plus the running one, with a disposal or a dispatch closing an open period. A returned
specimen opens a period again until it is checked in. The time between registration
and first storage is shown separately and is not counted as a check-out. No limit is applied: a
check-out that has run for weeks is shown with its age and is not flagged by any threshold,
because the product has none (thresholds are the repository's own; see product scope).

**Time away.** From a dispatch to a return, a distributed specimen is outside the repository's
custody. That time is listed on the record as time away, with the destination, and is not counted
as time out of storage.

**Freeze-thaw.** A thaw is recorded when it is observed, never inferred from leaving a freezer, the
time away or the destination. Check-in asks whether a thaw was observed: yes records one thaw, no
and not known record none, and the answer is kept on the check-in. A thaw seen at another time is
recorded on its own. The count shown is the known count before registration plus the thaws
recorded since; a specimen registered with an unknown history says so ("unknown before
registration; N recorded thaws") rather than starting at zero. Aliquots inherit their parent's
count or its unknown history. Thaws while a specimen was away are unknown unless the recipient
reported a count when it came back; an unreported period keeps the total at "at least N" and shows
the dates it covers. A child of a parent whose count is not fully known inherits every recorded
thaw and starts from an unknown baseline. A manager corrects a thaw record by a new record that points at the
earlier one (an observed thaw counts 1, or 0 if it did not happen).

**Aliquots and derivatives.** A specimen that is checked out, or has never been stored, can be split
into 1 to 50 new specimens, each with its own ID, container, amount and optionally a position, and
the parent is reduced by what was consumed, all in one transaction. Aliquots keep the parent's type
and unit and their amounts add up to the amount consumed; any process loss is a separate consumption
entry. Derivatives may have another type and unit (for example an extract measured in µL from material
measured in mL), with no conversion implied. Children take the parent's participant, collection, collection time,
custodian and thaw history and have one immutable parent. A specimen with nothing left or under a
use or all-use hold cannot be split. A manager can fill in the participant or collection of a
primary specimen registered without them, for the whole family at once, with a correction for each
member; it never overwrites a value already recorded.

**Holds.** Any operating role can place a hold; only a manager (admin or manager) releases it, with
a reason. Holds stack and each is released separately. A **disposal hold** blocks requesting,
approving and recording a disposal. A **use hold** blocks check-out, splitting, consumption,
hand-over and dispatch, and leaves disposal possible. An **all-use hold** blocks all of those and
disposal. A hold never blocks a protective relocation, a check-in or a correction, and it does not
pass to the specimen's children or parent.

A hold that carries a reference (an allocation to a request, a consent withdrawal, a return) is
placed and released by that workflow and shows "Released through its workflow"; the generic release
refuses it. A hold without a reference is a manual hold.

**Distributed.** A specimen leaves the repository's custody only through a dispatched shipment. The
dispatch is a custody event with no staff recipient and the destination, debits everything on hand
as a distribution, sets the status to distributed and closes any open check-out. While distributed
a specimen cannot be placed, relocated, checked out or in, handed over, split, consumed, adjusted,
thawed or disposed; it can still be held, corrected and annotated, and a disposal request that is
already open can still be rejected or cancelled. Its record reads "Outside the repository" with
where it was sent. The only way back is a recorded return (see [Distribution](#distribution)).

## Disposal

Disposal is a request, a decision by a different person, and a recorded act, each its own event.

1. **Request.** Any operating role requests disposal of a specimen, with a method, a destination
   and a reason. A specimen with an active disposal or all-use hold (a use hold does not count), a
   specimen already carrying an open request, or one outside the repository cannot be requested. The request is an immutable snapshot of the specimen's revision and location state.
2. **Decision.** Someone holding the approval permission, who is not the requester, approves or
   rejects, with a reason. Admin or superuser status does not substitute. A hold blocks approval.
   The requester, or a manager, can cancel a request that has no decision yet. Rejection and
   cancellation end it; an approved request is not cancelled but recorded or left out of date.
3. **Out-of-date request or approval.** A request or approval is valid only while the specimen is
   unchanged. Any change after it (a relocation, a hold, an amount change, a correction) makes it
   out of date; it can no longer be approved or recorded, the old record stays visible and a new
   request is needed.
4. **Record disposal.** An operating role records that the material was disposed of. This frees the
   position, closes out the amount, ends any open check-out and sets the status to disposed, using
   the approved method and destination. Approval is permission, not evidence that destruction
   happened; the recorded act is.

The specimen record, its history and its position history remain. A disposed specimen cannot be
returned, moved or used again; only an annotation can be added.

## Corrections

A correction adds a record with its reason and leaves the original in place. Admin and manager may:

| Correct | Rule |
| --- | --- |
| A specimen's type, container, participant, collection or collection and processing times | Needs a reason. An aliquot or derivative takes participant, collection and collection time from its parent, so those are corrected on the parent. Type, participant, collection and times cannot be corrected once a specimen has children (the lineage needs the owner's review). After disposal a correction is kept as an annotation only |
| A custody event | Destination and custodians only. Times, kinds, links and locations are never rewritten; a time or location mistake needs a new event |
| A thaw record | A new record replaces the latest version; the count is recomputed |
| A recorded movement (specimen or container) | A new compensating move linked to the wrong one |
| An amount | A signed adjustment, or a reversal of a consume or adjust entry |

## Bulk import

An operating role uploads a CSV with a fixed header (`row_key`, specimen type, container type,
amount, unit, collected, processing start and completion, participant code, collection code, box code,
position label and prior thaws). Unknown columns are errors. **Validation** creates no specimen, ID
or claim: it stores an immutable result with every row and column error and the rows as understood
(at most 1000 rows and 2 MiB), and shows the same file's earlier use as a warning. **Commit** revalidates every row
against the current catalogues and positions and registers and places all rows in one transaction,
or none. A validated batch expires after 24 hours and commits at most once; committing again returns
the first result. Only existing coded participants, collections, types and units are accepted. The
placement time is the commit time and the collection and processing times are as supplied.

## Labels, lookup and exports

**Labels** are a PDF with one label per page: the specimen ID in text and as a Code 128 barcode,
nothing else (no participant, no location). Any role prints up to 200 at a time. The stock is
50.8 × 25.4 mm by default, a working assumption the deployment sets with `SPECIMEN_LABEL_STOCK`;
barcodes have not been read with a scanner. Each print is audited with the IDs, count, stock and
template version, and makes no claim that a label was affixed.

**Lookup.** The top bar takes a typed or scanned ID (outer whitespace trimmed, matched exactly after
normalizing) and opens the record; no search by participant identity exists because none is stored.

**Lists and exports.** Specimen and storage lists search, filter, sort and paginate on the server.
The specimen, unit, rack, box, position, site and room lists and the catalogues export to CSV with
the filters on screen. Exports over 10,000 rows are refused rather than truncated, cells that could
be read as formulas are escaped, quantities are exact decimals, and each export is audited with the
selection and row count.

## Equipment

Equipment records describe the vessels and devices that hold or watch the specimens. Everything
here is a record of what the repository entered; Vapor Phase sets no limit, interval or level of
its own, and it neither monitors temperature nor raises alarms.

**Register.** Admin and manager register equipment with an asset ID, a kind (LN2 tank, freezer,
monitor, supply dewar or other), make and serial number, whether it is critical and, for a tank or
freezer, the storage unit it is the enclosure of (at most one active piece of equipment per unit).
The repository enters the temperature limits, the LN2 level unit and the low level it wants compared
against; none is defaulted. The kind and asset ID are fixed once entries exist. A status change
(in service, out of service, retired) carries the time it took effect and a reason and is
append-only. Out-of-service equipment still accepts entries; retired equipment accepts only
corrections.

**Service events.** Maintenance, calibration, repair, mapping and qualification are recorded with
the outcome, who did the work (staff or an outside provider), the reference device where one was
used, the findings as found and as left, an attachment and the next due date, which the repository
types. The due list shows the latest event per equipment and kind against that date, as overdue or
coming up; the dashboard counts both. A date is a date the repository entered, not a schedule the
product imposes.

**LN2 log.** A level check records one level; a fill records the level before and after, the amount
added, the supply dewar and the delivery reference. Levels and amounts are exact decimals with
units. The change since the previous entry is derived when shown, not stored, and a vessel whose
latest level is below the low level the repository set is flagged.

**Temperature readings.** Readings are entered one by one or imported from a CSV (checked first,
then committed with a reason; a checked file expires after 24 hours). A reading at the same time and
probe position as an existing one for the equipment is refused. Each reading keeps the limits in
force when it was recorded, so a later change of limits does not rewrite it. A reading outside
those limits that no excursion covers is listed on the dashboard until an excursion is opened for it
or the reading is corrected.

**Excursions.** A technician, manager or admin opens an excursion on a piece of equipment and fills
in what is known: period, extreme reached, how it was detected, cause, action taken and impact
assessment. The life is *open*, *awaiting review* and *closed*: a technician submits it once it has
an end, a cause, an action and an impact assessment, and a manager or admin **other than the person
who submitted it** returns it with a note or closes it. A closed excursion can be reopened by a
manager or admin with a reason. The second-person close is a default, not a sourced rule. While an excursion is open the specimens that were in the unit during it are
computed from storage's recorded movements (placements, check-outs and check-ins, disposals, box
and rack moves); closing captures that list with the closing entry, and it stays fixed. Vapor Phase
records the review; it does not decide the outcome or what happens to the specimens, and it places
no holds itself (a hold is placed from the specimen record).

**Corrections.** Log entries, readings and service events can be back-dated (paper logs and logger
downloads), but not into the future. A correction replaces or withdraws an entry with a reason and
keeps the original and every earlier version. Managers and admins correct any entry; others only
entries they recorded. Eight equipment history tables are append-only in the database, and
equipment and excursions cannot be deleted. Attachments (up to 10 MiB, the type confirmed by content,
checksummed) are always downloaded, never displayed inline.

## Distribution

Distribution records how material leaves the repository for another institution: what the
repository's own records say about consent, who asked, who decided, what was sent and what came
back. It records what people declared and did and refuses steps those records do not support. It
does not decide consent, judge a request's scientific merit, approve on a committee's behalf,
classify a shipment or claim compliance with any rule; every refusal quotes the recorded fact
behind it. Everyone who works with it is staff: requesters do not sign in. The investigators and
contacts it holds are professional contacts, and a participant is still only a coded identifier;
no consent form, signature or scan is stored.

**Studies and collections.** A manager defines a study (code, title, status, and its consent basis)
and links the collections it governs; a collection belongs to at most one study at a time. The basis
is chosen per study: **records** means each participant's consent record is looked up, and
**determination** means a recorded study-level determination (a reference the repository types)
covers the collection, so a participant's refusal still blocks but a missing record does not.
Material with no participant, no collection or a collection no study governs cannot be requested,
allocated or sent, except that a determination study can cover material without a participant.
Ending a link and linking the collection to another study does not lift what was recorded under
the earlier one: a participant's refusal or withdrawal under any study that has governed the
collection keeps blocking that participant's specimens in it.

**Consent records.** A manager records, per participant and study, the form, the decision
(consented or refused), the answer for each scope category the form asks (permitted or refused),
the date, who signed, how it was documented, a source reference, any restrictions (shown to
reviewers and pickers, never parsed) and an optional expiry. Categories, forms and their versions
are the repository's own data; a category the form does not ask is "not addressed" and permits
nothing. The current record governs all of a participant's material in the study, including
material collected under an earlier form. A change of scope is a new record; a mistake is corrected
with a reasoned correction that keeps the original.

**Withdrawal.** A manager records a withdrawal as no further contact, no further use or destroy,
with the date it takes effect and how it was received. No further use and destroy place a use hold,
referencing the participant and study, on each available or exhausted specimen of that participant
in every collection the study governs or has governed, release any allocation of those specimens and leave picked ones for
staff to put back, all in one transaction; specimens already distributed are listed on the consent
page with their shipment and the agreement's end-of-use and onward-transfer terms, and staff record
the follow-up they did. For destroy, the page offers to file ordinary disposal requests for the
held specimens; another person still approves each. Nothing is disposed, anonymized or deleted by
the withdrawal, and the software contacts nobody. Holds are placed and released at the time of
recording; the withdrawal keeps the date reported. A withdrawn record cannot be withdrawn again:
another manager (not the one who recorded the withdrawal) reinstates it with a new consent record,
which releases the withdrawal's holds, and a stronger withdrawal is then recorded. Reinstating
names the consent record made after the withdrawal whenever there is one, and its recorder cannot
reinstate either; "recorded in error" is available only while no record was made after the
withdrawal, and the status then returns to the last record before it. Specimens
registered after a withdrawal are not held automatically; the consent page lists them and a manager
holds them.

**Requests.** An operating role creates a request for an investigator at an institution, with the
purpose, an ethics reference, how it was received, the use categories it needs and its lines
(specimen type, a count and optionally a study), then submits it. Each submission is kept. A person
with the request decision permission, who neither created nor submitted it, approves, rejects or
returns it. A manager can reopen an approved request that has nothing picked or packed (it must be
approved again), cancel one that has nothing picked or shipped, and close one with nothing left to
pick or send. A rejected request releases what it had allocated (this arises after a reopened
request is resubmitted and rejected). A manager links the agreement the request is sent under.

**Agreements.** A manager records an agreement (an MTA or DUA) with its institution, data level,
permitted use, categories, end-of-use and onward-transfer terms and reporting date, attaches the
documents (PDF, image, text or CSV up to 10 MiB each, always downloaded, never displayed inline), executes it with its dates and
may terminate it. One agreement can cover many requests; a request links one. An agreement is valid
from its effective date through the day before it expires or is terminated. Nothing expires or
terminates by itself: the dashboard queue lists agreements ending soon or with a report due within a set window. The software does
not draft or interpret agreements.

**The gate.** One check decides whether a specimen can be allocated, picked, packed and dispatched:
the specimen is available and carries no use or all-use hold (a disposal hold, including the
allocation's own, does not block); it is not allocated to another request; its collection is
governed by an active study; the request is approved and names that study if it names any; the
consent basis is satisfied (a refusal and a withdrawal always block, including one recorded under a
study that governed the collection before a relink; under records there must be a
record, whose current version must permit every use category the request names and must not have
expired; under determination the participant's current record, if any, must not refuse those categories). Dispatch also needs
a valid agreement for the request's institution, categories within its permitted ones, a data
level that allows participant codes if they are included, and the specimen unchanged since
packing. Packing and dispatch also need the specimen still checked out from its pick. At dispatch
the agreement's validity and consent expiry are judged on both the day the dispatch is recorded and
the date entered, so an earlier dispatch time cannot pass an agreement or consent that has since
ended. Restrictions text, remaining stock and the minimum quantity are shown, not gated. There is
no override: a wrong record is corrected, with a reason, and the gate runs again.

**Allocation and picking.** A manager allocates whole specimens to a request line, up to 200 at a
time and never beyond the line's count; an allocation reserves the specimen, not its position, and
places a disposal hold that is released with the allocation. Allocated specimens can still be moved
or used by others; the pick list shows it. The pick list is a PDF printout sorted by location
(site, room, unit, rack, box, position) with restrictions and blockers; printing is audited and it is
not a record. To send part of a specimen, derive an aliquot first and allocate that. Picking is an
ordinary check-out to the person picking, destination the request; a line can be marked not found,
and nothing moves. Putting a picked specimen back is an ordinary check-in; one that was checked in
from the specimen record after the pick is recorded as put back without a second check-in (packing
and dispatch refuse it until then). Times entered cannot precede the previous step: allocation not
before the approval, picking not before the allocation, packing not before the last pick, dispatch
not before packing.

**Shipments.** An operating role creates a shipment for an approved request and chooses its contents
from the picked allocations. Packing records what the shipper declares: temperature class,
refrigerant and dry-ice weight, a classification and note, package count, emergency contact, logger
IDs, storage instructions, whether participant codes are shown on the manifest (refused at packing
unless the agreement's data level is coded or limited) and whether the packer confirms training. The software records these as declared and never classifies a shipment or
blocks on the shipping declarations. A second person who checked the package is optional and must differ from the packer.
Unpacking returns the shipment to draft. A notification (shipping notice, tracking) is a recorded
act; nothing is emailed. Dispatch records the courier, tracking number and who it was handed to,
rechecks every specimen and the agreement, then freezes the contents as one record per specimen
(accession, type, container, quantity and unit, study, and the participant code only if the
manifest includes it) and dispatches each specimen. The manifest is a PDF: before dispatch it shows
the shipment as it stands and is marked "Not dispatched"; afterwards it is built only from the frozen
records and the shipment's fields, so later corrections never change what was sent. Collection dates
are not on it, and participant codes are off unless chosen and the agreement's data level allows
them; before dispatch they print only while the request's current agreement allows them. A manager can cancel a draft or packed shipment.

**Receipt.** The recipient's verification is transcribed: who received and unpacked, when, the
condition (including lost), refrigerant on arrival, a temperature note, discrepancies, an
exception for each affected item and an attachment. A manager corrects a receipt by a new version
that keeps the earlier ones. Receipt changes no specimen; dispatched material is outside custody.

**Returns.** A specimen returns only from its shipment. Staff record the quantity returned, the
condition, the thaws the recipient reported (or that they are unknown) and who received it. The
specimen becomes available (or exhausted if nothing came back), goes into the receiving person's
hands as an open period, and carries a use hold referencing the shipment, so it cannot be used or
sent again. Storing it is an ordinary check-in. A manager reviews the return and either releases
the hold or keeps it with a reason; disposal remains the ordinary approved request.

**History.** Requests, shipments, consent records, withdrawals, agreements and returns each show a
timeline built from append-only tables, with notes; the specimen's history shows the allocation,
dispatch, return and holds with their references. Pick lists, manifests and CSV downloads are
audited. Requests, allocation, shipments, consent and returns take the same single lock as storage
and specimens; catalogue saves do not.

## Inventory audits

Inventory audits compare a box with a saved expected inventory. Operators record every position
as empty or with the identifier they observe, including identifiers the system does not know.
Discrepancies require an explanation. A completed audit is evidence of the check, not a change to
stock: use the ordinary custody workflows to investigate and correct a location separately.

Working defaults are one active box of at most 400 positions, all observations submitted together,
and completion or cancellation by any operating role. Viewers read the records. Expected contents
and final results are append-only; repeat an audit to correct it, retaining the earlier record.
Relevant storage changes after the snapshot prevent completion; cancel and start again against
the current inventory. Operators can enter observations manually or load a normalized scanner CSV
for one box, review and adjust the grid, then complete the audit. Loading a file does not save
results or a draft. The source file is not retained; the completed audit keeps the reviewed
observations. The project format and bounds are working defaults in [product scope](product.md);
device-specific exports need conversion. In-app help describes the fields, controls and errors.

## Limits to know

| Limit | Detail |
| --- | --- |
| Roles, disposal authority and request decisions | Defaults (product scope); each repository decides who holds them |
| No thresholds | No limit on time out of storage, time away, thaw count or hold age; none is flagged. Temperature limits, low levels and due dates exist only where the repository typed them |
| Consent is what the repository records | The software compares requests with the records it was given; it does not decide consent, read consent forms or contact participants, and it has no override of a blocked step |
| No requester accounts | Requests are entered and decided by staff; nothing is emailed or sent to a recipient |
| Distribution declarations | Shipping classification, refrigerant and training are recorded as the shipper declares them; nothing classifies a shipment or blocks on them. The manifest's fields are working defaults |
| No recall | A withdrawal lists material already distributed and records follow-up; it recalls nothing |
| Allocation reserves the specimen, not its position | Others can still move or use allocated material; a disposal hold stops only its disposal. Allocation is whole-container |
| Time away is not time out of storage | A long wait between picking and dispatch does count as time out of storage |
| No monitoring or alarms | Readings are entered or imported; nothing is polled, and no notification is sent |
| Box audit import | Normalized scanner CSV for one box at a time; no device-specific import or hardware qualification |
| Quality workflows | No deviations, training or automatic holds from excursions yet |
| One repository organization | No tenant layer; permissions are not site-scoped |
| Storage writes are serialized | One lock per storage-changing operation; fine for one repository's pace |
| Disposal approvals are brittle by design | Any change to the specimen after approval needs a new request |
| Stacked holds | The same scope can be placed twice; each needs its own release |
| Browser selection of boxes | Action forms list every active box; very large repositories would want search |
| Label output | Code 128 only; not verified with a scanner; stock size is a default |
| No compliance claim | The audit trail is built to support a quality system; it is not a conformance statement |

## Electronic record retention

Staff can read **Record retention** at `/repository/record-retention/`. It starts as **Not
recorded**. A deployment administrator with the `admin` role who is also a superuser records or
clears the repository's statement under **Administration → Core → Repositories**, with a required
reason and stale-edit protection. Changes keep the prior statement and attribution. No duration or
automatic deletion is inferred, and the statement is the repository's own text, not a compliance
claim.

## Announcements

The `admin` role posts announcements under **Setup → Announcements**. Each has a title, a
plain-text message, an importance (Notice or Important) and one or both places: the sign-in page
and the staff dashboard. No other page shows them. Who sees one follows the account, not the
page: a visitor who is not signed in sees only sign-in page announcements, and signed-in staff
only staff ones. Anything on the sign-in page is public, so keep confidential and participant
information out of it. Messages are shown exactly as typed; formatting, links and HTML are not
interpreted.

An announcement shows from **Show from** until **Show until**, or until it is withdrawn when no
end is set. Withdrawing is permanent: the announcement stays in the list with its history and
cannot be changed or shown again. Announcements are never deleted, there is no draft or approval
step, readers cannot dismiss them, and nothing is emailed. Posting, every change and withdrawal
are audited with the administrator and the values before and after.

## Demonstration accounts and reset

`seed_demo` creates the organization "Example Biorepository", the accounts `demo_admin`,
`demo_manager`, `demo_technician` and `demo_viewer`, a small storage structure, one short specimen
journey, the equipment records of its tank and freezer (level log, readings, two excursions) and a
first distribution journey (a study with a consent record, an executed agreement, one request sent,
received and partly returned, one waiting for a decision), all synthetic. `demo_manager` also holds
the disposal approval and request decision permissions so both approval flows can be shown. A
visitor can try a request with two accounts: the technician creates and submits one, and the
manager, who did not, decides it; the technician is refused. `seed_demo --full` adds a working repository: about 350 specimens from 70 coded
participants in seven storage units and some thirty boxes (from empty to full, with blocked
positions and one retired box), aliquot and derivative families, check-outs returned and check-outs
still out for hours to a month, observed thaws, holds in both scopes, disposal requests in every
state, bulk imports (two committed, one waiting, one with errors) and reasoned corrections. It
also gives every unit its equipment record with about three months of history: quarterly
maintenance, a mapping, an overdue calibration, a repair with out-of-service and back-in-service
changes, twelve weeks of LN2 level checks and fills, thirty days of imported temperature readings,
a closed excursion that captured the specimens in the unit, an open one, and one out-of-limit
reading left without an excursion. For distribution it adds studies governing the cohorts (one by
consent records, one by a study-level determination, one collection left ungoverned), consent
records with a refusal and a withdrawal whose holds sit on that participant's specimens, a second
institution whose agreement ends within 60 days, and a request approved, picked and packed awaiting
dispatch. The limits and low levels in it are labelled demonstration values.
It uses only the services the application uses, and adds nothing when run again. Its events carry
times relative to the moment of seeding, except approvals, disposals and other steps the services
stamp with the current time.

`--announce-accounts` posts one sign-in page announcement that lists the manager, technician and
viewer accounts by role and their shared password, for a public demonstration; see the README. It
never lists `demo_admin` and refuses to publish a password that opens that account. The listed
accounts become shared demonstration accounts: whoever signs in to one cannot open its password,
email address or two-factor pages, and no password-reset link is mailed for it. Nothing else sets
that restriction, and no page removes it.

`reset_demo` erases every record and seeds again, only on a deployment that opts in and holds the
demonstration repository or nothing; the public demonstration runs it daily with `--full`. See
[operations](operations.md).

## Tables and page help

Table search, filters, sorting and pagination run on the server. DataTables/ColReorder enhance
column visibility and order; column preferences stay in this browser. Vendor versions and licenses
are included under `static/vendor/datatables/`.

The documentation icon at the top right opens help for the current page or form in a resizable
right panel. It explains the page's purpose, actions, fields and workflow rules. The panel follows
navigation and remembers its width and open/closed state for each account in this browser.
Drag its left edge or focus the separator and use the arrow keys to resize; use its close button
or Escape from inside the panel to close it. Contributors use the
[`document-page` skill](../skills/document-page/SKILL.md) to update help with each page change.
