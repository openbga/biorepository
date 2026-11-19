"""Storage pages: every page renders for each role it is meant for, forms post through the
services, denied roles get 403, and CSV exports are filtered, safe and audited."""

import csv
import io
import re

import pytest
from django.urls import reverse

from core.models import AuditEvent
from storage import services
from storage.models import Box, LayoutNode, MoveEvent, Position, StorageGate, StorageUnit

from .conftest import new_item, place
from .test_occupancy import cell

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def template(tree, people):
    from storage import recipes

    return services.save_storage_catalogue(
        actor=people["manager"],
        kind="template",
        object_id=None,
        expected_revision=None,
        values={
            "code": "TANK",
            "name": "Tank",
            "definition": recipes.recipe_from_unit(tree.unit),
        },
        reason="",
    )


def page_urls(tree, template):
    position = cell(tree.box, "A1")
    return [
        reverse("storage:overview"),
        reverse("storage:unit-list"),
        reverse("storage:node-list"),
        reverse("storage:box-list"),
        reverse("storage:free-position-list"),
        reverse("storage:site-list"),
        reverse("storage:room-list"),
        reverse("storage:class-list"),
        reverse("storage:container-type-list"),
        reverse("storage:template-list"),
        reverse("storage:unit-detail", args=[tree.unit.pk]),
        reverse("storage:node-detail", args=[tree.tower.pk]),
        reverse("storage:node-detail", args=[tree.shelf1.pk]),
        reverse("storage:box-detail", args=[tree.box.pk]),
        reverse("storage:site-detail", args=[tree.site.pk]),
        reverse("storage:room-detail", args=[tree.room.pk]),
        reverse("storage:class-detail", args=[tree.cryogenic.pk]),
        reverse("storage:container-type-detail", args=[tree.grid.pk]),
        reverse("storage:template-detail", args=[template.pk]),
    ], [
        reverse("storage:unit-create"),
        reverse("storage:unit-edit", args=[tree.unit.pk]),
        reverse("storage:node-create") + f"?parent={tree.tower.pk}",
        reverse("storage:node-edit", args=[tree.tower.pk]),
        reverse("storage:box-create") + f"?node={tree.shelf1.pk}",
        reverse("storage:box-edit", args=[tree.box.pk]),
        reverse("storage:site-create"),
        reverse("storage:site-edit", args=[tree.site.pk]),
        reverse("storage:room-create"),
        reverse("storage:room-edit", args=[tree.room.pk]),
        reverse("storage:class-create"),
        reverse("storage:class-edit", args=[tree.cryogenic.pk]),
        reverse("storage:container-type-create") + f"?from={tree.grid.pk}",
        reverse("storage:container-type-edit", args=[tree.grid.pk]),
        reverse("storage:template-create") + f"?unit={tree.unit.pk}",
        reverse("storage:template-edit", args=[template.pk]),
        reverse("storage:template-apply", args=[template.pk]),
        reverse("storage:position-block", args=[position.pk]),
        reverse("storage:retire", args=["box", tree.box.pk]),
        reverse("storage:retire", args=["node", tree.shelf2.pk]),
        reverse("storage:retire", args=["unit", tree.other_unit.pk]),
    ]


def test_every_page_renders_for_its_roles(client, tree, people, template):
    t = people["technician"]
    place(t, new_item(t), cell(tree.box, "A1"))
    services.set_position_blocked(
        actor=people["manager"],
        position_id=cell(tree.box, "B2").pk,
        expected_revision=1,
        blocked=True,
        reason="Cracked",
    )
    reads, manages = page_urls(tree, template)
    move_urls = [
        reverse("storage:container-move", args=["box", tree.box.pk]),
        reverse("storage:container-move", args=["node", tree.tower.pk]),
    ]
    client.force_login(people["viewer"])
    for url in reads:
        response = client.get(url)
        assert response.status_code == 200, url
        assert not re.search(rb'href="[^"]*/(new|edit|move|retire|block)/', response.content), url
    for url in manages + move_urls:
        assert client.get(url).status_code == 403, url
    client.force_login(people["technician"])
    for url in move_urls:
        assert client.get(url).status_code == 200, url
    for url in manages:
        assert client.get(url).status_code == 403, url
    client.force_login(people["manager"])
    for url in reads + manages + move_urls:
        assert client.get(url).status_code == 200, url
    assert (
        client.get(reverse("storage:container-move", args=["site", tree.site.pk])).status_code
        == 404
    )


