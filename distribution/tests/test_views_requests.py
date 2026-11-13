"""Request, allocation, picking, shipment, receipt and return pages: each page renders for the
roles that may read it with only the controls they may use, posts are refused for roles without
permission, decisions keep the four-eyes rule, a blocked allocation names its reasons per
specimen, the manifest of a dispatched shipment comes from its snapshot, and the specimen filters
find the right requests and shipments. All data is synthetic."""

import csv
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent
from distribution import services
from distribution.models import (
    Allocation,
    OutputRecord,
    RequestDecision,
    Shipment,
    ShipmentItem,
    ShipmentReceipt,
    SpecimenRequest,
    SpecimenReturn,
)
from specimens.models import SpecimenType

from .conftest import (
    allocate,
    approved_request,
    dispatch,
    fresh,
    new_request,
    occ_rev,
    packed_shipment,
    pick,
)

pytestmark = pytest.mark.django_db
READERS = ("viewer", "technician", "manager", "admin")
PDF = b"%PDF-1.4\n% synthetic test receipt\n%%EOF\n"


@pytest.fixture(autouse=True)
def without_page_help(monkeypatch):
    """The page help names every control; these tests look for the controls themselves, so the
    help panel is left out of the rendered page (its coverage is tests/test_page_docs.py)."""
    monkeypatch.setattr("core.context_processors.documentation_for_request", lambda request: {})


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def when(moment=None):
    """A form time value to the microsecond, so "now" is never before an event just recorded."""
    return timezone.localtime(moment or timezone.now()).strftime("%Y-%m-%dT%H:%M:%S.%f")


def login(client, user):
    client.force_login(user)
    return client


def url(name, *args):
    return reverse(f"distribution:{name}", args=args)


def page(response):
    return response.content.decode()


@pytest.fixture
def shipped(people, dist, world):
    """An approved request with two specimens dispatched in one shipment."""
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:2])
    pick(people, request, allocations)
    shipment = packed_shipment(people, request, allocations)
    dispatch(people, shipment)
    shipment.refresh_from_db()
    return request, shipment


# --- reading ---------------------------------------------------------------------------------


def test_lists_render_for_every_reading_role_and_refuse_others(client, people, dist, world):
    approved_request(people, dist, world)
    for role in READERS:
        login(client, people[role])
        for name in ("request-list", "shipment-list", "return-list"):
            response = client.get(url(name))
            assert response.status_code == 200, (role, name)
        content = page(client.get(url("request-list")))
        assert ("New request" in content) == (role != "viewer"), role
    login(client, people["nobody"])
    assert client.get(url("request-list")).status_code == 403


def test_request_record_shows_only_the_controls_a_role_may_use(client, people, dist, world):
    request = approved_request(people, dist, world)
    allocate(people, request, dist.specimens[:1])
    detail = url("request-detail", request.pk)

    viewer = page(login(client, people["viewer"]).get(detail))
    assert dist.specimens[0].accession in viewer
    for control in ("Cancel request", "Pick specimens", "Release allocations", "Add note"):
        assert control not in viewer
    assert url("allocate", request.pk, request.items.get().pk) not in viewer
    assert url("pick-list", request.pk) in viewer  # a printout every reader may see

    technician = page(login(client, people["technician"]).get(detail))
    assert "Pick specimens" in technician and "Cancel request" not in technician
    assert url("allocate", request.pk, request.items.get().pk) not in technician

    manager = page(login(client, people["manager"]).get(detail))
    assert "Cancel request" in manager and "Release allocations" in manager
    assert url("allocate", request.pk, request.items.get().pk) in manager


