"""Headless sweep of every page each role can reach (phase 3b of the UI modernization).

For each demo role, the sweep crawls from the dashboard: the navigation, the links on each page
and one record per route shape (query-string variants only when the navigation or the dashboard
links them). Every page must load (no 4xx or 5xx from a link the role was shown), log no browser
errors or content-security-policy violations (the autouse fixture in conftest.py fails the test
on those), and fit its viewport without horizontal page overflow at 1440 and 1280 pixels, light
and dark, plus 768 for the pages the navigation links. At 1440 and 1280 every capsule (status,
count, chip, path segment, meta capsule) stays on one line. scripts/ui_compare/capture.py crawls
the same way for screenshots.
"""

import re
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

import pytest
from helpers import sign_in
from playwright.sync_api import Page

pytestmark = pytest.mark.django_db(transaction=True)

ROLES = ("admin", "manager", "technician", "viewer")
WIDTHS = (1440, 1280)
NARROW = 768
THEMES = ("light", "dark")
MAX_PAGES = 250
SKIP = re.compile(
    r"^/(admin|static|media|accounts/logout|__debug__)|/(export|download)|"
    r"\.(csv|pdf|png|jpe?g|zip|json|txt)$",
    re.I,
)
ID = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d+)$", re.I)
LINKS = """() => [...document.querySelectorAll('a[href]:not([download])')].map(a => [
  a.getAttribute('href'), !!a.closest('#vr-nav')])"""
OVERFLOW = """() => {
  const doc = document.documentElement;
  if (doc.scrollWidth <= doc.clientWidth + 1) return [];
  // Name the widest offenders so the report says what to fix.
  return [...document.querySelectorAll('main *')]
    .filter(el => el.getBoundingClientRect().right > doc.clientWidth + 1)
    .filter(el => !el.closest('.vr-table-wrap, .dt-scroll, [data-vr-scroll]'))
    .slice(0, 3)
    .map(el => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')}`);
}"""

# Capsules (statuses, counts, chips, path segments, meta and panel-count capsules) keep to one
# line: their shared classes, plus anything else drawn as a pill (pill radius), except buttons and
# form controls. A capsule spanning more than one line of text is reported with its text.
CAPSULES = """() => {
  // Plain statuses are text with a glyph, not capsules; journey counts stack a number on a label.
  const shared = '.vr-status:not(.vr-status-plain), .vr-count, .vr-chip, .vr-path-seg, ' +
    '.vr-navlink-count, .vr-topbar-count, .vr-help-count, .vr-journey-tag, .badge';
  const controls = 'button, .btn, input, select, textarea, .form-control, .form-select, ' +
    '.progress, .ts-control, .ts-wrapper, .nav-link, .page-link, [role=search]';
  const pill = el => parseFloat(getComputedStyle(el).borderTopLeftRadius) >= 500;
  const lines = el => {
    const tops = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (!n.textContent.trim() || n.parentElement.closest('.visually-hidden')) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      for (const r of range.getClientRects()) {
        if (r.width && r.height && !tops.some(t => Math.abs(t - r.top) < r.height / 2)) {
          tops.push(r.top);
        }
      }
    }
    return tops.length;
  };
  const found = [];
  for (const el of document.querySelectorAll('body *')) {
    if (!el.getClientRects().length || el.closest('[hidden], .visually-hidden, .d-none')) continue;
    const plain = el.matches('.vr-status-plain, .vr-journey-count');
    if (!(el.matches(shared) || (pill(el) && !plain && !el.matches(controls)))) continue;
    if (el.parentElement && el.parentElement.closest(shared)) continue;  // its capsule counts
    const n = lines(el);
    if (n > 1) {
      const text = el.innerText.trim().replace(/\\s+/g, ' ').slice(0, 50);
      const name = `${el.tagName.toLowerCase()}.${[...el.classList].join('.')}`;
      found.push(`${name} "${text}" (${n} lines)`);
    }
  }
  return [...new Set(found)].slice(0, 5);
}"""


def shape(path: str) -> str:
    parts = urlsplit(path)
    route = "/".join("<id>" if ID.match(p) else p for p in parts.path.split("/"))
    return route + (f"?{parts.query}" if parts.query else "")


def local(href: str, base: str, current: str) -> str | None:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    url = urldefrag(urljoin(current, href))[0]
    parts = urlsplit(url)
    if f"{parts.scheme}://{parts.netloc}" != base or SKIP.search(parts.path):
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


