"""Specimen mutations: catalogues, registration, placement, custody, quantity, thaw, holds,
disposal and corrections.

Every top-level mutation runs in `storage.operation()` (one transaction and operation scope),
checks the actor's role before reading records, takes the StorageGate first (`lock_gate`), then
locks the specimen row, then request/ledger rows, and lets the storage helpers lock positions and
claims. It revalidates against the locked state, writes the events, updates the specimen's
projections (quantity, status, custodian, open check-out), increases the specimen revision once,
advances the storage key's chronology watermark (`touch_occupancy` or `change_occupancy`) and
records the attributed audit event, all in the same transaction.

Validation failures raise django ValidationError with a contract code (stale_revision, occupied,
inactive_location, invalid_transition, invalid_time, held, insufficient_quantity,
approval_required, self_approval, stale_approval, invalid_import) or a field code (required,
invalid, unique). Role failures raise PermissionDenied before any record is read.

Disposal approval is the explicit `accounts.approve_disposal` permission (accounts.roles); Django
superuser status never satisfies it, and nobody approves a disposal they requested.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, connection, transaction
from django.utils import timezone as tz

from accounts.models import User
from accounts.roles import can_approve_disposal, effective_roles, has_role, require_role
from core.audit import record_event
from storage import services as storage
from storage.models import MoveMember, Occupancy

from .models import (
    QUANTITY_DIGITS,
    QUANTITY_PLACES,
    Collection,
    Correction,
    CustodyEvent,
    Derivation,
    DerivationChild,
    DisposalDecision,
    DisposalExecution,
    DisposalRequest,
    Hold,
    HoldRelease,
    Participant,
    QuantityEntry,
    QuantityUnit,
    Specimen,
    SpecimenContainerType,
    SpecimenType,
    ThawEvent,
)

READ_ROLES = storage.READ_ROLES
OPERATE_ROLES = storage.OPERATE_ROLES
MANAGE_ROLES = storage.MANAGE_ROLES
INITIAL_REASON = "Initial definition"
MAX_CHILDREN = 50
MAX_BASELINE_THAWS = 1000
TEXT_LIMIT = 2000
CORRECTABLE_FIELDS = (
    "specimen_type",
    "container_type",
    "participant",
    "collection",
    "collected_at",
    "processing_started_at",
    "processing_completed_at",
)
LINEAGE_FIXED_FIELDS = (
    "specimen_type",
    "participant",
    "collection",
    "collected_at",
    "processing_started_at",
    "processing_completed_at",
)
INHERITED_FIELDS = ("participant", "collection", "collected_at")


# --- typed inputs --------------------------------------------------------------------------


@dataclass(frozen=True)
class Registration:
    """Input for one new primary specimen (registration form, import row)."""

    specimen_type_id: object
    container_type_id: object
    quantity: Decimal
    supplied_text: str
    unit_id: object
    collected_at: datetime
    processing_started_at: datetime | None = None
    processing_completed_at: datetime | None = None
    participant_id: object = None
    collection_id: object = None
    baseline_thaws: int | None = None
    custodian_id: object = None  # defaults to the registering actor
    position_id: object = None  # optional placement in the same transaction


@dataclass(frozen=True)
class ChildSpec:
    """One aliquot or derivative. Aliquots must keep the parent's type and unit."""

    specimen_type_id: object
    container_type_id: object
    quantity: Decimal
    supplied_text: str
    unit_id: object
    position_id: object = None


@dataclass(frozen=True)
class CorrectionValues:
    """Only the fields named in `fields` are corrected; the others are ignored."""

    fields: frozenset = field(default_factory=frozenset)
    specimen_type_id: object = None
    container_type_id: object = None
    participant_id: object = None
    collection_id: object = None
    collected_at: datetime | None = None
    processing_started_at: datetime | None = None
    processing_completed_at: datetime | None = None


# --- small helpers -------------------------------------------------------------------------


def _error(message, code, field=None):
    error = ValidationError(message, code=code)
    return ValidationError({field: error}) if field else error


def _flat_errors(exc):
    """Every ValidationError inside `exc`, whether it holds a list or field errors."""
    if hasattr(exc, "error_dict"):
        return [error for errors in exc.error_dict.values() for error in errors]
    return exc.error_list


def need_reason(reason, field="reason"):
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        raise _error("Give the reason. It is kept in the history.", "required", field)
    if len(text) > TEXT_LIMIT:
        raise _error(f"Use at most {TEXT_LIMIT} characters.", "invalid", field)
    return text


def _text(value, field, limit, *, required=True, label="a value"):
    text = value.strip() if isinstance(value, str) else ""
    if required and not text:
        raise _error(f"Enter {label}.", "required", field)
    if len(text) > limit:
        raise _error(f"Use at most {limit} characters.", "invalid", field)
    return text


def parse_quantity(value, unit, field="quantity", *, allow_negative=False, allow_zero=False):
    """Exact Decimal amount in `unit`: no floats, NaN, infinity, excess scale or rounding."""
    if isinstance(value, (float, bool)) or value is None:
        raise _error("Enter the amount as an exact decimal number.", "invalid", field)
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation:
        raise _error("Enter a number such as 0.5.", "invalid", field) from None
    if not number.is_finite():
        raise _error("Enter a finite number.", "invalid", field)
    if number.as_tuple().exponent < -QUANTITY_PLACES:
        raise _error(
            f"Use at most {QUANTITY_PLACES} decimal places. Amounts are never rounded.",
            "invalid",
            field,
        )
    if abs(number) >= Decimal(10) ** (QUANTITY_DIGITS - QUANTITY_PLACES):
        raise _error("The amount is too large.", "invalid", field)
    if number < 0 and not allow_negative:
        raise _error("The amount cannot be negative.", "invalid", field)
    if number == 0 and not allow_zero:
        raise _error("Enter an amount other than zero.", "invalid", field)
    if unit is not None and not unit.fractional and number != number.to_integral_value():
        raise _error(f"Amounts in {unit.code} are whole numbers.", "invalid", field)
    return number.quantize(Decimal(1).scaleb(-QUANTITY_PLACES))


def _supplied(text, number):
    text = text.strip() if isinstance(text, str) else ""
    return (text or format(number.normalize(), "f"))[:80]


def _aware(value, field, label="the time"):
    if not isinstance(value, datetime) or tz.is_naive(value):
        raise _error(f"Enter {label} with its date and time.", "invalid_time", field)
    return value


def _when(occurred_at, *, floor=None, field="occurred_at", now=None):
    """Event time: aware, not in the future, not before the specimen's latest event."""
    _aware(occurred_at, field, "when it happened")
    if occurred_at > (now or tz.now()):
        raise _error("The time cannot be in the future.", "invalid_time", field)
    if floor is not None and occurred_at < floor:
        local = tz.localtime(floor).strftime("%Y-%m-%d %H:%M")
        raise _error(
            f"The time is earlier than this specimen's latest recorded event ({local}). "
            "Events are recorded in the order they happened.",
            "invalid_time",
            field,
        )
    return occurred_at


def _json(data):
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


def _audit(actor, action, obj, *, before=None, after=None, reason=""):
    return record_event(
        actor=actor,
        action=action,
        obj=obj,
        before=_json(before or {}),
        after=_json(after or {}),
        reason=reason,
    )


def _active(model, pk, field, noun):
    if pk in (None, ""):
        return None
    try:
        obj = model.objects.get(pk=pk)
    except model.DoesNotExist, ValidationError, ValueError:
        raise _error(f"Choose an existing {noun}.", "invalid", field) from None
    if not obj.is_active:
        raise _error(
            f"{obj.code} is inactive and cannot be used for new records.", "invalid", field
        )
    return obj


def _required(obj, field, noun):
    if obj is None:
        raise _error(f"Choose a {noun}.", "required", field)
    return obj


def _recipient(recipient_id, field="recipient_id"):
    try:
        user = User.objects.get(pk=recipient_id)
    except User.DoesNotExist, ValueError, TypeError, ValidationError:
        raise _error("Choose a staff member.", "invalid", field) from None
    if not user.is_active or not effective_roles(user):
        raise _error("Choose an active staff member.", "invalid", field)
    return user


def snapshot(specimen):
    return _json(
        {
            "accession": specimen.accession,
            "status": specimen.status,
            "quantity_on_hand": specimen.quantity_on_hand,
            "unit": specimen.unit.code,
            "custodian": specimen.custodian_id,
            "open_checkout": specimen.open_checkout_id,
            "revision": specimen.revision,
        }
    )


# --- locking and shared checks -------------------------------------------------------------


def _lock_specimen(specimen_id):
    """Gate, then the specimen row. Raises Specimen.DoesNotExist for an unknown key."""
    storage.lock_gate()
    return Specimen.objects.select_for_update(of=("self",)).get(pk=specimen_id)


def _occupancy(specimen):
    # Every occupancy writer holds the gate, which this operation already holds.
    return Occupancy.objects.get(pk=specimen.occupancy_id)


def _check_revision(specimen, expected):
    if expected is None or specimen.revision != expected:
        raise _error(
            f"Specimen {specimen.accession} changed since you opened it. "
            "Reload it, check the current state and try again.",
            "stale_revision",
        )


def _check_occupancy_revision(occupancy, expected):
    if expected is None or occupancy.revision != expected:
        raise _error(
            "The specimen's storage location changed since you opened this page "
            "(for example its box was moved). Reload it and try again.",
            "stale_revision",
        )


def _not_disposed(specimen):
    if specimen.status == Specimen.Status.DISPOSED:
        raise _error(
            f"Specimen {specimen.accession} was disposed. Its record is final.",
            "invalid_transition",
        )