def test_every_request_and_shipment_page_renders(client, people, dist, world, shipped):
    request, shipment = shipped
    item = ShipmentItem.objects.filter(shipment=shipment).first()
    other = approved_request(people, dist, world)
    allocations = allocate(people, other, dist.specimens[2:3])
    draft = new_request(people["technician"], dist, world)
    login(client, people["manager"])
    pages = [
        url("request-create"),
        url("request-detail", draft.pk),
        url("request-edit", draft.pk),
        url("request-items", draft.pk),
        url("request-submit", draft.pk),
        url("request-reopen", other.pk),
        url("request-cancel", other.pk),
        url("request-close", other.pk),
        url("request-agreement", other.pk),
        url("request-note", other.pk),
        url("allocate", other.pk, other.items.get().pk),
        url("allocation-release", other.pk),
        url("pick", other.pk),
        url("put-back", other.pk),
        url("pick-list", other.pk),
        url("shipment-create", other.pk),
        url("shipment-detail", shipment.pk),
        url("shipment-edit", shipment.pk),
        url("shipment-contents", shipment.pk),
        url("shipment-pack", shipment.pk),
        url("shipment-unpack", shipment.pk),
        url("shipment-notify", shipment.pk),
        url("shipment-dispatch", shipment.pk),
        url("shipment-receipt", shipment.pk),
        url("shipment-cancel", shipment.pk),
        url("shipment-note", shipment.pk),
        url("manifest", shipment.pk),
        url("return-record", item.pk),
    ]
    for address in pages:
        response = client.get(address)
        assert response.status_code == 200, (address, response.status_code)
    assert allocations  # the pick and release pages had something to list
    viewer = login(client, people["viewer"])
    for address in (
        url("request-detail", request.pk),
        url("shipment-detail", shipment.pk),
        url("manifest", shipment.pk),
        url("pick-list", other.pk),
    ):
        assert viewer.get(address).status_code == 200, address
    content = page(viewer.get(url("shipment-detail", shipment.pk)))
    assert "Record receipt" not in content and "Record return" not in content


# --- authorization ---------------------------------------------------------------------------


def test_posts_are_refused_for_roles_without_permission(client, people, dist, world, shipped):
    request, shipment = shipped
    item = ShipmentItem.objects.filter(shipment=shipment).first()
    login(client, people["viewer"])
    for address in (
        url("request-create"),
        url("request-note", request.pk),
        url("pick", request.pk),
        url("shipment-note", shipment.pk),
        url("shipment-receipt", shipment.pk),
        url("return-record", item.pk),
    ):
        assert client.post(address, {}).status_code == 403, address
    login(client, people["technician"])
    for address in (
        url("request-cancel", request.pk),
        url("request-close", request.pk),
        url("request-reopen", request.pk),
        url("request-agreement", request.pk),
        url("allocate", request.pk, request.items.get().pk),
        url("allocation-release", request.pk),
        url("shipment-cancel", shipment.pk),
    ):
        assert client.post(address, {"reason": "Not mine to do"}).status_code == 403, address
    # A technician without the grant may not decide, and does not see the control.
    assert client.post(url("request-decide", request.pk), {}).status_code == 403


def test_decide_is_refused_to_the_creator_and_to_a_superuser_without_the_grant(
    client, people, dist, world
):
    request = new_request(people["decider"], dist, world)
    request.refresh_from_db()
    services.submit_request(
        actor=people["decider"], request_id=request.pk, expected_revision=request.revision
    )
    request.refresh_from_db()
    address = url("request-decide", request.pk)
    data = {
        "decision": "approve",
        "reason": "Within scope",
        "expected_revision": request.revision,
        "submission_number": request.latest_submission,
    }

    login(client, people["decider"])
    shown = page(client.get(address))
    assert "You created or submitted this request" in shown
    refused = client.post(address, data)
    assert refused.status_code == 409
    assert not RequestDecision.objects.exists()
    detail = page(client.get(url("request-detail", request.pk)))
    assert "You created or submitted this request" in detail

    login(client, people["superuser"])
    assert client.get(address).status_code == 403
    assert client.post(address, data).status_code == 403
    assert "Decide request" not in page(client.get(url("request-detail", request.pk)))

    login(client, people["decider_manager"])
    response = client.post(address, data)
    assert response.status_code == 302
    assert fresh(request).status == SpecimenRequest.Status.APPROVED


# --- request workflow through the pages ------------------------------------------------------


