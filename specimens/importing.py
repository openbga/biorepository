"""Bulk registration from CSV: validate (no specimens, IDs or claims), then commit all or none.

`validate_import` parses outside any lock and stores an immutable ImportBatch holding the
normalized rows, the number of errors and the first MAX_ERRORS of them (a file can contain at most
MAX_ROWS nonblank records, whatever their width, so the work and the stored result stay
bounded). `commit_import` takes the StorageGate, revalidates every normalized row against the
current catalogues and positions, and registers (and places) all rows in one transaction. A
batch commits at most once; replaying the commit returns it.
"""

import csv
import hashlib
import io
import re
from datetime import datetime, timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone as tz
from django.utils.dateparse import parse_datetime

from accounts.roles import has_role, require_role
from core.audit import record_event
from storage import services as storage
from storage.models import Position

from .models import (
    Collection,
    ImportBatch,
    ImportCommit,
    Participant,
    QuantityUnit,
    SpecimenContainerType,
    SpecimenType,
)
from .services import (
    MANAGE_ROLES,
    MAX_BASELINE_THAWS,
    OPERATE_ROLES,
    Registration,
    _error,
    need_reason,
    parse_quantity,
    register_in_operation,
)

COLUMNS = (
    "row_key",
    "specimen_type",
    "container_type",
    "quantity",
    "unit",
    "collected_at",
    "processing_started_at",
    "processing_completed_at",
    "participant_code",
    "collection_code",
    "box_code",
    "position_label",
    "baseline_thaws",
)
REQUIRED = ("row_key", "specimen_type", "container_type", "quantity", "unit", "collected_at")
MAX_ROWS = 1000
MAX_ERRORS = 200  # errors kept on a batch; error_count holds how many were found
SHOWN = 40  # characters of a supplied value repeated in an error
MAX_BYTES = 2 * 1024 * 1024
EXPIRY = timedelta(hours=24)
SCHEMA_VERSION = 1


def template_csv():
    """Header plus one obviously synthetic example row (for the upload page download)."""
    return (
        ",".join(COLUMNS)
        + "\n"
        + "ROW-1,PLASMA,CRYOVIAL-2ML,1.000000,mL,2026-11-01T09:30:00-05:00,"
        + "2026-11-01T10:00:00-05:00,2026-11-01T10:40:00-05:00,P-000123,DEMO,,,0\n"
    )


def _time(value, column, now):
    """ISO 8601; a time without an offset is read in the deployment time zone (TIME_ZONE)."""
    parsed = parse_datetime(value.replace(" ", "T", 1)) if value else None
    if parsed is None:
        raise ValueError("Use ISO 8601 date and time, e.g. 2026-11-01T09:30:00-05:00.")
    if tz.is_naive(parsed):
        parsed = tz.make_aware(parsed)
    if parsed > now:
        raise ValueError("The time cannot be in the future.")
    return parsed


def _shown(value):
    """A supplied value as repeated in an error message: never more than SHOWN characters."""
    return value if len(value) <= SHOWN else value[: SHOWN - 1] + "…"


def _lookup(model, code, *, normalize=True):
    code = code.strip()
    key = code.upper() if normalize else code
    obj = model.objects.filter(code=key).first()
    if obj is None:
        raise ValueError(f"No {model._meta.verbose_name} has code {_shown(code)}.")
    if not obj.is_active:
        raise ValueError(f"{obj.code} is inactive.")
    return obj