def _in_custody(specimen):
    """Physical steps need the material here: not disposed and not distributed.

    Only the return, holds and record corrections apply to distributed material.
    """
    _not_disposed(specimen)
    if specimen.status == Specimen.Status.DISTRIBUTED:
        raise _error(
            f"Specimen {specimen.accession} was distributed: it left the repository in a "
            "shipment. Record its return before anything else is done with it.",
            "invalid_transition",
        )


USE_SCOPES = (Hold.Scope.USE, Hold.Scope.ALL_USE)  # block check-out, splitting, use, hand-over
DISPOSAL_SCOPES = (Hold.Scope.DISPOSAL, Hold.Scope.ALL_USE)  # block disposal


def active_holds(specimen, scope=None):
    holds = Hold.objects.filter(specimen=specimen, release__isnull=True)
    if scope is None:
        return holds
    return holds.filter(scope__in=scope) if isinstance(scope, tuple) else holds.filter(scope=scope)


def _hold_text(hold):
    text = hold.get_scope_display().lower()
    return f"{text} ({hold.reference})" if hold.reference else text


def _no_all_use_hold(specimen, doing):
    """Refuse use while a use or all-use hold is active (name kept from the first version)."""
    hold = active_holds(specimen, USE_SCOPES).order_by("occurred_at", "id").first()
    if hold is not None:
        raise _error(
            f"Specimen {specimen.accession} has an active {_hold_text(hold)}, so it cannot be "
            f"{doing}. "
            + (
                "It is released through the workflow that placed it."
                if hold.reference
                else "A repository manager can release the hold."
            ),
            "held",
        )


def _no_hold(specimen):
    """Refuse disposal steps while a disposal or all-use hold is active (a use hold allows it)."""
    hold = active_holds(specimen, DISPOSAL_SCOPES).order_by("occurred_at", "id").first()
    if hold is not None:
        raise _error(
            f"Specimen {specimen.accession} has an active {_hold_text(hold)}. It blocks "
            "disposal until it is released.",
            "held",
        )


# Public names for the checks above, so pages can show the same refusal before a form is sent.
require_not_disposed = _not_disposed
require_in_custody = _in_custody
require_no_use_hold = _no_all_use_hold
require_no_disposal_hold = _no_hold


def never_stored(specimen):
    return not MoveMember.objects.filter(occupancy_id=specimen.occupancy_id).exists()


def _in_hand(specimen, occupancy, doing):
    """Material must be checked out, or registered and never yet stored."""
    if specimen.open_checkout_id:
        return
    if occupancy.position_id is None and never_stored(specimen):
        return
    raise _error(
        f"Specimen {specimen.accession} is in storage. Record a check-out before it is {doing}.",
        "invalid_transition",
    )


def _bump(specimen, *fields):
    specimen.revision += 1
    specimen.save(update_fields=["revision", *fields])


def _touch(actor, specimen, occupancy, occurred_at, reason):
    return storage.touch_occupancy(
        actor=actor,
        occupancy_id=occupancy.pk,
        expected_revision=occupancy.revision,
        occurred_at=occurred_at,
        reason=reason,
    )


def _move(actor, occupancy, *, kind, position_id, occurred_at, reason, corrects_id=None):
    return storage.change_occupancy(
        actor=actor,
        occupancy_id=occupancy.pk,
        expected_revision=occupancy.revision,
        position_id=position_id,
        kind=kind,
        occurred_at=occurred_at,
        reason=reason,
        corrects_id=corrects_id,
    )


def _ledger(actor, specimen, *, kind, delta, supplied_text, occurred_at, reason, **links):
    balance = specimen.quantity_on_hand + delta
    if balance < 0:
        raise _error(
            f"Only {format(specimen.quantity_on_hand.normalize(), 'f')} {specimen.unit.code} "
            "is on hand.",
            "insufficient_quantity",
            "quantity",
        )
    entry = QuantityEntry.objects.create(
        specimen=specimen,
        kind=kind,
        delta=delta,
        balance_after=balance,
        unit=specimen.unit,
        supplied_text=supplied_text,
        occurred_at=occurred_at,
        reason=reason,
        actor=actor,
        **links,
    )
    specimen.quantity_on_hand = balance
    if specimen.status != Specimen.Status.DISPOSED:
        specimen.status = Specimen.Status.AVAILABLE if balance > 0 else Specimen.Status.EXHAUSTED
    return entry


def _custody(actor, specimen, *, kind, occurred_at, reason, to=None, **values):
    return CustodyEvent.objects.create(
        specimen=specimen,
        kind=kind,
        actor=actor,
        occurred_at=occurred_at,
        reason=reason,
        to_custodian=to or specimen.custodian,
        **values,
    )


def _save_unique(obj, field, message):
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as exc:
        name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", "") or ""
        if name.endswith("_key") or "unique" in name or "_uniq" in name:
            raise _error(message, "unique", field) from exc
        raise


# --- catalogues ----------------------------------------------------------------------------

CATALOGUES = {
    "participant": Participant,
    "collection": Collection,
    "specimen_type": SpecimenType,
    "container_type": SpecimenContainerType,
    "unit": QuantityUnit,
}
CATALOGUE_FIELDS = {
    "participant": ("code", "is_active"),
    "collection": ("code", "name", "is_active"),
    "specimen_type": ("code", "name", "is_active"),
    "container_type": ("code", "name", "nominal_capacity", "capacity_unit", "is_active"),
    "unit": ("code", "name", "dimension", "fractional", "is_active"),
}
CATALOGUE_NOUNS = {
    "participant": "participant",
    "collection": "collection",
    "specimen_type": "specimen type",
    "container_type": "container type",
    "unit": "unit",
}


def normalize_code(value, field="code"):
    return storage.normalize_code(value, field)


def _unit_code(value, field="code"):
    import re

    code = value.strip() if isinstance(value, str) else ""
    if not code:
        raise _error("Enter a code.", "required", field)
    if len(code) > 16 or not code.isascii() or not re.match(r"^[A-Za-z][A-Za-z0-9._/-]*$", code):
        raise _error(
            "Use at most 16 letters, digits, '.', '_', '/' or '-', starting with a letter. "
            "Unit codes keep their case (mL, uL).",
            "invalid",
            field,
        )
    return code


def catalogue_in_use(kind, obj):
    if obj is None or obj.pk is None:
        return False
    if kind == "unit":
        return (
            Specimen.objects.filter(unit=obj).exists()
            or SpecimenContainerType.objects.filter(capacity_unit=obj).exists()
            or QuantityEntry.objects.filter(unit=obj).exists()
        )
    field_name = {
        "participant": "participant",
        "collection": "collection",
        "specimen_type": "specimen_type",
        "container_type": "container_type",
    }[kind]
    return Specimen.objects.filter(**{field_name: obj}).exists()


def _catalogue_values(kind, values, obj):
    allowed = CATALOGUE_FIELDS[kind]
    unknown = set(values) - set(allowed)
    if unknown:
        raise _error(f"Unknown fields: {', '.join(sorted(unknown))}.", "invalid")
    data = {}
    if "code" in values:
        data["code"] = (_unit_code if kind == "unit" else normalize_code)(values["code"])
    if "name" in values:
        limit = 80 if kind == "unit" else 160
        data["name"] = _text(values["name"], "name", limit, label="a name")
    if "is_active" in values:
        data["is_active"] = bool(values["is_active"])
    if kind == "unit":
        if "dimension" in values:
            if values["dimension"] not in QuantityUnit.Dimension.values:
                raise _error("Choose volume, mass or count.", "invalid", "dimension")
            data["dimension"] = values["dimension"]
        if "fractional" in values:
            data["fractional"] = bool(values["fractional"])
        dimension = data.get("dimension", getattr(obj, "dimension", None))
        fractional = data.get("fractional", getattr(obj, "fractional", True))
        if dimension == "count" and fractional:
            raise _error("Counts are whole numbers: clear Fractional.", "invalid", "fractional")
    if kind == "container_type":
        capacity = values.get("nominal_capacity", getattr(obj, "nominal_capacity", None))
        unit_id = values.get("capacity_unit", getattr(obj, "capacity_unit_id", None))
        if isinstance(unit_id, QuantityUnit):
            unit_id = unit_id.pk
        unit = None
        if unit_id not in (None, ""):
            try:
                unit = QuantityUnit.objects.get(pk=unit_id)
            except QuantityUnit.DoesNotExist, ValidationError, ValueError:
                raise _error("Choose an existing unit.", "invalid", "capacity_unit") from None
        if capacity in (None, "") and unit is None:
            data["nominal_capacity"], data["capacity_unit"] = None, None
        elif capacity in (None, "") or unit is None:
            raise _error(
                "Give both the nominal capacity and its unit, or neither.",
                "invalid",
                "nominal_capacity",
            )
        else:
            data["nominal_capacity"] = parse_quantity(capacity, unit, "nominal_capacity")
            data["capacity_unit"] = unit
    if obj is None:
        missing = [
            name
            for name in allowed
            if name not in data
            and name not in ("is_active", "nominal_capacity", "capacity_unit")
            and not (kind == "unit" and name == "fractional")
        ]
        if missing:
            raise _error("Enter this value.", "required", missing[0])
    return data


