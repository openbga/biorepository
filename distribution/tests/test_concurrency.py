"""Distribution services under real concurrency: two threads, each with its own connection.

The thread named "first" is held after its writes and audit event, before commit, while
"second" runs; `pg_blocking_pids` (in `race`) proves "second" waits on the StorageGate that
"first" holds, then "second" revalidates after "first" commits. All data is synthetic.
"""

import csv
import io
import threading
from functools import partial

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.urls import reverse

from core.models import AuditEvent
from distribution import services
from distribution.models import (
    Allocation,
    ConsentWithdrawal,
    ConsentWithdrawalEscalation,
    OutputRecord,
)
from specimens.models import Hold
from specimens.tests.test_concurrency import race, run

from .helpers import (
    approved_request,
    codes,
    fast_password_hashing,  # noqa: F401
    fresh,
    new_request,
    save,
    timezone,
)
from .test_services_consent import withdraw

pytestmark = pytest.mark.django_db(transaction=True)


class HoldFirst:
    """Hold the thread named "first" in distribution.services.record_event until released."""

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


def allocation_call(people, request, specimen):
    item = request.items.get()
    revision = fresh(specimen).revision

    def call():
        return services.allocate(
            actor=people["manager"],
            request_id=request.pk,
            expected_revision=request.revision,
            item_id=item.pk,
            choices=(services.Choice(specimen.pk, revision),),
            below_minimum_ok=False,
            occurred_at=timezone.now(),
            reason="Allocated for the approved request",
        )

    return call


def test_two_allocations_of_one_specimen_to_two_requests_leave_exactly_one(
    people, dist, world, monkeypatch
):
    first_request = approved_request(people, dist, world)
    second_request = approved_request(people, dist, world)
    specimen = dist.specimens[0]
    first, second = race(
        HoldFirst(monkeypatch),
        allocation_call(people, first_request, specimen),
        allocation_call(people, second_request, specimen),
    )
    assert isinstance(first, tuple) and len(first) == 1
    # The loser revalidates after the winner committed: the specimen changed (stale revision)
    # or the gate reports it allocated elsewhere. Either way nothing was written.
    assert isinstance(second, ValidationError), second
    assert codes(second) & {"stale_revision", "blocked"}
    rows = Allocation.objects.filter(specimen=specimen)
    assert rows.count() == 1 and rows.get().request_id == first_request.pk
    assert (
        Hold.objects.filter(
            specimen=specimen, reference__startswith="Allocated to", release__isnull=True
        ).count()
        == 1
    )


def test_two_managers_escalating_the_same_withdrawal_record_one_instruction(
    people, dist, monkeypatch
):
    consent = dist.consent
    withdrawal = withdraw(people, consent)
    revision = fresh(consent).revision

    def escalate(actor):
        return services.escalate_withdrawal(
            actor=actor,
            consent_id=consent.pk,
            expected_revision=revision,
            effective_on=timezone.localdate(),
            received_via="Letter (synthetic)",
            occurred_at=timezone.now(),
            reason="Later destruction instruction (synthetic)",
        )

    first, second = race(
        HoldFirst(monkeypatch),
        lambda: escalate(people["manager"]),
        lambda: escalate(people["manager2"]),
    )
    assert isinstance(first, ConsentWithdrawalEscalation)
    assert isinstance(second, ValidationError), second
    assert codes(second) == {"stale_revision"}
    assert ConsentWithdrawalEscalation.objects.get(withdrawal=withdrawal).pk == first.pk
    assert fresh(consent).revision == revision + 1
    assert not withdrawal.holds.filter(hold__release__isnull=False).exists()


def test_the_loser_sees_the_gate_when_it_read_the_specimen_after_the_winner(
    people, dist, world, monkeypatch
):
    """The second caller's choice carries the revision that the first allocation produces, so
    only the gate (`allocated_elsewhere`) can refuse it."""
    first_request = approved_request(people, dist, world)
    second_request = approved_request(people, dist, world)
    specimen = dist.specimens[0]
    winner = allocation_call(people, first_request, specimen)
    hold = HoldFirst(monkeypatch)
    item = second_request.items.get()
    # Revision the specimen will have once the winner's hold is placed (one bump per hold).
    after = fresh(specimen).revision + 1

    def loser():
        return services.allocate(
            actor=people["manager"],
            request_id=second_request.pk,
            expected_revision=second_request.revision,
            item_id=item.pk,
            choices=(services.Choice(specimen.pk, after),),
            below_minimum_ok=False,
            occurred_at=timezone.now(),
            reason="Allocated for the approved request",
        )

    first, second = race(hold, winner, loser)
    assert isinstance(first, tuple)
    assert isinstance(second, ValidationError), second
    assert codes(second) == {"blocked"}
    assert Allocation.objects.filter(specimen=specimen).count() == 1


def test_simultaneous_allocations_without_a_hold_point_also_leave_one(people, dist, world):
    requests = [approved_request(people, dist, world) for _ in range(3)]
    specimen = dist.specimens[0]
    calls = [allocation_call(people, request, specimen) for request in requests]
    barrier = threading.Barrier(len(calls))
    outcomes = {}

    def run(index, call):
        try:
            connection.ensure_connection()
            barrier.wait(10)
            outcomes[index] = call()
        except Exception as exc:  # inspected below
            outcomes[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i, c), daemon=True) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    winners = [o for o in outcomes.values() if isinstance(o, tuple)]
    losers = [o for o in outcomes.values() if not isinstance(o, tuple)]
    assert len(winners) == 1 and len(losers) == 2, outcomes
    assert all(isinstance(o, ValidationError) for o in losers), losers
    assert (
        Allocation.objects.filter(specimen=specimen, state__in=("allocated", "picked")).count() == 1
    )


