"""Separation-of-duties and gate refusals, checked in the browser (AGENTS.md rule 4).

A missing button is not a gate. Each check signs in as the person the rule excludes, opens the
action page directly, submits the form anyway (the page offers no submit button, so the test
submits it by script, as a crafted request would), and confirms the server refused it: the
page names the reason and the database records nothing. Accounts and records are synthetic.
"""

import re
import secrets

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from helpers import make_user, sign_in
from playwright.sync_api import Page, expect

from distribution import services as distribution_services
from distribution.models import RequestDecision, SpecimenRequest
from specimens import services as specimen_services
from specimens.models import DisposalDecision, Hold, Specimen
from specimens.queries import specimen_queryset

PASSWORD = "e2e-guards-" + secrets.token_urlsafe(9)

FORM = "[...document.querySelectorAll('form')].find(f => f.querySelector('[name=reason]'))"

# What a crafted request sends: the form's own fields, re-enabled, with a reason and approval.
SUBMIT_ANYWAY = f"""() => {{
    const form = {FORM};
    form.querySelectorAll('[disabled]').forEach(field => {{ field.disabled = false; }});
    form.querySelector('[name=reason]').value = 'Submitted around the missing button (synthetic)';
    const choice = form.querySelector('[name=decision][value=approve]');
    if (choice) choice.checked = true;
    HTMLFormElement.prototype.submit.call(form);  // no browser validation, as a script would
}}"""


@pytest.fixture
def full_demo(transactional_db, monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", secrets.token_urlsafe(18))
    call_command("seed_demo", full=True, verbosity=0)


def grant(user, codename):
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="accounts", codename=codename)
    )
    return user


def submit_anyway(page: Page, url: str, refusal: str) -> None:
    page.goto(url)
    blocked = page.get_by_text(refusal).first
    expect(blocked).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("^(Approve|Record decision)"))).to_have_count(
        0
    )
    page.wait_for_function(f"() => !!({FORM})")
    with page.expect_response(lambda r: r.request.method == "POST") as response:
        page.evaluate(SUBMIT_ANYWAY)
    assert response.value.status < 500, response.value.status
    page.wait_for_load_state()
    expect(page.get_by_text(refusal).first).to_be_visible()


def test_nobody_approves_their_own_disposal_request(page: Page, full_demo, live_server):
    requester = grant(make_user("guard_disposer", PASSWORD, "technician"), "approve_disposal")
    stored = specimen_queryset({"status": "available", "placed": "yes", "checked_out": "no"})
    specimen = next(s for s in stored if not Hold.objects.filter(specimen=s).exists())
    request = specimen_services.request_disposal(
        actor=requester,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        method="Autoclave (synthetic)",
        destination="Clinical waste (synthetic)",
        reason="Past retention (synthetic)",
    )
    sign_in(page, requester.username, PASSWORD)
    page.goto(reverse("specimens:disposal-detail", args=[request.pk]))
    expect(page.get_by_text("You requested this.").first).to_be_visible()
    expect(page.get_by_role("link", name="Approve disposal")).to_have_count(0)
    submit_anyway(
        page,
        reverse("specimens:disposal-decide", args=[request.pk]) + "?decision=approve",
        "Another person with disposal approval must approve it.",
    )
    assert not DisposalDecision.objects.filter(request=request).exists()
    assert Specimen.objects.get(pk=specimen.pk).status == "available"


def test_nobody_decides_a_request_they_submitted(page: Page, full_demo, live_server):
    decider = grant(make_user("guard_decider", PASSWORD, "manager"), "approve_distribution")
    model = (
        SpecimenRequest.objects.filter(status="approved", agreement__isnull=False)
        .order_by("pk")
        .first()
    )
    draft = distribution_services.save_request(
        actor=decider,
        request_id=None,
        expected_revision=None,
        values={
            "title": "Self-decision guard (synthetic)",
            "investigator": model.investigator_id,
            "purpose": "Synthetic purpose for the guard check.",
            "received_via": "email",
            "use_categories": [c.pk for c in model.use_categories.all()],
            "studies": [],
        },
        items=(
            distribution_services.ItemSpec(
                line=1, specimen_type_id=model.items.first().specimen_type_id, count=1
            ),
        ),
    )
    distribution_services.submit_request(
        actor=decider, request_id=draft.pk, expected_revision=draft.revision
    )
    sign_in(page, decider.username, PASSWORD)
    submit_anyway(
        page,
        reverse("distribution:request-decide", args=[draft.pk]) + "?decision=approve",
        "Another person with request decision permission must decide it.",
    )
    assert not RequestDecision.objects.filter(submission__request=draft).exists()
    assert SpecimenRequest.objects.get(pk=draft.pk).status == "submitted"


def test_a_use_hold_blocks_check_out_in_the_browser(page: Page, full_demo, live_server):
    technician = make_user("guard_technician", PASSWORD, "technician")
    stored = specimen_queryset({"status": "available", "placed": "yes", "checked_out": "no"})
    specimen = next(s for s in stored if not Hold.objects.filter(specimen=s).exists())
    specimen_services.place_hold(
        actor=technician,
        specimen_id=specimen.pk,
        expected_revision=specimen.revision,
        scope="use",
        occurred_at=timezone.now(),
        reason="Quality review (synthetic)",
    )
    sign_in(page, technician.username, PASSWORD)
    page.goto(reverse("specimens:specimen-detail", args=[specimen.pk]))
    action = page.get_by_text("Record check-out", exact=True).first
    expect(action).to_be_attached()
    assert action.evaluate(
        "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
        " || el.classList.contains('disabled')"
    ), action.evaluate("el => el.outerHTML")
    page.goto(reverse("specimens:check-out", args=[specimen.pk]))
    expect(page.get_by_text("hold", exact=False).first).to_be_visible()
    page.wait_for_function(f"() => !!({FORM})")
    with page.expect_response(lambda r: r.request.method == "POST") as response:
        page.evaluate(SUBMIT_ANYWAY)
    assert response.value.status < 500
    page.wait_for_load_state()
    assert Specimen.objects.get(pk=specimen.pk).open_checkout_id is None


def test_people_without_an_active_role_reach_nothing(page: Page, full_demo, live_server):
    make_user("guard_norole", PASSWORD)
    former = make_user("guard_former", PASSWORD, "admin")
    former.is_active = False
    former.save(update_fields=["is_active"])
    sign_in(page, former.username, PASSWORD)
    expect(
        page.get_by_role("heading", level=1, name=re.compile("^Account inactive$", re.I))
    ).to_be_visible()
    page.goto("/specimens/")
    expect(page.get_by_role("heading", level=1, name="Sign in")).to_be_visible()
    sign_in(page, "guard_norole", PASSWORD)
    for path in ("/specimens/", "/storage/", "/distribution/requests/", "/equipment/"):
        response = page.goto(path)
        assert response.status == 403, (path, response.status)
    lookup = page.request.get("/lookups/active-participants/?q=P")
    assert lookup.status == 403, lookup.status
