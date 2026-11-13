"""Storage definitions: creation rules, grids, reasoned edits, catalogues, templates, roles."""

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from core.models import AuditEvent
from storage import recipes, services
from storage.models import Box, ContainerType, LayoutNode, Position, StorageUnit

from .conftest import gate_revision, grid

pytestmark = pytest.mark.django_db


def codes(exc):
    if hasattr(exc, "error_dict"):
        return {e.code for errors in exc.error_dict.values() for e in errors}
    return {e.code for e in exc.error_list}


def audits(obj):
    return list(AuditEvent.objects.filter(object_type=obj._meta.label_lower, object_id=str(obj.pk)))


def test_tree_is_created_with_attribution_audit_and_normalized_codes(tree, people):
    assert tree.site.code == "EXAMPLE"
    assert tree.unit.temperature_unit == "°C"
    assert tree.unit.nominal_temperature == Decimal("-160.00")
    assert tree.shelf1.path == "T-01/T-01-S1" and tree.shelf1.depth == 2
    for obj in (tree.site, tree.room, tree.unit, tree.tower, tree.box, tree.grid):
        (event,) = audits(obj)
        assert event.actor == people["manager"]
        assert event.reason == "Initial definition"
        assert event.action.endswith("_created")
    (box_event,) = audits(tree.box)
    assert box_event.after["positions"] == 81
    assert box_event.after["grid"]["rows"] == 9


def test_box_positions_follow_the_grid_scheme(tree, people):
    labels = dict(Position.objects.filter(box=tree.box).values_list("ordinal", "label"))
    assert len(labels) == 81
    assert labels[1] == "A1" and labels[9] == "A9" and labels[10] == "B1" and labels[81] == "I9"
    numeric = grid(people["manager"], "GRID-8X12", 8, 12, "numeric", "column_major")
    box = services.create_box(
        actor=people["manager"],
        node_id=tree.shelf1.pk,
        code="box-002",
        name="",
        slot=2,
        container_type_id=numeric.pk,
    )
    assert box.code == "BOX-002" and box.name == "BOX-002"
    cell = Position.objects.get(box=box, row=1, column=2)
    assert cell.ordinal == 9 and cell.label == "9"
    assert Position.objects.get(box=box, row=8, column=12).ordinal == 96


def test_row_letters_continue_past_z():
    assert [services.row_letters(n) for n in (1, 26, 27, 52, 53, 100)] == [
        "A",
        "Z",
        "AA",
        "AZ",
        "BA",
        "CV",
    ]


def test_codes_are_unique_and_never_reused(tree, people):
    with pytest.raises(ValidationError) as exc:
        services.create_unit(
            actor=people["manager"],
            room_id=tree.room.pk,
            code=" vp-01 ",
            name="Duplicate",
            storage_class_id=tree.cryogenic.pk,
            technology="ln2_vapor",
            nominal_temperature=Decimal("-150"),
        )
    assert codes(exc.value) == {"unique"}
    with pytest.raises(ValidationError) as exc:
        services.create_site(actor=people["manager"], code="bad code!", name="x", timezone="UTC")
    assert "code" in exc.value.error_dict


@pytest.mark.parametrize(
    "kwargs,field",
    [
        ({"kind": "tower"}, "kind"),  # a tower cannot hold a tower
        ({"child_slots": 2, "box_slots": 2}, "box_slots"),
        ({"child_slots": 0, "box_slots": 0}, "box_slots"),
        ({"slot": 5}, "slot"),  # the tower has 4 node slots
        ({"slot": 1}, "slot"),  # taken by shelf 1
    ],
)
def test_node_rules(tree, people, kwargs, field):
    values = {
        "actor": people["manager"],
        "unit_id": tree.unit.pk,
        "parent_id": tree.tower.pk,
        "code": "T-01-SX",
        "name": "Shelf",
        "kind": "shelf",
        "slot": 3,
        "child_slots": 0,
        "box_slots": 2,
    } | kwargs
    with pytest.raises(ValidationError) as exc:
        services.create_node(**values)
    assert field in exc.value.error_dict


