"""Every open menu and popover is fully visible: no ancestor clips it, nothing covers it.

A record header's More menu was once cut off by an ancestor with overflow: hidden. This sweep
signs in as a manager, visits one page of each route shape (records and lists), opens every
dropdown toggle (header More, row and section menus, the top navigation once) and, in an open
form dialog, a searchable select and a date calendar. For each open panel it checks that the
point inside each corner and the centre shows the panel itself (elementFromPoint), and that no
ancestor with clipping overflow cuts its box.
"""

import re

import pytest
from helpers import sign_in
from playwright.sync_api import Page
from test_sweeps import LINKS, local, shape

pytestmark = pytest.mark.django_db(transaction=True)

MAX_PAGES = 150
MAX_TOGGLES = 10  # per page, after the first of each repeated row menu
OPEN_PANEL = (
    ".dropdown-menu.show, .ts-wrapper.dropdown-active .ts-dropdown, "
    ".ts-dropdown:not([style*='display: none']), .vr-popover:not([hidden])"
)

# Why the panel is not fully visible, or "" when it is. The page scrolls (never a clipping
# ancestor: scrolling that would hide the fault) so the whole panel is inside the viewport.
VISIBLE = """panel => {
  const box = () => panel.getBoundingClientRect();
  let r = box();
  if (r.width < 1 || r.height < 1) return 'has no size';
  if (r.bottom > innerHeight) window.scrollBy(0, r.bottom - innerHeight + 8);
  if (box().top < 0) window.scrollBy(0, box().top - 8);
  r = box();
  const name = el => el ? `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}` +
    [...el.classList].slice(0, 3).map(c => '.' + c).join('') : 'nothing';
  for (let a = panel.parentElement; a && a !== document.documentElement; a = a.parentElement) {
    const s = getComputedStyle(a);
    if (!/hidden|clip|auto|scroll/.test(s.overflowX + ' ' + s.overflowY)) continue;
    const b = a.getBoundingClientRect();
    const out = r.left < b.left - 1 || r.right > b.right + 1;
    if (out || r.top < b.top - 1 || r.bottom > b.bottom + 1)
      return `is clipped by ${name(a)} (overflow ${s.overflowX}/${s.overflowY})`;
  }
  // Inside rounded corners: a point in the cut-off corner does not hit the panel.
  const ps = getComputedStyle(panel);
  const inset = 3 + Math.max(...['TopLeft', 'TopRight', 'BottomLeft', 'BottomRight']
    .map(c => parseFloat(ps[`border${c}Radius`]) || 0));
  const points = [[r.left + inset, r.top + inset], [r.right - inset, r.top + inset],
    [r.left + inset, r.bottom - inset], [r.right - inset, r.bottom - inset],
    [(r.left + r.right) / 2, (r.top + r.bottom) / 2]];
  for (const [x, y] of points) {
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight)
      return `extends outside the viewport at (${x | 0}, ${y | 0})`;
    const hit = document.elementFromPoint(x, y);
    if (!hit || !(hit === panel || panel.contains(hit)))
      return `is covered at (${x | 0}, ${y | 0}) by ${name(hit)}`;
  }
  return '';
}"""

TOGGLES = """() => {
  const seen = new Set();
  return [...document.querySelectorAll('[data-bs-toggle="dropdown"]')]
    .filter(t => t.getClientRects().length && !t.disabled && !t.closest('[inert]'))
    .map((t, index) => {
      // One of each repeated row menu: the same label in the same table counts once.
      const table = t.closest('table');
      const key = (table ? 'table' + [...document.querySelectorAll('table')].indexOf(table) : '') +
        '|' + (t.getAttribute('aria-label') || t.textContent).trim();
      if (seen.has(key)) return null;
      seen.add(key);
      return {index, label: (t.getAttribute('aria-label') || t.textContent).trim().slice(0, 40),
              nav: !!t.closest('header, .vr-topbar, #vr-nav')};
    })
    .filter(Boolean);
}"""


def check_panel(page: Page, where: str, what: str, problems: list) -> None:
    panel = page.locator(OPEN_PANEL).filter(visible=True).last
    if not panel.count():
        problems.append(f"{where}: {what} opened nothing visible")
        return
    reason = panel.evaluate(VISIBLE)
    if reason:
        problems.append(f"{where}: {what} menu {reason}")


def check_toggles(page: Page, path: str, include_nav: bool, problems: list) -> None:
    toggles = [t for t in page.evaluate(TOGGLES) if include_nav or not t["nav"]]
    for toggle in toggles[:MAX_TOGGLES]:
        page.goto(path)
        page.wait_for_load_state()
        control = page.locator('[data-bs-toggle="dropdown"]').nth(toggle["index"])
        if not control.is_visible():
            continue
        control.click(timeout=5000)
        page.wait_for_timeout(150)
        check_panel(page, path, f'"{toggle["label"]}"', problems)
        page.keyboard.press("Escape")


def check_dialog_controls(page: Page, path: str, opener: str, problems: list) -> None:
    page.goto(path)
    page.get_by_role("link", name=re.compile(f"^{opener}")).first.click()
    dialog = page.locator("#vr-form-dialog.show")
    dialog.wait_for()
    page.locator("#vr-form-content[aria-busy=false]").wait_for()
    where = f"{path} → {opener} dialog"
    for picker in dialog.locator(".ts-control").filter(visible=True).all()[:2]:
        picker.click()
        page.wait_for_timeout(250)
        check_panel(page, where, "searchable select", problems)
        page.keyboard.press("Escape")
    date = dialog.locator("input.vr-date-text").filter(visible=True).first
    if date.count():
        date.focus()
        page.keyboard.press("Alt+ArrowDown")
        page.wait_for_timeout(150)
        check_panel(page, where, "date calendar", problems)
        page.keyboard.press("Escape")


def test_open_menus_are_fully_visible(page: Page, demo, live_server):
    base = live_server.url
    sign_in(page, "demo_manager", demo.password)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    queue, seen, shapes, problems = ["/"], set(), set(), []
    first = True
    while queue and len(seen) < MAX_PAGES:
        path = queue.pop(0)
        if path in seen or shape(path) in shapes:
            continue
        seen.add(path)
        shapes.add(shape(path))
        response = page.goto(path)
        if not response or response.status >= 400:
            continue
        current = page.url
        for href, _ in page.evaluate(LINKS):
            target = local(href, base, current)
            if target and "?" not in target and shape(target) not in shapes:
                queue.append(target)
        check_toggles(page, path, first, problems)
        first = False
    check_dialog_controls(page, "/specimens/", "Register specimen", problems)
    check_dialog_controls(page, "/distribution/requests/", "New request", problems)
    assert not problems, f"{len(problems)} menus not fully visible:\n" + "\n".join(problems)