def test_a_withdrawal_and_an_allocation_cannot_interleave(people, dist, world, monkeypatch):
    """The gate serializes them: the allocation either lands before the withdrawal (and is then
    released by it) or is refused by the gate after it."""
    request = approved_request(people, dist, world)
    specimen = dist.specimens[0]
    consent = dist.consent
    consent.refresh_from_db()

    def withdraw():
        return services.withdraw_consent(
            actor=people["manager"],
            consent_id=consent.pk,
            expected_revision=consent.revision,
            kind="no_use",
            effective_on=timezone.localdate(),
            received_via="Letter (synthetic)",
            occurred_at=timezone.now(),
            reason="Participant asked to stop use",
        )

    first, second = race(
        HoldFirst(monkeypatch), withdraw, allocation_call(people, request, specimen)
    )
    assert isinstance(first, ConsentWithdrawal) and first.held_count == 3
    assert isinstance(second, ValidationError), second
    assert codes(second) & {"stale_revision", "blocked"}
    assert not Allocation.objects.exists()
    assert (
        Hold.objects.filter(
            reference="Consent withdrawn: P-000123, DEMO-STUDY", release__isnull=True
        ).count()
        == 3
    )


def test_two_deciders_of_one_request_leave_one_decision(people, dist, world, monkeypatch):
    request = services.save_request(
        actor=people["technician"],
        request_id=None,
        expected_revision=None,
        values={
            "title": "Plasma for assay development",
            "investigator": dist.investigator.pk,
            "purpose": "Method validation (synthetic)",
            "received_via": "email",
            "use_categories": [dist.categories["GENERAL"].pk],
        },
        items=(services.ItemSpec(line=1, specimen_type_id=world.plasma.pk, count=1),),
    )
    services.submit_request(
        actor=people["technician"], request_id=request.pk, expected_revision=request.revision
    )
    request.refresh_from_db()

    def decide(actor, decision):
        def call():
            return services.decide_request(
                actor=actor,
                request_id=request.pk,
                expected_revision=request.revision,
                submission_number=1,
                decision=decision,
                conditions="",
                committee_reference="Committee minute (synthetic)",
                conflicts_note="",
                reason="Checked against the consented scope",
            )

        return call

    first, second = race(
        HoldFirst(monkeypatch),
        decide(people["decider"], "approve"),
        decide(people["decider_manager"], "reject"),
    )
    assert first.decision == "approve"
    assert isinstance(second, ValidationError) and codes(second) & {
        "stale_revision",
        "invalid_transition",
    }
    request.refresh_from_db()
    assert request.status == "approved"


@pytest.mark.parametrize("cap", [1, 2], ids=["over-limit", "exact-limit"])
@pytest.mark.parametrize("kind", ["requests", "institutions"])
def test_distribution_export_uses_the_selected_rows_for_limit_and_audit(
    client, people, dist, world, monkeypatch, cap, kind
):
    if kind == "requests":
        create = partial(new_request, people["technician"], dist, world)
        first = create()
        route, register, sort, count_key = "export", "request-list", "number", "rows"
        monkeypatch.setattr("distribution.exports.MAX_ROWS", cap)
    else:
        first = dist.institution
        create = partial(
            save,
            people["manager"],
            "institution",
            code="EXZ",
            name="Example Z",
            address="100 Plaza",
        )
        route, register, sort, count_key = "catalogue-export", "institution-list", "code", "count"
        monkeypatch.setattr("distribution.governance_views.EXPORT_CAP", cap)

    client.force_login(people["viewer"])
    url = reverse(f"distribution:{route}", args=[kind])
    outcomes = {}

    def insert_before_selection(execute, sql, params, many, context):
        # Commit a matching row on another connection just before the ordered SELECT.
        if f'FROM "{first._meta.db_table}"' in sql and "ORDER BY" in sql and not outcomes:
            thread = run("writer", create, outcomes, {})
            thread.join(15)
            assert isinstance(outcomes.get("writer"), type(first)), outcomes
        return execute(sql, params, many, context)

    with connection.execute_wrapper(insert_before_selection):
        response = client.get(url, {"sort": sort})
    assert "writer" in outcomes
    records = OutputRecord.objects.filter(kind=OutputRecord.Kind.EXPORT)
    audits = AuditEvent.objects.filter(action="distribution.exported")
    if cap == 1:
        assert response.status_code == 302
        assert response.url == reverse(f"distribution:{register}", query={"sort": sort})
        assert "Content-Disposition" not in response
        assert not records.exists() and not audits.exists()
    else:
        assert response.status_code == 200
        rows = list(csv.reader(io.StringIO(response.content.decode())))[1:]
        assert [row[0] for row in rows] == [
            getattr(obj, sort) for obj in (first, outcomes["writer"])
        ]
        record = records.get()
        assert record.count == audits.get(object_id=str(record.pk)).after[count_key] == len(rows)

    empty = client.get(url, {"q": "not-a-matching-record"})
    assert empty.status_code == 200
    assert len(list(csv.reader(io.StringIO(empty.content.decode())))) == 1
    record = records.get(count=0)
    assert audits.get(object_id=str(record.pk)).after[count_key] == 0