def test_node_depth_is_limited_to_four_levels(tree, people):
    m = people["manager"]
    rack = services.create_node(
        actor=m,
        unit_id=tree.unit.pk,
        parent_id=None,
        code="R-9",
        name="Rack",
        kind="rack",
        slot=9,
        child_slots=1,
        box_slots=0,
    )
    shelf = services.create_node(
        actor=m,
        unit_id=tree.unit.pk,
        parent_id=rack.pk,
        code="R-9-S",
        name="Shelf",
        kind="shelf",
        slot=1,
        child_slots=1,
        box_slots=0,
    )
    inner = services.create_node(
        actor=m,
        unit_id=tree.unit.pk,
        parent_id=shelf.pk,
        code="R-9-S-R",
        name="Rack",
        kind="rack",
        slot=1,
        child_slots=1,
        box_slots=0,
    )
    assert inner.depth == 3 and inner.path == "R-9/R-9-S/R-9-S-R"
    services.create_node(
        actor=m,
        unit_id=tree.unit.pk,
        parent_id=inner.pk,
        code="R-9-S-R-S",
        name="Shelf",
        kind="shelf",
        slot=1,
        child_slots=1,
        box_slots=0,
    )
    deepest = LayoutNode.objects.get(code="R-9-S-R-S")
    with pytest.raises(ValidationError):
        services.create_node(
            actor=m,
            unit_id=tree.unit.pk,
            parent_id=deepest.pk,
            code="TOO-DEEP",
            name="Rack",
            kind="rack",
            slot=1,
            child_slots=0,
            box_slots=1,
        )


def test_boxes_need_a_free_box_slot_on_an_active_box_node(tree, people):
    base = {
        "actor": people["manager"],
        "code": "BOX-X",
        "name": "",
        "container_type_id": tree.grid.pk,
    }
    with pytest.raises(ValidationError) as exc:
        services.create_box(node_id=tree.shelf1.pk, slot=1, **base)
    assert codes(exc.value) == {"occupied"}
    with pytest.raises(ValidationError):
        services.create_box(node_id=tree.tower.pk, slot=1, **base)
    with pytest.raises(ValidationError):
        services.create_box(node_id=tree.shelf1.pk, slot=3, **base)


@pytest.mark.parametrize("role", ["technician", "viewer", "nobody"])
def test_only_managers_define_storage(tree, people, role):
    actor = people[role]
    with pytest.raises(PermissionDenied):
        services.create_site(actor=actor, code="S2", name="Second", timezone="UTC")
    with pytest.raises(PermissionDenied):
        services.edit_storage_definition(
            actor=actor,
            kind="unit",
            object_id=tree.unit.pk,
            expected_revision=1,
            values={"name": "Renamed"},
            reason="Typo",
        )
    with pytest.raises(PermissionDenied):
        services.retire_storage(
            actor=actor, kind="box", object_id=tree.box.pk, expected_revision=1, reason="x"
        )


def test_admin_has_manager_rights(tree, people):
    site = services.create_site(actor=people["admin"], code="S2", name="Two", timezone="UTC")
    assert site.created_by == people["admin"]


def test_reasoned_edit_is_audited_and_revisioned(tree, people):
    m = people["manager"]
    unit = services.edit_storage_definition(
        actor=m,
        kind="unit",
        object_id=tree.unit.pk,
        expected_revision=1,
        values={"name": "Tank VP-01 (north)", "nominal_temperature": "-170.5"},
        reason="Nominal setting changed by SOP revision",
    )
    assert unit.revision == 2 and unit.nominal_temperature == Decimal("-170.50")
    event = audits(unit)[-1]
    assert event.action == "storage.unit_updated"
    assert event.before["nominal_temperature"] == "-160.00"
    assert event.after["nominal_temperature"] == "-170.50"
    assert event.reason == "Nominal setting changed by SOP revision"
    with pytest.raises(ValidationError) as exc:  # stale
        services.edit_storage_definition(
            actor=m,
            kind="unit",
            object_id=unit.pk,
            expected_revision=1,
            values={"name": "Again"},
            reason="x",
        )
    assert codes(exc.value) == {"stale_revision"}


