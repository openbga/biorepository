"""Every form in the dialog, by mouse and by keyboard (phase 3b; data-driven over FORM_SURFACES).

The sweep crawls the full synthetic demo as the manager, then the administrator, and records for
each route in core.form_surfaces.FORM_SURFACES the first page that links to it and the link.
Routes join automatically as their pages start linking them. For every form found it checks:

- Mouse: the link opens the form in the dialog. Emptying its text fields and submitting keeps
  the dialog open with an inline error, and moves focus to the first invalid field or the error
  summary.
- Keyboard: Enter on the focused link opens the dialog with focus inside it. After a change,
  Escape asks "Discard unsaved changes?". Escape there means Keep editing. Discard changes
  closes the dialog and returns focus to the page.
- Controls: Escape inside an open select list or date calendar closes that control, not the
  dialog.
- Fix and submit: required fields are filled with obviously synthetic values and the form is
  submitted. It runs last, with forms that change a record's state after those that create or
  edit, because a successful submission changes the demo. The outcome is reported, not
  asserted: a domain rule may still refuse generic values.

FORM_SWEEP_APPS=distribution,quality limits the sweep to route prefixes ("core" = no prefix);
FORM_SWEEP_REPORT=<path> writes the per-form outcomes as JSON.
"""

import json
import os
import re
import time
from collections import deque
from urllib.parse import urlsplit

import pytest
from django.core.management import call_command
from django.urls import Resolver404, resolve
from django.utils import timezone
from helpers import sign_in, sign_out
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect
from test_sweeps import LINKS, local, shape

from accounts.models import User
from core.form_surfaces import FORM_SURFACES

pytestmark = pytest.mark.django_db(transaction=True)

MAX_PAGES = 1500
# Record pages of one route shape (one specimen, one request...) are visited up to this many times,
# so forms that only a record in another state links (Reopen, Record review) are found too.
RECORDS_PER_SHAPE = 8
# Route names that change a record's state when submitted successfully: these run last.
STATE_CHANGE = re.compile(
    r"cancel|terminate|withdraw|dispos|execute|reopen|close|submit|decide|review|dispatch|pack|"
    r"unpack|pick|put-back|release|retire|block|reinstate|escalate|hold|complete|correct|end|"
    r"reverse|move|apply|deactivate|generate|notify|receipt|return",
    re.I,
)
# Text fields of the form that posts (not a search form that only filters candidates).
FORM = "#vr-form-content form:not([method=get])"
TEXT_FIELDS = (
    f"{FORM} input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file])"
    ":not([type=submit]):not([type=search]):not([disabled]):not([readonly])"
    ":not(.ts-control input)"
    f", {FORM} textarea:not([disabled]):not([readonly])"
)


def app_of(route: str) -> str:
    return route.split(":", 1)[0] if ":" in route else "core"


def selected_apps():
    raw = os.environ.get("FORM_SWEEP_APPS", "").strip()
    return {part.strip() for part in raw.split(",") if part.strip()} or None


@pytest.fixture
def full_demo(transactional_db, monkeypatch):
    """The full synthetic demonstration (every record state), with a per-run random password."""
    import secrets

    password = secrets.token_urlsafe(18)
    monkeypatch.setenv("DEMO_PASSWORD", password)
    call_command("seed_demo", full=True, verbosity=0)
    return password


def form_route(path: str) -> str | None:
    try:
        name = resolve(urlsplit(path).path).view_name
    except Resolver404:
        return None
    return name if name in FORM_SURFACES else None


# URL sections of each route prefix, so a FORM_SWEEP_APPS run crawls only its own pages.
SECTIONS = {
    "distribution": ("/distribution/",),
    "quality": ("/quality/",),
    "equipment": ("/equipment/",),
    "storage": ("/storage/",),
    "specimens": ("/specimens/",),
    "core": ("/accounts/", "/repository/"),
}


def in_scope(path: str, apps) -> bool:
    if not apps:
        return True
    return any(path.startswith(p) for app in apps for p in SECTIONS.get(app, ()))


