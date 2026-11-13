"""Distribution reads: the distributability gate, allocation candidates, pick lists, consent
impact and small helpers shared by pages, documents and services.

The gate (`blockers` / `evaluate`) checks recorded facts against each other and returns coded
blockers, each with a message naming the fact behind it. It decides nothing about consent or
compliance; a wrong record is corrected and the gate re-evaluated. There is no override.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from django.db.models import Q
from django.utils import timezone as tz

from accounts.roles import require_role
from specimens.models import Hold, Specimen
from storage import queries as storage_queries
from storage import services as storage

from .models import (
    Allocation,
    AllocationEvent,
    Consent,
    ConsentAnswer,
    ConsentHold,
    ConsentWithdrawal,
    ShipmentItem,
    SpecimenRequest,
    Study,
    StudyCollection,
)

READ_ROLES = storage.READ_ROLES
STAGES = ("allocate", "pick", "pack", "dispatch")
CODES = {
    "not_available": "Not available",
    "held": "On a use hold",
    "allocated_elsewhere": "Allocated to another request",
    "no_study": "Not governed by a study",
    "study_inactive": "Study not active",
    "outside_request": "Outside the request",
    "no_consent": "No consent recorded",
    "consent_refused": "Consent refused",
    "consent_withdrawn": "Consent withdrawn",
    "scope_not_permitted": "Use not permitted by consent",
    "consent_expired": "Consent expired",
    "request_not_approved": "Request not approved",
    "agreement_missing": "No agreement",
    "agreement_invalid": "Agreement not valid",
    "agreement_mismatch": "Agreement does not match",
    "changed_since_packing": "Changed since packing",
    "not_in_pick_custody": "Not in the pick's custody",
}


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str

    @property
    def label(self):
        return CODES.get(self.code, self.code)


@dataclass(frozen=True)
class Governance:
    """What governs one specimen: its study (current collection link) and consent."""

    study: Study | None
    consent: Consent | None


def _studies_by_collection(collection_ids):
    links = StudyCollection.objects.filter(
        collection_id__in=[c for c in collection_ids if c], ended_at__isnull=True
    ).select_related("study")
    return {link.collection_id: link.study for link in links}


def governed_collections(study_id):
    """Collection ids the study governs or has governed (ended links included). A refusal or
    withdrawal recorded under the study keeps following that material after a relink."""
    return StudyCollection.objects.filter(study_id=study_id).values("collection_id")


def _past_stops(specimens, gov):
    """{specimen id: [Consent]}: refused or withdrawn consents of the specimen's participant in
    any study that has governed its collection, other than the current one (checked fully)."""
    specimens = [s for s in specimens if s.participant_id and s.collection_id]
    if not specimens:
        return {}
    studies_of = defaultdict(set)
    for collection_id, study_id in StudyCollection.objects.filter(
        collection_id__in={s.collection_id for s in specimens}
    ).values_list("collection_id", "study_id"):
        studies_of[collection_id].add(study_id)
    stops = defaultdict(list)
    for consent in (
        Consent.objects.filter(
            participant_id__in={s.participant_id for s in specimens},
            study_id__in=set().union(*studies_of.values()) if studies_of else (),
            status__in=(Consent.Status.REFUSED, Consent.Status.WITHDRAWN),
        )
        .select_related("study", "participant")
        .order_by("study__code")
    ):
        stops[consent.participant_id].append(consent)
    result = {}
    for s in specimens:
        current = gov[s.pk].study
        found = [
            c
            for c in stops.get(s.participant_id, ())
            if c.study_id in studies_of[s.collection_id]
            and (current is None or c.study_id != current.pk)
        ]
        if found:
            result[s.pk] = found
    return result


def _pick_custody(request, specimen_ids):
    """{specimen id: custody event id of the pick} for the request's picked allocations."""
    picked = {
        a.pk: a.specimen_id
        for a in Allocation.objects.filter(
            request=request, specimen_id__in=list(specimen_ids), state=Allocation.State.PICKED
        )
    }
    custody = {}
    for event in AllocationEvent.objects.filter(
        allocation_id__in=list(picked), kind=AllocationEvent.Kind.PICKED
    ).order_by("recorded_at", "id"):
        custody[picked[event.allocation_id]] = event.custody_event_id
    return custody


