"""The specimen-ID resolver storage's box map includes (`{% load specimen_wells %}`)."""

import pytest
from django.db import connection
from django.template import Context, Template
from django.test.utils import CaptureQueriesContext

from storage.models import Occupancy, Position

from .conftest import make_user, place, register

pytestmark = pytest.mark.django_db

CELLS = Template(
    "{% load specimen_wells %}{% attach_specimens positions user %}"
    "{% for p in positions %}{{ p.label }}="
    "{% if p.claim.specimen %}{{ p.claim.specimen.accession }}@{{ p.claim.specimen.url }}"
    "{% else %}-{% endif %};{% endfor %}"
)


def box_positions(world):
    """Positions with `.claim` as storage's box view builds them."""
    positions = list(Position.objects.filter(box=world.box).order_by("row", "column")[:3])
    claims = {o.position_id: o for o in Occupancy.objects.filter(position__box=world.box)}
    for position in positions:
        position.claim = claims.get(position.pk)
    return positions


def test_occupied_wells_get_their_specimen_from_one_query(people, world):
    tech = people["technician"]
    first, second = register(tech, world), register(tech, world)
    place(tech, first, world.position("A1"))
    place(tech, second, world.position("A3"))
    positions = box_positions(world)
    with CaptureQueriesContext(connection) as queries:
        text = CELLS.render(Context({"positions": positions, "user": people["viewer"]}))
    # One read of specimens whatever the number of wells (the other query is the role check).
    assert sum("specimens_specimen" in q["sql"] for q in queries) == 1
    assert text == (
        f"A1={first.accession}@/specimens/{first.pk}/;A2=-;"
        f"A3={second.accession}@/specimens/{second.pk}/;"
    )


def test_a_reader_without_a_role_gets_no_specimen_ids(people, world):
    tech = people["technician"]
    specimen = register(tech, world)
    place(tech, specimen, world.position("A1"))
    text = CELLS.render(Context({"positions": box_positions(world), "user": make_user("sp-none")}))
    assert specimen.accession not in text and text.startswith("A1=-;")
