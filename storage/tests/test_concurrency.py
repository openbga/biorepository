"""Storage locks under real concurrency: two threads, each with its own PostgreSQL connection.

The thread named "first" is held inside its transaction (after its writes and audit event,
before commit) while "second" runs; `blocked` asks PostgreSQL whether "second" is blocked
behind it. Then "first" commits and "second" revalidates against the committed state.
"""

import csv
import io
import threading
import time

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.urls import reverse

from core.models import AuditEvent
from storage import services
from storage.models import Box, MoveEvent, Occupancy, Position, Site, StorageGate

from .conftest import ago, gate_revision, new_item, place
from .test_occupancy import cell

pytestmark = pytest.mark.django_db(transaction=True)


class Hold:
    """Hold the thread named "first" in storage.services.record_event until released."""

    def __init__(self, monkeypatch):
        self.reached, self.release = threading.Event(), threading.Event()
        real = services.record_event

        def held(**kwargs):
            event = real(**kwargs)
            if threading.current_thread().name == "first" and not self.reached.is_set():
                self.reached.set()
                assert self.release.wait(15)
            return event

        monkeypatch.setattr(services, "record_event", held)


def run(name, fn, outcomes, pids):
    def target():
        try:
            connection.ensure_connection()
            pids[name] = connection.connection.info.backend_pid
            outcomes[name] = fn()
        except Exception as exc:  # the assertions inspect it
            outcomes[name] = exc
        finally:
            connection.close()

    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def blocked(pid):
    with connection.cursor() as cursor:
        cursor.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", [pid])
        return cursor.fetchone()[0]


def race(hold, first, second):
    outcomes, pids = {}, {}
    threads = [run("first", first, outcomes, pids)]
    assert hold.reached.wait(15), outcomes.get("first")
    threads.append(run("second", second, outcomes, pids))
    try:
        deadline = time.monotonic() + 10
        while not ("second" in pids and blocked(pids["second"])):
            if time.monotonic() > deadline:
                pytest.fail(f"second did not wait for the first transaction: {outcomes}")
            time.sleep(0.01)
    finally:
        hold.release.set()
        for thread in threads:
            thread.join(30)
    return outcomes["first"], outcomes["second"]


def codes(exc):
    assert isinstance(exc, ValidationError), exc
    if hasattr(exc, "error_dict"):
        return {e.code for errors in exc.error_dict.values() for e in errors}
    return {e.code for e in exc.error_list}


def test_two_people_cannot_store_into_the_same_position(tree, people, monkeypatch):
    t = people["technician"]
    one, two = new_item(t, ago(60)), new_item(t, ago(60))
    target = cell(tree.box, "E5")
    hold = Hold(monkeypatch)
    first, second = race(
        hold,
        lambda: place(t, one, target, ago(10)),
        lambda: place(people["manager"], two, target, ago(10)),
    )
    assert isinstance(first, MoveEvent)
    assert codes(second) == {"occupied"}
    assert Occupancy.objects.get(position=target).pk == one.pk
    assert Occupancy.objects.get(pk=two.pk).position is None


def test_two_boxes_cannot_take_the_same_slot(tree, people, monkeypatch):
    m = people["manager"]

    def create(code):
        return lambda: services.create_box(
            actor=m,
            node_id=tree.shelf2.pk,
            code=code,
            name="",
            slot=1,
            container_type_id=tree.grid.pk,
        )

    first, second = race(Hold(monkeypatch), create("BOX-A"), create("BOX-B"))
    assert isinstance(first, Box)
    assert codes(second) == {"occupied"}
    assert not Box.objects.filter(code="BOX-B").exists()
    assert not Position.objects.filter(box__code="BOX-B").exists()


def test_a_move_after_a_concurrent_change_is_stale(tree, people, monkeypatch):
    t = people["technician"]
    item = new_item(t, ago(60))
    seen_gate = gate_revision()
    seen_box = Box.objects.get(pk=tree.box.pk).revision

    def move_box():
        return services.move_container(
            actor=t,
            kind="box",
            object_id=tree.box.pk,
            expected_revision=seen_box,
            expected_gate_revision=seen_gate,
            target_kind="node",
            target_id=tree.rack.pk,
            target_slot=1,
            occurred_at=ago(5),
            reason="Move to freezer",
        )

    first, second = race(
        Hold(monkeypatch), lambda: place(t, item, cell(tree.box, "A1"), ago(10)), move_box
    )
    assert isinstance(first, MoveEvent)
    # The form was opened before the placement: the box's contents changed underneath it.
    assert codes(second) == {"stale_revision"}
    assert Box.objects.get(pk=tree.box.pk).node_id == tree.shelf1.pk


def test_a_failed_audit_rolls_back_the_whole_operation(tree, people, monkeypatch):
    before = (Box.objects.count(), Position.objects.count(), StorageGate.objects.get().revision)

    def broken(**kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(services, "record_event", broken)
    with pytest.raises(RuntimeError):
        services.create_box(
            actor=people["manager"],
            node_id=tree.shelf2.pk,
            code="BOX-LOST",
            name="",
            slot=1,
            container_type_id=tree.grid.pk,
        )
    after = (Box.objects.count(), Position.objects.count(), StorageGate.objects.get().revision)
    assert after == before


@pytest.mark.parametrize("cap", [2, 1], ids=["exact-limit", "over-limit"])
def test_storage_export_uses_the_downloaded_rows_for_the_limit_and_audit(
    client, people, monkeypatch, cap
):
    def add_site(code):
        return services.create_site(actor=people["manager"], code=code, name=code, timezone="UTC")

    add_site("EXAMPLE-A")
    client.force_login(people["viewer"])
    monkeypatch.setattr("storage.exports.EXPORT_CAP", cap)
    outcomes = {}

    def add_site_before_row_read(execute, sql, params, many, context):
        # Commit the second site after any preliminary count, just before PostgreSQL reads
        # the ordered rows. This interleaving needs neither sleeps nor a mocked row count.
        if 'FROM "storage_site"' in sql and "ORDER BY" in sql and not outcomes:
            worker = run("writer", lambda: add_site("EXAMPLE-B"), outcomes, {})
            worker.join(15)
            assert isinstance(outcomes.get("writer"), Site), outcomes
        return execute(sql, params, many, context)

    with connection.execute_wrapper(add_site_before_row_read):
        response = client.get(reverse("storage:export", args=["site"]))
    assert "writer" in outcomes
    audits = AuditEvent.objects.filter(action="storage.exported", object_id="site")
    if cap == 1:
        assert response.status_code == 400
        assert "Content-Disposition" not in response
        assert not audits.exists()
    else:
        assert response.status_code == 200
        rows = list(csv.reader(io.StringIO(response.content.decode())))[1:]
        assert [row[0] for row in rows] == ["EXAMPLE-A", "EXAMPLE-B"]
        assert audits.get().after["rows"] == len(rows)