def _days(on):
    """The dates expiry and validity are judged on: one date, several, or today."""
    if on is None:
        return (tz.localdate(),)
    if isinstance(on, date):
        return (on,)
    return tuple(sorted(set(on))) or (tz.localdate(),)


def governing_studies(specimens):
    """{specimen id: Study or None} by each specimen's collection's current link."""
    specimens = list(specimens)
    by_collection = _studies_by_collection({s.collection_id for s in specimens})
    return {s.pk: by_collection.get(s.collection_id) for s in specimens}


def governance(specimens):
    """{specimen id: Governance} with the consent loaded with its current record and answers."""
    specimens = list(specimens)
    studies = governing_studies(specimens)
    pairs = {
        (s.participant_id, studies[s.pk].pk)
        for s in specimens
        if studies[s.pk] and s.participant_id
    }
    consents = {}
    if pairs:
        query = Q()
        for participant_id, study_id in pairs:
            query |= Q(participant_id=participant_id, study_id=study_id)
        for consent in Consent.objects.filter(query).select_related(
            "current_record", "current_record__form", "participant", "study"
        ):
            consents[(consent.participant_id, consent.study_id)] = consent
    result = {}
    for s in specimens:
        study = studies[s.pk]
        consent = consents.get((s.participant_id, study.pk)) if study and s.participant_id else None
        result[s.pk] = Governance(study, consent)
    return result


def _answers(consents):
    record_ids = [c.current_record_id for c in consents if c and c.current_record_id]
    answers = defaultdict(dict)
    for answer in ConsentAnswer.objects.filter(record_id__in=record_ids).select_related("category"):
        answers[answer.record_id][answer.category_id] = (answer.answer, answer.category.code)
    return answers


