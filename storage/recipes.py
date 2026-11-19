"""Storage templates: a versioned data recipe for one unit and its layout.

A recipe is plain JSON, validated here with the same rules the creation services apply. It is not
an expression language: it names a storage class, a technology, a nominal temperature and a
finite tree of nodes and boxes. Codes in a recipe are suffixes; instantiation prefixes each with
the new unit's code ("VP-02" + "-" + "T01" -> "VP-02-T01"), so suffixes are unique per recipe.

    {
      "schema": 1,
      "storage_class": "CRYOGENIC",
      "technology": "ln2_vapor",
      "nominal_temperature": "-160.00",
      "nodes": [
        {"suffix": "T01", "name": "Tower 1", "kind": "tower", "slot": 1, "child_slots": 2,
         "children": [
           {"suffix": "T01-S1", "kind": "shelf", "slot": 1, "box_slots": 2,
            "boxes": [{"suffix": "B01", "slot": 1,
                       "container_type": {"code": "GRID-9X9", "version": 1}}]}
         ]}
      ]
    }
"""

import re
from decimal import Decimal, InvalidOperation
from itertools import chain, count

from django.core.exceptions import ValidationError

from .models import (
    ABSOLUTE_ZERO,
    CODE_PATTERN,
    MAX_DEPTH,
    Box,
    ContainerType,
    LayoutNode,
    StorageClass,
    StorageUnit,
)

SCHEMA = 1
MAX_SLOTS = 100
MAX_TOP_LEVEL_SLOT = 1_000_000
MAX_SUFFIX_LENGTH = 30
MAX_RECIPE_NODES = 400
MAX_RECIPE_BOXES = 400
MAX_RECIPE_POSITIONS = 40000
# Parent kind -> kinds it may contain: shelves hold racks; racks, towers and
# canisters hold shelves. Any kind may stand at the top of a unit.
CHILD_KINDS = {
    "shelf": frozenset({"rack"}),
    "rack": frozenset({"shelf"}),
    "tower": frozenset({"shelf"}),
    "canister": frozenset({"shelf"}),
}
_SUFFIX = re.compile(CODE_PATTERN)


def _fail(message):
    raise ValidationError({"definition": ValidationError(message, code="invalid")})


def _int(value, where, name, *, low=0, high=MAX_SLOTS):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail(f"{where}: {name} must be a whole number from {low} to {high}.")
    return value


def _text(value, where, name, *, limit=160, required=True):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        _fail(f"{where}: {name} must be text of at most {limit} characters.")
    return value.strip()


def _valid_suffix(text):
    return len(text) <= MAX_SUFFIX_LENGTH and _SUFFIX.fullmatch(text) is not None


def _suffix(value, where, seen):
    text = value.strip().upper() if isinstance(value, str) else ""
    if not _valid_suffix(text):
        _fail(f"{where}: suffix must be 1-{MAX_SUFFIX_LENGTH} letters, digits, '.', '_' or '-'.")
    if text in seen:
        _fail(f"{where}: suffix {text} is used twice in this template.")
    seen.add(text)
    return text


