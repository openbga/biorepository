import pytest

from storage import seed
from storage.models import Box, LayoutNode, MoveEvent, Position, StorageTemplate, StorageUnit

from .conftest import ago, new_item, place

pytestmark = pytest.mark.django_db


def test_demo_storage_is_idempotent_and_the_demo_move_carries_contents(people):
    m = people["manager"]
    first = seed.seed_storage(actor=m)
    counts = (StorageUnit.objects.count(), LayoutNode.objects.count(), Box.objects.count())
    seed.seed_storage(actor=m)
    assert (
        counts
        == (
            StorageUnit.objects.count(),
            LayoutNode.objects.count(),
            Box.objects.count(),
        )
        == (2, 9, 3)
    )
    assert str(first["tank"].nominal_temperature) == "-160.00"
    assert Position.objects.get(box__code="BOX-003", label="I9").blocked
    assert not Position.objects.filter(box__code="BOX-003", occupancy__isnull=False).exists()
    assert StorageTemplate.objects.get(code="TANK-2T").definition["nodes"][1]["suffix"] == "T-02"
    t = people["technician"]
    item = new_item(t, ago(120))
    place(t, item, Position.objects.get(box__code="BOX-001", label="A1"), ago(100))
    event = seed.move_demo_box(actor=m, occurred_at=ago(30))
    assert event.members.count() == 1
    assert Box.objects.get(code="BOX-001").node.code == "T-02-S1"
    assert seed.move_demo_box(actor=m, occurred_at=ago(10)) is None
    assert MoveEvent.objects.filter(kind="container_move").count() == 1
