"""Synthetic demo equipment, created through the services and idempotent by asset ID.

Runs after `storage.seed.seed_storage` and `specimens.seed.seed_specimens`. Registers Tank VP-01
and Freezer MF-01 as the enclosures of the demo storage units, a probe logger and a supply dewar;
records three days of LN2 level checks and fills, temperature readings (entered and imported from
a CSV), a calibration that is overdue and maintenance due soon, and two excursions: one closed by
the manager after the technician submitted it, which captures the demo specimens in VP-01, and one
still open on the freezer.

The limits and the low level below are demonstration values typed in by the demo repository so the
pages have something to compare against. They are not recommendations; Vapor Phase has no default
limits.

`seed_full_equipment` adds the equipment of the larger demonstration (`seed_demo --full`): the
enclosures of VP-02, VP-03, MF-02, MF-03 and RF-01, a second monitor and dewar, and about three
months of history.
"""

import io
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from storage.models import StorageUnit

from . import services
from .models import Equipment

TANK, FREEZER, MONITOR, DEWAR = "TANK-VP-01", "FRZ-MF-01", "MON-01", "DEWAR-01"


def _csv(text, name):
    data = io.BytesIO(text.encode("utf-8"))
    data.name, data.size = name, len(text.encode("utf-8"))
    return data


@transaction.atomic
def seed_equipment(*, manager, technician):
    """Create the demo equipment and its records once; return the register entries by code.

    Idempotent: recognised by TANK-VP-01 and skipped afterwards; one transaction, so a failure
    leaves nothing half-made for the next run to skip."""
    if Equipment.objects.filter(code=TANK).exists():
        return {
            e.code: e for e in Equipment.objects.filter(code__in=[TANK, FREEZER, MONITOR, DEWAR])
        }
    now = timezone.now()
    start = now - timedelta(days=3)

    def at(hours):
        return start + timedelta(hours=hours)

    vp01 = StorageUnit.objects.filter(code="VP-01").first()
    mf01 = StorageUnit.objects.filter(code="MF-01").first()
    tank = services.save_equipment(
        actor=manager,
        code=TANK,
        name="Tank VP-01",
        kind="ln2_tank",
        storage_unit=vp01,
        manufacturer="Example Cryo Works",
        model="VX-500",
        serial_number="SN-0000-0001",
        is_critical=True,
        installed_on=(now - timedelta(days=900)).date(),
        service_provider="Example Service Co.",
        upper_limit=Decimal("-150.00"),
        level_unit="cm",
        level_low_limit=Decimal("15.00"),
    )
    freezer = services.save_equipment(
        actor=manager,
        code=FREEZER,
        name="Freezer MF-01",
        kind="freezer",
        storage_unit=mf01,
        manufacturer="Example Cold Systems",
        model="UL-80",
        serial_number="SN-0000-0002",
        is_critical=True,
        upper_limit=Decimal("-70.00"),
    )
    monitor = services.save_equipment(
        actor=manager,
        code=MONITOR,
        name="Probe logger 1",
        kind="monitor",
        location="Tank VP-01, top of rack",
        manufacturer="Example Instruments",
        serial_number="SN-0000-0003",
    )
    dewar = services.save_equipment(
        actor=manager,
        code=DEWAR,
        name="Supply dewar 1",
        kind="supply_dewar",
        location="Loading bay",
        level_unit="%",
    )

    # LN2 log: a daily level check and one fill from the supply dewar.
    for day, level in ((0, "28.0"), (1, "22.5"), (2, "17.0")):
        services.record_ln2(
            actor=technician,
            equipment_id=tank.pk,
            kind="level",
            occurred_at=at(day * 24 + 8),
            method="dipstick",
            level=Decimal(level),
        )
    services.record_ln2(
        actor=technician,
        equipment_id=tank.pk,
        kind="fill",
        occurred_at=at(56),
        method="dipstick",
        level_before=Decimal("16.5"),
        level=Decimal("38.0"),
        amount_added=Decimal("95"),
        amount_unit="L",
        supply=dewar,
        reference="DN-000042",
    )

    # Readings: entered by hand, and a logger export imported from CSV.
    for hours, value in ((2, "-188.40"), (26, "-187.90"), (50, "-186.75")):
        services.record_reading(
            actor=technician,
            equipment_id=tank.pk,
            measured_at=at(hours),
            value=Decimal(value),
            probe_position="Top of rack",
            device=monitor,
        )
    rows = ["measured_at,temperature_c,probe_position,device"]
    for hours, value in ((40, "-185.10"), (41, "-149.20"), (41.5, "-142.80"), (42, "-170.30")):
        rows.append(f"{timezone.localtime(at(hours)).isoformat()},{value},Top of rack,{MONITOR}")
    batch = services.validate_reading_import(
        actor=technician, file=_csv("\n".join(rows) + "\n", "vp01-logger.csv"), equipment_id=tank.pk
    )
    services.commit_reading_import(
        actor=technician, batch_id=batch.pk, reason="Weekly logger download"
    )
    services.record_reading(
        actor=technician,
        equipment_id=freezer.pk,
        measured_at=at(60),
        value=Decimal("-64.50"),
        probe_position="Display",
    )

    # Service events: an overdue calibration of the logger and tank maintenance due soon.
    today = timezone.localdate()
    services.record_service_event(
        actor=technician,
        equipment_id=monitor.pk,
        kind="calibration",
        performed_on=today - timedelta(days=380),
        outcome="acceptable",
        performed_by_external="Example Calibration Lab",
        reference="CERT-2025-0117",
        reference_device="Reference thermometer RT-9, SN-0000-0099",
        as_found="-80.10 °C at -80.00 °C reference",
        as_left="-80.02 °C at -80.00 °C reference",
        next_due_on=today - timedelta(days=15),
        attachment=_csv(
            "Synthetic demonstration certificate. Not a real calibration.\n", "cert.txt"
        ),
    )
    services.record_service_event(
        actor=technician,
        equipment_id=tank.pk,
        kind="maintenance",
        performed_on=today - timedelta(days=170),
        outcome="completed",
        performed_by_external="Example Service Co.",
        description="Lid gasket inspected, level sensor checked against the dipstick.",
        next_due_on=today + timedelta(days=12),
    )

    # Excursions: the warm readings at hours 41-42, closed by a second person, and an open one.
    excursion = services.open_excursion(
        actor=technician,
        equipment_id=tank.pk,
        started_at=at(40.75),
        ended_at=at(41.75),
        extreme_value=Decimal("-142.80"),
        extreme_at=at(41.5),
        detection="reading",
        cause_category="door",
        cause="Lid left open during a box retrieval.",
        action="Lid closed; level checked; readings watched until back below the limit.",
        impact="Top shelves above the limit for about an hour; specimens listed for QC review.",
    )
    services.submit_excursion(
        actor=technician, excursion_id=excursion.pk, expected_revision=excursion.revision
    )
    excursion.refresh_from_db()
    services.close_excursion(
        actor=manager,
        excursion_id=excursion.pk,
        expected_revision=excursion.revision,
        note="Reviewed with the readings; no further action beyond the QC review.",
    )
    services.open_excursion(
        actor=technician,
        equipment_id=freezer.pk,
        started_at=at(59.5),
        extreme_value=Decimal("-64.50"),
        extreme_at=at(60),
        detection="reading",
        note="Display reading above the limit; door seal being checked.",
    )
    return {e.code: e for e in (tank, freezer, monitor, dewar)}


