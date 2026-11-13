"""core.attachments and core.models.Attachment: validation, one stored copy per upload, the
deployment storage budget, append-only rows, deferred bytes and the download response.
All files are synthetic."""

import hashlib
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from django import forms
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction

from core.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentField,
    attachment_fields,
    download_response,
    read_attachment,
    store_attachment,
)
from core.models import Attachment

PDF = b"%PDF-1.7\nsynthetic\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def upload(name, data):
    return SimpleUploadedFile(name, data, "application/octet-stream")


@pytest.mark.parametrize(
    ("name", "data", "content_type"),
    [
        ("report.pdf", PDF, "application/pdf"),
        ("photo.PNG", PNG, "image/png"),
        ("photo.jpeg", JPEG, "image/jpeg"),
        ("note.txt", "Synthetic note é\n".encode(), "text/plain; charset=utf-8"),
        ("probe.csv", b"probe,ok\n", "text/csv; charset=utf-8"),
    ],
)
def test_accepted_files_take_their_type_from_the_extension_and_content(name, data, content_type):
    fields = read_attachment(upload(name, data))
    assert fields == {
        "name": name,
        "content_type": content_type,  # never the client-supplied header
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": data,
    }


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("tool.exe", b"MZ", "Attach a PDF, PNG, JPEG, TXT or CSV file."),
        ("no-extension", PDF, "Attach a PDF, PNG, JPEG, TXT or CSV file."),
        ("fake.pdf", b"<html>", "does not look like a PDF file"),
        ("fake.png", PDF, "does not look like a PNG file"),
        ("empty.txt", b"", "The file is empty."),
        ("latin1.csv", "café".encode("latin-1"), "must be UTF-8"),
        ("big.pdf", b"%PDF-" + b"0" * MAX_ATTACHMENT_BYTES, "larger than 10 MiB"),
    ],
)
def test_refused_files(name, data, message):
    with pytest.raises(ValueError, match=message):
        read_attachment(upload(name, data))


def test_the_stored_name_is_the_base_name_only():
    assert read_attachment(upload("../../etc/report.pdf", PDF))["name"] == "report.pdf"


def test_the_form_field_validates_once_and_leaves_the_stream_readable():
    field = AttachmentField()
    assert not field.required and field.help_text
    assert field.clean(None) is None
    with pytest.raises(forms.ValidationError, match="does not look like a PDF"):
        field.clean(upload("fake.pdf", b"<html>"))
    cleaned = field.clean(upload("report.pdf", PDF))
    assert cleaned.read() == PDF  # rewound for anything else that reads it
    assert attachment_fields(cleaned) is cleaned.attachment_fields  # remembered, not re-read


def test_a_service_sees_a_bad_file_as_an_error_on_its_own_field():
    with pytest.raises(ValidationError) as caught:
        attachment_fields(upload("fake.pdf", b"<html>"), field="agreement")
    assert list(caught.value.message_dict) == ["agreement"]


@pytest.mark.django_db
def test_an_upload_is_stored_once_with_its_creator(staff, settings):
    settings.ATTACHMENT_STORAGE_LIMIT_BYTES = None
    actor = staff("attach_tech", "technician")
    assert store_attachment(None, actor=actor) is None
    stored = store_attachment(upload("report.pdf", PDF), actor=actor)
    assert Attachment.objects.count() == 1
    row = Attachment.objects.with_data().get()
    assert row.pk == stored.pk and row.created_by == actor and row.created_at
    assert (row.name, row.size, bytes(row.data)) == ("report.pdf", len(PDF), PDF)
    assert row.sha256 == hashlib.sha256(PDF).hexdigest()


@pytest.mark.django_db
def test_the_storage_budget_refuses_an_upload_that_would_exceed_it(staff, settings):
    actor = staff("budget_tech", "technician")
    settings.ATTACHMENT_STORAGE_LIMIT_BYTES = 2 * len(PDF)
    store_attachment(upload("one.pdf", PDF), actor=actor)
    store_attachment(upload("two.pdf", PDF), actor=actor)  # exactly at the limit is allowed
    with pytest.raises(ValidationError) as caught:
        store_attachment(upload("three.pdf", PDF), actor=actor, field="agreement")
    assert "storage for attached files is full" in caught.value.message_dict["agreement"][0]
    assert Attachment.objects.count() == 2


def test_the_budget_setting_comes_from_the_environment():
    root = Path(__file__).resolve().parents[1]
    code = "import config.settings as s; print(s.ATTACHMENT_STORAGE_LIMIT_BYTES)"

    def read(value):
        env = {**os.environ, "DEBUG": "true", "ATTACHMENT_STORAGE_LIMIT_BYTES": value}
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    assert read("1073741824") == "1073741824"
    assert read("") == "None"


@pytest.mark.django_db(transaction=True)
def test_concurrent_uploads_cannot_overshoot_the_budget_together(staff, settings):
    """The first upload holds its transaction open after its budget check; the second must wait
    for it and then see its bytes, so only one fits."""
    actor = staff("race_tech", "technician")
    settings.ATTACHMENT_STORAGE_LIMIT_BYTES = len(PDF) + 1
    checked, release = threading.Event(), threading.Event()
    outcomes = {}

    def run(name):
        try:
            with transaction.atomic():
                outcomes[name] = store_attachment(upload(f"{name}.pdf", PDF), actor=actor)
                if name == "first":
                    checked.set()
                    assert release.wait(15)
        except Exception as exc:  # noqa: BLE001 - reported below
            outcomes[name] = exc
        finally:
            connection.close()

    first = threading.Thread(target=run, args=("first",), name="first")
    first.start()
    assert checked.wait(15)
    second = threading.Thread(target=run, args=("second",), name="second")
    second.start()
    second.join(1)
    assert second.is_alive()  # waiting on the budget lock, not reading a stale total
    release.set()
    first.join(15)
    second.join(15)
    assert isinstance(outcomes["first"], Attachment)
    assert isinstance(outcomes["second"], ValidationError)
    assert Attachment.objects.count() == 1


@pytest.mark.django_db
def test_stored_files_are_append_only(staff):
    store_attachment(upload("note.txt", b"Synthetic.\n"), actor=staff("guard_tech", "technician"))
    for sql in ("UPDATE attachment SET name = 'other.txt'", "DELETE FROM attachment"):
        with pytest.raises(DatabaseError, match="Historical records cannot be updated or deleted"):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql)
    assert Attachment.objects.get().name == "note.txt"


@pytest.mark.django_db
def test_queries_and_followed_references_leave_the_bytes_unloaded(staff):
    store_attachment(upload("note.txt", b"Synthetic.\n"), actor=staff("defer_tech", "technician"))
    assert "data" in Attachment.objects.get().get_deferred_fields()
    # A ForeignKey to Attachment is followed through the base manager.
    assert "data" in Attachment._base_manager.get().get_deferred_fields()
    assert "data" not in Attachment.objects.with_data().get().get_deferred_fields()


@pytest.mark.django_db
def test_the_download_is_always_an_attachment(staff):
    stored = store_attachment(
        upload('synthetic "quoted" report.pdf', PDF), actor=staff("dl_tech", "technician")
    )
    response = download_response(Attachment.objects.with_data().get(pk=stored.pk))
    assert response.content == PDF
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("attachment;")
    assert '\\"quoted\\"' in response["Content-Disposition"]
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "private, no-store"