@storage.operation()
def save_specimen_catalogue(*, actor, kind, object_id, expected_revision, values, reason):
    """Create (object_id None) or edit a catalogue record; managers only."""
    require_role(actor, *MANAGE_ROLES)
    if kind not in CATALOGUES:
        raise _error("Unknown catalogue.", "invalid")
    model = CATALOGUES[kind]
    noun = CATALOGUE_NOUNS[kind]
    storage.lock_gate()
    obj = model.objects.select_for_update().get(pk=object_id) if object_id else None
    if obj is not None:
        if expected_revision is None or obj.revision != expected_revision:
            raise _error(
                f"This {noun} changed since you opened it. Reload it and try again.",
                "stale_revision",
            )
        reason = need_reason(reason)
    else:
        reason = (reason or "").strip() or INITIAL_REASON
    data = _catalogue_values(kind, values or {}, obj)
    if obj is None:
        obj = model(created_by=actor, **data)
        _save_unique(obj, "code", f"This code is already used by another {noun}.")
        _audit(
            actor,
            f"specimens.{kind}_created",
            obj,
            after=_catalogue_state(kind, obj),
            reason=reason,
        )
        return obj
    before = _catalogue_state(kind, obj)
    changed = {k: v for k, v in data.items() if _comparable(getattr(obj, k)) != _comparable(v)}
    if not changed:
        raise _error("Nothing to save: no value changed.", "invalid_transition")
    frozen = {"code"} | ({"dimension", "fractional"} if kind == "unit" else set())
    if frozen & set(changed) and catalogue_in_use(kind, obj):
        name = sorted(frozen & set(changed))[0]
        raise _error(
            f"This {noun} is already used by records, so its {name.replace('_', ' ')} cannot "
            "change. Create a new entry instead.",
            "invalid_transition",
            name,
        )
    for key, value in changed.items():
        setattr(obj, key, value)
    obj.revision += 1
    _save_unique(obj, "code", f"This code is already used by another {noun}.")
    _audit(
        actor,
        f"specimens.{kind}_changed",
        obj,
        before=before,
        after=_catalogue_state(kind, obj),
        reason=reason,
    )
    return obj


def _comparable(value):
    return value.pk if hasattr(value, "pk") else value


def _catalogue_state(kind, obj):
    data = {}
    for name in CATALOGUE_FIELDS[kind]:
        value = getattr(obj, name)
        data[name] = str(value.pk) if hasattr(value, "pk") else value
    data["revision"] = obj.revision
    return _json(data)


# --- registration --------------------------------------------------------------------------


def next_accession():
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval('specimens_accession_seq')")
        number = cursor.fetchone()[0]
    return f"VP-{number:06d}"


def _validate_times(collected_at, started, completed, *, now):
    _aware(collected_at, "collected_at", "the collection time")
    if collected_at > now:
        raise _error("The collection time cannot be in the future.", "invalid_time", "collected_at")
    if completed is not None and started is None:
        raise _error(
            "Give the processing start time when you give its completion.",
            "invalid_time",
            "processing_started_at",
        )
    if started is not None:
        _aware(started, "processing_started_at", "the processing start time")
        if started > now:
            raise _error(
                "The time cannot be in the future.", "invalid_time", "processing_started_at"
            )
        if started < collected_at:
            raise _error(
                "Processing cannot start before collection.",
                "invalid_time",
                "processing_started_at",
            )
    if completed is not None:
        _aware(completed, "processing_completed_at", "the processing completion time")
        if completed > now:
            raise _error(
                "The time cannot be in the future.", "invalid_time", "processing_completed_at"
            )
        if completed < started:
            raise _error(
                "Processing cannot finish before it started.",
                "invalid_time",
                "processing_completed_at",
            )


def _baseline(value):
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not (0 <= value <= MAX_BASELINE_THAWS)
    ):
        raise _error(
            f"Prior thaws must be a whole number from 0 to {MAX_BASELINE_THAWS}, or unknown.",
            "invalid",
            "baseline_thaws",
        )
    return value


def _create_specimen(
    *,
    actor,
    occurred_at,
    reason,
    specimen_type,
    container_type,
    unit,
    quantity,
    supplied_text,
    custodian,
    collected_at,
    processing_started_at=None,
    processing_completed_at=None,
    participant=None,
    collection=None,
    parent=None,
    derivation_kind=Specimen.Kind.PRIMARY,
    baseline_count=None,
    inherited_count=0,
    baseline_reason=None,
    derivation=None,
):
    """New storage key, specimen, initial ledger entry, register custody and baseline thaw."""
    occupancy = storage.new_occupancy(actor=actor, occurred_at=occurred_at)
    specimen = Specimen.objects.create(
        accession=next_accession(),
        occupancy=occupancy,
        specimen_type=specimen_type,
        container_type=container_type,
        participant=participant,
        collection=collection,
        parent=parent,
        derivation_kind=derivation_kind,
        collected_at=collected_at,
        processing_started_at=processing_started_at,
        processing_completed_at=processing_completed_at,
        registered_by=actor,
        unit=unit,
        quantity_on_hand=quantity,
        status=Specimen.Status.AVAILABLE,
        custodian=custodian,
    )
    QuantityEntry.objects.create(
        specimen=specimen,
        kind=QuantityEntry.Kind.INITIAL,
        delta=quantity,
        balance_after=quantity,
        unit=unit,
        supplied_text=supplied_text,
        occurred_at=occurred_at,
        actor=actor,
        reason=reason,
        derivation=derivation,
    )
    custody = _custody(
        actor,
        specimen,
        kind=CustodyEvent.Kind.REGISTER,
        occurred_at=occurred_at,
        reason=reason,
        to=custodian,
    )
    ThawEvent.objects.create(
        specimen=specimen,
        kind=ThawEvent.Kind.BASELINE,
        count=baseline_count,
        inherited_recorded_count=inherited_count,
        occurred_at=occurred_at,
        actor=actor,
        reason=baseline_reason
        or (
            "Thaw history before registration unknown"
            if baseline_count is None
            else f"{baseline_count} thaw(s) before registration, as stated at registration"
        ),
        custody_event=custody,
    )
    _audit(
        actor,
        "specimens.specimen_registered",
        specimen,
        after={
            **snapshot(specimen),
            "specimen_type": specimen_type.code,
            "container_type": container_type.code,
            "participant": participant.code if participant else None,
            "collection": collection.code if collection else None,
            "parent": parent.accession if parent else None,
            "derivation_kind": derivation_kind,
            "collected_at": collected_at,
            "processing_started_at": processing_started_at,
            "processing_completed_at": processing_completed_at,
            "baseline_thaws": baseline_count,
        },
        reason=reason,
    )
    return specimen, occupancy


def _place_new(actor, specimen, occupancy, position_id, occurred_at, reason):
    move = _move(
        actor,
        occupancy,
        kind="place",
        position_id=position_id,
        occurred_at=occurred_at,
        reason=reason,
    )
    _custody(
        actor,
        specimen,
        kind=CustodyEvent.Kind.PLACE,
        occurred_at=occurred_at,
        reason=reason,
        from_custodian=specimen.custodian,
        move_event=move,
    )
    return move


def register_in_operation(*, actor, data, occurred_at, reason):
    """Registration body for callers already inside an operation holding the gate (import)."""
    now = tz.now()
    reason = need_reason(reason)
    specimen_type = _required(
        _active(SpecimenType, data.specimen_type_id, "specimen_type", "specimen type"),
        "specimen_type",
        "specimen type",
    )
    container_type = _required(
        _active(SpecimenContainerType, data.container_type_id, "container_type", "container type"),
        "container_type",
        "container type",
    )
    unit = _required(_active(QuantityUnit, data.unit_id, "unit", "unit"), "unit", "unit")
    participant = _active(Participant, data.participant_id, "participant", "participant")
    collection = _active(Collection, data.collection_id, "collection", "collection")
    quantity = parse_quantity(data.quantity, unit)
    custodian = _recipient(data.custodian_id, "custodian") if data.custodian_id else actor
    _validate_times(
        data.collected_at, data.processing_started_at, data.processing_completed_at, now=now
    )
    floor = max(t for t in (data.collected_at, data.processing_completed_at) if t is not None)
    _when(occurred_at, now=now)
    if occurred_at < floor:
        raise _error(
            "Registration cannot be earlier than collection or the end of processing.",
            "invalid_time",
            "occurred_at",
        )
    baseline = _baseline(data.baseline_thaws)
    specimen, occupancy = _create_specimen(
        actor=actor,
        occurred_at=occurred_at,
        reason=reason,
        specimen_type=specimen_type,
        container_type=container_type,
        unit=unit,
        quantity=quantity,
        supplied_text=_supplied(data.supplied_text, quantity),
        custodian=custodian,
        collected_at=data.collected_at,
        processing_started_at=data.processing_started_at,
        processing_completed_at=data.processing_completed_at,
        participant=participant,
        collection=collection,
        baseline_count=baseline,
    )
    if data.position_id:
        _place_new(actor, specimen, occupancy, data.position_id, occurred_at, reason)
    return specimen


@storage.operation()
def register_specimen(*, actor, data, occurred_at, reason):
    """Register one primary specimen: opaque accession, initial amount, custody, thaw baseline."""
    require_role(actor, *OPERATE_ROLES)
    storage.lock_gate()
    specimen = register_in_operation(actor=actor, data=data, occurred_at=occurred_at, reason=reason)
    specimen.refresh_from_db()
    return specimen


# --- placement -----------------------------------------------------------------------------


