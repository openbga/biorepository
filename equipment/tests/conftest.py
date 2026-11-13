"""Equipment test fixtures. The people, storage tree and specimen catalogues come from the
specimens fixtures (imported, not copied); equipment adds a tank, a freezer unit, a monitor and a
supply dewar. All data is synthetic: users at example.invalid, codes like VP-01.
"""

import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from equipment import services
from specimens.tests import conftest as specimen_fixtures
from specimens.tests.conftest import (  # noqa: F401  (helpers re-exported to the tests)
    PASSWORD,
    ago,
    check_in,
    check_out,
    make_user,
    place,
    register,
)
from storage import services as storage

# The specimens fixtures, registered here under the same names.
people = specimen_fixtures.people
world = specimen_fixtures.world


def register_equipment(actor, **values):
    values.setdefault("name", values.get("code", "Equipment"))
    return services.save_equipment(actor=actor, **values)


@pytest.fixture
def tank(people, world):
    """Tank VP-01 linked to the specimens world's LN2 vapor-phase unit, with limits set."""
    return register_equipment(
        people["manager"],
        code="TANK-VP-01",
        name="Tank VP-01",
        kind="ln2_tank",
        storage_unit=world.unit,
        lower_limit=Decimal("-196.00"),
        upper_limit=Decimal("-150.00"),
        level_unit="cm",
        level_low_limit=Decimal("20.00"),
    )


@pytest.fixture
def freezer_unit(people, world):
    """A second storage unit (mechanical freezer) with one shelf, for moves between units."""
    m = people["manager"]
    vp01 = world.unit
    unit = storage.create_unit(
        actor=m,
        room_id=vp01.room_id,
        code="MF-01",
        name="Freezer MF-01",
        storage_class_id=vp01.storage_class_id,
        technology="mechanical",
        nominal_temperature=Decimal("-80.00"),
    )
    shelf = storage.create_node(
        actor=m,
        unit_id=unit.pk,
        parent_id=None,
        code="MF-01-S1",
        name="Shelf 1",
        kind="shelf",
        slot=1,
        child_slots=0,
        box_slots=2,
    )
    return unit, shelf


@pytest.fixture
def monitor(people):
    return register_equipment(
        people["manager"], code="MON-01", name="Probe logger 1", kind="monitor"
    )


@pytest.fixture
def dewar(people):
    return register_equipment(
        people["manager"],
        code="DEWAR-01",
        name="Supply dewar 1",
        kind="supply_dewar",
        location="Loading bay",
    )


def csv_file(text, name="readings.csv"):
    data = io.BytesIO(text.encode("utf-8"))
    data.name = name
    data.size = len(text.encode("utf-8"))
    return data


def hours_ago(hours):
    return timezone.now() - timedelta(hours=hours)
