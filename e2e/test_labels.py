"""Labels for several specimens print from the specimen list selection, by mouse and keyboard.

The specimen journey prints one specimen's label from its record; this covers the list's
bulk selection: the preview shows each selected specimen and the PDF carries every ID.
"""

import re

from helpers import pdf_text
from playwright.sync_api import Page, expect


def test_selected_specimens_print_their_labels(page: Page, sign_in_as, tmp_path):
    sign_in_as("technician")
    page.goto("/specimens/")
    boxes = page.get_by_role("checkbox", name=re.compile(r"^Select (?!all)"))
    picked = []
    for index in range(2):
        box = boxes.nth(index)
        picked.append(box.get_attribute("aria-label") or box.evaluate("e => e.labels[0].innerText"))
        box.check()
    # The third by keyboard: focus its box and press Space.
    boxes.nth(2).focus()
    page.keyboard.press("Space")
    expect(boxes.nth(2)).to_be_checked()
    picked.append(
        boxes.nth(2).evaluate("e => e.getAttribute('aria-label') || e.labels[0].innerText")
    )
    accessions = [re.sub(r"^Select\s+", "", text).strip() for text in picked]

    page.get_by_role("button", name="Print labels").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("label", ignore_case=True)
    for accession in accessions:
        expect(page.get_by_text(accession, exact=True).first).to_be_visible()
    href = page.get_by_role("link", name="Download PDF").get_attribute("href")
    response = page.request.get(href)
    assert response.ok and response.body().startswith(b"%PDF")
    target = tmp_path / "labels.pdf"
    target.write_bytes(response.body())
    text = pdf_text(target)
    assert all(accession in text for accession in accessions), (accessions, text[:200])