def set_theme(page: Page, theme: str) -> None:
    page.evaluate(
        "t => { document.documentElement.setAttribute('data-bs-theme', t); }",
        theme,
    )


@pytest.mark.parametrize("role", ROLES)
def test_every_reachable_page_loads_clean_and_fits(page: Page, demo, live_server, role):
    sign_in(page, f"demo_{role}", demo.password)
    base = live_server.url
    queue, seen, problems, visited = [("/", 0)], set(), [], 0
    while queue and visited < MAX_PAGES:
        path, depth = queue.pop(0)
        if shape(path) in seen:
            continue
        seen.add(shape(path))
        page.set_viewport_size({"width": WIDTHS[0], "height": 900})
        errors_before = len(page.console_errors)
        response = page.goto(path)
        if response is None or "text/html" not in response.headers.get("content-type", ""):
            continue
        visited += 1
        if response.status >= 400:
            problems.append(f"{path}: HTTP {response.status} from a link shown to {role}")
            continue
        if urlsplit(page.url).path != urlsplit(path).path:
            continue  # redirected: the target is crawled on its own
        page.wait_for_load_state("networkidle")
        errors = page.console_errors[errors_before:]
        problems += [f"{path}: browser error: {e}" for e in errors]
        widths = WIDTHS + ((NARROW,) if depth <= 1 else ())
        for theme in THEMES:
            set_theme(page, theme)
            for width in widths:
                page.set_viewport_size({"width": width, "height": 900})
                if offenders := page.evaluate(OVERFLOW):
                    problems.append(
                        f"{path}: horizontal overflow at {width} {theme}: {', '.join(offenders)}"
                    )
                if width in WIDTHS and (wrapped := page.evaluate(CAPSULES)):
                    problems.append(
                        f"{path}: capsule on more than one line at {width} {theme}: "
                        + "; ".join(wrapped)
                    )
        for href, in_nav in page.evaluate(LINKS):
            target = local(href, base, page.url)
            if not target:
                continue
            if urlsplit(target).query and not (in_nav or depth == 0):
                continue  # pagination and sort variants of a list are the same page
            queue.append((target, 1 if in_nav else depth + 1))
    assert visited > 20, f"the crawl reached only {visited} pages as {role}"
    assert not problems, f"{len(problems)} problems as {role}:\n" + "\n".join(problems)


# --- Lists fit at desktop widths ------------------------------------------------------------------
# Owner rule: at 1280 and 1440 a list's table fits its card with no horizontal scroll, and no row
# takes more than two lines. Column priorities (`priority="low"`) and wording get lists there.

TABLE_FIT = (Path(__file__).parent / "table_fit.js").read_text()

# Shrink-only: today's known offenders, as "path @width: grid". A new offender fails the test, and
# so does a listed one that now fits, until it is removed here. Platform's column-priority work
# empties this set; the owner rule is that it ends empty.
KNOWN_UNFIT: set[str] = set()


def test_lists_fit_at_desktop_widths(page: Page, demo, live_server):
    sign_in(page, "demo_admin", demo.password)
    page.goto("/")
    lists = sorted(
        {
            local(href, live_server.url, page.url)
            for href, in_nav in page.evaluate(LINKS)
            if in_nav and local(href, live_server.url, page.url)
        }
        - {None}
    )
    assert len(lists) > 15, lists
    found, details, measured = set(), [], 0
    for path in lists:
        page.set_viewport_size({"width": WIDTHS[0], "height": 900})
        page.goto(path)
        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(100)
            for table in page.evaluate(TABLE_FIT):
                measured += 1
                if table["scroll"] > 1 or table["lines"] > 2:
                    key = f"{path} @{width}: {table['grid']}"
                    found.add(key)
                    details.append(
                        f"{key} (scrolls {table['scroll']}px, tallest row {table['lines']} "
                        f'lines at "{table["cell"]}")'
                    )
    assert measured > 30, f"only {measured} tables measured"
    new, fixed = found - KNOWN_UNFIT, KNOWN_UNFIT - found
    assert not new and not fixed, (
        "Lists that do not fit (new):\n"
        + "\n".join(d for d in details if d.split(" (")[0] in new)
        + "\nFixed, remove from KNOWN_UNFIT:\n"
        + "\n".join(sorted(fixed))
    )
