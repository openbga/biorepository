"""Small shared helpers for the browser suite."""

import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import Group
from playwright.sync_api import Page, expect

REPOSITORY_TZ = ZoneInfo("America/New_York")


def local_input(delta: timedelta) -> str:
    """Value for a datetime-local input, in repository local time."""
    return (datetime.now(REPOSITORY_TZ) + delta).strftime("%Y-%m-%dT%H:%M")


def field(scope, name: str):
    """A form control by exact label text; the required-field marker may follow it."""
    return scope.get_by_label(re.compile(rf"^{re.escape(name)}\s*\*?$"))


def choose(select, value: str) -> None:
    """Enter `value` in a list-or-other control: a listed option, else "Other" with the text."""
    if value in select.locator("option").all_text_contents():
        select.select_option(label=value)
    else:
        select.select_option(value="__other__")
        select.page.locator(f"#{select.get_attribute('id')}_other").fill(value)


def expect_chosen(select, value: str) -> None:
    """The list-or-other control holds `value` ("" for nothing chosen)."""
    if not value or value in select.locator("option").all_text_contents():
        expect(select).to_have_value(value)
    else:
        expect(select).to_have_value("__other__")
        other = select.page.locator(f"#{select.get_attribute('id')}_other")
        expect(other).to_be_visible()
        expect(other).to_have_value(value)


def described_text(page: Page, field_id: str) -> str:
    """Text of the elements an invalid field points at through aria-describedby."""
    control = page.locator(f"#{field_id}")
    expect(control).to_have_attribute("aria-invalid", "true")
    return control.evaluate(
        "el => el.getAttribute('aria-describedby').split(' ')"
        ".map(id => document.getElementById(id).innerText).join(' ')"
    )


def sign_in(page: Page, username: str, password: str, *, visit: bool = True) -> None:
    """Fill the sign-in form; ``visit=False`` keeps the current page (and its ``next``)."""
    if visit:
        page.goto("/accounts/login/")
    page.get_by_label(re.compile("^(Username or email|Login)"), exact=False).fill(username)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    # A key pressed before the next document has loaded is lost.
    page.wait_for_load_state()


def sign_out(page: Page, who: str = r"^(Demo |E2E )") -> None:
    """Sign out through the user menu; ``who`` matches the menu button (the user's display name)."""
    page.get_by_role("button", name=re.compile(who)).click()
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("heading", level=1, name="Sign in")).to_be_visible()


def make_user(username: str, password: str, *roles: str, superuser: bool = False):
    """A synthetic account in the given roles (admin, manager, technician, viewer).

    ``superuser`` also makes it staff and superuser, which Django admin requires.
    """
    from accounts.models import User

    user = User.objects.create_user(username, f"{username}@example.invalid", password)
    user.first_name = "E2E"
    user.last_name = username
    user.is_staff = user.is_superuser = superuser
    user.save(update_fields=["first_name", "last_name", "is_staff", "is_superuser"])
    for role in roles:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def column_values(page: Page, header: str) -> list[str]:
    """Visible text of one table column, located by its header text."""
    return page.locator("table.vr-table").first.evaluate(
        """(table, header) => {
            const heads = [...table.tHead.rows[0].cells];
            // textContent: innerText would follow the headers' small-capitals label style.
            const label = th => th.textContent.replace(/\\s+/g, " ").trim();
            const index = heads.findIndex(th => label(th) === header);
            if (index < 0) return null;
            return [...table.tBodies[0].rows].map(row => row.cells[index].innerText.trim());
        }""",
        header,
    )


def column_headers(page: Page) -> list[str]:
    return page.locator("table.vr-table").first.evaluate(
        "table => [...table.tHead.rows[0].cells]"
        ".map(th => th.textContent.replace(/\\s+/g, ' ').trim())"
    )


def contrast_ratio(foreground, background) -> float:
    """WCAG 2.x contrast ratio between two sRGB triples (0-255)."""

    def luminance(rgb):
        def channel(value):
            value /= 255
            return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

        r, g, b = (channel(v) for v in rgb[:3])
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


THEME_SAMPLES_JS = (Path(__file__).parent / "theme_samples.js").read_text()


def theme_samples(page: Page) -> dict:
    """Foreground/background pairs of key elements as rendered now, with the minimum ratio."""
    return page.evaluate(THEME_SAMPLES_JS)


def contrast_failures(samples: dict) -> list[str]:
    """Human-readable list of sampled pairs below their WCAG minimum (empty when all pass)."""
    failures = []
    for label, pair in samples["pairs"].items():
        ratios = [contrast_ratio(fg, bg) for fg, bg in pair["candidates"]]
        if max(ratios) < pair["minimum"]:
            failures.append(
                f"{samples['scheme']}: {label} {max(ratios):.2f}:1 < {pair['minimum']}:1"
            )
    return failures


def _poppler(tool: str) -> str:
    """Path of a poppler command. Missing locally: skip; missing where PDFs are required: fail."""
    found = shutil.which(tool)
    if found:
        return found
    message = f"{tool} (poppler-utils) is needed to check PDF content"
    if os.environ.get("REQUIRE_WEASYPRINT"):
        raise AssertionError(message)
    pytest.skip(message)


def pdf_text(path) -> str:
    """Text of a downloaded PDF through pdftotext (the file is data; the path is an argument).

    Reading (content-stream) order, not -layout: -layout merges table columns line by line, so a
    phrase wrapped inside one cell (font metrics vary by platform) is split by its neighbours.
    """
    done = subprocess.run(
        [_poppler("pdftotext"), "-raw", str(path), "-"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, f"pdftotext failed: {done.stderr[:200]}"
    return re.sub(r"[ \t]+", " ", done.stdout)


def pdf_pages(path) -> int:
    done = subprocess.run(
        [_poppler("pdfinfo"), str(path)], capture_output=True, text=True, timeout=60, check=True
    )
    return int(re.search(r"^Pages:\s+(\d+)", done.stdout, re.M).group(1))


def grid_search(page: Page, label: str):
    """The search box of a list's own filter toolbar (not the lookup in the top bar)."""
    return page.get_by_role("search", name=label).get_by_role("searchbox")


def save_download(page: Page, trigger, target: Path) -> bytes:
    """Click/run ``trigger`` (a callable) that starts a download and save it to ``target``."""
    with page.expect_download() as info:
        trigger()
    info.value.save_as(target)
    return target.read_bytes()


def make_announcement(title: str = "Synthetic maintenance notice", **values):
    """A synthetic announcement showing now on the sign-in page and for staff."""
    from datetime import timedelta

    from django.utils import timezone

    from core.models import Announcement

    defaults = {
        "title": title,
        "body": "Example text only. Nothing here is real.",
        "show_on_sign_in": True,
        "show_to_staff": True,
        "starts_at": timezone.now() - timedelta(hours=1),
    }
    return Announcement.objects.create(**(defaults | values))
