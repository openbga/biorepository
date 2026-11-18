"""Every form is built from the form components (docs/ui-standards.md section 7).

A page or action form is {% form_page %} (its local=True variant for an action inside a record
panel), whose shell, error summary, required note and action row come from the shared includes.
A page template may write its own <form> only for a different, named pattern, declared by a
{# form: <pattern> #} comment on the line before it:

  entry-grid      an entry grid (section 2): many rows of values in one table
  bulk-selection  a table's row checkboxes and the bulk action they feed
  filter          a GET form that only chooses what the page shows
  menu            a dropdown menu's GET form (the label download)
  button-target   an empty form that buttons elsewhere submit through form="..."

Shared includes (templates/includes/), allauth elements and Django form widgets own their markup.
"""

import re
from pathlib import Path

from django.apps import apps
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
PATTERNS = {"entry-grid", "bulk-selection", "filter", "menu", "button-target"}
OWNERS = ("templates/includes/", "templates/allauth/", "templates/django/")
MARKER = re.compile(r"\{#\s*form:\s*([a-z-]+)\b[^#]*#\}\s*$")


def page_templates():
    dirs = [Path(d) for d in settings.TEMPLATES[0]["DIRS"]]
    dirs += [Path(app.path) / "templates" for app in apps.get_app_configs()]
    for directory in dirs:
        if not directory.is_dir() or ROOT not in directory.parents or ".venv" in directory.parts:
            continue
        for path in sorted(directory.rglob("*.html")):
            name = path.relative_to(ROOT).as_posix()
            if not name.startswith(OWNERS):
                yield name, path.read_text()


# Templates whose forms predate the components, with how many unnamed <form>s each still has
# (2026-11-18). The count only shrinks: delete an entry once its template is converted.
PENDING = {
    "specimens/templates/specimens/actions/derive.html": 1,
    "specimens/templates/specimens/actions/form.html": 1,
    "specimens/templates/specimens/catalogue_form.html": 1,
    "specimens/templates/specimens/import_upload.html": 1,
    "specimens/templates/specimens/specimen_list.html": 1,
    "storage/templates/storage/form.html": 1,
    "templates/core/announcement_form.html": 1,
    "templates/layouts/document.html": 1,
}


def unnamed_forms():
    problems = []
    for name, source in page_templates():
        source = re.sub(
            r"{% comment %}.*?{% endcomment %}",
            lambda m: "\n" * m[0].count("\n"),
            source,
            flags=re.S,
        )
        lines = source.split("\n")
        for number, line in enumerate(lines):
            if not re.search(r"<form\b", line):
                continue
            before = line[: line.index("<form")]
            marker = MARKER.search(before) or (number and MARKER.search(lines[number - 1]))
            if not marker or marker.group(1) not in PATTERNS:
                problems.append(f"{name}:{number + 1}")
    return problems


def test_page_forms_use_the_form_components_or_name_their_pattern():
    found = unnamed_forms()
    counts = {}
    for problem in found:
        name = problem.rsplit(":", 1)[0]
        counts[name] = counts.get(name, 0) + 1
    problems = [
        p for p in found if counts[p.rsplit(":", 1)[0]] > PENDING.get(p.rsplit(":", 1)[0], 0)
    ]
    stale = [
        f"{name}: {PENDING[name]} pending, {counts.get(name, 0)} found; lower PENDING"
        for name in sorted(PENDING)
        if counts.get(name, 0) < PENDING[name]
    ]
    assert not stale, "\n".join(stale)
    assert not problems, (
        "Build the form with {% form_page %} (local=True inside a record panel), or name its "
        "pattern on the line before with {# form: <pattern> #}, one of "
        + ", ".join(sorted(PATTERNS))
        + ":\n"
        + "\n".join(problems)
    )
