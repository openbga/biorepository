"""Equipment mutations: register and status, service events, LN2 log, temperature readings and
their CSV import, excursions.

Every function runs in one transaction, checks the actor's role before reading records, locks the
equipment row (and the excursion or the entry being corrected), revalidates against the locked
state, writes the rows and records an attributed audit event (`core.audit.record_event`) in the
same transaction. Log entries are append-only: a correction adds a row that `corrects` the earlier
one, with a reason, either replacing its values or withdrawing it (`void`).

The repository enters its own limits, refill levels and next-due dates; nothing here proposes one.
Validation failures raise ValidationError keyed by field where one applies; role failures raise
PermissionDenied.
"""

import csv
import hashlib
import io
import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.utils import timezone as tz

from accounts.models import User
from accounts.roles import has_role, require_role
from core.attachments import attachment_fields, store_attachment
from core.audit import record_event
from specimens import services as specimen_services
from specimens.models import Hold, Specimen
from storage import services as storage
from storage.models import StorageUnit

from .models import (
    ABSOLUTE_ZERO,
    AffectedSpecimen,
    AmountUnit,
    Equipment,
    Excursion,
    ExcursionEvent,
    LevelUnit,
    LN2Entry,
    ReadingImport,
    ReadingImportCommit,
    ServiceEvent,
    StatusChange,
    TemperatureReading,
)
from .occupancy import affected_specimens

READ_ROLES = storage.READ_ROLES  # admin, manager, technician, viewer
OPERATE_ROLES = storage.OPERATE_ROLES  # admin, manager, technician
MANAGE_ROLES = storage.MANAGE_ROLES  # admin, manager
TEXT_LIMIT = 4000
IMPORT_MAX_BYTES = 5 * 1024 * 1024
IMPORT_MAX_ROWS = 10000
IMPORT_LIFETIME = timedelta(hours=24)
EXCURSION_HOLD_LIMIT = 200

EQUIPMENT_FIELDS = (
    "code",
    "name",
    "kind",
    "storage_unit",
    "location",
    "manufacturer",
    "model",
    "serial_number",
    "is_critical",
    "installed_on",
    "service_provider",
    "notes",
    "lower_limit",
    "upper_limit",
    "level_unit",
    "level_low_limit",
)
SERVICE_FIELDS = (
    "kind",
    "performed_on",
    "performed_by",
    "performed_by_external",
    "outcome",
    "reference",
    "description",
    "reference_device",
    "as_found",
    "as_left",
    "next_due_on",
)
LN2_FIELDS = (
    "kind",
    "occurred_at",
    "method",
    "level_before",
    "level",
    "level_unit",
    "amount_added",
    "amount_unit",
    "supply",
    "reference",
    "performed_by_external",
    "note",
)
READING_FIELDS = ("measured_at", "value", "probe_position", "device", "note")
EXCURSION_FIELDS = (
    "started_at",
    "ended_at",
    "extreme_value",
    "extreme_at",
    "detection",
    "cause_category",
    "cause",
    "action",
    "impact",
)
EXCURSION_SNAPSHOT = (
    "equipment",
    "storage_unit",
    *EXCURSION_FIELDS,
    "lower_limit",
    "upper_limit",
    "status",
    "submitted_by",
    "closed_by",
    "closure_note",
)


# ------------------------------------------------------------------ helpers


def _error(message, code="invalid", field=None):
    error = ValidationError(message, code=code)
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


def _moment(value, field, *, required=True, label="the date and time", now=None):
    """An aware date-time, not in the future."""
    if value is None:
        if required:
            raise _error(f"Enter {label}.", "required", field)
        return None
    if not isinstance(value, datetime) or tz.is_naive(value):
        raise _error(f"Enter {label} with its date and time.", "invalid_time", field)
    if value > (now or tz.now()):
        raise _error("The time cannot be in the future.", "invalid_time", field)
    return value


def _day(value, field, *, required=True, past=True, label="the date"):
    if value is None:
        if required:
            raise _error(f"Enter {label}.", "required", field)
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise _error(f"Enter {label}.", "invalid", field)
    if past and value > tz.localdate():
        raise _error("The date cannot be in the future.", "invalid_time", field)
    return value


def _decimal(value, field, *, label, required=False, minimum=None, maximum=None, digits=6):
    """An exact decimal with at most two places; binary floats are refused."""
    if value is None or value == "":
        if required:
            raise _error(f"Enter {label}.", "required", field)
        return None
    if isinstance(value, (float, bool)):
        raise _error(f"Enter {label} as an exact decimal.", "invalid", field)
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation:
        raise _error(f"Enter {label} as a number.", "invalid", field) from None
    if not number.is_finite():
        raise _error(f"Enter {label} as a number.", "invalid", field)
    if number.as_tuple().exponent < -2:
        raise _error("Use at most two decimal places.", "invalid", field)
    if abs(number) >= Decimal(10) ** (digits - 2):
        raise _error(f"Enter a smaller value for {label}.", "invalid", field)
    if minimum is not None and number < minimum:
        raise _error(f"Enter {label} of at least {minimum}.", "invalid", field)
    if maximum is not None and number > maximum:
        raise _error(f"Enter {label} of at most {maximum}.", "invalid", field)
    # At the stored scale, so 31.5 and 31.50 compare (and snapshot) as the same value.
    return number.quantize(Decimal("0.01"))


def _temperature(value, field, *, label="the temperature", required=False):
    return _decimal(value, field, label=label, required=required, minimum=ABSOLUTE_ZERO)


def _choice(value, choices, field, label):
    if value not in dict(choices):
        raise _error(f"Choose {label}.", "invalid", field)
    return value


def _json(data):
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


def state(obj, fields):
    """JSON-safe snapshot of the named fields (foreign keys as their key) for audit events."""
    data = {}
    for name in fields:
        field = obj._meta.get_field(name)
        data[name] = getattr(obj, f"{name}_id") if field.is_relation else getattr(obj, name)
    return _json(data)


def _check_revision(obj, expected, noun):
    if expected is None or obj.revision != expected:
        raise _error(
            f"This {noun} changed since you opened it. Reload it and try again.", "stale_revision"
        )


def _lock_equipment(equipment_id):
    try:
        return Equipment.objects.select_for_update().get(pk=equipment_id)
    except Equipment.DoesNotExist, ValidationError, ValueError:
        raise _error("Choose equipment from the register.", "invalid", "equipment") from None


def _not_retired(equipment):
    if equipment.is_retired:
        raise _error(
            "This equipment is retired. Return it to service to record more entries.",
            "invalid_transition",
        )


