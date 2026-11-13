"""Screenshot set: platform, storage and specimen pages in both themes at desktop,
tablet and narrow widths. The storage and specimen pages show the full demonstration
(``seed_demo --full``), so the images carry a working repository's records.

Opt in with ``--screenshots-dir DIR``. The images are for human inspection and for the README
and presentation (docs/images, website/about/assets); the only assertion is that no page needs
horizontal scrolling (tables scroll inside their own wrapper).
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import DESKTOP, NARROW, TABLET
from django.core.management import call_command
from django.urls import reverse
from helpers import make_announcement, sign_in
from playwright.sync_api import Page

from accounts.models import User
from distribution.models import Consent, Shipment, SpecimenRequest
from equipment.models import Equipment, Excursion
from specimens.models import DisposalRequest, Hold, ImportBatch, Specimen
from storage.models import Box, StorageUnit

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.screenshots]

VIEWPORTS = {"desktop": DESKTOP, "tablet": TABLET, "narrow": NARROW}
# page key -> (role whose session is used, viewports to capture)
PAGES = {
    "login": (None, ("desktop", "tablet", "narrow")),
    "dashboard": ("admin", ("desktop", "tablet", "narrow")),
    "announcements": ("admin", ("desktop", "tablet", "narrow")),
    "announcements-empty": ("admin", ("desktop",)),
    "announcement-form": ("admin", ("desktop", "narrow")),
    "record-retention": ("viewer", ("desktop", "narrow")),
    "forbidden": ("viewer", ("desktop",)),
    # The full demonstration, as the repository manager sees it.
    "manager-dashboard": ("manager", ("desktop", "narrow")),
    "storage-overview": ("manager", ("desktop", "narrow")),
    "storage-units": ("manager", ("desktop",)),
    "storage-unit": ("manager", ("desktop", "narrow")),
    "storage-box": ("manager", ("desktop", "narrow")),
    "storage-free": ("manager", ("desktop",)),
    "specimens": ("manager", ("desktop", "tablet", "narrow")),
    "specimens-out": ("manager", ("desktop",)),
    "specimen-record": ("manager", ("desktop", "narrow")),
    "specimen-held": ("manager", ("desktop",)),
    "disposals": ("manager", ("desktop", "narrow")),
    "disposal-record": ("manager", ("desktop",)),
    "imports": ("manager", ("desktop",)),
    "import-errors": ("manager", ("desktop",)),
    "register": ("technician", ("desktop",)),
    "equipment": ("manager", ("desktop", "narrow")),
    "equipment-record": ("manager", ("desktop", "narrow")),
    "due": ("manager", ("desktop",)),
    "ln2": ("manager", ("desktop",)),
    "readings": ("manager", ("desktop",)),
    "excursions": ("manager", ("desktop",)),
    "excursion-record": ("manager", ("desktop", "narrow")),
    "requests": ("manager", ("desktop", "narrow")),
    "request-record": ("manager", ("desktop", "narrow")),
    "request-awaiting": ("manager", ("desktop",)),
    "shipments": ("manager", ("desktop",)),
    "shipment-record": ("manager", ("desktop", "narrow")),
    "consent-record": ("manager", ("desktop", "narrow")),
    "agreements": ("manager", ("desktop", "narrow")),
}
# Strings, or functions of the seeded records for pages whose address holds an identifier.
URLS = {
    "login": "/accounts/login/",
    "dashboard": "/",
    "announcements": "/repository/announcements/",
    "announcements-empty": "/repository/announcements/?q=no-such-announcement-xyz",
    "announcement-form": "/repository/announcements/new/",
    "record-retention": "/repository/record-retention/",
    "forbidden": "/repository/announcements/",
    "manager-dashboard": "/",
    "storage-overview": "/storage/",
    "storage-units": "/storage/units/",
    "storage-unit": lambda: reverse("storage:unit-detail", args=[_get(StorageUnit, code="VP-01")]),
    "storage-box": lambda: reverse("storage:box-detail", args=[_get(Box, code="BOX-004")]),
    "storage-free": "/storage/free-positions/",
    "specimens": "/specimens/",
    "specimens-out": "/specimens/?checked_out=yes",
    "specimen-record": lambda: reverse(
        "specimens:specimen-detail",
        args=[Specimen.objects.filter(children__isnull=False).order_by("accession")[0].pk],
    ),
    "specimen-held": lambda: reverse(
        "specimens:specimen-detail",
        args=[Hold.objects.filter(release__isnull=True, scope="all_use").first().specimen_id],
    ),
    "disposals": "/specimens/disposals/",
    "disposal-record": lambda: reverse(
        "specimens:disposal-detail",
        args=[DisposalRequest.objects.filter(decision__isnull=True).latest("recorded_at").pk],
    ),
    "imports": "/specimens/imports/",
    "import-errors": lambda: reverse(
        "specimens:import-detail",
        args=[ImportBatch.objects.exclude(errors=[]).latest("recorded_at").pk],
    ),
    "register": "/specimens/register/",
    "equipment": "/equipment/",
    "equipment-record": lambda: reverse(
        "equipment:equipment-detail", args=[_get(Equipment, code="TANK-VP-01")]
    ),
    "due": "/equipment/due/",
    "ln2": "/equipment/ln2/",
    "readings": "/equipment/readings/",
    "excursions": "/equipment/excursions/",
    "excursion-record": lambda: reverse(
        "equipment:excursion-detail", args=[Excursion.objects.filter(status="closed")[0].pk]
    ),
}


def _progressed_request():
    """The approved request furthest along: most shipments, then most allocations."""
    from django.db.models import Count

    return (
        SpecimenRequest.objects.filter(status="approved")
        .annotate(n_ship=Count("shipments", distinct=True), n_alloc=Count("allocations"))
        .order_by("-n_ship", "-n_alloc", "id")
        .first()
    )


def _shipment():
    from django.db.models import Count

    return (
        Shipment.objects.filter(state__in=["dispatched", "received"])
        .annotate(n=Count("items"))
        .order_by("-n", "id")
        .first()
    )


URLS.update(
    {
        "requests": "/distribution/requests/",
        "request-record": lambda: reverse(
            "distribution:request-detail", args=[_progressed_request().pk]
        ),
        "request-awaiting": lambda: reverse(
            "distribution:request-detail",
            args=[SpecimenRequest.objects.filter(status="submitted").latest("id").pk],
        ),
        "shipments": "/distribution/shipments/",
        "shipment-record": lambda: reverse("distribution:shipment-detail", args=[_shipment().pk]),
        "consent-record": lambda: reverse(
            "distribution:consent-detail",
            args=[Consent.objects.filter(status="withdrawn").first().pk],
        ),
        "agreements": "/distribution/agreements/",
    }
)


@dataclass
class FullDemo:
    password: str  # shared by the announced accounts
    admin_password: str


def _get(model, **where):
    return str(model.objects.get(**where).pk)


@pytest.fixture
def full_demo(demo, monkeypatch):
    """The working repository, seeded through the real command on top of the basic demo, with
    the shared demo accounts announced on the sign-in page as a public demonstration does.

    The shared password is an obviously synthetic one so the sign-in page image can show it;
    the administrator keeps a private one, as the seed requires before it will publish."""
    shared = "example-demo-password"
    monkeypatch.setenv("DEMO_PASSWORD", shared)
    monkeypatch.setenv("DEMO_RESET_NOTE", "Everything entered here is erased every day.")
    for user in User.objects.all():
        user.set_password(demo.password if user.username == "demo_admin" else shared)
        user.save()
    call_command("seed_demo", full=True, announce_accounts=True, verbosity=0)
    return FullDemo(password=shared, admin_password=demo.password)


def test_screenshots(page: Page, full_demo, browser, browser_context_args, request):
    demo = full_demo
    out = Path(request.config.getoption("--screenshots-dir"))
    out.mkdir(parents=True, exist_ok=True)

    # Synthetic announcements: one of each importance, one beside the sign-in form.
    make_announcement("Synthetic planned maintenance", tone="warning")
    make_announcement("Synthetic holiday hours", tone="info", show_on_sign_in=False)

    # One real sign-in per role; the saved state is reused for every theme and width.
    states = {None: None}
    for role in {role for role, _ in PAGES.values() if role}:
        sign_in(page, f"demo_{role}", demo.admin_password if role == "admin" else demo.password)
        states[role] = page.context.storage_state()
        page.context.clear_cookies()

    manifest = []
    for theme in ("light", "dark"):
        for size_name, size in VIEWPORTS.items():
            for role in states:
                keys = [
                    key for key, (r, sizes) in PAGES.items() if r == role and size_name in sizes
                ]
                if not keys:
                    continue
                options = {
                    **browser_context_args,
                    "viewport": size,
                    "color_scheme": theme,
                    "storage_state": states[role],
                    "is_mobile": size_name == "narrow",
                    "has_touch": size_name == "narrow",
                }
                context = browser.new_context(**options)
                context.add_init_script(f"localStorage.setItem('vr-theme', '{theme}')")
                shot = context.new_page()
                shot.set_default_timeout(10_000)
                for key in keys:
                    url = URLS[key]
                    shot.goto(url() if callable(url) else url)
                    shot.wait_for_load_state("networkidle")
                    overflow = shot.evaluate(
                        "document.documentElement.scrollWidth - window.innerWidth"
                    )
                    name = f"{key}-{theme}-{size_name}.png"
                    # Desktop/tablet: what is visible on arrival. Narrow: the whole stacked page.
                    shot.screenshot(path=str(out / name), full_page=size_name == "narrow")
                    manifest.append({"file": name, "horizontal_overflow_px": overflow})
                context.close()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    overflowing = [m for m in manifest if m["horizontal_overflow_px"] > 1]
    assert not overflowing, f"Pages needing horizontal page scroll: {overflowing}"


# Images for the README and the presentation: the full demonstration as the repository manager
# sees it, with no announcements, at 1280 x 800 and twice the pixel density, in the light theme.
# name -> (address or function, id of the section scrolled to the top, or None)
PUBLICATION = {
    "dashboard": ("/", None),
    "box": (lambda: reverse("storage:box-detail", args=[_get(Box, code="BOX-006")]), "box-grid"),
    "specimen": (URLS["specimen-record"], "custody-heading"),
    "disposal": (
        lambda: reverse(
            "specimens:disposal-detail",
            args=[
                next(
                    r.pk
                    for r in DisposalRequest.objects.filter(decision__isnull=True)
                    .order_by("-recorded_at")
                    .select_related("specimen")
                    if not r.specimen.holds.filter(release__isnull=True).exists()
                )
            ],
        ),
        None,
    ),
    "import": (URLS["import-errors"], None),
    "specimens": ("/specimens/?checked_out=yes", None),
    "equipment": (
        lambda: reverse("equipment:equipment-detail", args=[_get(Equipment, code="TANK-VP-01")]),
        None,
    ),
    "excursion": (
        lambda: reverse(
            "equipment:excursion-detail", args=[Excursion.objects.filter(status="closed")[0].pk]
        ),
        None,
    ),
    "request": (URLS["request-record"], None),
    "shipment": (URLS["shipment-record"], None),
}
PUBLICATION_VIEWPORT = {"width": 1280, "height": 800}


def test_publication_screenshots(page: Page, full_demo, browser, browser_context_args, request):
    """Regenerate with ``pytest -c e2e/pytest.ini e2e/test_screenshots.py -k publication
    --screenshots-dir DIR``. The `pub-*.png` files (2560 x 1600) are the sources for the README
    (docs/images/*.png, 1280 px wide) and the presentation (website/about/assets/*-1280.webp and
    dashboard-2560.webp); `cwebp -q 82` or Pillow converts them. Retake them after any visual
    change to the pages they show."""
    out = Path(request.config.getoption("--screenshots-dir"))
    out.mkdir(parents=True, exist_ok=True)
    sign_in(page, "demo_manager", full_demo.password)
    state = page.context.storage_state()
    context = browser.new_context(
        **{
            **browser_context_args,
            "viewport": PUBLICATION_VIEWPORT,
            "device_scale_factor": 2,
            "color_scheme": "light",
            "storage_state": state,
        }
    )
    context.add_init_script("localStorage.setItem('vr-theme', 'light')")
    shot = context.new_page()
    shot.set_default_timeout(10_000)
    for name, (url, section) in PUBLICATION.items():
        shot.goto(url() if callable(url) else url)
        shot.wait_for_load_state("networkidle")
        if section:
            shot.evaluate(
                "id => { const el = document.getElementById(id);"
                " window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 76); }",
                section,
            )
        shot.screenshot(path=str(out / f"pub-{name}.png"))
    context.close()
    # The sign-in page as a visitor to a public demonstration sees it.
    visitor = browser.new_context(
        **{
            **browser_context_args,
            "viewport": PUBLICATION_VIEWPORT,
            "device_scale_factor": 2,
            "color_scheme": "light",
        }
    )
    visitor.add_init_script("localStorage.setItem('vr-theme', 'light')")
    sign_in_page = visitor.new_page()
    sign_in_page.goto("/accounts/login/")
    sign_in_page.wait_for_load_state("networkidle")
    sign_in_page.screenshot(path=str(out / "pub-signin.png"))
    visitor.close()