@storage.operation()
def place_specimen(
    *,
    actor,
    specimen_id,
    expected_revision,
    expected_occupancy_revision,
    position_id,
    occurred_at,
    reason,
):
    """Store an unplaced specimen (registered, or a new aliquot) in a free position."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _check_occupancy_revision(occupancy, expected_occupancy_revision)
    _in_custody(specimen)
    if specimen.open_checkout_id:
        raise _error(
            "This specimen is checked out. Record a check-in to return it to storage.",
            "invalid_transition",
        )
    if specimen.status != Specimen.Status.AVAILABLE:
        raise _error(
            "Only material with an amount on hand is placed. An exhausted vessel stays where "
            "it is until it is disposed.",
            "invalid_transition",
        )
    _when(occurred_at, floor=occupancy.last_occurred_at)
    _place_new(actor, specimen, occupancy, position_id, occurred_at, reason)
    _bump(specimen)
    _audit(
        actor,
        "specimens.specimen_placed",
        specimen,
        after={"position": str(position_id), "revision": specimen.revision},
        reason=reason,
    )
    return specimen


@storage.operation()
def relocate_specimen(
    *,
    actor,
    specimen_id,
    expected_revision,
    expected_occupancy_revision,
    position_id,
    occurred_at,
    reason,
    corrects_id=None,
):
    """Move a stored specimen to another free position (allowed for held or exhausted ones).

    With `corrects_id` (a storage MoveEvent of this specimen) the move is recorded as a
    correction of that movement; corrections are for repository managers (admin, manager) only.
    A relocation always ends in a position: taking material out of storage is a check-out.
    """
    require_role(actor, *(MANAGE_ROLES if corrects_id else OPERATE_ROLES))
    reason = need_reason(reason)
    if position_id in (None, ""):
        raise _error("Choose the position it moves to.", "required", "position_id")
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _check_occupancy_revision(occupancy, expected_occupancy_revision)
    _in_custody(specimen)
    if occupancy.position_id is None:
        raise _error("The specimen is not in a storage position.", "invalid_transition")
    _when(occurred_at, floor=occupancy.last_occurred_at)
    before = occupancy.position_id
    move = _move(
        actor,
        occupancy,
        kind="correction" if corrects_id else "relocate",
        position_id=position_id,
        occurred_at=occurred_at,
        reason=reason,
        corrects_id=corrects_id,
    )
    _custody(
        actor,
        specimen,
        kind=CustodyEvent.Kind.RELOCATE,
        occurred_at=occurred_at,
        reason=reason,
        from_custodian=specimen.custodian,
        move_event=move,
    )
    _bump(specimen)
    _audit(
        actor,
        "specimens.specimen_relocated",
        specimen,
        before={"position": str(before)},
        after={
            "position": str(position_id),
            "move_event": str(move.pk),
            "corrects": str(corrects_id) if corrects_id else None,
            "revision": specimen.revision,
        },
        reason=reason,
    )
    return specimen


# --- custody -------------------------------------------------------------------------------


@storage.operation()
def check_out(
    *,
    actor,
    specimen_id,
    expected_revision,
    expected_occupancy_revision,
    recipient_id,
    destination,
    occurred_at,
    reason,
):
    """Take a stored specimen out: frees its position and starts the time-out-of-storage clock."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    destination = _text(destination, "destination", 160, label="where it is going")
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _check_occupancy_revision(occupancy, expected_occupancy_revision)
    _in_custody(specimen)
    if specimen.open_checkout_id:
        raise _error("This specimen is already checked out.", "invalid_transition")
    _no_all_use_hold(specimen, "checked out")
    if occupancy.position_id is None:
        raise _error(
            "This specimen is not in storage. Record a custody transfer to hand it over.",
            "invalid_transition",
        )
    recipient = _recipient(recipient_id)
    _when(occurred_at, floor=occupancy.last_occurred_at)
    move = _move(
        actor, occupancy, kind="check_out", position_id=None, occurred_at=occurred_at, reason=reason
    )
    event = _custody(
        actor,
        specimen,
        kind=CustodyEvent.Kind.CHECK_OUT,
        occurred_at=occurred_at,
        reason=reason,
        to=recipient,
        from_custodian=specimen.custodian,
        destination=destination,
        move_event=move,
    )
    before = snapshot(specimen)
    specimen.custodian = recipient
    specimen.open_checkout = event
    _bump(specimen, "custodian", "open_checkout")
    _audit(
        actor,
        "specimens.specimen_checked_out",
        specimen,
        before=before,
        after={**snapshot(specimen), "destination": destination},
        reason=reason,
    )
    return specimen


@storage.operation()
def check_in(
    *,
    actor,
    specimen_id,
    expected_revision,
    expected_occupancy_revision,
    position_id,
    recipient_id,
    thaw_observed,
    occurred_at,
    reason,
):
    """Return a checked-out specimen to a free position, with the explicit thaw observation.

    `thaw_observed` is True (one observed thaw is recorded), False (observed not thawed) or None
    (not known). Leaving storage alone is never counted as a thaw.
    """
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    if thaw_observed not in (True, False, None):
        raise _error(
            "Say whether a thaw was observed: yes, no or not known.", "invalid", "thaw_observed"
        )
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _check_occupancy_revision(occupancy, expected_occupancy_revision)
    _in_custody(specimen)
    if not specimen.open_checkout_id:
        raise _error("This specimen is not checked out.", "invalid_transition")
    recipient = _recipient(recipient_id)
    _when(occurred_at, floor=occupancy.last_occurred_at)
    move = _move(
        actor,
        occupancy,
        kind="check_in",
        position_id=position_id,
        occurred_at=occurred_at,
        reason=reason,
    )
    event = _custody(
        actor,
        specimen,
        kind=CustodyEvent.Kind.CHECK_IN,
        occurred_at=occurred_at,
        reason=reason,
        to=recipient,
        from_custodian=specimen.custodian,
        move_event=move,
        checkout_id=specimen.open_checkout_id,
        thaw_observed=thaw_observed,
    )
    if thaw_observed:
        ThawEvent.objects.create(
            specimen=specimen,
            kind=ThawEvent.Kind.THAW,
            count=1,
            occurred_at=occurred_at,
            actor=actor,
            reason=f"Thaw observed at check-in. {reason}",
            custody_event=event,
        )
    before = snapshot(specimen)
    specimen.custodian = recipient
    specimen.open_checkout = None
    _bump(specimen, "custodian", "open_checkout")
    _audit(
        actor,
        "specimens.specimen_checked_in",
        specimen,
        before=before,
        after={**snapshot(specimen), "position": str(position_id), "thaw_observed": thaw_observed},
        reason=reason,
    )
    return specimen


