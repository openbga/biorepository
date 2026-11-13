"""Equipment reads: due dates, uncovered readings, low levels, the LN2 log, latest-value
annotations, review queues, the specimens of an excursion and occupancy. Dashboard URLs are not
exercised here. All data is synthetic.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from equipment import queries, services
from equipment.models import Equipment, Excursion, LN2Entry
from equipment.occupancy import affected_specimens
from specimens.tests.conftest import ask_disposal, decide, execute

from .conftest import (
    ago,
    check_in,
    check_out,
    hours_ago,
    place,
    register,
    register_equipment,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fast_password_hashing(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def today():
    return timezone.localdate()


def days(n):
    return today() + timedelta(days=n)


def service(actor, equipment, performed_on, next_due_on=None, kind="calibration", **over):
    values = {
        "kind": kind,
        "performed_on": performed_on,
        "outcome": "completed" if kind in ("maintenance", "repair") else "acceptable",
        "performed_by": actor,
        "next_due_on": next_due_on,
    }
    values.update(over)
    return services.record_service_event(actor=actor, equipment_id=equipment.pk, **values)


def retire(actor, equipment):
    equipment.refresh_from_db()
    return services.change_status(
        actor=actor,
        equipment_id=equipment.pk,
        expected_revision=equipment.revision,
        status="retired",
        occurred_at=hours_ago(1),
        reason="Decommissioned",
    )


def reading(actor, equipment, value, minutes, **over):
    values = {"measured_at": ago(minutes), "value": Decimal(value)}
    values.update(over)
    return services.record_reading(actor=actor, equipment_id=equipment.pk, **values)


def level(actor, equipment, value, minutes, **over):
    values = {"kind": "level", "occurred_at": ago(minutes), "level": Decimal(value)}
    values.update(over)
    return services.record_ln2(actor=actor, equipment_id=equipment.pk, **values)


def open_ex(actor, equipment, **over):
    values = {"started_at": ago(300), "detection": "reading"}
    values.update(over)
    return services.open_excursion(actor=actor, equipment_id=equipment.pk, **values)


DETAILS = {"cause": "Lid left ajar", "action": "Lid closed", "impact": "Top boxes exposed"}


def in_review(actor, equipment, **over):
    ex = open_ex(actor, equipment, ended_at=ago(250), **DETAILS, **over)
    return services.submit_excursion(actor=actor, excursion_id=ex.pk, expected_revision=ex.revision)


def close(actor, ex):
    ex.refresh_from_db()
    return services.close_excursion(
        actor=actor, excursion_id=ex.pk, expected_revision=ex.revision, note="Reviewed"
    )


# ------------------------------------------------------------------ due dates


class TestDue:
    def test_latest_event_per_equipment_and_kind_wins(self, people, tank, monitor):
        t = people["technician"]
        service(t, tank, days(-400), days(-35))
        newer = service(t, tank, days(-30), days(10))
        other_kind = service(t, tank, days(-30), days(20), kind="maintenance")
        other_equipment = service(t, monitor, days(-30), days(5))
        due = list(queries.due_queryset())
        assert set(due) == {newer, other_kind, other_equipment}

    def test_a_newer_event_without_a_next_due_date_clears_the_due_date(self, people, tank):
        t = people["technician"]
        service(t, tank, days(-60), days(-5))
        assert queries.due_queryset(state="overdue").count() == 1
        service(t, tank, days(-10), None)  # done, nothing scheduled
        assert queries.due_queryset().count() == 0
        assert queries.due_queryset(state="overdue").count() == 0

    def test_ordering_is_by_performed_on_not_by_when_it_was_recorded(self, people, tank):
        t = people["technician"]
        recent = service(t, tank, days(-5), days(100))
        service(t, tank, days(-200), days(-100))  # recorded later, performed long ago
        assert list(queries.due_queryset()) == [recent]

    def test_a_correction_changes_the_due_row(self, people, tank):
        t, m = people["technician"], people["manager"]
        original = service(t, tank, days(-30), days(-1))
        assert queries.due_queryset(state="overdue").get() == original
        fixed = services.correct_service_event(
            actor=m, event_id=original.pk, reason="Wrong date", next_due_on=days(60)
        )
        assert queries.due_queryset(state="overdue").count() == 0
        assert list(queries.due_queryset()) == [fixed]

    def test_a_withdrawal_brings_back_the_previous_event(self, people, tank):
        t, m = people["technician"], people["manager"]
        previous = service(t, tank, days(-60), days(-2))
        newer = service(t, tank, days(-10), days(90))
        assert list(queries.due_queryset()) == [newer]
        services.correct_service_event(actor=m, event_id=newer.pk, reason="Wrong tank", void=True)
        assert list(queries.due_queryset()) == [previous]

    def test_a_withdrawal_of_the_only_event_leaves_nothing_due(self, people, tank):
        t, m = people["technician"], people["manager"]
        only = service(t, tank, days(-10), days(30))
        services.correct_service_event(actor=m, event_id=only.pk, reason="Wrong tank", void=True)
        assert queries.due_queryset().count() == 0

    def test_repairs_never_appear(self, people, tank):
        service(people["technician"], tank, days(-5), None, kind="repair")
        assert queries.due_queryset().count() == 0

    def test_retired_equipment_is_excluded(self, people, tank, monitor):
        t = people["technician"]
        service(t, tank, days(-30), days(-1))
        service(t, monitor, days(-30), days(-1))
        retire(people["manager"], monitor)
        assert [e.equipment for e in queries.due_queryset()] == [tank]

    def test_out_of_service_equipment_stays_listed(self, people, tank):
        service(people["technician"], tank, days(-30), days(-1))
        tank.refresh_from_db()
        services.change_status(
            actor=people["manager"],
            equipment_id=tank.pk,
            expected_revision=tank.revision,
            status="out_of_service",
            occurred_at=hours_ago(1),
            reason="Service",
        )
        assert queries.due_queryset().count() == 1

    @pytest.fixture
    def spread(self, people, tank):
        t = people["technician"]
        return {
            "overdue": service(t, tank, days(-60), days(-5), kind="calibration"),
            "today": service(t, tank, days(-60), days(0), kind="maintenance"),
            "soon": service(t, tank, days(-60), days(3), kind="mapping"),
            "later": service(t, tank, days(-60), days(20), kind="qualification"),
        }

    def test_overdue_is_strictly_before_today(self, spread):
        assert list(queries.due_queryset(state="overdue")) == [spread["overdue"]]

    @pytest.mark.parametrize(
        "within, expected",
        [
            (None, {"today", "soon", "later"}),  # the 30-day default
            (7, {"today", "soon"}),
            (3, {"today", "soon"}),
            (2, {"today"}),
            (90, {"today", "soon", "later"}),
        ],
    )
    def test_upcoming_runs_from_today_to_today_plus_within(self, spread, within, expected):
        found = set(queries.due_queryset(state="upcoming", within=within))
        assert found == {spread[name] for name in expected}

    def test_upcoming_with_a_zero_day_horizon_is_today_only(self, spread):
        assert list(queries.due_queryset(state="upcoming", within=0)) == [spread["today"]]

    def test_today_can_be_passed_in(self, spread):
        later = today() + timedelta(days=4)
        assert set(queries.due_queryset(state="overdue", today=later)) == {
            spread["overdue"],
            spread["today"],
            spread["soon"],
        }

    def test_kind_filters(self, spread, people, monitor):
        service(people["technician"], monitor, days(-60), days(1), kind="calibration")
        assert list(queries.due_queryset(kind="mapping")) == [spread["soon"]]
        found = queries.due_queryset(equipment_kind="monitor")
        assert [e.equipment.kind for e in found] == ["monitor"]
        assert (
            queries.due_queryset(kind="calibration", equipment_kind="ln2_tank").get()
            == (spread["overdue"])
        )

    def test_current_due_gives_one_event_per_kind(self, people, tank, spread):
        current = queries.current_due(tank)
        assert set(current) == {"calibration", "maintenance", "mapping", "qualification"}
        assert current["mapping"] == spread["soon"]

    def test_due_state_labels(self, people, tank):
        t = people["technician"]
        overdue = service(t, tank, days(-60), days(-3))
        label, tone, delta = queries.due_state(overdue)
        assert (label, tone, delta) == ("Overdue", "danger", -3)
        due_today = service(t, tank, days(-60), days(0), kind="maintenance")
        assert queries.due_state(due_today) == ("Due today", "warning", 0)
        scheduled = service(t, tank, days(-60), days(12), kind="mapping")
        assert queries.due_state(scheduled) == ("Scheduled", "info", 12)
        assert queries.due_state(scheduled, today=days(12))[0] == "Due today"
        assert queries.due_state(scheduled, today=days(13))[0] == "Overdue"


# ------------------------------------------------------------------ readings outside limits


class TestUncoveredReadings:
    def test_only_effective_readings_outside_the_recorded_limits(self, people, tank):
        t = people["technician"]
        high = reading(t, tank, "-140.00", 100)
        low = reading(t, tank, "-200.00", 90)
        reading(t, tank, "-170.00", 80)  # inside
        reading(t, tank, "-150.00", 70)  # on the limit
        assert set(queries.uncovered_readings()) == {high, low}

    def test_equipment_without_limits_never_lists_readings(self, people):
        loose = register_equipment(people["manager"], code="FZ-LOOSE", kind="freezer")
        reading(people["technician"], loose, "20.00", 60)
        assert queries.uncovered_readings().count() == 0

    def test_limits_are_those_recorded_with_the_reading(self, people, tank):
        t, m = people["technician"], people["manager"]
        recorded = reading(t, tank, "-140.00", 100)  # above -150 when recorded
        tank.refresh_from_db()
        services.save_equipment(
            actor=m,
            equipment_id=tank.pk,
            expected_revision=tank.revision,
            reason="Wider band",
            upper_limit=Decimal("-100.00"),
        )
        later = reading(t, tank, "-140.00", 50)  # inside the new band
        assert list(queries.uncovered_readings()) == [recorded]
        assert later.outside_limits == ""

    def test_corrected_and_withdrawn_readings_are_not_listed(self, people, tank):
        t = people["technician"]
        corrected = reading(t, tank, "-140.00", 100)
        withdrawn = reading(t, tank, "-141.00", 90)
        kept = reading(t, tank, "-142.00", 80)
        fixed = services.correct_reading(
            actor=t, reading_id=corrected.pk, reason="Misread", value=Decimal("-180.00")
        )
        services.correct_reading(actor=t, reading_id=withdrawn.pk, reason="Wrong", void=True)
        assert list(queries.uncovered_readings()) == [kept]
        assert fixed not in queries.uncovered_readings()
        # a correction that is itself outside the limits is listed instead of the original
        again = services.correct_reading(
            actor=t, reading_id=fixed.pk, reason="Misread again", value=Decimal("-120.00")
        )
        assert set(queries.uncovered_readings()) == {kept, again}

    def test_retired_equipment_is_excluded(self, people, tank):
        t = people["technician"]
        loose = register_equipment(
            people["manager"],
            code="FZ-OLD",
            kind="freezer",
            lower_limit=Decimal("-90.00"),
            upper_limit=Decimal("-60.00"),
        )
        reading(t, loose, "-20.00", 60)
        reading(t, tank, "-140.00", 60)
        retire(people["manager"], loose)
        assert [r.equipment for r in queries.uncovered_readings()] == [tank]

    def test_an_excursion_covers_readings_inside_its_period(self, people, tank):
        t = people["technician"]
        before = reading(t, tank, "-140.00", 130)
        inside = reading(t, tank, "-140.00", 100)
        after = reading(t, tank, "-140.00", 40)
        open_ex(t, tank, started_at=ago(120), ended_at=ago(60))
        assert set(queries.uncovered_readings()) == {before, after}
        assert inside not in queries.uncovered_readings()

    def test_the_period_includes_its_start_and_end_instants(self, people, tank):
        t = people["technician"]
        start, end = ago(120), ago(60)
        at_start = reading(t, tank, "-140.00", 0, measured_at=start)
        at_end = reading(t, tank, "-140.00", 0, measured_at=end)
        just_before = reading(t, tank, "-140.00", 0, measured_at=start - timedelta(microseconds=1))
        just_after = reading(t, tank, "-140.00", 0, measured_at=end + timedelta(microseconds=1))
        open_ex(t, tank, started_at=start, ended_at=end)
        assert set(queries.uncovered_readings()) == {just_before, just_after}
        assert at_start not in queries.uncovered_readings()
        assert at_end not in queries.uncovered_readings()

    def test_an_open_ended_excursion_covers_everything_after_its_start(self, people, tank):
        t = people["technician"]
        before = reading(t, tank, "-140.00", 130)
        reading(t, tank, "-140.00", 100)
        reading(t, tank, "-140.00", 10)
        open_ex(t, tank, started_at=ago(120))  # no end yet
        assert list(queries.uncovered_readings()) == [before]

    def test_an_excursion_covers_only_its_own_equipment(self, people, tank):
        t = people["technician"]
        other = register_equipment(
            people["manager"],
            code="FZ-OTHER",
            kind="freezer",
            lower_limit=Decimal("-90.00"),
            upper_limit=Decimal("-60.00"),
        )
        reading(t, tank, "-140.00", 100)
        stray = reading(t, other, "-20.00", 100)
        open_ex(t, tank, started_at=ago(120), ended_at=ago(60))
        assert list(queries.uncovered_readings()) == [stray]

    @pytest.mark.parametrize("stage", ["review", "closed"])
    def test_submitted_and_closed_excursions_still_cover(self, people, tank, stage):
        t = people["technician"]
        reading(t, tank, "-140.00", 280)
        ex = in_review(t, tank)
        if stage == "closed":
            close(people["manager"], ex)
        assert queries.uncovered_readings().count() == 0


# ------------------------------------------------------------------ low levels


class TestLowLevel:
    def test_only_when_the_latest_level_is_below_the_set_low_level(self, people, tank):
        t = people["technician"]
        level(t, tank, "25", 100)
        assert queries.low_level_equipment().count() == 0
        level(t, tank, "19.99", 50)
        assert list(queries.low_level_equipment()) == [tank]
        level(t, tank, "20.00", 10)  # on the low level is not below it
        assert queries.low_level_equipment().count() == 0

    def test_the_older_low_reading_does_not_count_once_a_newer_one_is_fine(self, people, tank):
        t = people["technician"]
        level(t, tank, "10", 100)
        level(t, tank, "30", 50)
        assert queries.low_level_equipment().count() == 0

    def test_latest_is_by_when_it_was_read_not_when_it_was_entered(self, people, tank):
        t = people["technician"]
        level(t, tank, "30", 10)
        level(t, tank, "10", 100)  # entered later, but read earlier
        assert queries.low_level_equipment().count() == 0

    def test_a_latest_level_in_another_unit_is_not_compared(self, people, tank):
        t = people["technician"]
        level(t, tank, "10", 100)
        assert queries.low_level_equipment().count() == 1
        level(t, tank, "10", 50, level_unit="%")  # 10 % is not 10 cm
        assert queries.low_level_equipment().count() == 0

    def test_a_withdrawn_latest_level_falls_back_to_the_one_before(self, people, tank):
        t = people["technician"]
        level(t, tank, "10", 100)
        newer = level(t, tank, "30", 50)
        assert queries.low_level_equipment().count() == 0
        services.correct_ln2(actor=t, entry_id=newer.pk, reason="Wrong tank", void=True)
        assert list(queries.low_level_equipment()) == [tank]

    def test_a_corrected_latest_level_uses_the_correction(self, people, tank):
        t = people["technician"]
        newer = level(t, tank, "30", 50)
        services.correct_ln2(actor=t, entry_id=newer.pk, reason="Misread", level=Decimal("5"))
        assert list(queries.low_level_equipment()) == [tank]

    def test_a_fill_with_only_an_amount_does_not_replace_the_latest_level(self, people, tank):
        t = people["technician"]
        level(t, tank, "10", 100)
        services.record_ln2(
            actor=t,
            equipment_id=tank.pk,
            kind="fill",
            occurred_at=ago(50),
            amount_added=Decimal("100"),
            amount_unit="L",
        )
        assert queries.low_level_equipment().count() == 1

    def test_a_fill_with_a_level_after_it_counts(self, people, tank):
        t = people["technician"]
        level(t, tank, "10", 100)
        services.record_ln2(
            actor=t,
            equipment_id=tank.pk,
            kind="fill",
            occurred_at=ago(50),
            level_before=Decimal("10"),
            level=Decimal("45"),
        )
        assert queries.low_level_equipment().count() == 0

    def test_vessels_without_a_set_low_level_are_never_listed(self, people, dewar):
        level(people["technician"], dewar, "1", 100, level_unit="%")
        assert queries.low_level_equipment().count() == 0

    def test_a_vessel_with_no_level_entries_is_not_listed(self, people, tank):
        assert queries.low_level_equipment().count() == 0

    def test_supply_dewars_with_a_low_level_are_listed(self, people):
        dewar = register_equipment(
            people["manager"],
            code="DEWAR-LOW",
            kind="supply_dewar",
            level_unit="%",
            level_low_limit=Decimal("25"),
        )
        level(people["technician"], dewar, "10", 100)
        assert list(queries.low_level_equipment()) == [dewar]

    def test_retired_vessels_are_excluded(self, people, tank):
        level(people["technician"], tank, "10", 100)
        retire(people["manager"], tank)
        assert queries.low_level_equipment().count() == 0


# ------------------------------------------------------------------ LN2 log and derived change


class TestLN2Log:
    @pytest.fixture
    def entries(self, people, tank, dewar):
        t = people["technician"]
        first = level(t, tank, "30", 300)
        second = level(t, tank, "25", 200)
        fill = services.record_ln2(
            actor=t,
            equipment_id=tank.pk,
            kind="fill",
            occurred_at=ago(100),
            level_before=Decimal("10"),
            level=Decimal("40"),
            amount_added=Decimal("120"),
            amount_unit="L",
            supply=dewar,
        )
        check = level(t, tank, "38", 50)
        return first, second, fill, check

    def by_pk(self, tank):
        return {e.pk: e for e in queries.ln2_log(tank.ln2_entries.all())}

    def test_level_check_changes_against_the_previous_level(self, entries, tank):
        first, second, fill, check = entries
        log = self.by_pk(tank)
        assert queries.level_change(log[first.pk]) is None  # nothing before it
        assert queries.level_change(log[second.pk]) == Decimal("-5.00")

    def test_a_fill_is_measured_from_its_level_before(self, entries, tank):
        first, second, fill, check = entries
        log = self.by_pk(tank)
        assert queries.level_change(log[fill.pk]) == Decimal("-15.00")  # 10 before, 25 earlier

    def test_a_check_after_a_fill_is_measured_against_the_level_after_the_fill(self, entries, tank):
        first, second, fill, check = entries
        log = self.by_pk(tank)
        assert queries.level_change(log[check.pk]) == Decimal("-2.00")  # 38 now, 40 after the fill
        assert log[check.pk].previous_level == Decimal("40.00")
        assert log[check.pk].previous_unit == "cm"
        assert log[check.pk].previous_at == fill.occurred_at

    def test_none_across_units(self, people, tank, entries):
        t = people["technician"]
        percent = level(t, tank, "90", 10, level_unit="%")
        assert queries.level_change(self.by_pk(tank)[percent.pk]) is None

    def test_none_when_the_fill_has_no_level_before_or_the_previous_has_no_level(
        self, people, tank, dewar
    ):
        t = people["technician"]
        level(t, tank, "30", 300)
        amount_only = services.record_ln2(
            actor=t,
            equipment_id=tank.pk,
            kind="fill",
            occurred_at=ago(200),
            amount_added=Decimal("50"),
            amount_unit="L",
            supply=dewar,
        )
        after = level(t, tank, "35", 100)
        log = self.by_pk(tank)
        assert queries.level_change(log[amount_only.pk]) is None  # no level before the fill
        assert queries.level_change(log[after.pk]) is None  # the previous entry has no level

    def test_withdrawn_entries_are_skipped_when_finding_the_previous_one(
        self, people, tank, entries
    ):
        first, second, fill, check = entries
        services.correct_ln2(
            actor=people["technician"], entry_id=second.pk, reason="Wrong tank", void=True
        )
        log = self.by_pk(tank)
        assert second.pk not in log
        assert queries.level_change(log[fill.pk]) == Decimal("-20.00")  # 10 before, 30 earlier

    def test_corrected_entries_use_the_correction(self, people, tank, entries):
        first, second, fill, check = entries
        fixed = services.correct_ln2(
            actor=people["technician"], entry_id=second.pk, reason="Misread", level=Decimal("22")
        )
        log = self.by_pk(tank)
        assert second.pk not in log and fixed.pk in log
        assert queries.level_change(log[fixed.pk]) == Decimal("-8.00")

    def test_each_vessel_is_compared_with_its_own_entries(self, people, tank, dewar):
        t = people["technician"]
        level(t, tank, "30", 300)
        level(t, dewar, "80", 250, level_unit="%")
        second = level(t, tank, "28", 200)
        log = {e.pk: e for e in queries.ln2_log(LN2Entry.objects.all())}
        assert queries.level_change(log[second.pk]) == Decimal("-2.00")

    def test_the_log_keeps_the_caller_s_filters(self, people, tank, dewar, entries):
        level(people["technician"], dewar, "80", 100, level_unit="%")
        assert queries.ln2_log(tank.ln2_entries.all()).count() == 4


# ------------------------------------------------------------------ latest annotations


class TestWithLatest:
    def latest(self, equipment):
        return queries.with_latest(Equipment.objects.filter(pk=equipment.pk)).get()

    def test_nothing_recorded_gives_empty_annotations(self, tank):
        row = self.latest(tank)
        assert row.latest_reading_value is None and row.latest_reading_at is None
        assert row.latest_level is None and row.latest_level_unit is None
        assert row.next_due_on is None and not row.active_excursions

    def test_latest_reading_is_by_measured_time_among_effective_readings(self, people, tank):
        t = people["technician"]
        reading(t, tank, "-170.00", 300)
        newest = reading(t, tank, "-171.00", 100)
        reading(t, tank, "-172.00", 200)  # recorded last, measured in between
        row = self.latest(tank)
        assert row.latest_reading_value == Decimal("-171.00")
        assert row.latest_reading_at == newest.measured_at
        services.correct_reading(actor=t, reading_id=newest.pk, reason="Wrong tank", void=True)
        assert self.latest(tank).latest_reading_value == Decimal("-172.00")

    def test_latest_level_comes_with_its_unit_and_time(self, people, tank):
        t = people["technician"]
        level(t, tank, "30", 300)
        newest = level(t, tank, "22", 100, level_unit="in")
        row = self.latest(tank)
        assert (row.latest_level, row.latest_level_unit) == (Decimal("22.00"), "in")
        assert row.latest_level_at == newest.occurred_at

    def test_next_due_is_the_earliest_current_due_date(self, people, tank):
        t = people["technician"]
        service(t, tank, days(-60), days(40), kind="calibration")
        service(t, tank, days(-60), days(15), kind="maintenance")
        service(t, tank, days(-90), days(-30), kind="mapping")
        service(t, tank, days(-10), None, kind="mapping")  # clears the overdue mapping
        assert self.latest(tank).next_due_on == days(15)

    def test_active_excursions_count_open_and_review_but_not_closed(self, people, tank):
        t, m = people["technician"], people["manager"]
        open_ex(t, tank)
        in_review(t, tank)
        closed = in_review(t, tank)
        close(m, closed)
        assert self.latest(tank).active_excursions == 2

    def test_annotations_are_per_equipment(self, people, tank):
        t = people["technician"]
        loose = register_equipment(people["manager"], code="FZ-LOOSE", kind="freezer")
        reading(t, tank, "-170.00", 100)
        rows = {e.code: e for e in queries.with_latest(Equipment.objects.all())}
        assert rows["TANK-VP-01"].latest_reading_value == Decimal("-170.00")
        assert rows[loose.code].latest_reading_value is None


# ------------------------------------------------------------------ excursion queues


class TestExcursionQueues:
    def test_awaiting_review_excludes_own_submissions_and_other_states(self, people, tank):
        t, m = people["technician"], people["manager"]
        by_tech = in_review(t, tank)
        by_manager = in_review(m, tank)
        open_ex(t, tank)
        done = in_review(t, tank)
        close(m, done)
        assert list(queries.awaiting_review(m)) == [by_tech]
        assert list(queries.awaiting_review(t)) == [by_manager]
        assert set(queries.awaiting_review(people["admin"])) == {by_tech, by_manager}

    def test_active_excursions_are_open_and_in_review(self, people, tank):
        t, m = people["technician"], people["manager"]
        a = open_ex(t, tank)
        b = in_review(t, tank)
        close(m, in_review(t, tank))
        assert set(queries.active_excursions()) == {a, b}

    def test_readings_during_an_excursion_are_the_effective_ones_inside_its_period(
        self, people, tank
    ):
        t = people["technician"]
        reading(t, tank, "-170.00", 200)
        inside = reading(t, tank, "-140.00", 100)
        gone = reading(t, tank, "-141.00", 90)
        reading(t, tank, "-170.00", 20)
        services.correct_reading(actor=t, reading_id=gone.pk, reason="Wrong", void=True)
        ex = open_ex(t, tank, started_at=ago(120), ended_at=ago(60))
        assert list(queries.readings_during(ex)) == [inside]
        open_ended = open_ex(t, tank, started_at=ago(120))
        assert len(list(queries.readings_during(open_ended))) == 2


class TestExcursionSpecimens:
    def stored(self, people, world, minutes=270, label="A1"):
        t = people["technician"]
        specimen = register(t, world, occurred_at=ago(272))
        place(t, specimen, world.position(label), occurred_at=ago(minutes))
        return specimen

    def test_a_closed_excursion_shows_the_captured_list(self, people, world, tank):
        inside = self.stored(people, world)
        ex = close(people["manager"], in_review(people["technician"], tank))
        rows, capture = queries.excursion_specimens(ex, actor=people["viewer"])
        assert [r.specimen_id for r in rows] == [inside.pk]
        assert rows[0].accession == inside.accession
        assert capture is not None and capture.kind == "closed"
        # movements recorded afterwards do not change what was captured
        later = self.stored(people, world, minutes=255, label="A2")
        rows, _ = queries.excursion_specimens(
            Excursion.objects.get(pk=ex.pk), actor=people["viewer"]
        )
        assert later.pk not in [r.specimen_id for r in rows]

    def test_an_open_or_submitted_excursion_shows_a_live_list(self, people, world, tank):
        inside = self.stored(people, world)
        ex = in_review(people["technician"], tank)
        rows, capture = queries.excursion_specimens(ex, actor=people["viewer"])
        assert capture is None and [r.specimen_id for r in rows] == [inside.pk]
        later = self.stored(people, world, minutes=255, label="A2")
        rows, capture = queries.excursion_specimens(
            Excursion.objects.get(pk=ex.pk), actor=people["viewer"]
        )
        assert capture is None
        assert {r.specimen_id for r in rows} == {inside.pk, later.pk}

    def test_an_ongoing_excursion_runs_up_to_now(self, people, world, tank):
        late = self.stored(people, world, minutes=10)
        ex = open_ex(people["technician"], tank, started_at=ago(300))  # no end
        rows, capture = queries.excursion_specimens(ex, actor=people["viewer"])
        assert capture is None and [r.specimen_id for r in rows] == [late.pk]

    def test_the_latest_capture_is_shown_after_a_reopen_and_second_close(self, people, world, tank):
        t, m, a = people["technician"], people["manager"], people["admin"]
        first = self.stored(people, world)
        ex = in_review(t, tank)
        ex = close(m, ex)
        first_rows, first_capture = queries.excursion_specimens(ex, actor=people["viewer"])
        assert [r.specimen_id for r in first_rows] == [first.pk]
        second = self.stored(people, world, minutes=255, label="A2")
        ex.refresh_from_db()
        services.reopen_excursion(
            actor=m, excursion_id=ex.pk, expected_revision=ex.revision, reason="Evidence"
        )
        # reopened: live list, no capture
        rows, capture = queries.excursion_specimens(
            Excursion.objects.get(pk=ex.pk), actor=people["viewer"]
        )
        assert capture is None and {r.specimen_id for r in rows} == {first.pk, second.pk}
        ex = Excursion.objects.get(pk=ex.pk)
        services.submit_excursion(actor=t, excursion_id=ex.pk, expected_revision=ex.revision)
        ex = close(a, ex)
        rows, capture = queries.excursion_specimens(ex, actor=people["viewer"])
        assert capture != first_capture and capture.kind == "closed"
        assert {r.specimen_id for r in rows} == {first.pk, second.pk}

    def test_equipment_without_a_storage_unit_has_no_specimens(self, people):
        loose = register_equipment(people["manager"], code="FZ-LOOSE", kind="freezer")
        ex = in_review(people["technician"], loose)
        assert queries.excursion_specimens(ex, actor=people["viewer"]) == ([], None)
        ex = close(people["manager"], ex)
        rows, capture = queries.excursion_specimens(ex, actor=people["viewer"])
        assert rows == [] and capture is not None


# ------------------------------------------------------------------ occupancy


class TestOccupancy:
    def accessions(self, unit, start, end, actor=None):
        actor = actor or self.viewer
        rows = affected_specimens(actor=actor, unit_id=unit.pk, start=start, end=end)
        return {r.accession: r for r in rows}

    @pytest.fixture(autouse=True)
    def _viewer(self, people):
        self.viewer = people["viewer"]

    def test_a_specimen_out_for_the_whole_period_is_excluded(self, people, world):
        t = people["technician"]
        away = register(t, world, occurred_at=ago(272))
        place(t, away, world.position("A1"), occurred_at=ago(260))
        check_out(t, away, t, occurred_at=ago(200))
        check_in(t, away, world.position("A1"), occurred_at=ago(50))
        present = register(t, world, occurred_at=ago(272))
        place(t, present, world.position("A2"), occurred_at=ago(260))
        found = self.accessions(world.unit, ago(150), ago(100))
        assert set(found) == {present.accession}

    def test_a_specimen_checked_out_during_the_period_is_present_until_then(self, people, world):
        t = people["technician"]
        specimen = register(t, world, occurred_at=ago(272))
        place(t, specimen, world.position("A1"), occurred_at=ago(260))
        out_at = ago(120)
        check_out(t, specimen, t, occurred_at=out_at)
        found = self.accessions(world.unit, ago(150), ago(100))
        row = found[specimen.accession]
        assert row.present_until == out_at
        assert row.present_from <= ago(149)  # it was already in at the start

    def test_a_disposed_specimen_leaves_at_disposal(self, people, world):
        t = people["technician"]
        specimen = register(t, world, occurred_at=ago(272))
        place(t, specimen, world.position("A1"), occurred_at=ago(260))
        request = ask_disposal(t, specimen)
        decide(people["approver"], request)
        execute(t, request)  # a disposal cannot be dated before its approval: now
        after = timezone.now()
        row = self.accessions(world.unit, ago(200), after)[specimen.accession]
        assert ago(1) < row.present_until <= after
        assert self.accessions(world.unit, after, after + timedelta(minutes=5)) == {}

    def test_placed_exactly_at_the_end_instant_is_included_with_a_zero_length_presence(
        self, people, world
    ):
        t = people["technician"]
        end = ago(100)
        specimen = register(t, world, occurred_at=ago(272))
        place(t, specimen, world.position("A1"), occurred_at=end)
        row = self.accessions(world.unit, ago(150), end)[specimen.accession]
        assert row.present_from == row.present_until == end
        assert row.intervals == ((end, end),)

    def test_placed_just_after_the_end_is_excluded(self, people, world):
        t = people["technician"]
        end = ago(100)
        specimen = register(t, world, occurred_at=ago(272))
        place(t, specimen, world.position("A1"), occurred_at=end + timedelta(microseconds=1))
        assert self.accessions(world.unit, ago(150), end) == {}

    def test_placed_exactly_at_the_start_instant_is_present_from_the_start(self, people, world):
        t = people["technician"]
        start = ago(150)
        specimen = register(t, world, occurred_at=ago(272))
        place(t, specimen, world.position("A1"), occurred_at=start)
        row = self.accessions(world.unit, start, ago(100))[specimen.accession]
        assert row.present_from == start

    def test_removed_exactly_at_the_start_instant_is_not_in_the_period(self, people, world):
        t = people["technician"]
        start = ago(150)
        specimen = register(t, world, occurred_at=ago(272))
        place(t, specimen, world.position("A1"), occurred_at=ago(260))
        check_out(t, specimen, t, occurred_at=start)
        assert self.accessions(world.unit, start, ago(100)) == {}

    def test_end_before_start_is_refused_by_storage(self, people, world):
        with pytest.raises(ValidationError):
            affected_specimens(
                actor=people["viewer"], unit_id=world.unit.pk, start=ago(10), end=ago(20)
            )

    def test_reading_who_was_in_a_unit_needs_a_role(self, people, world):
        with pytest.raises(PermissionDenied):
            affected_specimens(
                actor=people["nobody"], unit_id=world.unit.pk, start=ago(20), end=ago(10)
            )

    def test_a_unit_nothing_was_ever_in_has_no_occupants(self, people, world, freezer_unit):
        t = people["technician"]
        specimen = register(t, world, occurred_at=ago(272))
        place(t, specimen, world.position("A1"), occurred_at=ago(260))
        unit, _ = freezer_unit
        assert self.accessions(unit, ago(300), ago(10)) == {}

    def test_rows_are_ordered_by_location_then_accession(self, people, world):
        t = people["technician"]
        for label in ("A3", "A1", "A2"):
            specimen = register(t, world, occurred_at=ago(272))
            place(t, specimen, world.position(label), occurred_at=ago(260))
        rows = affected_specimens(
            actor=people["viewer"], unit_id=world.unit.pk, start=ago(200), end=ago(100)
        )
        assert [r.location for r in rows] == sorted(r.location for r in rows)
        assert len(rows) == 3