def evaluate(request, specimen_ids, *, stage="allocate", item=None, on=None):
    """{specimen id: (Blocker, ...)} for the given stage; an empty tuple means none.

    Stages: allocate (adds allocated_elsewhere and line checks when `item` is given), pick,
    pack (adds not_in_pick_custody), dispatch (adds agreement and changed_since_packing). `on`
    is the date, or the dates, consent expiry and agreement validity are judged on (today by
    default); with several, each must pass. Refusals and withdrawals of the participant under
    any study that has governed the specimen's collection block, whatever governs it now.
    """
    if stage not in STAGES:
        raise ValueError(stage)
    on = _days(on)
    specimens = list(
        Specimen.objects.select_related("participant", "collection", "specimen_type").filter(
            pk__in=list(specimen_ids)
        )
    )
    gov = governance(specimens)
    past = _past_stops(specimens, gov)
    answers = _answers([g.consent for g in gov.values()])
    categories = list(request.use_categories.all())
    request_studies = {s.pk for s in request.studies.all()}
    use_holds = defaultdict(list)
    for hold in Hold.objects.filter(
        specimen_id__in=[s.pk for s in specimens],
        release__isnull=True,
        scope__in=(Hold.Scope.USE, Hold.Scope.ALL_USE),
    ).order_by("occurred_at"):
        use_holds[hold.specimen_id].append(hold)
    open_elsewhere = {}
    if stage == "allocate":
        for allocation in Allocation.objects.filter(
            specimen_id__in=[s.pk for s in specimens], state__in=Allocation.OPEN
        ).select_related("request"):
            open_elsewhere[allocation.specimen_id] = allocation.request
    allocations = {}
    if stage == "dispatch":
        allocations = {
            a.specimen_id: a
            for a in Allocation.objects.filter(
                request=request, specimen_id__in=[s.pk for s in specimens], state="picked"
            )
        }
    agreement = request.agreement if stage == "dispatch" else None
    custody = (
        _pick_custody(request, [s.pk for s in specimens]) if stage in ("pack", "dispatch") else {}
    )
    result = {}
    for s in specimens:
        found = []

        def block(code, message, found=found):
            found.append(Blocker(code, message))

        if request.status != SpecimenRequest.Status.APPROVED:
            block(
                "request_not_approved",
                f"{request.number} is {request.get_status_display().lower()}, not approved.",
            )
        if s.status != Specimen.Status.AVAILABLE:
            block("not_available", f"Status is {s.get_status_display().lower()}.")
        for hold in use_holds[s.pk]:
            text = hold.get_scope_display().lower()
            block("held", f"Active {text}" + (f" ({hold.reference})." if hold.reference else "."))
        other = open_elsewhere.get(s.pk)
        if other is not None:
            block(
                "allocated_elsewhere",
                f"Already allocated to {other.number}."
                if other.pk != request.pk
                else "Already allocated to this request.",
            )
        g = gov[s.pk]
        study = g.study
        if study is None:
            block(
                "no_study",
                "No collection recorded."
                if s.collection_id is None
                else f"Collection {s.collection.code} is not linked to a study.",
            )
        else:
            if study.status != Study.Status.ACTIVE:
                block(
                    "study_inactive", f"Study {study.code} is {study.get_status_display().lower()}."
                )
            if request_studies and study.pk not in request_studies:
                block("outside_request", f"Study {study.code} is not one the request names.")
            if item is not None and item.study_id and item.study_id != study.pk:
                block(
                    "outside_request", f"Line {item.line} asks for another study than {study.code}."
                )
            _consent_checks(s, study, g.consent, categories, answers, on, block)
        for consent in past.get(s.pk, ()):
            word = "withdrew" if consent.status == Consent.Status.WITHDRAWN else "refused"
            block(
                f"consent_{'withdrawn' if word == 'withdrew' else 'refused'}",
                f"{consent.participant.code} {word} consent in {consent.study.code}, which has "
                f"governed collection {s.collection.code}.",
            )
        if item is not None and item.specimen_type_id != s.specimen_type_id:
            block(
                "outside_request",
                f"Type {s.specimen_type.code} is not the type line {item.line} asks for.",
            )
        if s.pk in custody and s.open_checkout_id != custody[s.pk]:
            block(
                "not_in_pick_custody",
                f"Checked in or moved since it was picked for {request.number}. Put it back on "
                "the request (this records where it is) and pick it again.",
            )
        if stage == "dispatch":
            _agreement_checks(request, agreement, on, block)
            allocation = allocations.get(s.pk)
            if allocation is None or allocation.packed_revision != s.revision:
                block(
                    "changed_since_packing",
                    "The specimen record changed after packing. Unpack, check and pack again.",
                )
        result[s.pk] = tuple(found)
    return result


def _consent_checks(specimen, study, consent, categories, answers, on, block):
    records_basis = study.consent_basis == Study.Basis.RECORDS
    if specimen.participant_id is None:
        if records_basis:
            block("no_consent", "No coded participant is recorded, so no consent can govern it.")
        return
    code = specimen.participant.code
    if consent is None:
        if records_basis:
            block("no_consent", f"No consent recorded for {code} in {study.code}.")
        return
    if consent.status == Consent.Status.WITHDRAWN:
        block("consent_withdrawn", f"{code} withdrew consent in {study.code}.")
        return
    record = consent.current_record
    form = record.form if record else None
    if consent.status == Consent.Status.REFUSED:
        block("consent_refused", f"{code} refused on form {form} ({study.code}).")
        return
    if record is None:
        if records_basis:
            block("no_consent", f"No consent record for {code} in {study.code}.")
        return
    given = answers.get(record.pk, {})
    for category in categories:
        answer = given.get(category.pk)
        if records_basis and (answer is None or answer[0] != "permitted"):
            word = "refused" if answer and answer[0] == "refused" else "did not address"
            block("scope_not_permitted", f"{code} {word} {category.code} on form {form}.")
        elif not records_basis and answer and answer[0] == "refused":
            block("scope_not_permitted", f"{code} refused {category.code} on form {form}.")
    if record.expires_on and record.expires_on < max(on):
        block("consent_expired", f"{code}'s consent on form {form} expired {record.expires_on}.")