@storage.operation()
def transfer_custody(
    *,
    actor,
    specimen_id,
    expected_revision,
    expected_occupancy_revision,
    recipient_id,
    destination,
    occurred_at,
    reason,
):
    """Hand a specimen to another staff member without changing where it is."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    destination = _text(destination, "destination", 160, required=False)
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _check_occupancy_revision(occupancy, expected_occupancy_revision)
    _in_custody(specimen)
    _no_all_use_hold(specimen, "handed over for use")
    recipient = _recipient(recipient_id)
    if recipient.pk == specimen.custodian_id:
        raise _error("This person already has custody.", "invalid", "recipient_id")
    _when(occurred_at, floor=occupancy.last_occurred_at)
    _touch(actor, specimen, occupancy, occurred_at, reason)
    _custody(
        actor,
        specimen,
        kind=CustodyEvent.Kind.TRANSFER,
        occurred_at=occurred_at,
        reason=reason,
        to=recipient,
        from_custodian=specimen.custodian,
        destination=destination,
    )
    before = snapshot(specimen)
    specimen.custodian = recipient
    _bump(specimen, "custodian")
    _audit(
        actor,
        "specimens.custody_transferred",
        specimen,
        before=before,
        after=snapshot(specimen),
        reason=reason,
    )
    return specimen


# --- derivation ----------------------------------------------------------------------------


def thaw_basis(specimen):
    """(known total before a derivation, or None, and the thaws recorded so far).

    When the count is not fully known (no baseline at registration, or a time away whose thaws
    were not reported) the first item is None and the second counts every thaw recorded, a known
    baseline included, so a child never loses a recorded thaw.
    """
    from .queries import thaw_summary

    summary = thaw_summary(specimen)
    if summary.known:
        return summary.baseline, summary.recorded
    return None, (summary.baseline or 0) + summary.recorded


@storage.operation()
def derive_specimens(
    *,
    actor,
    parent_id,
    expected_revision,
    expected_occupancy_revision,
    kind,
    consumed,
    children,
    procedure,
    occurred_at,
    reason,
):
    """Split a checked-out (or never stored) specimen into new aliquots or derivatives.

    The parent is debited by `consumed` in its own unit; each child gets a new accession, its own
    initial amount, the parent's participant, collection and collection time, the parent's
    custodian and thaw history, and optionally a position. All or nothing.
    """
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    procedure = _text(procedure, "procedure", 160, required=False)
    if kind not in Derivation.Kind.values:
        raise _error("Choose aliquots or derivatives.", "invalid", "kind")
    children = tuple(children or ())
    if not 1 <= len(children) <= MAX_CHILDREN:
        raise _error(f"Add from 1 to {MAX_CHILDREN} new specimens.", "invalid", "children")
    parent = _lock_specimen(parent_id)
    _check_revision(parent, expected_revision)
    occupancy = _occupancy(parent)
    _check_occupancy_revision(occupancy, expected_occupancy_revision)
    _in_custody(parent)
    if parent.status != Specimen.Status.AVAILABLE:
        raise _error(
            f"Specimen {parent.accession} has nothing left on hand.", "insufficient_quantity"
        )
    _no_all_use_hold(parent, "split or processed")
    _in_hand(parent, occupancy, "split")
    used = parse_quantity(consumed, parent.unit, "consumed")
    if used > parent.quantity_on_hand:
        raise _error(
            f"Only {format(parent.quantity_on_hand.normalize(), 'f')} {parent.unit.code} is on "
            "hand.",
            "insufficient_quantity",
            "consumed",
        )
    _when(occurred_at, floor=occupancy.last_occurred_at)
    specs = []
    errors = {}
    for index, child in enumerate(children):
        try:
            specimen_type = _required(
                _active(SpecimenType, child.specimen_type_id, "specimen_type", "specimen type"),
                "specimen_type",
                "specimen type",
            )
            container_type = _required(
                _active(
                    SpecimenContainerType,
                    child.container_type_id,
                    "container_type",
                    "container type",
                ),
                "container_type",
                "container type",
            )
            unit = _required(_active(QuantityUnit, child.unit_id, "unit", "unit"), "unit", "unit")
            if kind == Derivation.Kind.ALIQUOT and (
                specimen_type.pk != parent.specimen_type_id or unit.pk != parent.unit_id
            ):
                raise _error(
                    "An aliquot keeps the parent's specimen type and unit. Choose derivatives "
                    "to make a different material.",
                    "invalid",
                    "specimen_type" if specimen_type.pk != parent.specimen_type_id else "unit",
                )
            quantity = parse_quantity(child.quantity, unit)
        except ValidationError as exc:
            errors[index] = exc
            continue
        specs.append((child, specimen_type, container_type, unit, quantity))
    if errors:
        raise ValidationError(
            {
                f"children-{i}": [
                    ValidationError(e.message, code=e.code, params=e.params)
                    for e in _flat_errors(exc)
                ]
                for i, exc in errors.items()
            }
        )
    if kind == Derivation.Kind.ALIQUOT:
        total = sum((spec[4] for spec in specs), Decimal(0))
        if total != used:
            raise _error(
                f"The aliquots add up to {format(total.normalize(), 'f')} {parent.unit.code} but "
                f"{format(used.normalize(), 'f')} {parent.unit.code} is taken from the parent. "
                "Record any process loss separately as consumed.",
                "invalid",
                "consumed",
            )
    positions = [spec[0].position_id for spec in specs if spec[0].position_id]
    if len(set(map(str, positions))) != len(positions):
        raise _error("Two new specimens cannot go into the same position.", "occupied", "children")
    derivation = Derivation.objects.create(
        parent=parent,
        kind=kind,
        consumed=used,
        unit=parent.unit,
        procedure=procedure,
        occurred_at=occurred_at,
        actor=actor,
        reason=reason,
    )
    before = snapshot(parent)
    _ledger(
        actor,
        parent,
        kind=QuantityEntry.Kind.DERIVE,
        delta=-used,
        supplied_text=format(used.normalize(), "f"),
        occurred_at=occurred_at,
        reason=reason,
        derivation=derivation,
    )
    baseline, recorded = thaw_basis(parent)
    if baseline is None:
        baseline_reason = (
            f"Derived from {parent.accession}: its thaw history is not fully known; "
            f"{recorded} thaw(s) recorded for it before this derivation"
        )
        child_baseline, inherited = None, recorded
    else:
        child_baseline, inherited = baseline + recorded, 0
        baseline_reason = (
            f"Derived from {parent.accession}, which had {child_baseline} known thaw(s) at the time"
        )
    created = []
    for child, specimen_type, container_type, unit, quantity in specs:
        specimen, child_occupancy = _create_specimen(
            actor=actor,
            occurred_at=occurred_at,
            reason=reason,
            specimen_type=specimen_type,
            container_type=container_type,
            unit=unit,
            quantity=quantity,
            supplied_text=_supplied(child.supplied_text, quantity),
            custodian=parent.custodian,
            collected_at=parent.collected_at,
            processing_started_at=parent.processing_started_at,
            processing_completed_at=parent.processing_completed_at,
            participant=parent.participant,
            collection=parent.collection,
            parent=parent,
            derivation_kind=kind,
            baseline_count=child_baseline,
            inherited_count=inherited,
            baseline_reason=baseline_reason,
            derivation=derivation,
        )
        DerivationChild.objects.create(
            derivation=derivation, child=specimen, quantity=quantity, unit=unit
        )
        if child.position_id:
            _place_new(actor, specimen, child_occupancy, child.position_id, occurred_at, reason)
        created.append(specimen)
    _touch(actor, parent, occupancy, occurred_at, reason)
    _bump(parent, "quantity_on_hand", "status")
    _audit(
        actor,
        "specimens.specimen_derived",
        parent,
        before=before,
        after={
            **snapshot(parent),
            "derivation": str(derivation.pk),
            "kind": kind,
            "consumed": used,
            "children": [c.accession for c in created],
        },
        reason=reason,
    )
    return tuple(Specimen.objects.filter(pk__in=[c.pk for c in created]).order_by("accession"))


# --- quantity ------------------------------------------------------------------------------


@storage.operation()
def consume_quantity(
    *, actor, specimen_id, expected_revision, quantity, supplied_text, occurred_at, reason
):
    """Record material used up (tested, lost in processing) from material in hand."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _in_custody(specimen)
    _no_all_use_hold(specimen, "used")
    _in_hand(specimen, occupancy, "used")
    amount = parse_quantity(quantity, specimen.unit)
    _when(occurred_at, floor=occupancy.last_occurred_at)
    before = snapshot(specimen)
    entry = _ledger(
        actor,
        specimen,
        kind=QuantityEntry.Kind.CONSUME,
        delta=-amount,
        supplied_text=_supplied(supplied_text, amount),
        occurred_at=occurred_at,
        reason=reason,
    )
    _touch(actor, specimen, occupancy, occurred_at, reason)
    _bump(specimen, "quantity_on_hand", "status")
    _audit(
        actor,
        "specimens.quantity_consumed",
        specimen,
        before=before,
        after={**snapshot(specimen), "entry": str(entry.pk)},
        reason=reason,
    )
    return entry


@storage.operation()
def adjust_quantity(
    *, actor, specimen_id, expected_revision, delta, supplied_text, occurred_at, reason
):
    """Manager's reasoned signed adjustment (a measured correction of the amount on hand)."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _in_custody(specimen)
    amount = parse_quantity(delta, specimen.unit, "delta", allow_negative=True)
    _when(occurred_at, floor=occupancy.last_occurred_at)
    before = snapshot(specimen)
    entry = _ledger(
        actor,
        specimen,
        kind=QuantityEntry.Kind.ADJUST,
        delta=amount,
        supplied_text=_supplied(supplied_text, amount),
        occurred_at=occurred_at,
        reason=reason,
    )
    _touch(actor, specimen, occupancy, occurred_at, reason)
    _bump(specimen, "quantity_on_hand", "status")
    _audit(
        actor,
        "specimens.quantity_adjusted",
        specimen,
        before=before,
        after={**snapshot(specimen), "entry": str(entry.pk)},
        reason=reason,
    )
    return entry


@storage.operation()
def reverse_quantity(*, actor, entry_id, expected_revision, reason):
    """Reverse one consume or adjust entry, once, at the current time."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    specimen_id = QuantityEntry.objects.values_list("specimen_id", flat=True).get(pk=entry_id)
    specimen = Specimen.objects.select_for_update(of=("self",)).get(pk=specimen_id)
    _check_revision(specimen, expected_revision)
    entry = QuantityEntry.objects.get(pk=entry_id)
    occupancy = _occupancy(specimen)
    _in_custody(specimen)
    if entry.kind not in (QuantityEntry.Kind.CONSUME, QuantityEntry.Kind.ADJUST):
        raise _error(
            "Only a consumed or adjusted amount can be reversed. Initial amounts, derivations "
            "and disposals need a separate correction review.",
            "invalid_transition",
        )
    if QuantityEntry.objects.filter(reverses=entry).exists():
        raise _error("This entry was already reversed.", "invalid_transition")
    now = tz.now()
    _when(now, floor=occupancy.last_occurred_at, now=now)
    before = snapshot(specimen)
    reversal = _ledger(
        actor,
        specimen,
        kind=QuantityEntry.Kind.REVERSAL,
        delta=-entry.delta,
        supplied_text=format((-entry.delta).normalize(), "f"),
        occurred_at=now,
        reason=reason,
        reverses=entry,
    )
    _touch(actor, specimen, occupancy, now, reason)
    _bump(specimen, "quantity_on_hand", "status")
    _audit(
        actor,
        "specimens.quantity_reversed",
        specimen,
        before=before,
        after={**snapshot(specimen), "entry": str(reversal.pk), "reverses": str(entry.pk)},
        reason=reason,
    )
    return reversal


# --- thaw ----------------------------------------------------------------------------------