# ------------------------------------------------------------------ the full demonstration

FULL_MARKER = "TANK-VP-02"
# asset ID, name, kind, storage unit code, lower, upper, level unit, low level (demo values)
FULL_EQUIPMENT = (
    ("TANK-VP-02", "Tank VP-02", "ln2_tank", "VP-02", None, "-150.00", "cm", "15.00"),
    (
        "TANK-VP-03",
        "Tank VP-03 (liquid phase)",
        "ln2_tank",
        "VP-03",
        None,
        "-180.00",
        "cm",
        "20.00",
    ),
    ("FRZ-MF-02", "Freezer MF-02", "freezer", "MF-02", None, "-70.00", "", None),
    ("FRZ-MF-03", "Freezer MF-03", "freezer", "MF-03", None, "-70.00", "", None),
    ("FRG-RF-01", "Refrigerator RF-01", "freezer", "RF-01", "2.00", "8.00", "", None),
)
# Daily 08:00 readings, demo baselines in °C with a small repeating variation.
FULL_BASELINES = {
    "TANK-VP-02": Decimal("-186.00"),
    "TANK-VP-03": Decimal("-193.00"),
    "FRZ-MF-02": Decimal("-79.50"),
    "FRZ-MF-03": Decimal("-80.20"),
    "FRG-RF-01": Decimal("4.20"),
}
WOBBLE = (Decimal("0.00"), Decimal("0.40"), Decimal("-0.30"), Decimal("0.70"), Decimal("-0.50"))