def validate_definition(definition):
    """Return the normalized recipe or raise ValidationError({"definition": ...})."""
    if not isinstance(definition, dict):
        _fail("The template definition must be a JSON object.")
    unknown = set(definition) - {
        "schema",
        "storage_class",
        "technology",
        "nominal_temperature",
        "nodes",
    }
    if unknown:
        _fail(f"Unknown template keys: {', '.join(sorted(unknown))}.")
    if definition.get("schema") != SCHEMA:
        _fail(f"Template schema must be {SCHEMA}.")
    class_code = definition.get("storage_class")
    if not isinstance(class_code, str) or not class_code.strip():
        _fail("Name the storage class by its code.")
    class_code = class_code.strip().upper()
    if not StorageClass.objects.filter(code=class_code).exists():
        _fail(f"Storage class {class_code} does not exist.")
    technology = definition.get("technology")
    if technology not in StorageUnit.Technology.values:
        _fail("Technology must be one of: " + ", ".join(StorageUnit.Technology.values) + ".")
    raw_temperature = definition.get("nominal_temperature")
    if not isinstance(raw_temperature, str):
        _fail('Write the nominal temperature as a quoted decimal, e.g. "-160.00".')
    try:
        temperature = Decimal(raw_temperature.strip())
    except InvalidOperation:
        _fail("The nominal temperature is not a decimal number.")
    if not temperature.is_finite() or temperature < ABSOLUTE_ZERO or abs(temperature) >= 10000:
        _fail("The nominal temperature must be between -273.15 and 9999.99 °C.")
    if temperature.as_tuple().exponent < -2:
        _fail("The nominal temperature has at most two decimal places.")
    nodes = definition.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        _fail("A template needs at least one top-level node.")
    counts = {"nodes": 0, "boxes": 0, "positions": 0}
    seen = set()
    grids = {}
    normalized_nodes = _nodes(nodes, None, 1, "Unit", seen, counts, grids)
    return {
        "schema": SCHEMA,
        "storage_class": class_code,
        "technology": technology,
        "nominal_temperature": str(temperature.quantize(Decimal("0.01"))),
        "nodes": normalized_nodes,
    }


def _nodes(items, parent, depth, where, seen, counts, grids):
    if not isinstance(items, list):
        _fail(f"{where}: children must be a list.")
    if items and depth > MAX_DEPTH:
        _fail(f"{where}: layouts are at most {MAX_DEPTH} levels deep.")
    capacity = parent["child_slots"] if parent else MAX_TOP_LEVEL_SLOT
    slots = set()
    result = []
    for index, item in enumerate(items, start=1):
        here = f"{where} > node {index}"
        if not isinstance(item, dict):
            _fail(f"{here}: each node must be an object.")
        unknown = set(item) - {
            "suffix",
            "name",
            "kind",
            "slot",
            "child_slots",
            "box_slots",
            "children",
            "boxes",
        }
        if unknown:
            _fail(f"{here}: unknown keys {', '.join(sorted(unknown))}.")
        suffix = _suffix(item.get("suffix"), here, seen)
        here = f"{where} > {suffix}"
        kind = item.get("kind")
        if kind not in CHILD_KINDS:
            _fail(f"{here}: kind must be shelf, rack, tower or canister.")
        if parent and kind not in CHILD_KINDS[parent["kind"]]:
            _fail(f"{here}: a {parent['kind']} cannot hold a {kind}.")
        slot = _int(item.get("slot"), here, "slot", low=1, high=capacity)
        if slot in slots:
            _fail(f"{here}: slot {slot} is used twice.")
        slots.add(slot)
        child_slots = _int(item.get("child_slots", 0), here, "child_slots")
        box_slots = _int(item.get("box_slots", 0), here, "box_slots")
        if (child_slots > 0) == (box_slots > 0):
            _fail(f"{here}: give either child_slots or box_slots, not both and not neither.")
        counts["nodes"] += 1
        if counts["nodes"] > MAX_RECIPE_NODES:
            _fail(f"A template has at most {MAX_RECIPE_NODES} nodes.")
        node = {
            "suffix": suffix,
            "name": _text(item.get("name"), here, "name", required=False),
            "kind": kind,
            "slot": slot,
            "child_slots": child_slots,
            "box_slots": box_slots,
            "children": [],
            "boxes": [],
        }
        children = item.get("children", [])
        boxes = item.get("boxes", [])
        if children and not child_slots:
            _fail(f"{here}: a node with box slots has no child nodes.")
        if boxes and not box_slots:
            _fail(f"{here}: a node with child slots holds no boxes directly.")
        node["children"] = _nodes(children, node, depth + 1, here, seen, counts, grids)
        node["boxes"] = _boxes(boxes, node, here, seen, counts, grids)
        result.append(node)
    return result


