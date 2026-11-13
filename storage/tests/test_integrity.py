"""Database-level integrity: constraints and triggers hold even when services are bypassed."""

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from storage.models import (
    Box,
    LayoutNode,
    MoveEvent,
    MoveMember,
    Occupancy,
    Position,
    StorageGate,
    StorageUnit,
)

from .conftest import new_item, place
from .test_occupancy import cell

pytestmark = pytest.mark.django_db


def refused(fn, error=IntegrityError):
    with pytest.raises(error), transaction.atomic():
        fn()


def test_check_constraints(tree, people):
    refused(lambda: LayoutNode.objects.filter(pk=tree.tower.pk).update(box_slots=2))
    refused(lambda: LayoutNode.objects.filter(pk=tree.tower.pk).update(parent=tree.tower.pk))
    refused(lambda: LayoutNode.objects.filter(pk=tree.shelf1.pk).update(depth=5))
    refused(lambda: LayoutNode.objects.filter(pk=tree.tower.pk).update(code="lower case"))
    refused(lambda: StorageUnit.objects.filter(pk=tree.unit.pk).update(nominal_temperature=-300))
    refused(lambda: StorageUnit.objects.filter(pk=tree.unit.pk).update(technology="dry_ice"))
    refused(lambda: StorageUnit.objects.filter(pk=tree.unit.pk).update(is_active=False))
    refused(
        lambda: StorageUnit.objects.filter(pk=tree.unit.pk).update(
            is_active=False, retired_at=timezone.now(), retired_by=people["manager"]
        )
    )  # retirement needs a reason
    refused(lambda: Position.objects.filter(box=tree.box, label="A1").update(blocked=True))
    refused(lambda: Box.objects.filter(pk=tree.box.pk).update(slot=0))
    refused(lambda: StorageGate.objects.create(pk=2))


def test_unique_active_slots_allow_a_retired_predecessor(tree, people):
    def duplicate():
        return Box.objects.create(
            code="DUP",
            name="dup",
            node=tree.shelf1,
            slot=1,
            container_type=tree.grid,
            created_by=people["manager"],
        )

    refused(duplicate)  # BOX-001 is active in shelf 1, slot 1
    Box.objects.filter(pk=tree.box.pk).update(
        is_active=False,
        retired_at=timezone.now(),
        retired_by=people["manager"],
        retirement_reason="Test",
    )
    duplicate()


def test_move_event_shape_constraints(tree, people):
    base = {"actor": people["manager"], "occurred_at": timezone.now(), "reason": "x"}
    refused(lambda: MoveEvent.objects.create(kind="container_move", **base))
    refused(lambda: MoveEvent.objects.create(kind="place", box=tree.box, **base))
    refused(lambda: MoveEvent.objects.create(kind="correction", **base))  # needs corrects
    refused(lambda: MoveEvent.objects.create(kind="place", **{**base, "reason": ""}))
    refused(
        lambda: MoveEvent.objects.create(
            kind="place", **{**base, "occurred_at": timezone.now() + timezone.timedelta(hours=1)}
        )
    )


def test_history_tables_are_append_only_and_keys_undeletable(tree, people):
    t = people["technician"]
    item = new_item(t)
    event = place(t, item, cell(tree.box, "A1"))
    member = MoveMember.objects.get(event=event)
    refused(lambda: MoveEvent.objects.filter(pk=event.pk).update(reason="rewritten"), DatabaseError)
    refused(lambda: MoveEvent.objects.filter(pk=event.pk).delete(), DatabaseError)
    refused(lambda: MoveMember.objects.filter(pk=member.pk).update(to_path={}), DatabaseError)
    refused(lambda: MoveMember.objects.filter(pk=member.pk).delete(), DatabaseError)
    # Occupancy and the gate change their projection but are never deleted.
    Occupancy.objects.filter(pk=item.pk).update(revision=9)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM storage_occupancy WHERE id = %s", [item.pk])
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM storage_storagegate")
    assert StorageGate.objects.filter(pk=1).exists()
