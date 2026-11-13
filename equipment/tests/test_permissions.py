"""Who may do what in the equipment services (design note, Permissions table).

Each service is tried with every role. The matrix runs the roles of one service inside a single
test, undoing each attempt with a savepoint, so fixtures are built once per service. A refused
attempt must raise PermissionDenied and leave no row, no audit event and no changed record behind.
All data is synthetic.
"""

import uuid
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from core.models import AuditEvent
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

from .conftest import ago, csv_file, hours_ago, make_user, register_equipment

pytestmark = pytest.mark.django_db

ROLES = ("viewer", "technician", "manager", "admin", "nobody", "superuser_only")
OPERATE = {"technician", "manager", "admin"}
MANAGE = {"manager", "admin"}
MODELS = (
    Equipment,
    StatusChange,
    ServiceEvent,
    LN2Entry,
    TemperatureReading,
    ReadingImport,
    ReadingImportCommit,
    Excursion,
    ExcursionEvent,
    AffectedSpecimen,
    AuditEvent,
)
NOBODY_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@pytest.fixture(autouse=True)
def fast_password_hashing(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def snapshot():
    """Row counts of every equipment table plus the revisions and statuses of mutable records."""
    return (
        tuple(model.objects.count() for model in MODELS),
        tuple(Equipment.objects.order_by("pk").values_list("pk", "revision", "status")),
        tuple(Excursion.objects.order_by("pk").values_list("pk", "revision", "status")),
    )


@contextmanager
def rolled_back():
    savepoint = transaction.savepoint()
    try:
        yield
    finally:
        transaction.savepoint_rollback(savepoint)


@pytest.fixture
def actors(people):
    return {
        "viewer": people["viewer"],
        "technician": people["technician"],
        "manager": people["manager"],
        "admin": people["admin"],
        "nobody": people["nobody"],
        # Superuser status alone grants no application role.
        "superuser_only": make_user("sp-bare-superuser", superuser=True),
    }


def details(**over):
    values = {
        "ended_at": ago(250),
        "cause": "Lid left ajar",
        "action": "Lid closed",
        "impact": "Top boxes exposed",
    }
    values.update(over)
    return values


@pytest.fixture
def ctx(people, actors):
    """Equipment and one record of each kind, made by technician unless noted."""
    m, t, t2 = people["manager"], people["technician"], people["technician2"]
    tank = register_equipment(
        m,
        code="TANK-P-01",
        kind="ln2_tank",
        lower_limit=Decimal("-196.00"),
        upper_limit=Decimal("-150.00"),
        level_unit="cm",
    )
    dewar = register_equipment(m, code="DEWAR-P-01", kind="supply_dewar")
    service = services.record_service_event(
        actor=t,
        equipment_id=tank.pk,
        kind="calibration",
        performed_on=timezone.localdate(),
        outcome="acceptable",
        performed_by=t,
        next_due_on=timezone.localdate() + timedelta(days=30),
    )
    ln2 = services.record_ln2(
        actor=t, equipment_id=tank.pk, kind="level", occurred_at=ago(120), level=Decimal("30")
    )
    reading = services.record_reading(
        actor=t, equipment_id=tank.pk, measured_at=ago(110), value=Decimal("-180.00")
    )
    batch = services.validate_reading_import(
        actor=t,
        file=csv_file(f"measured_at,temperature_c\n{ago(90).isoformat()},-181.00\n"),
        equipment_id=tank.pk,
    )
    assert batch.is_valid
    ex_open = services.open_excursion(
        actor=t, equipment_id=tank.pk, started_at=ago(300), detection="reading"
    )
    ex_ready = services.open_excursion(
        actor=t, equipment_id=tank.pk, started_at=ago(300), detection="reading", **details()
    )
    ex_review = services.submit_excursion(
        actor=t2,
        excursion_id=services.open_excursion(
            actor=t2, equipment_id=tank.pk, started_at=ago(300), detection="reading", **details()
        ).pk,
        expected_revision=1,
    )
    closed = services.submit_excursion(
        actor=t2,
        excursion_id=services.open_excursion(
            actor=t2, equipment_id=tank.pk, started_at=ago(300), detection="reading", **details()
        ).pk,
        expected_revision=1,
    )
    ex_closed = services.close_excursion(
        actor=m, excursion_id=closed.pk, expected_revision=closed.revision, note="Reviewed"
    )
    return SimpleNamespace(
        tank=tank,
        dewar=dewar,
        service=service,
        ln2=ln2,
        reading=reading,
        batch=batch,
        ex_open=ex_open,
        ex_ready=ex_ready,
        ex_review=ex_review,
        ex_closed=ex_closed,
    )


# ------------------------------------------------------------------ the matrix

ACTIONS = {
    "save_equipment (register)": (
        lambda a, c: services.save_equipment(
            actor=a, code="FZ-NEW", name="Freezer", kind="freezer"
        ),
        MANAGE,
    ),
    "save_equipment (edit)": (
        lambda a, c: services.save_equipment(
            actor=a,
            equipment_id=c.dewar.pk,
            expected_revision=c.dewar.revision,
            reason="Relabelled",
            name="Supply dewar (renamed)",
        ),
        MANAGE,
    ),
    "change_status": (
        lambda a, c: services.change_status(
            actor=a,
            equipment_id=c.dewar.pk,
            expected_revision=c.dewar.revision,
            status="out_of_service",
            occurred_at=hours_ago(1),
            reason="Valve service",
        ),
        MANAGE,
    ),
    "record_service_event": (
        lambda a, c: services.record_service_event(
            actor=a,
            equipment_id=c.tank.pk,
            kind="maintenance",
            performed_on=timezone.localdate(),
            outcome="completed",
            performed_by_external="Example Cryo Service Ltd",
        ),
        OPERATE,
    ),
    "correct_service_event": (
        lambda a, c: services.correct_service_event(
            actor=a, event_id=c.service.pk, reason="Typo", description="Fixed"
        ),
        OPERATE,
    ),
    "void service event": (
        lambda a, c: services.correct_service_event(
            actor=a, event_id=c.service.pk, reason="Wrong tank", void=True
        ),
        OPERATE,
    ),
    "record_ln2": (
        lambda a, c: services.record_ln2(
            actor=a, equipment_id=c.tank.pk, kind="level", occurred_at=ago(5), level=Decimal("29")
        ),
        OPERATE,
    ),
    "correct_ln2": (
        lambda a, c: services.correct_ln2(
            actor=a, entry_id=c.ln2.pk, reason="Misread", level=Decimal("31")
        ),
        OPERATE,
    ),
    "void ln2": (
        lambda a, c: services.correct_ln2(
            actor=a, entry_id=c.ln2.pk, reason="Wrong tank", void=True
        ),
        OPERATE,
    ),
    "record_reading": (
        lambda a, c: services.record_reading(
            actor=a, equipment_id=c.tank.pk, measured_at=ago(5), value=Decimal("-175.00")
        ),
        OPERATE,
    ),
    "correct_reading": (
        lambda a, c: services.correct_reading(
            actor=a, reading_id=c.reading.pk, reason="Misread", value=Decimal("-179.00")
        ),
        OPERATE,
    ),
    "void reading": (
        lambda a, c: services.correct_reading(
            actor=a, reading_id=c.reading.pk, reason="Wrong tank", void=True
        ),
        OPERATE,
    ),
    "validate_reading_import": (
        lambda a, c: services.validate_reading_import(
            actor=a,
            file=csv_file(f"measured_at,temperature_c\n{ago(60).isoformat()},-170.00\n"),
            equipment_id=c.tank.pk,
        ),
        OPERATE,
    ),
    "commit_reading_import": (
        lambda a, c: services.commit_reading_import(
            actor=a, batch_id=c.batch.pk, reason="Download"
        ),
        OPERATE,
    ),
    "open_excursion": (
        lambda a, c: services.open_excursion(
            actor=a, equipment_id=c.tank.pk, started_at=ago(20), detection="alarm"
        ),
        OPERATE,
    ),
    "update_excursion": (
        lambda a, c: services.update_excursion(
            actor=a,
            excursion_id=c.ex_open.pk,
            expected_revision=c.ex_open.revision,
            cause="Door seal",
        ),
        OPERATE,
    ),
    "submit_excursion": (
        lambda a, c: services.submit_excursion(
            actor=a, excursion_id=c.ex_ready.pk, expected_revision=c.ex_ready.revision
        ),
        OPERATE,
    ),
    "add_excursion_note": (
        lambda a, c: services.add_excursion_note(
            actor=a, excursion_id=c.ex_open.pk, note="Supplier called"
        ),
        OPERATE,
    ),
    "return_excursion": (
        lambda a, c: services.return_excursion(
            actor=a,
            excursion_id=c.ex_review.pk,
            expected_revision=c.ex_review.revision,
            note="Add the level log",
        ),
        MANAGE,
    ),
    "close_excursion": (
        lambda a, c: services.close_excursion(
            actor=a,
            excursion_id=c.ex_review.pk,
            expected_revision=c.ex_review.revision,
            note="Reviewed",
        ),
        MANAGE,
    ),
    "reopen_excursion": (
        lambda a, c: services.reopen_excursion(
            actor=a,
            excursion_id=c.ex_closed.pk,
            expected_revision=c.ex_closed.revision,
            reason="New evidence",
        ),
        MANAGE,
    ),
}


@pytest.mark.parametrize("name", ACTIONS)
def test_role_matrix(ctx, actors, name):
    run, allowed = ACTIONS[name]
    for role in ROLES:
        with rolled_back():
            before = snapshot()
            if role in allowed:
                run(actors[role], ctx)  # any exception, even a ValidationError, is a failure
                assert snapshot() != before, f"{name} by {role} changed nothing"
                assert AuditEvent.objects.filter(actor=actors[role]).count() >= 1, (name, role)
            else:
                with pytest.raises(PermissionDenied):
                    run(actors[role], ctx)
                assert snapshot() == before, f"{name} by {role} left a trace"


def test_the_matrix_covers_every_mutating_service():
    mutating = {
        "save_equipment",
        "change_status",
        "record_service_event",
        "correct_service_event",
        "record_ln2",
        "correct_ln2",
        "record_reading",
        "correct_reading",
        "validate_reading_import",
        "commit_reading_import",
        "open_excursion",
        "update_excursion",
        "submit_excursion",
        "return_excursion",
        "close_excursion",
        "reopen_excursion",
        "add_excursion_note",
    }
    called = {name for run, _ in ACTIONS.values() for name in run.__code__.co_names}
    assert mutating - called == set()
    public = {
        name
        for name, value in vars(services).items()
        if callable(value)
        and getattr(value, "__module__", "") == "equipment.services"
        and not name.startswith("_")
        and name.split("_")[0]
        in {"save", "change", "record", "correct", "validate", "commit", "open", "update"}
        | {"submit", "return", "close", "reopen", "add"}
    }
    assert public - mutating == set(), "a new service needs a row in the matrix"


# ------------------------------------------------------------------ corrections: whose entries


def _make_service(actor, c):
    return services.record_service_event(
        actor=actor,
        equipment_id=c.tank.pk,
        kind="maintenance",
        performed_on=timezone.localdate(),
        outcome="completed",
        performed_by=actor,
    )


def _make_ln2(actor, c):
    return services.record_ln2(
        actor=actor, equipment_id=c.tank.pk, kind="level", occurred_at=ago(60), level=Decimal("30")
    )


def _make_reading(actor, c):
    return services.record_reading(
        actor=actor, equipment_id=c.tank.pk, measured_at=ago(60), value=Decimal("-180.00")
    )


def _fix_service(actor, entry):
    return services.correct_service_event(
        actor=actor, event_id=entry.pk, reason="Typo", description="Fixed"
    )


def _fix_ln2(actor, entry):
    return services.correct_ln2(actor=actor, entry_id=entry.pk, reason="Typo", level=Decimal("31"))


def _fix_reading(actor, entry):
    return services.correct_reading(
        actor=actor, reading_id=entry.pk, reason="Typo", value=Decimal("-179.00")
    )


def _void_service(actor, entry):
    return services.correct_service_event(
        actor=actor, event_id=entry.pk, reason="Wrong tank", void=True
    )


def _void_ln2(actor, entry):
    return services.correct_ln2(actor=actor, entry_id=entry.pk, reason="Wrong tank", void=True)


def _void_reading(actor, entry):
    return services.correct_reading(
        actor=actor, reading_id=entry.pk, reason="Wrong tank", void=True
    )


KINDS = {
    "service": (_make_service, _fix_service, _void_service),
    "ln2": (_make_ln2, _fix_ln2, _void_ln2),
    "reading": (_make_reading, _fix_reading, _void_reading),
}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("how", [1, 2], ids=["correct", "void"])
def test_who_may_correct_whose_entry(ctx, people, kind, how):
    make, *fixers = KINDS[kind]
    fix = fixers[how - 1]
    t, t2, m, a = people["technician"], people["technician2"], people["manager"], people["admin"]
    # (recorded by, corrected by, allowed)
    cases = [
        (t, t, True),  # a technician corrects their own entry
        (t, t2, False),  # not another technician's
        (m, t, False),  # nor a manager's
        (t, m, True),  # a manager corrects anyone's
        (t, a, True),
        (m, a, True),
        (a, m, True),
        (m, m, True),
        (t, people["viewer"], False),
        (t, people["nobody"], False),
    ]
    for owner, corrector, allowed in cases:
        with rolled_back():
            entry = make(owner, ctx)
            before = snapshot()
            if allowed:
                fixed = fix(corrector, entry)
                assert fixed.corrects_id == entry.pk and fixed.recorded_by == corrector
            else:
                with pytest.raises(PermissionDenied):
                    fix(corrector, entry)
                assert snapshot() == before, (owner.username, corrector.username)


@pytest.mark.parametrize("kind", KINDS)
def test_a_technician_cannot_correct_a_managers_correction_of_their_entry(ctx, people, kind):
    make, fix, _ = KINDS[kind]
    entry = make(people["technician"], ctx)
    newer = fix(people["manager"], entry)  # recorded by the manager
    with pytest.raises(PermissionDenied):
        fix(people["technician"], newer)


# ------------------------------------------------------------------ roles before records


@pytest.mark.parametrize(
    "name",
    [
        "correct_service_event",
        "correct_ln2",
        "correct_reading",
        "commit_reading_import",
        "update_excursion",
        "submit_excursion",
        "add_excursion_note",
    ],
)
@pytest.mark.parametrize("role", ["viewer", "nobody", "superuser_only"])
def test_role_is_checked_before_the_record_is_looked_up(actors, name, role):
    """A refused actor learns nothing about whether an id exists: PermissionDenied, never a
    'does not exist' validation error."""
    calls = {
        "correct_service_event": lambda a: services.correct_service_event(
            actor=a, event_id=NOBODY_ID, reason="x"
        ),
        "correct_ln2": lambda a: services.correct_ln2(actor=a, entry_id=NOBODY_ID, reason="x"),
        "correct_reading": lambda a: services.correct_reading(
            actor=a, reading_id=NOBODY_ID, reason="x"
        ),
        "commit_reading_import": lambda a: services.commit_reading_import(
            actor=a, batch_id=NOBODY_ID, reason="x"
        ),
        "update_excursion": lambda a: services.update_excursion(
            actor=a, excursion_id=999999, expected_revision=1, cause="x"
        ),
        "submit_excursion": lambda a: services.submit_excursion(
            actor=a, excursion_id=999999, expected_revision=1
        ),
        "add_excursion_note": lambda a: services.add_excursion_note(
            actor=a, excursion_id=999999, note="x"
        ),
    }
    with pytest.raises(PermissionDenied):
        calls[name](actors[role])


@pytest.mark.parametrize("name", ["change_status", "save_equipment", "return", "close", "reopen"])
def test_technician_is_refused_manager_actions_before_the_record_is_looked_up(people, name):
    t = people["technician"]
    calls = {
        "change_status": lambda: services.change_status(
            actor=t,
            equipment_id=NOBODY_ID,
            expected_revision=1,
            status="retired",
            occurred_at=hours_ago(1),
            reason="x",
        ),
        "save_equipment": lambda: services.save_equipment(
            actor=t, equipment_id=NOBODY_ID, expected_revision=1, reason="x", name="x"
        ),
        "return": lambda: services.return_excursion(
            actor=t, excursion_id=999999, expected_revision=1, note="x"
        ),
        "close": lambda: services.close_excursion(
            actor=t, excursion_id=999999, expected_revision=1, note="x"
        ),
        "reopen": lambda: services.reopen_excursion(
            actor=t, excursion_id=999999, expected_revision=1, reason="x"
        ),
    }
    with pytest.raises(PermissionDenied):
        calls[name]()


def test_an_inactive_user_with_a_role_is_refused(ctx, people):
    technician = people["technician"]
    technician.is_active = False
    technician.save()
    with pytest.raises(PermissionDenied):
        services.record_reading(
            actor=technician, equipment_id=ctx.tank.pk, measured_at=ago(5), value=Decimal("-180")
        )


# ------------------------------------------------------------------ four eyes on closing


def test_the_person_who_submitted_cannot_close_even_as_manager_or_admin(ctx, people):
    from django.core.exceptions import ValidationError

    for submitter in ("manager", "admin"):
        excursion = services.open_excursion(
            actor=people[submitter],
            equipment_id=ctx.tank.pk,
            started_at=ago(300),
            detection="reading",
            **details(),
        )
        excursion = services.submit_excursion(
            actor=people[submitter], excursion_id=excursion.pk, expected_revision=1
        )
        with pytest.raises(ValidationError) as info:
            services.close_excursion(
                actor=people[submitter],
                excursion_id=excursion.pk,
                expected_revision=excursion.revision,
                note="Self review",
            )
        assert "self_review" in [e.code for e in info.value.error_list]
        excursion.refresh_from_db()
        assert excursion.status == Excursion.Status.REVIEW


def test_someone_who_only_opened_or_edited_may_close_if_they_did_not_submit(ctx, people):
    """The separation is on the submission, as the design note states."""
    excursion = services.open_excursion(
        actor=people["manager"],
        equipment_id=ctx.tank.pk,
        started_at=ago(300),
        detection="reading",
        **details(),
    )
    excursion = services.submit_excursion(
        actor=people["technician"], excursion_id=excursion.pk, expected_revision=1
    )
    closed = services.close_excursion(
        actor=people["manager"],
        excursion_id=excursion.pk,
        expected_revision=excursion.revision,
        note="Reviewed",
    )
    assert closed.closed_by == people["manager"] and closed.opened_by == people["manager"]
