"""The storage path bar's context (templates/includes/storage_path.html): presentation only.

  storage_path    the levels from the site down to a record or a stored item, each a dict
                  {kind, code, url, temp, cold, here, freed}; built from services.build_path's
                  recorded shape, so a stored item and a record read the same way
  storage_census  every active storage unit with its nominal temperature and how full it is

Views add these to their context; the include renders them. Reads go through the same role check
as the pages that show them (READ_ROLES), and storage still learns nothing about what it stores.
"""

from decimal import Decimal

from django.urls import reverse

from accounts.roles import require_role

from .models import LayoutNode
from .services import READ_ROLES, build_path, node_chain

# How each storage technology is named in the path. Words for the bar only, not a policy.
UNIT_KINDS = {
    "ln2_vapor": "Tank",
    "ln2_liquid": "Tank",
    "mechanical": "Freezer",
    "ambient": "Unit",
}
LN2_AT_OR_BELOW = Decimal("-150")

# Callables returning the ids of storage units with an open temperature excursion. Storage knows
# nothing of equipment: the equipment app registers its source here (equipment/apps.py).
EXCURSION_SOURCES = []


def temperature(value):
    """'−160 °C' from a nominal temperature (Decimal or its string), with a true minus sign."""
    number = Decimal(str(value)).normalize()
    text = format(number, "f") if number else "0"
    return text.replace("-", "\N{MINUS SIGN}") + " °C"


def levels(path, *, here="position", freed=False):
    """Path-bar levels from a build_path dict, from the site down to `here`.

    here: the last level shown and marked current ("site", "room", "unit", a node id, "box" or
    "position"). freed=True marks the position as the one an item left while it is checked out.
    """
    if not path or "unit" not in path:
        return []
    result = [
        {"kind": "Site", "code": path["site"]["code"],
         "url": reverse("storage:site-detail", args=[path["site"]["id"]])},
        {"kind": "Room", "code": path["room"]["code"],
         "url": reverse("storage:room-detail", args=[path["room"]["id"]])},
    ]  # fmt: skip
    unit = path["unit"]
    result.append(
        {
            "kind": UNIT_KINDS.get(unit.get("technology"), "Unit"),
            "code": unit["code"],
            "url": reverse("storage:unit-detail", args=[unit["id"]]),
            "temp": temperature(unit["nominal_temperature"]),
            "cold": True,
        }
    )
    for node in path.get("nodes", []):
        kind = LayoutNode.Kind(node["kind"]).label if node.get("kind") in LayoutNode.Kind else ""
        result.append(
            {
                "kind": kind or "Node",
                "code": node["code"],
                "url": reverse("storage:node-detail", args=[node["id"]]),
                "node": node["id"],
            }
        )
    if path.get("box"):
        box = path["box"]
        result.append(
            {
                "kind": "Box",
                "code": box["code"],
                "url": reverse("storage:box-detail", args=[box["id"]]),
            }
        )
    if path.get("position"):
        result.append({"kind": "Position", "code": path["position"]["label"], "url": ""})
    names = {"site": 0, "room": 1, "unit": 2}
    if here in names:
        end = names[here]
    elif here == "box":
        end = next(i for i, level in enumerate(result) if level["kind"] == "Box")
    elif here == "position":
        end = len(result) - 1
    else:  # a node id
        end = next(i for i, level in enumerate(result) if level.get("node") == str(here))
    result = result[: end + 1]
    current = result[-1]
    current["here"] = not freed
    current["freed"] = freed and here == "position"
    current["url"] = ""
    for level in result:
        level.pop("node", None)
    return result


def for_record(*, actor, site=None, room=None, unit=None, node=None, box=None):
    """Levels down to one storage record: the record is the current level."""
    require_role(actor, *READ_ROLES)
    if site is not None:
        return [{"kind": "Site", "code": site.code, "url": "", "here": True}]
    if room is not None:
        return [
            {"kind": "Site", "code": room.site.code,
             "url": reverse("storage:site-detail", args=[room.site_id])},
            {"kind": "Room", "code": room.code, "url": "", "here": True},
        ]  # fmt: skip
    if box is not None:
        return levels(
            build_path(unit=box.node.unit, nodes=node_chain(box.node), box=box), here="box"
        )
    if node is not None:
        return levels(build_path(unit=node.unit, nodes=node_chain(node)), here=str(node.pk))
    if unit is not None:
        return levels(build_path(unit=unit, nodes=[]), here="unit")
    return []


def census(*, actor):
    """Every active storage unit, coldest first, with how many of its positions hold an item.

    The counts are the overview's own (storage.lists.units: active boxes, nodes and units)."""
    from .lists import units  # lists imports forms; keep this module light for the views

    require_role(actor, *READ_ROLES)
    flagged = set().union(*(set(source()) for source in EXCURSION_SOURCES))
    result = []
    for unit in units().filter(is_active=True).order_by("nominal_temperature", "code"):
        result.append(
            {
                "id": unit.pk,
                "code": unit.code,
                "temp": temperature(unit.nominal_temperature),
                "ln2": unit.nominal_temperature <= LN2_AT_OR_BELOW,
                "fridge": unit.nominal_temperature > 0,
                "occupied": unit.occupied,
                "positions": unit.installed,
                "fill_step": round(10 * unit.occupied / unit.installed) if unit.installed else 0,
                "url": reverse("storage:unit-detail", args=[unit.pk]),
                "excursion": unit.pk in flagged,
            }
        )
    return result