def _staff(user_or_id, field, label="staff member"):
    user = (
        user_or_id if isinstance(user_or_id, User) else User.objects.filter(pk=user_or_id).first()
    )
    if user is None or not user.is_active or not has_role(user, *READ_ROLES):
        raise _error(f"Choose an active {label}.", "invalid", field)
    return user


def _related_equipment(value, field, *, kinds, label, keep=None):
    """Another register entry of the given kinds (supply dewar, monitor), or None. `keep` is the
    one a corrected entry already names, accepted even if it has since been retired."""
    if value in (None, ""):
        return None
    pk = getattr(value, "pk", value)
    found = Equipment.objects.filter(pk=pk).first() if pk else None
    if found is not None and keep is not None and str(found.pk) == str(keep):
        return found
    if found is None or found.kind not in kinds or found.is_retired:
        raise _error(f"Choose {label} from the register.", "invalid", field)
    return found


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
    """The referenced file in audit snapshots: its record and checksum, never its bytes."""
    if not row.attachment_id:
        return {"attachment": None, "attachment_sha256": ""}
    return {"attachment": str(row.attachment_id), "attachment_sha256": row.attachment.sha256}


def _may_correct(actor, entry):
    """Managers correct any entry; others only the entries they recorded."""
    require_role(actor, *OPERATE_ROLES)
    if has_role(actor, *MANAGE_ROLES):
        return
    if entry.recorded_by_id != actor.pk:
        raise PermissionDenied(
            "Only the person who recorded this entry or a manager may correct it."
        )


def _lock_entry(model, entry_id, label):
    """Lock the entry's equipment, then the entry; refuse corrected or withdrawn entries."""
    try:
        equipment_id = model.objects.values_list("equipment_id", flat=True).get(pk=entry_id)
    except model.DoesNotExist, ValidationError, ValueError:
        raise _error(f"This {label} does not exist.", "invalid") from None
    equipment = _lock_equipment(equipment_id)
    entry = model.objects.select_for_update().get(pk=entry_id)
    if model.objects.filter(corrects_id=entry.pk).exists():
        raise _error(
            f"This {label} was already corrected. Correct the newer entry.", "invalid_transition"
        )
    if entry.voided:
        raise _error(f"This {label} was withdrawn. Record a new one instead.", "invalid_transition")
    return equipment, entry


# ------------------------------------------------------------------ register


def _equipment_values(data, *, existing=None):
    """Validated register values from `data`; keys missing from `data` keep `existing` values."""
    current = state(existing, EQUIPMENT_FIELDS) if existing else {}

    def get(name, default=None):
        if name in data:
            return data[name]
        if existing is not None:
            return getattr(existing, name)
        return default

    values = {}
    values["code"] = storage.normalize_code(get("code", ""), "code")
    if existing is not None and values["code"] != existing.code and _has_records(existing):
        raise _error(
            "The asset ID cannot change once entries are recorded for this equipment: logs, "
            "files and labels refer to it.",
            "invalid",
            "code",
        )
    values["name"] = _text(get("name", ""), "name", 160, required=True, label="a name")
    values["kind"] = _choice(get("kind"), Equipment.Kind.choices, "kind", "a kind of equipment")
    if existing is not None and values["kind"] != existing.kind and _has_records(existing):
        raise _error(
            "The kind cannot change once entries are recorded for this equipment.",
            "invalid",
            "kind",
        )
    unit = get("storage_unit")
    if unit not in (None, ""):
        pk = getattr(unit, "pk", unit)
        unit = StorageUnit.objects.filter(pk=pk).first() if pk else None
        if unit is None:
            raise _error("Choose a storage unit.", "invalid", "storage_unit")
        linked_before = current.get("storage_unit") == str(unit.pk)
        if not unit.is_active and not linked_before:
            raise _error("This storage unit is retired.", "inactive_location", "storage_unit")
    else:
        unit = None
    if values["kind"] == Equipment.Kind.SUPPLY_DEWAR and unit is not None:
        raise _error(
            "A supply dewar is not a storage unit. Leave the storage unit empty.",
            "invalid",
            "storage_unit",
        )
    values["storage_unit"] = unit
    for name, limit in (
        ("location", 160),
        ("manufacturer", 120),
        ("model", 120),
        ("serial_number", 120),
        ("service_provider", 160),
        ("notes", TEXT_LIMIT),
    ):
        values[name] = _text(get(name, ""), name, limit)
    values["is_critical"] = bool(get("is_critical", False))
    values["installed_on"] = _day(get("installed_on"), "installed_on", required=False)
    lower = _temperature(get("lower_limit"), "lower_limit", label="the lower limit")
    upper = _temperature(get("upper_limit"), "upper_limit", label="the upper limit")
    if values["kind"] == Equipment.Kind.MONITOR and (lower is not None or upper is not None):
        raise _error(
            "Set temperature limits on the tank or freezer the monitor watches.",
            "invalid",
            "lower_limit",
        )
    if lower is not None and upper is not None and lower >= upper:
        raise _error("The lower limit must be below the upper limit.", "invalid", "upper_limit")
    values["lower_limit"], values["upper_limit"] = lower, upper
    level_unit = get("level_unit", "") or ""
    level_low = _decimal(
        get("level_low_limit"),
        "level_low_limit",
        label="the low level",
        minimum=Decimal(0),
        digits=8,
    )
    if values["kind"] not in Equipment.LN2_KINDS and (level_unit or level_low is not None):
        raise _error(
            "Level settings apply to LN2 tanks and supply dewars.", "invalid", "level_unit"
        )
    if level_unit:
        _choice(level_unit, LevelUnit.choices, "level_unit", "a level unit")
    if level_low is not None and not level_unit:
        raise _error("Choose the unit the low level is in.", "required", "level_unit")
    if level_unit == "%" and level_low is not None and level_low > 100:
        raise _error("A percentage cannot exceed 100.", "invalid", "level_low_limit")
    values["level_unit"], values["level_low_limit"] = level_unit, level_low
    return values


def _has_records(equipment):
    return (
        equipment.service_events.exists()
        or equipment.ln2_entries.exists()
        or equipment.readings.exists()
        or equipment.excursions.exists()
    )