def test_create_request_add_lines_and_submit(client, people, dist, world):
    login(client, people["technician"])
    response = client.post(
        url("request-create"),
        {
            "title": "Plasma for a pilot (synthetic)",
            "investigator": dist.investigator.pk,
            "received_via": "email",
            "purpose": "Assay development (synthetic)",
            "use_categories": [dist.categories["GENERAL"].pk],
        },
    )
    assert response.status_code == 302
    request = SpecimenRequest.objects.get(title="Plasma for a pilot (synthetic)")
    assert response["Location"] == url("request-items", request.pk)

    grid = url("request-items", request.pk)
    rows = {
        "items-TOTAL_FORMS": "1",
        "items-INITIAL_FORMS": "0",
        "items-MIN_NUM_FORMS": "1",
        "items-MAX_NUM_FORMS": "50",
        "items-0-line": "1",
        "items-0-specimen_type": world.plasma.pk,
        "items-0-count": "2",
        "items-0-minimum_quantity": "",
        "items-0-unit": "",
        "items-0-study": "",
        "items-0-notes": "",
        "expected_revision": request.revision,
        "reason": "Lines as received",
    }
    added = client.post(grid, {**rows, "add_row": "1"})
    assert added.status_code == 200 and 'name="items-1-count"' in page(added)
    assert request.items.count() == 0  # adding a row records nothing
    saved = client.post(grid, rows)
    assert saved.status_code == 302
    assert list(request.items.values_list("line", "count")) == [(1, 2)]

    request.refresh_from_db()
    submitted = client.post(url("request-submit", request.pk), {"expected_revision": 1})
    assert submitted.status_code == 422  # stale: saving the lines changed the revision
    submitted = client.post(
        url("request-submit", request.pk), {"expected_revision": request.revision}
    )
    assert submitted.status_code == 302
    assert fresh(request).status == SpecimenRequest.Status.SUBMITTED


def test_a_blocked_allocation_names_the_reasons_per_specimen(client, people, dist, world):
    request = approved_request(people, dist, world, categories=("COMMERCIAL",))
    item = request.items.get()
    address = url("allocate", request.pk, item.pk)
    login(client, people["manager"])
    shown = page(client.get(address))
    specimen = dist.specimens[0]
    assert "Use not permitted by consent" in shown
    assert f'value="{specimen.pk}:{specimen.revision}"' in shown
    assert "disabled" in shown

    response = client.post(
        address,
        {
            "specimen": [f"{specimen.pk}:{fresh(specimen).revision}"],
            "occurred_at": when(),
            "reason": "Allocated for the request",
            "expected_revision": request.revision,
        },
    )
    assert response.status_code == 422
    content = page(response)
    assert specimen.accession in content and "Use not permitted by consent" in content
    assert "refused COMMERCIAL" in content
    assert not Allocation.objects.exists()


def test_allocate_pick_and_ship_through_the_pages(client, people, dist, world):
    request = approved_request(people, dist, world)
    item = request.items.get()
    specimen = fresh(dist.specimens[0])
    login(client, people["manager"])
    response = client.post(
        url("allocate", request.pk, item.pk),
        {
            "specimen": [f"{specimen.pk}:{specimen.revision}"],
            "occurred_at": when(),
            "reason": "Allocated for the request",
            "expected_revision": fresh(request).revision,
        },
    )
    assert response.status_code == 302
    allocation = Allocation.objects.get()

    login(client, people["technician"])
    specimen = fresh(specimen)
    response = client.post(
        url("pick", request.pk),
        {
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "1",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "200",
            "lines-0-allocation": allocation.pk,
            "lines-0-revision": specimen.revision,
            "lines-0-occupancy_revision": occ_rev(specimen),
            "lines-0-outcome": "picked",
            "lines-0-note": "",
            "recipient": people["technician"].pk,
            "occurred_at": when(),
            "reason": "Retrieved",
            "expected_revision": fresh(request).revision,
        },
    )
    assert response.status_code == 302, page(response)
    assert fresh(allocation).state == Allocation.State.PICKED

    response = client.post(
        url("shipment-create", request.pk),
        {
            "ship_to_name": "Example Investigator",
            "ship_to_institution": "Example University",
            "ship_to_address": "100 Example Avenue",
            "ship_to_contact": "investigator@example.invalid",
            "expected_revision": fresh(request).revision,
        },
    )
    assert response.status_code == 302
    shipment = Shipment.objects.get()
    response = client.post(
        url("shipment-contents", shipment.pk),
        {"allocations": [allocation.pk], "expected_revision": shipment.revision},
    )
    assert response.status_code == 302
    shipment.refresh_from_db()
    response = client.post(
        url("shipment-pack", shipment.pk),
        {
            "temperature_class": "minus80c",
            "refrigerant": "dry_ice",
            "dry_ice_kg": "4.50",
            "classification": "exempt_specimen",
            "package_count": "1",
            "emergency_contact": "Duty phone +1 555 0199",
            "training_confirmed": "yes",
            "checked_by": people["manager"].pk,
            "occurred_at": when(),
            "reason": "Packed",
            "expected_revision": shipment.revision,
        },
    )
    assert response.status_code == 302, page(response)
    shipment.refresh_from_db()
    assert shipment.state == Shipment.State.PACKED and shipment.checked_by == people["manager"]
    response = client.post(
        url("shipment-dispatch", shipment.pk),
        {
            "courier": "Example Courier",
            "tracking": "EX1",
            "occurred_at": when(),
            "reason": "Collected",
            "expected_revision": shipment.revision,
        },
    )
    assert response.status_code == 302, page(response)
    assert fresh(shipment).state == Shipment.State.DISPATCHED


