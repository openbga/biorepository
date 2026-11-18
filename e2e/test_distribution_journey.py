"""Distribution journey: a manager records a study and a participant's consent; a technician
enters a request; another person with the request decision permission approves it; the manager
allocates (a specimen of a participant without a consent record is shown as not allocatable,
with the recorded reason) and opens the pick list; the
technician picks, packs (checked by the manager), dispatches and records the receipt; a returned
specimen is reviewed; finally the manager withdraws consent and the participant's specimens are
held. Catalogues, agreement and specimens are set up through the services. Synthetic data only."""

import re
from datetime import timedelta

import pytest
from helpers import field, make_user, native_select, pick, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "e2e-Distribution-pass-1"


def dialog(page: Page):
    found = page.locator("#vr-form-dialog")
    expect(found).to_be_visible()
    return found


def heading(page: Page):
    return page.get_by_role("heading", level=1)


def pick_option(select, pattern):
    """Select the first option whose text matches `pattern`."""
    texts = native_select(select).locator("option").all_text_contents()
    label = next(t for t in texts if re.search(pattern, t))
    pick(select, label=label)


def action(page: Page, name):
    """Click a header action, opening the More menu when it lives there."""
    link = page.locator("main").get_by_role("link", name=name, exact=True).first
    if not link.is_visible():
        page.get_by_role("button", name="More", exact=True).first.click()
    link.click()


def submit(form, name):
    form.get_by_role("button", name=name, exact=True).click()


def setup_world():
    """Storage and specimens demo, catalogues, an executed MTA. Only P-000123 gets a consent."""
    from django.contrib.auth.models import Permission
    from django.utils import timezone

    from distribution import services
    from specimens.models import Participant
    from specimens.seed import seed_specimens
    from storage.seed import seed_storage

    manager = make_user("dist_manager", PASSWORD, "manager")
    manager.user_permissions.add(
        Permission.objects.get(content_type__app_label="accounts", codename="approve_disposal")
    )
    tech = make_user("dist_tech", PASSWORD, "technician")
    decider = make_user("dist_decider", PASSWORD, "manager")
    decider.user_permissions.add(
        Permission.objects.get(content_type__app_label="accounts", codename="approve_distribution")
    )
    seed_storage(actor=manager)
    seed_specimens(manager=manager, technician=tech)

    def catalogue(kind, **values):
        return services.save_distribution_catalogue(
            actor=manager, kind=kind, object_id=None, expected_revision=None, values=values
        )

    categories = {
        code: catalogue("category", code=code, name=code.title(), gates_use=True)
        for code in ("GENERAL", "COMMERCIAL")
    }
    institution = catalogue(
        "institution", code="EXU", name="Example University", address="100 Example Avenue"
    )
    catalogue(
        "investigator",
        code="INV-001",
        name="Example Investigator",
        institution=institution.pk,
        email="investigator@example.invalid",
    )
    today = timezone.localdate()
    agreement = services.save_agreement(
        actor=manager,
        agreement_id=None,
        expected_revision=None,
        values={
            "kind": "mta",
            "title": "Synthetic MTA",
            "institution": institution.pk,
            "data_level": "coded",
        },
        permitted_categories=(),
    )
    services.execute_agreement(
        actor=manager,
        agreement_id=agreement.pk,
        expected_revision=agreement.revision,
        executed_on=today - timedelta(days=5),
        effective_on=today - timedelta(days=5),
        expires_on=today + timedelta(days=365),
        document_reference="Agreements cabinet (synthetic)",
        reason="Signed",
    )
    return categories, Participant.objects.get(code="P-000123")


