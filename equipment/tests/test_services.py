"""Equipment services: register and status, service events, LN2 log, readings, CSV import,
excursions. Rules and refusals only; role matrices are in test_permissions.py, trigger guards in
test_history_guards.py. All data is synthetic.
"""

import hashlib
import io
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from core.models import Attachment, AuditEvent
from equipment import services
from equipment.models import (
    AffectedSpecimen,
    Equipment,
    Excursion,
    ExcursionEvent,
    LN2Entry,
    ReadingImport,
    ReadingImportCommit,
    ServiceEvent,
    StatusChange,
    TemperatureReading,
)

from .conftest import ago, csv_file, hours_ago, place, register, register_equipment

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fast_password_hashing(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


# ------------------------------------------------------------------ helpers


def field_errors(exc):
    if hasattr(exc, "error_dict"):
        return {key: [e.code for e in errs] for key, errs in exc.error_dict.items()}
    return {None: [e.code for e in exc.error_list]}


@contextmanager
def rejected(field=None, code=None):
    """The body must raise a ValidationError, on `field` and with `code` when given."""
    with pytest.raises(ValidationError) as info:
        yield
    errors = field_errors(info.value)
    if field is not None:
        assert field in errors, errors
        if code is not None:
            assert code in errors[field], errors
    elif code is not None:
        assert code in [c for codes in errors.values() for c in codes], errors


def today():
    return timezone.localdate()


def edit(actor, equipment, reason="Corrected record", **data):
    equipment.refresh_from_db()
    return services.save_equipment(
        actor=actor,
        equipment_id=equipment.pk,
        expected_revision=equipment.revision,
        reason=reason,
        **data,
    )


def set_status(actor, equipment, status, occurred_at=None, reason="Moved to new state"):
    equipment.refresh_from_db()
    return services.change_status(
        actor=actor,
        equipment_id=equipment.pk,
        expected_revision=equipment.revision,
        status=status,
        occurred_at=occurred_at or hours_ago(1),
        reason=reason,
    )


def service(actor, equipment, **over):
    values = {
        "kind": "calibration",
        "performed_on": today(),
        "outcome": "acceptable",
        "performed_by": actor,
    }
    values.update(over)
    return services.record_service_event(actor=actor, equipment_id=equipment.pk, **values)


def level(actor, equipment, value="31.5", minutes=100, **over):
    values = {"kind": "level", "occurred_at": ago(minutes), "level": Decimal(value)}
    values.update(over)
    return services.record_ln2(actor=actor, equipment_id=equipment.pk, **values)


def reading(actor, equipment, value="-180.00", minutes=100, **over):
    values = {"measured_at": ago(minutes), "value": Decimal(value)}
    values.update(over)
    return services.record_reading(actor=actor, equipment_id=equipment.pk, **values)


def pdf(name="certificate.pdf", body=b"%PDF-1.7 synthetic certificate"):
    return SimpleUploadedFile(name, body, content_type="application/pdf")


def audit(action):
    return AuditEvent.objects.filter(action=action)


@pytest.fixture
def freezer(people):
    return register_equipment(
        people["manager"],
        code="FZ-01",
        kind="freezer",
        lower_limit=Decimal("-90.00"),
        upper_limit=Decimal("-60.00"),
    )


# ------------------------------------------------------------------ save_equipment


class TestSaveEquipment:
    def test_register_normalizes_code_and_audits(self, people):
        item = register_equipment(people["manager"], code=" fz-lab.2 ", kind="freezer")
        assert item.code == "FZ-LAB.2"
        assert item.status == Equipment.Status.IN_SERVICE and item.revision == 1
        event = audit("equipment.registered").get()
        assert event.actor == people["manager"] and event.object_id == str(item.pk)

    def test_code_is_unique_whatever_the_case(self, people, tank):
        with rejected("code", "unique"):
            register_equipment(people["manager"], code="tank-vp-01", kind="freezer")
        assert Equipment.objects.filter(code__iexact="TANK-VP-01").count() == 1

    def test_invalid_code_refused(self, people):
        with rejected("code"):
            register_equipment(people["manager"], code="bad code!", kind="freezer")

    def test_edit_needs_reason(self, people, tank):
        with rejected("reason", "required"):
            services.save_equipment(
                actor=people["manager"],
                equipment_id=tank.pk,
                expected_revision=tank.revision,
                reason="  ",
                name="Renamed",
            )

    @pytest.mark.parametrize("revision", [None, 0, 99])
    def test_edit_needs_the_current_revision(self, people, tank, revision):
        with rejected(code="stale_revision"):
            services.save_equipment(
                actor=people["manager"],
                equipment_id=tank.pk,
                expected_revision=revision,
                reason="Rename",
                name="Renamed",
            )
        tank.refresh_from_db()
        assert tank.name == "Tank VP-01" and tank.revision == 1

    def test_edit_updates_record_bumps_revision_and_audits_before_after(self, people, tank):
        updated = edit(people["manager"], tank, name="Tank VP-01 (north)", reason="Relabelled")
        assert updated.name == "Tank VP-01 (north)" and updated.revision == 2
        event = audit("equipment.updated").get()
        assert event.reason == "Relabelled"
        assert event.before["name"] == "Tank VP-01" and event.after["name"] == "Tank VP-01 (north)"

    def test_edit_that_changes_nothing_is_refused(self, people, tank):
        with rejected(code="invalid_transition"):
            edit(people["manager"], tank, name=tank.name)
        tank.refresh_from_db()
        assert tank.revision == 1

    def test_edit_restating_a_limit_with_other_trailing_zeros_is_a_no_op(self, people, tank):
        with rejected(code="invalid_transition"):
            edit(people["manager"], tank, lower_limit=Decimal("-196.0"))

    def test_kind_cannot_change_once_entries_exist(self, people, tank):
        level(people["technician"], tank)
        with rejected("kind"):
            edit(people["manager"], tank, kind="freezer", level_unit="", level_low_limit=None)

    def test_kind_can_change_while_no_entries_exist(self, people, dewar):
        assert edit(people["manager"], dewar, kind="other").kind == "other"

    def test_second_enclosure_for_one_unit_refused(self, people, world, tank):
        with rejected("storage_unit", "unique"):
            register_equipment(
                people["manager"], code="FZ-DUP", kind="freezer", storage_unit=world.unit
            )
        assert not Equipment.objects.filter(code="FZ-DUP").exists()

    def test_retired_enclosure_frees_the_unit(self, people, world, tank):
        set_status(people["manager"], tank, "retired")
        replacement = register_equipment(
            people["manager"], code="TANK-VP-02", kind="ln2_tank", storage_unit=world.unit
        )
        assert replacement.storage_unit_id == world.unit.pk

    def test_other_kinds_may_share_a_unit_with_the_enclosure(self, people, world, tank):
        logger = register_equipment(
            people["manager"], code="MON-VP", kind="monitor", storage_unit=world.unit
        )
        assert logger.storage_unit_id == world.unit.pk

    def test_monitor_cannot_have_temperature_limits(self, people):
        with rejected("lower_limit"):
            register_equipment(
                people["manager"], code="MON-X", kind="monitor", lower_limit=Decimal("-80.00")
            )
        with rejected("lower_limit"):
            register_equipment(
                people["manager"], code="MON-X", kind="monitor", upper_limit=Decimal("-60.00")
            )

    def test_level_settings_only_on_ln2_kinds(self, people):
        with rejected("level_unit"):
            register_equipment(people["manager"], code="FZ-X", kind="freezer", level_unit="cm")
        with rejected("level_unit"):
            register_equipment(
                people["manager"], code="FZ-X", kind="freezer", level_low_limit=Decimal("5")
            )
        dewar = register_equipment(
            people["manager"],
            code="DW-X",
            kind="supply_dewar",
            level_unit="%",
            level_low_limit=Decimal("10"),
        )
        assert dewar.level_unit == "%"

    def test_low_level_needs_a_unit_and_percent_caps_at_100(self, people):
        with rejected("level_unit", "required"):
            register_equipment(
                people["manager"], code="T-X", kind="ln2_tank", level_low_limit=Decimal("5")
            )
        with rejected("level_low_limit"):
            register_equipment(
                people["manager"],
                code="T-X",
                kind="ln2_tank",
                level_unit="%",
                level_low_limit=Decimal("101"),
            )

    @pytest.mark.parametrize(
        "lower, upper", [("-60.00", "-90.00"), ("-80.00", "-80.00")], ids=["inverted", "equal"]
    )
    def test_lower_limit_must_be_below_upper(self, people, lower, upper):
        with rejected("upper_limit"):
            register_equipment(
                people["manager"],
                code="FZ-X",
                kind="freezer",
                lower_limit=Decimal(lower),
                upper_limit=Decimal(upper),
            )

    def test_one_limit_alone_is_fine(self, people):
        item = register_equipment(
            people["manager"], code="FZ-X", kind="freezer", upper_limit=Decimal("-60.00")
        )
        assert item.lower_limit is None and item.upper_limit == Decimal("-60.00")

    def test_supply_dewar_cannot_link_a_unit(self, people, world):
        with rejected("storage_unit"):
            register_equipment(
                people["manager"], code="DW-X", kind="supply_dewar", storage_unit=world.unit
            )

    def test_float_limit_refused(self, people):
        with rejected("lower_limit", "invalid"):
            register_equipment(people["manager"], code="FZ-X", kind="freezer", lower_limit=-80.0)

    def test_three_decimal_limit_refused(self, people):
        with rejected("upper_limit", "invalid"):
            register_equipment(
                people["manager"], code="FZ-X", kind="freezer", upper_limit=Decimal("-60.125")
            )

    def test_limit_below_absolute_zero_refused(self, people):
        with rejected("lower_limit"):
            register_equipment(
                people["manager"], code="FZ-X", kind="freezer", lower_limit=Decimal("-300.00")
            )

    def test_unknown_field_refused(self, people):
        with rejected():
            services.save_equipment(
                actor=people["manager"], code="FZ-X", name="x", kind="freezer", status="retired"
            )

    def test_status_cannot_be_set_through_edit(self, people, tank):
        with rejected():
            edit(people["manager"], tank, status="retired")


# ------------------------------------------------------------------ change_status


class TestChangeStatus:
    def test_reason_required(self, people, tank):
        with rejected("reason", "required"):
            set_status(people["manager"], tank, "out_of_service", reason="")
        assert not StatusChange.objects.exists()

    def test_same_status_refused(self, people, tank):
        with rejected("status", "invalid_transition"):
            set_status(people["manager"], tank, "in_service")

    def test_unknown_status_refused(self, people, tank):
        with rejected("status"):
            set_status(people["manager"], tank, "broken")

    def test_stale_revision_refused(self, people, tank):
        with rejected(code="stale_revision"):
            services.change_status(
                actor=people["manager"],
                equipment_id=tank.pk,
                expected_revision=7,
                status="retired",
                occurred_at=hours_ago(1),
                reason="Old",
            )

    def test_future_time_refused(self, people, tank):
        with rejected("occurred_at", "invalid_time"):
            set_status(
                people["manager"],
                tank,
                "out_of_service",
                occurred_at=timezone.now() + timedelta(minutes=5),
            )

    def test_naive_time_refused(self, people, tank):
        with rejected("occurred_at", "invalid_time"):
            set_status(
                people["manager"],
                tank,
                "out_of_service",
                occurred_at=datetime(2026, 1, 1, 8, 0),
            )

    def test_time_earlier_than_the_last_change_refused(self, people, tank):
        set_status(people["manager"], tank, "out_of_service", occurred_at=hours_ago(2))
        with rejected("occurred_at", "invalid_time"):
            set_status(people["manager"], tank, "in_service", occurred_at=hours_ago(3))
        assert StatusChange.objects.count() == 1

    def test_back_dated_first_change_is_allowed(self, people, tank):
        set_status(people["manager"], tank, "out_of_service", occurred_at=hours_ago(72))
        assert StatusChange.objects.get().occurred_at < hours_ago(71)

    def test_status_changes_are_written_with_actor_and_times(self, people, tank):
        set_status(people["manager"], tank, "out_of_service", hours_ago(5), reason="Vacuum service")
        set_status(people["admin"], tank, "in_service", hours_ago(2), reason="Back after service")
        first, second = StatusChange.objects.order_by("occurred_at")
        assert (first.from_status, first.to_status) == ("in_service", "out_of_service")
        assert (second.from_status, second.to_status) == ("out_of_service", "in_service")
        assert first.actor == people["manager"] and second.actor == people["admin"]
        assert first.reason == "Vacuum service"
        assert first.occurred_at < first.recorded_at
        tank.refresh_from_db()
        assert tank.status == "in_service" and tank.revision == 3
        assert audit("equipment.status_changed").count() == 2

    def test_retiring_with_an_open_excursion_refused(self, people, tank):
        open_ex(people["technician"], tank)
        with rejected("status", "invalid_transition"):
            set_status(people["manager"], tank, "retired")
        tank.refresh_from_db()
        assert tank.status == "in_service"

    def test_retiring_with_an_excursion_in_review_refused(self, people, tank):
        in_review(people, tank)
        with rejected("status", "invalid_transition"):
            set_status(people["manager"], tank, "retired")

    def test_retiring_after_the_excursion_closed_is_allowed(self, people, tank):
        ex = in_review(people, tank)
        services.close_excursion(
            actor=people["manager"], excursion_id=ex.pk, expected_revision=ex.revision, note="ok"
        )
        assert set_status(people["manager"], tank, "retired").status == "retired"

    ENTRY_ACTIONS = {
        "ln2": lambda p, e: level(p["technician"], e),
        "reading": lambda p, e: reading(p["technician"], e),
        "service": lambda p, e: service(p["technician"], e),
        "excursion": lambda p, e: open_ex(p["technician"], e),
    }

    @pytest.mark.parametrize("action", ENTRY_ACTIONS)
    def test_retired_equipment_refuses_new_entries(self, people, tank, action):
        set_status(people["manager"], tank, "retired")
        counts = (
            LN2Entry.objects.count(),
            TemperatureReading.objects.count(),
            ServiceEvent.objects.count(),
            Excursion.objects.count(),
        )
        with rejected(code="invalid_transition"):
            self.ENTRY_ACTIONS[action](people, tank)
        assert counts == (
            LN2Entry.objects.count(),
            TemperatureReading.objects.count(),
            ServiceEvent.objects.count(),
            Excursion.objects.count(),
        )

    def test_return_to_service_allows_entries_again(self, people, tank):
        set_status(people["manager"], tank, "retired", hours_ago(3))
        set_status(people["manager"], tank, "in_service", hours_ago(2))
        assert level(people["technician"], tank).equipment_id == tank.pk

    def test_out_of_service_still_takes_entries(self, people, tank):
        set_status(people["manager"], tank, "out_of_service")
        assert reading(people["technician"], tank).pk

    def test_reactivating_a_retired_tank_when_another_holds_the_unit_refused(
        self, people, world, tank
    ):
        set_status(people["manager"], tank, "retired", hours_ago(3))
        register_equipment(
            people["manager"], code="TANK-VP-02", kind="ln2_tank", storage_unit=world.unit
        )
        with rejected("status", "unique"):
            set_status(people["manager"], tank, "in_service", hours_ago(2))
        tank.refresh_from_db()
        assert tank.status == "retired"
        assert StatusChange.objects.count() == 1  # the failed attempt left no row behind


# ------------------------------------------------------------------ service events


class TestServiceEvents:
    @pytest.mark.parametrize(
        "kind, outcome",
        [
            ("maintenance", "acceptable"),
            ("repair", "acceptable"),
            ("calibration", "completed"),
            ("mapping", "completed"),
            ("qualification", "completed"),
        ],
    )
    def test_outcome_must_fit_the_kind(self, people, tank, kind, outcome):
        with rejected("outcome"):
            service(people["technician"], tank, kind=kind, outcome=outcome)
        assert not ServiceEvent.objects.exists()

    @pytest.mark.parametrize(
        "kind, outcome",
        [
            ("maintenance", "completed"),
            ("repair", "not_acceptable"),
            ("calibration", "acceptable"),
            ("mapping", "not_acceptable"),
        ],
    )
    def test_valid_outcomes_recorded(self, people, tank, kind, outcome):
        event = service(people["technician"], tank, kind=kind, outcome=outcome)
        assert (event.kind, event.outcome) == (kind, outcome)

    def test_unknown_kind_refused(self, people, tank):
        with rejected("kind"):
            service(people["technician"], tank, kind="polish")

    def test_repair_cannot_have_a_next_due_date(self, people, tank):
        with rejected("next_due_on"):
            service(
                people["technician"],
                tank,
                kind="repair",
                outcome="completed",
                next_due_on=today() + timedelta(days=30),
            )

    def test_next_due_before_performed_refused(self, people, tank):
        with rejected("next_due_on"):
            service(
                people["technician"],
                tank,
                performed_on=today() - timedelta(days=1),
                next_due_on=today() - timedelta(days=2),
            )

    def test_next_due_on_the_performed_day_is_accepted(self, people, tank):
        event = service(people["technician"], tank, next_due_on=today())
        assert event.next_due_on == today()

    def test_performer_required(self, people, tank):
        with rejected("performed_by", "required"):
            service(people["technician"], tank, performed_by=None)
        with rejected("performed_by", "required"):
            service(people["technician"], tank, performed_by=None, performed_by_external="  ")

    def test_performer_may_be_staff_or_external(self, people, tank):
        staff = service(people["technician"], tank, performed_by=people["technician2"])
        outside = service(
            people["technician"],
            tank,
            performed_by=None,
            performed_by_external="Example Cryo Service Ltd",
        )
        assert staff.performed_by == people["technician2"]
        assert outside.performed_by is None and outside.performer == "Example Cryo Service Ltd"

    def test_performer_must_be_active_staff(self, people, tank):
        with rejected("performed_by", "invalid"):
            service(people["technician"], tank, performed_by=people["nobody"])

    def test_performed_on_in_the_future_refused(self, people, tank):
        with rejected("performed_on", "invalid_time"):
            service(people["technician"], tank, performed_on=today() + timedelta(days=1))

    def test_performed_on_required(self, people, tank):
        with rejected("performed_on", "required"):
            service(people["technician"], tank, performed_on=None)

    def test_record_is_audited_with_kind_in_the_action(self, people, tank):
        event = service(people["technician"], tank, kind="mapping")
        entry = audit("equipment.mapping_recorded").get()
        assert entry.actor == people["technician"]
        assert entry.after["service_event"] == str(event.pk)

    # --- attachments

    def test_attachment_is_stored_with_checksum_size_and_type(self, people, tank):
        body = b"%PDF-1.7 synthetic calibration certificate"
        event = service(people["technician"], tank, attachment=pdf(body=body))
        stored = Attachment.objects.with_data().get(pk=event.attachment_id)
        assert stored.name == "certificate.pdf"
        assert stored.content_type == "application/pdf"
        assert stored.size == len(body)
        assert stored.sha256 == hashlib.sha256(body).hexdigest()
        assert bytes(stored.data) == body
        assert stored.created_by == people["technician"]
        assert event.has_attachment

    def test_attachment_bytes_are_deferred_on_default_queries(self, people, tank):
        event = service(people["technician"], tank, attachment=pdf())
        plain = ServiceEvent.objects.get(pk=event.pk).attachment
        assert "data" in plain.get_deferred_fields()
        assert plain.sha256  # the metadata is loaded
        assert "data" in Attachment.objects.filter(pk=plain.pk).first().get_deferred_fields()

    def test_client_content_type_is_ignored(self, people, tank):
        upload = SimpleUploadedFile("log.txt", b"synthetic", content_type="text/html")
        event = service(people["technician"], tank, attachment=upload)
        assert event.attachment.content_type == "text/plain; charset=utf-8"

    @pytest.mark.parametrize(
        "upload",
        [
            SimpleUploadedFile("cert.pdf", b"\x89PNG\r\n\x1a\n not a pdf"),
            SimpleUploadedFile("cert.png", b"%PDF-1.7 not a png"),
            SimpleUploadedFile("cert.jpg", b"GIF89a"),
            SimpleUploadedFile("cert.csv", b"\xff\xfe\x00 not utf-8"),
            SimpleUploadedFile("cert.txt", b"\xc3\x28 broken utf-8"),
            SimpleUploadedFile("cert.exe", b"MZ"),
            SimpleUploadedFile("cert.pdf", b""),
            SimpleUploadedFile("cert.pdf", b"%PDF-" + b"0" * (10 * 1024 * 1024)),
        ],
        ids=[
            "pdf-with-png-bytes",
            "png-with-pdf-bytes",
            "jpg-with-gif-bytes",
            "non-utf8-csv",
            "non-utf8-txt",
            "unknown-extension",
            "empty",
            "over-10-MiB",
        ],
    )
    def test_bad_attachment_refused_and_nothing_recorded(self, people, tank, upload):
        with rejected("attachment"):
            service(people["technician"], tank, attachment=upload)
        assert not ServiceEvent.objects.exists()
        assert not audit("equipment.calibration_recorded").exists()

    def test_exactly_10_mib_is_accepted(self, people, tank):
        body = b"%PDF-" + b"0" * (10 * 1024 * 1024 - 5)
        event = service(people["technician"], tank, attachment=pdf(body=body))
        assert event.attachment.size == 10 * 1024 * 1024

    # --- corrections

    def test_correction_keeps_kind_and_replaces_values(self, people, tank):
        original = service(
            people["technician"], tank, description="Wrong text", next_due_on=today()
        )
        fixed = services.correct_service_event(
            actor=people["technician"],
            event_id=original.pk,
            reason="Typo",
            kind="maintenance",  # ignored: a correction never changes the kind
            description="Right text",
            next_due_on=today() + timedelta(days=365),
        )
        assert fixed.kind == "calibration" and fixed.outcome == "acceptable"
        assert fixed.description == "Right text"
        assert fixed.next_due_on == today() + timedelta(days=365)
        assert fixed.corrects == original and fixed.correction_reason == "Typo"
        assert not fixed.voided
        assert fixed.recorded_by == people["technician"]
        original.refresh_from_db()
        assert original.description == "Wrong text"  # the original is kept
        assert ServiceEvent.objects.get(pk=original.pk).corrected_by == fixed
        entry = audit("equipment.service_corrected").get()
        assert entry.before["description"] == "Wrong text"
        assert entry.after["description"] == "Right text" and entry.reason == "Typo"

    def test_correction_needs_a_reason(self, people, tank):
        original = service(people["technician"], tank)
        with rejected("reason", "required"):
            services.correct_service_event(
                actor=people["technician"], event_id=original.pk, reason=" ", description="x"
            )
        assert ServiceEvent.objects.count() == 1

    def test_correction_that_changes_nothing_is_refused(self, people, tank):
        original = service(people["technician"], tank, description="Same")
        with rejected(code="invalid_transition"):
            services.correct_service_event(
                actor=people["technician"],
                event_id=original.pk,
                reason="Nothing really",
                description="Same",
            )

    def test_correction_is_validated_like_a_new_entry(self, people, tank):
        original = service(people["technician"], tank)
        with rejected("outcome"):
            services.correct_service_event(
                actor=people["technician"], event_id=original.pk, reason="x", outcome="completed"
            )
        with rejected("next_due_on"):
            services.correct_service_event(
                actor=people["technician"],
                event_id=original.pk,
                reason="x",
                next_due_on=original.performed_on - timedelta(days=1),
            )

    def test_correction_may_still_change_the_performer(self, people, tank):
        original = service(people["technician"], tank)
        fixed = services.correct_service_event(
            actor=people["manager"],
            event_id=original.pk,
            reason="Done by the vendor",
            performed_by=None,
            performed_by_external="Example Cryo Service Ltd",
        )
        assert fixed.performed_by is None and fixed.performer == "Example Cryo Service Ltd"

    def test_void_copies_values_and_withdraws(self, people, tank):
        original = service(
            people["technician"],
            tank,
            description="Entered on the wrong tank",
            next_due_on=today() + timedelta(days=90),
        )
        withdrawn = services.correct_service_event(
            actor=people["manager"],
            event_id=original.pk,
            reason="Wrong equipment",
            void=True,
            description="ignored on a withdrawal",
        )
        assert withdrawn.voided and withdrawn.corrects == original
        for name in services.SERVICE_FIELDS:
            assert getattr(withdrawn, name) == getattr(original, name), name
        assert audit("equipment.service_withdrawn").get().reason == "Wrong equipment"

    def test_already_corrected_entry_cannot_be_corrected_again(self, people, tank):
        original = service(people["technician"], tank)
        services.correct_service_event(
            actor=people["technician"], event_id=original.pk, reason="x", description="A"
        )
        with rejected(code="invalid_transition"):
            services.correct_service_event(
                actor=people["technician"], event_id=original.pk, reason="y", description="B"
            )
        assert ServiceEvent.objects.count() == 2

    def test_the_newer_entry_of_a_chain_can_be_corrected(self, people, tank):
        original = service(people["technician"], tank)
        first = services.correct_service_event(
            actor=people["technician"], event_id=original.pk, reason="x", description="A"
        )
        second = services.correct_service_event(
            actor=people["technician"], event_id=first.pk, reason="y", description="B"
        )
        assert second.corrects == first and second.description == "B"

    def test_withdrawn_entry_cannot_be_corrected(self, people, tank):
        original = service(people["technician"], tank)
        withdrawn = services.correct_service_event(
            actor=people["technician"], event_id=original.pk, reason="x", void=True
        )
        with rejected(code="invalid_transition"):
            services.correct_service_event(
                actor=people["technician"], event_id=withdrawn.pk, reason="y", description="B"
            )

    def test_unknown_entry_refused(self, people):
        with rejected():
            services.correct_service_event(
                actor=people["technician"],
                event_id="00000000-0000-0000-0000-000000000000",
                reason="x",
            )
        with rejected():
            services.correct_service_event(
                actor=people["technician"], event_id="not-a-uuid", reason="x"
            )

    def test_correction_keeps_the_original_attachment_when_none_is_given(self, people, tank):
        body = b"%PDF-1.7 synthetic certificate"
        original = service(people["technician"], tank, attachment=pdf(body=body))
        fixed = services.correct_service_event(
            actor=people["technician"], event_id=original.pk, reason="x", description="Added text"
        )
        kept = Attachment.objects.with_data().get(pk=fixed.attachment_id)
        assert kept.pk == original.attachment_id  # the same stored file, not a copy
        assert kept.name == "certificate.pdf"
        assert kept.sha256 == hashlib.sha256(body).hexdigest()
        assert bytes(kept.data) == body
        assert Attachment.objects.count() == 1

    def test_void_keeps_the_original_attachment(self, people, tank):
        body = b"%PDF-1.7 synthetic certificate"
        original = service(people["technician"], tank, attachment=pdf(body=body))
        withdrawn = services.correct_service_event(
            actor=people["technician"], event_id=original.pk, reason="x", void=True
        )
        assert withdrawn.attachment_id == original.attachment_id
        assert bytes(Attachment.objects.with_data().get(pk=withdrawn.attachment_id).data) == body

    def test_correction_may_replace_the_attachment_and_alone_counts_as_a_change(self, people, tank):
        original = service(people["technician"], tank, attachment=pdf())
        new_body = b"%PDF-1.7 replacement"
        fixed = services.correct_service_event(
            actor=people["technician"],
            event_id=original.pk,
            reason="Scanned the signed copy",
            attachment=pdf("signed.pdf", new_body),
        )
        assert fixed.attachment.name == "signed.pdf"
        assert fixed.attachment.sha256 == hashlib.sha256(new_body).hexdigest()
        assert fixed.attachment_id != original.attachment_id

    def test_bad_replacement_attachment_refused(self, people, tank):
        original = service(people["technician"], tank, attachment=pdf())
        with rejected("attachment"):
            services.correct_service_event(
                actor=people["technician"],
                event_id=original.pk,
                reason="x",
                attachment=SimpleUploadedFile("x.pdf", b"nope"),
            )
        assert ServiceEvent.objects.count() == 1


# ------------------------------------------------------------------ LN2 log


class TestLN2:
    @pytest.mark.parametrize("make", ["freezer", "monitor", "other"])
    def test_only_ln2_kinds_keep_a_log(self, people, make):
        item = register_equipment(people["manager"], code=f"X-{make}", kind=make)
        with rejected("equipment"):
            level(people["technician"], item, level_unit="cm")

    def test_supply_dewar_keeps_a_log(self, people, dewar):
        entry = level(people["technician"], dewar, level_unit="%", value="80")
        assert entry.equipment_id == dewar.pk

    def test_level_check_needs_a_level(self, people, tank):
        with rejected("level", "required"):
            services.record_ln2(
                actor=people["technician"], equipment_id=tank.pk, kind="level", occurred_at=ago(5)
            )

    @pytest.mark.parametrize(
        "extra",
        [
            {"amount_added": Decimal("10"), "amount_unit": "L"},
            {"level_before": Decimal("10")},
            {"supply": "dewar"},
        ],
        ids=["amount", "before", "supply"],
    )
    def test_level_check_refuses_fill_fields(self, people, tank, dewar, extra):
        extra = {k: (dewar if v == "dewar" else v) for k, v in extra.items()}
        with rejected("level_before"):
            level(people["technician"], tank, **extra)
        assert not LN2Entry.objects.exists()

    def test_fill_needs_level_after_or_amount(self, people, tank):
        with rejected("level", "required"):
            services.record_ln2(
                actor=people["technician"], equipment_id=tank.pk, kind="fill", occurred_at=ago(5)
            )
        with rejected("level"):
            services.record_ln2(
                actor=people["technician"],
                equipment_id=tank.pk,
                kind="fill",
                occurred_at=ago(5),
                level_before=Decimal("10"),
            )

    def test_fill_with_amount_only_is_recorded(self, people, tank, dewar):
        entry = services.record_ln2(
            actor=people["technician"],
            equipment_id=tank.pk,
            kind="fill",
            occurred_at=ago(5),
            amount_added=Decimal("120.50"),
            amount_unit="L",
            supply=dewar,
            reference="Delivery note DN-0001",
        )
        assert entry.level is None and entry.level_unit == ""
        assert entry.amount_added == Decimal("120.50") and entry.supply == dewar

    def test_fill_with_level_after_below_before_refused(self, people, tank):
        with rejected("level"):
            services.record_ln2(
                actor=people["technician"],
                equipment_id=tank.pk,
                kind="fill",
                occurred_at=ago(5),
                level_before=Decimal("30"),
                level=Decimal("29.99"),
            )

    def test_fill_with_equal_levels_is_allowed(self, people, tank):
        entry = services.record_ln2(
            actor=people["technician"],
            equipment_id=tank.pk,
            kind="fill",
            occurred_at=ago(5),
            level_before=Decimal("30"),
            level=Decimal("30"),
        )
        assert entry.level == entry.level_before

    def test_percent_over_100_refused(self, people, tank):
        with rejected("level", "invalid"):
            level(people["technician"], tank, value="101", level_unit="%")
        with rejected("level_before"):
            services.record_ln2(
                actor=people["technician"],
                equipment_id=tank.pk,
                kind="fill",
                occurred_at=ago(5),
                level_before=Decimal("101"),
                level=Decimal("100"),
                level_unit="%",
            )

    def test_one_hundred_percent_is_allowed(self, people, tank):
        assert level(people["technician"], tank, value="100", level_unit="%").level == 100

    def test_negative_level_refused(self, people, tank):
        with rejected("level"):
            level(people["technician"], tank, value="-1")

    def test_level_unit_defaults_from_the_tank(self, people, tank):
        assert level(people["technician"], tank).level_unit == "cm"

    def test_level_unit_may_be_overridden_per_entry(self, people, tank):
        assert level(people["technician"], tank, level_unit="in").level_unit == "in"

    def test_level_unit_needed_when_the_vessel_has_none(self, people, dewar):
        with rejected("level_unit", "required"):
            level(people["technician"], dewar)

    def test_unknown_level_unit_refused(self, people, tank):
        with rejected("level_unit"):
            level(people["technician"], tank, level_unit="furlong")

    def test_amount_needs_a_unit(self, people, tank):
        with rejected("amount_unit", "required"):
            services.record_ln2(
                actor=people["technician"],
                equipment_id=tank.pk,
                kind="fill",
                occurred_at=ago(5),
                amount_added=Decimal("5"),
            )

    @pytest.mark.parametrize("amount", ["0", "-3"])
    def test_amount_must_be_positive(self, people, tank, amount):
        with rejected("amount_added"):
            services.record_ln2(
                actor=people["technician"],
                equipment_id=tank.pk,
                kind="fill",
                occurred_at=ago(5),
                amount_added=Decimal(amount),
                amount_unit="L",
            )

    def test_unknown_amount_unit_refused(self, people, tank):
        with rejected("amount_unit"):
            services.record_ln2(
                actor=people["technician"],
                equipment_id=tank.pk,
                kind="fill",
                occurred_at=ago(5),
                amount_added=Decimal("5"),
                amount_unit="gal",
            )

    def test_supply_must_be_a_supply_dewar(self, people, tank, monitor, freezer):
        for other in (monitor, freezer):
            with rejected("supply"):
                services.record_ln2(
                    actor=people["technician"],
                    equipment_id=tank.pk,
                    kind="fill",
                    occurred_at=ago(5),
                    level=Decimal("40"),
                    supply=other,
                )

    def test_a_vessel_cannot_be_filled_from_itself(self, people, dewar):
        with rejected("supply"):
            services.record_ln2(
                actor=people["technician"],
                equipment_id=dewar.pk,
                kind="fill",
                occurred_at=ago(5),
                level=Decimal("40"),
                level_unit="%",
                supply=dewar,
            )

    def test_retired_supply_dewar_refused_for_new_entries(self, people, tank, dewar):
        set_status(people["manager"], dewar, "retired")
        with rejected("supply"):
            services.record_ln2(
                actor=people["technician"],
                equipment_id=tank.pk,
                kind="fill",
                occurred_at=ago(5),
                level=Decimal("40"),
                supply=dewar,
            )

    @pytest.mark.parametrize("field", ["level", "level_before", "amount_added"])
    def test_floats_refused(self, people, tank, field):
        values = {"level": Decimal("40"), "amount_unit": "L"}
        values[field] = 12.5
        with rejected(field, "invalid"):
            services.record_ln2(
                actor=people["technician"],
                equipment_id=tank.pk,
                kind="fill",
                occurred_at=ago(5),
                **values,
            )

    def test_three_decimals_refused(self, people, tank):
        with rejected("level", "invalid"):
            level(people["technician"], tank, value="31.505")

    def test_back_dated_entry_allowed_future_refused(self, people, tank):
        old = level(people["technician"], tank, minutes=60 * 24 * 30)
        assert old.occurred_at < ago(60 * 24 * 29) and old.recorded_at > old.occurred_at
        with rejected("occurred_at", "invalid_time"):
            services.record_ln2(
                actor=people["technician"],
                equipment_id=tank.pk,
                kind="level",
                occurred_at=timezone.now() + timedelta(minutes=1),
                level=Decimal("30"),
            )

    def test_naive_time_refused(self, people, tank):
        with rejected("occurred_at", "invalid_time"):
            services.record_ln2(
                actor=people["technician"],
                equipment_id=tank.pk,
                kind="level",
                occurred_at=datetime(2026, 1, 1, 8, 0),
                level=Decimal("30"),
            )

    def test_unknown_kind_refused(self, people, tank):
        with rejected("kind"):
            level(people["technician"], tank, kind="top-up")

    def test_correction_replaces_values_and_keeps_kind(self, people, tank):
        original = level(people["technician"], tank, value="31.5")
        fixed = services.correct_ln2(
            actor=people["technician"],
            entry_id=original.pk,
            reason="Misread dipstick",
            level=Decimal("13.5"),
            kind="fill",  # ignored
        )
        assert fixed.kind == "level" and fixed.level == Decimal("13.5")
        assert fixed.corrects == original and fixed.correction_reason == "Misread dipstick"
        assert LN2Entry.objects.get(pk=original.pk).level == Decimal("31.5")
        assert audit("equipment.level_log_entry_corrected").get().after["ln2_entry"] == str(
            fixed.pk
        )

    def test_correction_may_drop_the_unit_pair_consistently(self, people, tank, dewar):
        original = services.record_ln2(
            actor=people["technician"],
            equipment_id=tank.pk,
            kind="fill",
            occurred_at=ago(20),
            level=Decimal("40"),
            amount_added=Decimal("10"),
            amount_unit="L",
            supply=dewar,
        )
        fixed = services.correct_ln2(
            actor=people["technician"], entry_id=original.pk, reason="No supply", supply=None
        )
        assert fixed.supply is None

    def test_correction_may_keep_a_supply_dewar_retired_since(self, people, tank, dewar):
        original = services.record_ln2(
            actor=people["technician"],
            equipment_id=tank.pk,
            kind="fill",
            occurred_at=ago(20),
            level=Decimal("40"),
            supply=dewar,
        )
        set_status(people["manager"], dewar, "retired")
        fixed = services.correct_ln2(
            actor=people["technician"], entry_id=original.pk, reason="Add note", note="Checked"
        )
        assert fixed.supply == dewar

    def test_correction_needs_a_reason_and_a_change(self, people, tank):
        original = level(people["technician"], tank)
        with rejected("reason", "required"):
            services.correct_ln2(
                actor=people["technician"], entry_id=original.pk, reason="", level=Decimal("1")
            )
        with rejected(code="invalid_transition"):
            services.correct_ln2(
                actor=people["technician"],
                entry_id=original.pk,
                reason="Same",
                level=Decimal("31.50"),
            )

    def test_correction_restating_the_same_number_with_other_trailing_zeros_is_a_no_op(
        self, people, tank
    ):
        original = level(people["technician"], tank, value="31.5")
        with rejected(code="invalid_transition"):
            services.correct_ln2(
                actor=people["technician"],
                entry_id=original.pk,
                reason="Same",
                level=Decimal("31.5"),
            )

    def test_correction_is_validated_like_a_new_entry(self, people, tank):
        original = level(people["technician"], tank, level_unit="%", value="50")
        with rejected("level", "invalid"):
            services.correct_ln2(
                actor=people["technician"],
                entry_id=original.pk,
                reason="Typo",
                level=Decimal("500"),
            )
        with rejected("level_before"):
            services.correct_ln2(
                actor=people["technician"],
                entry_id=original.pk,
                reason="Typo",
                level_before=Decimal("1"),
            )

    def test_void_withdraws_and_cannot_be_corrected_again(self, people, tank):
        original = level(people["technician"], tank)
        withdrawn = services.correct_ln2(
            actor=people["manager"], entry_id=original.pk, reason="Wrong tank", void=True
        )
        assert withdrawn.voided and withdrawn.level == original.level
        assert audit("equipment.level_log_entry_withdrawn").count() == 1
        with rejected(code="invalid_transition"):
            services.correct_ln2(
                actor=people["manager"], entry_id=withdrawn.pk, reason="x", level=Decimal("1")
            )
        with rejected(code="invalid_transition"):
            services.correct_ln2(
                actor=people["manager"], entry_id=original.pk, reason="x", level=Decimal("1")
            )

    def test_corrections_on_retired_equipment_refuse_invalid_state_only_via_new_entries(
        self, people, tank
    ):
        # A correction replaces an old row; retirement stops new entries, not fixing history.
        original = level(people["technician"], tank)
        set_status(people["manager"], tank, "retired")
        fixed = services.correct_ln2(
            actor=people["manager"], entry_id=original.pk, reason="Typo", level=Decimal("30")
        )
        assert fixed.level == Decimal("30")


# ------------------------------------------------------------------ temperature readings


class TestReadings:
    def test_monitor_equipment_refused(self, people, monitor):
        with rejected("equipment"):
            reading(people["technician"], monitor)
        assert not TemperatureReading.objects.exists()

    def test_device_must_be_a_monitor(self, people, tank, dewar):
        with rejected("device"):
            reading(people["technician"], tank, device=dewar)
        recorded = reading(people["technician"], tank, device=None)
        assert recorded.device is None

    def test_monitor_device_accepted(self, people, tank, monitor):
        assert reading(people["technician"], tank, device=monitor).device == monitor

    def test_retired_monitor_refused_as_device(self, people, tank, monitor):
        set_status(people["manager"], monitor, "retired")
        with rejected("device"):
            reading(people["technician"], tank, device=monitor)

    def test_reading_is_recorded_with_exact_value_and_audit(self, people, tank):
        recorded = reading(people["technician"], tank, value="-185.10", probe_position="Top")
        recorded.refresh_from_db()
        assert recorded.value == Decimal("-185.10") and recorded.unit == "°C"
        assert recorded.source == "manual" and recorded.batch is None
        assert audit("equipment.reading_recorded").get().actor == people["technician"]

    def test_float_refused(self, people, tank):
        with rejected("value", "invalid"):
            reading(people["technician"], tank, value=-185.1)

    @pytest.mark.parametrize("value", ["-185.123", "-0.001"])
    def test_three_decimals_refused(self, people, tank, value):
        with rejected("value", "invalid"):
            reading(people["technician"], tank, value=value)

    def test_value_below_absolute_zero_refused(self, people, tank):
        with rejected("value"):
            reading(people["technician"], tank, value="-273.16")
        assert reading(people["technician"], tank, value="-273.15").value == Decimal("-273.15")

    def test_value_required(self, people, tank):
        with rejected("value", "required"):
            services.record_reading(
                actor=people["technician"], equipment_id=tank.pk, measured_at=ago(5)
            )

    def test_future_time_refused(self, people, tank):
        with rejected("measured_at", "invalid_time"):
            reading(people["technician"], tank, measured_at=timezone.now() + timedelta(minutes=1))

    def test_duplicate_time_and_probe_refused(self, people, tank):
        at = ago(60)
        reading(people["technician"], tank, measured_at=at, probe_position="Top")
        with rejected("measured_at", "unique"):
            reading(people["technician"], tank, value="-170", measured_at=at, probe_position="Top")
        assert TemperatureReading.objects.count() == 1

    def test_same_time_other_probe_or_other_equipment_is_fine(self, people, tank, freezer):
        at = ago(60)
        reading(people["technician"], tank, measured_at=at, probe_position="Top")
        reading(people["technician"], tank, measured_at=at, probe_position="Bottom")
        reading(people["technician"], tank, measured_at=at, probe_position="")
        reading(people["technician"], freezer, measured_at=at, probe_position="Top", value="-80")
        assert TemperatureReading.objects.count() == 4

    def test_correction_may_reuse_the_original_slot(self, people, tank):
        at = ago(60)
        original = reading(people["technician"], tank, measured_at=at, probe_position="Top")
        fixed = services.correct_reading(
            actor=people["technician"],
            reading_id=original.pk,
            reason="Transcription error",
            value=Decimal("-181.00"),
        )
        assert (fixed.measured_at, fixed.probe_position) == (at, "Top")
        assert fixed.corrects == original and fixed.value == Decimal("-181.00")

    def test_second_effective_reading_at_a_corrected_slot_refused(self, people, tank):
        at = ago(60)
        original = reading(people["technician"], tank, measured_at=at, probe_position="Top")
        services.correct_reading(
            actor=people["technician"], reading_id=original.pk, reason="x", value=Decimal("-181")
        )
        with rejected("measured_at"):
            reading(people["technician"], tank, measured_at=at, probe_position="Top")

    def test_slot_of_a_withdrawn_reading_can_be_entered_again(self, people, tank):
        at = ago(60)
        original = reading(people["technician"], tank, measured_at=at, probe_position="Top")
        services.correct_reading(
            actor=people["technician"], reading_id=original.pk, reason="x", void=True
        )
        again = reading(
            people["technician"], tank, value="-170", measured_at=at, probe_position="Top"
        )
        assert again.value == Decimal("-170.00") and again.corrects is None

    def test_correction_moving_a_reading_frees_its_old_slot_and_takes_the_new_one(
        self, people, tank
    ):
        t1, t2 = ago(90), ago(80)
        original = reading(people["technician"], tank, measured_at=t1, probe_position="Top")
        services.correct_reading(
            actor=people["technician"],
            reading_id=original.pk,
            reason="Wrong time",
            measured_at=t2,
        )
        # T1 is free again: a new reading there is accepted.
        at_t1 = reading(
            people["technician"], tank, value="-171", measured_at=t1, probe_position="Top"
        )
        assert at_t1.measured_at == t1
        # T2 is taken by the correction.
        with rejected("measured_at", "unique"):
            reading(people["technician"], tank, value="-172", measured_at=t2, probe_position="Top")
        assert (
            TemperatureReading.objects.filter(
                equipment=tank, corrected_by__isnull=True, voided=False
            ).count()
            == 2
        )

    def test_correction_moving_onto_another_effective_reading_refused(self, people, tank):
        taken = ago(60)
        reading(people["technician"], tank, measured_at=taken, probe_position="Top")
        other = reading(people["technician"], tank, minutes=90, probe_position="Top")
        with rejected("measured_at", "unique"):
            services.correct_reading(
                actor=people["technician"],
                reading_id=other.pk,
                reason="Wrong time",
                measured_at=taken,
            )

    def test_correction_may_move_to_a_free_time(self, people, tank):
        original = reading(people["technician"], tank, minutes=90)
        moved = ago(80)
        fixed = services.correct_reading(
            actor=people["technician"],
            reading_id=original.pk,
            reason="Wrong time",
            measured_at=moved,
        )
        assert fixed.measured_at == moved

    def test_correction_cannot_name_a_non_monitor_device(self, people, tank, dewar):
        original = reading(people["technician"], tank)
        with rejected("device"):
            services.correct_reading(
                actor=people["technician"], reading_id=original.pk, reason="x", device=dewar
            )

    def test_correction_with_no_change_refused(self, people, tank):
        original = reading(people["technician"], tank, value="-180.00")
        with rejected(code="invalid_transition"):
            services.correct_reading(
                actor=people["technician"],
                reading_id=original.pk,
                reason="Same",
                value=Decimal("-180.00"),
            )

    def test_void_withdraws_the_reading(self, people, tank):
        original = reading(people["technician"], tank, probe_position="Top", note="n")
        withdrawn = services.correct_reading(
            actor=people["technician"], reading_id=original.pk, reason="Wrong tank", void=True
        )
        assert withdrawn.voided and withdrawn.value == original.value
        assert (withdrawn.probe_position, withdrawn.note) == ("Top", "n")
        with rejected(code="invalid_transition"):
            services.correct_reading(
                actor=people["technician"], reading_id=withdrawn.pk, reason="x", note="y"
            )

    def test_limits_snapshot_is_copied_at_record_time(self, people, tank):
        recorded = reading(people["technician"], tank, value="-140.00")
        assert (recorded.lower_limit, recorded.upper_limit) == (
            Decimal("-196.00"),
            Decimal("-150.00"),
        )
        edit(
            people["manager"], tank, lower_limit=Decimal("-190.00"), upper_limit=Decimal("-100.00")
        )
        recorded.refresh_from_db()
        assert (recorded.lower_limit, recorded.upper_limit) == (
            Decimal("-196.00"),
            Decimal("-150.00"),
        )
        assert recorded.outside_limits == "above"
        later = reading(people["technician"], tank, value="-140.00", minutes=30)
        assert (later.lower_limit, later.upper_limit) == (Decimal("-190.00"), Decimal("-100.00"))
        assert later.outside_limits == ""

    def test_correction_keeps_the_limits_recorded_with_the_original(self, people, tank):
        original = reading(people["technician"], tank, value="-140.00")
        edit(people["manager"], tank, upper_limit=Decimal("-100.00"))
        fixed = services.correct_reading(
            actor=people["technician"],
            reading_id=original.pk,
            reason="Misread",
            value=Decimal("-141.00"),
        )
        assert (fixed.lower_limit, fixed.upper_limit) == (Decimal("-196.00"), Decimal("-150.00"))
        assert fixed.outside_limits == "above"

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("-149.99", "above"),
            ("-150.00", ""),
            ("-196.00", ""),
            ("-196.01", "below"),
            ("-170", ""),
        ],
    )
    def test_outside_limits_above_and_below(self, people, tank, value, expected):
        assert reading(people["technician"], tank, value=value).outside_limits == expected

    def test_no_limits_means_never_outside(self, people):
        loose = register_equipment(people["manager"], code="FZ-LOOSE", kind="freezer")
        assert reading(people["technician"], loose, value="20.00").outside_limits == ""


