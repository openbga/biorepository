"""Equipment services under real concurrency: two threads, each with its own connection.

The thread named "first" is held after its writes and audit event, before commit, while "second"
runs; `pg_blocking_pids` proves "second" waits on the equipment row lock, then revalidates.
"""

import threading
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from equipment import services
from equipment.models import (
    AffectedSpecimen,
    Excursion,
    ExcursionEvent,
    LN2Entry,
    ServiceEvent,
    TemperatureReading,
)
from specimens.tests.test_concurrency import race

from .conftest import ago, make_user

pytestmark = pytest.mark.django_db(transaction=True)


class HoldFirst:
    """Hold the thread named "first" in equipment.services.record_event until released."""

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


def codes(outcome):
    assert isinstance(outcome, ValidationError), outcome
    if hasattr(outcome, "error_dict"):
        return {e.code for errors in outcome.error_dict.values() for e in errors}
    return {error.code for error in outcome.error_list}


def test_two_people_recording_the_same_reading_leave_one(people, tank, monkeypatch):
    at = ago(30)

    def record(actor):
        return lambda: services.record_reading(
            actor=actor,
            equipment_id=tank.pk,
            measured_at=at,
            value=Decimal("-180.00"),
            probe_position="Top of rack",
        )

    first, second = race(
        HoldFirst(monkeypatch), record(people["technician"]), record(people["technician2"])
    )
    assert isinstance(first, TemperatureReading)
    assert codes(second) == {"unique"}
    assert TemperatureReading.objects.filter(equipment=tank).count() == 1


def test_two_reviewers_closing_one_excursion_leave_one_close(people, world, tank, monkeypatch):
    excursion = services.open_excursion(
        actor=people["technician"],
        equipment_id=tank.pk,
        started_at=ago(50),
        ended_at=ago(40),
        detection="alarm",
        cause="Fill valve stuck",
        action="Valve freed, tank refilled",
        impact="No boxes above the warm zone",
    )
    services.submit_excursion(
        actor=people["technician"], excursion_id=excursion.pk, expected_revision=1
    )
    second_manager = make_user("eq-manager-2", "manager")

    def close(actor):
        return lambda: services.close_excursion(
            actor=actor, excursion_id=excursion.pk, expected_revision=2, note="Reviewed"
        )

    first, second = race(HoldFirst(monkeypatch), close(people["manager"]), close(second_manager))
    assert isinstance(first, Excursion)
    assert codes(second) == {"stale_revision"}
    assert ExcursionEvent.objects.filter(excursion=excursion, kind="closed").count() == 1
    assert AffectedSpecimen.objects.filter(excursion=excursion).count() == 0


def test_two_corrections_of_one_entry_leave_one(people, tank, monkeypatch):
    entry = services.record_ln2(
        actor=people["technician"],
        equipment_id=tank.pk,
        kind="level",
        occurred_at=ago(20),
        level=Decimal("30.0"),
    )

    def correct(actor, level):
        return lambda: services.correct_ln2(
            actor=actor, entry_id=entry.pk, reason="Misread the dipstick", level=Decimal(level)
        )

    first, second = race(
        HoldFirst(monkeypatch), correct(people["manager"], "31.0"), correct(people["admin"], "32.0")
    )
    assert isinstance(first, LN2Entry)
    assert codes(second) == {"invalid_transition"}
    assert LN2Entry.objects.filter(corrects=entry).count() == 1
    assert ServiceEvent.objects.count() == 0
