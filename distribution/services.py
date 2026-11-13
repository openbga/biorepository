"""Distribution mutations: catalogues, studies, consent and withdrawal, requests and decisions,
allocation, picking, shipments, receipt, returns and agreements.

Every function runs in `storage.operation()` (one transaction; nesting joins it), checks the
actor's role before reading any record (PermissionDenied), then takes the StorageGate
(`storage.lock_gate()`) except for catalogue saves, notes, receipts and agreement documents,
then locks its own rows in the order consent -> request -> shipment -> agreement ->
allocations (by id) and changes specimens only through the specimens services, which lock the
specimen rows themselves. Revisions are checked after locking; the distributability gate
(`queries.blockers`) is evaluated after locking; events, projections and the attributed audit
event are written in the same transaction.

The software records what people decided and did and refuses steps the records do not support.
It never decides consent, approves on a committee's behalf or classifies a shipment.

Errors: ValidationError with the codes `stale_revision`, `invalid_transition`, `invalid_time`,
`held`, `insufficient_quantity`, `self_approval`, `blocked` (params {"blockers": {accession:
[codes]}}), or a field code (required, invalid, unique). Role failures raise PermissionDenied.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.utils import timezone as tz

from accounts.models import User
from accounts.roles import effective_roles, has_role, require_request_authority, require_role
from core.attachments import store_attachment
from core.audit import record_event
from specimens import services as specimens
from specimens.models import (
    Collection,
    CustodyEvent,
    DisposalRequest,
    Participant,
    QuantityUnit,
    Specimen,
    SpecimenType,
)
from storage import services as storage

from . import queries
from .models import (
    Agreement,
    AgreementDocument,
    AgreementEvent,
    Allocation,
    AllocationEvent,
    Consent,
    ConsentAnswer,
    ConsentForm,
    ConsentHold,
    ConsentRecord,
    ConsentReinstatement,
    ConsentScopeCategory,
    ConsentWithdrawal,
    Institution,
    Investigator,
    ReceiptException,
    RequestDecision,
    RequestEvent,
    RequestItem,
    RequestSubmission,
    ReturnReview,
    Shipment,
    ShipmentEvent,
    ShipmentItem,
    ShipmentReceipt,
    SpecimenRequest,
    SpecimenReturn,
    Study,
    StudyCollection,
    WithdrawalFollowUp,
)

READ_ROLES = storage.READ_ROLES  # admin, manager, technician, viewer
OPERATE_ROLES = storage.OPERATE_ROLES  # admin, manager, technician
MANAGE_ROLES = storage.MANAGE_ROLES  # admin, manager
TEXT_LIMIT = 4000
INITIAL_REASON = "Initial definition"
MAX_LINES = 200  # specimens per allocate, pick or put-back call; items per shipment
MAX_WITHDRAWAL = 2000  # specimens held by one withdrawal
MAX_ITEMS = 50  # lines per request
ALLOCATION_REFERENCE = "Allocated to {number}"
RETURN_REFERENCE = "Returned in {number}"
WITHDRAWAL_REFERENCE = "Consent withdrawn: {participant}, {study}"


# ------------------------------------------------------------------ typed inputs


@dataclass(frozen=True)
class ItemSpec:
    line: int
    specimen_type_id: object
    count: int
    minimum_quantity: Decimal | None = None
    unit_id: object = None
    study_id: object = None
    notes: str = ""


@dataclass(frozen=True)
class Choice:
    specimen_id: object
    expected_revision: int


@dataclass(frozen=True)
class PickLine:
    allocation_id: object
    expected_revision: int  # the specimen's
    expected_occupancy_revision: int
    missing: bool = False
    note: str = ""


@dataclass(frozen=True)
class PutBackLine:
    allocation_id: object
    expected_revision: int  # the specimen's
    expected_occupancy_revision: int
    position_id: object  # None for a specimen already back in storage (`out_of_pick_custody`)
    thaw_observed: bool | None = None


@dataclass(frozen=True)
class PackDeclaration:
    temperature_class: str
    refrigerant: str
    dry_ice_kg: Decimal | None
    classification: str
    classification_note: str
    package_count: int
    emergency_contact: str
    logger_ids: str
    storage_instructions: str
    include_participant_codes: bool
    training_confirmed: bool | None


@dataclass(frozen=True)
class ReceiptValues:
    received_at: datetime
    received_by_name: str
    unpacked_by_name: str
    condition: str
    refrigerant_on_arrival: str
    temperature_note: str
    discrepancies: str
    received_via: str
    exceptions: tuple = ()  # (item_id, outcome, note)


# ------------------------------------------------------------------ helpers


def _error(message, code="invalid", field=None, params=None):
    error = ValidationError(message, code=code, params=params)
    return ValidationError({field: error}) if field else error


def need_reason(reason, field="reason"):
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        raise _error("Give the reason. It is kept in the history.", "required", field)
    if len(text) > TEXT_LIMIT:
        raise _error(f"Use at most {TEXT_LIMIT} characters.", "invalid", field)
    return text


def _text(value, field, limit=TEXT_LIMIT, *, required=False, label="a value"):
    text = value.strip() if isinstance(value, str) else ""
    if required and not text:
        raise _error(f"Enter {label}.", "required", field)
    if len(text) > limit:
        raise _error(f"Use at most {limit} characters.", "invalid", field)
    return text


def _choice(value, choices, field, label):
    if value not in dict(choices):
        raise _error(f"Choose {label}.", "invalid", field)
    return value


def _day(value, field, *, required=True, past=True, label="the date"):
    if value in (None, ""):
        if required:
            raise _error(f"Enter {label}.", "required", field)
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise _error(f"Enter {label}.", "invalid", field)
    if past and value > tz.localdate():
        raise _error("The date cannot be in the future.", "invalid_time", field)
    return value


def _moment(value, field="occurred_at", *, label="when it happened", floor=None, now=None):
    if not isinstance(value, datetime) or tz.is_naive(value):
        raise _error(f"Enter {label} with its date and time.", "invalid_time", field)
    if value > (now or tz.now()):
        raise _error("The time cannot be in the future.", "invalid_time", field)
    # Forms enter times to the second: a floor is compared at that precision.
    if floor is not None and value < floor.replace(microsecond=0):
        local = tz.localtime(floor).strftime("%Y-%m-%d %H:%M")
        raise _error(f"The time cannot be earlier than {local}.", "invalid_time", field)
    return value


def _whole(value, field, *, low, high, label):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _error(f"Enter {label} as a whole number from {low} to {high}.", "invalid", field)
    return value


def _uuid(value, field=None, noun="record"):
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError, TypeError, AttributeError:
        raise _error(f"Choose an existing {noun}.", "invalid", field) from None


def _json(data):
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


def state(obj, fields):
    """JSON-safe snapshot of the named fields (foreign keys as their key) for audit events."""
    data = {}
    for name in fields:
        field = obj._meta.get_field(name)
        if field.many_to_many:
            data[name] = sorted(str(pk) for pk in getattr(obj, name).values_list("pk", flat=True))
        else:
            data[name] = getattr(obj, field.attname)
    return _json(data)


def _changes(before, after):
    return {k: [before.get(k), after.get(k)] for k in after if before.get(k) != after.get(k)}


def _audit(actor, action, obj, *, before=None, after=None, reason=""):
    record_event(
        actor=actor,
        action=action,
        obj=obj,
        before=_json(before or {}),
        after=_json(after or {}),
        reason=reason,
    )


def _attachment_audit(row):
    if not getattr(row, "attachment_id", None):
        return {"attachment": None, "attachment_sha256": ""}
    return {"attachment": str(row.attachment_id), "attachment_sha256": row.attachment.sha256}


def _check_revision(obj, expected, noun):
    if expected is None or obj.revision != expected:
        raise _error(
            f"This {noun} changed since you opened it. Reload it and try again.", "stale_revision"
        )


def _bump(obj, *fields):
    obj.revision += 1
    obj.save(update_fields=["revision", *fields] if fields else None)


def _lock(model, pk, noun=""):
    """Row lock by key; an unknown key is DoesNotExist (404 in views)."""
    return model.objects.select_for_update(of=("self",)).get(pk=pk)


def _staff(user_id, field, roles=READ_ROLES, label="an active staff member"):
    user = user_id if isinstance(user_id, User) else User.objects.filter(pk=user_id).first()
    if user is None or not user.is_active or not effective_roles(user).intersection(roles):
        raise _error(f"Choose {label}.", "invalid", field)
    return user


def _save_unique(obj, field, message):
    """Save in a savepoint; a unique conflict becomes a field error."""
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as exc:
        if getattr(exc.__cause__, "pgcode", None) == "23505":  # unique_violation
            raise _error(message, "unique", field) from exc
        raise


def _blocked(found, specimens_by_id):
    """Raise `blocked` naming each specimen's recorded facts."""
    found = {k: v for k, v in found.items() if v}
    if not found:
        return
    params = {"blockers": {}}
    lines = []
    for specimen_id, blockers in sorted(
        found.items(), key=lambda kv: specimens_by_id[kv[0]].accession
    ):
        accession = specimens_by_id[specimen_id].accession
        params["blockers"][accession] = [b.code for b in blockers]
        lines.extend(f"{accession}: {b.message}" for b in blockers)
    raise ValidationError(" ".join(lines), code="blocked", params=params)


def _specimen(specimen_id):
    return Specimen.objects.select_related(
        "specimen_type", "container_type", "unit", "participant", "collection"
    ).get(pk=specimen_id)


# ------------------------------------------------------------------ catalogues

CATALOGUES = {
    "category": ConsentScopeCategory,
    "consent_form": ConsentForm,
    "institution": Institution,
    "investigator": Investigator,
}
CATALOGUE_FIELDS = {
    "category": ("code", "name", "description", "gates_use", "is_active"),
    "consent_form": ("study", "code", "version", "title", "kind", "categories", "is_active"),
    "institution": ("code", "name", "address", "country", "is_active"),
    "investigator": ("code", "name", "institution", "email", "phone", "is_active"),
}
CATALOGUE_NOUNS = {
    "category": "scope category",
    "consent_form": "consent form",
    "institution": "institution",
    "investigator": "investigator",
}


def catalogue_in_use(kind, obj):
    """Whether recorded data refers to this entry, which fixes its identifying fields."""
    if obj is None or obj.pk is None:
        return False
    if kind == "category":
        return ConsentAnswer.objects.filter(category=obj).exists()
    if kind == "consent_form":
        return ConsentRecord.objects.filter(form=obj).exists()
    if kind == "institution":
        return (
            SpecimenRequest.objects.filter(institution=obj).exists()
            or Agreement.objects.filter(institution=obj).exists()
        )
    if kind == "investigator":
        return SpecimenRequest.objects.filter(investigator=obj).exists()
    return False


def _catalogue_values(kind, values, obj):
    allowed = CATALOGUE_FIELDS[kind]
    unknown = set(values) - set(allowed)
    if unknown:
        raise _error(f"Unknown field: {sorted(unknown)[0]}.", "invalid")

    def get(name, default=""):
        if name in values:
            return values[name]
        if obj is not None:
            if obj._meta.get_field(name).many_to_many:
                return list(getattr(obj, name).all())
            return getattr(obj, name)
        return default

    data = {
        "code": storage.normalize_code(get("code")),
        "is_active": bool(get("is_active", True)),
    }
    if kind == "category":
        data["name"] = _text(get("name"), "name", 160, required=True, label="a name")
        data["description"] = _text(get("description"), "description", TEXT_LIMIT)
        data["gates_use"] = bool(get("gates_use", True))
    elif kind == "consent_form":
        study = get("study", None)
        study_id = getattr(study, "pk", study)
        if not study_id:
            raise _error("Choose the study.", "required", "study")
        data["study"] = Study.objects.filter(pk=study_id).first()
        if data["study"] is None:
            raise _error("Choose an existing study.", "invalid", "study")
        data["version"] = _text(get("version"), "version", 20, required=True, label="a version")
        data["title"] = _text(get("title"), "title", 160, required=True, label="a title")
        data["kind"] = _choice(get("kind", "study"), ConsentForm.Kind.choices, "kind", "a kind")
        categories = get("categories", [])
        ids = {str(getattr(c, "pk", c)) for c in categories}
        found = list(ConsentScopeCategory.objects.filter(pk__in=ids))
        if len(found) != len(ids):
            raise _error("Choose existing scope categories.", "invalid", "categories")
        if not found:
            raise _error("Choose the categories this form asks about.", "required", "categories")
        data["categories"] = sorted(found, key=lambda c: c.code)
    elif kind == "institution":
        data["name"] = _text(get("name"), "name", 160, required=True, label="a name")
        data["address"] = _text(get("address"), "address", 500)
        country = _text(get("country"), "country", 2).upper()
        if country and not (country.isascii() and country.isalpha() and len(country) == 2):
            raise _error("Use a two-letter country code, e.g. US.", "invalid", "country")
        data["country"] = country
    elif kind == "investigator":
        data["name"] = _text(get("name"), "name", 160, required=True, label="a name")
        institution = get("institution", None)
        institution_id = getattr(institution, "pk", institution)
        data["institution"] = Institution.objects.filter(pk=institution_id).first()
        if data["institution"] is None:
            raise _error("Choose the institution.", "required", "institution")
        if not data["institution"].is_active and (
            obj is None or obj.institution_id != data["institution"].pk
        ):
            raise _error("This institution is inactive.", "invalid", "institution")
        email = _text(get("email"), "email", 254)
        if email:
            from django.core.validators import validate_email

            try:
                validate_email(email)
            except ValidationError:
                raise _error("Enter a valid email address.", "invalid", "email") from None
        data["email"] = email
        data["phone"] = _text(get("phone"), "phone", 40)
    return data


def _catalogue_state(kind, obj):
    data = state(obj, CATALOGUE_FIELDS[kind])
    return data


