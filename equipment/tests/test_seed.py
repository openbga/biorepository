"""The equipment demo seed runs through the services after the storage and specimen seeds, once."""

import pytest
from django.core.management import call_command

from accounts.models import User
from equipment import queries
from equipment.models import AffectedSpecimen, Equipment, Excursion, TemperatureReading
from equipment.seed import seed_equipment

pytestmark = pytest.mark.django_db


def test_equipment_seed_after_the_demo_seed_is_complete_and_idempotent(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-demo-password-1")
    call_command("seed_demo", stdout=None)
    manager = User.objects.get(username="demo_manager")
    technician = User.objects.get(username="demo_technician")
    seed_equipment(manager=manager, technician=technician)
    counts = (Equipment.objects.count(), TemperatureReading.objects.count())
    seed_equipment(manager=manager, technician=technician)
    assert (Equipment.objects.count(), TemperatureReading.objects.count()) == counts
    assert counts[0] == 4

    closed = Excursion.objects.get(status="closed")
    assert closed.closed_by == manager and closed.submitted_by == technician
    assert AffectedSpecimen.objects.filter(excursion=closed).exists()  # the stored demo specimens
    assert Excursion.objects.filter(status="open").count() == 1
    assert queries.uncovered_readings().count() == 0
    assert queries.due_queryset(state="overdue").count() == 1
    assert queries.due_queryset(state="upcoming", within=30).count() == 1
    assert queries.low_level_equipment().count() == 0  # the fill brought VP-01 above its low level


def test_full_equipment_seed_adds_a_believable_history_once(monkeypatch):
    """`seed_demo --full` runs seed_full_equipment (core/management/commands/seed_demo.py); it adds
    the history on the first run and nothing afterwards, called again directly or by the command."""
    from core.management.commands import seed_demo
    from equipment.models import LN2Entry, ServiceEvent, StatusChange
    from equipment.seed import seed_full_equipment

    results = []

    def spy(**kwargs):
        results.append(seed_full_equipment(**kwargs))
        return results[-1]

    monkeypatch.setattr(seed_demo, "seed_full_equipment", spy)
    monkeypatch.setenv("DEMO_PASSWORD", "synthetic-demo-password-1")
    call_command("seed_demo", full=True, stdout=None)
    assert results == [True]  # the command's call added the equipment history

    def counts():
        return [m.objects.count() for m in (Equipment, ServiceEvent, LN2Entry, TemperatureReading)]

    seeded = counts()
    users = {u.username: u for u in User.objects.filter(username__startswith="demo_")}
    people = {
        "manager": users["demo_manager"],
        "technician": users["demo_technician"],
        "admin": users["demo_admin"],
    }
    assert seed_full_equipment(**people) is False
    call_command("seed_demo", full=True, stdout=None)
    assert results == [True, False]
    assert counts() == seeded

    assert seeded[0] == 11 and seeded[3] > 150
    assert StatusChange.objects.filter(equipment__code="FRZ-MF-03").count() == 2
    assert Excursion.objects.filter(status="closed").count() == 2
    assert Excursion.objects.filter(status="open").count() == 2
    assert queries.uncovered_readings().count() == 1  # RF-01's 9.10 °C, left for the queue
    # MON-02 overdue; MON-01, overdue in the basic demo, was recalibrated ten days ago.
    assert [e.equipment.code for e in queries.due_queryset(state="overdue")] == ["MON-02"]
    assert queries.low_level_equipment().count() == 0
