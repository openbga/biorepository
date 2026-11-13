"""Position claims through the internal helpers, capacity, free-position search, blocking and
retirement gates."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from core.models import AuditEvent
from storage import queries, services
from storage.models import MoveEvent, Occupancy, Position

from .conftest import ago, gate_revision, new_item, place
from .test_definitions import codes

pytestmark = pytest.mark.django_db


def cell(box, label):
    return Position.objects.get(box=box, label=label)


def test_helpers_refuse_to_run_outside_an_operation_holding_the_gate(people):
    with pytest.raises(RuntimeError):
        services.new_occupancy(actor=people["technician"], occurred_at=ago(5))
    with pytest.raises(RuntimeError):
        services.lock_gate()


def test_require_gate_is_the_public_guard_for_other_apps(people):
    with pytest.raises(RuntimeError):  # no operation
        services.require_gate()
    with services.operation():
        with pytest.raises(RuntimeError):  # operation, gate not locked yet
            services.require_gate()
        gate = services.lock_gate()
        assert services.require_gate() is gate and gate.pk == 1
        with services.operation():  # nested operations share the held gate
            assert services.require_gate() is gate
        services._require_gate()  # the private name keeps working for existing callers
    with pytest.raises(RuntimeError):  # the scope ends with the outermost operation
        services.require_gate()


def test_viewer_cannot_claim(tree, people):
    with pytest.raises(PermissionDenied):
        new_item(people["viewer"])


def test_place_relocate_check_out_check_in_and_dispose(tree, people):
    t = people["technician"]
    item = new_item(t, ago(120))
    assert item.position is None and item.revision == 1
    gate = gate_revision()
    event = place(t, item, cell(tree.box, "A1"), ago(100))
    item.refresh_from_db()
    assert item.position.label == "A1" and item.revision == 2
    assert gate_revision() == gate + 1
    member = event.members.get()
    assert (
        member.from_path == {}
        and member.to_path["display"] == "VP-01 / T-01 / T-01-S1 / BOX-001 / A1"
    )
    assert member.to_path["unit"]["nominal_temperature"] == "-160.00"
    assert member.to_path["unit"]["technology"] == "ln2_vapor"

    place(t, item, cell(tree.box, "B2"), ago(90), kind="relocate", expected_revision=2)
    place(t, item, None, ago(80), kind="check_out", expected_revision=3)
    item.refresh_from_db()
    assert item.position is None
    place(t, item, cell(tree.box, "A1"), ago(70), kind="check_in", expected_revision=4)
    place(t, item, None, ago(60), kind="dispose", expected_revision=5)
    item.refresh_from_db()
    assert item.position is None and item.revision == 6
    history = queries.movement_history(actor=people["viewer"], occupancy_id=item.pk)
    assert [m.event.kind for m in history] == [
        "place",
        "relocate",
        "check_out",
        "check_in",
        "dispose",
    ]
    assert history[1].from_path["position"]["label"] == "A1"
    actions = list(
        AuditEvent.objects.filter(object_id=str(item.pk)).values_list("action", flat=True)
    )
    assert actions[0] == "storage.occupancy_created"
    assert actions[-1] == "storage.occupancy_dispose"


def test_unplaced_item_may_be_disposed_without_a_position(tree, people):
    item = new_item(people["technician"])
    event = place(people["technician"], item, None, ago(10), kind="dispose")
    assert event.from_path == {} and event.to_path == {}


@pytest.mark.parametrize(
    "kind,first,target,code",
    [
        ("place", True, "B1", "invalid_transition"),  # already placed: relocate instead
        ("relocate", False, "B1", "invalid_transition"),  # not placed
        ("check_out", False, None, "invalid_transition"),
        ("relocate", True, "A1", "invalid_transition"),  # same position
    ],
)
def test_transition_shapes(tree, people, kind, first, target, code):
    t = people["technician"]
    item = new_item(t)
    if first:
        place(t, item, cell(tree.box, "A1"), ago(50))
        item.refresh_from_db()
    with pytest.raises(ValidationError) as exc:
        place(t, item, cell(tree.box, target) if target else None, ago(40), kind=kind)
    assert code in codes(exc.value)


def test_occupied_blocked_and_retired_targets_are_refused(tree, people):
    t, m = people["technician"], people["manager"]
    first, second = new_item(t), new_item(t)
    place(t, first, cell(tree.box, "A1"))
    with pytest.raises(ValidationError) as exc:
        place(t, second, cell(tree.box, "A1"))
    assert codes(exc.value) == {"occupied"}
    blocked = services.set_position_blocked(
        actor=m,
        position_id=cell(tree.box, "A2").pk,
        expected_revision=1,
        blocked=True,
        reason="Cracked well",
    )
    assert blocked.block_reason == "Cracked well" and blocked.revision == 2
    with pytest.raises(ValidationError) as exc:
        place(t, second, blocked)
    assert codes(exc.value) == {"inactive_location"}
    empty = services.create_box(
        actor=m,
        node_id=tree.shelf2.pk,
        code="BOX-EMPTY",
        name="",
        slot=1,
        container_type_id=tree.grid.pk,
    )
    services.retire_storage(
        actor=m, kind="box", object_id=empty.pk, expected_revision=1, reason="Damaged"
    )
    with pytest.raises(ValidationError) as exc:
        place(t, second, cell(empty, "A1"))
    assert codes(exc.value) == {"inactive_location"}


def test_database_constraint_is_the_last_line_for_one_item_per_position(tree, people):
    t = people["technician"]
    first, second = new_item(t), new_item(t)
    place(t, first, cell(tree.box, "A1"))
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        Occupancy.objects.filter(pk=second.pk).update(position=cell(tree.box, "A1"))


def test_chronology_and_staleness(tree, people):
    t = people["technician"]
    item = new_item(t, ago(60))
    with pytest.raises(ValidationError) as exc:
        place(t, item, cell(tree.box, "A1"), timezone.now() + timezone.timedelta(minutes=5))
    assert codes(exc.value) == {"invalid_time"}
    with pytest.raises(ValidationError) as exc:
        place(t, item, cell(tree.box, "A1"), ago(61))  # before registration
    assert codes(exc.value) == {"invalid_time"}
    place(t, item, cell(tree.box, "A1"), ago(30))
    with pytest.raises(ValidationError) as exc:
        place(t, item, cell(tree.box, "A2"), ago(20), kind="relocate", expected_revision=1)
    assert codes(exc.value) == {"stale_revision"}
    # Equal times are allowed (ordered by recorded time).
    item.refresh_from_db()
    place(
        t, item, cell(tree.box, "A2"), item.last_occurred_at, kind="relocate", expected_revision=2
    )


def test_touch_advances_watermark_and_revision_once_per_operation(tree, people):
    t = people["technician"]
    item = new_item(t, ago(60))
    with services.operation():
        services.lock_gate()
        services.change_occupancy(
            actor=t,
            occupancy_id=item.pk,
            expected_revision=1,
            position_id=cell(tree.box, "C3").pk,
            kind="place",
            occurred_at=ago(30),
            reason="Stored",
        )
        # A second helper in the same operation does not increase the revision again and
        # still accepts the revision the caller read before the operation.
        services.touch_occupancy(
            actor=t, occupancy_id=item.pk, expected_revision=1, occurred_at=ago(30), reason="x"
        )
    item.refresh_from_db()
    assert item.revision == 2
    touched = None
    with services.operation():
        services.lock_gate()
        touched = services.touch_occupancy(
            actor=t, occupancy_id=item.pk, expected_revision=2, occurred_at=ago(10), reason="Hold"
        )
    assert touched.revision == 3
    with pytest.raises(ValidationError) as exc, services.operation():
        services.lock_gate()
        services.touch_occupancy(
            actor=t, occupancy_id=item.pk, expected_revision=3, occurred_at=ago(20), reason="x"
        )
    assert codes(exc.value) == {"invalid_time"}


def test_item_correction_links_the_original_once(tree, people):
    t, m = people["technician"], people["manager"]
    item = new_item(t, ago(60))
    wrong = place(t, item, cell(tree.box, "A1"), ago(50))
    # Corrections are managers' and admins' only, whichever specimens action calls storage.
    for extra in ({"corrects_id": wrong.pk}, {}):
        with pytest.raises(PermissionDenied):
            place(t, item, cell(tree.box, "A3"), ago(5), kind="correction", **extra)
    fixed = place(
        m,
        item,
        cell(tree.box, "A3"),
        ago(5),
        kind="correction",
        expected_revision=2,
        corrects_id=wrong.pk,
        reason="Placed in A3, recorded as A1",
    )
    assert fixed.corrects == wrong and fixed.kind == "correction"
    with pytest.raises(ValidationError):
        place(
            m,
            item,
            cell(tree.box, "A4"),
            ago(1),
            kind="correction",
            expected_revision=3,
            corrects_id=wrong.pk,
        )
    with pytest.raises(ValidationError):  # correction needs corrects_id
        place(m, item, cell(tree.box, "A4"), ago(1), kind="correction", expected_revision=3)


def test_capacity_free_positions_and_occupants(tree, people):
    t, m, v = people["technician"], people["manager"], people["viewer"]
    services.set_position_blocked(
        actor=m,
        position_id=cell(tree.box, "A2").pk,
        expected_revision=1,
        blocked=True,
        reason="Damaged",
    )
    item = new_item(t)
    place(t, item, cell(tree.box, "A1"))
    expected = queries.Capacity(installed=81, blocked=1, usable=80, occupied=1, free=79)
    for kind, pk in (("unit", tree.unit.pk), ("node", tree.tower.pk), ("box", tree.box.pk)):
        assert queries.capacity(actor=v, kind=kind, object_id=pk) == expected
    assert queries.capacity(actor=v, kind="unit", object_id=tree.other_unit.pk).installed == 0
    free = queries.free_positions(actor=v, box_id=tree.box.pk, limit=3)
    assert [p.label for p in free] == ["A3", "A4", "A5"]
    page2 = queries.free_positions(actor=v, unit_id=tree.unit.pk, offset=3, limit=2)
    assert [p.label for p in page2] == ["A6", "A7"]
    with pytest.raises(ValidationError):
        queries.free_positions(actor=v, limit=500)
    with pytest.raises(ValidationError):
        queries.free_positions(actor=v, unit_id=tree.unit.pk, box_id=tree.box.pk)
    (occupant,) = queries.occupants(actor=v, kind="node", object_id=tree.tower.pk)
    assert occupant.occupancy_id == item.pk and occupant.path["position"]["label"] == "A1"
    assert queries.locations(actor=v, occupancy_ids=[item.pk])[item.pk]["box"]["code"] == "BOX-001"
    with pytest.raises(PermissionDenied):
        queries.capacity(actor=people["nobody"], kind="box", object_id=tree.box.pk)


def test_retired_structure_is_not_free_capacity(tree, people):
    m = people["manager"]
    box = services.create_box(
        actor=m,
        node_id=tree.rack.pk,
        code="BOX-R1",
        name="",
        slot=1,
        container_type_id=tree.grid.pk,
    )
    v = people["viewer"]
    assert queries.capacity(actor=v, kind="unit", object_id=tree.other_unit.pk).free == 81
    services.retire_storage(
        actor=m, kind="box", object_id=box.pk, expected_revision=1, reason="Old"
    )
    assert queries.capacity(actor=v, kind="unit", object_id=tree.other_unit.pk).installed == 0
    assert not queries.free_positions(actor=v, unit_id=tree.other_unit.pk)


def test_blocking_rules(tree, people):
    t, m = people["technician"], people["manager"]
    item = new_item(t)
    place(t, item, cell(tree.box, "A1"))
    with pytest.raises(ValidationError) as exc:
        services.set_position_blocked(
            actor=m,
            position_id=cell(tree.box, "A1").pk,
            expected_revision=1,
            blocked=True,
            reason="x",
        )
    assert codes(exc.value) == {"occupied"}
    with pytest.raises(ValidationError) as exc:
        services.set_position_blocked(
            actor=m,
            position_id=cell(tree.box, "A2").pk,
            expected_revision=1,
            blocked=False,
            reason="x",
        )
    assert codes(exc.value) == {"invalid_transition"}
    with pytest.raises(PermissionDenied):
        services.set_position_blocked(
            actor=t,
            position_id=cell(tree.box, "A2").pk,
            expected_revision=1,
            blocked=True,
            reason="x",
        )
    pos = services.set_position_blocked(
        actor=m, position_id=cell(tree.box, "A2").pk, expected_revision=1, blocked=True, reason="y"
    )
    pos = services.set_position_blocked(
        actor=m, position_id=pos.pk, expected_revision=2, blocked=False, reason="Repaired"
    )
    assert not pos.blocked and pos.block_reason == "" and pos.revision == 3
    assert (
        AuditEvent.objects.filter(object_id=str(pos.pk), action="storage.position_unblocked")
        .get()
        .reason
        == "Repaired"
    )


def test_retirement_gates(tree, people):
    t, m = people["technician"], people["manager"]
    item = new_item(t)
    place(t, item, cell(tree.box, "A1"))
    with pytest.raises(ValidationError) as exc:
        services.retire_storage(
            actor=m, kind="box", object_id=tree.box.pk, expected_revision=1, reason="x"
        )
    assert codes(exc.value) == {"occupied"}
    with pytest.raises(ValidationError) as exc:
        services.retire_storage(
            actor=m, kind="node", object_id=tree.shelf1.pk, expected_revision=1, reason="x"
        )
    assert codes(exc.value) == {"invalid_transition"}
    with pytest.raises(ValidationError):
        services.retire_storage(
            actor=m, kind="unit", object_id=tree.unit.pk, expected_revision=1, reason="x"
        )
    retired = services.retire_storage(
        actor=m, kind="node", object_id=tree.shelf2.pk, expected_revision=1, reason="Removed"
    )
    assert not retired.is_active and retired.retired_by == m and retired.retirement_reason
    # The slot is free again for a new node; the retired one keeps its code and history.
    services.create_node(
        actor=m,
        unit_id=tree.unit.pk,
        parent_id=tree.tower.pk,
        code="T-01-S2B",
        name="Shelf 2",
        kind="shelf",
        slot=2,
        child_slots=0,
        box_slots=2,
    )
    with pytest.raises(ValidationError) as exc:
        services.retire_storage(
            actor=m, kind="node", object_id=tree.shelf2.pk, expected_revision=2, reason="Again"
        )
    assert codes(exc.value) == {"invalid_transition"}
    assert MoveEvent.objects.count() == 1