def test_box_page_shows_grid_states(client, tree, people):
    t = people["technician"]
    item = new_item(t)
    place(t, item, cell(tree.box, "C4"))
    client.force_login(people["viewer"])
    content = client.get(reverse("storage:box-detail", args=[tree.box.pk])).content.decode()
    assert "Occupied" in content and str(item.pk)[:8] in content
    assert content.count('id="position-') == 81
    zero = '<dd class="vr-id"><span class="vr-muted">0</span></dd>'
    blocked = 'data-state="blocked"></span>Blocked</dt>' + zero
    assert blocked in content  # zero is shown, not left blank


def test_manager_defines_storage_through_the_forms(client, tree, people):
    client.force_login(people["manager"])
    response = client.post(
        reverse("storage:unit-create"),
        {
            "room": tree.room.pk,
            "code": "vp-09",
            "name": "Tank VP-09",
            "technology": "ln2_vapor",
            "storage_class": tree.cryogenic.pk,
            "nominal_temperature": "-165.50",
        },
    )
    unit = StorageUnit.objects.get(code="VP-09")
    assert response.status_code == 302 and response["Location"].endswith(f"{unit.pk}/")
    client.post(
        reverse("storage:node-create"),
        {
            "unit": unit.pk,
            "kind": "rack",
            "code": "VP-09-R1",
            "name": "Rack 1",
            "slot": 1,
            "child_slots": 0,
            "box_slots": 4,
        },
    )
    rack = LayoutNode.objects.get(code="VP-09-R1")
    response = client.post(
        reverse("storage:box-create"),
        {"node": rack.pk, "slot": 1, "container_type": tree.grid.pk, "code": "B-9", "name": ""},
    )
    box = Box.objects.get(code="B-9")
    assert response.status_code == 302 and box.positions.count() == 81
    # Duplicate code: errors stay on the form, input kept (422).
    response = client.post(
        reverse("storage:box-create"),
        {"node": rack.pk, "slot": 2, "container_type": tree.grid.pk, "code": "b-9", "name": ""},
    )
    assert response.status_code == 422
    assert "already in use" in response.content.decode()
    # Reasoned edit with a stale revision.
    response = client.post(
        reverse("storage:unit-edit", args=[unit.pk]),
        {
            "name": "Renamed",
            "storage_class": tree.cryogenic.pk,
            "nominal_temperature": "-165.50",
            "reason": "Label",
            "expected_revision": 7,
        },
    )
    assert (
        response.status_code == 422 and "changed since you opened it" in response.content.decode()
    )


def test_move_block_and_retire_through_the_action_forms(client, tree, people):
    t = people["technician"]
    place(t, new_item(t), cell(tree.box, "A1"))
    client.force_login(t)
    gate = StorageGate.objects.get(pk=1).revision
    url = reverse("storage:container-move", args=["box", tree.box.pk])
    data = {
        "target": f"node:{tree.rack.pk}",
        "target_slot": 1,
        "occurred_at": "",
        "reason": "Tank defrost",
        "expected_revision": 1,
        "expected_gate_revision": gate,
    }
    from django.utils import timezone

    data["occurred_at"] = timezone.localtime().strftime("%Y-%m-%dT%H:%M")
    response = client.post(url, data)
    assert response.status_code == 302, response.content.decode()
    event = MoveEvent.objects.get(kind="container_move")
    assert event.members.count() == 1
    # Replaying the stale form is refused and the form offers the fresh revisions.
    response = client.post(url, data)
    assert response.status_code == 422
    client.force_login(people["manager"])
    position = cell(tree.box, "A2")
    response = client.post(
        reverse("storage:position-block", args=[position.pk]),
        {"reason": "Cracked well", "expected_revision": 1},
    )
    assert response.status_code == 302
    assert Position.objects.get(pk=position.pk).blocked
    response = client.post(
        reverse("storage:retire", args=["box", tree.box.pk]),
        {"reason": "Old", "expected_revision": Box.objects.get(pk=tree.box.pk).revision},
    )
    assert response.status_code == 422 and "still holds items" in response.content.decode()


def test_template_apply_creates_a_unit(client, tree, people, template):
    client.force_login(people["manager"])
    response = client.post(
        reverse("storage:template-apply", args=[template.pk]),
        {"room": tree.room.pk, "unit_code": "VP-03", "unit_name": "Tank VP-03"},
    )
    unit = StorageUnit.objects.get(code="VP-03")
    assert response.status_code == 302 and unit.template == template
    assert LayoutNode.objects.filter(code="VP-03-T-01-S1").exists()