@storage.operation()
def record_thaw(*, actor, specimen_id, expected_revision, occurred_at, reason):
    """Record one more observed thaw episode (one that no check-in has already recorded)."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _in_custody(specimen)
    _when(occurred_at, floor=occupancy.last_occurred_at)
    event = ThawEvent.objects.create(
        specimen=specimen,
        kind=ThawEvent.Kind.THAW,
        count=1,
        occurred_at=occurred_at,
        actor=actor,
        reason=reason,
    )
    _touch(actor, specimen, occupancy, occurred_at, reason)
    _bump(specimen)
    _audit(
        actor,
        "specimens.thaw_recorded",
        specimen,
        after={"thaw_event": str(event.pk)},
        reason=reason,
    )
    return event


def _chain_root(event):
    while event.corrects_id:
        event = event.corrects
    return event


@storage.operation()
def correct_thaw(*, actor, event_id, expected_revision, count, reason):
    """Append a replacement for the latest effective version of a thaw record."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    specimen_id = ThawEvent.objects.values_list("specimen_id", flat=True).get(pk=event_id)
    specimen = Specimen.objects.select_for_update(of=("self",)).get(pk=specimen_id)
    _check_revision(specimen, expected_revision)
    target = ThawEvent.objects.get(pk=event_id)
    occupancy = _occupancy(specimen)
    if ThawEvent.objects.filter(corrects=target).exists():
        raise _error(
            "This record was already corrected. Correct the latest version.", "invalid_transition"
        )
    root = _chain_root(target)
    if root.kind == ThawEvent.Kind.THAW:
        if count not in (0, 1):
            raise _error("An observed thaw counts 0 (it did not happen) or 1.", "invalid", "count")
    elif count is not None and (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 0 <= count <= MAX_BASELINE_THAWS
    ):
        raise _error(
            f"Give a whole number from 0 to {MAX_BASELINE_THAWS}, or unknown.", "invalid", "count"
        )
    if count == target.count:
        raise _error("Nothing to correct: the count is unchanged.", "invalid_transition", "count")
    now = tz.now()
    event = ThawEvent.objects.create(
        specimen=specimen,
        kind=ThawEvent.Kind.CORRECTION,
        count=count,
        inherited_recorded_count=target.inherited_recorded_count,
        corrects=target,
        occurred_at=now,
        actor=actor,
        reason=reason,
    )
    _touch(actor, specimen, occupancy, now, reason)
    _bump(specimen)
    _audit(
        actor,
        "specimens.thaw_corrected",
        specimen,
        before={"thaw_event": str(target.pk), "count": target.count},
        after={"thaw_event": str(event.pk), "count": count},
        reason=reason,
    )
    return event


# --- holds ---------------------------------------------------------------------------------


@storage.operation()
def place_hold(*, actor, specimen_id, expected_revision, scope, occurred_at, reason):
    """Place a manual disposal, use or all-use hold (no reference). Any technician may."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    if scope not in Hold.Scope.values:
        raise _error("Choose a disposal hold, a use hold or an all-use hold.", "invalid", "scope")
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _not_disposed(specimen)  # a distributed specimen can be held (holds are not physical)
    _when(occurred_at, floor=occupancy.last_occurred_at)
    hold = Hold.objects.create(
        specimen=specimen, scope=scope, occurred_at=occurred_at, actor=actor, reason=reason
    )
    _touch(actor, specimen, occupancy, occurred_at, reason)
    _bump(specimen)
    _audit(
        actor,
        "specimens.hold_placed",
        specimen,
        after={"hold": str(hold.pk), "scope": scope},
        reason=reason,
    )
    return hold


@storage.operation()
def release_hold(*, actor, hold_id, expected_revision, occurred_at, reason):
    """Release an active hold (repository managers)."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    specimen_id = Hold.objects.values_list("specimen_id", flat=True).get(pk=hold_id)
    specimen = Specimen.objects.select_for_update(of=("self",)).get(pk=specimen_id)
    _check_revision(specimen, expected_revision)
    hold = Hold.objects.get(pk=hold_id)
    occupancy = _occupancy(specimen)
    if HoldRelease.objects.filter(hold=hold).exists():
        raise _error("This hold was already released.", "invalid_transition")
    if hold.reference:
        raise _error(
            f"This hold belongs to “{hold.reference}” and is released through that workflow.",
            "invalid_transition",
        )
    _when(occurred_at, floor=max(occupancy.last_occurred_at, hold.occurred_at))
    release = HoldRelease.objects.create(
        hold=hold, occurred_at=occurred_at, actor=actor, reason=reason
    )
    _touch(actor, specimen, occupancy, occurred_at, reason)
    _bump(specimen)
    _audit(
        actor,
        "specimens.hold_released",
        specimen,
        before={"hold": str(hold.pk), "scope": hold.scope},
        after={"released": True},
        reason=reason,
    )
    return release


# --- distribution support (internal: called by the distribution app inside its operation) ---
#
# These have no specimens route. Each must run inside a storage operation that already holds the
# gate (the caller's): it raises RuntimeError otherwise. Each checks the operating role itself.


def _require_caller_gate():
    """RuntimeError unless the current storage operation holds the StorageGate."""
    storage.require_gate()


@storage.operation()
def place_linked_hold(*, actor, specimen_id, scope, reference, occurred_at, reason):
    """Place a hold owned by another workflow (`reference`, e.g. "Allocated to RQ-000012").

    Only `release_linked_hold` releases it; the generic hold release refuses it.
    """
    _require_caller_gate()
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    reference = _text(reference, "reference", 120, label="the workflow reference")
    if scope not in Hold.Scope.values:
        raise _error("Choose a disposal hold, a use hold or an all-use hold.", "invalid", "scope")
    specimen = _lock_specimen(specimen_id)
    occupancy = _occupancy(specimen)
    _not_disposed(specimen)
    _when(occurred_at, floor=occupancy.last_occurred_at)
    hold = Hold.objects.create(
        specimen=specimen,
        scope=scope,
        reference=reference,
        occurred_at=occurred_at,
        actor=actor,
        reason=reason,
    )
    _touch(actor, specimen, occupancy, occurred_at, reason)
    _bump(specimen)
    _audit(
        actor,
        "specimens.hold_placed",
        specimen,
        after={"hold": str(hold.pk), "scope": scope, "reference": reference},
        reason=reason,
    )
    return hold


@storage.operation()
def release_linked_hold(*, actor, hold_id, occurred_at, reason):
    """Release a hold placed by `place_linked_hold` (its workflow decides when)."""
    _require_caller_gate()
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    specimen_id = Hold.objects.values_list("specimen_id", flat=True).get(pk=hold_id)
    specimen = Specimen.objects.select_for_update(of=("self",)).get(pk=specimen_id)
    hold = Hold.objects.get(pk=hold_id)
    occupancy = _occupancy(specimen)
    if not hold.reference:
        raise _error(
            "This is a manual hold. A repository manager releases it from the specimen record.",
            "invalid_transition",
        )
    if HoldRelease.objects.filter(hold=hold).exists():
        raise _error("This hold was already released.", "invalid_transition")
    _when(occurred_at, floor=max(occupancy.last_occurred_at, hold.occurred_at))
    release = HoldRelease.objects.create(
        hold=hold, occurred_at=occurred_at, actor=actor, reason=reason
    )
    _touch(actor, specimen, occupancy, occurred_at, reason)
    _bump(specimen)
    _audit(
        actor,
        "specimens.hold_released",
        specimen,
        before={"hold": str(hold.pk), "scope": hold.scope, "reference": hold.reference},
        after={"released": True},
        reason=reason,
    )
    return release


@storage.operation()
def dispatch_specimen(*, actor, specimen_id, expected_revision, destination, occurred_at, reason):
    """The specimen leaves the repository: dispatch custody, debit of all on hand, distributed.

    It must be checked out (picked), available and free of use and all-use holds. The open
    check-out ends here; time out of storage stops and the time away begins.
    """
    _require_caller_gate()
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    destination = _text(destination, "destination", 160, label="where it is sent")
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _in_custody(specimen)
    if specimen.status != Specimen.Status.AVAILABLE:
        raise _error(
            f"Specimen {specimen.accession} has nothing on hand to send.", "invalid_transition"
        )
    if not specimen.open_checkout_id:
        raise _error(
            f"Specimen {specimen.accession} is not checked out. It is picked (checked out) "
            "before it is dispatched.",
            "invalid_transition",
        )
    _no_all_use_hold(specimen, "dispatched")
    _when(occurred_at, floor=occupancy.last_occurred_at)
    before = snapshot(specimen)
    event = CustodyEvent.objects.create(
        specimen=specimen,
        kind=CustodyEvent.Kind.DISPATCH,
        actor=actor,
        occurred_at=occurred_at,
        reason=reason,
        from_custodian=specimen.custodian,
        to_custodian=None,
        destination=destination,
    )
    sent = specimen.quantity_on_hand
    _ledger(
        actor,
        specimen,
        kind=QuantityEntry.Kind.DISTRIBUTE,
        delta=-sent,
        supplied_text=format(sent.normalize(), "f"),
        occurred_at=occurred_at,
        reason=reason,
    )
    specimen.status = Specimen.Status.DISTRIBUTED
    specimen.custodian = None
    specimen.open_checkout = None
    _touch(actor, specimen, occupancy, occurred_at, reason)
    _bump(specimen, "status", "quantity_on_hand", "custodian", "open_checkout")
    _audit(
        actor,
        "specimens.specimen_dispatched",
        specimen,
        before=before,
        after={**snapshot(specimen), "destination": destination, "custody_event": str(event.pk)},
        reason=reason,
    )
    return event


