"""Template tag and filter names are unique across the project's libraries.

A template that loads two libraries registering the same name silently gets the last one loaded
(a dispatched shipment page once failed that way). Names must not repeat across the project's own
libraries, nor repeat a Django built-in, which every template has. Third-party libraries are not
compared with each other: they cannot be renamed here.
"""

from collections import defaultdict
from importlib import import_module
from pathlib import Path

from django.conf import settings
from django.template.backends.django import get_installed_libraries
from django.template.engine import Engine

ROOT = Path(settings.BASE_DIR).resolve()


def _project_library(module):
    path = Path(module.__file__).resolve()
    return ROOT in path.parents and ".venv" not in path.relative_to(ROOT).parts


def test_project_template_tags_and_filters_have_unique_names():
    owners = defaultdict(list)
    for label, module_path in sorted(get_installed_libraries().items()):
        module = import_module(module_path)
        if not _project_library(module):
            continue
        for kind in ("tags", "filters"):
            for name in getattr(module.register, kind):
                owners[(kind, name)].append(label)
    for library in Engine.get_default().template_builtins:
        for kind in ("tags", "filters"):
            for name in getattr(library, kind):
                if (kind, name) in owners:
                    owners[(kind, name)].append("Django built-in")
    clashes = {
        f"{kind[:-1]} {name!r}": labels
        for (kind, name), labels in sorted(owners.items())
        if len(labels) > 1
    }
    assert not clashes, f"Rename so each name has one library: {clashes}"
