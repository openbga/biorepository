"""What's new: changelog.d/ entries, the generated CHANGELOG.md and the page with its unread
indicator data (core/changelog.py, core/views.whats_new, core/templatetags/whats_new.py). The
browser side of the indicator (static/js/whats-new.js) is covered by e2e/test_whats_new.py."""

import datetime
import re
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from accounts.models import User
from core import changelog
from core.changelog import ChangelogError, Entry, group_entries, parse_entry, render_markdown

GOOD = """---
date: 2026-11-16
type: added
impact: high
title: Prepare invoice drafts from received tests
area: Specimens
link: specimens:specimen-list
pr: 15
---
Project managers can price a received order.

Drafts stay internal.

## Technical notes

Adds a migration in commercial.
"""


def write(directory, name, text):
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def entry(day, title, impact="medium", type="added", id=None, **extra):
    date = datetime.date.fromisoformat(day)
    slug = id or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return Entry(
        id=f"{day}-{slug}",
        date=date,
        type=type,
        impact=impact,
        title=title,
        summary="Synthetic summary.",
        **extra,
    )


# ------------------------------------------------------------------ parsing


def test_entry_parses_front_matter_summary_and_technical_notes(tmp_path):
    parsed = parse_entry(write(tmp_path, "2026-11-16-invoice-drafts.md", GOOD))
    assert parsed.id == "2026-11-16-invoice-drafts"
    assert parsed.date == datetime.date(2026, 11, 16)
    assert (parsed.type, parsed.type_label, parsed.impact) == ("added", "Added", "high")
    assert (parsed.area, parsed.link, parsed.pr) == ("Specimens", "specimens:specimen-list", 15)
    assert parsed.summary == "Project managers can price a received order.\n\nDrafts stay internal."
    assert parsed.notes == "Adds a migration in commercial."


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("type: added", "type: improved"), "key 'type' must be one of"),
        (("impact: high", "impact: critical"), "key 'impact' must be one of"),
        (("date: 2026-11-16", "date: 2026-02-30"), "key 'date' must be a date"),
        (("date: 2026-11-16", "date: 16/11/2026"), "key 'date' must be a date"),
        (("date: 2026-11-16\n", ""), "missing required key 'date'"),
        (
            ("title: Prepare invoice drafts from received tests\n", ""),
            "missing required key 'title'",
        ),
        (("impact: high", "impact:"), "missing required key 'impact'"),
        (("pr: 15", "pr: #15"), "key 'pr' must be a pull request number"),
        (("area: Specimens", "owner: Specimens"), "unknown key 'owner'"),
        (("area: Specimens", "area: Specimens\narea: Boxes"), "key 'area' appears twice"),
        (("from received tests", "from received tests."), "without a trailing period"),
        (("link: specimens:specimen-list", "link: /specimens/"), "key 'link' must be a route name"),
        (
            ("Project managers can price a received order.\n\nDrafts stay internal.\n", ""),
            "summary",
        ),
    ],
)
def test_invalid_entries_are_rejected_naming_file_and_key(tmp_path, change, message):
    path = write(tmp_path, "2026-11-16-invoice-drafts.md", GOOD.replace(*change))
    with pytest.raises(ChangelogError) as error:
        parse_entry(path)
    assert str(error.value).startswith("2026-11-16-invoice-drafts.md: ")
    assert message in str(error.value)


@pytest.mark.parametrize("name", ["invoice-drafts.md", "2026-11-16_Invoice.md", "2026-11-16.md"])
def test_file_names_follow_the_date_slug_pattern(tmp_path, name):
    with pytest.raises(ChangelogError, match="YYYY-MM-DD-short-slug.md"):
        parse_entry(write(tmp_path, name, GOOD))


def test_front_matter_must_open_and_close(tmp_path):
    with pytest.raises(ChangelogError, match="must start with a '---'"):
        parse_entry(write(tmp_path, "2026-11-16-a.md", GOOD.removeprefix("---\n")))
    with pytest.raises(ChangelogError, match="no closing '---'"):
        parse_entry(write(tmp_path, "2026-11-16-a.md", GOOD.replace("\n---\nProject", "\nProject")))


# ------------------------------------------------------------------ ordering and the document


def test_groups_are_months_newest_first_with_impact_sections_and_date_ranges():
    entries = [
        entry("2026-10-03", "October fix", "low", "fixed"),
        entry("2026-11-02", "Early high", "high"),
        entry("2026-11-17", "Beta medium"),
        entry("2026-11-17", "Alpha medium"),
        entry("2026-11-09", "Later medium"),
        entry("2026-11-17", "Late high", "high"),
    ]
    groups = group_entries(entries)
    assert [(g.label, g.start, g.end, g.count) for g in groups] == [
        ("November 2026", datetime.date(2026, 11, 2), datetime.date(2026, 11, 17), 5),
        ("October 2026", datetime.date(2026, 10, 3), datetime.date(2026, 10, 3), 1),
    ]
    november = {impact: [e.title for e in items] for impact, _, items in groups[0].sections}
    assert list(november) == ["high", "medium"]  # empty "low" left out
    assert november["high"] == ["Late high", "Early high"]
    assert november["medium"] == ["Alpha medium", "Beta medium", "Later medium"]