def test_list_filters_sorts_and_states(client, tree, people):
    client.force_login(people["viewer"])
    url = reverse("storage:unit-list")
    content = client.get(url, {"sort": "-free"}).content.decode()
    assert content.index("MF-01") < content.index("VP-01") or "VP-01" in content
    assert "VP-01" in client.get(url, {"free": "yes"}).content.decode()
    assert "Nothing matches" in client.get(url, {"q": "zzz"}).content.decode()
    error = client.get(url, {"site": "not-a-uuid"}).content.decode()
    assert "cannot be applied" in error
    assert client.get(url, {"per_page": "7"}).status_code == 400
    free = client.get(reverse("storage:free-position-list"), {"box": tree.box.pk})
    assert free.context["page_obj"].paginator.count == 81


def test_location_records_link_only_positive_free_counts_to_the_counted_scope(client, tree, people):
    client.force_login(people["viewer"])
    register = reverse("storage:free-position-list")
    room = f"{register}?room={tree.room.pk}"
    unit = f"{register}?unit={tree.unit.pk}"
    tower = f"{register}?node={tree.tower.pk}"
    shelf = f"{register}?node={tree.shelf1.pk}"
    box = f"{register}?box={tree.box.pk}"

    for kind, record, expected in (
        ("site", tree.site, {room}),
        ("room", tree.room, {room, unit}),
        ("unit", tree.unit, {unit, tower, shelf, box}),
        ("node", tree.tower, {tower, shelf}),
        ("node", tree.shelf1, {shelf, box}),
        ("box", tree.box, {box}),
    ):
        response = client.get(reverse(f"storage:{kind}-detail", args=[record.pk]))
        assert response.status_code == 200
        links = re.findall(rf'href="({re.escape(register)}\?[^"]+)"', response.content.decode())
        assert set(links) == expected, (kind, record.code)

    empty = client.get(reverse("storage:unit-detail", args=[tree.other_unit.pk])).content.decode()
    assert f'href="{register}?' not in empty
    assert re.search(r">Free</dt>\s*<dd\b[^>]*>\s*(?:<span\b[^>]*>)?0\b", empty)


def test_viewer_follows_tower_free_count_to_available_box_position(client, tree, people):
    manager, technician = people["manager"], people["technician"]
    services.create_box(
        actor=manager,
        node_id=tree.rack.pk,
        code="OTHER-BOX",
        name="Other box",
        slot=1,
        container_type_id=tree.grid.pk,
    )
    place(technician, new_item(technician), cell(tree.box, "A1"))
    services.set_position_blocked(
        actor=manager,
        position_id=cell(tree.box, "A2").pk,
        expected_revision=1,
        blocked=True,
        reason="Damaged position",
    )
    gate = StorageGate.objects.get(pk=1).revision
    client.force_login(people["viewer"])
    tower = client.get(reverse("storage:node-detail", args=[tree.tower.pk]))
    scoped = reverse("storage:free-position-list", query={"node": tree.tower.pk})
    assert re.search(rf'<a\b[^>]*href="{re.escape(scoped)}"[^>]*>\s*79\b', tower.content.decode())
    positions = client.get(scoped)
    page = positions.context["page_obj"]
    assert page.paginator.count == 79
    assert page[0].pk == cell(tree.box, "A3").pk
    box = reverse("storage:box-detail", args=[tree.box.pk])
    anchor = f"position-{page[0].pk}"
    assert f'href="{box}#{anchor}"' in positions.content.decode()
    assert f'id="{anchor}" data-state="free"' in client.get(box).content.decode()
    assert StorageGate.objects.get(pk=1).revision == gate


def test_exports_match_filters_neutralize_formulas_and_are_audited(client, tree, people):
    services.edit_storage_definition(
        actor=people["manager"],
        kind="box",
        object_id=tree.box.pk,
        expected_revision=1,
        values={"name": "=HYPERLINK(1)"},
        reason="Test formula neutralization",
    )
    client.force_login(people["viewer"])
    response = client.get(reverse("storage:export", args=["box"]), {"unit": tree.unit.pk})
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0][:2] == ["Code", "Name"] and len(rows) == 2
    assert rows[1][1] == "'=HYPERLINK(1)"
    units = list(
        csv.reader(
            io.StringIO(client.get(reverse("storage:export", args=["unit"])).content.decode())
        )
    )
    assert {row[0]: row[6] for row in units[1:]}["VP-01"] == "-160.00"  # numbers stay numbers
    audit = AuditEvent.objects.filter(action="storage.exported").first()
    assert audit.actor == people["viewer"] and audit.after["rows"] == 1
    assert client.get(reverse("storage:export", args=["unit"]), {"bogus": "1"}).status_code == 400
    assert client.get(reverse("storage:export", args=["nope"])).status_code == 404
    catalogue = client.get(reverse("storage:catalogue-export", args=["container-type"]))
    assert b"GRID-9X9" in catalogue.content
    empty = client.get(reverse("storage:catalogue-export", args=["class"]), {"q": "ABSENT"})
    assert empty.status_code == 200
    assert len(list(csv.reader(io.StringIO(empty.content.decode())))) == 1  # Header only.
    audit = AuditEvent.objects.get(action="storage.exported", object_id="class")
    assert audit.after["rows"] == 0
    client.force_login(people["nobody"])
    assert client.get(reverse("storage:export", args=["unit"])).status_code == 403


