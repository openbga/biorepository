import csv
import io
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.urls import reverse

from core.models import AuditEvent
from quality import services
from quality.models import AuditFinalization
from quality.scanner_import import MAX_UPLOAD_BYTES, parse_observations
from quality.tests.test_audits import complete, create
from specimens.models import CustodyEvent
from specimens.tests.conftest import move_box, stored
from storage.models import Occupancy, Position

pytestmark = pytest.mark.django_db


def rows_for(audit):
    return [[audit.box_code, position["label"], "empty", ""] for position in audit.positions]


def upload_rows(rows, *, header=None, bom=False):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(header if header is not None else ["box", "position", "state", "accession"])
    writer.writerows(rows)
    content = output.getvalue().encode("utf-8")
    return upload_bytes((b"\xef\xbb\xbf" if bom else b"") + content)


def upload_bytes(content):
    return SimpleUploadedFile("synthetic-scanner.csv", content, content_type="text/csv")


def posted_grid(audit):
    data = {
        "action": "load_scanner",
        "reason": "Synthetic inspection notes",
        "discrepancy_explanation": "Synthetic discrepancy explanation",
    }
    for i in range(len(audit.positions)):
        data[f"state_{i}"] = "empty"
        data[f"accession_{i}"] = ""
    data.update(state_0="accession", accession_0="SYNTHETIC-PRIOR-OBSERVATION")
    return data


@pytest.mark.parametrize("bom", [False, True])
def test_complete_file_maps_exact_labels_in_any_order_and_unknown_ids(people, world, bom):
    audit = create(people, world)
    rows = rows_for(audit)
    rows[0][2:] = ["accession", 'SYNTHETIC-UNKNOWN,"CODE"']
    observations = parse_observations(upload_rows(rows[::-1], bom=bom), audit=audit)
    assert list(observations) == [p["id"] for p in audit.positions[::-1]]
    assert observations[audit.positions[0]["id"]] == 'SYNTHETIC-UNKNOWN,"CODE"'
    assert observations[audit.positions[1]["id"]] == ""


@pytest.mark.parametrize(
    "kind",
    [
        "wrong_box",
        "box_case",
        "unknown_position",
        "position_case",
        "duplicate_position",
        "missing_position",
        "blank_row",
        "extra_column",
        "missing_column",
        "bad_state",
        "state_case",
        "empty_with_accession",
        "missing_accession",
        "long_accession",
        "surrounding_spaces",
        "duplicate_accession",
        "control",
        "format_control",
    ],
)
def test_rejects_invalid_or_incomplete_observations(people, world, kind):
    audit = create(people, world)
    rows = rows_for(audit)
    if kind == "wrong_box":
        rows[0][0] = "SYNTHETIC-OTHER-BOX"
    elif kind == "box_case":
        rows[0][0] = audit.box_code.lower()
    elif kind == "unknown_position":
        rows[0][1] = "SYNTHETIC-NO-SUCH-POSITION"
    elif kind == "position_case":
        rows[0][1] = rows[0][1].lower()
    elif kind == "duplicate_position":
        rows[-1] = rows[0].copy()
    elif kind == "missing_position":
        rows.pop()
    elif kind == "blank_row":
        rows.append([])
    elif kind == "extra_column":
        rows[0].append("unexpected")
    elif kind == "missing_column":
        rows[0].pop()
    elif kind == "bad_state":
        rows[0][2] = "unobserved"
    elif kind == "state_case":
        rows[0][2] = "Empty"
    elif kind == "empty_with_accession":
        rows[0][3] = "SYNTHETIC-NOT-EMPTY"
    elif kind == "missing_accession":
        rows[0][2] = "accession"
    else:
        value = {
            "long_accession": "X" * 81,
            "surrounding_spaces": " SYNTHETIC-ID ",
            "duplicate_accession": "SYNTHETIC-DUPLICATE",
            "control": "SYNTHETIC\nCODE",
            "format_control": "SYNTHETIC\u200bCODE",
        }[kind]
        rows[0][2:] = ["accession", value]
        if kind == "duplicate_accession":
            rows[1][2:] = ["accession", value.lower()]
    with pytest.raises(ValidationError):
        parse_observations(upload_rows(rows), audit=audit)