@pytest.mark.parametrize(
    "values",
    [
        {"code": "NEW"},
        {"room_id": None},
        {"nominal_temperature": -160.0},  # binary float
        {"nominal_temperature": "-300"},
        {"nominal_temperature": "-160.001"},
        {"name": "Tank VP-01"},  # unchanged
    ],
)
def test_edit_rejects_identity_changes_floats_and_no_ops(tree, people, values):
    with pytest.raises(ValidationError):
        services.edit_storage_definition(
            actor=people["manager"],
            kind="unit",
            object_id=tree.unit.pk,
            expected_revision=1,
            values=values,
            reason="Attempt",
        )


def test_edit_needs_a_reason_and_a_valid_time_zone(tree, people):
    with pytest.raises(ValidationError) as exc:
        services.edit_storage_definition(
            actor=people["manager"],
            kind="site",
            object_id=tree.site.pk,
            expected_revision=1,
            values={"timezone": "Mars/Olympus"},
            reason="Moved",
        )
    assert "timezone" in exc.value.error_dict
    with pytest.raises(ValidationError) as exc:
        services.edit_storage_definition(
            actor=people["manager"],
            kind="site",
            object_id=tree.site.pk,
            expected_revision=1,
            values={"timezone": "Europe/Berlin"},
            reason="  ",
        )
    assert "reason" in exc.value.error_dict


def test_definition_changes_raise_the_gate_revision_once(tree, people):
    before = gate_revision()
    services.create_site(actor=people["manager"], code="S3", name="Three", timezone="UTC")
    assert gate_revision() == before + 1
    services.edit_storage_definition(
        actor=people["manager"],
        kind="site",
        object_id=tree.site.pk,
        expected_revision=1,
        values={"name": "Renamed"},
        reason="Rename",
    )
    assert gate_revision() == before + 1  # a rename is not a topology change


def test_container_type_geometry_is_frozen_after_use_and_versions_instead(tree, people):
    m = people["manager"]
    with pytest.raises(ValidationError) as exc:
        services.save_storage_catalogue(
            actor=m,
            kind="container_type",
            object_id=tree.grid.pk,
            expected_revision=1,
            values={"rows": 10},
            reason="Bigger",
        )
    assert codes(exc.value) == {"invalid_transition"}
    renamed = services.save_storage_catalogue(
        actor=m,
        kind="container_type",
        object_id=tree.grid.pk,
        expected_revision=1,
        values={"name": "81-place box"},
        reason="Clearer name",
    )
    assert renamed.revision == 2
    second = grid(m, rows=10, columns=10)
    assert second.code == tree.grid.code and second.version == 2
    unused = grid(m, "GRID-5X5", 5, 5)
    edited = services.save_storage_catalogue(
        actor=m,
        kind="container_type",
        object_id=unused.pk,
        expected_revision=1,
        values={"rows": 6, "label_scheme": "numeric"},
        reason="Correct the geometry before first use",
    )
    assert (edited.rows, edited.label_scheme) == (6, "numeric")


def test_catalogue_rejects_unknown_fields_and_deactivation_blocks_new_references(tree, people):
    m = people["manager"]
    with pytest.raises(ValidationError):
        services.save_storage_catalogue(
            actor=m,
            kind="class",
            object_id=tree.cryogenic.pk,
            expected_revision=1,
            values={"code": "OTHER"},
            reason="x",
        )
    services.save_storage_catalogue(
        actor=m,
        kind="container_type",
        object_id=tree.grid.pk,
        expected_revision=1,
        values={"is_active": False},
        reason="Superseded",
    )
    with pytest.raises(ValidationError) as exc:
        services.create_box(
            actor=m,
            node_id=tree.shelf1.pk,
            code="BOX-NEW",
            name="",
            slot=2,
            container_type_id=tree.grid.pk,
        )
    assert codes(exc.value) == {"inactive_location"}
    assert Box.objects.get(pk=tree.box.pk).container_type_id == tree.grid.pk