def discover(page: Page, base: str, account: str, found: dict, apps=None) -> None:
    """Crawl as one account; record the first opener of each form route it is shown."""
    queue, seen, visited = deque([("/", 0)]), {}, 0
    done = set()
    while queue and visited < MAX_PAGES:
        path, depth = queue.popleft()
        key = shape(path)
        limit = RECORDS_PER_SHAPE if "<id>" in key else 1
        if path in done or seen.get(key, 0) >= limit:
            continue
        seen[key] = seen.get(key, 0) + 1
        done.add(path)
        response = page.goto(path)
        if response is None or response.status >= 400:
            continue
        if "text/html" not in response.headers.get("content-type", ""):
            continue
        if urlsplit(page.url).path != urlsplit(path).path:
            continue
        visited += 1
        for href, in_nav in page.evaluate(LINKS):
            target = local(href, base, page.url)
            if not target:
                continue
            route = form_route(target)
            if route:
                found.setdefault(route, {"account": account, "opener": path, "href": href})
                continue
            if urlsplit(target).query and not (in_nav or depth == 0):
                continue
            if not in_scope(target, apps):
                continue
            queue.append((target, 1 if in_nav else depth + 1))


def link_for(page: Page, href: str):
    """The opener link, revealing it from a More menu when it lives there."""
    link = page.locator(f'main a[href="{href}"]').first
    if link.count() and not link.is_visible():
        more = page.locator("main .dropdown").filter(has=page.locator(f'a[href="{href}"]'))
        more.get_by_role("button").first.click()
    return link


class LeftPage(Exception):
    """The opener led to another page (re-authentication, a sign-in step), not the dialog."""


OPENED = """opener => {
    const dialog = document.getElementById('vr-form-dialog');
    const content = document.getElementById('vr-form-content');
    if (dialog && dialog.classList.contains('show') && content
        && content.getAttribute('aria-busy') === 'false'
        && content.querySelector('form, [role=alert]')) return 'dialog';
    if (location.pathname !== opener) return 'left:' + location.pathname;
    return '';
}"""


# What the sweep's accounts need to confirm access (set by the test: password, app secret).
SESSION = {}


def confirm_access(page: Page) -> None:
    """Answer allauth's recent-authentication step: the password, or a code from the app."""
    challenge = page.url
    if page.get_by_label("Password").count():
        page.get_by_label("Password").fill(SESSION["password"])
    else:
        page.get_by_label("Code").fill(totp_code())
    page.get_by_role("button", name="Confirm").click()
    expect(page).not_to_have_url(challenge)


def totp_code() -> str:
    """Leave time to submit the app code and never reuse one from the last challenge."""
    from allauth.mfa import app_settings
    from allauth.mfa.totp.internal.auth import format_hotp_value, hotp_value

    period = app_settings.TOTP_PERIOD
    while True:
        now = time.time()
        counter = int(now) // period
        remaining = (counter + 1) * period - now
        code = format_hotp_value(hotp_value(SESSION["secret"], counter))
        if remaining >= period / 2 and code != SESSION.get("last_totp_code"):
            SESSION["last_totp_code"] = code
            return code
        time.sleep(remaining)


def ready(page: Page, opener: str = ""):
    if opener:
        outcome = page.wait_for_function(OPENED, arg=opener, timeout=10000).json_value()
        if outcome.startswith("left:") and "reauthenticate" in outcome:
            # By design: a two-factor change asks to confirm access on its own page first,
            # then opens in the dialog (tests/test_form_dialog.py guards that).
            confirm_access(page)
            outcome = "dialog"
        if outcome.startswith("left:"):
            raise LeftPage(outcome[5:])
    dialog = page.locator("#vr-form-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#vr-form-content")).to_have_attribute("aria-busy", "false")
    return dialog


def open_by_mouse(page: Page, item: dict):
    page.goto(item["opener"])
    link_for(page, item["href"]).click()
    return ready(page, urlsplit(item["opener"]).path)


def blocked(dialog) -> bool:
    return dialog.locator(".vr-form-actions button[type=submit]").count() == 0


OUTCOME = """() => {
    const dialog = document.getElementById('vr-form-dialog');
    const confirm = document.getElementById('vr-form-discard');
    if (confirm && confirm.classList.contains('show')) return 'confirm';
    if (!dialog || !dialog.classList.contains('show')) return 'closed';
    return '';
}"""