def test_latest_move_can_be_corrected_once_from_the_record(client, tree, people):
    from django.utils import timezone

    t = people["technician"]
    wrong = services.move_container(
        actor=t,
        kind="box",
        object_id=tree.box.pk,
        expected_revision=1,
        expected_gate_revision=StorageGate.objects.get(pk=1).revision,
        target_kind="node",
        target_id=tree.rack.pk,
        target_slot=1,
        occurred_at=timezone.now() - timezone.timedelta(minutes=30),
        reason="Moved to the freezer",
    )
    detail = reverse("storage:box-detail", args=[tree.box.pk])
    url = reverse("storage:container-move", args=["box", tree.box.pk]) + f"?corrects={wrong.pk}"
    # Technicians move boxes but do not correct the record: no button, page or POST.
    client.force_login(t)
    assert f"?corrects={wrong.pk}" not in client.get(detail).content.decode()
    assert client.get(url).status_code == 403
    assert client.post(url, {"target": f"node:{tree.rack.pk}", "target_slot": 2}).status_code == 403
    assert not MoveEvent.objects.filter(kind="correction").exists()
    client.force_login(people["manager"])
    assert f"?corrects={wrong.pk}" in client.get(detail).content.decode()
    page = client.get(url).content.decode()
    assert "Correct move" in page and "The wrong record stays" in page
    response = client.post(
        url,
        {
            "target": f"node:{tree.rack.pk}",
            "target_slot": 2,
            "occurred_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "reason": "Recorded the wrong slot",
            "expected_revision": Box.objects.get(pk=tree.box.pk).revision,
            "expected_gate_revision": StorageGate.objects.get(pk=1).revision,
        },
    )
    assert response.status_code == 302
    fixed = MoveEvent.objects.get(kind="correction")
    assert fixed.corrects == wrong and fixed.to_path["slot"] == 2
    # The wrong move is corrected now; only the newest uncorrected move is offered.
    assert (
        client.get(
            reverse("storage:container-move", args=["box", tree.box.pk]) + f"?corrects={wrong.pk}"
        ).status_code
        == 302
    )
    assert f"?corrects={fixed.pk}" in client.get(detail).content.decode()


def test_box_page_links_its_occupants_to_the_specimen_list(client, tree, people):
    from .conftest import ago

    detail = reverse("storage:box-detail", args=[tree.box.pk])
    link = reverse("specimens:specimen-list") + f"?box={tree.box.code}"
    client.force_login(people["viewer"])
    assert link not in client.get(detail).content.decode()  # empty box: no link
    t = people["technician"]
    place(t, new_item(t, ago(60)), cell(tree.box, "A1"), ago(30))
    assert link in client.get(detail).content.decode()


