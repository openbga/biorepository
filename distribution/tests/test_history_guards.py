"""Exercise every append-only and undeletable record, including with raw SQL.

Each protected table gets at least one synthetic row written through the services.
"""

import importlib
from decimal import Decimal

import pytest
from django.apps import apps
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from core.models import AuditEvent
from distribution import services
from distribution.models import (
    ConsentWithdrawalEscalation,
    OutputRecord,
    RequestItem,
    SpecimenRequest,
)

from .helpers import (
    allocate,
    approved_request,
    consent_for,
    csv_file,
    dispatch,
    fast_password_hashing,  # noqa: F401
    fresh,
    new_request,
    packed_shipment,
    pick,
)

pytestmark = pytest.mark.django_db

migration = importlib.import_module("distribution.migrations.0002_history_triggers")
HISTORY_TABLES = (*migration.HISTORY_TABLES, ConsentWithdrawalEscalation._meta.db_table)
UNDELETABLE_TABLES = migration.UNDELETABLE_TABLES
MESSAGE = "Historical records cannot be updated or deleted"


def model_for(table):
    for model in apps.get_app_config("distribution").get_models():
        if model._meta.db_table == table:
            return model
    raise LookupError(table)


def count(table):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {table}")
        return cursor.fetchone()[0]


def refused(sql, params=()):
    """The trigger itself refuses (not a foreign key or some other error)."""
    with (
        pytest.raises(DatabaseError, match=MESSAGE),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(sql, params)


def text_column(table):
    """Some non-key text column of the table, so an UPDATE is a real change."""
    for field in model_for(table)._meta.concrete_fields:
        if not field.primary_key and field.get_internal_type() in ("CharField", "TextField"):
            return field.column
    return None


def now():
    return timezone.now()


@pytest.fixture
def history(people, dist, world):
    """At least one row in every table of both lists, made through the services."""
    tech, manager = people["technician"], people["manager"]
    services.add_agreement_document(
        actor=manager,
        agreement_id=dist.agreement.pk,
        kind="amendment",
        title="Amendment 1 (synthetic)",
        note="",
        upload=csv_file("amendment,1\n", "amendment.csv"),
    )
    request = approved_request(people, dist, world, count=3)
    rows = {a.specimen_id: a for a in allocate(people, request, dist.specimens)}
    first, second, third = (rows[s.pk] for s in dist.specimens)
    pick(people, request, [first, second])
    shipment = packed_shipment(people, request, [first, second])
    dispatch(people, shipment)
    shipment.refresh_from_db()
    items = list(shipment.items.order_by("line"))
    services.record_receipt(
        actor=tech,
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        values=services.ReceiptValues(
            received_at=now(),
            received_by_name="Example Receiver",
            unpacked_by_name="",
            condition="discrepancy",
            refrigerant_on_arrival="adequate",
            temperature_note="",
            discrepancies="One vial thawed",
            received_via="email",
            exceptions=((items[0].pk, "thawed", "Arrived thawed"),),
        ),
        upload=csv_file("receipt,1\n", "receipt.csv"),
        reason="Recipient's email of today",
    )
    returned = services.record_return(
        actor=tech,
        item_id=items[1].pk,
        expected_revision=fresh(items[1].specimen).revision,
        quantity=Decimal("1.5"),
        supplied_text="1.5",
        thaw_count=None,
        condition="Unopened",
        recipient_id=tech.pk,
        occurred_at=now(),
        reason="Returned unused",
    )
    services.review_return(
        actor=manager,
        return_id=returned.pk,
        expected_revision=fresh(returned.specimen).revision,
        decision="release",
        occurred_at=now(),
        reason="Seal intact",
    )
    consent = dist.consent
    consent.refresh_from_db()
    withdrawal = services.withdraw_consent(
        actor=manager,
        consent_id=consent.pk,
        expected_revision=consent.revision,
        kind="no_use",
        effective_on=timezone.localdate(),
        received_via="Letter (synthetic)",
        occurred_at=now(),
        reason="Participant asked to stop use",
    )
    consent.refresh_from_db()
    services.escalate_withdrawal(
        actor=manager,
        consent_id=consent.pk,
        expected_revision=consent.revision,
        effective_on=timezone.localdate(),
        received_via="Letter (synthetic)",
        occurred_at=now(),
        reason="Participant later asked for destruction",
    )
    services.record_follow_up(
        actor=manager,
        withdrawal_id=withdrawal.pk,
        shipment_id=shipment.pk,
        action="recipient_notified",
        note="Recipient told by email (synthetic)",
        occurred_at=now(),
    )
    record = consent_for(
        people,
        dist.study,
        dist.form,
        dist.consent.participant,
        answers={
            "GENERAL": "permitted",
            "GENETIC": "permitted",
            "COMMERCIAL": "refused",
            "RECONTACT": "permitted",
        },
        actor=people["manager2"],
    )
    consent.refresh_from_db()
    services.reinstate_consent(
        actor=people["admin"],
        consent_id=consent.pk,
        expected_revision=consent.revision,
        record_id=record.pk,
        occurred_at=now(),
        reason="Participant confirmed consent again",
    )
    OutputRecord.objects.create(
        kind="manifest", actor=manager, count=2, details={"shipment": shipment.number}
    )
    return request


def test_every_listed_table_has_rows_in_the_scenario(history):
    empty = [t for t in HISTORY_TABLES + UNDELETABLE_TABLES if count(t) == 0]
    assert empty == []


@pytest.mark.parametrize("table", HISTORY_TABLES)
def test_history_rows_cannot_be_updated_or_deleted(history, table):
    before = count(table)
    pk = model_for(table)._meta.pk.column
    refused(f"UPDATE {table} SET {pk} = {pk}")
    column = text_column(table)
    if column:
        refused(f"UPDATE {table} SET {column} = {column}")
    refused(f"DELETE FROM {table}")
    assert count(table) == before


@pytest.mark.parametrize("table", [t for t in HISTORY_TABLES if text_column(t)])
def test_a_changed_value_is_refused_not_just_a_no_op_update(history, table):
    column = text_column(table)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {column} FROM {table} ORDER BY 1 LIMIT 1")
        original = cursor.fetchone()[0]
    refused(f"UPDATE {table} SET {column} = 'tampered'")
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {column} FROM {table} ORDER BY 1 LIMIT 1")
        assert cursor.fetchone()[0] == original


@pytest.mark.parametrize("table", HISTORY_TABLES)
def test_history_rows_cannot_be_saved_over_or_deleted_through_the_models(history, table):
    model = model_for(table)
    row = model.objects.first()
    with pytest.raises(DatabaseError, match=MESSAGE), transaction.atomic():
        row.save()
    with pytest.raises(DatabaseError, match=MESSAGE), transaction.atomic():
        model.objects.filter(pk=row.pk)._raw_delete("default")  # past the PROTECT collector
    assert model.objects.filter(pk=row.pk).exists()


@pytest.mark.parametrize("table", UNDELETABLE_TABLES)
def test_governance_and_projection_rows_cannot_be_deleted(history, table):
    before = count(table)
    refused(f"DELETE FROM {table}")
    assert count(table) == before
    row = model_for(table).objects.first()
    with pytest.raises(DatabaseError, match=MESSAGE), transaction.atomic():
        model_for(table).objects.filter(pk=row.pk)._raw_delete("default")


@pytest.mark.parametrize("table", UNDELETABLE_TABLES)
def test_projection_rows_can_still_be_updated_through_their_services(history, table):
    """UPDATE stays possible (revisions, state): only DELETE is guarded."""
    pk = model_for(table)._meta.pk.column
    with connection.cursor() as cursor:
        cursor.execute(f"UPDATE {table} SET {pk} = {pk}")
        assert cursor.rowcount > 0


def test_request_lines_are_deletable_while_nothing_refers_to_them(people, dist, world):
    request = new_request(people["technician"], dist, world)
    item = request.items.get()
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM distribution_requestitem WHERE id = %s", [item.pk])
        assert cursor.rowcount == 1
    assert not RequestItem.objects.filter(pk=item.pk).exists()


def test_request_lines_with_allocations_are_protected_by_the_foreign_key(history):
    item = RequestItem.objects.filter(allocations__isnull=False).first()
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM distribution_requestitem WHERE id = %s", [item.pk])
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # Django's foreign keys are deferred
    assert RequestItem.objects.filter(pk=item.pk).exists()


def test_every_distribution_table_is_guarded_or_deliberately_mutable(history):
    guarded = set(HISTORY_TABLES) | set(UNDELETABLE_TABLES)
    tables = {
        model._meta.db_table
        for model in apps.get_app_config("distribution").get_models(include_auto_created=True)
    }
    unguarded = tables - guarded
    through = {t for t in unguarded if model_for_through(t)}
    assert unguarded - through == {"distribution_requestitem"}


def model_for_through(table):
    """Many-to-many link tables (frozen by the services' allowlists, not by triggers)."""
    for model in apps.get_app_config("distribution").get_models(include_auto_created=True):
        if model._meta.db_table == table:
            return model._meta.auto_created is not False
    return False


def test_a_correction_is_a_new_row_and_leaves_the_original_untouched(history, people, dist):
    consent = dist.consent
    consent.refresh_from_db()
    current = consent.current_record
    services.correct_consent_record(
        actor=people["manager"],
        record_id=current.pk,
        expected_revision=consent.revision,
        values={"restrictions": "Corrected restriction text"},
        answers=None,
        reason="Transcription fix",
    )
    original = type(current).objects.get(pk=current.pk)
    assert original.restrictions == current.restrictions
    assert type(current).objects.filter(consent=consent).count() == 3


def test_services_write_an_attributed_audit_event_with_the_change(history, people):
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {
        "distribution.request_submitted",
        "distribution.request_decided",
        "distribution.allocated",
        "distribution.picked",
        "distribution.shipment_packed",
        "distribution.shipment_dispatched",
        "distribution.receipt_recorded",
        "distribution.return_recorded",
        "distribution.return_reviewed",
        "distribution.consent_withdrawn",
        "distribution.withdrawal_escalated",
        "distribution.consent_reinstated",
        "distribution.agreement_executed",
    } <= actions
    event = AuditEvent.objects.get(action="distribution.shipment_dispatched")
    assert event.actor_id == people["technician"].pk


def test_a_failed_service_leaves_neither_rows_nor_an_audit_event(people, dist, world):
    request = approved_request(people, dist, world)
    audit_before = AuditEvent.objects.count()
    events_before = count("distribution_requestevent")
    request.refresh_from_db()
    with pytest.raises(Exception):  # noqa: B017 - any refusal
        services.cancel_request(
            actor=people["technician"],
            request_id=request.pk,
            expected_revision=request.revision,
            reason="Not allowed",
        )
    with pytest.raises(Exception):  # noqa: B017
        services.cancel_request(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=request.revision + 5,
            reason="Stale",
        )
    assert AuditEvent.objects.count() == audit_before
    assert count("distribution_requestevent") == events_before
    assert SpecimenRequest.objects.get(pk=request.pk).status == "approved"
