"""queries.occupants_during: which storage keys were in a unit during a window (for excursions)."""

from datetime import datetime

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from storage import queries
from storage.models import StorageUnit

from .conftest import ago, new_item
from .conftest import place as _place
from .test_definitions import codes
from .test_moves import move
from .test_occupancy import cell

pytestmark = pytest.mark.django_db


def place(actor, item, position, at, kind="place"):
    item.refresh_from_db()
    return _place(actor, item, position, at, kind=kind)


@pytest.fixture
def history(tree, people):
    """Window [T80, T20] (minutes ago). VP-01 holds BOX-001 until it moves to MF-01 at T30.

    stayed:   placed T100 (before the window), still in BOX-001
    visits:   placed T70, out T60, back in T50, out T40 (in and out inside the window)
    gone:     placed T100, out T90 (left before the window)
    late:     placed T10 in BOX-001, after the box moved (after the window, in MF-01)
    """
    t = people["technician"]
    T = {m: ago(m) for m in (120, 100, 95, 90, 85, 80, 70, 65, 60, 55, 50, 40, 30, 20, 10)}
    items = {name: new_item(t, T[120]) for name in ("stayed", "visits", "gone", "late")}
    place(t, items["stayed"], cell(tree.box, "A1"), T[100])
    place(t, items["gone"], cell(tree.box, "A2"), T[100])
    place(t, items["gone"], None, T[90], kind="check_out")
    place(t, items["visits"], cell(tree.box, "A3"), T[70])
    place(t, items["visits"], None, T[60], kind="check_out")
    place(t, items["visits"], cell(tree.box, "A4"), T[50], kind="check_in")
    place(t, items["visits"], None, T[40], kind="check_out")
    move(t, tree.box, tree.rack, 1, T[30])
    place(t, items["late"], cell(tree.box, "B1"), T[10])
    return items, T


def during(people, unit, start, end):
    found = queries.occupants_during(actor=people["viewer"], unit_id=unit.pk, start=start, end=end)
    return {p.occupancy_id: p for p in found}


def test_presence_follows_item_and_box_movements(tree, people, history):
    items, T = history
    tank = during(people, tree.unit, T[80], T[20])
    assert set(tank) == {items["stayed"].pk, items["visits"].pk}
    # Placed before the window and carried out of the unit by the box move at T30.
    assert tank[items["stayed"].pk].intervals == ((T[80], T[30]),)
    assert tank[items["stayed"].pk].path["display"] == "VP-01 / T-01 / T-01-S1 / BOX-001 / A1"
    # In and out twice inside the window; the path is where its first interval began.
    assert tank[items["visits"].pk].intervals == ((T[70], T[60]), (T[50], T[40]))
    assert tank[items["visits"].pk].path["position"]["label"] == "A3"

    # The freezer received the box (and the item still in it) at T30, mid-window.
    freezer = during(people, tree.other_unit, T[80], T[20])
    assert set(freezer) == {items["stayed"].pk}
    assert freezer[items["stayed"].pk].intervals == ((T[30], T[20]),)
    assert freezer[items["stayed"].pk].path["unit"]["code"] == "MF-01"
    # Placed after the window closed: only in a later window.
    assert items["late"].pk not in freezer
    later = during(people, tree.other_unit, T[20], ago(0))
    assert set(later) == {items["stayed"].pk, items["late"].pk}
    assert later[items["late"].pk].intervals[0][0] == T[10]


def test_window_edges_are_inclusive(tree, people, history):
    items, T = history
    # A movement exactly at the start decides the state at the start.
    at_start = during(people, tree.unit, T[70], T[65])
    assert at_start[items["visits"].pk].intervals == ((T[70], T[65]),)
    # A single instant: whoever was there then.
    instant = during(people, tree.unit, T[55], T[55])
    assert set(instant) == {items["stayed"].pk}
    assert instant[items["stayed"].pk].intervals == ((T[55], T[55]),)
    # A movement exactly at the end counts; left before the window: absent.
    at_end = during(people, tree.unit, T[95], T[90])
    assert at_end[items["gone"].pk].intervals == ((T[95], T[90]),)
    assert items["gone"].pk not in during(people, tree.unit, T[85], T[80])


def test_occupants_during_checks_role_unit_and_window(tree, people):
    with pytest.raises(PermissionDenied):
        queries.occupants_during(
            actor=people["nobody"], unit_id=tree.unit.pk, start=ago(10), end=ago(0)
        )
    with pytest.raises(StorageUnit.DoesNotExist):
        queries.occupants_during(
            actor=people["viewer"], unit_id=tree.box.pk, start=ago(10), end=ago(0)
        )
    with pytest.raises(ValidationError) as error:
        queries.occupants_during(
            actor=people["viewer"], unit_id=tree.unit.pk, start=ago(0), end=ago(10)
        )
    assert codes(error.value) == {"invalid_time"}
    with pytest.raises(ValidationError) as error:
        queries.occupants_during(
            actor=people["viewer"],
            unit_id=str(tree.unit.pk),
            start=datetime(2026, 1, 1),
            end=ago(0),
        )
    assert codes(error.value) == {"invalid"}
    assert (
        queries.occupants_during(
            actor=people["viewer"], unit_id=str(tree.unit.pk), start=ago(10), end=ago(0)
        )
        == ()
    )