def _agreement_checks(request, agreement, on, block):
    if agreement is None:
        block("agreement_missing", f"{request.number} has no agreement linked.")
        return
    invalid = [day for day in on if not agreement.is_valid_on(day)]
    if invalid:
        days = " or ".join(day.isoformat() for day in invalid)
        block("agreement_invalid", f"{agreement.number} is not valid on {days}.")
    if agreement.institution_id != request.institution_id:
        block("agreement_mismatch", f"{agreement.number} is with another institution.")
    permitted = {c.pk for c in agreement.permitted_categories.all()}
    if permitted:
        outside = sorted(c.code for c in request.use_categories.all() if c.pk not in permitted)
        if outside:
            block(
                "agreement_mismatch",
                f"{agreement.number} does not cover {', '.join(outside)}.",
            )


def blockers(*, actor, request_id, specimen_ids, on=None, stage="allocate", item_id=None):
    """Public form of `evaluate` for pages: {specimen id: (Blocker, ...)}."""
    require_role(actor, *READ_ROLES)
    request = SpecimenRequest.objects.select_related("agreement").get(pk=request_id)
    item = request.items.filter(pk=item_id).first() if item_id else None
    return evaluate(request, specimen_ids, stage=stage, item=item, on=on)


def participant_code_blocker(shipment):
    """A shipment asking for participant codes needs an agreement at data level coded or
    limited (owner default; dates of collection are never on the manifest)."""
    agreement = shipment.request.agreement
    if not shipment.include_participant_codes:
        return None
    if agreement is None or agreement.data_level not in ("coded", "limited"):
        return Blocker(
            "agreement_mismatch",
            "Participant codes are included but the agreement's data level is not coded or "
            "limited.",
        )
    return None


# ------------------------------------------------------------------ candidates and pick lists


@dataclass(frozen=True)
class Candidate:
    specimen: Specimen
    blockers: tuple
    path: str | None
    same_type_remaining: int


def candidate_queryset(request, item, filters=None):
    """Available specimens of the line's type (and study, when the line or request names one)."""
    filters = filters or {}
    queryset = Specimen.objects.select_related(
        "specimen_type", "container_type", "unit", "participant", "collection"
    ).filter(specimen_type_id=item.specimen_type_id, status=Specimen.Status.AVAILABLE)
    study_ids = [item.study_id] if item.study_id else [s.pk for s in request.studies.all()]
    if study_ids:
        queryset = queryset.filter(
            collection_id__in=StudyCollection.objects.filter(
                study_id__in=study_ids, ended_at__isnull=True
            ).values("collection_id")
        )
    if filters.get("participant"):
        queryset = queryset.filter(participant__code__iexact=filters["participant"].strip())
    if filters.get("collection"):
        queryset = queryset.filter(collection__code__iexact=filters["collection"].strip())
    if filters.get("q"):
        queryset = queryset.filter(accession__icontains=filters["q"].strip())
    return queryset.order_by("accession")


def candidates(*, actor, request_id, item_id, filters=None, offset=0, limit=50):
    """(page of Candidate, total) for the allocation grid; blocked ones are listed with reasons."""
    require_role(actor, *READ_ROLES)
    request = SpecimenRequest.objects.get(pk=request_id)
    item = request.items.get(pk=item_id)
    queryset = candidate_queryset(request, item, filters)
    total = queryset.count()
    rows = list(queryset[offset : offset + limit])
    found = evaluate(request, [s.pk for s in rows], stage="allocate", item=item)
    paths = storage_queries.locations(actor=actor, occupancy_ids=[s.occupancy_id for s in rows])
    remaining = defaultdict(int)
    participants = {s.participant_id for s in rows if s.participant_id}
    for participant_id in Specimen.objects.filter(
        participant_id__in=participants,
        specimen_type_id=item.specimen_type_id,
        status=Specimen.Status.AVAILABLE,
    ).values_list("participant_id", flat=True):
        remaining[participant_id] += 1
    page = tuple(
        Candidate(
            specimen=s,
            blockers=found[s.pk],
            path=(paths.get(s.occupancy_id) or {}).get("display")
            if paths.get(s.occupancy_id)
            else None,
            same_type_remaining=remaining.get(s.participant_id, 0) if s.participant_id else 0,
        )
        for s in rows
    )
    return page, total


@dataclass(frozen=True)
class PickRow:
    allocation: Allocation
    specimen: Specimen
    path: dict | None
    restrictions: str
    blockers: tuple