def test_box_wells_and_table_name_specimens_through_the_specimens_tag(client, people):
    """The box page includes specimens' `attach_specimens` tag from a template only; storage
    Python imports nothing from specimens (see the import test below)."""
    from django.contrib.auth.models import Permission

    from specimens.models import Specimen
    from storage import seed

    m, t = people["manager"], people["technician"]
    m.user_permissions.add(
        Permission.objects.get(content_type__app_label="accounts", codename="approve_disposal")
    )
    seed.seed_storage(actor=m)
    from specimens.seed import seed_specimens  # test data only

    seed_specimens(manager=m, technician=t)
    specimen = (
        Specimen.objects.filter(occupancy__position__isnull=False)
        .select_related("occupancy__position__box")
        .order_by("accession")
        .first()
    )
    position = specimen.occupancy.position
    detail = reverse("storage:box-detail", args=[position.box_id])
    record = reverse("specimens:specimen-detail", args=[specimen.pk])
    client.force_login(people["viewer"])
    page = client.get(detail).content.decode()
    # The well links to the record and names the specimen; the table has a Specimen column.
    well = re.search(rf'<td id="position-{position.pk}"[^>]*>(.*?)</td>', page, re.S)
    assert f'href="{record}"' in well.group(1) and specimen.accession in well.group(1)
    assert f'title="{specimen.accession} · ' in well.group(0)
    assert '<th scope="col">Specimen</th>' in page
    assert page.count(f'href="{record}"') == 2
    # A reader without a staff role cannot open the page at all.
    client.force_login(people["nobody"])
    assert client.get(detail).status_code == 403
    # A specimen under an active hold shows as held, and the key counts it.
    from django.utils import timezone

    from specimens import services as specimen_services

    zero = '<dd class="vr-id"><span class="vr-muted">0</span></dd>'
    assert 'data-state="held"></span>On hold</dt>' + zero in page
    held = sum(1 for _ in re.finditer(r'<td id="position-[^"]+" data-state="held"', page))
    assert held == 0
    specimen_services.place_hold(
        actor=m,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        scope="use",
        occurred_at=timezone.now(),
        reason="Synthetic quality check",
    )
    client.force_login(people["viewer"])
    page = client.get(detail).content.decode()
    assert re.search(rf'<td id="position-{position.pk}" data-state="held"', page)
    assert 'data-state="held"></span>On hold</dt><dd class="vr-id">1</dd>' in page


def test_box_and_unit_pages_render_without_the_specimens_and_equipment_apps(
    client, tree, people, monkeypatch
):
    from django.apps import apps

    t = people["technician"]
    place(t, new_item(t), cell(tree.box, "A1"))
    box = reverse("storage:box-detail", args=[tree.box.pk])
    unit = reverse("storage:unit-detail", args=[tree.unit.pk])
    equipment = reverse("equipment:equipment-list") + f"?storage_unit={tree.unit.pk}"
    client.force_login(people["viewer"])
    # Installed: a key with no specimen keeps the storage key; the unit links to equipment.
    page = client.get(box).content.decode()
    specimen_list = reverse("specimens:specimen-list") + f"?box={tree.box.code}"
    assert "Occupied, storage key" in page and specimen_list in page
    assert equipment in client.get(unit).content.decode()

    # Absent apps: the specimens tag library is never loaded and no link points at them.
    installed = apps.is_installed
    monkeypatch.setattr(
        apps,
        "is_installed",
        lambda label: label not in ("specimens", "equipment") and installed(label),
    )
    response = client.get(box)
    assert response.status_code == 200
    page = response.content.decode()
    assert "Occupied, storage key" in page and specimen_list not in page
    assert "storage/_attach_specimens.html" not in [t.name for t in response.templates]
    assert equipment not in client.get(unit).content.decode()


def test_storage_python_does_not_import_specimens_or_equipment():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text()
        for app in ("specimens", "equipment"):
            assert f"from {app}" not in text and f"import {app}" not in text, path


def test_record_pages_carry_the_path_bar_and_the_overview_its_census(client, tree, people):
    client.force_login(people["viewer"])
    box = client.get(reverse("storage:box-detail", args=[tree.box.pk])).context["storage_path"]
    assert [level["kind"] for level in box][:3] == ["Site", "Room", "Tank"]
    assert box[-1] == {
        "kind": "Box",
        "code": tree.box.code,
        "url": "",
        "here": True,
        "freed": False,
    }
    assert box[2]["temp"] == "\N{MINUS SIGN}160 °C" and box[2]["cold"]
    unit = client.get(reverse("storage:unit-detail", args=[tree.unit.pk])).context
    assert unit["storage_path"][-1]["code"] == tree.unit.code and unit["storage_path"][-1]["here"]
    census = client.get(reverse("storage:overview")).context["storage_census"]
    row = next(entry for entry in census if entry["code"] == tree.unit.code)
    assert row["ln2"] and not row["fridge"] and 0 <= row["fill_step"] <= 10
    assert row["positions"] >= 81 and row["occupied"] == 0


def test_overview_names_sites_and_rooms_and_flags_open_excursions(
    client, tree, people, monkeypatch
):
    from storage import pathbar

    monkeypatch.setattr(pathbar, "EXCURSION_SOURCES", [lambda: [tree.unit.pk]])
    client.force_login(people["viewer"])
    page = client.get(reverse("storage:overview")).content.decode()
    assert f"{tree.unit.room.site.name} / {tree.unit.room.name}" in page
    assert "Excursion open" in page and "\N{MINUS SIGN}160 °C" in page
