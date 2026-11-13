"""Which specimens were in the affected unit during an excursion, from recorded movements."""

from equipment.occupancy import affected_specimens
from storage.models import StorageGate

from .conftest import ago, check_in, check_out, place, register


def _move_box(actor, box, shelf, minutes):
    box.refresh_from_db()
    from storage import services as storage

    return storage.move_container(
        actor=actor,
        kind="box",
        object_id=box.pk,
        expected_revision=box.revision,
        expected_gate_revision=StorageGate.objects.get().revision,
        target_kind="node",
        target_id=shelf.pk,
        target_slot=1,
        occurred_at=ago(minutes),
        reason="Consolidating boxes",
    )


def _accessions(rows):
    return {row.accession: row for row in rows}


def test_presence_follows_placement_checkout_checkin_and_box_moves(people, world, freezer_unit):
    tech = people["technician"]
    a = register(tech, world, occurred_at=ago(270))
    b = register(tech, world, occurred_at=ago(270))
    c = register(tech, world, occurred_at=ago(270))
    place(tech, a, world.position("A1"), occurred_at=ago(260))
    place(tech, b, world.position("A2"), occurred_at=ago(250))
    check_out(tech, b, tech, occurred_at=ago(200))
    place(tech, c, world.position("A3"), occurred_at=ago(140))
    check_in(tech, b, world.position("A2"), occurred_at=ago(135))

    rows = _accessions(
        affected_specimens(
            actor=people["viewer"], unit_id=world.unit.pk, start=ago(230), end=ago(150)
        )
    )
    assert set(rows) == {a.accession, b.accession}
    assert rows[b.accession].intervals[0][1] <= ago(199)  # left at the check-out
    assert len(rows[b.accession].intervals) == 1
    assert "BOX-001" in rows[a.accession].location

    # B leaves and comes back inside the window: two intervals.
    rows = _accessions(
        affected_specimens(
            actor=people["viewer"], unit_id=world.unit.pk, start=ago(230), end=ago(130)
        )
    )
    assert len(rows[b.accession].intervals) == 2
    assert c.accession in rows

    # The whole box moves to the freezer: present in VP-01 until the move, then in MF-01.
    unit, shelf = freezer_unit
    _move_box(people["manager"], world.box, shelf, 120)
    before = _accessions(
        affected_specimens(
            actor=people["viewer"], unit_id=world.unit.pk, start=ago(125), end=ago(110)
        )
    )
    after = _accessions(
        affected_specimens(actor=people["viewer"], unit_id=unit.pk, start=ago(125), end=ago(110))
    )
    assert set(before) == set(after) == {a.accession, b.accession, c.accession}
    assert before[a.accession].present_until <= ago(119)
    assert after[a.accession].present_from >= ago(121)
    # Nothing was in the freezer before the move.
    assert (
        affected_specimens(actor=people["viewer"], unit_id=unit.pk, start=ago(200), end=ago(125))
        == []
    )