@pytest.mark.parametrize(
    "header",
    [
        [],
        ["box", "position", "accession", "state"],
        ["box", "position", "state", "state"],
        [" box", "position", "state", "accession"],
        ["box", "position", "state", "accession", "extra"],
    ],
)
def test_header_is_exact(people, world, header):
    audit = create(people, world)
    with pytest.raises(ValidationError, match="header"):
        parse_observations(upload_rows(rows_for(audit), header=header), audit=audit)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xff\xfe",
        b'box,position,state,accession\n"unterminated',
        b'box,position,state,accession\nBOX,A1,accession,unquoted"quote',
        b'box,position,state,accession\nBOX,A1,accession,"quoted"trailing',
        b"X" * (MAX_UPLOAD_BYTES + 1),
    ],
)
def test_encoding_csv_syntax_and_size_are_bounded(people, world, content):
    audit = create(people, world)
    upload = Mock(read=Mock(return_value=content))
    with pytest.raises(ValidationError):
        parse_observations(upload, audit=audit)
    upload.read.assert_called_once_with(MAX_UPLOAD_BYTES + 1)


@pytest.mark.parametrize("role", ["admin", "manager", "technician"])
def test_preview_populates_editable_grid_keeps_notes_and_changes_no_records(
    client, people, world, role
):
    audit = create(people, world)
    rows = rows_for(audit)
    rows[1][2:] = ["accession", "SYNTHETIC-LOADED-UNKNOWN"]
    before = evidence_state()
    data = posted_grid(audit)
    data["scanner_file"] = upload_rows(rows)
    client.force_login(people[role])
    response = client.post(reverse("quality:audit-complete", args=[audit.pk]), data)
    assert response.status_code == 200
    assert response.context["loaded"]
    form = response.context["form"]
    assert form["state_0"].value() == "empty"
    assert form["accession_0"].value() == ""
    assert form["state_1"].value() == "accession"
    assert form["accession_1"].value() == "SYNTHETIC-LOADED-UNKNOWN"
    assert form["reason"].value() == data["reason"]
    assert form["discrepancy_explanation"].value() == data["discrepancy_explanation"]
    assert not form.fields["state_0"].widget.attrs.get("disabled")
    assert not form.errors
    assert evidence_state() == before
    assert b"multipart/form-data" in response.content
    assert b"Nothing has been saved" in response.content


def evidence_state():
    return {
        "events": list(AuditEvent.objects.values()),
        "finalizations": list(AuditFinalization.objects.values()),
        "occupancy": list(Occupancy.objects.values()),
        "positions": list(Position.objects.values()),
        "custody": list(CustodyEvent.objects.values()),
    }


@pytest.mark.parametrize("kind", ["invalid", "missing", "oversize"])
def test_rejected_upload_keeps_grid_and_notes(client, people, world, kind):
    audit = create(people, world)
    client.force_login(people["technician"])
    data = posted_grid(audit)
    if kind != "missing":
        data["scanner_file"] = upload_bytes(
            b"invalid" if kind == "invalid" else b"X" * (MAX_UPLOAD_BYTES + 1)
        )
    before = evidence_state()
    response = client.post(reverse("quality:audit-complete", args=[audit.pk]), data)
    assert response.status_code == 200
    assert response.context["upload_form"].errors
    assert not response.context["loaded"]
    form = response.context["form"]
    for name in ("state_0", "accession_0", "reason", "discrepancy_explanation"):
        assert form[name].value() == data[name]
    assert evidence_state() == before


@pytest.mark.parametrize("role", ["viewer", "nobody", "bare_superuser"])
def test_import_roles_rejected_before_parsing(client, people, world, role):
    audit = create(people, world)
    actor = people["nobody"] if role == "bare_superuser" else people[role]
    if role == "bare_superuser":
        actor.is_superuser = True
        actor.save()
    client.force_login(actor)
    with patch("quality.services.parse_observations") as parse:
        assert (
            client.post(
                reverse("quality:audit-complete", args=[audit.pk]),
                {"action": "load_scanner", "scanner_file": upload_rows(rows_for(audit))},
            ).status_code
            == 403
        )
        with pytest.raises(PermissionDenied):
            services.load_scanner_observations(actor=actor, audit_id=999999, upload=None)
    parse.assert_not_called()


@pytest.mark.parametrize("state", ["stale", "completed", "cancelled"])
def test_blocked_import_retains_inputs_and_never_parses(client, people, world, state):
    audit = create(people, world)
    if state == "stale":
        move_box(people["manager"], world.box, world.shelf2)
    elif state == "completed":
        complete(people, audit)
    else:
        services.cancel_audit(actor=people["technician"], audit_id=audit.pk, reason="Restart")
    data = posted_grid(audit)
    data["scanner_file"] = upload_rows(rows_for(audit))
    before = evidence_state()
    client.force_login(people["technician"])
    with patch("quality.services.parse_observations") as parse:
        response = client.post(reverse("quality:audit-complete", args=[audit.pk]), data)
        with pytest.raises(ValidationError):
            services.load_scanner_observations(
                actor=people["technician"], audit_id=audit.pk, upload=data["scanner_file"]
            )
    parse.assert_not_called()
    assert response.context["blocked"]
    assert response.context["form"]["accession_0"].value() == data["accession_0"]
    assert response.context["form"]["reason"].value() == data["reason"]
    assert b'value="load_scanner"' not in response.content
    assert evidence_state() == before