def _boxes(items, node, where, seen, counts, grids):
    if not isinstance(items, list):
        _fail(f"{where}: boxes must be a list.")
    slots = set()
    result = []
    for index, item in enumerate(items, start=1):
        here = f"{where} > box {index}"
        if not isinstance(item, dict):
            _fail(f"{here}: each box must be an object.")
        unknown = set(item) - {"suffix", "name", "slot", "container_type"}
        if unknown:
            _fail(f"{here}: unknown keys {', '.join(sorted(unknown))}.")
        suffix = _suffix(item.get("suffix"), here, seen)
        here = f"{where} > {suffix}"
        slot = _int(item.get("slot"), here, "slot", low=1, high=node["box_slots"])
        if slot in slots:
            _fail(f"{here}: box slot {slot} is used twice.")
        slots.add(slot)
        grid = item.get("container_type")
        if not isinstance(grid, dict) or set(grid) != {"code", "version"}:
            _fail(f'{here}: container_type must be {{"code": ..., "version": ...}}.')
        code = grid["code"].strip().upper() if isinstance(grid["code"], str) else ""
        version = _int(grid["version"], here, "container_type version", low=1, high=10**6)
        key = (code, version)
        if key not in grids:
            grids[key] = ContainerType.objects.filter(code=code, version=version).first()
        if grids[key] is None:
            _fail(f"{here}: container type {code} v{version} does not exist.")
        counts["boxes"] += 1
        counts["positions"] += grids[key].capacity
        if counts["boxes"] > MAX_RECIPE_BOXES:
            _fail(f"A template has at most {MAX_RECIPE_BOXES} boxes.")
        if counts["positions"] > MAX_RECIPE_POSITIONS:
            _fail(f"A template creates at most {MAX_RECIPE_POSITIONS} positions.")
        result.append(
            {
                "suffix": suffix,
                "name": _text(item.get("name"), here, "name", required=False),
                "slot": slot,
                "container_type": {"code": code, "version": version},
            }
        )
    return result


def _captured_suffixes(unit, nodes, boxes):
    prefix = f"{unit.code}-"
    suffixes = {}
    reserved = set()
    for record in chain(nodes, boxes):
        preferred = record.code.removeprefix(prefix) or record.code
        if _valid_suffix(preferred) and preferred not in reserved:
            suffixes[record] = preferred
            reserved.add(preferred)

    # All usable source suffixes are reserved before assigning any replacements.
    for records, label in ((nodes, "NODE"), (boxes, "BOX")):
        candidates = (f"{label}-{number}" for number in count(1))
        for record in records:
            if record not in suffixes:
                suffixes[record] = next(code for code in candidates if code not in reserved)
    return suffixes


def recipe_from_unit(unit):
    """A recipe reproducing an existing unit's active layout (for "Save layout as template")."""
    nodes = list(
        LayoutNode.objects.filter(unit=unit, is_active=True).order_by("depth", "slot", "code")
    )
    boxes = list(unit_boxes(unit))
    suffixes = _captured_suffixes(unit, nodes, boxes)
    boxes_by_node = {}
    for box in boxes:
        boxes_by_node.setdefault(box.node_id, []).append(box)

    def node_recipe(node):
        return {
            "suffix": suffixes[node],
            "name": node.name,
            "kind": node.kind,
            "slot": node.slot,
            "child_slots": node.child_slots,
            "box_slots": node.box_slots,
            "children": [node_recipe(c) for c in nodes if c.parent_id == node.pk],
            "boxes": [
                {
                    "suffix": suffixes[box],
                    "name": box.name,
                    "slot": box.slot,
                    "container_type": {
                        "code": box.container_type.code,
                        "version": box.container_type.version,
                    },
                }
                for box in sorted(boxes_by_node.get(node.pk, []), key=lambda b: b.slot)
            ],
        }

    return {
        "schema": SCHEMA,
        "storage_class": unit.storage_class.code,
        "technology": unit.technology,
        "nominal_temperature": str(unit.nominal_temperature),
        "nodes": [node_recipe(n) for n in nodes if n.parent_id is None],
    }


def unit_boxes(unit):
    return Box.objects.filter(node__unit=unit, is_active=True).select_related("container_type")
