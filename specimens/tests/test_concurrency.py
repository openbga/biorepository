"""Specimen services under real concurrency: two threads, each with its own connection.

The thread named "first" is held inside its transaction (after its writes and audit event, before
commit) while "second" runs; PostgreSQL's `pg_blocking_pids` confirms "second" waits on the lock
the first one holds (the StorageGate), then "first" commits and "second" revalidates.
"""

import threading
import time

import pytest
from django.db import connection

from specimens import services
from specimens.models import DisposalDecision, Specimen
from storage.models import Occupancy

from .conftest import (
    ago,
    ask_disposal,
    codes,
    fresh,
    make_user,
    occ_rev,
    register,
    stored,
)

pytestmark = pytest.mark.django_db(transaction=True)


class HoldFirst:
    """Hold the thread named "first" in specimens.services.record_event until released."""

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
    """Run `first` to the hold point, start `second`, prove it waits, then let both finish."""
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


def test_two_specimens_cannot_be_placed_into_the_same_free_position(people, world, monkeypatch):
    t, m = people["technician"], people["manager"]
    one, two = register(t, world), register(m, world)
    target = world.position("C2")
    seen = {s.pk: (s.revision, occ_rev(s)) for s in (one, two)}

    def place(actor, specimen):
        def call():
            revision, occupancy = seen[specimen.pk]
            return services.place_specimen(
                actor=actor,
                specimen_id=specimen.pk,
                expected_revision=revision,
                expected_occupancy_revision=occupancy,
                position_id=target.pk,
                occurred_at=ago(100),
                reason="Stored after processing",
            )

        return call

    first, second = race(HoldFirst(monkeypatch), place(t, one), place(m, two))
    assert isinstance(first, Specimen)
    assert codes(second) == {"occupied"}  # the second one waited, then found the position taken
    assert Occupancy.objects.get(position=target).pk == one.occupancy_id
    assert fresh(two).occupancy.position_id is None
    assert fresh(two).custody.filter(kind="place").count() == 0


def test_two_concurrent_approvals_leave_one_decision(people, world, monkeypatch):
    t = people["technician"]
    second_approver = make_user("sp-approver-2", "technician", approver=True)
    specimen = stored(t, world)
    request = ask_disposal(t, specimen)
    revision = fresh(specimen).revision

    def approve(actor):
        return lambda: services.decide_disposal(
            actor=actor,
            request_id=request.pk,
            expected_revision=revision,
            decision="approve",
            reason="Checked against the closure notice",
        )

    first, second = race(
        HoldFirst(monkeypatch), approve(people["approver"]), approve(second_approver)
    )
    assert isinstance(first, DisposalDecision)
    assert codes(second) <= {"stale_revision", "invalid_transition"}
    assert DisposalDecision.objects.filter(request=request).count() == 1
    assert DisposalDecision.objects.get(request=request).actor_id == people["approver"].pk


def test_concurrent_registrations_get_distinct_accessions(people, world, monkeypatch):
    t, m = people["technician"], people["manager"]
    first, second = race(
        HoldFirst(monkeypatch), lambda: register(t, world), lambda: register(m, world)
    )
    assert isinstance(first, Specimen) and isinstance(second, Specimen)
    assert first.accession != second.accession
    assert Specimen.objects.count() == 2
    assert len({s.occupancy_id for s in Specimen.objects.all()}) == 2


def test_a_failed_concurrent_transaction_leaves_nothing_behind(people, world, monkeypatch):
    t = people["technician"]
    target = world.position("C2")
    first, second = race(
        HoldFirst(monkeypatch),
        lambda: register(t, world, position=target),
        lambda: register(t, world, position=target),
    )
    assert isinstance(first, Specimen) and codes(second) == {"occupied"}
    assert Specimen.objects.count() == 1  # the loser registered nothing at all