def _sort_key(path):
    if path is None:
        return (1,)
    nodes = tuple((n.get("slot") or 0) for n in path.get("nodes", []))
    box = path.get("box") or {}
    position = path.get("position") or {}
    return (
        0,
        path["site"]["code"],
        path["room"]["code"],
        path["unit"]["code"],
        nodes,
        box.get("slot") or 0,
        box.get("code") or "",
        position.get("ordinal") or 0,
    )


def pick_list(*, actor, request_id):
    """Allocated (not yet picked) specimens of a request in walking order: site, room, unit,
    rack slots, box, position; specimens not in storage last."""
    require_role(actor, *READ_ROLES)
    request = SpecimenRequest.objects.select_related("agreement").get(pk=request_id)
    allocations = list(
        Allocation.objects.filter(request=request, state=Allocation.State.ALLOCATED).select_related(
            "specimen",
            "specimen__specimen_type",
            "specimen__container_type",
            "specimen__unit",
            "specimen__participant",
            "item",
        )
    )
    specimens = [a.specimen for a in allocations]
    paths = storage_queries.locations(
        actor=actor, occupancy_ids=[s.occupancy_id for s in specimens]
    )
    found = evaluate(request, [s.pk for s in specimens], stage="pick")
    gov = governance(specimens)
    rows = []
    for allocation in allocations:
        s = allocation.specimen
        consent = gov[s.pk].consent
        restrictions = (
            consent.current_record.restrictions if consent and consent.current_record else ""
        )
        rows.append(
            PickRow(
                allocation=allocation,
                specimen=s,
                path=paths.get(s.occupancy_id),
                restrictions=restrictions,
                blockers=found[s.pk],
            )
        )
    rows.sort(key=lambda r: (_sort_key(r.path), r.specimen.accession))
    return tuple(rows)


# ------------------------------------------------------------------ consent impact


@dataclass(frozen=True)
class Impact:
    held: tuple  # specimens in custody with an active hold from the open withdrawal
    unheld: tuple  # in custody (available/exhausted) without one
    open_allocations: tuple
    distributed: tuple  # ShipmentItem rows of this participant's material in the study


def consent_impact(*, actor, consent_id):
    require_role(actor, *READ_ROLES)
    consent = Consent.objects.select_related("participant", "study").get(pk=consent_id)
    collections = governed_collections(consent.study_id)
    in_custody = list(
        Specimen.objects.filter(
            participant_id=consent.participant_id,
            collection_id__in=collections,
            status__in=(Specimen.Status.AVAILABLE, Specimen.Status.EXHAUSTED),
        )
        .select_related("specimen_type")
        .order_by("accession")
    )
    withdrawal = (
        consent.withdrawals.filter(
            kind__in=(ConsentWithdrawal.Kind.NO_USE, ConsentWithdrawal.Kind.DESTROY),
            reinstatement__isnull=True,
        )
        .order_by("-recorded_at", "-id")
        .first()
    )
    held_ids = set()
    if withdrawal is not None:
        held_ids = set(
            ConsentHold.objects.filter(
                withdrawal=withdrawal, hold__release__isnull=True
            ).values_list("specimen_id", flat=True)
        )
    open_allocations = tuple(
        Allocation.objects.filter(
            specimen_id__in=[s.pk for s in in_custody], state__in=Allocation.OPEN
        ).select_related("request", "specimen")
    )
    distributed = tuple(
        ShipmentItem.objects.filter(
            specimen__participant_id=consent.participant_id,
            specimen__collection_id__in=StudyCollection.objects.filter(
                study_id=consent.study_id
            ).values("collection_id"),
        )
        .select_related("shipment", "shipment__request", "shipment__request__agreement")
        .order_by("shipment_id", "line")
    )
    return Impact(
        held=tuple(s for s in in_custody if s.pk in held_ids),
        unheld=tuple(s for s in in_custody if s.pk not in held_ids),
        open_allocations=open_allocations,
        distributed=distributed,
    )


def agreement_horizon(today=None, days=60):
    """The display horizon for expiring agreements and reporting dates (not a rule)."""
    today = today or tz.localdate()
    return today, date.fromordinal(today.toordinal() + days)
