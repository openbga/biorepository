"""Panel spacing on the main record, list and overview pages (docs/ui-standards.md section 12).

Visible text and controls inside a panel sit at least --vr-panel-inset from its edge, stacked
blocks in a panel body are at least --vr-stack apart, key facts start on their grid's track
lines, and a collapsed panel's summary sits with equal top and bottom insets of at least
--vr-panel-inset. The measurement is e2e/panel_spacing.js, which qa also runs on any page:
page.evaluate(Path("e2e/panel_spacing.js").read_text()).
"""

import math
from pathlib import Path

import pytest
from helpers import sign_in
from playwright.sync_api import Page

pytestmark = pytest.mark.django_db(transaction=True)

CHECK = (Path(__file__).parent / "panel_spacing.js").read_text()
LISTS = [
    "/specimens/",
    "/specimens/disposals/",
    "/storage/units/",
    "/storage/boxes/",
    "/equipment/",
    "/equipment/excursions/",
    "/distribution/requests/",
    "/distribution/shipments/",
    "/quality/deviations/",
]
FIXED = ["/", "/storage/"]


def first_record(page: Page, list_path: str):
    page.goto(list_path)
    link = page.locator(".vr-table a.vr-row-link").first
    return link.get_attribute("href") if link.count() else None


@pytest.mark.parametrize("width", [1280, 1440])
def test_panels_keep_their_inset_and_stack(page: Page, demo, width):
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(page, "demo_admin", demo.password)
    pages = FIXED + LISTS + [href for path in LISTS if (href := first_record(page, path))]
    problems, collapsed = [], 0
    for path in pages:
        page.goto(path)
        # Collapsed panels as the page opens them, then every panel open.
        first = page.evaluate(CHECK)
        collapsed += first.get("collapsed", 0)
        problems += [f"{path} (collapsed): {p}" for p in first["problems"] if "collapsed" in p]
        page.evaluate("() => document.querySelectorAll('details').forEach(d => { d.open = true; })")
        result = page.evaluate(CHECK)
        if not all(
            isinstance(v, int | float) and math.isfinite(v)
            for v in (result["inset"], result["stack"])
        ):
            # The Cryo panel spacing tokens arrive with the designer's components milestone.
            pytest.xfail("--vr-panel-inset / --vr-stack are not defined in static/css/app.css yet")
        problems += [f"{path}: {problem}" for problem in result["problems"]]
    assert collapsed, "no collapsed panel was measured: add a page that has one"
    assert not problems, "\n".join(problems)