IN_CONFIRM = "() => !!document.activeElement.closest('#vr-form-discard')"


def settle_confirm(page: Page, item: dict, problems: list) -> None:
    """Wait for the discard question to take focus (Bootstrap moves it once shown)."""
    try:
        page.wait_for_function(IN_CONFIRM, timeout=3000)
    except PlaywrightError:
        problems.append(f"{item['route']}: focus did not move into the discard question")


def after_escape(page: Page) -> str:
    """'closed' or 'confirm': what Escape led to, once its animation has settled."""
    handle = page.wait_for_function(OUTCOME, timeout=5000)
    return handle.json_value()


def close(page: Page) -> None:
    """Leave the dialog, discarding any change."""
    dialog = page.locator("#vr-form-dialog")
    if not dialog.is_visible():
        return
    page.keyboard.press("Escape")
    if after_escape(page) == "confirm":
        page.locator("#vr-form-discard").get_by_role("button", name="Discard changes").click()
    expect(dialog).to_be_hidden()


FOCUSED_ON_ERROR = """() => {
    const el = document.activeElement;
    if (!el || !el.closest('#vr-form-dialog')) return false;
    if (el.getAttribute('aria-invalid') === 'true') return true;
    if (el.closest('[role=alert], .alert')) return true;
    const group = el.closest('.vr-field, fieldset');
    return !!(group && group.querySelector('[aria-invalid=true]'));
}"""
DESCRIBE_FOCUS = """() => {
    const el = document.activeElement;
    if (!el || !el.closest('#vr-form-dialog')) return 'outside: ' + (el ? el.tagName : '');
    return el.outerHTML.slice(0, 80);
}"""


def enter(page: Page, account: str, live_server) -> None:
    """Sign in as one of the sweep's accounts."""
    from sweep_states import NO_PASSWORD, WITH_APP

    if account == NO_PASSWORD:  # no usable password: a session made on the server
        from django.test import Client

        client = Client()
        client.force_login(User.objects.get(username=account))
        page.context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": client.cookies["sessionid"].value,
                    "url": live_server.url,
                }
            ]
        )
        page.goto("/")
        return
    sign_in(page, account, SESSION["password"])
    if account == WITH_APP:
        page.get_by_label("Code").fill(totp_code())
        page.get_by_role("button", name="Sign in").click()
        expect(page).to_have_url(live_server.url + "/")


def leave(page: Page) -> None:
    """Sign out from the dashboard: some account pages have no top bar."""
    page.goto("/")
    sign_out(page)


def close_quietly(page: Page) -> None:
    try:
        close(page)
    except PlaywrightError, AssertionError:
        page.goto("/")  # a fresh page has no dialog open


def check_mouse_error(page: Page, item: dict, problems: list, notes: dict) -> None:
    dialog = open_by_mouse(page, item)
    if blocked(dialog):
        notes["mouse"] = "blocked: Not available in this record's state"
        close(page)
        return
    emptied = page.evaluate(
        """selector => {
            let n = 0;
            for (const el of document.querySelectorAll(selector)) {
                if (!el.getClientRects().length || !el.value) continue;
                el.value = '';
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                n += 1;
            }
            return n;
        }""",
        TEXT_FIELDS,
    )
    required_empty = page.evaluate(
        """selector => [...document.querySelectorAll(selector)]
            .filter(el => el.required && !el.value && el.getClientRects().length).length""",
        TEXT_FIELDS,
    )
    if not emptied and not required_empty:
        notes["mouse"] = "no text field to empty: error check skipped"
        close(page)
        return
    dialog.locator(".vr-form-actions button[type=submit]").last.click()
    try:
        shown = dialog.locator("[aria-invalid=true]:visible, [role=alert]:visible")
        expect(shown.first).to_be_visible()
    except AssertionError:
        if not dialog.is_visible():
            notes["mouse"] = "submitted with the text fields empty: nothing was required"
            return
        problems.append(f"{item['route']}: no inline error after submitting empty fields")
        close(page)
        return
    try:  # the error fragment is swapped in, then focus moves: give it a moment
        page.wait_for_function(FOCUSED_ON_ERROR, timeout=3000)
        focus = "ok"
    except PlaywrightError:
        focus = page.evaluate(DESCRIBE_FOCUS)
    if focus != "ok":
        problems.append(f"{item['route']}: focus after the error is not on it ({focus})")
    notes["mouse"] = "inline error with focus"
    close(page)