def test_request_to_return_and_withdrawal(page: Page, transactional_db):
    from distribution.models import ConsentForm, Shipment, SpecimenRequest, Study
    from specimens.models import Specimen

    categories, participant = setup_world()

    # 1. The manager records the study, links collection DEMO, adds the form and the consent.
    sign_in(page, "dist_manager", PASSWORD)
    page.goto("/distribution/studies/")
    action(page, "New study")
    form = dialog(page)
    field(form, "Code").fill("DEMO-STUDY")
    field(form, "Title").fill("Demonstration study (synthetic)")
    submit(form, "Create study")
    expect(heading(page)).to_have_text("DEMO-STUDY")
    action(page, "Link collection")
    form = dialog(page)
    pick_option(field(form, "Collection"), "^DEMO")
    field(form, "Reason").fill("Collection governed under the study")
    submit(form, "Link collection")
    expect(page.locator("main")).to_contain_text("DEMO")

    from accounts.models import User
    from distribution import services

    manager = User.objects.get(username="dist_manager")
    services.save_distribution_catalogue(
        actor=manager,
        kind="consent_form",
        object_id=None,
        expected_revision=None,
        values={
            "study": Study.objects.get(code="DEMO-STUDY").pk,
            "code": "DEMO-BROAD",
            "version": "1",
            "title": "Broad consent (synthetic)",
            "kind": "broad",
            "categories": [c.pk for c in categories.values()],
        },
    )
    assert ConsentForm.objects.count() == 1

    # From the study's consent list, with the participant and study filled in, so the only
    # active form version is chosen and its categories are asked.
    study = Study.objects.get(code="DEMO-STUDY")
    page.goto(f"/distribution/consents/new/?participant=P-000123&study={study.pk}")
    form = dialog(page) if page.locator("#vr-form-dialog").is_visible() else page.locator("main")
    form.get_by_label("Consented", exact=True).check()
    pick(form.get_by_label(re.compile("^GENERAL")), label="Permitted")
    pick(form.get_by_label(re.compile("^COMMERCIAL")), label="Refused")
    field(form, "Date of the decision").fill(
        (__import__("datetime").date.today() - timedelta(days=30)).isoformat()
    )
    field(form, "Reason").fill("Transcribed from the filed form")
    submit(form, "Record consent")
    expect(heading(page)).to_contain_text("P-000123")
    expect(page.locator("main")).to_contain_text("Consented")
    sign_out(page)

    # 2. The technician enters the request and its line, and submits it.
    sign_in(page, "dist_tech", PASSWORD)
    page.goto("/distribution/requests/")
    action(page, "New request")
    form = dialog(page)
    field(form, "Title").fill("Plasma for assay development")
    pick_option(field(form, "Investigator"), "INV-001")
    field(form, "Purpose").fill("Method validation (synthetic)")
    pick(field(form, "Received by"), label="Email")
    form.get_by_label(re.compile("GENERAL")).check()
    submit(form, "Create request")
    # Creating a request continues straight to its lines.
    form = dialog(page)
    expect(form).to_contain_text("Add its lines")
    pick_option(form.locator("select[name$='specimen_type']").first, "^PLASMA")
    form.locator("input[name$='count']").first.fill("2")
    submit(form, "Save lines")
    expect(heading(page)).to_have_text(re.compile(r"^RQ-\d{6}$"))
    request_url = page.url
    expect(page.locator("main")).to_contain_text("PLASMA")
    action(page, "Submit for decision")
    submit(dialog(page), "Submit for decision")
    expect(page.locator("main")).to_contain_text("Awaiting decision")
    sign_out(page)

    # 3. Another person with the decision permission approves it.
    sign_in(page, "dist_decider", PASSWORD)
    page.goto(request_url)
    action(page, "Decide request")
    form = dialog(page)
    form.get_by_label("Approve", exact=True).check()
    field(form, "Reason").fill("Within the consented scope")
    submit(form, "Record decision")
    expect(page.locator("main")).to_contain_text("Approved")
    sign_out(page)

    # 4. The manager links the agreement and allocates two specimens.
    sign_in(page, "dist_manager", PASSWORD)
    page.goto(request_url)
    action(page, "Link agreement")
    form = dialog(page)
    pick(field(form, "Agreement"), index=1)
    field(form, "Reason").fill("Umbrella MTA")
    submit(form, "Link agreement")
    expect(page.locator("main")).not_to_contain_text("No agreement")
    action(page, "Allocate")
    form = dialog(page)
    expect(form).to_contain_text("No consent recorded for P-000124 in DEMO-STUDY.")
    expect(
        form.get_by_label(re.compile("^Allocate VP-")).and_(form.locator("[disabled]")).first
    ).to_be_disabled()
    boxes = form.locator("input[type=checkbox][name='specimen']:not([disabled])")
    expect(boxes.first).to_be_visible()
    assert boxes.count() >= 2
    boxes.nth(0).check()
    boxes.nth(1).check()
    field(form, "Reason").fill("Allocated for the approved request")
    submit(form, "Allocate")
    expect(page.get_by_text(re.compile("Allocated 2 specimens"))).to_be_visible()
    request = SpecimenRequest.objects.get()
    assert request.allocations.filter(state="allocated").count() == 2
    page.goto(request_url + "pick-list/")
    expect(heading(page)).to_contain_text("Pick list")
    sign_out(page)

    # 5. The technician picks, prepares and packs the shipment; the manager checks it.
    sign_in(page, "dist_tech", PASSWORD)
    page.goto(request_url)
    action(page, "Pick specimens")
    form = dialog(page)
    for radio in form.get_by_label("Picked", exact=True).all():
        radio.check()
    field(form, "Reason").fill("Retrieved for shipment")
    submit(form, "Record pick")
    expect(page.locator("main")).to_contain_text("Picked")
    action(page, "New shipment")
    submit(dialog(page), "Create shipment")
    # Creating a shipment continues straight to its contents.
    form = dialog(page)
    expect(form).to_contain_text("Choose its contents")
    for box in form.locator("#contents-grid input[type=checkbox]").all():
        box.check()
    submit(form, "Save contents")
    expect(heading(page)).to_have_text(re.compile(r"^SH-\d{6}$"))
    shipment_url = page.url
    action(page, "Pack")
    form = dialog(page)
    pick(field(form, "Temperature class"), label="−80 °C")
    pick(field(form, "Refrigerant"), label="Dry ice")
    field(form, "Dry ice").fill("5.00")
    pick(field(form, "Classification as declared by the shipper"), label="Exempt specimen")
    field(form, "Packages").fill("1")
    field(form, "Emergency contact").fill("Repository duty phone +1 555 0199")
    form.get_by_role("group", name=re.compile("training")).get_by_label("Yes").check()
    pick_option(field(form, "Checked by"), "dist_manager")
    field(form, "Reason").fill("Packed on dry ice")
    submit(form, "Pack")
    expect(page.locator("main")).to_contain_text("Packed")
    page.goto(shipment_url + "manifest/")
    expect(page.locator("main")).to_contain_text("Not dispatched")
    page.goto(shipment_url)
    action(page, "Dispatch")
    form = dialog(page)
    field(form, "Courier").fill("Example Courier")
    field(form, "Tracking number").fill("EX123")
    field(form, "Reason").fill("Collected by the courier")
    submit(form, "Dispatch")
    expect(page.locator("main")).to_contain_text("Dispatched")
    shipment = Shipment.objects.get()
    accessions = list(shipment.items.order_by("line").values_list("accession", flat=True))
    assert len(accessions) == 2
    assert set(
        Specimen.objects.filter(accession__in=accessions).values_list("status", flat=True)
    ) == {"distributed"}

    action(page, "Record receipt")
    form = dialog(page)
    from datetime import datetime

    from helpers import REPOSITORY_TZ

    field(form, "Received at").fill(datetime.now(REPOSITORY_TZ).strftime("%Y-%m-%dT%H:%M:%S"))
    field(form, "Received by").fill("Example Receiving Desk")
    pick(field(form, "Condition"), label="As expected")
    pick(field(form, "Refrigerant on arrival"), label="Refrigerant remaining")
    pick(field(form, "Reported by"), label="Email")
    field(form, "Reason").fill("Recipient's email")
    submit(form, "Record receipt")
    expect(page.locator("main")).to_contain_text("Receipt recorded")

    page.get_by_role("link", name=re.compile("^Record return of ")).last.click()
    form = dialog(page)
    field(form, "Amount returned").fill("0.5")
    field(form, "Reason").fill("Returned unused")
    submit(form, "Record return")
    expect(page.locator("main")).to_contain_text("Returned")
    sign_out(page)

    # 6. The manager reviews the return and then records a withdrawal.
    sign_in(page, "dist_manager", PASSWORD)
    page.goto(shipment_url)
    page.get_by_role("link", name=re.compile("^Review return of ")).first.click()
    form = dialog(page)
    form.get_by_label(re.compile("^Release for use")).check()
    field(form, "Reason").fill("Seal intact")
    submit(form, "Record review")
    expect(page.locator("main")).to_contain_text("Recorded the review of")

    page.goto("/distribution/consents/?participant=P-000123")
    page.get_by_role("link", name="P-000123 · DEMO-STUDY", exact=True).first.click()
    expect(heading(page)).to_contain_text("P-000123")
    action(page, "Withdraw consent")
    form = dialog(page)
    form.get_by_label("No further use", exact=True).check()
    field(form, "Received via").fill("Letter (synthetic)")
    field(form, "Reason").fill("Participant asked to stop use")
    submit(form, "Withdraw consent")
    main = page.locator("main")
    expect(main).to_contain_text("Withdrawn")
    held = Specimen.objects.filter(
        participant=participant,
        status__in=("available", "exhausted"),
        holds__scope="use",
        holds__release__isnull=True,
    ).distinct()
    assert held.exists()
    for accession in held.values_list("accession", flat=True):
        expect(main.get_by_role("link", name=accession, exact=True).first).to_be_visible()
    assert not page.console_errors