@storage.operation()
def save_distribution_catalogue(*, actor, kind, object_id, expected_revision, values, reason=""):
    """Create (object_id None) or edit a scope category, consent form, institution or
    investigator. Identifying fields are fixed once records use the entry."""
    require_role(actor, *MANAGE_ROLES)
    if kind not in CATALOGUES:
        raise _error("Unknown catalogue.", "invalid")
    model = CATALOGUES[kind]
    noun = CATALOGUE_NOUNS[kind]
    obj = None
    if object_id is not None:
        obj = _lock(model, object_id, noun)
        _check_revision(obj, expected_revision, noun)
        reason = need_reason(reason)
    else:
        reason = (reason or "").strip() or INITIAL_REASON
    data = _catalogue_values(kind, values, obj)
    if obj is not None and catalogue_in_use(kind, obj):
        fixed = {
            "category": ("code", "gates_use"),
            "consent_form": ("study", "code", "version", "categories"),
            "institution": ("code",),
            "investigator": ("code", "institution"),
        }[kind]
        for name in fixed:
            current = getattr(obj, name)
            if name == "categories":
                current = sorted(current.all(), key=lambda c: c.code)
            if current != data[name]:
                raise _error(
                    "This is fixed because recorded data already uses this entry.",
                    "invalid",
                    name,
                )
    before = _catalogue_state(kind, obj) if obj else {}
    categories = data.pop("categories", None)
    if obj is None:
        obj = model(created_by=actor, **data)
    else:
        for name, value in data.items():
            setattr(obj, name, value)
        obj.revision += 1
    message = (
        "This version already exists for the study."
        if kind == "consent_form"
        else "This code is already in use."
    )
    _save_unique(obj, "version" if kind == "consent_form" else "code", message)
    if categories is not None:
        obj.categories.set(categories)
    _audit(
        actor,
        f"distribution.{kind}_saved",
        obj,
        before=before,
        after=_catalogue_state(kind, obj),
        reason=reason,
    )
    return obj


# ------------------------------------------------------------------ studies

STUDY_FIELDS = (
    "code",
    "title",
    "description",
    "principal_investigator",
    "ethics_reference",
    "ethics_approved_until",
    "consent_basis",
    "determination_reference",
    "status",
)


