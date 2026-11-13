"""Migration-level protections, exercised with writes that bypass model save hooks."""

import pytest
from django.db import DatabaseError, connection, transaction

from core.immutability import FUNCTION, protect_history
from core.models import AuditEvent


def forward_and_reverse(table, trigger=None):
    operation = protect_history(table, trigger)
    return operation.sql, operation.reverse_sql


@pytest.mark.django_db
def test_audit_events_cannot_be_updated_or_deleted(staff):
    actor = staff("history_admin", "admin")
    event = AuditEvent.objects.create(
        actor=actor, action="probe.created", object_type="probe", object_id="1"
    )
    rows = AuditEvent.objects.filter(pk=event.pk)
    with pytest.raises(DatabaseError), transaction.atomic():
        rows.update(action="probe.rewritten")
    with pytest.raises(DatabaseError), transaction.atomic():
        rows.delete()
    with pytest.raises(DatabaseError), transaction.atomic():
        event.delete()
    event.refresh_from_db()
    assert event.action == "probe.created"
    AuditEvent.objects.create(
        actor=actor, action="probe.corrected", object_type="probe", object_id="1", reason="Fix"
    )  # corrections add a record
    assert AuditEvent.objects.count() == 2


@pytest.mark.django_db
def test_protect_history_attaches_and_removes_an_append_only_trigger():
    forward, reverse_sql = forward_and_reverse("tmp_history_probe")
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE tmp_history_probe (id integer PRIMARY KEY, note text)")
        cursor.execute(forward)
        cursor.execute("INSERT INTO tmp_history_probe VALUES (1, 'first')")
        with pytest.raises(DatabaseError), transaction.atomic():
            cursor.execute("UPDATE tmp_history_probe SET note = 'rewritten'")
        with pytest.raises(DatabaseError), transaction.atomic():
            cursor.execute("DELETE FROM tmp_history_probe")
        cursor.execute("INSERT INTO tmp_history_probe VALUES (2, 'second')")
        cursor.execute("SELECT note FROM tmp_history_probe ORDER BY id")
        assert [row[0] for row in cursor.fetchall()] == ["first", "second"]

        cursor.execute(reverse_sql)
        cursor.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'tmp_history_probe_immutable'"
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute("UPDATE tmp_history_probe SET note = 'changed' WHERE id = 1")
        cursor.execute("DELETE FROM tmp_history_probe WHERE id = 2")
        cursor.execute("SELECT note FROM tmp_history_probe")
        assert [row[0] for row in cursor.fetchall()] == ["changed"]


def test_protect_history_uses_the_shared_function_and_a_custom_trigger_name():
    forward, reverse_sql = forward_and_reverse("tmp_history_probe", "probe_guard")
    assert f"EXECUTE FUNCTION {FUNCTION}()" in forward
    assert "TRIGGER probe_guard BEFORE UPDATE OR DELETE ON tmp_history_probe" in forward
    assert reverse_sql == "DROP TRIGGER probe_guard ON tmp_history_probe;"


@pytest.mark.parametrize(
    "bad", ["Mixed", "x; DROP TABLE core_auditevent", "with space", "1leading", "a" * 64, "a-b"]
)
def test_protect_history_rejects_a_non_identifier_table_name(bad):
    with pytest.raises(ValueError):
        protect_history(bad)
    with pytest.raises(ValueError):
        protect_history("tmp_history_probe", trigger=bad)
    with pytest.raises(ValueError):
        protect_history("")
