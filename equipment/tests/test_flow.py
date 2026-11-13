"""One pass through the equipment services: register, log, import, excursion, due dates."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from equipment import dashboard, queries, services
from equipment.models import AffectedSpecimen, Excursion, TemperatureReading

from .conftest import ago, csv_file, place, register


def test_equipment_flow(people, world, tank, monitor, dewar):
    tech, manager, admin = people["technician"], people["manager"], people["admin"]
    specimen = register(tech, world, occurred_at=ago(270))
    place(tech, specimen, world.position("A1"), occurred_at=ago(260))

    services.record_ln2(
        actor=tech, equipment_id=tank.pk, kind="level", occurred_at=ago(240), level=Decimal("31.5")
    )
    services.record_ln2(
        actor=tech,
        equipment_id=tank.pk,
        kind="fill",
        occurred_at=ago(230),
        level_before=Decimal("18.0"),
        level=Decimal("42.0"),
        amount_added=Decimal("120"),
        amount_unit="L",
        supply=dewar,
    )
    log = list(queries.ln2_log(tank.ln2_entries.all()).order_by("occurred_at"))
    assert queries.level_change(log[1]) == Decimal("-13.50")

    services.record_reading(
        actor=tech,
        equipment_id=tank.pk,
        measured_at=ago(220),
        value=Decimal("-185.10"),
        probe_position="Top of rack",
        device=monitor,
    )
    batch = services.validate_reading_import(
        actor=tech,
        file=csv_file(
            "measured_at,temperature_c,probe_position\n"
            f"{timezone.localtime(ago(210)).isoformat()},-182.00,Top of rack\n"
            f"{timezone.localtime(ago(200)).isoformat()},-140.25,Top of rack\n"
        ),
        equipment_id=tank.pk,
    )
    assert batch.is_valid, batch.errors
    services.commit_reading_import(actor=tech, batch_id=batch.pk, reason="Logger download")
    assert TemperatureReading.objects.filter(batch=batch).count() == 2
    assert queries.uncovered_readings().count() == 1
    with pytest.raises(ValidationError):
        services.commit_reading_import(actor=tech, batch_id=batch.pk, reason="Again")

    excursion = services.open_excursion(
        actor=tech,
        equipment_id=tank.pk,
        started_at=ago(205),
        detection="reading",
        extreme_value=Decimal("-140.25"),
    )
    assert queries.uncovered_readings().count() == 0
    services.update_excursion(
        actor=tech,
        excursion_id=excursion.pk,
        expected_revision=1,
        ended_at=ago(190),
        cause="Lid left ajar",
        action="Lid closed, level checked",
        impact="Top boxes exposed",
        cause_category="door",
    )
    services.submit_excursion(actor=manager, excursion_id=excursion.pk, expected_revision=2)
    with pytest.raises(ValidationError):
        services.close_excursion(
            actor=manager, excursion_id=excursion.pk, expected_revision=3, note="Reviewed"
        )
    services.close_excursion(
        actor=admin, excursion_id=excursion.pk, expected_revision=3, note="Reviewed"
    )
    excursion.refresh_from_db()
    assert excursion.status == Excursion.Status.CLOSED
    assert list(AffectedSpecimen.objects.values_list("specimen_id", flat=True)) == [specimen.pk]
    rows, capture = queries.excursion_specimens(excursion, actor=people["viewer"])
    assert capture is not None and rows[0].accession == specimen.accession

    today = timezone.localdate()
    services.record_service_event(
        actor=tech,
        equipment_id=monitor.pk,
        kind="calibration",
        performed_on=today - timedelta(400),
        outcome="acceptable",
        performed_by_external="Example Calibration Lab",
        next_due_on=today - timedelta(35),
    )
    newer = services.record_service_event(
        actor=tech,
        equipment_id=monitor.pk,
        kind="calibration",
        performed_on=today - timedelta(30),
        outcome="acceptable",
        performed_by=tech,
        next_due_on=today + timedelta(10),
    )
    assert list(queries.due_queryset()) == [newer]
    assert queries.due_queryset(state="overdue").count() == 0
    services.correct_service_event(
        actor=manager, event_id=newer.pk, reason="Wrong instrument", void=True
    )
    assert queries.due_queryset(state="overdue").count() == 1
    labels = {q["label"]: q["count"] for q in dashboard.queues(manager)}
    assert labels["Maintenance and calibration overdue"] == 1
