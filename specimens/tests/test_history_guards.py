"""Append-only history and undeletable records, exercised with raw SQL that bypasses the models."""

import importlib

import pytest
from django.apps import apps
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from specimens import exports, importing, services
from specimens.services import CorrectionValues

from .conftest import (
    ago,
    ask_disposal,
    catalogue,
    check_out,
    child,
    decide,
    derive,
    execute,
    hold,
    release,
    stored,
)

pytestmark = pytest.mark.django_db

migration = importlib.import_module("specimens.migrations.0002_history_triggers_and_accession")
HISTORY_TABLES = migration.HISTORY_TABLES
UNDELETABLE_TABLES = migration.UNDELETABLE_TABLES


def count(table):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {table}")
        return cursor.fetchone()[0]


def refused(sql):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql)


@pytest.fixture
def history(people, world):
    """At least one row in every append-only table, made through the services."""
    t, m, a = people["technician"], people["manager"], people["approver"]
    parent = stored(t, world, quantity="3")
    check_out(t, parent, t, occurred_at=ago(200))
    derive(t, parent, [child(world, "1")], "1", occurred_at=ago(190))
    held = hold(t, parent, "disposal", occurred_at=ago(180))
    release(m, held, occurred_at=ago(170))
    bigger = catalogue(
        m,
        "container_type",
        code="CRYOVIAL-5ML",
        name="Cryovial 5 mL",
        nominal_capacity="5",
        capacity_unit=world.ml.pk,
    )
    services.correct_specimen(
        actor=m,
        specimen_id=parent.pk,
        expected_revision=type(parent).objects.get(pk=parent.pk).revision,
        values=CorrectionValues(fields=frozenset({"container_type"}), container_type_id=bigger.pk),
        reason="Wrong vial type entered",
    )
    request = ask_disposal(t, parent)
    decide(a, request)
    execute(t, request, occurred_at=timezone.now())
    csv = (
        ",".join(importing.COLUMNS) + "\nR1,PLASMA,CRYOVIAL-2ML,1,mL,2026-01-01T09:00:00Z,,,,,,,0\n"
    ).encode()
    batch = importing.validate_import(actor=t, csv_bytes=csv)
    importing.commit_import(
        actor=t, batch_id=batch.pk, expected_sha256=batch.sha256, reason="Shipment 7"
    )
    exports.export_specimens(actor=people["viewer"], filters={})


def test_every_append_only_table_has_rows_in_the_scenario(history):
    assert [table for table in HISTORY_TABLES if count(table) == 0] == []


@pytest.mark.parametrize("table", HISTORY_TABLES)
def test_history_rows_cannot_be_updated_or_deleted(history, table):
    before = count(table)
    refused(f"UPDATE {table} SET id = id")
    refused(f"DELETE FROM {table}")
    assert count(table) == before


@pytest.mark.parametrize("table", UNDELETABLE_TABLES)
def test_specimens_and_catalogues_cannot_be_deleted(history, table):
    before = count(table)
    refused(f"DELETE FROM {table}")
    assert count(table) == before


def test_the_specimen_projection_and_catalogue_names_may_still_change(history):
    with connection.cursor() as cursor:
        cursor.execute("UPDATE specimens_specimen SET revision = revision + 1")
        assert cursor.rowcount > 0
        cursor.execute("UPDATE specimens_specimentype SET name = name || ' (renamed)'")
        assert cursor.rowcount > 0


def test_every_specimens_table_is_guarded_or_deliberately_mutable(history):
    mutable = {"specimens_specimen", *UNDELETABLE_TABLES}
    tables = {model._meta.db_table for model in apps.get_app_config("specimens").get_models()}
    assert tables - set(HISTORY_TABLES) - mutable == set()
