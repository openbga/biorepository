"""The English catalog (locale/en) puts django-allauth's Title Case wording in sentence case.

It only overrides allauth strings, so each msgid must still exist in the installed allauth catalog
(an upgrade that renames one would silently bring the Title Case back), and the compiled .mo must
match the .po it is built from (uv run python manage.py compilemessages -l en).
"""

import gettext
import re
from pathlib import Path

import allauth
from django.conf import settings
from django.utils import translation

CATALOG = Path(settings.BASE_DIR) / "locale" / "en" / "LC_MESSAGES"
ENTRY = re.compile(r'^msgid "(.+)"\nmsgstr "(.+)"$', re.M)


def entries(path):
    return dict(ENTRY.findall(path.read_text()))


def test_catalog_is_on_the_locale_path():
    assert CATALOG.parent.parent in [Path(p) for p in settings.LOCALE_PATHS]


def test_compiled_catalog_matches_the_source():
    source = entries(CATALOG / "django.po")
    assert source
    with (CATALOG / "django.mo").open("rb") as handle:
        compiled = gettext.GNUTranslations(handle)
    stale = {k: v for k, v in source.items() if compiled.gettext(k) != v}
    assert not stale, f"recompile locale/en: {stale}"


def test_every_override_is_an_allauth_string():
    allauth_po = Path(allauth.__file__).parent / "locale" / "en" / "LC_MESSAGES" / "django.po"
    known = set(re.findall(r'^msgid "(.+)"$', allauth_po.read_text(), re.M))
    unknown = sorted(set(entries(CATALOG / "django.po")) - known)
    assert not unknown, f"no longer in allauth's catalog: {unknown}"


def test_the_site_language_uses_the_catalog():
    assert settings.LANGUAGE_CODE == "en-us"
    with translation.override(settings.LANGUAGE_CODE):
        assert translation.gettext("Make Primary") == "Make primary"
        assert translation.gettext("Two-Factor Authentication") == "Two-factor authentication"