def test_pack_refuses_the_packer_as_checker(client, people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:1])
    pick(people, request, allocations)
    shipment = services.create_shipment(
        actor=people["technician"],
        request_id=request.pk,
        expected_revision=fresh(request).revision,
        values={},
    )
    shipment = services.set_shipment_contents(
        actor=people["technician"],
        shipment_id=shipment.pk,
        expected_revision=shipment.revision,
        allocation_ids=(allocations[0].pk,),
    )
    login(client, people["technician"])
    form = page(client.get(url("shipment-pack", shipment.pk)))
    assert "does not classify" in form
    assert f'value="{people["technician"].pk}"' not in form.split('name="checked_by"')[1][:2000]


# --- receipt, returns and attachments --------------------------------------------------------


def test_receipt_return_and_review_through_the_pages(client, people, shipped):
    request, shipment = shipped
    items = list(ShipmentItem.objects.filter(shipment=shipment).order_by("line"))
    login(client, people["technician"])
    response = client.post(
        url("shipment-receipt", shipment.pk),
        {
            "received_at": when(),
            "received_by_name": "Example Receiver",
            "condition": "discrepancy",
            "refrigerant_on_arrival": "adequate",
            "discrepancies": "One vial thawed",
            "received_via": "document",
            f"outcome_{items[0].pk}": "thawed",
            f"note_{items[0].pk}": "Soft on arrival",
            "attachment": SimpleUploadedFile("receipt.pdf", PDF, "application/pdf"),
            "reason": "Signed receipt",
            "expected_revision": shipment.revision,
        },
    )
    assert response.status_code == 302, page(response)
    receipt = ShipmentReceipt.objects.get()
    assert receipt.exceptions.get().outcome == "thawed"
    download = client.get(url("attachment", "receipt", receipt.pk))
    assert download.status_code == 200 and download.content == PDF

    login(client, people["viewer"])
    assert client.get(url("attachment", "receipt", receipt.pk)).status_code == 200
    assert client.get(url("attachment", "nonsense", receipt.pk)).status_code == 404

    login(client, people["technician"])
    specimen = fresh(items[1].specimen)
    response = client.post(
        url("return-record", items[1].pk),
        {
            "quantity": "0.5",
            "thaw_count": "",
            "condition": "Unopened",
            "recipient": people["technician"].pk,
            "occurred_at": when(),
            "reason": "Returned unused",
            "expected_revision": specimen.revision,
        },
    )
    assert response.status_code == 302, page(response)
    returned = SpecimenReturn.objects.get()
    assert returned.thaw_count is None
    listing = page(client.get(url("return-list") + "?review=awaiting"))
    assert items[1].accession in listing

    assert client.post(url("return-review", returned.pk), {}).status_code == 403
    login(client, people["manager"])
    response = client.post(
        url("return-review", returned.pk),
        {
            "decision": "release",
            "occurred_at": when(),
            "reason": "Seal intact",
            "expected_revision": fresh(specimen).revision,
        },
    )
    assert response.status_code == 302, page(response)
    awaiting = page(client.get(url("return-list") + "?review=awaiting"))
    assert "Nothing matches these filters" in awaiting  # the released return left the queue

    correct = url("receipt-correct", receipt.pk)
    login(client, people["technician"])
    assert client.get(correct).status_code == 403
    login(client, people["manager"])
    assert client.get(correct).status_code == 200