def test_markdown_lists_groups_impacts_and_entries_in_order():
    entries = [
        entry("2026-11-16", "Invoice drafts", "high", pr=15, area="Billing", notes="Migration."),
        entry("2026-11-17", "Due dates", "medium", "changed"),
        entry("2026-10-31", "Spacing", "low", "fixed"),
    ]
    text = render_markdown(entries, repository="https://example.invalid/repo")
    assert text.startswith("<!-- Generated from changelog.d/")
    assert "Do not edit this file" in text.splitlines()[0]
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == [
        "# Changelog",
        "## November 2026 (2026-11-16 to 2026-11-17)",
        "### High impact",
        "### Medium impact",
        "## October 2026 (2026-10-31)",
        "### Low impact",
    ]
    assert (
        "- **Added:** Invoice drafts (2026-11-16, Billing, "
        "[#15](https://example.invalid/repo/pull/15))" in text
    )
    assert "  Technical notes:\n\n  > Migration." in text
    assert "- **Changed:** Due dates (2026-11-17)" in text


def test_check_mode_detects_stale_output_and_regenerates(tmp_path, monkeypatch, settings):
    entries_dir = tmp_path / "changelog.d"
    entries_dir.mkdir()
    write(entries_dir, "2026-11-16-invoice-drafts.md", GOOD)
    output = tmp_path / "CHANGELOG.md"
    monkeypatch.setattr(changelog, "ENTRIES_DIR", entries_dir)
    monkeypatch.setattr(changelog, "CHANGELOG", output)

    with pytest.raises(CommandError, match="out of date"):  # missing
        call_command("changelog", "--check", stdout=StringIO())
    call_command("changelog", stdout=StringIO())
    call_command("changelog", "--check", stdout=StringIO())

    write(entries_dir, "2026-11-17-due-dates.md", GOOD.replace("invoice drafts", "due dates"))
    with pytest.raises(CommandError, match="out of date"):  # a new entry
        call_command("changelog", "--check", stdout=StringIO())
    call_command("changelog", stdout=StringIO())
    output.write_text(output.read_text() + "\nA hand edit.\n")
    with pytest.raises(CommandError, match="out of date"):  # a hand edit
        call_command("changelog", "--check", stdout=StringIO())

    write(entries_dir, "2026-11-18-bad.md", GOOD.replace("type: added", "type: new"))
    with pytest.raises(CommandError, match=r"2026-11-18-bad.md: key 'type'"):
        call_command("changelog", "--check", stdout=StringIO())
    (entries_dir / "2026-11-18-bad.md").unlink()
    write(entries_dir, "2026-11-18-link.md", GOOD.replace("specimens:specimen-list", "nope:list"))
    with pytest.raises(CommandError, match=r"2026-11-18-link.md: key 'link' names no page"):
        call_command("changelog", "--check", stdout=StringIO())


def test_the_repository_entries_are_valid_and_the_committed_document_is_current():
    call_command("changelog", "--check", stdout=StringIO())


# ------------------------------------------------------------------ unread logic


def test_unread_counts_entries_newer_than_the_seen_marker():
    old = entry("2026-11-10", "Old")
    seen_day = entry("2026-11-16", "Seen that day")
    entries = [old, seen_day]
    assert changelog.unread(entries, None) == entries  # never opened: everything is new
    marker = changelog.seen_marker(entries)
    assert marker == {"date": "2026-11-16", "ids": [seen_day.id, old.id]}
    assert changelog.unread(entries, marker) == []

    newer = entry("2026-11-17", "Newer")
    same_day = entry("2026-11-16", "Merged later the same day")
    backdated = entry("2026-11-12", "Merged late with an earlier date")
    ancient = entry("2026-08-01", "Older than the window")
    later = [newer, same_day, backdated, ancient, *entries]
    assert changelog.unread(later, marker) == [newer, same_day, backdated]
    assert changelog.unread(later, changelog.seen_marker(later)) == []
    assert changelog.seen_marker([]) is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2026-11-16",
        {"date": "2026-11-16"},
        {"date": "yesterday", "ids": []},
        {"date": "2026-11-16", "ids": ["../etc"]},
        {"date": "2026-11-16", "ids": [], "extra": 1},
    ],
)
def test_a_malformed_seen_marker_is_ignored(value):
    assert not changelog.valid_marker(value)
    assert changelog.unread([entry("2026-11-16", "A")], value) == [entry("2026-11-16", "A")]


# ------------------------------------------------------------------ page and indicator data


def test_indicator_data_carries_the_recent_entries_and_the_seen_mark():
    items = [entry("2026-11-17", "Newest"), entry("2025-09-01", "Over a year before")]
    data = changelog.indicator_data(items)
    assert data == {
        "recent": [[items[0].id, "2026-11-17"]],
        "seen": changelog.seen_marker(items),  # stored by a browser without a mark: nothing new
    }
    assert changelog.indicator_data([]) == {"recent": [], "seen": None}


