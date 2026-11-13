"""Box and node moves with their contents: one event, members, paths, chronology, corrections."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from core.models import AuditEvent
from storage import queries, services
from storage.models import Box, LayoutNode, MoveEvent, Occupancy

from .conftest import ago, gate_revision, new_item, place
from .test_definitions import codes
from .test_occupancy import cell

pytestmark = pytest.mark.django_db


def move(actor, obj, target, slot, occurred_at=None, **extra):
    kind = "box" if isinstance(obj, Box) else "node"
    obj.refresh_from_db()
    return services.move_container(
        actor=actor,
        kind=kind,
        object_id=obj.pk,
        expected_revision=extra.pop("expected_revision", obj.revision),
        expected_gate_revision=extra.pop("expected_gate_revision", gate_revision()),
        target_kind=extra.pop("target_kind", "node"),
        target_id=target.pk,
        target_slot=slot,
        occurred_at=occurred_at or ago(10),
        reason=extra.pop("reason", "Consolidating tower 1"),
        **extra,
    )


def test_box_move_records_one_event_with_every_occupied_position(tree, people):
    t = people["technician"]
    items = [new_item(t, ago(120)) for _ in range(3)]
    for item, label in zip(items, ("A1", "B5", "I9"), strict=True):
        place(t, item, cell(tree.box, label), ago(100))
    revisions = {o.pk: o.revision for o in Occupancy.objects.all()}
    event = move(t, tree.box, tree.rack, 2, ago(50))
    assert event.kind == "container_move" and event.box_id == tree.box.pk
    assert event.from_path["display"] == "VP-01 / T-01 / T-01-S1 / BOX-001"
    assert event.to_path["display"] == "MF-01 / R-01 / BOX-001" and event.to_path["slot"] == 2
    members = list(event.members.all())
    assert len(members) == 3
    for member in members:
        assert member.from_position_id == member.to_position_id
        assert member.from_path["unit"]["code"] == "VP-01"
        assert member.to_path["unit"]["code"] == "MF-01"
        assert member.to_path["unit"]["nominal_temperature"] == "-80.00"
    for item in Occupancy.objects.all():
        assert item.revision == revisions[item.pk] + 1
        assert item.last_occurred_at == event.occurred_at
    box = Box.objects.get(pk=tree.box.pk)
    assert (box.node_id, box.slot, box.revision) == (tree.rack.pk, 2, 2)
    # The earlier placement keeps the path recorded at the time.
    history = queries.movement_history(actor=people["viewer"], occupancy_id=items[0].pk)
    assert [m.event.kind for m in history] == ["place", "container_move"]
    assert history[0].to_path["unit"]["code"] == "VP-01"
    assert AuditEvent.objects.filter(action="storage.occupancy_moved_with_container").count() == 3
    moved = AuditEvent.objects.get(action="storage.box_moved")
    assert moved.after["event"] == str(event.pk) and moved.reason == "Consolidating tower 1"


def test_empty_box_move_still_records_the_header(tree, people):
    event = move(people["technician"], tree.box, tree.shelf2, 1)
    assert event.members.count() == 0


def test_node_move_carries_descendants_to_another_unit(tree, people):
    t, m = people["technician"], people["manager"]
    item = new_item(t, ago(120))
    place(t, item, cell(tree.box, "A1"), ago(100))
    event = move(
        t, tree.tower, tree.other_unit, 2, ago(50), target_kind="unit", reason="Tank maintenance"
    )
    assert event.node_id == tree.tower.pk
    for node in LayoutNode.objects.filter(code__startswith="T-01"):
        assert node.unit_id == tree.other_unit.pk
        assert node.revision == 2
    shelf = LayoutNode.objects.get(pk=tree.shelf1.pk)
    assert shelf.path == "T-01/T-01-S1" and shelf.depth == 2
    (member,) = event.members.all()
    assert member.to_path["display"] == "MF-01 / T-01 / T-01-S1 / BOX-001 / A1"
    v = people["viewer"]
    assert queries.capacity(actor=v, kind="unit", object_id=tree.other_unit.pk).occupied == 1
    assert queries.capacity(actor=v, kind="unit", object_id=tree.unit.pk).installed == 0
    carried = AuditEvent.objects.filter(action="storage.carried_by_move")
    assert carried.count() == 3  # two shelves and the box
    # Under a shelf rack -> shelf nesting the tower may not go (a shelf holds racks only).
    new_shelf = services.create_node(
        actor=m,
        unit_id=tree.unit.pk,
        parent_id=None,
        code="SH-1",
        name="Shelf",
        kind="shelf",
        slot=1,
        child_slots=2,
        box_slots=0,
    )
    with pytest.raises(ValidationError) as exc:
        move(t, tree.tower, new_shelf, 1)
    assert "target" in exc.value.error_dict


def test_node_moves_refuse_cycles_depth_and_taken_slots(tree, people):
    t, m = people["technician"], people["manager"]
    rack = services.create_node(
        actor=m,
        unit_id=tree.unit.pk,
        parent_id=None,
        code="R-X",
        name="Rack",
        kind="rack",
        slot=2,
        child_slots=2,
        box_slots=0,
    )
    shelf = services.create_node(
        actor=m,
        unit_id=tree.unit.pk,
        parent_id=rack.pk,
        code="R-X-S",
        name="Shelf",
        kind="shelf",
        slot=1,
        child_slots=1,
        box_slots=0,
    )
    with pytest.raises(ValidationError):  # into its own subtree
        move(t, rack, shelf, 1)
    with pytest.raises(ValidationError) as exc:  # top-level slot 1 holds the tower
        move(t, rack, tree.unit, 1, target_kind="unit")
    assert codes(exc.value) == {"occupied"}
    with pytest.raises(ValidationError) as exc:  # same place
        move(t, rack, tree.unit, 2, target_kind="unit")
    assert codes(exc.value) == {"invalid_transition"}


def test_box_move_refusals(tree, people):
    t = people["technician"]
    with pytest.raises(ValidationError) as exc:
        move(t, tree.box, tree.tower, 1)  # tower holds shelves, not boxes
    assert "target" in exc.value.error_dict
    with pytest.raises(ValidationError) as exc:
        move(t, tree.box, tree.shelf1, 1)
    assert codes(exc.value) == {"invalid_transition"}
    with pytest.raises(ValidationError) as exc:
        move(t, tree.box, tree.rack, 2, expected_gate_revision=gate_revision() - 1)
    assert codes(exc.value) == {"stale_revision"}
    with pytest.raises(ValidationError) as exc:
        move(t, tree.box, tree.rack, 2, expected_revision=7)
    assert codes(exc.value) == {"stale_revision"}
    with pytest.raises(PermissionDenied):
        move(people["viewer"], tree.box, tree.rack, 2)
    services.retire_storage(
        actor=people["manager"],
        kind="node",
        object_id=tree.shelf2.pk,
        expected_revision=1,
        reason="Removed",
    )
    with pytest.raises(ValidationError) as exc:
        move(t, tree.box, tree.shelf2, 1)
    assert codes(exc.value) == {"inactive_location"}


def test_move_chronology_respects_items_and_containers(tree, people):
    t = people["technician"]
    item = new_item(t, ago(120))
    place(t, item, cell(tree.box, "A1"), ago(60))
    with pytest.raises(ValidationError) as exc:  # before the item's placement
        move(t, tree.box, tree.rack, 1, ago(70))
    assert codes(exc.value) == {"invalid_time"}
    move(t, tree.box, tree.rack, 1, ago(50))
    other = new_item(t, ago(120))
    with pytest.raises(ValidationError) as exc:  # the box was elsewhere then
        place(t, other, cell(tree.box, "A2"), ago(55))
    assert codes(exc.value) == {"invalid_time"}
    with pytest.raises(ValidationError) as exc:  # before the rack's last move
        move(t, tree.box, tree.shelf2, 1, ago(55))
    assert codes(exc.value) == {"invalid_time"}


def test_container_move_correction_is_a_compensating_move(tree, people):
    t, m = people["technician"], people["manager"]
    wrong = move(t, tree.box, tree.rack, 1, ago(40))
    # Corrections are reserved to managers and admins (permission table).
    with pytest.raises(PermissionDenied):
        move(t, tree.box, tree.rack, 2, ago(1), corrects_id=wrong.pk, reason="Wrong slot")
    assert not MoveEvent.objects.filter(kind="correction").exists()
    fixed = move(
        m,
        tree.box,
        tree.rack,
        2,
        ago(1),
        corrects_id=wrong.pk,
        reason="Recorded the wrong slot",
    )
    assert fixed.kind == "correction" and fixed.corrects == wrong and fixed.box_id == tree.box.pk
    assert MoveEvent.objects.get(pk=wrong.pk).to_path["slot"] == 1  # never rewritten
    with pytest.raises(ValidationError):
        move(m, tree.box, tree.rack, 1, ago(0), corrects_id=wrong.pk)
    with pytest.raises(ValidationError):  # different object
        move(m, tree.tower, tree.unit, 3, target_kind="unit", corrects_id=fixed.pk)


def test_container_move_cannot_precede_a_departed_items_recorded_movement(tree, people):
    """Review 2026-11-12: an emptied box cannot be recorded as leaving before its last
    recorded check-out, or the item's history and the box's history contradict."""
    t = people["technician"]
    item = new_item(t, ago(120))
    place(t, item, cell(tree.box, "A1"), ago(100))
    item.refresh_from_db()
    place(t, item, None, ago(20), kind="check_out")
    for obj, target, slot, extra in (
        (tree.box, tree.rack, 1, {}),
        (tree.tower, tree.other_unit, 2, {"target_kind": "unit"}),
    ):
        with pytest.raises(ValidationError) as error:
            move(t, obj, target, slot, ago(30), **extra)
        assert codes(error.value) == {"invalid_time"}
    assert not MoveEvent.objects.filter(kind="container_move").exists()
    event = move(t, tree.box, tree.rack, 1, ago(20))  # equal times are allowed
    assert event.to_path["unit"]["code"] == "MF-01"


def test_location_reads_assemble_paths_from_one_snapshot(tree, people, django_assert_num_queries):
    """Review 2026-11-12: paths came from two SELECTs, so a cross-unit rack move committing
    between them raised KeyError. Box, node chain and unit now come from one statement."""
    from unittest.mock import patch

    t, v = people["technician"], people["viewer"]
    item = new_item(t, ago(120))
    place(t, item, cell(tree.box, "A1"), ago(100))
    # Role check, the claims, then one SELECT of boxes joined to node chain, unit, room, site.
    with django_assert_num_queries(3):
        before = queries.locations(actor=v, occupancy_ids=[item.pk])[item.pk]
    assert before["unit"]["code"] == "VP-01"
    original, moved = Box.objects.filter, []

    def move_first(*args, **kwargs):  # the tower moves after the claims were read
        if not moved:
            moved.append(move(t, tree.tower, tree.other_unit, 2, ago(50), target_kind="unit"))
        return original(*args, **kwargs)

    with patch.object(Box.objects, "filter", side_effect=move_first):
        after = queries.locations(actor=v, occupancy_ids=[item.pk])[item.pk]
    assert moved and after["unit"]["code"] == "MF-01"
    assert [n["code"] for n in after["nodes"]] == [tree.tower.code, tree.shelf1.code]
    assert after["display"].startswith("MF-01 / ")
