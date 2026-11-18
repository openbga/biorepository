"""Form controls never show the browser's default UI (docs/ui-standards.md section 8).

Each control is one widget template in templates/django/forms/widgets/ plus static/js/controls.js.
A page gets it through a Django form field, never by writing the native element itself.
"""

import importlib
import inspect
import re
from pathlib import Path

from django import forms
from django.apps import apps
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
WIDGETS = ROOT / "templates/django/forms/widgets"
SKIP_DIRS = {".venv", "node_modules", "staticfiles", ".local", ".git", "vendor"}

NATIVE = re.compile(
    r"<select\b|<datalist\b"
    r"|<input\b[^>]*\btype=[\"']?(?:date|datetime-local|time|month|week|number|file)\b",
    re.IGNORECASE,
)


def project_templates():
    for path in ROOT.rglob("*.html"):
        if SKIP_DIRS.intersection(path.relative_to(ROOT).parts) or path.is_relative_to(WIDGETS):
            continue
        yield path


def test_no_native_control_outside_its_widget():
    """A raw <select>, <datalist> or date/number/file <input> in a page template bypasses the
    shared component: render a form field instead ({{ form.x }})."""
    problems = []
    for path in project_templates():
        text = path.read_text()
        for match in NATIVE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{path.relative_to(ROOT)}:{line} {match.group()}")
    assert not problems, "Native controls outside their widget:\n" + "\n".join(problems)


class Controls(forms.Form):
    count = forms.IntegerField(min_value=1)
    signed = forms.IntegerField()
    amount = forms.DecimalField(min_value=0, decimal_places=2)
    quantity = forms.DecimalField(
        widget=forms.NumberInput(attrs={"inputmode": "decimal", "step": "any"})
    )
    day = forms.DateField(widget=forms.DateInput(attrs={"type": "date", "max": "2099-12-31"}))
    moment = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
    )
    document = forms.FileField(widget=forms.FileInput(attrs={"accept": ".pdf,application/pdf"}))
    optional_document = forms.FileField(required=False)
    temperature = forms.DecimalField(
        help_text="As read.", widget=forms.NumberInput(attrs={"unit": "°C"})
    )


def test_number_is_text_with_exact_string_and_safe_keypad():
    form = Controls(initial={"amount": "0.0100"})
    amount = str(form["amount"])
    assert 'type="text"' in amount and "vr-number" in amount
    assert 'value="0.0100"' in amount  # the typed string, not a float
    assert 'inputmode="decimal"' in amount
    assert not re.search(r"\b(min|max|step)=", amount)  # the server validates
    assert 'inputmode="numeric"' in str(form["count"])
    # No lower bound: negatives are possible, and touch keypads for numeric modes lack a minus.
    assert "inputmode" not in str(form["signed"])
    assert str(form["quantity"]).count("inputmode") == 1  # an explicit inputmode wins


def test_number_unit_is_one_input_group_read_with_the_field():
    temperature = " ".join(str(Controls({"temperature": "-80.50"})["temperature"]).split())
    assert temperature.startswith('<div class="input-group vr-unit-group"><input type="text"')
    assert 'value="-80.50"' in temperature  # the exact typed string
    assert 'aria-describedby="id_temperature_helptext id_temperature_unit"' in temperature
    assert '<span class="input-group-text" id="id_temperature_unit">°C</span></div>' in temperature
    assert ' unit="' not in temperature and "inputmode" not in temperature  # minus sign allowed


def test_dates_keep_their_native_input_and_server_format():
    form = Controls(initial={"moment": "2026-11-13T09:30"})
    day = str(form["day"])
    moment = str(form["moment"])
    assert 'type="date"' in day and 'data-vr-date="date"' in day and 'max="2099-12-31"' in day
    assert 'type="datetime-local"' in moment and 'data-vr-date="datetime"' in moment
    assert 'value="2026-11-13T09:30"' in moment
    assert f'data-vr-time-zone="{settings.TIME_ZONE}"' in moment


def test_file_inputs_render_the_drop_zone():
    form = Controls()
    document = str(form["document"])
    assert "data-vr-file" in document and 'class="vr-drop" for="id_document"' in document
    assert 'accept=".pdf,application/pdf"' in document and "data-vr-file-name" in document
    assert "data-vr-file" in str(form["optional_document"])  # ClearableFileInput too


def test_controls_script_and_calendar_are_vendored():
    vendor = ROOT / "static/vendor/vanilla-calendar-pro-3.4.0"
    assert (vendor / "index.js").exists() and (vendor / "LICENSE").exists()
    assert "Vanilla Calendar Pro 3.4.0" in (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    script = (ROOT / "static/js/controls.js").read_text()
    # Inline mode only: the library's input popup appends to <body> and breaks modal focus.
    assert "inputMode" not in script
    assert "firstWeekday: 0" in script


def project_forms():
    """Every form class declared in this project's apps (their forms/lists modules)."""
    for config in apps.get_app_configs():
        path = Path(config.path)
        if ROOT not in path.parents or ".venv" in path.parts:
            continue
        for module_path in sorted(path.glob("*.py")):
            if not any(word in module_path.stem for word in ("form", "list")):
                continue
            module = importlib.import_module(f"{config.name}.{module_path.stem}")
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if issubclass(cls, forms.BaseForm) and cls.__module__ == module.__name__:
                    yield cls


def test_every_declared_date_field_uses_the_date_control():
    """Date and date-time fields render the one date control (type date / datetime-local)."""
    problems = []
    checked = 0
    for cls in project_forms():
        for name, field in cls.base_fields.items():
            if isinstance(field, forms.DateTimeField):
                expected = "datetime-local"
            elif isinstance(field, forms.DateField):
                expected = "date"
            else:
                continue
            checked += 1
            if field.widget.input_type != expected:
                problems.append(f"{cls.__module__}.{cls.__name__}.{name}")
    assert checked > 10, "the scan found no date fields"
    assert not problems, f"Use a type={expected!r} widget (DATE / DATETIME): {problems}"
