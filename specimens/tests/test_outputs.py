"""Labels, CSV import and exports: outputs."""

import os

import pytest
from django.core.exceptions import ValidationError

from core.models import AuditEvent
from specimens import exports, importing, labels
from specimens.models import ImportCommit, OutputRecord, Specimen

from .conftest import register

pytestmark = pytest.mark.django_db


def weasyprint_or_skip():
    try:
        import weasyprint  # noqa: F401
    except OSError as exc:
        if os.environ.get("REQUIRE_WEASYPRINT") == "1":
            raise
        pytest.skip(f"WeasyPrint native libraries unavailable: {str(exc)[:80]}")


def test_label_pdf_has_one_page_per_label_and_is_audited(people, world):
    weasyprint_or_skip()
    specimen = register(people["technician"], world)
    pdf = labels.render_labels(actor=people["viewer"], specimen_ids=(specimen.pk, specimen.pk))
    assert pdf.startswith(b"%PDF")
    record = OutputRecord.objects.get(kind="labels")
    assert record.count == 2 and record.details["accessions"] == [specimen.accession] * 2
    assert AuditEvent.objects.filter(action="specimens.labels_printed").count() == 1


def test_label_barcode_must_fit_the_stock(people, world):
    specimen = register(people["technician"], world)
    narrow = labels.LabelStock("TINY", 1, labels.Decimal("20"), labels.Decimal("10"))
    with pytest.raises(ValidationError):
        labels.barcode_svg(specimen.accession, narrow)


CSV_HEADER = ",".join(importing.COLUMNS)


def csv_bytes(*rows):
    return ("\n".join([CSV_HEADER, *rows]) + "\n").encode()


def test_import_validates_then_commits_all_rows_once(people, world):
    tech = people["technician"]
    good = csv_bytes(
        "R1,PLASMA,CRYOVIAL-2ML,1.5,mL,2026-01-01T09:00:00Z,,,P-000123,DEMO,BOX-001,C3,",
        "R2,plasma,CRYOVIAL-2ML,0.25,mL,2026-01-01T09:00:00Z,,,,,,,2",
    )
    batch = importing.validate_import(actor=tech, csv_bytes=good)
    assert batch.errors == [] and batch.row_count == 2
    assert not Specimen.objects.exists()  # validation registers nothing
    commit = importing.commit_import(
        actor=tech, batch_id=batch.pk, expected_sha256=batch.sha256, reason="Shipment 7"
    )
    assert len(commit.specimen_ids) == 2
    again = importing.commit_import(
        actor=tech, batch_id=batch.pk, expected_sha256=batch.sha256, reason="Shipment 7"
    )
    assert again.pk == commit.pk and ImportCommit.objects.count() == 1
    assert Specimen.objects.filter(occupancy__position__label="C3").count() == 1


def test_import_reports_row_errors_and_unknown_columns(people, world):
    bad = csv_bytes(
        "R1,PLASMA,CRYOVIAL-2ML,1.1234567,mL,2026-01-01T09:00:00Z,,,P-999999,,BOX-001,,",
        "R1,NOPE,CRYOVIAL-2ML,1,ML,2999-01-01T09:00:00Z,,,,,,,x",
    )
    batch = importing.validate_import(actor=people["technician"], csv_bytes=bad)
    columns = {(e["line"], e["column"]) for e in batch.errors}
    assert {(2, "quantity"), (2, "participant_code"), (2, "position_label")} <= columns
    assert {
        (3, "row_key"),
        (3, "specimen_type"),
        (3, "unit"),
        (3, "collected_at"),
        (3, "baseline_thaws"),
    } <= columns
    with pytest.raises(ValidationError):
        importing.commit_import(
            actor=people["technician"], batch_id=batch.pk, expected_sha256=batch.sha256, reason="x"
        )
    header = importing.validate_import(
        actor=people["technician"], csv_bytes=b"row_key,donor_name\nR1,x\n"
    )
    assert "Unknown columns: donor_name" in header.errors[0]["message"]


def test_export_escapes_formula_cells_and_records_the_selection(people, world):
    register(people["technician"], world)
    data = exports.export_specimens(actor=people["viewer"], filters={"status": "available"})
    lines = data.decode().splitlines()
    assert lines[0].startswith("accession,status") and len(lines) == 2
    assert exports.safe_cell("=1+1") == "'=1+1" and exports.safe_cell("-2") == "'-2"
    assert OutputRecord.objects.get(kind="export").count == 1