# --- manifest and pick list documents --------------------------------------------------------


def test_manifest_after_dispatch_comes_from_the_snapshot(client, people, dist, world, shipped):
    request, shipment = shipped
    item = ShipmentItem.objects.filter(shipment=shipment).first()
    sent_type = item.specimen_type
    SpecimenType.objects.filter(pk=world.plasma.pk).update(name="Renamed after dispatch")
    login(client, people["viewer"])
    content = page(client.get(url("manifest", shipment.pk)))
    assert sent_type in content and "Renamed after dispatch" not in content
    assert "Not dispatched" not in content
    assert world.participant.code not in content  # codes are excluded unless included
    assert "declared by the shipper" in content

    response = client.get(url("manifest-pdf", shipment.pk))
    assert response.status_code == 200 and response["Content-Type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    record = OutputRecord.objects.get(kind=OutputRecord.Kind.MANIFEST)
    assert record.count == 2 and record.details["from_snapshot"] is True
    assert AuditEvent.objects.filter(
        action="distribution.manifest_printed", object_id=str(shipment.pk)
    ).exists()


def test_manifest_shows_participant_codes_only_when_included(client, people, dist, world):
    request = approved_request(people, dist, world)
    allocations = allocate(people, request, dist.specimens[:1])
    pick(people, request, allocations)
    shipment = packed_shipment(people, request, allocations, include_participant_codes=True)
    login(client, people["viewer"])
    packed = page(client.get(url("manifest", shipment.pk)))
    assert "Not dispatched" in packed and world.participant.code in packed
    dispatch(people, shipment)
    sent = page(client.get(url("manifest", shipment.pk)))
    assert "Not dispatched" not in sent and world.participant.code in sent


def test_pick_list_page_and_pdf(client, people, dist, world):
    request = approved_request(people, dist, world)
    allocate(people, request, dist.specimens[:2])
    login(client, people["viewer"])
    content = page(client.get(url("pick-list", request.pk)))
    assert dist.specimens[0].accession in content and "Download PDF" in content
    response = client.get(url("pick-list-pdf", request.pk))
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert OutputRecord.objects.get(kind=OutputRecord.Kind.PICK_LIST).count == 2
    assert AuditEvent.objects.filter(
        action="distribution.pick_list_printed", object_id=str(request.pk)
    ).exists()


# --- lists, filters and exports --------------------------------------------------------------


def test_specimen_filter_finds_requests_and_shipments(client, people, dist, world, shipped):
    request, shipment = shipped
    other = approved_request(people, dist, world)
    allocate(people, other, dist.specimens[2:3])
    sent, waiting = dist.specimens[0].accession, dist.specimens[2].accession
    login(client, people["viewer"])

    found = page(client.get(url("request-list") + f"?specimen={sent.lower()}"))
    assert request.number in found and other.number not in found
    found = page(client.get(url("request-list") + f"?specimen={waiting}"))
    assert other.number in found and request.number not in found

    found = page(client.get(url("shipment-list") + f"?specimen={sent}"))
    assert shipment.number in found
    found = page(client.get(url("shipment-list") + f"?specimen={waiting}"))
    assert shipment.number not in found and "Nothing matches these filters" in found


def test_list_csv_export_is_exact_and_audited(client, people, shipped):
    request, shipment = shipped
    login(client, people["viewer"])
    response = client.get(url("export", "shipments"))
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0][0] == "shipment" and rows[1][0] == shipment.number
    assert rows[1][rows[0].index("dry_ice_kg")] == "5"
    assert OutputRecord.objects.filter(kind=OutputRecord.Kind.EXPORT).count() == 1
    assert client.get(url("export", "requests") + "?bogus=1").status_code == 400
    assert client.get(url("export", "nonsense")).status_code == 404