@storage.operation()
def record_return(
    *,
    actor,
    specimen_id,
    expected_revision,
    quantity,
    supplied_text,
    recipient_id,
    thaw_count,
    source,
    occurred_at,
    reason,
):
    """A distributed specimen comes back: return custody to `recipient_id`, open like a check-out.

    `quantity` is the measured amount returned (0 allowed: the specimen becomes exhausted).
    `thaw_count` is what the recipient reported for the time away, or None when not known.
    Storing it afterwards is an ordinary check-in.
    """
    _require_caller_gate()
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    source = _text(source, "source", 160, label="where it came back from")
    if thaw_count is not None and (
        isinstance(thaw_count, bool)
        or not isinstance(thaw_count, int)
        or not 0 <= thaw_count <= MAX_BASELINE_THAWS
    ):
        raise _error(
            f"Give the reported thaws as a whole number from 0 to {MAX_BASELINE_THAWS}, or "
            "unknown.",
            "invalid",
            "thaw_count",
        )
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _not_disposed(specimen)
    if specimen.status != Specimen.Status.DISTRIBUTED:
        raise _error(
            f"Specimen {specimen.accession} was not distributed, so it cannot be returned.",
            "invalid_transition",
        )
    amount = parse_quantity(quantity, specimen.unit, allow_zero=True)
    recipient = _recipient(recipient_id)
    _when(occurred_at, floor=occupancy.last_occurred_at)
    before = snapshot(specimen)
    event = CustodyEvent.objects.create(
        specimen=specimen,
        kind=CustodyEvent.Kind.RETURN,
        actor=actor,
        occurred_at=occurred_at,
        reason=reason,
        from_custodian=None,
        to_custodian=recipient,
        destination=source,
    )
    if amount > 0:
        _ledger(
            actor,
            specimen,
            kind=QuantityEntry.Kind.RETURN,
            delta=amount,
            supplied_text=_supplied(supplied_text, amount),
            occurred_at=occurred_at,
            reason=reason,
        )
    else:
        specimen.status = Specimen.Status.EXHAUSTED
    ThawEvent.objects.create(
        specimen=specimen,
        kind=ThawEvent.Kind.EXTERNAL,
        count=thaw_count,
        occurred_at=occurred_at,
        actor=actor,
        reason=f"Reported for the time away. {reason}",
        custody_event=event,
    )
    specimen.custodian = recipient
    specimen.open_checkout = event
    _touch(actor, specimen, occupancy, occurred_at, reason)
    _bump(specimen, "status", "quantity_on_hand", "custodian", "open_checkout")
    _audit(
        actor,
        "specimens.specimen_returned",
        specimen,
        before=before,
        after={
            **snapshot(specimen),
            "source": source,
            "thaw_count": thaw_count,
            "custody_event": str(event.pk),
        },
        reason=reason,
    )
    return event


# --- disposal ------------------------------------------------------------------------------


def approval_is_current(decision, specimen, occupancy):
    return (
        decision.decision == DisposalDecision.Decision.APPROVE
        and decision.approved_revision == specimen.revision
        and decision.approved_occupancy_revision == occupancy.revision
    )


def request_state(request, specimen=None, occupancy=None):
    """pending | approved | stale | rejected | cancelled | executed for a disposal request."""
    specimen = specimen or request.specimen
    occupancy = occupancy or Occupancy.objects.get(pk=specimen.occupancy_id)
    decision = request.decision if hasattr(request, "decision") else None
    if hasattr(request, "execution"):
        return "executed"
    if decision is None:
        if (request.specimen_revision, request.occupancy_revision) != (
            specimen.revision,
            occupancy.revision,
        ):
            return "stale"
        return "pending"
    if decision.decision == DisposalDecision.Decision.APPROVE:
        return "approved" if approval_is_current(decision, specimen, occupancy) else "stale"
    return "rejected" if decision.decision == DisposalDecision.Decision.REJECT else "cancelled"


def _unresolved(specimen, occupancy):
    for request in DisposalRequest.objects.filter(specimen=specimen).select_related(
        "decision", "execution"
    ):
        if request_state(request, specimen, occupancy) in ("pending", "approved"):
            return request
    return None