@transaction.atomic
def save_equipment(*, actor, equipment_id=None, expected_revision=None, reason="", **data):
    """Register equipment or edit its details and limits. Status changes use `change_status`."""
    require_role(actor, *MANAGE_ROLES)
    unknown = set(data) - set(EQUIPMENT_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {', '.join(sorted(unknown))}.")
    if equipment_id is None:
        values = _equipment_values(data)
        obj = Equipment(created_by=actor, **values)
        before = {}
        reason = reason.strip() if isinstance(reason, str) else ""
    else:
        obj = _lock_equipment(equipment_id)
        _check_revision(obj, expected_revision, "equipment record")
        reason = need_reason(reason)
        before = state(obj, EQUIPMENT_FIELDS)
        values = _equipment_values(data, existing=obj)
        for key, value in values.items():
            setattr(obj, key, value)
        if state(obj, EQUIPMENT_FIELDS) == before:
            raise _error("Nothing to save: no value changed.", "invalid_transition")
        obj.revision += 1
    if Equipment.objects.filter(code=values["code"]).exclude(pk=obj.pk).exists():
        raise _error("This asset ID is already registered.", "unique", "code")
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError as exc:
        if "equipment_one_enclosure_per_unit" in str(exc):
            raise _error(
                "This storage unit already has a tank or freezer in the register.",
                "unique",
                "storage_unit",
            ) from exc
        raise _error("This asset ID is already registered.", "unique", "code") from exc
    _audit(
        actor,
        "equipment.updated" if before else "equipment.registered",
        obj,
        before=before,
        after=state(obj, EQUIPMENT_FIELDS),
        reason=reason,
    )
    return obj


@transaction.atomic
def change_status(*, actor, equipment_id, expected_revision, status, occurred_at, reason):
    """Put equipment in or out of service, or retire it, as of a stated time."""
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    obj = _lock_equipment(equipment_id)
    _check_revision(obj, expected_revision, "equipment record")
    _choice(status, Equipment.Status.choices, "status", "a status")
    if status == obj.status:
        raise _error("The equipment already has this status.", "invalid_transition", "status")
    now = tz.now()
    _moment(occurred_at, "occurred_at", label="when the status changed", now=now)
    latest = obj.status_changes.order_by("-occurred_at", "-recorded_at").first()
    if latest and occurred_at < latest.occurred_at:
        local = tz.localtime(latest.occurred_at).strftime("%Y-%m-%d %H:%M")
        raise _error(
            f"The time is earlier than the last status change ({local}).",
            "invalid_time",
            "occurred_at",
        )
    if (
        status == Equipment.Status.RETIRED
        and obj.excursions.exclude(status=Excursion.Status.CLOSED).exists()
    ):
        raise _error(
            "Close this equipment's excursions before retiring it.", "invalid_transition", "status"
        )
    before = state(obj, EQUIPMENT_FIELDS + ("status",))
    change = StatusChange.objects.create(
        equipment=obj,
        from_status=obj.status,
        to_status=status,
        occurred_at=occurred_at,
        recorded_at=now,
        actor=actor,
        reason=reason,
    )
    obj.status = status
    obj.revision += 1
    try:
        with transaction.atomic():
            obj.save(update_fields=["status", "revision"])
    except IntegrityError as exc:
        raise _error(
            "Another tank or freezer is in use for this storage unit. "
            "Retire it or unlink it first.",
            "unique",
            "status",
        ) from exc
    _audit(
        actor,
        "equipment.status_changed",
        obj,
        before=before,
        after={**state(obj, EQUIPMENT_FIELDS + ("status",)), "status_change": str(change.pk)},
        reason=reason,
    )
    return obj


# ------------------------------------------------------------------ service events


def _service_values(equipment, values):
    kind = _choice(values.get("kind"), ServiceEvent.Kind.choices, "kind", "a kind of event")
    outcome = values.get("outcome")
    if outcome not in ServiceEvent.OUTCOMES[kind]:
        raise _error("This outcome does not apply to that kind of event.", "invalid", "outcome")
    performed_on = _day(values.get("performed_on"), "performed_on", label="the date performed")
    next_due_on = _day(values.get("next_due_on"), "next_due_on", required=False, past=False)
    if next_due_on is not None and kind not in ServiceEvent.DUE_KINDS:
        raise _error("A repair has no next due date.", "invalid", "next_due_on")
    if next_due_on is not None and next_due_on < performed_on:
        raise _error("The next due date is before the date performed.", "invalid", "next_due_on")
    external = _text(values.get("performed_by_external"), "performed_by_external", 160)
    performer = values.get("performed_by")
    if performer not in (None, ""):
        performer = _staff(performer, "performed_by")
    elif not external:
        raise _error(
            "Name who performed it: a staff member or an outside provider.",
            "required",
            "performed_by",
        )
    else:
        performer = None
    return {
        "kind": kind,
        "outcome": outcome,
        "performed_on": performed_on,
        "performed_by": performer,
        "performed_by_external": external,
        "reference": _text(values.get("reference"), "reference", 160),
        "description": _text(values.get("description"), "description"),
        "reference_device": _text(values.get("reference_device"), "reference_device", 160),
        "as_found": _text(values.get("as_found"), "as_found", 200),
        "as_left": _text(values.get("as_left"), "as_left", 200),
        "next_due_on": next_due_on,
    }


@transaction.atomic
def record_service_event(*, actor, equipment_id, attachment=None, **values):
    """Append maintenance, calibration, repair, mapping or qualification performed."""
    require_role(actor, *OPERATE_ROLES)
    unknown = set(values) - set(SERVICE_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {', '.join(sorted(unknown))}.")
    equipment = _lock_equipment(equipment_id)
    _not_retired(equipment)
    cleaned = _service_values(equipment, values)
    attachment_fields(attachment)  # a bad file fails before anything is written
    event = ServiceEvent.objects.create(
        equipment=equipment,
        recorded_by=actor,
        attachment=store_attachment(attachment, actor=actor),
        **cleaned,
    )
    _audit(
        actor,
        f"equipment.{event.kind}_recorded",
        equipment,
        after={
            "service_event": str(event.pk),
            **state(event, SERVICE_FIELDS),
            **_attachment_audit(event),
        },
    )
    return event


@transaction.atomic
def correct_service_event(*, actor, event_id, reason, void=False, attachment=None, **values):
    """Replace an event's values (kind stays) or withdraw it; the original row is kept."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    unknown = set(values) - set(SERVICE_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {', '.join(sorted(unknown))}.")
    equipment, original = _lock_entry(ServiceEvent, event_id, "event")
    _may_correct(actor, original)
    merged = {name: getattr(original, name) for name in SERVICE_FIELDS}
    if not void:
        merged.update(values)
    merged["kind"] = original.kind
    cleaned = _service_values(equipment, merged)
    if (
        not void
        and attachment is None
        and all(cleaned[name] == getattr(original, name) for name in SERVICE_FIELDS)
    ):
        raise _error("Nothing to correct: no value changed.", "invalid_transition")
    # A new file is stored once; otherwise the correction refers to the original's file, never
    # a copy of its bytes.
    attachment_fields(attachment)
    event = ServiceEvent.objects.create(
        equipment=equipment,
        recorded_by=actor,
        corrects=original,
        voided=void,
        correction_reason=reason,
        attachment=store_attachment(attachment, actor=actor) or original.attachment,
        **cleaned,
    )
    _audit(
        actor,
        "equipment.service_withdrawn" if void else "equipment.service_corrected",
        equipment,
        before={"service_event": str(original.pk), **state(original, SERVICE_FIELDS)},
        after={
            "service_event": str(event.pk),
            **state(event, SERVICE_FIELDS),
            **_attachment_audit(event),
            "voided": void,
        },
        reason=reason,
    )
    return event


# ------------------------------------------------------------------ LN2 log


def _ln2_values(equipment, values, *, now, keep_supply=None):
    if not equipment.keeps_ln2_log:
        raise _error("The LN2 log is kept for LN2 tanks and supply dewars.", "invalid", "equipment")
    kind = _choice(values.get("kind"), LN2Entry.Kind.choices, "kind", "level check or fill")
    occurred_at = _moment(
        values.get("occurred_at"), "occurred_at", label="when it was done", now=now
    )
    method = values.get("method") or ""
    if method:
        _choice(method, LN2Entry.Method.choices, "method", "how the level was read")
    level = _decimal(values.get("level"), "level", label="the level", minimum=Decimal(0), digits=8)
    before = _decimal(
        values.get("level_before"),
        "level_before",
        label="the level before",
        minimum=Decimal(0),
        digits=8,
    )
    amount = _decimal(
        values.get("amount_added"), "amount_added", label="the amount added", digits=10
    )
    if amount is not None and amount <= 0:
        raise _error("Enter an amount greater than zero.", "invalid", "amount_added")
    level_unit = values.get("level_unit") or ""
    if (level is not None or before is not None) and not level_unit:
        level_unit = equipment.level_unit
    if level is None and before is None:
        level_unit = ""
    elif not level_unit:
        raise _error("Choose the unit the level is in.", "required", "level_unit")
    else:
        _choice(level_unit, LevelUnit.choices, "level_unit", "a level unit")
    if level_unit == "%":
        for name, number in (("level", level), ("level_before", before)):
            if number is not None and number > 100:
                raise _error("A percentage cannot exceed 100.", "invalid", name)
    amount_unit = values.get("amount_unit") or ""
    if amount is None:
        amount_unit = ""
    else:
        if not amount_unit:
            raise _error("Choose the unit of the amount added.", "required", "amount_unit")
        _choice(amount_unit, AmountUnit.choices, "amount_unit", "a unit")
    supply = _related_equipment(
        values.get("supply"),
        "supply",
        kinds=("supply_dewar",),
        label="a supply dewar",
        keep=keep_supply,
    )
    if supply is not None and supply.pk == equipment.pk:
        raise _error("A vessel cannot be filled from itself.", "invalid", "supply")
    if kind == LN2Entry.Kind.LEVEL:
        if level is None:
            raise _error("Enter the level read.", "required", "level")
        if before is not None or amount is not None or supply is not None:
            raise _error(
                "A level check has one level. Record a fill for amounts and supply.",
                "invalid",
                "level_before",
            )
    else:
        if level is None and amount is None:
            raise _error("Enter the level after the fill or the amount added.", "required", "level")
        if level is not None and before is not None and level < before:
            raise _error(
                "The level after the fill is below the level before it.", "invalid", "level"
            )
    return {
        "kind": kind,
        "occurred_at": occurred_at,
        "method": method,
        "level_before": before,
        "level": level,
        "level_unit": level_unit,
        "amount_added": amount,
        "amount_unit": amount_unit,
        "supply": supply,
        "reference": _text(values.get("reference"), "reference", 160),
        "performed_by_external": _text(
            values.get("performed_by_external"), "performed_by_external", 160
        ),
        "note": _text(values.get("note"), "note"),
    }


@transaction.atomic
def record_ln2(*, actor, equipment_id, **values):
    """Append a level check or a fill to a vessel's LN2 log."""
    require_role(actor, *OPERATE_ROLES)
    unknown = set(values) - set(LN2_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {', '.join(sorted(unknown))}.")
    equipment = _lock_equipment(equipment_id)
    _not_retired(equipment)
    now = tz.now()
    cleaned = _ln2_values(equipment, values, now=now)
    entry = LN2Entry.objects.create(
        equipment=equipment, recorded_by=actor, recorded_at=now, **cleaned
    )
    _audit(
        actor,
        "equipment.level_check_recorded" if entry.kind == "level" else "equipment.fill_recorded",
        equipment,
        after={"ln2_entry": str(entry.pk), **state(entry, LN2_FIELDS)},
    )
    return entry


@transaction.atomic
def correct_ln2(*, actor, entry_id, reason, void=False, **values):
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    unknown = set(values) - set(LN2_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {', '.join(sorted(unknown))}.")
    equipment, original = _lock_entry(LN2Entry, entry_id, "entry")
    _may_correct(actor, original)
    merged = {name: getattr(original, name) for name in LN2_FIELDS}
    if not void:
        merged.update(values)
    merged["kind"] = original.kind
    now = tz.now()
    cleaned = _ln2_values(equipment, merged, now=now, keep_supply=original.supply_id)
    if not void and state(LN2Entry(equipment=equipment, **cleaned), LN2_FIELDS) == state(
        original, LN2_FIELDS
    ):
        raise _error("Nothing to correct: no value changed.", "invalid_transition")
    entry = LN2Entry.objects.create(
        equipment=equipment,
        recorded_by=actor,
        recorded_at=now,
        corrects=original,
        voided=void,
        correction_reason=reason,
        **cleaned,
    )
    _audit(
        actor,
        "equipment.level_log_entry_withdrawn" if void else "equipment.level_log_entry_corrected",
        equipment,
        before={"ln2_entry": str(original.pk), **state(original, LN2_FIELDS)},
        after={"ln2_entry": str(entry.pk), **state(entry, LN2_FIELDS), "voided": void},
        reason=reason,
    )
    return entry


# ------------------------------------------------------------------ temperature readings


def _reading_values(equipment, values, *, now, keep_device=None):
    if not equipment.takes_readings:
        raise _error(
            "Record readings against the tank or freezer the monitor watches.",
            "invalid",
            "equipment",
        )
    measured_at = _moment(
        values.get("measured_at"), "measured_at", label="when it was read", now=now
    )
    value = _temperature(values.get("value"), "value", required=True)
    return {
        "measured_at": measured_at,
        "value": value,
        "probe_position": _text(values.get("probe_position"), "probe_position", 80),
        "device": _related_equipment(
            values.get("device"), "device", kinds=("monitor",), label="a monitor", keep=keep_device
        ),
        "note": _text(values.get("note"), "note"),
    }


def _duplicate_reading(equipment, measured_at, probe_position, *, exclude=None):
    """An effective reading at the same time and probe position (originals are unique)."""
    query = TemperatureReading.objects.filter(
        equipment=equipment,
        measured_at=measured_at,
        probe_position=probe_position,
        corrected_by__isnull=True,
        voided=False,
    )
    if exclude is not None:
        query = query.exclude(pk=exclude.pk)
    return query.exists()


DUPLICATE_READING = "A reading at this time and probe position is already recorded."


@transaction.atomic
def record_reading(*, actor, equipment_id, **values):
    """Append one temperature reading entered by hand; the current limits are kept with it."""
    require_role(actor, *OPERATE_ROLES)
    unknown = set(values) - set(READING_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {', '.join(sorted(unknown))}.")
    equipment = _lock_equipment(equipment_id)
    _not_retired(equipment)
    now = tz.now()
    cleaned = _reading_values(equipment, values, now=now)
    if _duplicate_reading(equipment, cleaned["measured_at"], cleaned["probe_position"]):
        raise _error(DUPLICATE_READING, "unique", "measured_at")
    # The equipment row lock serializes reading writes, so the duplicate check above holds.
    reading = TemperatureReading.objects.create(
        equipment=equipment,
        recorded_by=actor,
        recorded_at=now,
        lower_limit=equipment.lower_limit,
        upper_limit=equipment.upper_limit,
        **cleaned,
    )
    _audit(
        actor,
        "equipment.reading_recorded",
        equipment,
        after={"reading": str(reading.pk), **state(reading, READING_FIELDS)},
    )
    return reading


@transaction.atomic
def correct_reading(*, actor, reading_id, reason, void=False, **values):
    """Replace a reading's values or withdraw it. The limits recorded with it are kept."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    unknown = set(values) - set(READING_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {', '.join(sorted(unknown))}.")
    equipment, original = _lock_entry(TemperatureReading, reading_id, "reading")
    _may_correct(actor, original)
    merged = {name: getattr(original, name) for name in READING_FIELDS}
    if not void:
        merged.update(values)
    now = tz.now()
    cleaned = _reading_values(equipment, merged, now=now, keep_device=original.device_id)
    if not void:
        if all(cleaned[name] == getattr(original, name) for name in READING_FIELDS):
            raise _error("Nothing to correct: no value changed.", "invalid_transition")
        if _duplicate_reading(
            equipment, cleaned["measured_at"], cleaned["probe_position"], exclude=original
        ):
            raise _error(DUPLICATE_READING, "unique", "measured_at")
    reading = TemperatureReading.objects.create(
        equipment=equipment,
        recorded_by=actor,
        recorded_at=now,
        source=original.source,
        batch=original.batch,
        lower_limit=original.lower_limit,
        upper_limit=original.upper_limit,
        corrects=original,
        voided=void,
        correction_reason=reason,
        **cleaned,
    )
    _audit(
        actor,
        "equipment.reading_withdrawn" if void else "equipment.reading_corrected",
        equipment,
        before={"reading": str(original.pk), **state(original, READING_FIELDS)},
        after={"reading": str(reading.pk), **state(reading, READING_FIELDS), "voided": void},
        reason=reason,
    )
    return reading


# ------------------------------------------------------------------ readings CSV import

IMPORT_COLUMNS = ("equipment", "measured_at", "temperature_c", "probe_position", "device", "note")
REQUIRED_COLUMNS = ("measured_at", "temperature_c")


def _parse_time(text):
    value = datetime.fromisoformat(text.strip())
    if tz.is_naive(value):
        value = tz.make_aware(value, tz.get_default_timezone())
    return value


def _read_csv(file):
    """(filename, sha256, rows as dicts, header) or ValidationError on `file`."""
    if file is None:
        raise _error("Choose a CSV file.", "required", "file")
    name = (getattr(file, "name", "") or "")[:160]
    if getattr(file, "size", 0) and file.size > IMPORT_MAX_BYTES:
        raise _error("The file is larger than 5 MiB.", "invalid", "file")
    data = file.read(IMPORT_MAX_BYTES + 1)
    if len(data) > IMPORT_MAX_BYTES:
        raise _error("The file is larger than 5 MiB.", "invalid", "file")
    if not data:
        raise _error("The file is empty.", "invalid", "file")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _error("Save the CSV as UTF-8.", "invalid", "file") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    return name, hashlib.sha256(data).hexdigest(), reader, header


@transaction.atomic
def validate_reading_import(*, actor, file, equipment_id=None):
    """Check every row of a readings CSV and keep the result; nothing is recorded yet.

    Columns: measured_at (ISO 8601; without an offset it is read in the deployment's time zone),
    temperature_c, and optionally equipment (asset ID; else the equipment chosen on the form),
    probe_position, device (a monitor's asset ID), note.
    """
    require_role(actor, *OPERATE_ROLES)
    chosen = None
    if equipment_id not in (None, ""):
        chosen = Equipment.objects.filter(pk=getattr(equipment_id, "pk", equipment_id)).first()
        if chosen is None:
            raise _error("Choose equipment from the register.", "invalid", "equipment")
    filename, sha256, reader, header = _read_csv(file)
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise _error(f"The file needs the column(s): {', '.join(missing)}.", "invalid", "file")
    if "equipment" not in header and chosen is None:
        raise _error(
            "Choose the equipment, or add an equipment column with asset IDs.",
            "required",
            "equipment",
        )
    unknown = [c for c in header if c and c not in IMPORT_COLUMNS]
    warnings = []
    if unknown:
        warnings.append(f"Ignored column(s): {', '.join(unknown)}.")
    by_code = {e.code: e for e in Equipment.objects.all()}
    now = tz.now()
    rows, errors, seen = [], [], set()
    outside = 0
    for number, raw in enumerate(reader, start=2):
        if number - 1 > IMPORT_MAX_ROWS:
            raise _error(f"The file has more than {IMPORT_MAX_ROWS} rows.", "invalid", "file")
        raw = {k: (v or "").strip() for k, v in raw.items() if k}
        if not any(raw.values()):
            continue

        def fail(field, message, number=number):
            errors.append({"row": number, "field": field, "message": message})

        code = raw.get("equipment", "").upper()
        equipment = by_code.get(code) if code else chosen
        if equipment is None:
            fail(
                "equipment",
                f"No equipment with asset ID {code!r}." if code else "Name the equipment.",
            )
            continue
        if not equipment.takes_readings:
            fail(
                "equipment", f"{equipment.code} is a monitor; name the tank or freezer it watches."
            )
            continue
        if equipment.is_retired:
            fail("equipment", f"{equipment.code} is retired.")
            continue
        try:
            measured_at = _parse_time(raw.get("measured_at", ""))
        except ValueError:
            fail("measured_at", "Use an ISO 8601 date and time, e.g. 2026-11-12T08:30.")
            continue
        if measured_at > now:
            fail("measured_at", "The time is in the future.")
            continue
        try:
            value = _temperature(raw.get("temperature_c"), "temperature_c", required=True)
        except ValidationError as exc:
            fail("temperature_c", exc.message_dict["temperature_c"][0])
            continue
        device = None
        if raw.get("device"):
            device = by_code.get(raw["device"].upper())
            if device is None or device.kind != Equipment.Kind.MONITOR or device.is_retired:
                fail("device", f"No monitor with asset ID {raw['device']!r}.")
                continue
        probe = raw.get("probe_position", "")[:80]
        key = (equipment.pk, measured_at, probe)
        if key in seen:
            fail("measured_at", "The same reading appears twice in this file.")
            continue
        seen.add(key)
        if _duplicate_reading(equipment, measured_at, probe):
            fail("measured_at", DUPLICATE_READING)
            continue
        if (equipment.upper_limit is not None and value > equipment.upper_limit) or (
            equipment.lower_limit is not None and value < equipment.lower_limit
        ):
            outside += 1
        rows.append(
            {
                "row": number,
                "equipment": str(equipment.pk),
                "equipment_code": equipment.code,
                "measured_at": measured_at.isoformat(),
                "value": str(value),
                "probe_position": probe,
                "device": str(device.pk) if device else "",
                "note": raw.get("note", "")[:TEXT_LIMIT],
            }
        )
    if outside:
        warnings.append(f"{outside} reading(s) are outside the limits set for their equipment.")
    if ReadingImport.objects.filter(sha256=sha256, commit__isnull=False).exists():
        warnings.append("A file with exactly this content was imported before.")
    batch = ReadingImport.objects.create(
        actor=actor,
        recorded_at=now,
        filename=filename,
        sha256=sha256,
        equipment=chosen,
        row_count=len(rows),
        normalized_rows=rows if not errors else [],
        errors=errors,
        warnings=warnings,
        expires_at=now + IMPORT_LIFETIME,
    )
    _audit(
        actor,
        "equipment.import_validated",
        batch,
        after={
            "filename": filename,
            "sha256": sha256,
            "rows": len(rows),
            "errors": len(errors),
        },
    )
    return batch


@transaction.atomic
def commit_reading_import(*, actor, batch_id, reason):
    """Record every reading of a valid batch in one transaction, or none."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    try:
        batch = ReadingImport.objects.select_for_update().get(pk=batch_id)
    except ReadingImport.DoesNotExist, ValidationError, ValueError:
        raise _error("This import does not exist.") from None
    if ReadingImportCommit.objects.filter(batch=batch).exists():
        raise _error("This import was already recorded.", "invalid_transition")
    if not batch.is_valid:
        raise _error("Fix the errors in the file and upload it again.", "invalid_import")
    now = tz.now()
    if batch.expires_at < now:
        raise _error("This check has expired. Upload the file again.", "invalid_import")
    ids = sorted({row["equipment"] for row in batch.normalized_rows})
    locked = {
        str(e.pk): e
        for e in Equipment.objects.select_for_update().filter(pk__in=ids).order_by("pk")
    }
    readings = []
    for row in batch.normalized_rows:
        equipment = locked.get(row["equipment"])
        if equipment is None or equipment.is_retired:
            raise _error(
                f"Row {row['row']}: the equipment is no longer in service.", "invalid_import"
            )
        measured_at = datetime.fromisoformat(row["measured_at"])
        if _duplicate_reading(equipment, measured_at, row["probe_position"]):
            raise _error(f"Row {row['row']}: {DUPLICATE_READING}", "invalid_import")
        readings.append(
            TemperatureReading(
                equipment=equipment,
                measured_at=measured_at,
                recorded_at=now,
                recorded_by=actor,
                value=Decimal(row["value"]),
                probe_position=row["probe_position"],
                device_id=row["device"] or None,
                note=row["note"],
                source=TemperatureReading.Source.IMPORT,
                batch=batch,
                lower_limit=equipment.lower_limit,
                upper_limit=equipment.upper_limit,
            )
        )
    TemperatureReading.objects.bulk_create(readings, batch_size=1000)
    commit = ReadingImportCommit.objects.create(
        batch=batch, actor=actor, recorded_at=now, reason=reason, reading_count=len(readings)
    )
    _audit(
        actor,
        "equipment.import_committed",
        batch,
        after={"commit": str(commit.pk), "readings": len(readings), "sha256": batch.sha256},
        reason=reason,
    )
    return commit


# ------------------------------------------------------------------ excursions


def _lock_excursion(excursion_id, expected_revision=None):
    try:
        equipment_id = Excursion.objects.values_list("equipment_id", flat=True).get(pk=excursion_id)
    except Excursion.DoesNotExist, ValidationError, ValueError:
        raise _error("This excursion does not exist.") from None
    _lock_equipment(equipment_id)
    excursion = Excursion.objects.select_for_update().get(pk=excursion_id)
    if expected_revision is not False:
        _check_revision(excursion, expected_revision, "excursion")
    return excursion


def _excursion_values(values, *, now):
    started = _moment(values.get("started_at"), "started_at", label="when it started", now=now)
    ended = _moment(
        values.get("ended_at"), "ended_at", required=False, label="when it ended", now=now
    )
    if ended is not None and ended < started:
        raise _error("The end is before the start.", "invalid_time", "ended_at")
    extreme = _temperature(values.get("extreme_value"), "extreme_value", label="the extreme")
    extreme_at = _moment(
        values.get("extreme_at"), "extreme_at", required=False, label="when it was reached", now=now
    )
    if extreme_at is not None and (extreme_at < started or (ended and extreme_at > ended)):
        raise _error(
            "The time of the extreme must fall within the excursion.", "invalid_time", "extreme_at"
        )
    return {
        "started_at": started,
        "ended_at": ended,
        "extreme_value": extreme,
        "extreme_at": extreme_at,
        "detection": _choice(
            values.get("detection"), Excursion.Detection.choices, "detection", "how it was detected"
        ),
        "cause_category": _choice(
            values.get("cause_category") or Excursion.Cause.UNKNOWN,
            Excursion.Cause.choices,
            "cause_category",
            "a cause",
        ),
        "cause": _text(values.get("cause"), "cause"),
        "action": _text(values.get("action"), "action"),
        "impact": _text(values.get("impact"), "impact"),
    }


def _timeline(excursion, actor, kind, *, note="", changes=None, attachment=None, at=None):
    return ExcursionEvent.objects.create(
        excursion=excursion,
        kind=kind,
        note=note,
        changes=_json(changes or {}),
        actor=actor,
        occurred_at=at or tz.now(),
        attachment=store_attachment(attachment, actor=actor),
    )


@transaction.atomic
def open_excursion(*, actor, equipment_id, note="", attachment=None, **values):
    """Record that a unit's temperature went outside its limits. The limits in force and the
    storage unit the equipment holds are kept with the record."""
    require_role(actor, *OPERATE_ROLES)
    unknown = set(values) - set(EXCURSION_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {', '.join(sorted(unknown))}.")
    equipment = _lock_equipment(equipment_id)
    _not_retired(equipment)
    if not equipment.takes_readings:
        raise _error(
            "Record the excursion against the tank or freezer the monitor watches.",
            "invalid",
            "equipment",
        )
    now = tz.now()
    cleaned = _excursion_values(values, now=now)
    excursion = Excursion.objects.create(
        equipment=equipment,
        storage_unit=equipment.storage_unit,
        lower_limit=equipment.lower_limit,
        upper_limit=equipment.upper_limit,
        opened_by=actor,
        opened_at=now,
        **cleaned,
    )
    _timeline(
        excursion,
        actor,
        ExcursionEvent.Kind.OPENED,
        note=_text(note, "note"),
        attachment=attachment,
        at=now,
    )
    _audit(actor, "excursion.opened", excursion, after=state(excursion, EXCURSION_SNAPSHOT))
    return excursion


@transaction.atomic
def update_excursion(*, actor, excursion_id, expected_revision, **values):
    """Edit an open excursion's details; the timeline keeps each prior value."""
    require_role(actor, *OPERATE_ROLES)
    unknown = set(values) - set(EXCURSION_FIELDS)
    if unknown:
        raise _error(f"Unknown field: {', '.join(sorted(unknown))}.")
    excursion = _lock_excursion(excursion_id, expected_revision)
    if excursion.status != Excursion.Status.OPEN:
        raise _error(
            "Only an open excursion can be edited. Ask a manager to return it first.",
            "invalid_transition",
        )
    merged = {name: getattr(excursion, name) for name in EXCURSION_FIELDS}
    merged.update(values)
    cleaned = _excursion_values(merged, now=tz.now())
    before = state(excursion, EXCURSION_FIELDS)
    for key, value in cleaned.items():
        setattr(excursion, key, value)
    after = state(excursion, EXCURSION_FIELDS)
    changes = {k: [before[k], after[k]] for k in EXCURSION_FIELDS if before[k] != after[k]}
    if not changes:
        raise _error("Nothing to save: no value changed.", "invalid_transition")
    excursion.revision += 1
    excursion.save()
    _timeline(excursion, actor, ExcursionEvent.Kind.UPDATED, changes=changes)
    _audit(actor, "excursion.updated", excursion, before=before, after=after)
    return excursion


@transaction.atomic
def submit_excursion(*, actor, excursion_id, expected_revision, note=""):
    """Send an ended, explained excursion for review by a manager."""
    require_role(actor, *OPERATE_ROLES)
    excursion = _lock_excursion(excursion_id, expected_revision)
    if excursion.status != Excursion.Status.OPEN:
        raise _error("Only an open excursion can be submitted.", "invalid_transition")
    missing = {}
    if excursion.ended_at is None:
        missing["ended_at"] = "Enter when it ended before submitting."
    for name, label in (
        ("cause", "the cause"),
        ("action", "the action taken"),
        ("impact", "the impact assessment"),
    ):
        if not getattr(excursion, name).strip():
            missing[name] = f"Describe {label} before submitting."
    if missing:
        raise ValidationError({k: ValidationError(v, code="required") for k, v in missing.items()})
    before = state(excursion, EXCURSION_SNAPSHOT)
    now = tz.now()
    excursion.status = Excursion.Status.REVIEW
    excursion.submitted_by, excursion.submitted_at = actor, now
    excursion.revision += 1
    excursion.save()
    _timeline(excursion, actor, ExcursionEvent.Kind.SUBMITTED, note=_text(note, "note"), at=now)
    _audit(
        actor,
        "excursion.submitted",
        excursion,
        before=before,
        after=state(excursion, EXCURSION_SNAPSHOT),
    )
    return excursion


@transaction.atomic
def return_excursion(*, actor, excursion_id, expected_revision, note):
    """Send an excursion in review back for more work, saying what is missing."""
    require_role(actor, *MANAGE_ROLES)
    note = need_reason(note, "note")
    excursion = _lock_excursion(excursion_id, expected_revision)
    if excursion.status != Excursion.Status.REVIEW:
        raise _error("Only an excursion awaiting review can be returned.", "invalid_transition")
    before = state(excursion, EXCURSION_SNAPSHOT)
    excursion.status = Excursion.Status.OPEN
    excursion.submitted_by = excursion.submitted_at = None
    excursion.revision += 1
    excursion.save()
    _timeline(excursion, actor, ExcursionEvent.Kind.RETURNED, note=note)
    _audit(
        actor,
        "excursion.returned",
        excursion,
        before=before,
        after=state(excursion, EXCURSION_SNAPSHOT),
        reason=note,
    )
    return excursion


@transaction.atomic
def close_excursion(*, actor, excursion_id, expected_revision, note):
    """Review and close: a manager or administrator who did not submit it. The specimens that
    were in the unit during the excursion are captured with the closing entry."""
    require_role(actor, *MANAGE_ROLES)
    note = need_reason(note, "note")
    excursion = _lock_excursion(excursion_id, expected_revision)
    if excursion.status != Excursion.Status.REVIEW:
        raise _error("Only an excursion awaiting review can be closed.", "invalid_transition")
    if excursion.submitted_by_id == actor.pk:
        raise _error(
            "You submitted this excursion. Another manager or administrator reviews and closes it.",
            "self_review",
        )
    before = state(excursion, EXCURSION_SNAPSHOT)
    now = tz.now()
    rows = (
        affected_specimens(
            actor=actor,
            unit_id=excursion.storage_unit_id,
            start=excursion.started_at,
            end=excursion.ended_at,
        )
        if excursion.storage_unit_id
        else []
    )
    event = _timeline(
        excursion,
        actor,
        ExcursionEvent.Kind.CLOSED,
        note=note,
        changes={"affected_specimens": len(rows)},
        at=now,
    )
    AffectedSpecimen.objects.bulk_create(
        [
            AffectedSpecimen(
                excursion=excursion,
                capture=event,
                specimen_id=row.specimen_id,
                present_from=row.present_from,
                present_until=row.present_until,
                location=row.location[:300],
            )
            for row in rows
        ],
        batch_size=1000,
    )
    excursion.status = Excursion.Status.CLOSED
    excursion.closed_by, excursion.closed_at, excursion.closure_note = actor, now, note
    excursion.revision += 1
    excursion.save()
    _audit(
        actor,
        "excursion.closed",
        excursion,
        before=before,
        after={**state(excursion, EXCURSION_SNAPSHOT), "affected_specimens": len(rows)},
        reason=note,
    )
    return excursion


@transaction.atomic
def reopen_excursion(*, actor, excursion_id, expected_revision, reason):
    require_role(actor, *MANAGE_ROLES)
    reason = need_reason(reason)
    excursion = _lock_excursion(excursion_id, expected_revision)
    if excursion.status != Excursion.Status.CLOSED:
        raise _error("Only a closed excursion can be reopened.", "invalid_transition")
    before = state(excursion, EXCURSION_SNAPSHOT)
    excursion.status = Excursion.Status.OPEN
    excursion.submitted_by = excursion.submitted_at = None
    excursion.closed_by = excursion.closed_at = None
    excursion.closure_note = ""
    excursion.revision += 1
    excursion.save()
    _timeline(excursion, actor, ExcursionEvent.Kind.REOPENED, note=reason)
    _audit(
        actor,
        "excursion.reopened",
        excursion,
        before=before,
        after=state(excursion, EXCURSION_SNAPSHOT),
        reason=reason,
    )
    return excursion


@transaction.atomic
def add_excursion_note(*, actor, excursion_id, note="", attachment=None):
    """Any operator may add a note or a file to an excursion in any state."""
    require_role(actor, *OPERATE_ROLES)
    note = _text(note, "note")
    files = attachment_fields(attachment)
    if not note and not files:
        raise _error("Enter a note or attach a file.", "required", "note")
    excursion = _lock_excursion(excursion_id, expected_revision=False)
    event = _timeline(excursion, actor, ExcursionEvent.Kind.NOTE, note=note, attachment=attachment)
    _audit(
        actor,
        "excursion.note_added",
        excursion,
        after={"event": str(event.pk), **_attachment_audit(event)},
        reason=note,
    )
    return event


@storage.operation()
def place_excursion_holds(
    *, actor, excursion_id, expected_revision, specimen_ids, specimen_revisions, scope, reason
):
    """Place explicitly selected ordinary manual holds, keeping excursion provenance.

    Lock order is storage gate, equipment, excursion, then specimens by primary key. Membership
    and reviewed revisions are checked again under those locks. Closing/reopening never owns or
    releases these holds; managers release each through the ordinary specimen workflow.
    """
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    if type(expected_revision) is not int or expected_revision < 1:
        raise _error("Reload the excursion and review its current state.", "stale_revision")
    if scope not in Hold.Scope.values:
        raise _error("Choose a disposal hold, a use hold or an all-use hold.", "invalid", "scope")
    try:
        selected = [uuid.UUID(str(value)) for value in specimen_ids]
        revisions = {uuid.UUID(str(key)): value for key, value in specimen_revisions.items()}
    except AttributeError, TypeError, ValueError:
        raise _error(
            "Reload the candidates and review your selection.", "invalid", "specimen_ids"
        ) from None
    if not selected or len(selected) > EXCURSION_HOLD_LIMIT or len(set(selected)) != len(selected):
        raise _error(
            f"Select between 1 and {EXCURSION_HOLD_LIMIT} distinct specimens.",
            "invalid",
            "specimen_ids",
        )
    if any(type(revisions.get(key)) is not int or revisions[key] < 1 for key in selected):
        raise _error("Reload the candidates and review their current state.", "stale_revision")
    storage.lock_gate()
    excursion = _lock_excursion(excursion_id, expected_revision)
    # Local import avoids the query module depending on service implementation details.
    from .queries import excursion_specimens

    affected, _ = excursion_specimens(excursion, actor=actor)
    if not set(selected).issubset({row.specimen_id for row in affected}):
        raise _error(
            "A selected specimen is not affected by this excursion. Reload the candidates and "
            "review your selection.",
            "invalid",
            "specimen_ids",
        )
    specimens = list(Specimen.objects.select_for_update().filter(pk__in=selected).order_by("pk"))
    if len(specimens) != len(selected):
        raise _error("A selected specimen no longer exists. Reload the candidates.", "invalid")
    for specimen in specimens:
        if specimen.revision != revisions[specimen.pk]:
            raise _error(
                f"Specimen {specimen.accession} changed since you reviewed it. Reload the "
                "candidates, check its current state and try again.",
                "stale_revision",
            )
    hold_reason = f"Excursion {excursion.number}: {reason}"
    if len(hold_reason) > specimen_services.TEXT_LIMIT:
        limit = specimen_services.TEXT_LIMIT - len(hold_reason) + len(reason)
        raise _error(
            f"Use at most {limit} characters so the excursion number fits in the hold reason.",
            "invalid",
            "reason",
        )
    now = tz.now()
    holds = []
    provenance = []
    for specimen in specimens:
        hold = specimen_services.place_hold(
            actor=actor,
            specimen_id=specimen.pk,
            expected_revision=revisions[specimen.pk],
            scope=scope,
            occurred_at=now,
            reason=hold_reason,
        )
        holds.append(hold)
        provenance.append(
            {
                "hold_id": str(hold.pk),
                "specimen_id": str(specimen.pk),
                "accession": specimen.accession,
                "scope": scope,
            }
        )
    before_revision = excursion.revision
    excursion.revision += 1
    excursion.save(update_fields=["revision"])
    event = _timeline(
        excursion,
        actor,
        ExcursionEvent.Kind.NOTE,
        note=hold_reason,
        changes={"manual_holds": [[], provenance]},
        at=now,
    )
    _audit(
        actor,
        "excursion.holds_placed",
        excursion,
        before={"revision": before_revision},
        after={"revision": excursion.revision, "event": str(event.pk), "manual_holds": provenance},
        reason=hold_reason,
    )
    return holds