def _validate_row(number, row, now, seen_keys, seen_positions):
    """Return (normalized dict, [errors]) for one CSV row."""
    errors = []
    out = {"line": number}

    def fail(column, message):
        errors.append(
            {
                "line": number,
                "row_key": _shown(row.get("row_key", "")),
                "column": column,
                "message": message,
            }
        )

    for column in REQUIRED:
        if not row.get(column):
            fail(column, "Required.")
    key = row.get("row_key", "")
    if key:
        if len(key) > 40:
            fail("row_key", "Use at most 40 characters.")
        elif key in seen_keys:
            fail("row_key", f"Duplicate row key (also on line {seen_keys[key]}).")
        else:
            seen_keys[key] = number
    out["row_key"] = _shown(key)
    for column, model in (
        ("specimen_type", SpecimenType),
        ("container_type", SpecimenContainerType),
    ):
        if row.get(column):
            try:
                obj = _lookup(model, row[column])
                out[column] = {"id": str(obj.pk), "code": obj.code}
            except ValueError as exc:
                fail(column, str(exc))
    unit = None
    if row.get("unit"):
        try:
            unit = _lookup(QuantityUnit, row["unit"], normalize=False)
            out["unit"] = {"id": str(unit.pk), "code": unit.code}
        except ValueError as exc:
            fail("unit", str(exc))
    if row.get("quantity") and unit is not None:
        try:
            quantity = parse_quantity(row["quantity"], unit)
            out["quantity"] = str(quantity)
            out["supplied_text"] = row["quantity"][:80]
        except ValidationError as exc:
            fail("quantity", " ".join(exc.messages))
    times = {}
    for column in ("collected_at", "processing_started_at", "processing_completed_at"):
        if row.get(column):
            try:
                times[column] = _time(row[column], column, now)
                out[column] = times[column].isoformat()
            except ValueError as exc:
                fail(column, str(exc))
    started, completed = times.get("processing_started_at"), times.get("processing_completed_at")
    collected = times.get("collected_at")
    if completed and not started and "processing_started_at" not in {e["column"] for e in errors}:
        fail("processing_started_at", "Give the start when you give the completion.")
    if collected and started and started < collected:
        fail("processing_started_at", "Processing cannot start before collection.")
    if started and completed and completed < started:
        fail("processing_completed_at", "Processing cannot finish before it started.")
    for column, model in (("participant_code", Participant), ("collection_code", Collection)):
        if row.get(column):
            try:
                obj = _lookup(model, row[column])
                out[column] = {"id": str(obj.pk), "code": obj.code}
            except ValueError as exc:
                fail(column, str(exc))
    box_code, label = row.get("box_code", ""), row.get("position_label", "")
    if bool(box_code) != bool(label):
        fail(
            "box_code" if not box_code else "position_label",
            "Give both the box code and the position label, or neither.",
        )
    elif box_code:
        position = (
            Position.objects.filter(box__code=box_code.strip().upper(), label=label.strip())
            .select_related("box")
            .first()
        )
        if position is None:
            fail("position_label", f"Box {_shown(box_code)} has no position {_shown(label)}.")
        elif not position.box.is_active:
            fail("box_code", f"Box {position.box.code} is retired.")
        elif position.blocked:
            fail("position_label", f"Position {position.label} is blocked: {position.block_reason}")
        elif hasattr(position, "occupancy"):
            fail("position_label", f"Position {position.box.code} {position.label} is occupied.")
        elif position.pk in seen_positions:
            fail("position_label", f"Also used on line {seen_positions[position.pk]} of this file.")
        else:
            seen_positions[position.pk] = number
            out["position"] = {
                "id": str(position.pk),
                "box": position.box.code,
                "label": position.label,
            }
    if row.get("baseline_thaws"):
        value = row["baseline_thaws"].strip()
        if value.lower() == "unknown":
            out["baseline_thaws"] = None
        elif not re.fullmatch(r"[0-9]{1,4}", value) or int(value) > MAX_BASELINE_THAWS:
            fail(
                "baseline_thaws",
                f"Give a whole number from 0 to {MAX_BASELINE_THAWS}, or leave blank if unknown.",
            )
        else:
            out["baseline_thaws"] = int(value)
    else:
        out["baseline_thaws"] = None
    return out, errors


def parse(csv_bytes):
    """(rows as dicts with their line numbers, file-level errors)."""
    if len(csv_bytes) > MAX_BYTES:
        return [], ["The file is larger than 2 MiB. Split it into smaller files."]
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], ["The file is not UTF-8 text. Save it as CSV UTF-8 and upload it again."]
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        return [], ["The file is empty."]
    except csv.Error as exc:
        return [], [f"The file is not valid CSV: {exc}"]
    header = [h.strip() for h in header]
    problems = []
    unknown = [h for h in header if h not in COLUMNS]
    missing = [c for c in COLUMNS if c not in header]
    if unknown:
        problems.append(
            f"Unknown columns: {', '.join(_shown(h) for h in unknown[:10])}"
            f"{' and more' if len(unknown) > 10 else ''}. Only the template's columns are accepted."
        )
    if missing:
        problems.append(f"Missing columns: {', '.join(missing)}.")
    if len(set(header)) != len(header):
        problems.append("A column appears twice in the header.")
    if problems:
        return [], problems
    rows, records = [], 0
    try:
        for line, cells in enumerate(reader, start=2):
            if not any(cell.strip() for cell in cells):
                continue
            # Every nonblank record counts, valid or not, so malformed lines cannot pass the limit.
            records += 1
            if records > MAX_ROWS:
                return [], [f"The file has more than {MAX_ROWS} rows. Split it."]
            if len(cells) != len(header):
                problems.append(
                    f"Line {line} has {len(cells)} cells; the header has {len(header)}."
                )
                continue
            rows.append((line, {h: c.strip() for h, c in zip(header, cells, strict=True)}))
    except csv.Error as exc:
        problems.append(f"The file is not valid CSV: {exc}")
    if not rows and not problems:
        problems.append("The file has a header but no rows.")
    return rows, problems


