"""Bounded project CSV interchange; file contents are observations, never instructions."""

import csv
import io
import unicodedata

from django.core.exceptions import ValidationError

MAX_UPLOAD_BYTES = 128 * 1024
HEADER = ["box", "position", "state", "accession"]


def _check_quoting(source):
    # csv.reader(strict=True) still accepts quotes embedded in unquoted fields.
    # Refuse those rather than silently accepting a malformed interchange file.
    state = "start"
    for character in source:
        if state == "quoted":
            if character == '"':
                state = "after_quote"
        elif state == "after_quote":
            if character == '"':
                state = "quoted"
            elif character in ",\r\n":
                state = "start"
            else:
                raise ValidationError("Invalid CSV. Check quoted fields and delimiters.")
        elif character == '"':
            if state != "start":
                raise ValidationError("Invalid CSV. Quotes must enclose the whole field.")
            state = "quoted"
        elif character in ",\r\n":
            state = "start"
        else:
            state = "unquoted"
    if state == "quoted":
        raise ValidationError("Invalid CSV. Close every quoted field.")


def parse_observations(upload, *, audit):
    """Return a complete position-id mapping without retaining or changing records."""
    content = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValidationError("Use a CSV file of at most 128 KiB.")
    try:
        source = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("Use a UTF-8 CSV file (a UTF-8 BOM is accepted).") from None
    _check_quoting(source)
    positions = {position["label"]: position["id"] for position in audit.positions}
    observed, seen = {}, set()
    try:
        reader = csv.reader(io.StringIO(source, newline=""), strict=True)
        if next(reader, None) != HEADER:
            raise ValidationError("Use the exact header: box,position,state,accession.")
        for number, row in enumerate(reader, start=2):
            if len(row) != len(HEADER):
                raise ValidationError(f"Row {number}: supply exactly four columns; no blank rows.")
            if any(
                unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                for value in row
                for character in value
            ):
                raise ValidationError(f"Row {number}: control characters are not allowed.")
            box, label, state, accession = row
            if box != audit.box_code:
                raise ValidationError(f"Row {number}: the box must exactly match {audit.box_code}.")
            if label not in positions:
                raise ValidationError(f"Row {number}: use an exact position label from this audit.")
            position_id = positions[label]
            if position_id in observed:
                raise ValidationError(f"Row {number}: position {label} appears more than once.")
            if state not in {"empty", "accession"}:
                raise ValidationError(f"Row {number}: state must be empty or accession.")
            if state == "empty" and accession:
                raise ValidationError(f"Row {number}: an empty position must have no accession.")
            if state == "accession" and (
                not accession or accession != accession.strip() or len(accession) > 80
            ):
                raise ValidationError(
                    f"Row {number}: supply an accession of 1 to 80 characters "
                    "with no surrounding whitespace."
                )
            if accession:
                key = accession.casefold()
                if key in seen:
                    raise ValidationError(
                        f"Row {number}: an observed accession appears more than once "
                        "(ignoring letter case)."
                    )
                seen.add(key)
            observed[position_id] = accession
    except csv.Error:
        raise ValidationError("Invalid CSV. Check quoting, delimiters and field lengths.") from None
    if set(observed) != set(positions.values()):
        raise ValidationError(
            "Include every snapshotted position exactly once, including empty ones."
        )
    return observed