def test_reviewed_edits_only_are_finalized_and_immutable(client, people, world):
    specimen = stored(people["technician"], world)
    audit = create(people, world)
    rows = rows_for(audit)
    rows[0][2:] = ["accession", specimen.accession]
    rows[1][2:] = ["accession", "SYNTHETIC-CSV-UNKNOWN"]
    data = posted_grid(audit)
    data["scanner_file"] = upload_rows(rows)
    client.force_login(people["technician"])
    url = reverse("quality:audit-complete", args=[audit.pk])
    preview = client.post(url, data)
    form = preview.context["form"]
    reviewed = {name: form[name].value() for name in form.fields}
    reviewed.update(action="complete", accession_1="SYNTHETIC-REVIEWED-UNKNOWN")
    assert client.post(url, reviewed).status_code == 302
    final = AuditFinalization.objects.get(audit=audit)
    assert final.observations["positions"][1]["observed"] == "SYNTHETIC-REVIEWED-UNKNOWN"
    assert final.counts == {"match": 8, "missing": 0, "unexpected": 1, "mismatch": 0}
    assert AuditEvent.objects.filter(action="quality.audit_completed").count() == 1
    assert Occupancy.objects.filter(pk=specimen.occupancy_id).exists()
    with pytest.raises(DatabaseError), transaction.atomic():
        AuditFinalization.objects.filter(pk=final.pk).update(reason="Changed synthetic result")


def test_complete_with_unreviewed_file_does_not_commit(client, people, world):
    audit = create(people, world)
    data = posted_grid(audit)
    data.update(action="complete", scanner_file=upload_rows(rows_for(audit)))
    client.force_login(people["technician"])
    response = client.post(reverse("quality:audit-complete", args=[audit.pk]), data)
    assert response.context["upload_form"].errors
    assert not AuditFinalization.objects.exists()


@pytest.mark.parametrize("action", ["load_scanner", "complete"])
def test_staleness_between_preflight_and_lock_rejects_without_events(client, people, world, action):
    audit = create(people, world)
    data = posted_grid(audit)
    data["action"] = action
    if action == "load_scanner":
        data["scanner_file"] = upload_rows(rows_for(audit))
    client.force_login(people["technician"])
    before = evidence_state()
    with patch("quality.services.is_stale", side_effect=[False, True]):
        response = client.post(reverse("quality:audit-complete", args=[audit.pk]), data)
    assert response.context["blocked"]
    assert response.context["form"]["reason"].value() == data["reason"]
    assert evidence_state() == before


def test_max_length_and_markup_are_literal_observations(client, people, world):
    audit = create(people, world)
    rows = rows_for(audit)
    rows[0][2:] = ["accession", "X" * 80]
    rows[1][2:] = ["accession", "<script>SYNTHETIC</script>"]
    rows[2][2:] = ["accession", '=SYNTHETIC("literal")']
    data = {"action": "load_scanner", "scanner_file": upload_rows(rows)}
    client.force_login(people["technician"])
    response = client.post(reverse("quality:audit-complete", args=[audit.pk]), data)
    assert response.context["loaded"]
    assert response.context["form"]["accession_0"].value() == "X" * 80
    assert response.context["form"]["accession_2"].value() == '=SYNTHETIC("literal")'
    assert b"<script>SYNTHETIC</script>" not in response.content
    assert b"&lt;script&gt;SYNTHETIC&lt;/script&gt;" in response.content
    assert not AuditFinalization.objects.exists()


def test_finalization_between_preflight_and_load_is_blocked(client, people, world):
    audit = create(people, world)
    data = posted_grid(audit)
    data["scanner_file"] = upload_rows(rows_for(audit))
    client.force_login(people["technician"])
    before = evidence_state()
    with (
        patch(
            "quality.services._open_locked",
            side_effect=ValidationError(
                "This audit is already finalized.", code="invalid_transition"
            ),
        ),
        patch("quality.services.parse_observations") as parse,
    ):
        response = client.post(reverse("quality:audit-complete", args=[audit.pk]), data)
    parse.assert_not_called()
    assert response.context["blocked"]
    assert response.context["form"]["accession_0"].value() == data["accession_0"]
    assert evidence_state() == before