@pytest.fixture
def entries(monkeypatch):
    items = [
        entry(
            "2026-11-17", "Record due dates", "medium", "changed", link="specimens:specimen-list"
        ),
        entry(
            "2026-11-16",
            "Box audits",
            "high",
            pr=15,
            link="quality:audit-list",
            notes="Adds a migration in quality.",
        ),
        entry("2026-10-30", "Tidy spacing", "low", "fixed"),
    ]
    monkeypatch.setattr(changelog, "entries", lambda: tuple(items))
    return items


def embedded(html, element_id):
    import json

    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', html)
    return json.loads(match.group(1)) if match else None


@pytest.mark.django_db
def test_every_page_carries_the_link_and_the_indicator_data(client, staff, entries):
    client.force_login(staff("wn-tech", "technician"))
    html = client.get(reverse("dashboard")).content.decode()
    assert re.search(r'<a class="btn btn-sm vr-topbar-btn vr-whats-new" href="/whats-new/"', html)
    assert 'aria-label="What\'s new"' in html
    data = embedded(html, "vr-whats-new-data")
    assert data == changelog.indicator_data(entries)
    assert "js/whats-new" in html


@pytest.mark.django_db
def test_list_shows_groups_impacts_entries_links_and_the_seen_mark(client, staff, entries):
    client.force_login(staff("wn-manager", "manager"))
    html = client.get(reverse("whats-new")).content.decode()
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    # Each month: its title, the change count, then the date range.
    assert "November 2026 2 changes 2026-11-16 to 2026-11-17" in text
    assert "October 2026 1 change 2026-10-30" in text
    assert text.index("November 2026") < text.index("October 2026")
    november = text[text.index("November 2026") : text.index("October 2026")]
    assert november.index("High impact") < november.index("Medium impact")
    # Each change tile: its type, the (hidden until new) New mark, its date, then its title.
    tile = r"{} (New )?\d{{4}}-\d{{2}}-\d{{2}} {}"
    assert re.search(tile.format("Added", "Box audits"), november) and "Change #15" in november
    assert re.search(tile.format("Changed", "Record due dates"), november)
    assert re.search(tile.format("Fixed", "Tidy spacing"), text)
    assert "Technical notes" in html and "Adds a migration in quality." in html
    assert f'href="{reverse("quality:audit-list")}">Go to Inventory audits' in html
    assert embedded(html, "vr-whats-new-data")["seen"] == changelog.seen_marker(entries)
    assert html.count("data-vr-whats-new-mark hidden") == 3  # the script reveals unread ones
    assert 'aria-current="page"' in html


@pytest.mark.django_db
def test_empty_list_has_an_empty_state(client, staff, monkeypatch):
    monkeypatch.setattr(changelog, "entries", lambda: ())
    client.force_login(staff("wn-empty", "viewer"))
    html = client.get(reverse("whats-new")).content.decode()
    assert "No changes recorded yet" in html
    assert embedded(html, "vr-whats-new-data") == {"recent": [], "seen": None}


@pytest.mark.django_db
def test_any_signed_in_account_may_open_it_and_visitors_may_not(client, entries):
    no_role = User.objects.create_user("wn-none", "wn-none@example.invalid", "test-password-729!")
    client.force_login(no_role)
    assert client.get(reverse("whats-new")).status_code == 200
    client.logout()
    response = client.get(reverse("whats-new"))
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]
    sign_in = client.get(reverse("account_login")).content.decode()
    assert "vr-whats-new" not in sign_in


@pytest.mark.django_db
def test_go_to_links_only_offer_pages_the_account_may_open(client, staff, monkeypatch):
    items = [
        entry("2026-11-18", "Import register", link="specimens:import-list"),
        entry("2026-11-17", "Specimen filters", link="specimens:specimen-list"),
        entry("2026-11-16", "Announcement audiences", link="announcement-list"),
    ]
    monkeypatch.setattr(changelog, "entries", lambda: tuple(items))
    imports = f'href="{reverse("specimens:import-list")}">Go to Imports'
    specimens = f'href="{reverse("specimens:specimen-list")}">Go to Specimens'
    announcements = f'href="{reverse("announcement-list")}">Go to Announcements'

    def page(user):
        client.force_login(user)
        return client.get(reverse("whats-new")).content.decode()

    viewer = page(staff("wn-viewer", "viewer"))  # the menus' own visibility (navigation.visible)
    assert imports not in viewer and announcements not in viewer and specimens in viewer
    technician = page(staff("wn-tech-2", "technician"))
    assert imports in technician and specimens in technician and announcements not in technician
    assert announcements in page(staff("wn-admin", "admin"))
    no_role = User.objects.create_user("wn-none-2", "wn-none-2@example.invalid", "test-pass-729!")
    assert not re.search(r">Go to (Imports|Specimens|Announcements)", page(no_role))