def check_keyboard(page: Page, item: dict, problems: list, notes: dict) -> None:
    page.goto(item["opener"])
    link = link_for(page, item["href"])
    link.focus()
    page.keyboard.press("Enter")
    dialog = ready(page, urlsplit(item["opener"]).path)
    if not page.evaluate("() => !!document.activeElement.closest('#vr-form-dialog')"):
        problems.append(f"{item['route']}: Enter opened the dialog without moving focus into it")
    field = page.locator(TEXT_FIELDS).filter(visible=True).first
    if blocked(dialog) or not field.count():
        close(page)
        notes["keyboard"] = "opened and closed (no field to change)"
        return
    field.focus()
    # Replace the value rather than append: a field at its maximum length ignores typing.
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type("x")
    page.keyboard.press("Escape")
    confirm = page.locator("#vr-form-discard")
    if after_escape(page) != "confirm":
        problems.append(f"{item['route']}: Escape after a change did not ask to discard")
        return
    settle_confirm(page, item, problems)
    page.keyboard.press("Escape")  # means Keep editing
    expect(confirm, "Escape in the discard question should mean Keep editing").to_be_hidden()
    if not dialog.is_visible():
        problems.append(f"{item['route']}: Escape in the discard question closed the form")
        return
    page.keyboard.press("Escape")
    expect(confirm, "Escape again should ask again").to_be_visible()
    settle_confirm(page, item, problems)
    confirm.get_by_role("button", name="Discard changes").focus()
    page.keyboard.press("Enter")
    expect(dialog, "Enter on Discard changes should close the form").to_be_hidden()
    try:  # Bootstrap returns focus once the dialog has finished hiding
        page.wait_for_function("() => document.activeElement !== document.body", timeout=3000)
    except PlaywrightError:
        problems.append(f"{item['route']}: focus was lost after Discard changes")
    notes["keyboard"] = "opened, discard asked, kept, discarded, focus returned"


def check_controls(page: Page, item: dict, problems: list, notes: dict) -> None:
    dialog = open_by_mouse(page, item)
    checked = []
    picker = dialog.locator(".ts-control").filter(visible=True).first
    if picker.count():
        picker.click()
        wrapper = dialog.locator(".ts-wrapper.dropdown-active")
        if wrapper.count():
            page.keyboard.press("Escape")
            if wrapper.count() or not dialog.is_visible():
                problems.append(f"{item['route']}: Escape in an open select did not just close it")
            checked.append("select")
    date = dialog.locator("input.vr-date-text").filter(visible=True).first
    if date.count() and dialog.is_visible():
        date.focus()
        page.keyboard.press("Alt+ArrowDown")
        if date.get_attribute("aria-expanded") == "true":
            page.keyboard.press("Escape")
            if date.get_attribute("aria-expanded") == "true" or not dialog.is_visible():
                problems.append(f"{item['route']}: Escape in an open calendar closed more")
            checked.append("date")
    notes["controls"] = ", ".join(checked) or "no select or date control"
    close(page)