@storage.operation()
def save_study(*, actor, study_id, expected_revision, values, reason=""):
    """Create or edit a study. Suspending or closing it blocks distribution of its material
    (the gate re-evaluates); nothing else changes."""
    require_role(actor, *MANAGE_ROLES)
    unknown = set(values) - set(STUDY_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {sorted(unknown)[0]}.", "invalid")
    storage.lock_gate()
    study = None
    if study_id is not None:
        study = _lock(Study, study_id, "study")
        _check_revision(study, expected_revision, "study")
        reason = need_reason(reason)
    else:
        reason = (reason or "").strip() or INITIAL_REASON

    def get(name, default=""):
        return values[name] if name in values else (getattr(study, name) if study else default)

    code = storage.normalize_code(get("code"))
    if study is not None and code != study.code:
        if (
            study.consents.exists()
            or study.collection_links.exists()
            or study.consent_forms.exists()
        ):
            raise _error("The code is fixed once the study has records.", "invalid", "code")
    data = {
        "code": code,
        "title": _text(get("title"), "title", 160, required=True, label="a title"),
        "description": _text(get("description"), "description"),
        "principal_investigator": _text(
            get("principal_investigator"), "principal_investigator", 160
        ),
        "ethics_reference": _text(get("ethics_reference"), "ethics_reference", 80),
        "ethics_approved_until": _day(
            get("ethics_approved_until", None),
            "ethics_approved_until",
            required=False,
            past=False,
        ),
        "consent_basis": _choice(
            get("consent_basis", Study.Basis.RECORDS),
            Study.Basis.choices,
            "consent_basis",
            "the consent basis",
        ),
        "determination_reference": _text(
            get("determination_reference"), "determination_reference", 160
        ),
        "status": _choice(
            get("status", Study.Status.ACTIVE), Study.Status.choices, "status", "a status"
        ),
    }
    if data["consent_basis"] == Study.Basis.DETERMINATION and not data["determination_reference"]:
        raise _error(
            "Give the reference of the determination (for example the ethics committee's "
            "letter) that this study's use rests on.",
            "required",
            "determination_reference",
        )
    before = state(study, STUDY_FIELDS) if study else {}
    if study is None:
        study = Study(created_by=actor, **data)
    else:
        for name, value in data.items():
            setattr(study, name, value)
        study.revision += 1
    _save_unique(study, "code", "This code is already in use.")
    _audit(
        actor,
        "distribution.study_saved",
        study,
        before=before,
        after=state(study, STUDY_FIELDS),
        reason=reason,
    )
    return study


@storage.operation()
def link_collection(*, actor, study_id, expected_revision, collection_id, reason):
    """Govern a collection under the study (one current study per collection)."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    study = _lock(Study, study_id, "study")
    _check_revision(study, expected_revision, "study")
    collection = Collection.objects.filter(pk=collection_id).first() if collection_id else None
    if collection is None:
        raise _error("Choose a collection.", "required", "collection")
    current = (
        StudyCollection.objects.filter(collection=collection, ended_at__isnull=True)
        .select_related("study")
        .first()
    )
    if current is not None:
        raise _error(
            f"Collection {collection.code} is already governed under study {current.study.code}. "
            "End that link first.",
            "invalid_transition",
            "collection",
        )
    link = StudyCollection(study=study, collection=collection, linked_by=actor, link_reason=reason)
    _save_unique(link, "collection", "This collection already has a current study.")
    _bump(study)
    _audit(
        actor,
        "distribution.collection_linked",
        study,
        after={"collection": collection.code, "link": str(link.pk)},
        reason=reason,
    )
    return link


@storage.operation()
def end_collection_link(*, actor, link_id, expected_revision, reason):
    """End a collection's link to a study; refused while open allocations depend on it."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    study_id = StudyCollection.objects.values_list("study_id", flat=True).get(pk=link_id)
    study = _lock(Study, study_id, "study")
    _check_revision(study, expected_revision, "study")
    link = StudyCollection.objects.select_for_update().get(pk=link_id)
    if link.ended_at is not None:
        raise _error("This link has already ended.", "invalid_transition")
    open_count = Allocation.objects.filter(
        state__in=Allocation.OPEN, specimen__collection_id=link.collection_id
    ).count()
    if open_count:
        raise _error(
            f"{open_count} specimen(s) of this collection are allocated or picked for requests. "
            "Release or ship them first.",
            "invalid_transition",
        )
    link.ended_by = actor
    link.ended_at = tz.now()
    link.end_reason = reason
    link.save(update_fields=["ended_by", "ended_at", "end_reason"])
    _bump(study)
    _audit(
        actor,
        "distribution.collection_unlinked",
        study,
        before={"collection": str(link.collection_id), "link": str(link.pk)},
        after={"ended": True},
        reason=reason,
    )
    return link


# ------------------------------------------------------------------ consent

RECORD_FIELDS = (
    "form",
    "decision",
    "consented_on",
    "signer",
    "documentation",
    "source_reference",
    "restrictions",
    "expires_on",
)


def _record_values(consent_study, values):
    form_value = values.get("form")
    form_id = getattr(form_value, "pk", form_value)
    form = ConsentForm.objects.select_for_update().filter(pk=form_id).first() if form_id else None
    if form is None:
        raise _error("Choose the consent form.", "required", "form")
    if form.study_id != consent_study.pk:
        raise _error(
            f"Form {form} belongs to another study. Choose a form of {consent_study.code}.",
            "invalid",
            "form",
        )
    decision = _choice(
        values.get("decision"), ConsentRecord.Decision.choices, "decision", "the decision"
    )
    consented_on = _day(values.get("consented_on"), "consented_on", label="the date")
    expires_on = _day(values.get("expires_on"), "expires_on", required=False, past=False)
    if expires_on is not None and expires_on < consented_on:
        raise _error("The expiry date is before the date of the decision.", "invalid", "expires_on")
    return {
        "form": form,
        "decision": decision,
        "consented_on": consented_on,
        "signer": _choice(
            values.get("signer"), ConsentRecord.Signer.choices, "signer", "who decided"
        ),
        "documentation": _choice(
            values.get("documentation"),
            ConsentRecord.Documentation.choices,
            "documentation",
            "how it was documented",
        ),
        "source_reference": _text(values.get("source_reference"), "source_reference", 80),
        "restrictions": _text(values.get("restrictions"), "restrictions", 2000),
        "expires_on": expires_on,
    }


def _answers(form, decision, answers):
    categories = {str(c.pk): c for c in form.categories.all()}
    given = {str(k): v for k, v in (answers or {}).items()}
    if decision == ConsentRecord.Decision.REFUSED:
        if any(given.values()):
            raise _error(
                "A refusal has no category answers: the participant refused the whole form.",
                "invalid",
                "answers",
            )
        return []
    if set(given) - set(categories):
        raise _error("Answer only the categories this form asks.", "invalid", "answers")
    missing = sorted(c.code for k, c in categories.items() if given.get(k) in (None, ""))
    if missing:
        raise _error(
            f"Record an answer for every category the form asks: {', '.join(missing)}.",
            "required",
            "answers",
        )
    result = []
    for key, category in categories.items():
        _choice(given[key], ConsentAnswer.Answer.choices, "answers", "permitted or refused")
        result.append((category, given[key]))
    return result


def _record_snapshot(record):
    return _json(
        {
            **{
                name: getattr(record, record._meta.get_field(name).attname)
                for name in RECORD_FIELDS
            },
            "answers": {
                a.category.code: a.answer for a in record.answers.select_related("category")
            },
            "corrects": record.corrects_id,
        }
    )


def _write_record(actor, consent, data, answers, reason, corrects=None):
    record = ConsentRecord.objects.create(
        consent=consent, recorded_by=actor, reason=reason, corrects=corrects, **data
    )
    ConsentAnswer.objects.bulk_create(
        [ConsentAnswer(record=record, category=c, answer=a) for c, a in answers]
    )
    return record


def open_withdrawal(consent):
    """The withdrawal that currently stops use (no_use or destroy, not reinstated), or None."""
    return (
        consent.withdrawals.filter(
            kind__in=(ConsentWithdrawal.Kind.NO_USE, ConsentWithdrawal.Kind.DESTROY),
            reinstatement__isnull=True,
        )
        .order_by("-recorded_at", "-id")
        .first()
    )


@storage.operation()
def record_consent(
    *,
    actor,
    participant_id,
    study_id,
    form_id,
    decision,
    answers,
    consented_on,
    signer,
    documentation,
    source_reference,
    restrictions,
    expires_on,
    expected_revision,
    reason,
):
    """Record a participant's consent decision on a form (first consent or re-consent).

    The newest record governs. While the consent is withdrawn the record is kept but the status
    stays withdrawn until a reinstatement by another person.
    """
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    participant = Participant.objects.filter(pk=participant_id).first() if participant_id else None
    if participant is None:
        raise _error("Choose a coded participant.", "required", "participant")
    study = Study.objects.filter(pk=study_id).first() if study_id else None
    if study is None:
        raise _error("Choose the study.", "required", "study")
    consent = (
        Consent.objects.select_for_update(of=("self",))
        .filter(participant=participant, study=study)
        .first()
    )
    if consent is None:
        if expected_revision is not None:
            raise _error("This consent changed since you opened it.", "stale_revision")
    else:
        _check_revision(consent, expected_revision, "consent")
    data = _record_values(
        study,
        {
            "form": form_id,
            "decision": decision,
            "consented_on": consented_on,
            "signer": signer,
            "documentation": documentation,
            "source_reference": source_reference,
            "restrictions": restrictions,
            "expires_on": expires_on,
        },
    )
    if not data["form"].is_active:
        raise _error("This form version is inactive. Choose the current one.", "invalid", "form")
    answer_rows = _answers(data["form"], data["decision"], answers)
    before = {}
    if consent is None:
        consent = Consent.objects.create(
            participant=participant, study=study, status=data["decision"]
        )
    else:
        before = {"status": consent.status, "current_record": str(consent.current_record_id)}
    record = _write_record(actor, consent, data, answer_rows, reason)
    consent.current_record = record
    if consent.status != Consent.Status.WITHDRAWN:
        consent.status = data["decision"]
    if before:
        _bump(consent, "current_record", "status")
    else:
        consent.save(update_fields=["current_record", "status"])
    _audit(
        actor,
        "distribution.consent_recorded",
        consent,
        before=before,
        after={"status": consent.status, "record": _record_snapshot(record)},
        reason=reason,
    )
    return record


@storage.operation()
def correct_consent_record(*, actor, record_id, expected_revision, values, answers, reason):
    """Append a corrected version of the consent's current record (transcription errors)."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    consent_id = ConsentRecord.objects.values_list("consent_id", flat=True).get(pk=record_id)
    consent = _lock(Consent, consent_id, "consent")
    _check_revision(consent, expected_revision, "consent")
    current = consent.current_record
    if current is None or str(current.pk) != str(record_id):
        raise _error(
            "Only the consent's current record can be corrected. Correct the newest record.",
            "invalid_transition",
        )
    unknown = set(values) - set(RECORD_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {sorted(unknown)[0]}.", "invalid")
    merged = {
        name: getattr(current, name if name != "form" else "form_id") for name in RECORD_FIELDS
    }
    merged.update(values)
    data = _record_values(consent.study, merged)
    if answers is None:
        if data["form"].pk == current.form_id and data["decision"] == current.decision:
            answers = {str(a.category_id): a.answer for a in current.answers.all()}
        else:
            answers = {}
    answer_rows = _answers(data["form"], data["decision"], answers)
    before = _record_snapshot(current)
    record = _write_record(actor, consent, data, answer_rows, reason, corrects=current)
    consent.current_record = record
    if consent.status != Consent.Status.WITHDRAWN:
        consent.status = data["decision"]
    _bump(consent, "current_record", "status")
    _audit(
        actor,
        "distribution.consent_corrected",
        consent,
        before=before,
        after=_record_snapshot(record),
        reason=reason,
    )
    return record


def affected_specimens(consent, statuses=("available", "exhausted")):
    """Specimens of the participant in every collection the study governs or has governed, by
    id: a withdrawal keeps following that material after a relink."""
    return Specimen.objects.filter(
        participant_id=consent.participant_id,
        collection_id__in=queries.governed_collections(consent.study_id),
        status__in=statuses,
    ).order_by("id")


def _withdrawal_reference(consent):
    return WITHDRAWAL_REFERENCE.format(
        participant=consent.participant.code, study=consent.study.code
    )[:120]


def _hold_for_withdrawal(actor, consent, withdrawal, now, reason):
    """Place a use hold on each affected specimen that has no active hold from `withdrawal`
    (None: a withdrawal being recorded now) and release its allocated (not picked) allocations.
    Returns ([(specimen_id, hold)], released allocations); the caller writes the ConsentHold
    rows once the withdrawal row exists."""
    held_ids = (
        set(
            ConsentHold.objects.filter(
                withdrawal=withdrawal, hold__release__isnull=True
            ).values_list("specimen_id", flat=True)
        )
        if withdrawal is not None
        else set()
    )
    targets = [s for s in affected_specimens(consent).only("id") if s.pk not in held_ids]
    if len(held_ids) + len(targets) > MAX_WITHDRAWAL:
        raise _error(
            f"A withdrawal holds at most {MAX_WITHDRAWAL} specimens at a time.",
            "invalid_transition",
        )
    reference = _withdrawal_reference(consent)
    hold_rows = []
    for specimen in targets:
        hold = specimens.place_linked_hold(
            actor=actor,
            specimen_id=specimen.pk,
            scope="use",
            reference=reference,
            occurred_at=now,
            reason=reason,
        )
        hold_rows.append((specimen.pk, hold))
    affected_ids = [s.pk for s in affected_specimens(consent).only("id")]
    released = _release_open(
        actor,
        Allocation.objects.filter(specimen_id__in=affected_ids, state=Allocation.State.ALLOCATED),
        now,
        f"Consent withdrawn ({consent.participant.code}, {consent.study.code}). {reason}",
    )
    return hold_rows, released


def _release_open(actor, queryset, occurred_at, reason):
    """Release allocated allocations with their holds (allocations locked in id order)."""
    rows = list(queryset.select_for_update(of=("self",)).order_by("id"))
    for allocation in rows:
        if allocation.state != Allocation.State.ALLOCATED:
            continue
        specimens.release_linked_hold(
            actor=actor, hold_id=allocation.hold_id, occurred_at=occurred_at, reason=reason
        )
        allocation.state = Allocation.State.RELEASED
        _bump(allocation, "state")
        AllocationEvent.objects.create(
            allocation=allocation,
            kind=AllocationEvent.Kind.RELEASED,
            actor=actor,
            occurred_at=occurred_at,
            reason=reason,
        )
    return rows


@storage.operation()
def withdraw_consent(
    *, actor, consent_id, expected_revision, kind, effective_on, received_via, occurred_at, reason
):
    """Record a withdrawal. `no_contact` sets the flag only. `no_use` and `destroy` set the
    status to withdrawn, place a use hold on every specimen of the participant in the study's
    current collections and release their allocated (not yet picked) allocations, all in this
    transaction. Nothing is disposed, deleted or anonymized; distributed material is counted
    for recorded follow-up."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    kind = _choice(kind, ConsentWithdrawal.Kind.choices, "kind", "what was withdrawn")
    effective_on = _day(effective_on, "effective_on", label="the effective date")
    received_via = _text(
        received_via, "received_via", 80, required=True, label="how it was received"
    )
    now = tz.now()
    occurred_at = _moment(occurred_at, label="when it was received", now=now)
    storage.lock_gate()
    consent = _lock(Consent, consent_id, "consent")
    _check_revision(consent, expected_revision, "consent")
    stops_use = kind != ConsentWithdrawal.Kind.NO_CONTACT
    if stops_use and consent.status == Consent.Status.WITHDRAWN:
        raise _error(
            "This consent is already withdrawn. Its holds are in place; use Hold withdrawn "
            "specimens for material registered since.",
            "invalid_transition",
        )
    before = {"status": consent.status, "no_contact": consent.no_contact}
    holds, released = [], []
    if stops_use:
        holds, released = _hold_for_withdrawal(actor, consent, None, now, reason)
        consent.status = Consent.Status.WITHDRAWN
    # The counts are a snapshot of the effects, written once with the append-only row.
    withdrawal = ConsentWithdrawal.objects.create(
        consent=consent,
        kind=kind,
        effective_on=effective_on,
        received_via=received_via,
        actor=actor,
        occurred_at=occurred_at,
        reason=reason,
        held_count=len(holds),
        released_count=len(released),
        distributed_count=affected_specimens(consent, ("distributed",)).count(),
    )
    ConsentHold.objects.bulk_create(
        [ConsentHold(withdrawal=withdrawal, specimen_id=sid, hold=h) for sid, h in holds]
    )
    consent.no_contact = True
    _bump(consent, "status", "no_contact")
    _audit(
        actor,
        "distribution.consent_withdrawn",
        consent,
        before=before,
        after={
            "status": consent.status,
            "no_contact": True,
            "withdrawal": str(withdrawal.pk),
            "kind": kind,
            "held": [str(sid) for sid, _ in holds],
            "released_allocations": [str(a.pk) for a in released],
            "distributed": withdrawal.distributed_count,
        },
        reason=reason,
    )
    return withdrawal


@storage.operation()
def hold_withdrawn_specimens(*, actor, consent_id, expected_revision, occurred_at=None, reason):
    """Hold specimens of a withdrawn consent that have no hold from its withdrawal yet (for
    example registered after the withdrawal). Idempotent."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    now = tz.now()
    storage.lock_gate()
    consent = _lock(Consent, consent_id, "consent")
    _check_revision(consent, expected_revision, "consent")
    withdrawal = open_withdrawal(consent)
    if withdrawal is None:
        raise _error("This consent has no withdrawal that stops use.", "invalid_transition")
    holds, released = _hold_for_withdrawal(actor, consent, withdrawal, now, reason)
    rows = ConsentHold.objects.bulk_create(
        [ConsentHold(withdrawal=withdrawal, specimen_id=sid, hold=h) for sid, h in holds]
    )
    _bump(consent)
    _audit(
        actor,
        "distribution.withdrawn_specimens_held",
        consent,
        after={
            "withdrawal": str(withdrawal.pk),
            "held": [str(sid) for sid, _ in holds],
            "released_allocations": [str(a.pk) for a in released],
        },
        reason=reason,
    )
    return tuple(rows)


@storage.operation()
def request_withdrawal_disposals(
    *, actor, consent_id, expected_revision, method, destination, reason
):
    """File ordinary disposal requests for the held specimens of a `destroy` withdrawal. Each
    still needs approval by another person with disposal approval (specimens rule). Specimens
    with a disposal hold (e.g. picked for a request) or an open disposal request are skipped."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    consent = _lock(Consent, consent_id, "consent")
    _check_revision(consent, expected_revision, "consent")
    withdrawal = open_withdrawal(consent)
    if withdrawal is None or withdrawal.kind != ConsentWithdrawal.Kind.DESTROY:
        raise _error(
            "Disposal requests are offered only for a withdrawal asking for destruction.",
            "invalid_transition",
        )
    created = []
    for row in withdrawal.holds.filter(hold__release__isnull=True).order_by("specimen_id"):
        specimen = Specimen.objects.get(pk=row.specimen_id)
        if specimen.status not in (Specimen.Status.AVAILABLE, Specimen.Status.EXHAUSTED):
            continue
        if specimens.active_holds(specimen, specimens.DISPOSAL_SCOPES).exists():
            continue
        pending = [
            r
            for r in DisposalRequest.objects.filter(specimen=specimen).select_related(
                "decision", "execution"
            )
            if specimens.request_state(r, specimen) in ("pending", "approved")
        ]
        if pending:
            continue
        created.append(
            specimens.request_disposal(
                actor=actor,
                specimen_id=specimen.pk,
                expected_revision=specimen.revision,
                method=method,
                destination=destination,
                reason=reason,
            )
        )
    _bump(consent)
    _audit(
        actor,
        "distribution.withdrawal_disposals_requested",
        consent,
        after={"withdrawal": str(withdrawal.pk), "requests": [str(r.pk) for r in created]},
        reason=reason,
    )
    return tuple(created)


@storage.operation()
def reinstate_consent(*, actor, consent_id, expected_revision, record_id, occurred_at, reason):
    """End a withdrawal (new consent recorded, or the withdrawal was recorded in error) and
    release its holds. Another person than the withdrawal's recorder and the new record's
    recorder must do it; superuser status does not bypass this."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    now = tz.now()
    occurred_at = _moment(occurred_at, now=now)
    storage.lock_gate()
    consent = _lock(Consent, consent_id, "consent")
    _check_revision(consent, expected_revision, "consent")
    withdrawal = open_withdrawal(consent)
    if consent.status != Consent.Status.WITHDRAWN or withdrawal is None:
        raise _error("This consent is not withdrawn.", "invalid_transition")
    if withdrawal.actor_id == actor.pk:
        raise _error(
            "You recorded this withdrawal. Another manager must reinstate the consent.",
            "self_approval",
        )
    current = consent.current_record
    newer = current if current and current.recorded_at >= withdrawal.recorded_at else None
    if newer is not None and newer.recorded_by_id == actor.pk:
        raise _error(
            "You recorded the new consent. Another manager must reinstate it.", "self_approval"
        )
    record = None
    if record_id:
        record = ConsentRecord.objects.filter(pk=record_id, consent=consent).first()
        if record is None:
            raise _error("Choose a record of this consent.", "invalid", "record")
        if newer is None or record.pk != newer.pk:
            raise _error(
                "Choose the consent record made after the withdrawal.", "invalid", "record"
            )
    elif newer is not None:
        # "Recorded in error" would leave the history saying no new consent while the status
        # came from one: a record made after the withdrawal is named.
        raise _error(
            "A consent was recorded after the withdrawal. Reinstate with that record, or "
            "correct it first.",
            "invalid",
            "record",
        )
    reinstatement = ConsentReinstatement.objects.create(
        consent=consent,
        withdrawal=withdrawal,
        record=record,
        actor=actor,
        occurred_at=occurred_at,
        reason=reason,
    )
    released = 0
    for row in withdrawal.holds.filter(hold__release__isnull=True).order_by("specimen_id"):
        specimens.release_linked_hold(
            actor=actor,
            hold_id=row.hold_id,
            occurred_at=now,
            reason=f"Consent reinstated. {reason}",
        )
        released += 1
    # The named new record, or (recorded in error) the last record before the withdrawal.
    governing = record or current
    consent.status = governing.decision if governing else Consent.Status.REFUSED
    _bump(consent, "status")
    _audit(
        actor,
        "distribution.consent_reinstated",
        consent,
        before={"status": Consent.Status.WITHDRAWN, "withdrawal": str(withdrawal.pk)},
        after={
            "status": consent.status,
            "record": str(record.pk) if record else None,
            "holds_released": released,
        },
        reason=reason,
    )
    return reinstatement


def _consent_shipments(consent):
    return Shipment.objects.filter(
        items__specimen__participant_id=consent.participant_id,
        items__specimen__collection_id__in=StudyCollection.objects.filter(
            study_id=consent.study_id
        ).values("collection_id"),
    ).distinct()


@storage.operation()
def record_follow_up(*, actor, withdrawal_id, shipment_id, action, note, occurred_at, upload=None):
    """Record what was done about material already distributed when consent was withdrawn.
    The software sends nothing and claims no recall."""
    require_role(actor, *MANAGE_ROLES)
    action = _choice(action, WithdrawalFollowUp.Action.choices, "action", "what was done")
    note = _text(note, "note", TEXT_LIMIT, required=True, label="what happened")
    occurred_at = _moment(occurred_at)
    storage.lock_gate()
    consent_id = ConsentWithdrawal.objects.values_list("consent_id", flat=True).get(
        pk=withdrawal_id
    )
    consent = _lock(Consent, consent_id, "consent")
    withdrawal = ConsentWithdrawal.objects.get(pk=withdrawal_id)
    shipment = _consent_shipments(consent).filter(pk=shipment_id).first()
    if shipment is None:
        raise _error(
            "Choose a shipment that contained this participant's material.", "invalid", "shipment"
        )
    attachment = store_attachment(upload, actor=actor)
    row = WithdrawalFollowUp.objects.create(
        withdrawal=withdrawal,
        shipment=shipment,
        action=action,
        note=note,
        actor=actor,
        occurred_at=occurred_at,
        attachment=attachment,
    )
    _bump(consent)
    _audit(
        actor,
        "distribution.withdrawal_follow_up",
        consent,
        after={
            "withdrawal": str(withdrawal.pk),
            "shipment": shipment.number,
            "action": action,
            **_attachment_audit(row),
        },
        reason=note,
    )
    return row


# ------------------------------------------------------------------ requests

REQUEST_FIELDS = (
    "title",
    "investigator",
    "purpose",
    "ethics_reference",
    "ethics_approved_until",
    "use_categories",
    "studies",
    "data_requested",
    "needed_by",
    "received_via",
)
REQUEST_STATE = (*REQUEST_FIELDS, "institution", "agreement", "status")


def _ids(values, model, field, label, *, extra=None):
    ids = {str(getattr(v, "pk", v)) for v in (values or [])}
    for value in ids:
        _uuid(value, field, label)
    rows = list(model.objects.filter(pk__in=ids, **(extra or {})))
    if len(rows) != len(ids):
        raise _error(f"Choose existing {label}.", "invalid", field)
    return rows


def _item_values(request, items):
    if len(items) > MAX_ITEMS:
        raise _error(f"A request has at most {MAX_ITEMS} lines.", "invalid", "items")
    seen = set()
    result = []
    for spec in items:
        line = _whole(spec.line, "items", low=1, high=999, label="the line number")
        if line in seen:
            raise _error(f"Line {line} appears twice.", "invalid", "items")
        seen.add(line)
        specimen_type = SpecimenType.objects.filter(pk=spec.specimen_type_id).first()
        if specimen_type is None:
            raise _error(f"Line {line}: choose a specimen type.", "required", "items")
        count = _whole(spec.count, "items", low=1, high=500, label=f"line {line}'s count")
        unit = QuantityUnit.objects.filter(pk=spec.unit_id).first() if spec.unit_id else None
        minimum = None
        if spec.minimum_quantity not in (None, ""):
            if unit is None:
                raise _error(
                    f"Line {line}: give the unit of the minimum amount.", "required", "items"
                )
            minimum = specimens.parse_quantity(spec.minimum_quantity, unit, "items")
        elif unit is not None:
            raise _error(f"Line {line}: give the minimum amount or no unit.", "invalid", "items")
        study = Study.objects.filter(pk=spec.study_id).first() if spec.study_id else None
        if spec.study_id and study is None:
            raise _error(f"Line {line}: choose an existing study.", "invalid", "items")
        result.append(
            {
                "line": line,
                "specimen_type": specimen_type,
                "count": count,
                "minimum_quantity": minimum,
                "unit": unit,
                "study": study,
                "notes": _text(spec.notes, "items", 500),
            }
        )
    return result


def _item_state(item):
    return _json(
        {
            "line": item.line,
            "specimen_type": item.specimen_type.code,
            "count": item.count,
            "minimum_quantity": item.minimum_quantity,
            "unit": item.unit.code if item.unit_id else None,
            "study": item.study.code if item.study_id else None,
            "notes": item.notes,
        }
    )


def request_snapshot(request):
    """What a submission freezes: the request's fields, lines, categories and studies."""
    return _json(
        {
            "number": request.number,
            "title": request.title,
            "investigator": {
                "code": request.investigator.code,
                "name": request.investigator.name,
            },
            "institution": {"code": request.institution.code, "name": request.institution.name},
            "purpose": request.purpose,
            "ethics_reference": request.ethics_reference,
            "ethics_approved_until": request.ethics_approved_until,
            "use_categories": sorted(c.code for c in request.use_categories.all()),
            "studies": sorted(s.code for s in request.studies.all()),
            "data_requested": request.data_requested,
            "needed_by": request.needed_by,
            "received_via": request.received_via,
            "agreement": request.agreement.number if request.agreement_id else None,
            "items": [
                _item_state(i)
                for i in request.items.select_related("specimen_type", "unit", "study").order_by(
                    "line"
                )
            ],
        }
    )


def _lock_request(request_id, expected_revision):
    request = _lock(SpecimenRequest, request_id, "request")
    _check_revision(request, expected_revision, "request")
    return request


@storage.operation()
def save_request(*, actor, request_id, expected_revision, values, items, reason=""):
    """Create a draft request or edit a draft or returned one, with its lines. Lines an
    allocation refers to stay as they are."""
    require_role(actor, *OPERATE_ROLES)
    unknown = set(values) - set(REQUEST_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {sorted(unknown)[0]}.", "invalid")
    storage.lock_gate()
    request = None
    if request_id is not None:
        request = _lock_request(request_id, expected_revision)
        if not request.is_editable:
            raise _error("Only a draft or returned request can be edited.", "invalid_transition")
        reason = need_reason(reason)

    def get(name, default=""):
        if name in values:
            return values[name]
        if request is None:
            return default
        field = SpecimenRequest._meta.get_field(name)
        return list(getattr(request, name).all()) if field.many_to_many else getattr(request, name)

    investigator_value = get("investigator", None)
    investigator_id = getattr(investigator_value, "pk", investigator_value)
    investigator = (
        Investigator.objects.select_related("institution").filter(pk=investigator_id).first()
        if investigator_id
        else None
    )
    if investigator is None:
        raise _error("Choose the requesting investigator.", "required", "investigator")
    if not investigator.is_active and (
        request is None or request.investigator_id != investigator.pk
    ):
        raise _error("This investigator is inactive.", "invalid", "investigator")
    data = {
        "title": _text(get("title"), "title", 160, required=True, label="a title"),
        "investigator": investigator,
        "purpose": _text(get("purpose"), "purpose", TEXT_LIMIT, required=True, label="the purpose"),
        "ethics_reference": _text(get("ethics_reference"), "ethics_reference", 80),
        "ethics_approved_until": _day(
            get("ethics_approved_until", None), "ethics_approved_until", required=False, past=False
        ),
        "data_requested": _text(get("data_requested"), "data_requested"),
        "needed_by": _day(get("needed_by", None), "needed_by", required=False, past=False),
        "received_via": _choice(
            get("received_via", None),
            SpecimenRequest.ReceivedVia.choices,
            "received_via",
            "how the request was received",
        ),
    }
    categories = _ids(
        get("use_categories", []),
        ConsentScopeCategory,
        "use_categories",
        "scope categories that govern use",
        extra={"gates_use": True},
    )
    studies = _ids(get("studies", []), Study, "studies", "studies")
    item_rows = _item_values(request, items)
    institution = investigator.institution
    if request is not None and request.agreement_id and request.institution_id != institution.pk:
        raise _error(
            "The linked agreement is with another institution. Remove it before changing the "
            "investigator.",
            "invalid",
            "investigator",
        )
    before = state(request, REQUEST_STATE) if request else {}
    before_items = [_item_state(i) for i in request.items.order_by("line")] if request else []
    if request is None:
        request = SpecimenRequest.objects.create(created_by=actor, institution=institution, **data)
    else:
        for name, value in data.items():
            setattr(request, name, value)
        request.institution = institution
        _bump(request)
    request.use_categories.set(categories)
    request.studies.set(studies)
    # Lines: unreferenced ones are replaced; referenced ones must stay exactly as they are.
    existing = {i.line: i for i in request.items.select_related("specimen_type", "unit", "study")}
    referenced = set(
        Allocation.objects.filter(item__request=request).values_list("item__line", flat=True)
    )
    wanted = {row["line"]: row for row in item_rows}
    for line in referenced:
        row = wanted.get(line)
        item = existing[line]
        if row is None or any(
            getattr(item, k) != v for k, v in row.items() if k not in ("count", "notes")
        ):
            raise _error(
                f"Line {line} has allocations; its type, study and minimum amount cannot change.",
                "invalid",
                "items",
            )
        allocated = item.allocations.exclude(state=Allocation.State.RELEASED).count()
        if row["count"] < allocated:
            raise _error(
                f"Line {line} already has {allocated} allocated specimen(s).", "invalid", "items"
            )
    for line, item in existing.items():
        if line not in referenced:
            item.delete()
    for line, row in wanted.items():
        if line in referenced:
            item = existing[line]
            item.count, item.notes = row["count"], row["notes"]
            item.save(update_fields=["count", "notes"])
        else:
            RequestItem.objects.create(request=request, **row)
    after = state(request, REQUEST_STATE)
    after_items = [_item_state(i) for i in request.items.order_by("line")]
    changes = _changes(before, after)
    if before_items != after_items:
        changes["items"] = [before_items, after_items]
    RequestEvent.objects.create(
        request=request,
        kind=RequestEvent.Kind.EDITED if before else RequestEvent.Kind.CREATED,
        changes=_json(changes) if before else {},
        actor=actor,
        reason=reason if before else "",
    )
    _audit(
        actor,
        "distribution.request_saved",
        request,
        before={**before, "items": before_items} if before else {},
        after={**after, "items": after_items},
        reason=reason or "Request created",
    )
    return request


@storage.operation()
def submit_request(*, actor, request_id, expected_revision):
    """Freeze the request as a numbered submission awaiting a decision."""
    require_role(actor, *OPERATE_ROLES)
    storage.lock_gate()
    request = _lock_request(request_id, expected_revision)
    if not request.is_editable:
        raise _error("Only a draft or returned request can be submitted.", "invalid_transition")
    if not request.items.exists():
        raise _error("Add at least one line before submitting.", "invalid_transition")
    if not request.use_categories.exists():
        raise _error(
            "Choose the uses the material is requested for (scope categories) before submitting.",
            "required",
            "use_categories",
        )
    request.latest_submission += 1
    submission = RequestSubmission.objects.create(
        request=request,
        number=request.latest_submission,
        data=request_snapshot(request),
        submitted_by=actor,
    )
    before = request.status
    request.status = SpecimenRequest.Status.SUBMITTED
    _bump(request, "status", "latest_submission")
    _audit(
        actor,
        "distribution.request_submitted",
        request,
        before={"status": before},
        after={"status": request.status, "submission": submission.number},
        reason=f"Submission {submission.number}",
    )
    return submission


def decision_refusal(actor, request):
    """Why `actor` may not decide `request` now, or None (the page shows the same sentence)."""
    submission = request.submissions.filter(number=request.latest_submission).first()
    if request.created_by_id == actor.pk or (
        submission is not None and submission.submitted_by_id == actor.pk
    ):
        return (
            "You created or submitted this request. Another person with request decision "
            "permission must decide it."
        )
    return None


@storage.operation()
def decide_request(
    *,
    actor,
    request_id,
    expected_revision,
    submission_number,
    decision,
    conditions,
    committee_reference,
    conflicts_note,
    reason,
):
    """Record the access decision on the latest submission (approve, reject or return).

    Needs the explicit `approve_distribution` grant and an operating role; never the request's
    creator or submitter, superuser included. The decision is the committee's or reviewer's;
    the software records it."""
    require_role(actor, *OPERATE_ROLES)
    require_request_authority(actor)
    reason = need_reason(reason)
    decision = _choice(decision, RequestDecision.Decision.choices, "decision", "a decision")
    storage.lock_gate()
    request = _lock_request(request_id, expected_revision)
    if request.status != SpecimenRequest.Status.SUBMITTED:
        raise _error("This request is not awaiting a decision.", "invalid_transition")
    if submission_number != request.latest_submission:
        raise _error(
            "A newer submission exists. Reload the request and decide the latest one.",
            "stale_revision",
        )
    refusal = decision_refusal(actor, request)
    if refusal:
        raise _error(refusal, "self_approval")
    submission = request.submissions.get(number=submission_number)
    row = RequestDecision.objects.create(
        submission=submission,
        decision=decision,
        conditions=_text(conditions, "conditions"),
        committee_reference=_text(committee_reference, "committee_reference", 160),
        conflicts_note=_text(conflicts_note, "conflicts_note"),
        actor=actor,
        reason=reason,
    )
    status = {
        "approve": SpecimenRequest.Status.APPROVED,
        "reject": SpecimenRequest.Status.REJECTED,
        "return": SpecimenRequest.Status.RETURNED,
    }[decision]
    released, drafts = [], []
    if status == SpecimenRequest.Status.REJECTED:
        # A request reopened after allocation can be rejected on resubmission: nothing it
        # reserved may outlive it (reopening refuses picked material and packed shipments).
        released, drafts = _wind_down(
            actor, request, tz.now(), f"Request rejected. {reason}", "Request rejected."
        )
    request.status = status
    _bump(request, "status")
    _audit(
        actor,
        "distribution.request_decided",
        request,
        before={"status": SpecimenRequest.Status.SUBMITTED},
        after={
            "status": status,
            "submission": submission_number,
            "decision": decision,
            "committee_reference": row.committee_reference,
            "released_allocations": [str(a.pk) for a in released],
            "cancelled_shipments": [s.number for s in drafts],
        },
        reason=reason,
    )
    return row


def _wind_down(actor, request, now, reason, note):
    """Cancel the request's draft shipments and release its allocated (not picked) specimens
    with their holds: what a request ending without shipping leaves behind. The caller has
    refused picked material."""
    drafts = list(
        Shipment.objects.select_for_update()
        .filter(request=request, state=Shipment.State.DRAFT)
        .order_by("id")
    )
    for shipment in drafts:
        shipment.state = Shipment.State.CANCELLED
        _bump(shipment, "state")
        ShipmentEvent.objects.create(
            shipment=shipment,
            kind=ShipmentEvent.Kind.CANCELLED,
            note=f"{note} {reason}",
            actor=actor,
            occurred_at=now,
        )
    released = _release_open(
        actor, request.allocations.filter(state=Allocation.State.ALLOCATED), now, reason
    )
    return released, drafts


def _request_event(request, actor, kind, reason, changes=None):
    RequestEvent.objects.create(
        request=request, kind=kind, actor=actor, reason=reason, changes=_json(changes or {})
    )


@storage.operation()
def reopen_request(*, actor, request_id, expected_revision, reason):
    """Approved -> draft for changes; allocations stay but cannot be picked, packed or
    dispatched until a new approval."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    request = _lock_request(request_id, expected_revision)
    if request.status != SpecimenRequest.Status.APPROVED:
        raise _error("Only an approved request can be reopened.", "invalid_transition")
    if request.allocations.filter(state=Allocation.State.PICKED).exists():
        raise _error(
            "Specimens are picked for this request. Put them back first.", "invalid_transition"
        )
    if request.shipments.filter(state=Shipment.State.PACKED).exists():
        raise _error("A shipment is packed. Unpack or cancel it first.", "invalid_transition")
    request.status = SpecimenRequest.Status.DRAFT
    _bump(request, "status")
    _request_event(request, actor, RequestEvent.Kind.REOPENED, reason)
    _audit(
        actor,
        "distribution.request_reopened",
        request,
        before={"status": "approved"},
        after={"status": "draft"},
        reason=reason,
    )
    return request


@storage.operation()
def cancel_request(*, actor, request_id, expected_revision, reason):
    """Cancel a request that has nothing picked or shipped; allocations and draft shipments
    are released and cancelled with it."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    now = tz.now()
    storage.lock_gate()
    request = _lock_request(request_id, expected_revision)
    if request.status in SpecimenRequest.FINAL:
        raise _error("This request is already final.", "invalid_transition")
    if request.allocations.filter(state__in=("picked", "shipped")).exists():
        raise _error(
            "Specimens are picked or shipped for this request. Put picked specimens back first; "
            "a request with shipped material is closed, not cancelled.",
            "invalid_transition",
        )
    released, drafts = _wind_down(actor, request, now, reason, "Request cancelled.")
    before = request.status
    request.status = SpecimenRequest.Status.CANCELLED
    _bump(request, "status")
    _request_event(request, actor, RequestEvent.Kind.CANCELLED, reason)
    _audit(
        actor,
        "distribution.request_cancelled",
        request,
        before={"status": before},
        after={
            "status": "cancelled",
            "released_allocations": [str(a.pk) for a in released],
            "cancelled_shipments": [s.number for s in drafts],
        },
        reason=reason,
    )
    return request


@storage.operation()
def close_request(*, actor, request_id, expected_revision, reason):
    """Close an approved request once nothing is allocated, picked or waiting to ship."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    request = _lock_request(request_id, expected_revision)
    if request.status != SpecimenRequest.Status.APPROVED:
        raise _error("Only an approved request can be closed.", "invalid_transition")
    if request.allocations.filter(state__in=Allocation.OPEN).exists():
        raise _error(
            "Specimens are still allocated or picked. Release or ship them first.",
            "invalid_transition",
        )
    if request.shipments.filter(state__in=("draft", "packed")).exists():
        raise _error(
            "A shipment is still in preparation. Dispatch or cancel it.", "invalid_transition"
        )
    request.status = SpecimenRequest.Status.CLOSED
    _bump(request, "status")
    _request_event(request, actor, RequestEvent.Kind.CLOSED, reason)
    _audit(
        actor,
        "distribution.request_closed",
        request,
        before={"status": "approved"},
        after={"status": "closed"},
        reason=reason,
    )
    return request


@storage.operation()
def link_agreement(*, actor, request_id, expected_revision, agreement_id, reason):
    """Link (or unlink, None) the agreement covering the request. It must be with the request's
    institution; dispatch later requires it to be valid on the dispatch date."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    request = _lock_request(request_id, expected_revision)
    if request.status in SpecimenRequest.FINAL:
        raise _error("This request is final.", "invalid_transition")
    agreement = None
    if agreement_id:
        agreement = Agreement.objects.select_for_update().filter(pk=agreement_id).first()
        if agreement is None:
            raise _error("Choose an existing agreement.", "invalid", "agreement")
        if agreement.institution_id != request.institution_id:
            raise _error(
                f"{agreement.number} is with another institution than the request's "
                f"({request.institution.name}).",
                "invalid",
                "agreement",
            )
        if agreement.status == Agreement.Status.TERMINATED:
            raise _error(f"{agreement.number} is terminated.", "invalid", "agreement")
    before = request.agreement.number if request.agreement_id else None
    request.agreement = agreement
    _bump(request, "agreement")
    after = agreement.number if agreement else None
    _request_event(
        request, actor, RequestEvent.Kind.AGREEMENT_LINKED, reason, {"agreement": [before, after]}
    )
    _audit(
        actor,
        "distribution.request_agreement_linked",
        request,
        before={"agreement": before},
        after={"agreement": after},
        reason=reason,
    )
    return request


@storage.operation()
def add_request_note(*, actor, request_id, note):
    require_role(actor, *OPERATE_ROLES)
    note = _text(note, "note", TEXT_LIMIT, required=True, label="the note")
    request = SpecimenRequest.objects.get(pk=request_id)
    _request_event(request, actor, RequestEvent.Kind.NOTE, note)
    _audit(actor, "distribution.request_note", request, after={"note": note}, reason=note)
    return request


# ------------------------------------------------------------------ allocation and picking


def _gate(request, specimen_ids, stage, *, item=None, on=None, actor):
    found = queries.evaluate(request, specimen_ids, stage=stage, item=item, on=on)
    by_id = {s.pk: s for s in Specimen.objects.filter(pk__in=specimen_ids)}
    _blocked(found, by_id)


@storage.operation()
def allocate(
    *, actor, request_id, expected_revision, item_id, choices, below_minimum_ok, occurred_at, reason
):
    """Reserve specimens for one line of an approved request: an allocation row (one open
    allocation per specimen, database constraint) and a linked disposal hold. The specimen stays
    where it is; nothing in storage is reserved. All or none."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    choices = tuple(choices)
    if not 1 <= len(choices) <= MAX_LINES:
        raise _error(f"Choose from 1 to {MAX_LINES} specimens.", "required", "choices")
    occurred_at = _moment(occurred_at)
    storage.lock_gate()
    request = _lock_request(request_id, expected_revision)
    approved = (
        RequestDecision.objects.filter(
            submission__request=request, decision=RequestDecision.Decision.APPROVE
        )
        .order_by("-recorded_at")
        .values_list("recorded_at", flat=True)
        .first()
    )
    if approved is not None:
        _moment(occurred_at, floor=approved, label="when they were allocated")
    item = request.items.select_related("unit").filter(pk=item_id).first()
    if item is None:
        raise _error("Choose a line of this request.", "invalid", "item")
    ids = [_uuid(c.specimen_id, "choices", "specimen") for c in choices]
    if len(set(ids)) != len(ids):
        raise _error("A specimen is chosen twice.", "invalid", "choices")
    taken = item.allocations.exclude(state=Allocation.State.RELEASED).count()
    if taken + len(ids) > item.count:
        raise _error(
            f"Line {item.line} asks for {item.count}; {taken} already allocated.",
            "invalid",
            "choices",
        )
    found = {s.pk: s for s in Specimen.objects.select_related("unit").filter(pk__in=ids)}
    if len(found) != len(ids):
        raise _error("Choose existing specimens.", "invalid", "choices")
    for choice in sorted(choices, key=lambda c: str(c.specimen_id)):
        specimen = found[_uuid(choice.specimen_id)]
        if specimen.revision != choice.expected_revision:
            raise _error(
                f"Specimen {specimen.accession} changed since you opened this page. Reload it.",
                "stale_revision",
            )
    _gate(request, ids, "allocate", item=item, actor=actor)
    if item.minimum_quantity is not None and not below_minimum_ok:
        short = sorted(
            s.accession
            for s in found.values()
            if s.unit_id != item.unit_id or s.quantity_on_hand < item.minimum_quantity
        )
        if short:
            raise _error(
                f"Below the requested minimum of {format(item.minimum_quantity.normalize(), 'f')} "
                f"{item.unit.code} (or in another unit): {', '.join(short)}. Confirm to allocate "
                "them anyway.",
                "insufficient_quantity",
                "below_minimum_ok",
            )
    reference = ALLOCATION_REFERENCE.format(number=request.number)
    rows = []
    for specimen_id in sorted(ids, key=str):
        hold = specimens.place_linked_hold(
            actor=actor,
            specimen_id=specimen_id,
            scope="disposal",
            reference=reference,
            occurred_at=occurred_at,
            reason=reason,
        )
        allocation = Allocation(
            request=request,
            item=item,
            specimen_id=specimen_id,
            hold=hold,
            allocated_by=actor,
            allocated_at=occurred_at,
        )
        try:
            with transaction.atomic():
                allocation.save()
        except IntegrityError as exc:
            raise _error(
                f"Specimen {found[specimen_id].accession} is already allocated to a request.",
                "invalid_transition",
            ) from exc
        AllocationEvent.objects.create(
            allocation=allocation,
            kind=AllocationEvent.Kind.ALLOCATED,
            actor=actor,
            occurred_at=occurred_at,
            reason=reason,
        )
        rows.append(allocation)
    _bump(request)
    _audit(
        actor,
        "distribution.allocated",
        request,
        after={"line": item.line, "specimens": sorted(found[i].accession for i in ids)},
        reason=reason,
    )
    return tuple(rows)


def out_of_pick_custody(allocations):
    """Ids of picked allocations whose specimen is no longer in the pick's check-out (checked
    in, or checked out again, from the specimen record). Putting them back records where they
    are without another check-in."""
    allocations = [a for a in allocations if a.state == Allocation.State.PICKED]
    if not allocations:
        return set()
    custody = {}
    for event in AllocationEvent.objects.filter(
        allocation__in=allocations, kind=AllocationEvent.Kind.PICKED
    ).order_by("recorded_at", "id"):
        custody[event.allocation_id] = event.custody_event_id
    checkouts = dict(
        Specimen.objects.filter(pk__in=[a.specimen_id for a in allocations]).values_list(
            "id", "open_checkout_id"
        )
    )
    return {a.pk for a in allocations if checkouts.get(a.specimen_id) != custody.get(a.pk)}


def _lock_allocations(request, ids, field="allocations"):
    ids = [_uuid(i, field, "allocation") for i in ids]
    if len(set(ids)) != len(ids):
        raise _error("An allocation is listed twice.", "invalid", field)
    rows = list(
        Allocation.objects.select_for_update(of=("self",))
        .filter(pk__in=ids, request=request)
        .order_by("id")
    )
    if len(rows) != len(ids):
        raise _error("Choose allocations of this request.", "invalid", field)
    return {r.pk: r for r in rows}


@storage.operation()
def release_allocations(
    *, actor, request_id, expected_revision, allocation_ids, occurred_at, reason
):
    """Release allocated (not picked) specimens from the request; their holds are released."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    allocation_ids = tuple(allocation_ids)
    if not 1 <= len(allocation_ids) <= MAX_LINES:
        raise _error(f"Choose from 1 to {MAX_LINES} allocations.", "required", "allocations")
    occurred_at = _moment(occurred_at)
    storage.lock_gate()
    request = _lock_request(request_id, expected_revision)
    rows = _lock_allocations(request, allocation_ids)
    if any(r.state != Allocation.State.ALLOCATED for r in rows.values()):
        raise _error(
            "Only allocated specimens can be released here; put picked ones back first.",
            "invalid_transition",
        )
    _release_open(actor, Allocation.objects.filter(pk__in=list(rows)), occurred_at, reason)
    _bump(request)
    _audit(
        actor,
        "distribution.allocations_released",
        request,
        after={"allocations": sorted(str(i) for i in rows)},
        reason=reason,
    )
    return tuple(Allocation.objects.filter(pk__in=list(rows)).order_by("id"))


@storage.operation()
def pick(*, actor, request_id, expected_revision, lines, recipient_id, occurred_at, reason):
    """Retrieve allocated specimens: each found line is an ordinary specimens check-out to the
    picker (the position becomes free); a line marked missing records `not_found` only."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    lines = tuple(lines)
    if not 1 <= len(lines) <= MAX_LINES:
        raise _error(f"Pick from 1 to {MAX_LINES} lines at a time.", "required", "lines")
    occurred_at = _moment(occurred_at)
    recipient = _staff(recipient_id, "recipient_id", OPERATE_ROLES)
    storage.lock_gate()
    request = _lock_request(request_id, expected_revision)
    rows = _lock_allocations(request, [line.allocation_id for line in lines], "lines")
    if any(r.state != Allocation.State.ALLOCATED for r in rows.values()):
        raise _error("Only allocated specimens can be picked.", "invalid_transition")
    _moment(occurred_at, floor=max(r.allocated_at for r in rows.values()))
    found = [rows[_uuid(line.allocation_id)].specimen_id for line in lines if not line.missing]
    if found:
        _gate(request, found, "pick", actor=actor)
    destination = f"Request {request.number}"
    picked = []
    for line in sorted(lines, key=lambda x: str(rows[_uuid(x.allocation_id)].specimen_id)):
        allocation = rows[_uuid(line.allocation_id)]
        note = _text(line.note, "lines", 500)
        if line.missing:
            AllocationEvent.objects.create(
                allocation=allocation,
                kind=AllocationEvent.Kind.NOT_FOUND,
                actor=actor,
                occurred_at=occurred_at,
                reason=f"{note} {reason}".strip() if note else reason,
            )
            continue
        specimen = specimens.check_out(
            actor=actor,
            specimen_id=allocation.specimen_id,
            expected_revision=line.expected_revision,
            expected_occupancy_revision=line.expected_occupancy_revision,
            recipient_id=recipient.pk,
            destination=destination,
            occurred_at=occurred_at,
            reason=reason,
        )
        allocation.state = Allocation.State.PICKED
        _bump(allocation, "state")
        AllocationEvent.objects.create(
            allocation=allocation,
            kind=AllocationEvent.Kind.PICKED,
            custody_event_id=specimen.open_checkout_id,
            actor=actor,
            occurred_at=occurred_at,
            reason=reason,
        )
        picked.append(allocation)
    _bump(request)
    _audit(
        actor,
        "distribution.picked",
        request,
        after={
            "picked": sorted(str(a.specimen_id) for a in picked),
            "not_found": sorted(
                str(rows[_uuid(x.allocation_id)].specimen_id) for x in lines if x.missing
            ),
            "recipient": recipient.pk,
        },
        reason=reason,
    )
    return tuple(rows[_uuid(x.allocation_id)] for x in lines)


@storage.operation()
def put_back(
    *, actor, request_id, expected_revision, lines, recipient_id, release, occurred_at, reason
):
    """Return picked specimens to storage (ordinary check-in to a free position). They stay
    allocated, or are released with their hold when `release` (managers)."""
    require_role(actor, *OPERATE_ROLES)
    if release:
        require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    lines = tuple(lines)
    if not 1 <= len(lines) <= MAX_LINES:
        raise _error(f"Put back from 1 to {MAX_LINES} lines at a time.", "required", "lines")
    occurred_at = _moment(occurred_at)
    recipient = _staff(recipient_id, "recipient_id", OPERATE_ROLES)
    storage.lock_gate()
    request = _lock_request(request_id, expected_revision)
    rows = _lock_allocations(request, [line.allocation_id for line in lines], "lines")
    if any(r.state != Allocation.State.PICKED for r in rows.values()):
        raise _error("Only picked specimens can be put back.", "invalid_transition")
    shipments = {
        s.pk: s
        for s in Shipment.objects.select_for_update().filter(
            pk__in={r.shipment_id for r in rows.values() if r.shipment_id}
        )
    }
    if any(s.state == Shipment.State.PACKED for s in shipments.values()):
        raise _error(
            "A specimen is in a packed shipment. Unpack the shipment first.", "invalid_transition"
        )
    elsewhere = out_of_pick_custody(rows.values())
    for line in sorted(lines, key=lambda x: str(rows[_uuid(x.allocation_id)].specimen_id)):
        allocation = rows[_uuid(line.allocation_id)]
        if allocation.pk in elsewhere:
            # Checked in from the specimen record since the pick: record it, check in nothing.
            specimen = Specimen.objects.get(pk=allocation.specimen_id)
            note = "Already checked in from the specimen record; recorded here."
        else:
            if not line.position_id:
                raise _error("Choose the box and position to put it in.", "required", "lines")
            specimen = specimens.check_in(
                actor=actor,
                specimen_id=allocation.specimen_id,
                expected_revision=line.expected_revision,
                expected_occupancy_revision=line.expected_occupancy_revision,
                position_id=line.position_id,
                recipient_id=recipient.pk,
                thaw_observed=line.thaw_observed,
                occurred_at=occurred_at,
                reason=reason,
            )
            note = ""
        event = (
            CustodyEvent.objects.filter(specimen_id=specimen.pk, kind=CustodyEvent.Kind.CHECK_IN)
            .order_by("-recorded_at", "-id")
            .first()
        )
        if allocation.shipment_id:
            shipment = shipments[allocation.shipment_id]
            ShipmentEvent.objects.create(
                shipment=shipment,
                kind=ShipmentEvent.Kind.CONTENTS,
                note=f"{specimen.accession} put back in storage.",
                changes={"removed": [specimen.accession]},
                actor=actor,
                occurred_at=occurred_at,
            )
            allocation.shipment = None
        AllocationEvent.objects.create(
            allocation=allocation,
            kind=AllocationEvent.Kind.PUT_BACK,
            custody_event=event,
            actor=actor,
            occurred_at=occurred_at,
            reason=f"{note} {reason}" if note else reason,
        )
        if release:
            specimens.release_linked_hold(
                actor=actor, hold_id=allocation.hold_id, occurred_at=occurred_at, reason=reason
            )
            allocation.state = Allocation.State.RELEASED
            AllocationEvent.objects.create(
                allocation=allocation,
                kind=AllocationEvent.Kind.RELEASED,
                actor=actor,
                occurred_at=occurred_at,
                reason=reason,
            )
        else:
            allocation.state = Allocation.State.ALLOCATED
        _bump(allocation, "state", "shipment")
    for shipment in shipments.values():
        _bump(shipment)
    _bump(request)
    _audit(
        actor,
        "distribution.put_back",
        request,
        after={
            "specimens": sorted(str(r.specimen_id) for r in rows.values()),
            "already_in_storage": sorted(str(rows[i].specimen_id) for i in elsewhere),
            "released": bool(release),
        },
        reason=reason,
    )
    return tuple(rows[_uuid(x.allocation_id)] for x in lines)


# ------------------------------------------------------------------ shipments

SHIPMENT_DRAFT_FIELDS = (
    "ship_to_name",
    "ship_to_institution",
    "ship_to_address",
    "ship_to_contact",
    "planned_on",
    "courier",
    "service_level",
    "tracking",
    "storage_instructions",
)
SHIPMENT_PACKED_FIELDS = ("planned_on", "courier", "service_level", "tracking")
SHIPMENT_STATE = (
    *SHIPMENT_DRAFT_FIELDS,
    "state",
    "handed_to",
    "temperature_class",
    "refrigerant",
    "dry_ice_kg",
    "classification",
    "classification_note",
    "package_count",
    "emergency_contact",
    "logger_ids",
    "include_participant_codes",
    "training_confirmed",
    "packed_by",
    "packed_at",
    "checked_by",
    "dispatched_by",
    "dispatched_at",
)
SHIPMENT_LIMITS = {
    "ship_to_name": (160, "the recipient"),
    "ship_to_institution": (160, "the institution"),
    "ship_to_address": (500, "the address"),
    "ship_to_contact": (160, "a contact"),
    "courier": (80, "the courier"),
    "service_level": (80, "the service"),
    "tracking": (80, "the tracking number"),
    "storage_instructions": (500, "storage instructions"),
}


def _shipment_values(values, allowed, shipment=None):
    unknown = set(values) - set(allowed)
    if unknown:
        raise _error(f"Unknown or fixed field: {sorted(unknown)[0]}.", "invalid")
    data = {}
    for name in allowed:
        if name not in values:
            continue
        if name == "planned_on":
            data[name] = _day(values[name], name, required=False, past=False)
            continue
        limit, label = SHIPMENT_LIMITS[name]
        required = name in ("ship_to_name", "ship_to_institution", "ship_to_address")
        data[name] = _text(values[name], name, limit, required=required, label=label)
    return data


def _lock_shipment(shipment_id, expected_revision, *, request=True):
    """Lock the shipment's request, then the shipment (contract order)."""
    request_id = Shipment.objects.values_list("request_id", flat=True).get(pk=shipment_id)
    locked_request = _lock(SpecimenRequest, request_id, "request") if request else None
    shipment = _lock(Shipment, shipment_id, "shipment")
    if expected_revision is not ...:
        _check_revision(shipment, expected_revision, "shipment")
    return locked_request, shipment


def _shipment_event(shipment, actor, kind, occurred_at, *, note="", changes=None, attachment=None):
    return ShipmentEvent.objects.create(
        shipment=shipment,
        kind=kind,
        note=note,
        changes=_json(changes or {}),
        actor=actor,
        occurred_at=occurred_at,
        attachment=attachment,
    )


@storage.operation()
def create_shipment(*, actor, request_id, expected_revision, values, reason=""):
    """Start a draft shipment for an approved request; ship-to copied from the investigator and
    institution and editable."""
    require_role(actor, *OPERATE_ROLES)
    storage.lock_gate()
    request = _lock_request(request_id, expected_revision)
    if request.status != SpecimenRequest.Status.APPROVED:
        raise _error("Shipments are prepared for approved requests only.", "invalid_transition")
    investigator = request.investigator
    contact = " · ".join(v for v in (investigator.email, investigator.phone) if v)
    defaults = {
        "ship_to_name": investigator.name,
        "ship_to_institution": request.institution.name,
        "ship_to_address": request.institution.address,
        "ship_to_contact": contact[:160],
    }
    data = _shipment_values({**defaults, **values}, SHIPMENT_DRAFT_FIELDS)
    now = tz.now()
    shipment = Shipment.objects.create(request=request, created_by=actor, **data)
    _shipment_event(shipment, actor, ShipmentEvent.Kind.CREATED, now, note=(reason or "").strip())
    _bump(request)
    _audit(
        actor,
        "distribution.shipment_created",
        shipment,
        after=state(shipment, SHIPMENT_STATE),
        reason=(reason or "").strip() or "Shipment created",
    )
    return shipment


@storage.operation()
def edit_shipment(*, actor, shipment_id, expected_revision, values, reason):
    """Change logistics: every ship-to and courier field while draft; courier, service,
    tracking and planned date once packed."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    _, shipment = _lock_shipment(shipment_id, expected_revision)
    if shipment.state == Shipment.State.DRAFT:
        allowed = SHIPMENT_DRAFT_FIELDS
    elif shipment.state == Shipment.State.PACKED:
        allowed = SHIPMENT_PACKED_FIELDS
    else:
        raise _error("A dispatched or cancelled shipment cannot be edited.", "invalid_transition")
    data = _shipment_values(values, allowed)
    before = state(shipment, SHIPMENT_STATE)
    for name, value in data.items():
        setattr(shipment, name, value)
    _bump(shipment)
    after = state(shipment, SHIPMENT_STATE)
    _shipment_event(
        shipment,
        actor,
        ShipmentEvent.Kind.EDITED,
        tz.now(),
        note=reason,
        changes=_changes(before, after),
    )
    _audit(
        actor, "distribution.shipment_edited", shipment, before=before, after=after, reason=reason
    )
    return shipment


@storage.operation()
def set_shipment_contents(*, actor, shipment_id, expected_revision, allocation_ids, reason=""):
    """Choose which picked specimens of the request go in this draft shipment."""
    require_role(actor, *OPERATE_ROLES)
    allocation_ids = tuple(allocation_ids)
    if len(allocation_ids) > MAX_LINES:
        raise _error(f"A shipment holds at most {MAX_LINES} specimens.", "invalid", "allocations")
    storage.lock_gate()
    request, shipment = _lock_shipment(shipment_id, expected_revision)
    if shipment.state != Shipment.State.DRAFT:
        raise _error("Only a draft shipment's contents can change.", "invalid_transition")
    wanted = _lock_allocations(request, allocation_ids) if allocation_ids else {}
    for allocation in wanted.values():
        if allocation.state != Allocation.State.PICKED:
            raise _error(
                "Only picked specimens can be added to a shipment.", "invalid", "allocations"
            )
        if allocation.shipment_id not in (None, shipment.pk):
            raise _error(
                "A specimen is already in another shipment of this request.",
                "invalid",
                "allocations",
            )
    current = {
        a.pk: a
        for a in Allocation.objects.select_for_update(of=("self",))
        .filter(shipment=shipment)
        .order_by("id")
    }
    added = [a for k, a in wanted.items() if k not in current]
    removed = [a for k, a in current.items() if k not in wanted]
    for allocation in removed:
        allocation.shipment = None
        _bump(allocation, "shipment")
    for allocation in added:
        allocation.shipment = shipment
        _bump(allocation, "shipment")
    accession = dict(
        Specimen.objects.filter(pk__in=[a.specimen_id for a in added + removed]).values_list(
            "id", "accession"
        )
    )
    changes = {
        "added": sorted(accession[a.specimen_id] for a in added),
        "removed": sorted(accession[a.specimen_id] for a in removed),
    }
    if added or removed:
        _bump(shipment)
        _shipment_event(
            shipment,
            actor,
            ShipmentEvent.Kind.CONTENTS,
            tz.now(),
            note=(reason or "").strip(),
            changes=changes,
        )
        _audit(
            actor,
            "distribution.shipment_contents",
            shipment,
            after=changes,
            reason=(reason or "").strip() or "Contents changed",
        )
    return shipment


def _declaration(declaration):
    d = declaration
    temperature = _choice(
        d.temperature_class, Shipment.Temperature.choices, "temperature_class", "the temperature"
    )
    refrigerant = _choice(
        d.refrigerant, Shipment.Refrigerant.choices, "refrigerant", "the refrigerant"
    )
    dry_ice = None
    if refrigerant == Shipment.Refrigerant.DRY_ICE:
        if d.dry_ice_kg in (None, "") or isinstance(d.dry_ice_kg, (float, bool)):
            raise _error("Give the net dry ice mass in kg.", "required", "dry_ice_kg")
        try:
            dry_ice = Decimal(str(d.dry_ice_kg))
        except InvalidOperation:
            raise _error("Give the dry ice mass as a number.", "invalid", "dry_ice_kg") from None
        if (
            not dry_ice.is_finite()
            or dry_ice <= 0
            or dry_ice >= 10000
            or dry_ice.as_tuple().exponent < -2
        ):
            raise _error(
                "Give the dry ice mass in kg, above 0, with at most two decimals.",
                "invalid",
                "dry_ice_kg",
            )
    elif d.dry_ice_kg not in (None, ""):
        raise _error(
            "Dry ice mass applies only when the refrigerant is dry ice.", "invalid", "dry_ice_kg"
        )
    if d.training_confirmed not in (True, False):
        raise _error(
            "Say whether the packer confirmed current shipping training.",
            "required",
            "training_confirmed",
        )
    return {
        "temperature_class": temperature,
        "refrigerant": refrigerant,
        "dry_ice_kg": dry_ice,
        "classification": _choice(
            d.classification,
            Shipment.Classification.choices,
            "classification",
            "the classification the shipper declared",
        ),
        "classification_note": _text(d.classification_note, "classification_note", 160),
        "package_count": _whole(
            d.package_count, "package_count", low=1, high=99, label="the package count"
        ),
        "emergency_contact": _text(
            d.emergency_contact,
            "emergency_contact",
            160,
            required=True,
            label="the emergency contact",
        ),
        "logger_ids": _text(d.logger_ids, "logger_ids", 160),
        "storage_instructions": _text(d.storage_instructions, "storage_instructions", 500),
        "include_participant_codes": bool(d.include_participant_codes),
        "training_confirmed": d.training_confirmed,
    }


@storage.operation()
def pack_shipment(
    *, actor, shipment_id, expected_revision, declaration, checked_by_id, occurred_at, reason
):
    """Freeze the contents: every specimen is rechecked and its revision stored, so dispatch can
    refuse anything changed since. Records the declarations the shipper made; it classifies
    nothing."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    data = _declaration(declaration)
    occurred_at = _moment(occurred_at)
    checker = None
    if checked_by_id:
        checker = _staff(checked_by_id, "checked_by", OPERATE_ROLES, "an active staff member")
        if checker.pk == actor.pk:
            raise _error(
                "The second check is by another person than the packer.",
                "self_approval",
                "checked_by",
            )
    storage.lock_gate()
    request, shipment = _lock_shipment(shipment_id, expected_revision)
    if shipment.state != Shipment.State.DRAFT:
        raise _error("Only a draft shipment can be packed.", "invalid_transition")
    rows = list(
        Allocation.objects.select_for_update(of=("self",)).filter(shipment=shipment).order_by("id")
    )
    if not rows:
        raise _error("Add picked specimens before packing.", "invalid_transition")
    if any(r.state != Allocation.State.PICKED for r in rows):
        raise _error("Every specimen in the shipment must be picked.", "invalid_transition")
    last_pick = (
        AllocationEvent.objects.filter(allocation__in=rows, kind=AllocationEvent.Kind.PICKED)
        .order_by("-occurred_at")
        .values_list("occurred_at", flat=True)
        .first()
    )
    if last_pick is not None:
        _moment(occurred_at, floor=last_pick)
    _gate(request, [r.specimen_id for r in rows], "pack", actor=actor)
    if data.get("include_participant_codes"):
        codes = queries.participant_code_blocker(
            Shipment(request=request, include_participant_codes=True)
        )
        if codes is not None:
            raise _error(codes.message, "blocked", "include_participant_codes")
    revisions = dict(
        Specimen.objects.filter(pk__in=[r.specimen_id for r in rows]).values_list("id", "revision")
    )
    for allocation in rows:
        allocation.packed_revision = revisions[allocation.specimen_id]
        _bump(allocation, "packed_revision")
    before = state(shipment, SHIPMENT_STATE)
    for name, value in data.items():
        setattr(shipment, name, value)
    shipment.state = Shipment.State.PACKED
    shipment.packed_by = actor
    shipment.packed_at = occurred_at
    shipment.checked_by = checker
    _bump(shipment)
    _shipment_event(shipment, actor, ShipmentEvent.Kind.PACKED, occurred_at, note=reason)
    _audit(
        actor,
        "distribution.shipment_packed",
        shipment,
        before=before,
        after={
            **state(shipment, SHIPMENT_STATE),
            "packed_revisions": {str(k): v for k, v in revisions.items()},
        },
        reason=reason,
    )
    return shipment


@storage.operation()
def unpack_shipment(*, actor, shipment_id, expected_revision, reason):
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    _, shipment = _lock_shipment(shipment_id, expected_revision)
    if shipment.state != Shipment.State.PACKED:
        raise _error("Only a packed shipment can be unpacked.", "invalid_transition")
    for allocation in (
        Allocation.objects.select_for_update(of=("self",)).filter(shipment=shipment).order_by("id")
    ):
        allocation.packed_revision = None
        _bump(allocation, "packed_revision")
    shipment.state = Shipment.State.DRAFT
    shipment.packed_by = None
    shipment.packed_at = None
    shipment.checked_by = None
    _bump(shipment)
    _shipment_event(shipment, actor, ShipmentEvent.Kind.UNPACKED, tz.now(), note=reason)
    _audit(
        actor,
        "distribution.shipment_unpacked",
        shipment,
        before={"state": "packed"},
        after={"state": "draft"},
        reason=reason,
    )
    return shipment


@storage.operation()
def record_notification(*, actor, shipment_id, expected_revision, kind, occurred_at, note):
    """Record that the recipient was told about the shipment, or acknowledged it. Nothing is
    sent by the software."""
    require_role(actor, *OPERATE_ROLES)
    if kind not in (ShipmentEvent.Kind.NOTIFIED, ShipmentEvent.Kind.ACKNOWLEDGED):
        raise _error("Choose notified or acknowledged.", "invalid", "kind")
    note = _text(note, "note", TEXT_LIMIT)
    occurred_at = _moment(occurred_at)
    _, shipment = _lock_shipment(shipment_id, expected_revision, request=False)
    if shipment.state in (Shipment.State.RECEIVED, Shipment.State.CANCELLED):
        raise _error("This shipment is already final.", "invalid_transition")
    event = _shipment_event(shipment, actor, kind, occurred_at, note=note)
    _audit(
        actor, f"distribution.shipment_{kind}", shipment, after={"note": note}, reason=note or kind
    )
    return event


def manifest_participant_code(specimen, shipment):
    if not shipment.include_participant_codes or specimen.participant_id is None:
        return ""
    return specimen.participant.code


@storage.operation()
def dispatch_shipment(
    *, actor, shipment_id, expected_revision, courier, tracking, handed_to, occurred_at, reason
):
    """Send a packed shipment. Every specimen is rechecked (consent, holds, agreement valid on
    the dispatch date, unchanged since packing) before anything is written; then each one's
    allocation hold is released, it leaves custody (specimens `dispatch_specimen`), and an
    append-only ShipmentItem keeps what was sent."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    occurred_at = _moment(occurred_at)
    courier = _text(courier, "courier", 80)
    tracking = _text(tracking, "tracking", 80)
    handed_to = _text(handed_to, "handed_to", 160)
    storage.lock_gate()
    request, shipment = _lock_shipment(shipment_id, expected_revision)
    if request.agreement_id:
        Agreement.objects.select_for_update().get(pk=request.agreement_id)
    if shipment.state != Shipment.State.PACKED:
        raise _error("Only a packed shipment can be dispatched.", "invalid_transition")
    if occurred_at < shipment.packed_at:
        raise _error("Dispatch cannot be before packing.", "invalid_time", "occurred_at")
    rows = list(
        Allocation.objects.select_for_update(of=("self",)).filter(shipment=shipment).order_by("id")
    )
    if not rows:
        raise _error("The shipment is empty.", "invalid_transition")
    # Judged on the day it is recorded and the day it is said to have happened: an earlier
    # dispatch time cannot pass an agreement or consent that has since ended.
    _gate(
        request,
        [r.specimen_id for r in rows],
        "dispatch",
        on=(tz.localdate(), tz.localdate(occurred_at)),
        actor=actor,
    )
    codes = queries.participant_code_blocker(shipment)
    if codes is not None:
        by_id = {s.pk: s for s in Specimen.objects.filter(pk__in=[r.specimen_id for r in rows])}
        _blocked({r.specimen_id: (codes,) for r in rows}, by_id)
    specimen_rows = {
        s.pk: s
        for s in Specimen.objects.select_related(
            "specimen_type", "container_type", "unit", "participant", "collection"
        ).filter(pk__in=[r.specimen_id for r in rows])
    }
    studies = queries.governing_studies(specimen_rows.values())
    destination = f"{shipment.number} → {shipment.ship_to_institution}"[:160]
    ordered = sorted(rows, key=lambda r: specimen_rows[r.specimen_id].accession)
    for line, allocation in enumerate(ordered, start=1):
        specimen = specimen_rows[allocation.specimen_id]
        quantity = specimen.quantity_on_hand
        specimens.release_linked_hold(
            actor=actor,
            hold_id=allocation.hold_id,
            occurred_at=occurred_at,
            reason=f"Dispatched in {shipment.number}. {reason}",
        )
        revision = Specimen.objects.values_list("revision", flat=True).get(pk=specimen.pk)
        event = specimens.dispatch_specimen(
            actor=actor,
            specimen_id=specimen.pk,
            expected_revision=revision,
            destination=destination,
            occurred_at=occurred_at,
            reason=reason,
        )
        study = studies.get(specimen.pk)
        ShipmentItem.objects.create(
            shipment=shipment,
            line=line,
            allocation=allocation,
            specimen=specimen,
            accession=specimen.accession,
            specimen_type=str(specimen.specimen_type)[:210],
            container=str(specimen.container_type)[:210],
            quantity=quantity,
            unit=specimen.unit.code,
            study_code=study.code if study else "",
            participant_code=manifest_participant_code(specimen, shipment),
            specimen_revision=revision,
            custody_event=event,
        )
        allocation.state = Allocation.State.SHIPPED
        allocation.packed_revision = None
        _bump(allocation, "state", "packed_revision")
        AllocationEvent.objects.create(
            allocation=allocation,
            kind=AllocationEvent.Kind.SHIPPED,
            custody_event=event,
            actor=actor,
            occurred_at=occurred_at,
            reason=reason,
        )
    before = state(shipment, SHIPMENT_STATE)
    shipment.courier = courier or shipment.courier
    shipment.tracking = tracking or shipment.tracking
    shipment.handed_to = handed_to
    shipment.state = Shipment.State.DISPATCHED
    shipment.dispatched_by = actor
    shipment.dispatched_at = occurred_at
    _bump(shipment)
    _shipment_event(shipment, actor, ShipmentEvent.Kind.DISPATCHED, occurred_at, note=reason)
    _bump(request)
    _audit(
        actor,
        "distribution.shipment_dispatched",
        shipment,
        before=before,
        after={
            **state(shipment, SHIPMENT_STATE),
            "items": [specimen_rows[a.specimen_id].accession for a in ordered],
        },
        reason=reason,
    )
    return shipment


def _receipt_values(shipment, values, *, now):
    v = values
    condition = _choice(
        v.condition, ShipmentReceipt.Condition.choices, "condition", "the condition"
    )
    received_at = _moment(v.received_at, "received_at", label="when it was received", now=now)
    if received_at < shipment.dispatched_at:
        raise _error("Receipt cannot be before dispatch.", "invalid_time", "received_at")
    discrepancies = _text(v.discrepancies, "discrepancies")
    if condition != ShipmentReceipt.Condition.AS_EXPECTED and not discrepancies:
        raise _error("Describe what was reported.", "required", "discrepancies")
    data = {
        "received_at": received_at,
        "received_by_name": _text(
            v.received_by_name, "received_by_name", 160, required=True, label="who received it"
        ),
        "unpacked_by_name": _text(v.unpacked_by_name, "unpacked_by_name", 160),
        "condition": condition,
        "refrigerant_on_arrival": _choice(
            v.refrigerant_on_arrival,
            ShipmentReceipt.Refrigerant.choices,
            "refrigerant_on_arrival",
            "the refrigerant on arrival",
        ),
        "temperature_note": _text(v.temperature_note, "temperature_note", 160),
        "discrepancies": discrepancies,
        "received_via": _choice(
            v.received_via, ShipmentReceipt.Via.choices, "received_via", "how it was reported"
        ),
    }
    items = {str(i.pk): i for i in shipment.items.all()}
    exceptions = []
    seen = set()
    for item_id, outcome, note in v.exceptions or ():
        key = str(item_id)
        if key not in items or key in seen:
            raise _error("Choose items of this shipment, once each.", "invalid", "exceptions")
        seen.add(key)
        _choice(outcome, ReceiptException.Outcome.choices, "exceptions", "an outcome")
        exceptions.append((items[key], outcome, _text(note, "exceptions", 500)))
    return data, exceptions


def effective_receipt(shipment):
    return (
        ShipmentReceipt.objects.filter(shipment=shipment, corrected_by__isnull=True)
        .order_by("-recorded_at", "-id")
        .first()
    )


def _write_receipt(actor, shipment, data, exceptions, reason, attachment, corrects=None):
    receipt = ShipmentReceipt.objects.create(
        shipment=shipment,
        actor=actor,
        reason=reason,
        corrects=corrects,
        attachment=attachment,
        **data,
    )
    ReceiptException.objects.bulk_create(
        [ReceiptException(receipt=receipt, item=i, outcome=o, note=n) for i, o, n in exceptions]
    )
    return receipt


@storage.operation()
def record_receipt(*, actor, shipment_id, expected_revision, values, upload=None, reason):
    """Transcribe the recipient's verification. No specimen changes: dispatched material is
    outside custody, and exceptions are facts on the record."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    now = tz.now()
    _, shipment = _lock_shipment(shipment_id, expected_revision, request=False)
    if shipment.state != Shipment.State.DISPATCHED:
        raise _error(
            "Receipt is recorded once, for a dispatched shipment. Correct the recorded receipt "
            "instead.",
            "invalid_transition",
        )
    data, exceptions = _receipt_values(shipment, values, now=now)
    attachment = store_attachment(upload, actor=actor)
    receipt = _write_receipt(actor, shipment, data, exceptions, reason, attachment)
    shipment.state = Shipment.State.RECEIVED
    _bump(shipment, "state")
    _shipment_event(shipment, actor, ShipmentEvent.Kind.RECEIPT, data["received_at"], note=reason)
    _audit(
        actor,
        "distribution.receipt_recorded",
        shipment,
        before={"state": "dispatched"},
        after={
            "state": "received",
            "condition": data["condition"],
            "exceptions": [(i.accession, o) for i, o, _ in exceptions],
            **_attachment_audit(receipt),
        },
        reason=reason,
    )
    return receipt


@storage.operation()
def correct_receipt(*, actor, receipt_id, expected_revision, values, upload=None, reason):
    """Append a corrected receipt (managers). The file is kept unless a new one is attached."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    now = tz.now()
    shipment_id = ShipmentReceipt.objects.values_list("shipment_id", flat=True).get(pk=receipt_id)
    _, shipment = _lock_shipment(shipment_id, expected_revision, request=False)
    current = effective_receipt(shipment)
    if current is None or str(current.pk) != str(receipt_id):
        raise _error("Correct the latest recorded receipt.", "invalid_transition")
    data, exceptions = _receipt_values(shipment, values, now=now)
    attachment = store_attachment(upload, actor=actor) or current.attachment
    receipt = _write_receipt(
        actor, shipment, data, exceptions, reason, attachment, corrects=current
    )
    _bump(shipment)
    _shipment_event(shipment, actor, ShipmentEvent.Kind.RECEIPT, now, note=f"Corrected. {reason}")
    _audit(
        actor,
        "distribution.receipt_corrected",
        shipment,
        before={"receipt": str(current.pk), "condition": current.condition},
        after={
            "receipt": str(receipt.pk),
            "condition": data["condition"],
            **_attachment_audit(receipt),
        },
        reason=reason,
    )
    return receipt


@storage.operation()
def cancel_shipment(*, actor, shipment_id, expected_revision, reason):
    """Cancel a draft or packed shipment; its specimens stay picked (put back or reuse)."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    _, shipment = _lock_shipment(shipment_id, expected_revision)
    if shipment.state not in (Shipment.State.DRAFT, Shipment.State.PACKED):
        raise _error("Only a draft or packed shipment can be cancelled.", "invalid_transition")
    for allocation in (
        Allocation.objects.select_for_update(of=("self",)).filter(shipment=shipment).order_by("id")
    ):
        allocation.shipment = None
        allocation.packed_revision = None
        _bump(allocation, "shipment", "packed_revision")
    before = shipment.state
    shipment.state = Shipment.State.CANCELLED
    _bump(shipment)
    _shipment_event(shipment, actor, ShipmentEvent.Kind.CANCELLED, tz.now(), note=reason)
    _audit(
        actor,
        "distribution.shipment_cancelled",
        shipment,
        before={"state": before},
        after={"state": "cancelled"},
        reason=reason,
    )
    return shipment


@storage.operation()
def add_shipment_note(*, actor, shipment_id, note, occurred_at, upload=None):
    """A note or attachment on the shipment's timeline (waybill, photo, logger output)."""
    require_role(actor, *OPERATE_ROLES)
    note = _text(note, "note", TEXT_LIMIT)
    occurred_at = _moment(occurred_at)
    if not note and not upload:
        raise _error("Write a note or attach a file.", "required", "note")
    shipment = Shipment.objects.select_for_update().get(pk=shipment_id)
    attachment = store_attachment(upload, actor=actor)
    event = _shipment_event(
        shipment, actor, ShipmentEvent.Kind.NOTE, occurred_at, note=note, attachment=attachment
    )
    _audit(
        actor,
        "distribution.shipment_note",
        shipment,
        after={"note": note, **_attachment_audit(event)},
        reason=note or "Attachment added",
    )
    return event


# ------------------------------------------------------------------ returns


@storage.operation()
def record_return(
    *,
    actor,
    item_id,
    expected_revision,
    quantity,
    supplied_text,
    thaw_count,
    condition,
    recipient_id,
    occurred_at,
    reason,
    upload=None,
):
    """A shipped specimen comes back: specimens `record_return` (return custody to the receiving
    staff member, measured amount, reported thaws or unknown), then a use hold "Returned in
    SH-..." until a manager reviews it. Storing it is the ordinary check-in."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    condition = _text(condition, "condition", TEXT_LIMIT)
    occurred_at = _moment(occurred_at)
    storage.lock_gate()
    shipment_id = ShipmentItem.objects.values_list("shipment_id", flat=True).get(pk=item_id)
    _, shipment = _lock_shipment(shipment_id, ...)
    item = ShipmentItem.objects.select_related("specimen__unit").get(pk=item_id)
    if not shipment.is_sent:
        raise _error(
            "Only material of a dispatched shipment can be returned.", "invalid_transition"
        )
    if SpecimenReturn.objects.filter(item=item).exists():
        raise _error(
            f"{item.accession} was already returned from this shipment.", "invalid_transition"
        )
    if occurred_at < shipment.dispatched_at:
        raise _error("The return cannot be before dispatch.", "invalid_time", "occurred_at")
    source = f"{shipment.number} · {shipment.ship_to_institution}"[:160]
    event = specimens.record_return(
        actor=actor,
        specimen_id=item.specimen_id,
        expected_revision=expected_revision,
        quantity=quantity,
        supplied_text=supplied_text,
        recipient_id=recipient_id,
        thaw_count=thaw_count,
        source=source,
        occurred_at=occurred_at,
        reason=reason,
    )
    hold = specimens.place_linked_hold(
        actor=actor,
        specimen_id=item.specimen_id,
        scope="use",
        reference=RETURN_REFERENCE.format(number=shipment.number),
        occurred_at=occurred_at,
        reason=f"Returned material awaits review. {reason}",
    )
    specimen = Specimen.objects.select_related("unit").get(pk=item.specimen_id)
    amount = specimens.parse_quantity(quantity, specimen.unit, allow_zero=True)
    attachment = store_attachment(upload, actor=actor)
    row = SpecimenReturn.objects.create(
        shipment=shipment,
        item=item,
        specimen=specimen,
        occurred_at=occurred_at,
        quantity=amount,
        unit=specimen.unit,
        supplied_text=(supplied_text or "").strip()[:80],
        thaw_count=thaw_count,
        condition=condition,
        custody_event=event,
        hold=hold,
        actor=actor,
        reason=reason,
        attachment=attachment,
    )
    _bump(shipment)
    _shipment_event(
        shipment,
        actor,
        ShipmentEvent.Kind.RETURNED,
        occurred_at,
        note=f"{item.accession} returned. {reason}",
        attachment=attachment,
    )
    _audit(
        actor,
        "distribution.return_recorded",
        shipment,
        after={
            "item": item.accession,
            "quantity": amount,
            "thaw_count": thaw_count,
            "custody_event": str(event.pk),
            "hold": str(hold.pk),
            **_attachment_audit(row),
        },
        reason=reason,
    )
    return row


def return_reviewed(specimen_return):
    return specimen_return.reviews.filter(decision=ReturnReview.Decision.RELEASE).exists()


@storage.operation()
def review_return(*, actor, return_id, expected_revision, decision, occurred_at, reason):
    """Release returned material for use (its hold is released) or keep it on hold, with a
    reason. Disposal stays the ordinary four-eyes path."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    decision = _choice(decision, ReturnReview.Decision.choices, "decision", "a decision")
    occurred_at = _moment(occurred_at)
    storage.lock_gate()
    shipment_id = SpecimenReturn.objects.values_list("shipment_id", flat=True).get(pk=return_id)
    _, shipment = _lock_shipment(shipment_id, ...)
    row = SpecimenReturn.objects.select_related("specimen").get(pk=return_id)
    if row.specimen.revision != expected_revision:
        raise _error(
            f"Specimen {row.specimen.accession} changed since you opened this page. Reload it.",
            "stale_revision",
        )
    if return_reviewed(row):
        raise _error("This return was already released for use.", "invalid_transition")
    review = ReturnReview.objects.create(
        specimen_return=row, decision=decision, actor=actor, reason=reason
    )
    if decision == ReturnReview.Decision.RELEASE:
        specimens.release_linked_hold(
            actor=actor, hold_id=row.hold_id, occurred_at=occurred_at, reason=reason
        )
    _audit(
        actor,
        "distribution.return_reviewed",
        shipment,
        after={"item": row.item.accession, "decision": decision},
        reason=reason,
    )
    return review


# ------------------------------------------------------------------ agreements

AGREEMENT_FIELDS = (
    "kind",
    "title",
    "institution",
    "investigator",
    "provider_reference",
    "recipient_reference",
    "data_level",
    "end_disposition",
    "onward_transfer",
    "permitted_use",
    "expires_on",
    "reporting_due_on",
    "document_reference",
)
AGREEMENT_EXECUTED_FIELDS = (
    "title",
    "provider_reference",
    "recipient_reference",
    "expires_on",
    "reporting_due_on",
    "document_reference",
)
AGREEMENT_STATE = (
    *AGREEMENT_FIELDS,
    "status",
    "executed_on",
    "effective_on",
    "terminated_on",
    "permitted_categories",
)


def _lock_agreement(agreement_id, expected_revision):
    agreement = _lock(Agreement, agreement_id, "agreement")
    _check_revision(agreement, expected_revision, "agreement")
    return agreement


def _agreement_event(agreement, actor, kind, reason, changes=None):
    AgreementEvent.objects.create(
        agreement=agreement, kind=kind, actor=actor, reason=reason, changes=_json(changes or {})
    )


@storage.operation()
def save_agreement(
    *, actor, agreement_id, expected_revision, values, permitted_categories=None, reason=""
):
    """Create or edit an agreement's recorded terms. Once executed, only the title, references,
    expiry, reporting date and filing reference change (with a reason); the terms are fixed."""
    require_role(actor, *MANAGE_ROLES)
    storage.lock_gate()
    agreement = None
    allowed = AGREEMENT_FIELDS
    if agreement_id is not None:
        agreement = _lock_agreement(agreement_id, expected_revision)
        reason = need_reason(reason)
        if agreement.status == Agreement.Status.TERMINATED:
            raise _error("A terminated agreement cannot be edited.", "invalid_transition")
        if agreement.status == Agreement.Status.EXECUTED:
            allowed = AGREEMENT_EXECUTED_FIELDS
            if permitted_categories is not None and {str(c) for c in permitted_categories} != {
                str(c) for c in agreement.permitted_categories.values_list("pk", flat=True)
            }:
                raise _error(
                    "The terms of an executed agreement are fixed.",
                    "invalid",
                    "permitted_categories",
                )
            permitted_categories = None
    unknown = set(values) - set(AGREEMENT_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {sorted(unknown)[0]}.", "invalid")

    def get(name, default=""):
        return (
            values[name] if name in values else (getattr(agreement, name) if agreement else default)
        )

    institution_value = get("institution", None)
    institution_id = getattr(institution_value, "pk", institution_value)
    institution = Institution.objects.filter(pk=institution_id).first() if institution_id else None
    if institution is None:
        raise _error("Choose the institution.", "required", "institution")
    investigator_value = get("investigator", None)
    investigator_id = getattr(investigator_value, "pk", investigator_value)
    investigator = (
        Investigator.objects.filter(pk=investigator_id).first() if investigator_id else None
    )
    if investigator_id and investigator is None:
        raise _error("Choose an existing investigator.", "invalid", "investigator")
    if investigator is not None and investigator.institution_id != institution.pk:
        raise _error("The investigator belongs to another institution.", "invalid", "investigator")
    if agreement is not None and institution.pk != agreement.institution_id:
        if agreement.requests.exists():
            raise _error(
                "Requests are linked to this agreement; its institution is fixed.",
                "invalid",
                "institution",
            )
    expires_on = _day(get("expires_on", None), "expires_on", required=False, past=False)
    if (
        agreement is not None
        and agreement.effective_on
        and expires_on
        and expires_on < agreement.effective_on
    ):
        raise _error("The expiry date is before the effective date.", "invalid", "expires_on")
    data = {
        "kind": _choice(get("kind", None), Agreement.Kind.choices, "kind", "the kind of agreement"),
        "title": _text(get("title"), "title", 160, required=True, label="a title"),
        "institution": institution,
        "investigator": investigator,
        "provider_reference": _text(get("provider_reference"), "provider_reference", 80),
        "recipient_reference": _text(get("recipient_reference"), "recipient_reference", 80),
        "data_level": _choice(
            get("data_level", Agreement.DataLevel.NOT_STATED),
            Agreement.DataLevel.choices,
            "data_level",
            "the data level",
        ),
        "end_disposition": _choice(
            get("end_disposition", Agreement.EndDisposition.NOT_STATED),
            Agreement.EndDisposition.choices,
            "end_disposition",
            "what happens at the end",
        ),
        "onward_transfer": _choice(
            get("onward_transfer", Agreement.OnwardTransfer.NOT_STATED),
            Agreement.OnwardTransfer.choices,
            "onward_transfer",
            "onward transfer",
        ),
        "permitted_use": _text(get("permitted_use"), "permitted_use"),
        "expires_on": expires_on,
        "reporting_due_on": _day(
            get("reporting_due_on", None), "reporting_due_on", required=False, past=False
        ),
        "document_reference": _text(get("document_reference"), "document_reference", 160),
    }
    if agreement is not None and allowed is AGREEMENT_EXECUTED_FIELDS:
        for name in AGREEMENT_FIELDS:
            if name not in allowed and data[name] != getattr(agreement, name):
                raise _error("The terms of an executed agreement are fixed.", "invalid", name)
    categories = None
    if permitted_categories is not None:
        categories = _ids(
            permitted_categories,
            ConsentScopeCategory,
            "permitted_categories",
            "scope categories that govern use",
            extra={"gates_use": True},
        )
    before = state(agreement, AGREEMENT_STATE) if agreement else {}
    if agreement is None:
        agreement = Agreement.objects.create(created_by=actor, **data)
    else:
        for name, value in data.items():
            setattr(agreement, name, value)
        _bump(agreement)
    if categories is not None:
        agreement.permitted_categories.set(categories)
    after = state(agreement, AGREEMENT_STATE)
    _agreement_event(
        agreement,
        actor,
        AgreementEvent.Kind.EDITED if before else AgreementEvent.Kind.CREATED,
        reason,
        _changes(before, after) if before else {},
    )
    _audit(
        actor,
        "distribution.agreement_saved",
        agreement,
        before=before,
        after=after,
        reason=reason or "Agreement recorded",
    )
    return agreement


@storage.operation()
def execute_agreement(
    *,
    actor,
    agreement_id,
    expected_revision,
    executed_on,
    effective_on,
    expires_on,
    document_reference,
    reason,
):
    """Record that the agreement was signed: dates and where the signed copy is filed (or an
    attached executed copy)."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    agreement = _lock_agreement(agreement_id, expected_revision)
    if agreement.status != Agreement.Status.DRAFT:
        raise _error("Only a draft agreement can be executed.", "invalid_transition")
    executed_on = _day(executed_on, "executed_on", label="the signature date")
    effective_on = _day(effective_on, "effective_on", past=False, label="the effective date")
    expires_on = _day(expires_on, "expires_on", required=False, past=False)
    if expires_on is not None and expires_on < effective_on:
        raise _error("The expiry date is before the effective date.", "invalid", "expires_on")
    document_reference = _text(document_reference, "document_reference", 160)
    if (
        not document_reference
        and not agreement.documents.filter(kind=AgreementDocument.Kind.EXECUTED).exists()
    ):
        raise _error(
            "Say where the signed agreement is filed, or attach the executed copy first.",
            "required",
            "document_reference",
        )
    before = state(agreement, AGREEMENT_STATE)
    agreement.status = Agreement.Status.EXECUTED
    agreement.executed_on = executed_on
    agreement.effective_on = effective_on
    agreement.expires_on = expires_on
    if document_reference:
        agreement.document_reference = document_reference
    _bump(agreement)
    after = state(agreement, AGREEMENT_STATE)
    _agreement_event(
        agreement, actor, AgreementEvent.Kind.EXECUTED, reason, _changes(before, after)
    )
    _audit(
        actor,
        "distribution.agreement_executed",
        agreement,
        before=before,
        after=after,
        reason=reason,
    )
    return agreement


@storage.operation()
def terminate_agreement(*, actor, agreement_id, expected_revision, terminated_on, reason):
    """Record the agreement's termination; dispatch under it is blocked from that date."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    agreement = _lock_agreement(agreement_id, expected_revision)
    if agreement.status != Agreement.Status.EXECUTED:
        raise _error("Only an executed agreement can be terminated.", "invalid_transition")
    terminated_on = _day(terminated_on, "terminated_on", past=False, label="the termination date")
    if terminated_on < agreement.effective_on:
        raise _error("Termination cannot be before the effective date.", "invalid", "terminated_on")
    agreement.status = Agreement.Status.TERMINATED
    agreement.terminated_on = terminated_on
    _bump(agreement)
    _agreement_event(
        agreement,
        actor,
        AgreementEvent.Kind.TERMINATED,
        reason,
        {"terminated_on": [None, terminated_on.isoformat()]},
    )
    _audit(
        actor,
        "distribution.agreement_terminated",
        agreement,
        before={"status": "executed"},
        after={"status": "terminated", "terminated_on": terminated_on},
        reason=reason,
    )
    return agreement


@storage.operation()
def add_agreement_document(*, actor, agreement_id, kind, title, note, upload, supersedes_id=None):
    """Attach a document (executed copy, amendment, correspondence). A file is required."""
    require_role(actor, *MANAGE_ROLES)
    kind = _choice(kind, AgreementDocument.Kind.choices, "kind", "the kind of document")
    title = _text(title, "title", 160, required=True, label="a title")
    note = _text(note, "note")
    if not upload:
        raise _error("Attach the document.", "required", "attachment")
    agreement = Agreement.objects.select_for_update().get(pk=agreement_id)
    supersedes = None
    if supersedes_id:
        supersedes = AgreementDocument.objects.filter(pk=supersedes_id, agreement=agreement).first()
        if supersedes is None:
            raise _error("Choose a document of this agreement.", "invalid", "supersedes")
    attachment = store_attachment(upload, actor=actor)
    document = AgreementDocument.objects.create(
        agreement=agreement,
        kind=kind,
        title=title,
        note=note,
        supersedes=supersedes,
        actor=actor,
        attachment=attachment,
    )
    _agreement_event(
        agreement, actor, AgreementEvent.Kind.DOCUMENT, title, {"document": str(document.pk)}
    )
    _audit(
        actor,
        "distribution.agreement_document",
        agreement,
        after={
            "document": str(document.pk),
            "kind": kind,
            "title": title,
            **_attachment_audit(document),
        },
        reason=title,
    )
    return document


def require_read(actor):
    require_role(actor, *READ_ROLES)


def can_operate(actor):
    return has_role(actor, *OPERATE_ROLES)