@storage.operation()
def request_disposal(*, actor, specimen_id, expected_revision, method, destination, reason):
    """Ask for a specimen's disposal. Another person with disposal approval must approve it."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    method = _text(method, "method", 160, label="the disposal method")
    destination = _text(destination, "destination", 160, label="the destination")
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _in_custody(specimen)
    _no_hold(specimen)
    if _unresolved(specimen, occupancy):
        raise _error(
            "This specimen already has an open disposal request. Decide or cancel it first.",
            "invalid_transition",
        )
    now = tz.now()
    occupancy = _touch(actor, specimen, occupancy, now, reason)
    _bump(specimen)
    request = DisposalRequest.objects.create(
        specimen=specimen,
        method=method,
        destination=destination,
        reason=reason,
        requested_by=actor,
        recorded_at=now,
        specimen_revision=specimen.revision,
        occupancy_revision=occupancy.revision,
    )
    _audit(
        actor,
        "specimens.disposal_requested",
        specimen,
        after={"request": str(request.pk), "method": method, "destination": destination},
        reason=reason,
    )
    return request


@storage.operation()
def decide_disposal(*, actor, request_id, expected_revision, decision, reason):
    """Approve or reject (explicit permission, never one's own request) or cancel a request."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    if decision not in DisposalDecision.Decision.values:
        raise _error("Choose approve, reject or cancel.", "invalid", "decision")
    storage.lock_gate()
    specimen_id = DisposalRequest.objects.values_list("specimen_id", flat=True).get(pk=request_id)
    specimen = Specimen.objects.select_for_update(of=("self",)).get(pk=specimen_id)
    request = DisposalRequest.objects.select_for_update().get(pk=request_id)
    if decision == DisposalDecision.Decision.CANCEL:
        if request.requested_by_id != actor.pk and not has_role(actor, *MANAGE_ROLES):
            raise PermissionDenied("Only the requester or a repository manager can cancel it.")
    else:
        # Superuser status never counts; the requester never decides their own request.
        if not can_approve_disposal(actor):
            raise PermissionDenied("Explicit disposal approval permission is required.")
        if request.requested_by_id == actor.pk:
            raise _error(
                "You requested this. Another person with disposal approval must approve it.",
                "self_approval",
            )
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    if DisposalDecision.objects.filter(request=request).exists():
        raise _error("This request was already decided.", "invalid_transition")
    if decision == DisposalDecision.Decision.APPROVE:
        _in_custody(specimen)
        _no_hold(specimen)
        if (request.specimen_revision, request.occupancy_revision) != (
            specimen.revision,
            occupancy.revision,
        ):
            raise _error(
                "The specimen changed after this request was made, so the request is out of "
                "date. Ask for a new disposal request.",
                "stale_approval",
            )
    now = tz.now()
    occupancy = _touch(actor, specimen, occupancy, now, reason)
    _bump(specimen)
    approve = decision == DisposalDecision.Decision.APPROVE
    row = DisposalDecision.objects.create(
        request=request,
        decision=decision,
        actor=actor,
        recorded_at=now,
        reason=reason,
        approved_revision=specimen.revision if approve else None,
        approved_occupancy_revision=occupancy.revision if approve else None,
    )
    _audit(
        actor,
        f"specimens.disposal_{row.get_decision_display().lower()}",
        specimen,
        after={"request": str(request.pk), "decision": decision},
        reason=reason,
    )
    return row


@storage.operation()
def execute_disposal(
    *, actor, request_id, expected_revision, expected_occupancy_revision, occurred_at, reason
):
    """Record the approved disposal as done: frees the position, debits the rest, keeps history."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    specimen_id = DisposalRequest.objects.values_list("specimen_id", flat=True).get(pk=request_id)
    specimen = Specimen.objects.select_for_update(of=("self",)).get(pk=specimen_id)
    request = DisposalRequest.objects.select_for_update().get(pk=request_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    _check_occupancy_revision(occupancy, expected_occupancy_revision)
    _in_custody(specimen)
    decision = DisposalDecision.objects.filter(request=request).first()
    if decision is None or decision.decision != DisposalDecision.Decision.APPROVE:
        raise _error("This disposal has not been approved.", "approval_required")
    if DisposalExecution.objects.filter(request=request).exists():
        raise _error("This disposal was already recorded.", "invalid_transition")
    if not approval_is_current(decision, specimen, occupancy):
        raise _error(
            "The specimen changed after the approval, so the approval is out of date. "
            "Ask for a new disposal request.",
            "stale_approval",
        )
    approver = decision.actor
    if approver.pk == request.requested_by_id or not can_approve_disposal(approver):
        raise _error(
            "The approval is no longer valid: the approver lacks disposal approval.",
            "approval_required",
        )
    _no_hold(specimen)
    _when(occurred_at, floor=max(occupancy.last_occurred_at, decision.recorded_at))
    before = snapshot(specimen)
    move = _move(
        actor, occupancy, kind="dispose", position_id=None, occurred_at=occurred_at, reason=reason
    )
    custody = _custody(
        actor,
        specimen,
        kind=CustodyEvent.Kind.DISPOSE,
        occurred_at=occurred_at,
        reason=reason,
        to=actor,
        from_custodian=specimen.custodian,
        destination=request.destination,
        move_event=move,
    )
    if specimen.quantity_on_hand > 0:
        _ledger(
            actor,
            specimen,
            kind=QuantityEntry.Kind.DISPOSE,
            delta=-specimen.quantity_on_hand,
            supplied_text=format(specimen.quantity_on_hand.normalize(), "f"),
            occurred_at=occurred_at,
            reason=reason,
        )
    execution = DisposalExecution.objects.create(
        request=request,
        decision=decision,
        specimen=specimen,
        custody_event=custody,
        occurred_at=occurred_at,
        actor=actor,
        reason=reason,
    )
    specimen.status = Specimen.Status.DISPOSED
    specimen.quantity_on_hand = Decimal(0)
    specimen.custodian = actor
    specimen.open_checkout = None
    _bump(specimen, "status", "quantity_on_hand", "custodian", "open_checkout")
    _audit(
        actor,
        "specimens.specimen_disposed",
        specimen,
        before=before,
        after={
            **snapshot(specimen),
            "request": str(request.pk),
            "method": request.method,
            "destination": request.destination,
            "approved_by": approver.pk,
        },
        reason=reason,
    )
    return execution


# --- corrections ---------------------------------------------------------------------------


def _correction_display(name, value):
    if value is None:
        return None
    if hasattr(value, "code"):
        return value.code
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@storage.operation()
def correct_specimen(*, actor, specimen_id, expected_revision, values, reason):
    """Correct allow-listed metadata with a reason; after disposal only an annotation is kept."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    names = set(values.fields)
    if not names or names - set(CORRECTABLE_FIELDS):
        raise _error("Choose what to correct.", "invalid")
    specimen = _lock_specimen(specimen_id)
    _check_revision(specimen, expected_revision)
    occupancy = _occupancy(specimen)
    proposed = {}
    for name in names:
        if name in ("specimen_type", "container_type", "participant", "collection"):
            model, noun = {
                "specimen_type": (SpecimenType, "specimen type"),
                "container_type": (SpecimenContainerType, "container type"),
                "participant": (Participant, "participant"),
                "collection": (Collection, "collection"),
            }[name]
            value = _active(model, getattr(values, f"{name}_id"), name, noun)
            if name in ("specimen_type", "container_type"):
                _required(value, name, noun)
        else:
            value = getattr(values, name)
        proposed[name] = value
    changed = {
        name: value
        for name, value in proposed.items()
        if _comparable(getattr(specimen, name)) != _comparable(value)
    }
    if not changed:
        raise _error("Nothing to correct: the values are unchanged.", "invalid_transition")
    if specimen.parent_id and set(changed) & set(INHERITED_FIELDS):
        raise _error(
            "Participant, collection and collection time come from the parent specimen. "
            "Correct the parent instead.",
            "invalid_transition",
        )
    if specimen.status != Specimen.Status.DISPOSED and set(changed) & set(LINEAGE_FIXED_FIELDS):
        if Specimen.objects.filter(parent=specimen).exists():
            raise _error(
                "Aliquots or derivatives were made from this specimen, so its type, participant, "
                "collection and times cannot be changed here. The owner must review the "
                "lineage first.",
                "invalid_transition",
            )
    before = {name: _correction_display(name, getattr(specimen, name)) for name in changed}
    after = {name: _correction_display(name, value) for name, value in changed.items()}
    annotation = specimen.status == Specimen.Status.DISPOSED
    if not annotation:
        merged = {
            name: changed.get(name, getattr(specimen, name))
            for name in ("collected_at", "processing_started_at", "processing_completed_at")
        }
        _validate_times(
            merged["collected_at"],
            merged["processing_started_at"],
            merged["processing_completed_at"],
            now=tz.now(),
        )
        registered = specimen.custody.filter(kind=CustodyEvent.Kind.REGISTER).first()
        floor = max(t for t in (merged["collected_at"], merged["processing_completed_at"]) if t)
        if registered and registered.occurred_at < floor:
            raise _error(
                "Collection and processing must be before registration.",
                "invalid_time",
                "collected_at",
            )
        for name, value in changed.items():
            setattr(specimen, name, value)
    correction = Correction.objects.create(
        specimen=specimen,
        actor=actor,
        reason=reason,
        before=_json(before),
        after=_json(after),
        annotation=annotation,
    )
    _touch(actor, specimen, occupancy, tz.now(), reason)
    _bump(specimen, *([] if annotation else list(changed)))
    _audit(
        actor,
        "specimens.specimen_annotated" if annotation else "specimens.specimen_corrected",
        specimen,
        before=before,
        after={**after, "correction": str(correction.pk)},
        reason=reason,
    )
    return correction


MAX_FAMILY = 2000


def _family(primary):
    """The primary and every aliquot or derivative made from it, at any depth."""
    ids, frontier = [primary.pk], [primary.pk]
    while frontier:
        frontier = list(
            Specimen.objects.filter(parent_id__in=frontier).values_list("pk", flat=True)
        )
        ids.extend(frontier)
        if len(ids) > MAX_FAMILY:
            raise _error(
                f"This family has more than {MAX_FAMILY} specimens; ask for a bulk correction.",
                "invalid_transition",
            )
    return ids


@storage.operation()
def assign_lineage_context(
    *, actor, specimen_id, expected_revision, participant_id, collection_id, reason
):
    """Give a primary specimen, and every specimen made from it, a missing participant or
    collection: one Correction per specimen. Values already recorded are never replaced here.
    """
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    if participant_id in (None, "") and collection_id in (None, ""):
        raise _error("Choose a participant, a collection or both.", "required")
    primary = _lock_specimen(specimen_id)
    _check_revision(primary, expected_revision)
    if primary.parent_id:
        raise _error(
            f"{primary.accession} was made from another specimen. Assign the context on the "
            "primary specimen of its family.",
            "invalid_transition",
        )
    values = {}
    if participant_id not in (None, ""):
        values["participant"] = _active(Participant, participant_id, "participant", "participant")
    if collection_id not in (None, ""):
        values["collection"] = _active(Collection, collection_id, "collection", "collection")
    ids = _family(primary)
    family = list(
        Specimen.objects.select_for_update(of=("self",))
        .filter(pk__in=ids)
        .select_related("participant", "collection")
        .order_by("pk")
    )
    for name in values:
        recorded = [s for s in family if getattr(s, f"{name}_id") is not None]
        if recorded:
            raise _error(
                f"{recorded[0].accession} already has a {name}. This only fills a missing "
                f"{name}; a recorded one is corrected on the specimen record.",
                "invalid_transition",
                name,
            )
    now = tz.now()
    for specimen in family:
        before = {name: None for name in values}
        after = {name: value.code for name, value in values.items()}
        for name, value in values.items():
            setattr(specimen, name, value)
        correction = Correction.objects.create(
            specimen=specimen,
            actor=actor,
            reason=reason,
            before=_json(before),
            after=_json(after),
            annotation=False,
        )
        _touch(actor, specimen, _occupancy(specimen), now, reason)
        _bump(specimen, *values)
        _audit(
            actor,
            "specimens.lineage_context_assigned",
            specimen,
            before=before,
            after={**after, "correction": str(correction.pk), "primary": primary.accession},
            reason=reason,
        )
    return tuple(Specimen.objects.filter(pk__in=ids).order_by("accession"))


CUSTODY_CORRECTABLE = ("destination", "from_custodian_id", "to_custodian_id")


@storage.operation()
def correct_custody(*, actor, event_id, expected_revision, values, reason):
    """Append a correction of a custody event's destination or custodians (latest version only).

    Times, kinds, links and locations are never rewritten; those mistakes need a compensating
    operation.
    """
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    unknown = set(values or {}) - set(CUSTODY_CORRECTABLE)
    if unknown or not values:
        raise _error("Only the destination and custodians can be corrected.", "invalid")
    storage.lock_gate()
    specimen_id = CustodyEvent.objects.values_list("specimen_id", flat=True).get(pk=event_id)
    specimen = Specimen.objects.select_for_update(of=("self",)).get(pk=specimen_id)
    _check_revision(specimen, expected_revision)
    target = CustodyEvent.objects.get(pk=event_id)
    occupancy = _occupancy(specimen)
    if CustodyEvent.objects.filter(corrects=target).exists():
        raise _error(
            "This event was already corrected. Correct the latest version.", "invalid_transition"
        )
    new = {
        "destination": target.destination,
        "from_custodian_id": target.from_custodian_id,
        "to_custodian_id": target.to_custodian_id,
    }
    if "destination" in values:
        new["destination"] = _text(values["destination"], "destination", 160, required=False)
    root = _chain_root(target)
    for key in ("from_custodian_id", "to_custodian_id"):
        if key in values and values[key] not in (None, ""):
            if key == "to_custodian_id" and root.kind == CustodyEvent.Kind.DISPATCH:
                raise _error(
                    "A dispatch has no staff recipient: the material left the repository. "
                    "Correct the destination instead.",
                    "invalid",
                    key,
                )
            if key == "from_custodian_id" and root.kind == CustodyEvent.Kind.RETURN:
                raise _error(
                    "A return comes from outside the repository; it has no staff member handing "
                    "it over. Correct where it came from instead.",
                    "invalid",
                    key,
                )
            user = User.objects.filter(pk=values[key]).first()
            if user is None:
                raise _error("Choose a staff member.", "invalid", key)
            new[key] = user.pk
    if (
        root.kind not in (CustodyEvent.Kind.REGISTER, CustodyEvent.Kind.RETURN)
        and new["from_custodian_id"] is None
    ):
        raise _error("Choose who handed it over.", "required", "from_custodian_id")
    if new == {k: getattr(target, k) for k in new}:
        raise _error("Nothing to correct: the values are unchanged.", "invalid_transition")
    now = tz.now()
    event = CustodyEvent.objects.create(
        specimen=specimen,
        kind=CustodyEvent.Kind.CORRECTION,
        corrects=target,
        occurred_at=now,
        actor=actor,
        reason=reason,
        destination=new["destination"],
        from_custodian_id=new["from_custodian_id"],
        to_custodian_id=new["to_custodian_id"],
    )
    latest = (
        specimen.custody.exclude(kind=CustodyEvent.Kind.CORRECTION)
        .order_by("-occurred_at", "-recorded_at", "-id")
        .first()
    )
    fields = []
    if latest and latest.pk == root.pk and specimen.custodian_id != new["to_custodian_id"]:
        specimen.custodian_id = new["to_custodian_id"]
        fields.append("custodian")
    _touch(actor, specimen, occupancy, now, reason)
    _bump(specimen, *fields)
    _audit(
        actor,
        "specimens.custody_corrected",
        specimen,
        before={"event": str(target.pk), **{k: getattr(target, k) for k in new}},
        after={"event": str(event.pk), **new},
        reason=reason,
    )
    return event


# Convenience re-exports for callers that treat import as part of the service API.
def validate_import(*, actor, csv_bytes, filename=""):
    from .importing import validate_import as run

    return run(actor=actor, csv_bytes=csv_bytes, filename=filename)


def commit_import(*, actor, batch_id, expected_sha256, reason):
    from .importing import commit_import as run

    return run(actor=actor, batch_id=batch_id, expected_sha256=expected_sha256, reason=reason)