@transaction.atomic
def seed_full_equipment(*, manager, technician, admin):
    """Add the full demonstration's equipment and about three months of its history, once.

    Runs after `storage.seed.seed_full_storage` (units VP-02, VP-03, MF-02, MF-03, RF-01) and the
    full specimen story; calls `seed_equipment` first. Idempotent: recognised by TANK-VP-02. One
    transaction. Returns True when it added the data."""
    seed_equipment(manager=manager, technician=technician)
    if Equipment.objects.filter(code=FULL_MARKER).exists():
        return False
    now = timezone.now()
    local_now = timezone.localtime(now)
    today = local_now.date()

    def morning(days_ago):
        # Subtract calendar days locally so DST cannot repeat or skip a morning.
        local = local_now - timedelta(days=days_ago)
        return local.replace(hour=8, minute=0, second=0, microsecond=0)

    units = {u.code: u for u in StorageUnit.objects.filter(code__in=[e[3] for e in FULL_EQUIPMENT])}
    gear = {}
    for code, name, kind, unit, lower, upper, level_unit, low in FULL_EQUIPMENT:
        gear[code] = services.save_equipment(
            actor=manager,
            code=code,
            name=name,
            kind=kind,
            storage_unit=units.get(unit),
            is_critical=kind == "ln2_tank",
            manufacturer="Example Cryo Works" if kind == "ln2_tank" else "Example Cold Systems",
            serial_number=f"SN-0000-01{len(gear):02d}",
            installed_on=today - timedelta(days=700 + 30 * len(gear)),
            service_provider="Example Service Co.",
            lower_limit=Decimal(lower) if lower else None,
            upper_limit=Decimal(upper),
            level_unit=level_unit,
            level_low_limit=Decimal(low) if low else None,
        )
    monitor = services.save_equipment(
        actor=manager,
        code="MON-02",
        name="Probe logger 2",
        kind="monitor",
        location="Freezer room, MF-02",
        manufacturer="Example Instruments",
        serial_number="SN-0000-0201",
    )
    dewar = services.save_equipment(
        actor=manager,
        code="DEWAR-02",
        name="Supply dewar 2",
        kind="supply_dewar",
        location="Loading bay",
        level_unit="%",
    )
    first_monitor = Equipment.objects.get(code=MONITOR)

    # Service history: quarterly maintenance with the next one due, yearly calibrations (MON-02's
    # is overdue), a compressor repair on MF-03 with the freezer out of service meanwhile.
    for code in ("TANK-VP-02", "TANK-VP-03", "FRZ-MF-02", "FRZ-MF-03"):
        for days_ago, due in ((95, 0), (5, 85)):
            services.record_service_event(
                actor=technician,
                equipment_id=gear[code].pk,
                kind="maintenance",
                performed_on=today - timedelta(days=days_ago),
                outcome="completed",
                performed_by_external="Example Service Co.",
                description="Seals, alarm contacts and display checked; condenser cleaned "
                "where fitted.",
                next_due_on=today + timedelta(days=due) if due else today - timedelta(days=5),
            )
    services.record_service_event(
        actor=technician,
        equipment_id=monitor.pk,
        kind="calibration",
        performed_on=today - timedelta(days=400),
        outcome="acceptable",
        performed_by_external="Example Calibration Lab",
        reference="CERT-2025-0233",
        reference_device="Reference thermometer RT-9, SN-0000-0099",
        as_found="-80.30 °C at -80.00 °C reference",
        as_left="-80.05 °C at -80.00 °C reference",
        next_due_on=today - timedelta(days=35),
    )
    services.record_service_event(
        actor=technician,
        equipment_id=first_monitor.pk,
        kind="calibration",
        performed_on=today - timedelta(days=10),
        outcome="acceptable",
        performed_by=technician,
        reference="SOP-EQ-004 v2",
        reference_device="Reference thermometer RT-9, SN-0000-0099",
        as_found="-79.70 °C at -80.00 °C reference",
        as_left="-79.98 °C at -80.00 °C reference",
        next_due_on=today + timedelta(days=355),
    )
    services.record_service_event(
        actor=technician,
        equipment_id=gear["TANK-VP-02"].pk,
        kind="mapping",
        performed_on=today - timedelta(days=60),
        outcome="acceptable",
        performed_by=technician,
        description="Probes at top and bottom of each tower; top of tower 2 warmest.",
        next_due_on=today + timedelta(days=305),
    )
    mf03 = gear["FRZ-MF-03"]
    services.change_status(
        actor=manager,
        equipment_id=mf03.pk,
        expected_revision=mf03.revision,
        status="out_of_service",
        occurred_at=now - timedelta(days=41),
        reason="Compressor cycling; contents moved to MF-02 while it is repaired.",
    )
    services.record_service_event(
        actor=technician,
        equipment_id=mf03.pk,
        kind="repair",
        performed_on=today - timedelta(days=39),
        outcome="completed",
        performed_by_external="Example Service Co.",
        reference="WO-000318",
        description="Start relay replaced; pull-down to set point verified over 24 hours.",
    )
    mf03.refresh_from_db()
    services.change_status(
        actor=manager,
        equipment_id=mf03.pk,
        expected_revision=mf03.revision,
        status="in_service",
        occurred_at=now - timedelta(days=38),
        reason="Repaired and verified; back in service.",
    )

    # LN2 log: weekly level checks for 12 weeks, a fill whenever the level drops near the low level.
    for code, start_level, use in (("TANK-VP-02", "38.0", "6.5"), ("TANK-VP-03", "42.0", "5.0")):
        level = Decimal(start_level)
        for week in range(12, 0, -1):
            when = morning(week * 7)
            services.record_ln2(
                actor=technician,
                equipment_id=gear[code].pk,
                kind="level",
                occurred_at=when,
                method="dipstick",
                level=level,
            )
            after = level - Decimal(use)
            if after < Decimal("24"):
                services.record_ln2(
                    actor=technician,
                    equipment_id=gear[code].pk,
                    kind="fill",
                    occurred_at=when + timedelta(hours=2),
                    method="dipstick",
                    level_before=level,
                    level=Decimal(start_level),
                    amount_added=Decimal("110"),
                    amount_unit="L",
                    supply=dewar,
                    reference=f"DN-{1000 + week:06d}",
                )
                after = Decimal(start_level) - Decimal(use)
            level = after
    misread = services.record_ln2(
        actor=technician,
        equipment_id=gear["TANK-VP-03"].pk,
        kind="level",
        occurred_at=morning(1),
        method="dipstick",
        level=Decimal("3.20"),
    )
    services.correct_ln2(
        actor=technician,
        entry_id=misread.pk,
        reason="Read the dipstick upside down; actual level 32.0 cm.",
        level=Decimal("32.0"),
    )

    # Readings: 30 days of 08:00 logger values, imported; MF-02's warm afternoon 12 days ago.
    rows = ["equipment,measured_at,temperature_c,probe_position,device"]
    for code, base in FULL_BASELINES.items():
        for day in range(30, 0, -1):
            value = base + WOBBLE[day % len(WOBBLE)]
            probe = "Top of rack" if code.startswith("TANK") else "Display"
            rows.append(f"{code},{morning(day).isoformat()},{value},{probe},")
    warm = morning(12) + timedelta(hours=6)
    for minutes, value in ((0, "-66.40"), (40, "-61.80"), (90, "-74.90")):
        rows.append(
            f"FRZ-MF-02,{(warm + timedelta(minutes=minutes)).isoformat()},{value},Display,MON-02"
        )
    batch = services.validate_reading_import(
        actor=technician, file=_csv("\n".join(rows) + "\n", "monthly-logger-export.csv")
    )
    services.commit_reading_import(
        actor=technician, batch_id=batch.pk, reason="Monthly logger export"
    )
    typo = services.record_reading(
        actor=technician,
        equipment_id=gear["FRG-RF-01"].pk,
        measured_at=now - timedelta(hours=30),
        value=Decimal("-4.10"),
        probe_position="Display",
    )
    services.correct_reading(
        actor=technician, reading_id=typo.pk, reason="Sign typed wrongly.", value=Decimal("4.10")
    )
    # Left without an excursion so the dashboard queue shows one reading to look at.
    services.record_reading(
        actor=technician,
        equipment_id=gear["FRG-RF-01"].pk,
        measured_at=now - timedelta(hours=20),
        value=Decimal("9.10"),
        probe_position="Display",
        note="Door found ajar at the morning round.",
    )

    # Excursions: MF-02's warm afternoon, reviewed and closed; MF-03 warming now, open.
    closed = services.open_excursion(
        actor=technician,
        equipment_id=gear["FRZ-MF-02"].pk,
        started_at=warm - timedelta(minutes=10),
        ended_at=warm + timedelta(minutes=80),
        extreme_value=Decimal("-61.80"),
        extreme_at=warm + timedelta(minutes=40),
        detection="alarm",
        cause_category="door",
        cause="Inner door left unlatched after a box retrieval.",
        action="Door latched; temperature watched until back below the limit.",
        impact="About 90 minutes above the limit; boxes on the top shelf listed for QC review.",
    )
    services.submit_excursion(
        actor=technician, excursion_id=closed.pk, expected_revision=closed.revision
    )
    closed.refresh_from_db()
    services.close_excursion(
        actor=admin,
        excursion_id=closed.pk,
        expected_revision=closed.revision,
        note="Reviewed with the logger trace; QC review of the listed specimens requested.",
    )
    services.record_reading(
        actor=technician,
        equipment_id=mf03.pk,
        measured_at=now - timedelta(hours=2),
        value=Decimal("-66.50"),
        probe_position="Display",
        device=monitor,
    )
    services.open_excursion(
        actor=technician,
        equipment_id=mf03.pk,
        started_at=now - timedelta(hours=3),
        extreme_value=Decimal("-66.50"),
        extreme_at=now - timedelta(hours=2),
        detection="reading",
        cause_category="equipment",
        note="Warming since this morning; service company called.",
    )
    return True