def fill_and_submit(page: Page, item: dict, notes: dict) -> None:
    dialog = open_by_mouse(page, item)
    if blocked(dialog):
        notes["submit"] = "blocked"
        close(page)
        return
    today = timezone.localdate().isoformat()
    page.evaluate(
        """([selector, today]) => {
            for (const el of document.querySelectorAll(selector)) {
                if (!el.required || el.value || !el.getClientRects().length) continue;
                const hint = (el.getAttribute('aria-describedby') || '').split(' ')
                    .map(id => (document.getElementById(id) || {}).textContent || '').join(' ');
                if (el.classList.contains('vr-date-text')) {
                    el.value = hint.includes('HH:MM') ? today + ' 08:00' : today;
                } else if (/numeric|decimal/.test(el.getAttribute('inputmode') || '')) {
                    el.value = '1';
                } else {
                    el.value = 'Synthetic sweep value';
                }
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }
            const posting = '#vr-form-content form:not([method=get]) select';
            for (const select of document.querySelectorAll(posting)) {
                if (!select.required || select.value || select.disabled) continue;
                const option = [...select.options].find(o => o.value);
                if (!option) continue;
                if (select.tomselect) select.tomselect.setValue(option.value);
                else select.value = option.value;
                select.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }""",
        [TEXT_FIELDS + ", #vr-form-content form input.vr-date-text", today],
    )
    dialog.locator(".vr-form-actions button[type=submit]").last.click()
    try:
        expect(dialog).to_be_hidden(timeout=8000)
        notes["submit"] = "saved"
    except AssertionError:
        messages = dialog.locator(".invalid-feedback:visible, .alert:visible > :not(.vr-status)")
        texts = [t.strip() for t in messages.all_inner_texts() if t.strip()]
        notes["submit"] = "refused: " + (" | ".join(texts)[:200] or "?")
        close(page)


def test_every_form_opens_and_works_by_mouse_and_keyboard(page: Page, full_demo, live_server):
    from sweep_states import prepare

    base = live_server.url
    SESSION["password"] = full_demo
    openers, SESSION["secret"] = prepare(full_demo)
    apps = selected_apps()
    found = {}
    for account in ("demo_manager", "demo_admin"):
        enter(page, account, live_server)
        discover(page, base, account, found, apps)
        leave(page)
    # Forms the crawl did not reach: open the page prepared for them, as the account that can.
    pending = {}
    for route, (account, opener) in openers.items():
        if (route not in found or account not in ("demo_manager", "demo_admin")) and (
            not apps or app_of(route) in apps
        ):
            pending.setdefault(account, []).append((route, opener))
    for account, routes in pending.items():
        enter(page, account, live_server)
        for route, opener in routes:
            page.goto(opener)
            for href, _nav in page.evaluate(LINKS):
                target = local(href, base, page.url)
                if target and form_route(target) == route:
                    found[route] = {"account": account, "opener": opener, "href": href}
                    break
        leave(page)
    items = sorted(
        (dict(route=route, **info) for route, info in found.items()),
        key=lambda i: (bool(STATE_CHANGE.search(i["route"])), i["route"]),
    )
    if apps:
        items = [i for i in items if app_of(i["route"]) in apps]
    assert items, "the crawl found no form links"
    problems, report = [], {}
    not_linked = sorted(
        route for route in set(FORM_SURFACES) - set(found) if not apps or app_of(route) in apps
    )
    problems += [f"{route}: no page links this form for the sweep to open" for route in not_linked]
    by_account = {}
    for item in items:
        by_account.setdefault(item["account"], []).append(item)
    for phase in ("checks", "submit"):
        for account, group in by_account.items():
            enter(page, account, live_server)
            for item in group:
                notes = report.setdefault(item["route"], {"opener": item["opener"]})
                steps = (
                    [
                        ("mouse", check_mouse_error),
                        ("keyboard", check_keyboard),
                        ("controls", check_controls),
                    ]
                    if phase == "checks"
                    else [("submit", None)]
                )
                for step, check in steps:
                    try:
                        if check:
                            check(page, item, problems, notes)
                        else:
                            fill_and_submit(page, item, notes)
                    except LeftPage as exc:
                        notes[step] = f"left the page for {exc} (no dialog to check)"
                        page.goto("/")
                    except (PlaywrightError, AssertionError) as exc:
                        message = str(exc).splitlines()[0][:160]
                        if check:
                            problems.append(
                                f"{item['route']} [{step}, from {item['opener']}]: {message}"
                            )
                        else:
                            notes["submit"] = f"not reached: {message}"
                        close_quietly(page)
            leave(page)
    if os.environ.get("FORM_SWEEP_REPORT"):
        with open(os.environ["FORM_SWEEP_REPORT"], "w") as handle:
            json.dump(
                {"forms": report, "not_linked": not_linked},
                handle,
                indent=2,
            )
    assert not problems, f"{len(problems)} problems in {len(items)} forms:\n" + "\n".join(problems)