# ------------------------------------------------------------------ CSV import


HEADER = "measured_at,temperature_c,probe_position\n"


def iso(minutes):
    return timezone.localtime(ago(minutes)).isoformat()


def validate(actor, text, equipment=None, name="readings.csv"):
    return services.validate_reading_import(
        actor=actor, file=csv_file(text, name), equipment_id=getattr(equipment, "pk", equipment)
    )


class TestImport:
    def test_valid_file_is_normalized_and_nothing_recorded_yet(self, people, tank):
        batch = validate(
            people["technician"],
            HEADER + f"{iso(30)},-182.00,Top\n{iso(20)},-181.50,Bottom\n",
            tank,
        )
        assert batch.is_valid and batch.errors == [] and batch.row_count == 2
        assert [r["equipment_code"] for r in batch.normalized_rows] == ["TANK-VP-01"] * 2
        assert [r["value"] for r in batch.normalized_rows] == ["-182.00", "-181.50"]
        assert [r["row"] for r in batch.normalized_rows] == [2, 3]
        assert batch.equipment == tank and batch.expires_at > timezone.now()
        assert not TemperatureReading.objects.exists()
        assert audit("equipment.import_validated").get().actor == people["technician"]

    def test_sha256_covers_the_uploaded_bytes(self, people, tank):
        text = HEADER + "2026-01-15T08:30:00+00:00,-182.00,Top\n"
        batch = validate(people["technician"], text, tank)
        assert batch.sha256 == hashlib.sha256(text.encode()).hexdigest()
        assert batch.filename == "readings.csv"

    @pytest.mark.parametrize(
        "header, missing",
        [
            ("temperature_c,probe_position\n-180.00,Top\n", "measured_at"),
            ("measured_at,probe_position\n2026-01-15T08:30:00Z,Top\n", "temperature_c"),
        ],
    )
    def test_required_columns(self, people, tank, header, missing):
        with rejected("file"):
            validate(people["technician"], header, tank)
        assert not ReadingImport.objects.exists()

    def test_header_matching_ignores_case_and_spaces(self, people, tank):
        batch = validate(
            people["technician"], f" Measured_At , TEMPERATURE_C \n{iso(10)},-180.00\n", tank
        )
        assert batch.is_valid

    def test_equipment_needed_from_column_or_form(self, people, tank):
        with rejected("equipment", "required"):
            validate(people["technician"], HEADER + f"{iso(10)},-180.00,Top\n")

    def test_unknown_form_equipment_refused(self, people):
        with rejected("equipment"):
            validate(
                people["technician"],
                HEADER + f"{iso(10)},-180.00,Top\n",
                "00000000-0000-0000-0000-000000000000",
            )

    def test_equipment_column_is_matched_whatever_the_case(self, people, tank, freezer):
        text = (
            "equipment,measured_at,temperature_c\n"
            f"tank-vp-01,{iso(30)},-180.00\n"
            f"FZ-01,{iso(30)},-80.00\n"
        )
        batch = validate(people["technician"], text)
        assert batch.is_valid
        assert {r["equipment_code"] for r in batch.normalized_rows} == {"TANK-VP-01", "FZ-01"}

    def test_column_overrides_the_form_equipment_and_blank_cell_uses_it(
        self, people, tank, freezer
    ):
        text = f"equipment,measured_at,temperature_c\nFZ-01,{iso(30)},-80.00\n,{iso(30)},-180.00\n"
        batch = validate(people["technician"], text, tank)
        assert [r["equipment_code"] for r in batch.normalized_rows] == ["FZ-01", "TANK-VP-01"]

    def test_device_column_names_a_monitor(self, people, tank, monitor):
        text = f"measured_at,temperature_c,device\n{iso(30)},-180.00,mon-01\n"
        batch = validate(people["technician"], text, tank)
        assert batch.normalized_rows[0]["device"] == str(monitor.pk)

    def test_unknown_columns_warn(self, people, tank):
        batch = validate(
            people["technician"], f"measured_at,temperature_c,colour\n{iso(30)},-180.00,red\n", tank
        )
        assert batch.is_valid and any("colour" in w for w in batch.warnings)

    def test_blank_lines_are_skipped(self, people, tank):
        batch = validate(people["technician"], HEADER + f"{iso(30)},-180.00,Top\n,,\n\n", tank)
        assert batch.row_count == 1 and batch.is_valid

    def test_a_file_with_only_a_header_is_not_valid(self, people, tank):
        batch = validate(people["technician"], HEADER, tank)
        assert batch.row_count == 0 and not batch.is_valid
        with rejected(code="invalid_import"):
            services.commit_reading_import(
                actor=people["technician"], batch_id=batch.pk, reason="Empty"
            )

    @pytest.mark.parametrize(
        "cell, field",
        [
            ("yesterday", "measured_at"),
            ("2026-13-45T10:00:00", "measured_at"),
            ("", "measured_at"),
        ],
    )
    def test_bad_time_is_a_row_error(self, people, tank, cell, field):
        batch = validate(people["technician"], HEADER + f"{cell},-180.00,Top\n", tank)
        assert not batch.is_valid and batch.normalized_rows == []
        assert batch.errors[0]["row"] == 2 and batch.errors[0]["field"] == field

    def test_future_time_is_a_row_error(self, people, tank):
        future = (timezone.localtime() + timedelta(hours=1)).isoformat()
        batch = validate(people["technician"], HEADER + f"{future},-180.00,Top\n", tank)
        assert batch.errors[0]["field"] == "measured_at" and batch.normalized_rows == []

    @pytest.mark.parametrize("cell", ["abc", "", "-180.123", "-273.16", "nan", "1e400"])
    def test_bad_number_is_a_row_error(self, people, tank, cell):
        batch = validate(people["technician"], HEADER + f"{iso(30)},{cell},Top\n", tank)
        assert batch.errors[0]["field"] == "temperature_c"
        assert batch.normalized_rows == [] and not batch.is_valid

    def test_unknown_asset_id_is_a_row_error(self, people, tank):
        text = f"equipment,measured_at,temperature_c\nNOPE-1,{iso(30)},-180.00\n"
        batch = validate(people["technician"], text)
        assert batch.errors == [
            {"row": 2, "field": "equipment", "message": batch.errors[0]["message"]}
        ]
        assert "NOPE-1" in batch.errors[0]["message"] and batch.normalized_rows == []

    def test_monitor_row_is_a_row_error(self, people, tank, monitor):
        text = f"equipment,measured_at,temperature_c\nMON-01,{iso(30)},-180.00\n"
        batch = validate(people["technician"], text)
        assert batch.errors[0]["field"] == "equipment" and "monitor" in batch.errors[0]["message"]

    def test_retired_equipment_row_is_a_row_error(self, people, tank, dewar):
        set_status(people["manager"], dewar, "retired")
        text = f"equipment,measured_at,temperature_c\nDEWAR-01,{iso(30)},-180.00\n"
        batch = validate(people["technician"], text)
        assert batch.errors[0]["field"] == "equipment" and "retired" in batch.errors[0]["message"]

    def test_retired_form_equipment_is_a_row_error(self, people, tank):
        set_status(people["manager"], tank, "retired")
        batch = validate(people["technician"], HEADER + f"{iso(30)},-180.00,Top\n", tank)
        assert not batch.is_valid and batch.errors[0]["field"] == "equipment"

    @pytest.mark.parametrize("device", ["NOPE", "TANK-VP-01"])
    def test_device_that_is_not_a_monitor_is_a_row_error(self, people, tank, device):
        text = f"measured_at,temperature_c,device\n{iso(30)},-180.00,{device}\n"
        batch = validate(people["technician"], text, tank)
        assert batch.errors[0]["field"] == "device" and batch.normalized_rows == []

    def test_duplicate_in_file_is_a_row_error_on_the_second_row(self, people, tank):
        at = iso(30)
        batch = validate(
            people["technician"], HEADER + f"{at},-180.00,Top\n{at},-179.00,Top\n", tank
        )
        assert [e["row"] for e in batch.errors] == [3]
        assert batch.normalized_rows == []

    def test_same_time_different_probe_in_file_is_fine(self, people, tank):
        at = iso(30)
        batch = validate(
            people["technician"], HEADER + f"{at},-180.00,Top\n{at},-179.00,Bottom\n", tank
        )
        assert batch.is_valid

    def test_duplicate_against_recorded_reading_is_a_row_error(self, people, tank):
        at = ago(30)
        reading(people["technician"], tank, measured_at=at, probe_position="Top")
        text = HEADER + f"{timezone.localtime(at).isoformat()},-180.00,Top\n"
        batch = validate(people["technician"], text, tank)
        assert batch.errors[0]["field"] == "measured_at" and batch.normalized_rows == []

    def test_one_bad_row_blocks_the_whole_file(self, people, tank):
        batch = validate(
            people["technician"], HEADER + f"{iso(30)},-180.00,Top\n{iso(20)},oops,Top\n", tank
        )
        assert batch.normalized_rows == [] and [e["row"] for e in batch.errors] == [3]
        assert batch.row_count == 1  # the rows that were fine are counted, none is kept

    def test_all_row_errors_are_reported_not_just_the_first(self, people, tank):
        batch = validate(
            people["technician"], HEADER + "x,-180.00,Top\n" + f"{iso(5)},y,Top\n", tank
        )
        assert [(e["row"], e["field"]) for e in batch.errors] == [
            (2, "measured_at"),
            (3, "temperature_c"),
        ]

    def test_naive_time_is_read_in_the_deployment_time_zone(self, people, tank):
        batch = validate(people["technician"], HEADER + "2026-01-15T08:30:00,-180.00,Top\n", tank)
        parsed = datetime.fromisoformat(batch.normalized_rows[0]["measured_at"])
        expected = datetime(2026, 1, 15, 8, 30, tzinfo=ZoneInfo(settings.TIME_ZONE))
        assert parsed == expected and parsed.utcoffset() == expected.utcoffset()

    def test_summer_naive_time_uses_the_summer_offset(self, people, tank):
        batch = validate(people["technician"], HEADER + "2026-07-15T08:30:00,-180.00,Top\n", tank)
        parsed = datetime.fromisoformat(batch.normalized_rows[0]["measured_at"])
        assert parsed == datetime(2026, 7, 15, 8, 30, tzinfo=ZoneInfo(settings.TIME_ZONE))

    def test_offset_time_is_kept(self, people, tank):
        batch = validate(
            people["technician"], HEADER + "2026-01-15T08:30:00+02:00,-180.00,Top\n", tank
        )
        parsed = datetime.fromisoformat(batch.normalized_rows[0]["measured_at"])
        assert parsed == datetime(2026, 1, 15, 6, 30, tzinfo=UTC)
        assert parsed.utcoffset() == timedelta(hours=2)

    def test_outside_limit_rows_warn(self, people, tank):
        batch = validate(
            people["technician"],
            HEADER + f"{iso(30)},-140.00,Top\n{iso(20)},-180.00,Top\n{iso(10)},-200.00,Top\n",
            tank,
        )
        assert batch.is_valid
        assert any(w.startswith("2 reading(s) are outside") for w in batch.warnings)

    def test_no_warning_when_every_row_is_inside(self, people, tank):
        batch = validate(people["technician"], HEADER + f"{iso(30)},-180.00,Top\n", tank)
        assert batch.warnings == []

    def test_previously_imported_identical_file_warns(self, people, tank):
        text = HEADER + "2026-01-15T08:30:00+00:00,-180.00,Top\n"
        first = validate(people["technician"], text, tank)
        assert not any("imported before" in w for w in first.warnings)
        services.commit_reading_import(
            actor=people["technician"], batch_id=first.pk, reason="Logger download"
        )
        again = validate(people["technician"], text, tank)
        assert any("imported before" in w for w in again.warnings)
        assert not again.is_valid  # its rows are now duplicates of the recorded readings

    def test_uncommitted_validation_does_not_count_as_imported(self, people, tank):
        text = HEADER + "2026-01-15T08:30:00+00:00,-180.00,Top\n"
        validate(people["technician"], text, tank)
        again = validate(people["technician"], text, tank)
        assert not any("imported before" in w for w in again.warnings)

    def test_non_utf8_empty_and_oversize_files_refused(self, people, tank):
        bad = io.BytesIO(b"\xff\xfe measured_at")
        bad.name, bad.size = "x.csv", 12
        with rejected("file"):
            services.validate_reading_import(
                actor=people["technician"], file=bad, equipment_id=tank.pk
            )
        with rejected("file"):
            validate(people["technician"], "", tank)
        with rejected("file"):
            services.validate_reading_import(
                actor=people["technician"], file=None, equipment_id=tank.pk
            )
        huge = io.BytesIO(b"measured_at,temperature_c\n" + b"0" * (5 * 1024 * 1024))
        huge.name = "x.csv"
        with rejected("file"):
            services.validate_reading_import(
                actor=people["technician"], file=huge, equipment_id=tank.pk
            )

    def test_utf8_byte_order_mark_is_accepted(self, people, tank):
        data = io.BytesIO("﻿".encode() + f"measured_at,temperature_c\n{iso(10)},-180.00\n".encode())
        data.name = "bom.csv"
        batch = services.validate_reading_import(
            actor=people["technician"], file=data, equipment_id=tank.pk
        )
        assert batch.is_valid

    def test_row_count_cap(self, people, tank, monkeypatch):
        monkeypatch.setattr(services, "IMPORT_MAX_ROWS", 2)
        three = HEADER + "".join(f"{iso(60 - n)},-180.00,P{n}\n" for n in range(3))
        with rejected("file"):
            validate(people["technician"], three, tank)
        assert not ReadingImport.objects.exists()
        two = HEADER + "".join(f"{iso(60 - n)},-180.00,P{n}\n" for n in range(2))
        assert validate(people["technician"], two, tank).is_valid

    # --- commit

    def test_commit_records_every_row_as_an_import_with_the_batch(self, people, tank, monitor):
        at = [ago(40), ago(30), ago(20)]
        text = (
            "measured_at,temperature_c,probe_position,device,note\n"
            f"{at[0].isoformat()},-182.00,Top,MON-01,first\n"
            f"{at[1].isoformat()},-140.25,Top,,\n"
            f"{at[2].isoformat()},-181.00,Bottom,,\n"
        )
        batch = validate(people["technician"], text, tank)
        commit = services.commit_reading_import(
            actor=people["manager"], batch_id=batch.pk, reason="Logger download 2026-11"
        )
        assert commit.reading_count == 3 and commit.batch == batch
        assert commit.actor == people["manager"] and commit.reason == "Logger download 2026-11"
        rows = list(TemperatureReading.objects.filter(batch=batch).order_by("measured_at"))
        assert [r.source for r in rows] == ["import"] * 3
        assert [r.value for r in rows] == [
            Decimal("-182.00"),
            Decimal("-140.25"),
            Decimal("-181.00"),
        ]
        assert [r.measured_at for r in rows] == at
        assert all(r.recorded_by == people["manager"] for r in rows)
        assert rows[0].device == monitor and rows[0].note == "first"
        assert (rows[1].lower_limit, rows[1].upper_limit) == (
            Decimal("-196.00"),
            Decimal("-150.00"),
        )
        assert rows[1].outside_limits == "above"
        entry = audit("equipment.import_committed").get()
        assert entry.reason == "Logger download 2026-11" and entry.after["readings"] == 3

    def test_commit_needs_a_reason(self, people, tank):
        batch = validate(people["technician"], HEADER + f"{iso(30)},-180.00,Top\n", tank)
        with rejected("reason", "required"):
            services.commit_reading_import(actor=people["technician"], batch_id=batch.pk, reason="")
        assert not ReadingImportCommit.objects.exists()

    def test_commit_twice_refused(self, people, tank):
        batch = validate(people["technician"], HEADER + f"{iso(30)},-180.00,Top\n", tank)
        services.commit_reading_import(actor=people["technician"], batch_id=batch.pk, reason="One")
        with rejected(code="invalid_transition"):
            services.commit_reading_import(
                actor=people["technician"], batch_id=batch.pk, reason="Two"
            )
        assert TemperatureReading.objects.count() == 1

    def test_commit_of_an_invalid_batch_refused(self, people, tank):
        batch = validate(people["technician"], HEADER + "oops,-180.00,Top\n", tank)
        with rejected(code="invalid_import"):
            services.commit_reading_import(
                actor=people["technician"], batch_id=batch.pk, reason="Try"
            )
        assert not TemperatureReading.objects.exists()

    def test_expired_batch_refused(self, people, tank):
        fresh = validate(people["technician"], HEADER + f"{iso(30)},-180.00,Top\n", tank)
        stale = ReadingImport.objects.create(
            actor=people["technician"],
            filename="old.csv",
            sha256="0" * 64,
            row_count=fresh.row_count,
            normalized_rows=fresh.normalized_rows,
            errors=[],
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        with rejected(code="invalid_import"):
            services.commit_reading_import(
                actor=people["technician"], batch_id=stale.pk, reason="Late"
            )
        assert not TemperatureReading.objects.exists()

    def test_unknown_batch_refused(self, people):
        with rejected():
            services.commit_reading_import(
                actor=people["technician"],
                batch_id="00000000-0000-0000-0000-000000000000",
                reason="x",
            )

    def test_reading_recorded_after_validation_makes_commit_fail_whole(self, people, tank):
        first, second = ago(40), ago(30)
        text = HEADER + f"{first.isoformat()},-180.00,Top\n{second.isoformat()},-180.00,Top\n"
        batch = validate(people["technician"], text, tank)
        reading(people["technician"], tank, measured_at=second, probe_position="Top")
        with rejected(code="invalid_import"):
            services.commit_reading_import(
                actor=people["technician"], batch_id=batch.pk, reason="Race"
            )
        assert TemperatureReading.objects.count() == 1  # nothing from the file got in
        assert not ReadingImportCommit.objects.exists()

    def test_equipment_retired_after_validation_makes_commit_fail(self, people, tank):
        batch = validate(people["technician"], HEADER + f"{iso(30)},-180.00,Top\n", tank)
        set_status(people["manager"], tank, "retired")
        with rejected(code="invalid_import"):
            services.commit_reading_import(
                actor=people["technician"], batch_id=batch.pk, reason="Late"
            )
        assert not TemperatureReading.objects.exists()

    def test_limits_in_force_at_commit_are_kept_with_the_readings(self, people, tank):
        batch = validate(people["technician"], HEADER + f"{iso(30)},-140.00,Top\n", tank)
        edit(people["manager"], tank, upper_limit=Decimal("-100.00"))
        services.commit_reading_import(actor=people["technician"], batch_id=batch.pk, reason="Go")
        recorded = TemperatureReading.objects.get()
        assert recorded.upper_limit == Decimal("-100.00") and recorded.outside_limits == ""

    def test_imported_readings_can_be_corrected_and_keep_their_source(self, people, tank):
        batch = validate(people["technician"], HEADER + f"{iso(30)},-180.00,Top\n", tank)
        services.commit_reading_import(actor=people["technician"], batch_id=batch.pk, reason="Go")
        original = TemperatureReading.objects.get()
        fixed = services.correct_reading(
            actor=people["manager"],
            reading_id=original.pk,
            reason="Logger drift",
            value=Decimal("-179.00"),
        )
        assert fixed.source == "import" and fixed.batch == batch


# ------------------------------------------------------------------ excursions


def open_ex(actor, equipment, **over):
    values = {"started_at": ago(300), "detection": "reading"}
    values.update(over)
    return services.open_excursion(actor=actor, equipment_id=equipment.pk, **values)


def details(**over):
    values = {
        "ended_at": ago(250),
        "cause": "Lid left ajar",
        "action": "Lid closed, level checked",
        "impact": "Top boxes exposed",
        "cause_category": "door",
    }
    values.update(over)
    return values


def in_review(people, equipment, by="technician", **over):
    actor = people[by]
    ex = open_ex(actor, equipment, **over)
    ex = services.update_excursion(
        actor=actor, excursion_id=ex.pk, expected_revision=ex.revision, **details()
    )
    return services.submit_excursion(actor=actor, excursion_id=ex.pk, expected_revision=ex.revision)


def close(actor, ex, note="Reviewed"):
    ex.refresh_from_db()
    return services.close_excursion(
        actor=actor, excursion_id=ex.pk, expected_revision=ex.revision, note=note
    )


def reopen(actor, ex, reason="New evidence"):
    ex.refresh_from_db()
    return services.reopen_excursion(
        actor=actor, excursion_id=ex.pk, expected_revision=ex.revision, reason=reason
    )


def timeline(ex, kind):
    return list(ex.events.filter(kind=kind))


class TestExcursions:
    def test_open_snapshots_unit_and_limits_and_starts_the_timeline(self, people, world, tank):
        ex = open_ex(
            people["technician"], tank, extreme_value=Decimal("-140.25"), note="Alarm at 03:10"
        )
        assert ex.status == "open" and ex.revision == 1 and ex.number == f"EX-{ex.pk:06d}"
        assert ex.storage_unit_id == world.unit.pk
        assert (ex.lower_limit, ex.upper_limit) == (Decimal("-196.00"), Decimal("-150.00"))
        assert ex.opened_by == people["technician"] and ex.cause_category == "unknown"
        opened = ex.events.get()
        assert opened.kind == "opened" and opened.note == "Alarm at 03:10"
        assert opened.actor == people["technician"]
        entry = audit("excursion.opened").get()
        assert entry.after["storage_unit"] == str(world.unit.pk)

    def test_snapshots_do_not_follow_later_equipment_changes(self, people, world, tank):
        ex = open_ex(people["technician"], tank)
        edit(
            people["manager"], tank, lower_limit=Decimal("-190.00"), upper_limit=Decimal("-100.00")
        )
        ex.refresh_from_db()
        assert (ex.lower_limit, ex.upper_limit) == (Decimal("-196.00"), Decimal("-150.00"))
        assert ex.storage_unit_id == world.unit.pk

    def test_monitor_refused(self, people, monitor):
        with rejected("equipment"):
            open_ex(people["technician"], monitor)

    def test_detection_required_and_checked(self, people, tank):
        with rejected("detection"):
            open_ex(people["technician"], tank, detection="")
        with rejected("detection"):
            open_ex(people["technician"], tank, detection="rumour")

    def test_cause_category_checked(self, people, tank):
        with rejected("cause_category"):
            open_ex(people["technician"], tank, cause_category="gremlins")

    def test_end_before_start_refused(self, people, tank):
        with rejected("ended_at", "invalid_time"):
            open_ex(people["technician"], tank, started_at=ago(100), ended_at=ago(200))
        assert not Excursion.objects.exists()

    def test_end_equal_to_start_is_allowed(self, people, tank):
        at = ago(100)
        assert open_ex(people["technician"], tank, started_at=at, ended_at=at).ended_at == at

    @pytest.mark.parametrize("field", ["started_at", "ended_at", "extreme_at"])
    def test_future_times_refused(self, people, tank, field):
        values = {field: timezone.now() + timedelta(minutes=5)}
        if field != "started_at":
            values["started_at"] = ago(10)
        with rejected(field, "invalid_time"):
            open_ex(people["technician"], tank, **values)

    def test_naive_start_refused(self, people, tank):
        with rejected("started_at", "invalid_time"):
            open_ex(people["technician"], tank, started_at=datetime(2026, 1, 1, 8, 0))

    @pytest.mark.parametrize("offset", [-1, 101])
    def test_extreme_time_outside_the_period_refused(self, people, tank, offset):
        start, end = ago(300), ago(200)
        at = start + timedelta(minutes=offset)
        with rejected("extreme_at", "invalid_time"):
            open_ex(people["technician"], tank, started_at=start, ended_at=end, extreme_at=at)

    def test_extreme_time_on_the_boundaries_and_inside_is_allowed(self, people, tank):
        start, end = ago(300), ago(200)
        for at in (start, start + timedelta(minutes=50), end):
            ex = open_ex(people["technician"], tank, started_at=start, ended_at=end, extreme_at=at)
            assert ex.extreme_at == at

    def test_extreme_time_while_still_open_ended_needs_only_to_follow_the_start(self, people, tank):
        assert open_ex(people["technician"], tank, extreme_at=ago(10)).extreme_at
        with rejected("extreme_at"):
            open_ex(people["technician"], tank, extreme_at=ago(400))

    def test_extreme_value_is_an_exact_decimal(self, people, tank):
        with rejected("extreme_value", "invalid"):
            open_ex(people["technician"], tank, extreme_value=-140.25)
        with rejected("extreme_value", "invalid"):
            open_ex(people["technician"], tank, extreme_value=Decimal("-140.255"))
        with rejected("extreme_value"):
            open_ex(people["technician"], tank, extreme_value=Decimal("-300"))

    # --- update

    def test_update_records_what_changed_in_the_timeline(self, people, tank):
        ex = open_ex(people["technician"], tank)
        updated = services.update_excursion(
            actor=people["technician"],
            excursion_id=ex.pk,
            expected_revision=1,
            cause="Lid left ajar",
            cause_category="door",
        )
        assert updated.revision == 2 and updated.cause == "Lid left ajar"
        event = timeline(updated, "updated")[0]
        assert event.changes == {
            "cause": ["", "Lid left ajar"],
            "cause_category": ["unknown", "door"],
        }
        assert event.actor == people["technician"]
        entry = audit("excursion.updated").get()
        assert entry.before["cause"] == "" and entry.after["cause"] == "Lid left ajar"

    def test_update_changes_for_times_are_recorded_as_before_after_pairs(self, people, tank):
        start = ago(300)
        ex = open_ex(people["technician"], tank, started_at=start)
        end = ago(250)
        services.update_excursion(
            actor=people["technician"], excursion_id=ex.pk, expected_revision=1, ended_at=end
        )
        changes = timeline(ex, "updated")[0].changes
        assert list(changes) == ["ended_at"]
        assert changes["ended_at"][0] is None and changes["ended_at"][1] is not None

    def test_update_that_changes_nothing_refused(self, people, tank):
        ex = open_ex(people["technician"], tank, detection="reading")
        with rejected(code="invalid_transition"):
            services.update_excursion(
                actor=people["technician"],
                excursion_id=ex.pk,
                expected_revision=1,
                detection="reading",
            )
        assert not timeline(ex, "updated")

    def test_update_with_stale_revision_refused(self, people, tank):
        ex = open_ex(people["technician"], tank)
        services.update_excursion(
            actor=people["technician"], excursion_id=ex.pk, expected_revision=1, cause="A"
        )
        with rejected(code="stale_revision"):
            services.update_excursion(
                actor=people["technician"], excursion_id=ex.pk, expected_revision=1, cause="B"
            )
        ex.refresh_from_db()
        assert ex.cause == "A"

    def test_update_is_validated_against_the_stored_values(self, people, tank):
        ex = open_ex(people["technician"], tank, started_at=ago(300))
        with rejected("ended_at", "invalid_time"):
            services.update_excursion(
                actor=people["technician"],
                excursion_id=ex.pk,
                expected_revision=1,
                ended_at=ago(400),
            )
        with rejected("extreme_at"):
            services.update_excursion(
                actor=people["technician"],
                excursion_id=ex.pk,
                expected_revision=1,
                extreme_at=ago(400),
            )

    def test_update_restating_the_extreme_with_other_trailing_zeros_is_a_no_op(self, people, tank):
        ex = open_ex(people["technician"], tank, extreme_value=Decimal("-140.20"))
        with rejected(code="invalid_transition"):
            services.update_excursion(
                actor=people["technician"],
                excursion_id=ex.pk,
                expected_revision=ex.revision,
                extreme_value=Decimal("-140.2"),
            )

    def test_update_does_not_change_the_snapshot_or_the_unit(self, people, world, tank):
        ex = open_ex(people["technician"], tank)
        edit(people["manager"], tank, upper_limit=Decimal("-100.00"))
        ex = services.update_excursion(
            actor=people["technician"], excursion_id=ex.pk, expected_revision=1, cause="A"
        )
        assert ex.upper_limit == Decimal("-150.00") and ex.storage_unit_id == world.unit.pk

    @pytest.mark.parametrize("state", ["review", "closed"])
    def test_update_only_while_open(self, people, tank, state):
        ex = in_review(people, tank)
        if state == "closed":
            ex = close(people["manager"], ex)
        with rejected(code="invalid_transition"):
            services.update_excursion(
                actor=people["technician"],
                excursion_id=ex.pk,
                expected_revision=ex.revision,
                cause="Changed afterwards",
            )

    # --- submit

    def test_submit_needs_end_cause_action_and_impact(self, people, tank):
        ex = open_ex(people["technician"], tank)
        with pytest.raises(ValidationError) as info:
            services.submit_excursion(
                actor=people["technician"], excursion_id=ex.pk, expected_revision=1
            )
        assert set(field_errors(info.value)) == {"ended_at", "cause", "action", "impact"}
        ex.refresh_from_db()
        assert ex.status == "open" and ex.revision == 1

    @pytest.mark.parametrize("missing", ["ended_at", "cause", "action", "impact"])
    def test_submit_names_only_what_is_missing(self, people, tank, missing):
        ex = open_ex(people["technician"], tank)
        values = details()
        values[missing] = None if missing == "ended_at" else "   "
        ex = services.update_excursion(
            actor=people["technician"],
            excursion_id=ex.pk,
            expected_revision=1,
            **{k: v for k, v in values.items() if v is not None},
        )
        if missing == "ended_at":
            ex.ended_at = None
            ex.save(update_fields=["ended_at"])
        with pytest.raises(ValidationError) as info:
            services.submit_excursion(
                actor=people["technician"], excursion_id=ex.pk, expected_revision=ex.revision
            )
        assert set(field_errors(info.value)) == {missing}

    def test_submit_moves_to_review_and_records_who(self, people, tank):
        ex = in_review(people, tank)
        assert ex.status == "review" and ex.submitted_by == people["technician"]
        assert ex.submitted_at is not None
        assert timeline(ex, "submitted")[0].actor == people["technician"]
        assert audit("excursion.submitted").get().actor == people["technician"]

    def test_submit_twice_refused(self, people, tank):
        ex = in_review(people, tank)
        with rejected(code="invalid_transition"):
            services.submit_excursion(
                actor=people["technician"], excursion_id=ex.pk, expected_revision=ex.revision
            )

    def test_submit_with_stale_revision_refused(self, people, tank):
        ex = open_ex(people["technician"], tank)
        services.update_excursion(
            actor=people["technician"], excursion_id=ex.pk, expected_revision=1, **details()
        )
        with rejected(code="stale_revision"):
            services.submit_excursion(
                actor=people["technician"], excursion_id=ex.pk, expected_revision=1
            )

    # --- return

    def test_return_needs_a_note(self, people, tank):
        ex = in_review(people, tank)
        with rejected("note", "required"):
            services.return_excursion(
                actor=people["manager"], excursion_id=ex.pk, expected_revision=ex.revision, note=" "
            )
        ex.refresh_from_db()
        assert ex.status == "review"

    def test_return_reopens_for_work_and_clears_the_submission(self, people, tank):
        ex = in_review(people, tank)
        returned = services.return_excursion(
            actor=people["manager"],
            excursion_id=ex.pk,
            expected_revision=ex.revision,
            note="Add the level log",
        )
        assert returned.status == "open"
        assert returned.submitted_by is None and returned.submitted_at is None
        event = timeline(returned, "returned")[0]
        assert event.note == "Add the level log" and event.actor == people["manager"]
        assert audit("excursion.returned").get().reason == "Add the level log"
        # and it can be edited and submitted again
        services.update_excursion(
            actor=people["technician"],
            excursion_id=ex.pk,
            expected_revision=returned.revision,
            impact="Revised impact",
        )

    @pytest.mark.parametrize("state", ["open", "closed"])
    def test_return_only_from_review(self, people, tank, state):
        ex = open_ex(people["technician"], tank) if state == "open" else in_review(people, tank)
        if state == "closed":
            ex = close(people["manager"], ex)
        with rejected(code="invalid_transition"):
            services.return_excursion(
                actor=people["manager"], excursion_id=ex.pk, expected_revision=ex.revision, note="x"
            )

    # --- close

    def test_close_by_the_submitter_refused(self, people, tank):
        ex = in_review(people, tank, by="manager")
        with rejected(code="self_review"):
            close(people["manager"], ex)
        ex.refresh_from_db()
        assert ex.status == "review" and not AffectedSpecimen.objects.exists()
        assert not timeline(ex, "closed")

    def test_admin_who_submitted_cannot_close_either(self, people, tank):
        ex = in_review(people, tank, by="admin")
        with rejected(code="self_review"):
            close(people["admin"], ex)

    def test_close_by_another_manager_or_admin(self, people, tank):
        first = in_review(people, tank, by="manager")
        closed = close(people["admin"], first)
        assert closed.status == "closed" and closed.closed_by == people["admin"]
        assert closed.closed_at and closed.closure_note == "Reviewed"
        assert closed.submitted_by == people["manager"]

    def test_close_needs_a_note(self, people, tank):
        ex = in_review(people, tank)
        with rejected("note", "required"):
            close(people["manager"], ex, note="")

    @pytest.mark.parametrize("state", ["open", "closed"])
    def test_close_only_from_review(self, people, tank, state):
        if state == "open":
            ex = open_ex(people["technician"], tank)
        else:
            ex = close(people["manager"], in_review(people, tank))
        with rejected(code="invalid_transition"):
            close(people["admin"], ex)

    def test_close_with_stale_revision_refused(self, people, tank):
        ex = in_review(people, tank)
        with rejected(code="stale_revision"):
            services.close_excursion(
                actor=people["manager"], excursion_id=ex.pk, expected_revision=1, note="x"
            )

    def test_close_captures_the_specimens_in_the_unit_during_the_period(self, people, world, tank):
        tech = people["technician"]
        inside = register(tech, world, occurred_at=ago(270))
        place(tech, inside, world.position("A1"), occurred_at=ago(260))
        late = register(tech, world, occurred_at=ago(270))
        place(tech, late, world.position("A2"), occurred_at=ago(100))  # after the excursion
        ex = in_review(people, tank)
        closed = close(people["manager"], ex)
        rows = list(AffectedSpecimen.objects.filter(excursion=closed))
        assert [row.specimen_id for row in rows] == [inside.pk]
        closing = timeline(closed, "closed")
        assert len(closing) == 1 and closing[0].changes == {"affected_specimens": 1}
        assert closing[0].actor == people["manager"] and closing[0].note == "Reviewed"
        assert rows[0].capture_id == closing[0].pk
        assert "BOX-001" in rows[0].location
        assert rows[0].present_from <= rows[0].present_until
        assert audit("excursion.closed").get().after["affected_specimens"] == 1

    def test_close_with_nothing_in_the_unit_captures_none(self, people, tank):
        closed = close(people["manager"], in_review(people, tank))
        assert not AffectedSpecimen.objects.exists()
        assert timeline(closed, "closed")[0].changes == {"affected_specimens": 0}

    def test_equipment_without_a_storage_unit_closes_with_no_specimens(self, people):
        loose = register_equipment(people["manager"], code="FZ-LOOSE", kind="freezer")
        ex = in_review(people, loose)
        assert ex.storage_unit_id is None
        closed = close(people["manager"], ex)
        assert closed.status == "closed"
        assert not AffectedSpecimen.objects.exists()

    # --- reopen

    def test_reopen_needs_a_reason(self, people, tank):
        ex = close(people["manager"], in_review(people, tank))
        with rejected("reason", "required"):
            reopen(people["manager"], ex, reason="")

    @pytest.mark.parametrize("state", ["open", "review"])
    def test_reopen_only_a_closed_excursion(self, people, tank, state):
        ex = open_ex(people["technician"], tank) if state == "open" else in_review(people, tank)
        with rejected(code="invalid_transition"):
            reopen(people["manager"], ex)

    def test_reopen_clears_submission_and_closure(self, people, tank):
        ex = close(people["manager"], in_review(people, tank))
        reopened = reopen(people["admin"], ex, reason="Probe was out of calibration")
        assert reopened.status == "open"
        assert reopened.submitted_by is None and reopened.submitted_at is None
        assert reopened.closed_by is None and reopened.closed_at is None
        assert reopened.closure_note == ""
        event = timeline(reopened, "reopened")[0]
        assert event.note == "Probe was out of calibration" and event.actor == people["admin"]
        assert audit("excursion.reopened").get().reason == "Probe was out of calibration"
        # the details are kept and it can be edited again
        assert reopened.cause == "Lid left ajar"
        services.update_excursion(
            actor=people["technician"],
            excursion_id=ex.pk,
            expected_revision=reopened.revision,
            impact="Revised",
        )

    def test_reclose_after_reopen_makes_a_second_capture_and_leaves_the_first(
        self, people, world, tank
    ):
        tech = people["technician"]
        first = register(tech, world, occurred_at=ago(272))
        place(tech, first, world.position("A1"), occurred_at=ago(270))
        ex = in_review(people, tank)
        close(people["manager"], ex)
        before = list(
            AffectedSpecimen.objects.values_list(
                "pk", "specimen_id", "capture_id", "present_from", "present_until", "location"
            )
        )
        assert len(before) == 1
        # a movement recorded afterwards for a time inside the excursion adds to the list
        second = register(tech, world, occurred_at=ago(272))
        place(tech, second, world.position("A2"), occurred_at=ago(255))
        reopen(people["admin"], ex)
        ex.refresh_from_db()
        services.submit_excursion(actor=tech, excursion_id=ex.pk, expected_revision=ex.revision)
        closed = close(people["admin"], ex, note="Second review")
        captures = timeline(closed, "closed")
        assert len(captures) == 2
        first_capture, second_capture = captures
        assert first_capture.changes == {"affected_specimens": 1}
        assert second_capture.changes == {"affected_specimens": 2}
        rows = AffectedSpecimen.objects.filter(excursion=closed)
        assert rows.count() == 3
        assert rows.filter(capture=first_capture).count() == 1
        assert rows.filter(capture=second_capture).count() == 2
        assert (
            list(
                AffectedSpecimen.objects.filter(capture=first_capture).values_list(
                    "pk", "specimen_id", "capture_id", "present_from", "present_until", "location"
                )
            )
            == before
        )

    # --- notes

    def test_note_needs_text_or_attachment(self, people, tank):
        ex = open_ex(people["technician"], tank)
        with rejected("note", "required"):
            services.add_excursion_note(actor=people["technician"], excursion_id=ex.pk, note="  ")
        assert not timeline(ex, "note")

    def test_note_with_text_only(self, people, tank):
        ex = open_ex(people["technician"], tank)
        event = services.add_excursion_note(
            actor=people["technician"], excursion_id=ex.pk, note="Called the supplier"
        )
        assert event.kind == "note" and event.note == "Called the supplier"
        assert not event.has_attachment
        assert audit("excursion.note_added").get().after["event"] == str(event.pk)

    def test_note_with_attachment_only(self, people, tank):
        ex = open_ex(people["technician"], tank)
        body = b"%PDF-1.7 synthetic alarm log"
        event = services.add_excursion_note(
            actor=people["technician"], excursion_id=ex.pk, attachment=pdf("alarm.pdf", body)
        )
        stored = Attachment.objects.with_data().get(pk=event.attachment_id)
        assert stored.sha256 == hashlib.sha256(body).hexdigest()
        assert stored.size == len(body) and bytes(stored.data) == body
        assert "data" in ExcursionEvent.objects.get(pk=event.pk).attachment.get_deferred_fields()

    def test_bad_attachment_on_a_note_refused(self, people, tank):
        ex = open_ex(people["technician"], tank)
        with rejected("attachment"):
            services.add_excursion_note(
                actor=people["technician"],
                excursion_id=ex.pk,
                note="Photo",
                attachment=SimpleUploadedFile("photo.png", b"not a png"),
            )
        assert not timeline(ex, "note")

    @pytest.mark.parametrize("state", ["open", "review", "closed"])
    def test_notes_may_be_added_in_any_state_without_changing_it(self, people, tank, state):
        ex = open_ex(people["technician"], tank) if state == "open" else in_review(people, tank)
        if state == "closed":
            ex = close(people["manager"], ex)
        revision = Excursion.objects.get(pk=ex.pk).revision
        services.add_excursion_note(actor=people["technician"], excursion_id=ex.pk, note="Later")
        after = Excursion.objects.get(pk=ex.pk)
        assert (after.status, after.revision) == (
            state if state != "review" else "review",
            revision,
        )

    def test_unknown_excursion_refused(self, people):
        with rejected():
            services.add_excursion_note(actor=people["technician"], excursion_id=999999, note="x")
        with rejected():
            services.update_excursion(
                actor=people["technician"], excursion_id=999999, expected_revision=1, cause="x"
            )


def test_asset_id_is_fixed_once_entries_exist(people, tank):
    manager = people["manager"]
    tank = services.save_equipment(
        actor=manager,
        equipment_id=tank.pk,
        expected_revision=tank.revision,
        reason="Asset tag reprinted",
        code="TANK-VP-01A",
    )
    assert tank.code == "TANK-VP-01A"  # nothing recorded yet
    services.record_ln2(
        actor=people["technician"],
        equipment_id=tank.pk,
        kind="level",
        occurred_at=timezone.now() - timedelta(minutes=5),
        level=Decimal("30"),
    )
    with pytest.raises(ValidationError) as caught:
        services.save_equipment(
            actor=manager,
            equipment_id=tank.pk,
            expected_revision=tank.revision,
            reason="Rename",
            code="TANK-VP-02",
        )
    assert "code" in caught.value.message_dict