def template_definition(grid_type):
    return {
        "schema": 1,
        "storage_class": "cryogenic",
        "technology": "ln2_vapor",
        "nominal_temperature": "-160.00",
        "nodes": [
            {
                "suffix": "T01",
                "kind": "tower",
                "slot": 1,
                "child_slots": 2,
                "children": [
                    {
                        "suffix": "T01-S1",
                        "kind": "shelf",
                        "slot": 1,
                        "box_slots": 2,
                        "boxes": [
                            {
                                "suffix": "B01",
                                "slot": 1,
                                "container_type": {
                                    "code": grid_type.code,
                                    "version": grid_type.version,
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_template_instantiates_a_whole_unit_atomically(tree, people):
    m = people["manager"]
    template = services.save_storage_catalogue(
        actor=m,
        kind="template",
        object_id=None,
        expected_revision=None,
        values={
            "code": "TANK-SMALL",
            "name": "Small tank",
            "definition": template_definition(tree.grid),
        },
        reason="",
    )
    assert template.definition["storage_class"] == "CRYOGENIC"
    unit = services.instantiate_template(
        actor=m, template_id=template.pk, room_id=tree.room.pk, unit_code="vp-02", unit_name="VP 2"
    )
    assert unit.template == template and unit.code == "VP-02"
    assert set(LayoutNode.objects.filter(unit=unit).values_list("code", flat=True)) == {
        "VP-02-T01",
        "VP-02-T01-S1",
    }
    assert Position.objects.filter(box__code="VP-02-B01").count() == 81
    with pytest.raises(ValidationError):  # used: layout frozen
        services.save_storage_catalogue(
            actor=m,
            kind="template",
            object_id=template.pk,
            expected_revision=1,
            values={"definition": {**template.definition, "nominal_temperature": "-150.00"}},
            reason="Warmer",
        )
    units_before = StorageUnit.objects.count()
    long_code = "X" * 35
    with pytest.raises(ValidationError) as exc:
        services.instantiate_template(
            actor=m,
            template_id=template.pk,
            room_id=tree.room.pk,
            unit_code=long_code,
            unit_name="Too long",
        )
    assert "unit_code" in exc.value.error_dict
    assert StorageUnit.objects.count() == units_before  # all or none


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(nominal_temperature=-160.0),
        lambda d: d.update(storage_class="MISSING"),
        lambda d: d["nodes"][0].update(kind="cassette"),
        lambda d: d["nodes"][0]["children"][0].update(kind="tower"),
        lambda d: d["nodes"][0]["children"][0]["boxes"][0].update(slot=3),
        lambda d: d["nodes"][0]["children"][0].update(suffix="T01"),
        lambda d: d["nodes"][0].update(box_slots=1),
        lambda d: d.update(script="import os"),
    ],
)
def test_template_validation_rejects_bad_recipes(tree, change):
    definition = template_definition(tree.grid)
    change(definition)
    with pytest.raises(ValidationError):
        recipes.validate_definition(definition)


def test_recipe_from_unit_round_trips(tree, people):
    recipe = recipes.validate_definition(recipes.recipe_from_unit(tree.unit))
    assert recipe["nodes"][0]["suffix"] == "T-01"
    assert recipe["nodes"][0]["children"][0]["boxes"][0]["container_type"] == {
        "code": "GRID-9X9",
        "version": 1,
    }


def test_container_type_capacity_property():
    assert ContainerType(rows=8, columns=12).capacity == 96