def validate_import(*, actor, csv_bytes, filename=""):
    """Validate an upload and keep the normalized result; nothing is registered."""
    require_role(actor, *OPERATE_ROLES)
    if not isinstance(csv_bytes, bytes):
        raise _error("Upload a CSV file.", "invalid_import", "file")
    digest = hashlib.sha256(csv_bytes).hexdigest()
    rows, problems = parse(csv_bytes)
    now = tz.now()
    normalized, errors = (
        [],
        [{"line": None, "row_key": "", "column": "", "message": p} for p in problems],
    )
    seen_keys, seen_positions = {}, {}
    for line, row in rows:
        out, row_errors = _validate_row(line, row, now, seen_keys, seen_positions)
        normalized.append(out)
        errors.extend(row_errors)
    warnings = []
    prior = ImportBatch.objects.filter(sha256=digest).order_by("-recorded_at").first()
    if prior is not None:
        when = tz.localtime(prior.recorded_at).strftime("%Y-%m-%d %H:%M")
        committed = " and committed" if hasattr(prior, "commit") else ""
        warnings.append(
            f"The same file was validated before, on {when}{committed}. Commit again only "
            "if these are separate specimens."
        )
    with transaction.atomic():
        batch = ImportBatch.objects.create(
            actor=actor,
            recorded_at=now,
            filename=(filename or "")[:160],
            sha256=digest,
            schema_version=SCHEMA_VERSION,
            row_count=len(normalized),
            normalized_rows=normalized,
            errors=errors[:MAX_ERRORS],
            error_count=len(errors),
            warnings=warnings,
            expires_at=now + EXPIRY,
        )
        record_event(
            actor=actor,
            action="specimens.import_validated",
            obj=batch,
            after={"rows": len(normalized), "errors": len(errors), "sha256": digest},
            reason="Import file validated",
        )
    return batch


def _registration(row, actor):
    return Registration(
        specimen_type_id=row["specimen_type"]["id"],
        container_type_id=row["container_type"]["id"],
        quantity=Decimal(row["quantity"]),
        supplied_text=row.get("supplied_text", row["quantity"]),
        unit_id=row["unit"]["id"],
        collected_at=datetime.fromisoformat(row["collected_at"]),
        processing_started_at=datetime.fromisoformat(row["processing_started_at"])
        if row.get("processing_started_at")
        else None,
        processing_completed_at=datetime.fromisoformat(row["processing_completed_at"])
        if row.get("processing_completed_at")
        else None,
        participant_id=(row.get("participant_code") or {}).get("id"),
        collection_id=(row.get("collection_code") or {}).get("id"),
        baseline_thaws=row.get("baseline_thaws"),
        custodian_id=actor.pk,
        position_id=(row.get("position") or {}).get("id"),
    )


@storage.operation()
def commit_import(*, actor, batch_id, expected_sha256, reason):
    """Register every row of a valid, unexpired batch, or nothing. Idempotent per batch."""
    require_role(actor, *OPERATE_ROLES)
    reason = need_reason(reason)
    storage.lock_gate()
    batch = ImportBatch.objects.select_for_update().get(pk=batch_id)
    if batch.actor_id != actor.pk and not has_role(actor, *MANAGE_ROLES):
        raise PermissionDenied("Only the person who validated the file or a manager commits it.")
    existing = ImportCommit.objects.filter(batch=batch).first()
    if existing is not None:
        return existing
    if expected_sha256 != batch.sha256:
        raise _error("This is not the file that was validated.", "invalid_import")
    if batch.total_errors or not batch.row_count:
        raise _error("Correct the errors and validate the file again.", "invalid_import")
    now = tz.now()
    if batch.expires_at < now:
        raise _error(
            "The validation expired after 24 hours. Upload the file again.", "invalid_import"
        )
    created, problems = [], []
    for row in batch.normalized_rows:
        try:
            with transaction.atomic():
                specimen = register_in_operation(
                    actor=actor,
                    data=_registration(row, actor),
                    occurred_at=now,
                    reason=f"{reason} (import row {row['row_key']})",
                )
        except ValidationError as exc:
            problems.append(f"Line {row['line']} ({row['row_key']}): {' '.join(exc.messages)}")
            continue
        created.append(specimen)
    if problems:
        raise ValidationError(
            [
                ValidationError(message, code="invalid_import")
                for message in (
                    "Nothing was registered. Something changed since validation:",
                    *problems,
                )
            ]
        )
    commit = ImportCommit.objects.create(
        batch=batch,
        actor=actor,
        recorded_at=now,
        reason=reason,
        specimen_ids=[str(s.pk) for s in created],
    )
    record_event(
        actor=actor,
        action="specimens.import_committed",
        obj=batch,
        after={
            "rows": len(created),
            "accessions": [s.accession for s in created],
            "commit": str(commit.pk),
        },
        reason=reason,
    )
    return commit
