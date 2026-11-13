"""Governance forms: studies and collection links, consent records and withdrawal, agreements and
the four distribution catalogues. Transport and input shape only; distribution.services repeats
every rule under lock.

Free text that could carry participant identity (restrictions, where filed, received via, notes,
reasons) and every upload field say what must not be entered. Dates are dates; times are entered
in the repository's time zone to the second.
"""

from django import forms
from django.utils import timezone

from core.attachments import AttachmentField
from specimens.models import Collection, Participant

from .models import (
    NO_PARTICIPANT_TEXT,
    UPLOAD_TEXT,
    Agreement,
    AgreementDocument,
    ConsentAnswer,
    ConsentForm,
    ConsentRecord,
    ConsentScopeCategory,
    ConsentWithdrawal,
    Institution,
    Investigator,
    Study,
    StudyCollection,
    WithdrawalFollowUp,
)
from .services import TEXT_LIMIT

DATETIME = forms.DateTimeInput(
    attrs={"type": "datetime-local", "step": "1"}, format="%Y-%m-%dT%H:%M:%S"
)
DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
CODE_HELP = "Letters, digits, '.', '_' or '-'; stored in capitals."
FIXED = "Fixed: recorded data already uses this entry."

# Service error keys that differ from the form field showing them.
FIELD_ALIASES = {"participant_id": "participant", "study_id": "study", "form_id": "form"}


def apply_errors(form, error):
    """Show a service ValidationError on the form, under the field it names where it can."""
    if hasattr(error, "error_dict"):
        for name, errors in error.error_dict.items():
            target = FIELD_ALIASES.get(name, name)
            for item in errors:
                form.add_error(target if target in form.fields else None, item)
    else:
        for item in error.error_list:
            form.add_error(None, item)


def now_to_the_second():
    return timezone.localtime().replace(microsecond=0)


def text_field(label, *, required=False, max_length=TEXT_LIMIT, rows=3, help_text=""):
    return forms.CharField(
        label=label,
        required=required,
        max_length=max_length,
        widget=forms.Textarea(attrs={"rows": rows}),
        help_text=help_text,
    )


def reason_field(label="Reason", help_text="Kept in the history with the change."):
    return text_field(label, required=True, help_text=f"{help_text} {NO_PARTICIPANT_TEXT}")


def when_field(label, help_text="Repository local time; not in the future."):
    return forms.DateTimeField(label=label, widget=DATETIME, help_text=help_text)


def date_field(label, *, required=True, help_text=""):
    return forms.DateField(label=label, widget=DATE, required=required, help_text=help_text)


def revision_field():
    return forms.IntegerField(widget=forms.HiddenInput, required=False)


def code_field(label="Code", max_length=40):
    field = forms.CharField(label=label, max_length=max_length, help_text=CODE_HELP)
    field.widget.attrs.update({"autocapitalize": "characters", "spellcheck": "false"})
    return field


class CodedChoice(forms.ModelChoiceField):
    """`CODE · name` (or title) labels for catalogue and study choices."""

    def label_from_instance(self, obj):
        return f"{obj.code} · {getattr(obj, 'name', None) or getattr(obj, 'title', '')}"


class CodedMultiple(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.code} · {obj.name}"


class FormVersionChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.study.code} · {obj.code} v{obj.version} · {obj.title}"


class GroupedForm(forms.Form):
    """`groups` is ((legend, names, note), ...); a tuple in names renders side by side."""

    groups = ()

    def grouped(self):
        sections = []
        for group in self.groups:
            legend, names, note = (*group, "")[:3]
            rows = []
            for item in names:
                names_in_row = item if isinstance(item, tuple) else (item,)
                row = [self[n] for n in names_in_row if n in self.fields]
                row = [bound for bound in row if not bound.is_hidden]
                if row:
                    rows.append(row)
            if rows:
                sections.append((legend, rows, note))
        return sections

    @property
    def has_times(self):
        return any(isinstance(f, forms.DateTimeField) for f in self.fields.values())

    def disable(self):
        for field in self.fields.values():
            field.disabled = True

    def fix(self, names, why=FIXED):
        """Render fields read-only with the reason; their submitted values are ignored."""
        for name in names:
            if name in self.fields:
                self.fields[name].disabled = True
                self.fields[name].help_text = why


def values(form, names, skip=()):
    return {
        name: form.cleaned_data.get(name)
        for name in names
        if name in form.fields and name not in skip and not form.fields[name].disabled
    }


def _key(value):
    return str(getattr(value, "pk", value)) if value not in (None, "") else ""


# --- studies --------------------------------------------------------------------------------

STUDY_VALUES = (
    "code",
    "title",
    "description",
    "principal_investigator",
    "ethics_reference",
    "ethics_approved_until",
    "consent_basis",
    "determination_reference",
    "status",
)


class StudyForm(GroupedForm):
    code = code_field()
    title = forms.CharField(label="Title", max_length=160)
    description = text_field("Description", help_text=NO_PARTICIPANT_TEXT)
    principal_investigator = forms.CharField(
        label="Principal investigator",
        max_length=160,
        required=False,
        help_text="A professional name, as on the ethics approval.",
    )
    ethics_reference = forms.CharField(
        label="Ethics reference",
        max_length=80,
        required=False,
        help_text="The committee's reference for the study's approval.",
    )
    ethics_approved_until = date_field(
        "Ethics approved until",
        required=False,
        help_text="Shown on the study; nothing is blocked when it passes.",
    )
    consent_basis = forms.ChoiceField(
        label="Consent basis",
        choices=Study.Basis.choices,
        widget=forms.RadioSelect,
        help_text="Consent records: each participant's recorded consent governs their material. "
        "Study-level determination: an ethics committee's determination covers the study "
        "(for example a waiver); recorded refusals still block.",
    )
    determination_reference = forms.CharField(
        label="Determination reference",
        max_length=160,
        required=False,
        help_text="Required for a study-level determination: the committee's letter or reference.",
    )
    status = forms.ChoiceField(
        label="Status",
        choices=Study.Status.choices,
        help_text="A suspended or closed study's material cannot be allocated, picked, packed "
        "or dispatched.",
    )
    reason = reason_field("Reason for the change")
    expected_revision = revision_field()

    groups = (
        ("Study", [("code", "status"), "title", "description"]),
        (
            "Approval",
            ["principal_investigator", ("ethics_reference", "ethics_approved_until")],
        ),
        ("Consent basis", ["consent_basis", "determination_reference"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, instance=None, code_fixed=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        if instance is None:
            del self.fields["reason"]
            del self.fields["expected_revision"]
            self.initial.setdefault("consent_basis", Study.Basis.RECORDS)
            self.initial.setdefault("status", Study.Status.ACTIVE)
            return
        self.initial.setdefault("expected_revision", instance.revision)
        for name in STUDY_VALUES:
            self.initial.setdefault(name, getattr(instance, name))
        if code_fixed:
            self.fix(
                ["code"], "Fixed: the study already has consent forms, collections or consents."
            )


class CollectionChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.code} · {obj.name}"


class CollectionLinkForm(GroupedForm):
    collection = CollectionChoice(
        label="Collection",
        queryset=Collection.objects.none(),
        help_text="Active collections not governed by any study at present. A collection is "
        "governed by one study at a time.",
    )
    reason = reason_field("Reason", "Kept in the study's history.")
    expected_revision = revision_field()
    groups = (("", ["collection", "reason"]),)

    def __init__(self, *args, study, **kwargs):
        super().__init__(*args, **kwargs)
        current = StudyCollection.objects.filter(ended_at__isnull=True).values("collection_id")
        self.fields["collection"].queryset = (
            Collection.objects.filter(is_active=True).exclude(pk__in=current).order_by("code")
        )
        self.initial.setdefault("expected_revision", study.revision)


class ReasonForm(GroupedForm):
    """An action whose only input is the reason (end a link, hold withdrawn specimens)."""

    reason = reason_field("Reason", "Kept in the history.")
    expected_revision = revision_field()
    groups = (("", ["reason"]),)

    def __init__(self, *args, revision, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("expected_revision", revision)


# --- consent records ------------------------------------------------------------------------

RECORD_VALUES = (
    "decision",
    "consented_on",
    "signer",
    "documentation",
    "source_reference",
    "restrictions",
    "expires_on",
)
ANSWER_PREFIX = "answer_"


class ConsentRecordForm(GroupedForm):
    """Record a consent (new or re-consent) or correct the current record.

    The categories asked depend on the chosen form version. The form renders one answer per
    category of the version it was shown with (`asked`); when the version is changed the page
    comes back with the new version's categories to answer before anything is saved."""

    participant = forms.CharField(
        label="Participant",
        max_length=40,
        help_text="The coded participant identifier, e.g. P-000123. Never a name or record number.",
    )
    study = CodedChoice(label="Study", queryset=Study.objects.none())
    form = FormVersionChoice(
        label="Consent form version",
        queryset=ConsentForm.objects.none(),
        help_text="The form the participant decided on. Only active versions are offered.",
    )
    decision = forms.ChoiceField(
        label="Decision",
        choices=ConsentRecord.Decision.choices,
        widget=forms.RadioSelect,
        help_text="Consented: answer every category the form asks. Refused: the participant "
        "refused the whole form; no categories are answered.",
    )
    consented_on = date_field("Date of the decision", help_text="Not in the future.")
    signer = forms.ChoiceField(label="Who decided", choices=ConsentRecord.Signer.choices)
    documentation = forms.ChoiceField(
        label="How it was documented", choices=ConsentRecord.Documentation.choices
    )
    source_reference = forms.CharField(
        label="Where the original is filed",
        max_length=80,
        required=False,
        help_text=f"A binder, cabinet or system reference. {NO_PARTICIPANT_TEXT}",
    )
    restrictions = text_field(
        "Restrictions",
        max_length=2000,
        help_text="Restrictions in the participant's own terms, shown to reviewers, allocators "
        f"and pickers; never interpreted by the software. {NO_PARTICIPANT_TEXT}",
    )
    expires_on = date_field(
        "Expires on", required=False, help_text="Leave empty when the consent has no end date."
    )
    asked = forms.CharField(widget=forms.HiddenInput, required=False)
    consent = forms.CharField(widget=forms.HiddenInput, required=False)
    reason = reason_field("Reason", "Kept with the record, e.g. where it was transcribed from.")
    expected_revision = revision_field()

    def __init__(self, *args, original=None, study=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.original = original
        if original is not None:  # a correction: the consent is fixed
            del self.fields["participant"]
            del self.fields["study"]
            del self.fields["consent"]
            study = original.consent.study
            self.fields["reason"].label = "Reason for the correction"
            self.fields[
                "reason"
            ].help_text = (
                f"What was transcribed wrongly. Kept in the history. {NO_PARTICIPANT_TEXT}"
            )
            for name in ("form", *RECORD_VALUES):
                self.initial.setdefault(
                    name, getattr(original, "form_id" if name == "form" else name)
                )
            self.initial.setdefault("expected_revision", original.consent.revision)
        else:
            self.fields["study"].queryset = Study.objects.order_by("code")
        forms_offered = ConsentForm.objects.select_related("study").filter(is_active=True)
        chosen_study = self._value("study") or (study.pk if study else None)
        if chosen_study:
            forms_offered = forms_offered.filter(study_id=chosen_study)
        if original is not None:
            forms_offered = ConsentForm.objects.filter(
                pk__in=forms_offered.values("pk")
            ) | ConsentForm.objects.filter(pk=original.form_id)
        self.fields["form"].queryset = forms_offered.select_related("study").order_by(
            "study__code", "code", "version"
        )
        if not self._value("form") and chosen_study:
            only = list(self.fields["form"].queryset[:2])
            if len(only) == 1:
                self.initial.setdefault("form", only[0].pk)
        self.asked_form = self._chosen_form()
        self.categories = (
            list(self.asked_form.categories.order_by("code")) if self.asked_form else []
        )
        previous = {}
        if original is not None and self.asked_form and original.form_id == self.asked_form.pk:
            previous = {str(a.category_id): a.answer for a in original.answers.all()}
        for category in self.categories:
            name = f"{ANSWER_PREFIX}{category.pk}"
            self.fields[name] = forms.ChoiceField(
                label=f"{category.code} · {category.name}",
                choices=[("", "Not answered"), *ConsentAnswer.Answer.choices],
                required=False,
                help_text=category.description,
            )
            if str(category.pk) in previous:
                self.initial.setdefault(name, previous[str(category.pk)])
        asked = str(self.asked_form.pk) if self.asked_form else ""
        self.initial["asked"] = asked
        self.posted_asked = None
        if self.is_bound:
            # The hidden field re-renders with the version whose categories are now shown.
            self.posted_asked = self.data.get("asked") or ""
            self.data = self.data.copy()
            self.data["asked"] = asked

    def _value(self, name):
        if self.is_bound:
            return self.data.get(name) or None
        return _key(self.initial.get(name)) or None

    def _chosen_form(self):
        key = self._value("form")
        if not key:
            return None
        try:
            return self.fields["form"].queryset.filter(pk=key).first()
        except ValueError, forms.ValidationError:
            return None

    @property
    def groups(self):
        answers = [f"{ANSWER_PREFIX}{c.pk}" for c in self.categories]
        form_note = (
            ""
            if answers
            else "Choose the form version and save: its categories then appear to answer."
        )
        return (
            ("Participant and form", [("participant", "study"), "form"], form_note),
            (
                "Decision",
                ["decision", ("consented_on", "expires_on"), ("signer", "documentation")],
            ),
            (
                "Answers per category",
                answers,
                "Record each category the form asks as the participant answered it; leave them "
                "unanswered for a refusal. Categories the form does not ask are not addressed "
                "and do not permit a use.",
            ),
            ("Original and restrictions", ["source_reference", "restrictions"]),
            ("Reason", ["reason"]),
        )

    def clean_participant(self):
        code = (self.cleaned_data.get("participant") or "").strip().upper()
        participant = Participant.objects.filter(code=code).first()
        if participant is None:
            raise forms.ValidationError(
                "No participant has this code. Participants are added in the specimen catalogues."
            )
        return participant

    def clean(self):
        data = super().clean()
        chosen = data.get("form")
        if chosen is not None and _key(chosen) != self.posted_asked:
            raise forms.ValidationError(
                "The form version changed. Answer the categories it asks, then save again.",
                code="asked",
            )
        return data

    def answers(self):
        decision = self.cleaned_data.get("decision")
        if decision == ConsentRecord.Decision.REFUSED:
            return {}
        return {
            c.pk: self.cleaned_data.get(f"{ANSWER_PREFIX}{c.pk}") or "" for c in self.categories
        }

    def record_values(self):
        data = {name: self.cleaned_data.get(name) for name in RECORD_VALUES}
        data["form"] = self.cleaned_data.get("form")
        return data


class WithdrawForm(GroupedForm):
    kind = forms.ChoiceField(
        label="What was withdrawn",
        choices=ConsentWithdrawal.Kind.choices,
        widget=forms.RadioSelect,
        help_text="No further contact: the consent is flagged Do not contact; nothing else "
        "changes. No further use, and No further use and destroy: the consent becomes "
        "withdrawn, the participant's specimens in this study's collections get a use hold, and "
        "their allocations not yet picked are released. Nothing is disposed or deleted.",
    )
    effective_on = date_field("Effective on", help_text="Not in the future.")
    received_via = forms.CharField(
        label="Received via",
        max_length=80,
        help_text=f"How the withdrawal reached the repository, e.g. letter or phone call. "
        f"{NO_PARTICIPANT_TEXT}",
    )
    occurred_at = when_field("Received at")
    reason = reason_field("Reason", "Kept in the history and on the holds it places.")
    expected_revision = revision_field()
    groups = (
        ("Withdrawal", ["kind", ("effective_on", "occurred_at"), "received_via"]),
        ("Reason", ["reason"]),
    )

    def __init__(self, *args, consent, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("expected_revision", consent.revision)
        self.initial.setdefault("effective_on", timezone.localdate())
        self.initial.setdefault("occurred_at", now_to_the_second())
        if consent.status == consent.Status.WITHDRAWN:
            self.fields["kind"].choices = [
                c
                for c in ConsentWithdrawal.Kind.choices
                if c[0] == ConsentWithdrawal.Kind.NO_CONTACT
            ]
            self.fields[
                "kind"
            ].help_text = (
                "The consent is already withdrawn; only a withdrawal of contact can be added."
            )


class DisposalsForm(GroupedForm):
    method = forms.CharField(
        label="Disposal method",
        max_length=160,
        help_text="As your repository's disposal procedure names it, e.g. autoclave.",
    )
    destination = forms.CharField(
        label="Destination",
        max_length=160,
        help_text="Where the material goes, e.g. clinical waste.",
    )
    reason = reason_field("Reason", "Kept on each disposal request.")
    expected_revision = revision_field()
    groups = (("", [("method", "destination"), "reason"]),)

    def __init__(self, *args, consent, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("expected_revision", consent.revision)


class RecordChoice(forms.ModelChoiceField):
    def label_from_instance(self, record):
        when = timezone.localtime(record.recorded_at).strftime("%Y-%m-%d %H:%M")
        decided = f"{record.consented_on:%Y-%m-%d}"
        return f"{record.get_decision_display()} on {record.form} ({decided}), recorded {when}"


class ReinstateForm(GroupedForm):
    record = RecordChoice(
        label="New consent record",
        queryset=ConsentRecord.objects.none(),
        required=False,
        empty_label="None: the withdrawal was recorded in error",
        help_text="The consent recorded after the withdrawal, when the participant consented "
        'again. Its decision becomes the consent\'s status. "Recorded in error" is offered '
        "only while no consent was recorded after the withdrawal.",
    )
    occurred_at = when_field("Reinstated at")
    reason = reason_field("Reason", "Kept in the history and on the released holds.")
    expected_revision = revision_field()
    groups = (("", ["record", "occurred_at", "reason"]),)

    def __init__(self, *args, consent, withdrawal, **kwargs):
        super().__init__(*args, **kwargs)
        records = ConsentRecord.objects.none()
        if withdrawal is not None and consent.current_record_id:
            records = ConsentRecord.objects.filter(
                pk=consent.current_record_id, recorded_at__gte=withdrawal.recorded_at
            ).select_related("form")
        self.fields["record"].queryset = records
        first = records.first()
        if first is not None:
            # A record made after the withdrawal is named: "recorded in error" is not offered.
            self.initial.setdefault("record", first.pk)
            self.fields["record"].required = True
            self.fields["record"].empty_label = None
        self.initial.setdefault("occurred_at", now_to_the_second())
        self.initial.setdefault("expected_revision", consent.revision)


class ShipmentChoice(forms.ModelChoiceField):
    def label_from_instance(self, shipment):
        return f"{shipment.number} · {shipment.ship_to_institution}"


class FollowUpForm(GroupedForm):
    shipment = ShipmentChoice(
        label="Shipment",
        queryset=None,
        help_text="Shipments that contained this participant's material in the study.",
    )
    action = forms.ChoiceField(label="What was done", choices=WithdrawalFollowUp.Action.choices)
    note = text_field("Note", required=True, help_text=f"What happened. {NO_PARTICIPANT_TEXT}")
    occurred_at = when_field("When")
    attachment = AttachmentField(
        label="Attachment", help_text=f"PDF, PNG, JPEG, TXT or CSV, up to 10 MiB. {UPLOAD_TEXT}"
    )
    groups = (("", ["shipment", "action", "occurred_at", "note", "attachment"]),)

    def __init__(self, *args, shipments, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["shipment"].queryset = shipments
        self.initial.setdefault("occurred_at", now_to_the_second())


# --- agreements -----------------------------------------------------------------------------

AGREEMENT_VALUES = (
    "kind",
    "title",
    "institution",
    "investigator",
    "provider_reference",
    "recipient_reference",
    "data_level",
    "end_disposition",
    "onward_transfer",
    "permitted_use",
    "expires_on",
    "reporting_due_on",
    "document_reference",
)
EXECUTED_EDITABLE = (
    "title",
    "provider_reference",
    "recipient_reference",
    "expires_on",
    "reporting_due_on",
    "document_reference",
)


class InvestigatorChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.code} · {obj.name} ({obj.institution.code})"


class AgreementForm(GroupedForm):
    kind = forms.ChoiceField(label="Kind", choices=Agreement.Kind.choices)
    title = forms.CharField(label="Title", max_length=160)
    institution = CodedChoice(
        label="Institution",
        queryset=Institution.objects.none(),
        help_text="The recipient organization. Requests under this agreement must be from it.",
    )
    investigator = InvestigatorChoice(
        label="Investigator",
        queryset=Investigator.objects.none(),
        required=False,
        empty_label="Not named",
        help_text="Optional; must belong to the institution.",
    )
    provider_reference = forms.CharField(
        label="Provider reference",
        max_length=80,
        required=False,
        help_text="The repository's own reference for the agreement.",
    )
    recipient_reference = forms.CharField(
        label="Recipient reference",
        max_length=80,
        required=False,
        help_text="The recipient's reference for the agreement.",
    )
    data_level = forms.ChoiceField(
        label="Data level",
        choices=Agreement.DataLevel.choices,
        help_text="As the agreement states. Participant codes can go on a manifest only under "
        "Coded or Limited data set.",
    )
    end_disposition = forms.ChoiceField(
        label="At the end of use",
        choices=Agreement.EndDisposition.choices,
        help_text="What the agreement says happens to the material when use ends.",
    )
    onward_transfer = forms.ChoiceField(
        label="Onward transfer",
        choices=Agreement.OnwardTransfer.choices,
        help_text="Whether the recipient may pass the material on, as the agreement states.",
    )
    permitted_use = text_field(
        "Permitted use",
        help_text=f"The use the agreement permits, in its own words. {NO_PARTICIPANT_TEXT}",
    )
    permitted_categories = CodedMultiple(
        label="Permitted scope categories",
        queryset=ConsentScopeCategory.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Optional. When any are chosen, a request whose uses fall outside them cannot "
        "be dispatched under this agreement. Only categories that govern use are offered.",
    )
    expires_on = date_field("Expires on", required=False)
    reporting_due_on = date_field(
        "Reporting due on",
        required=False,
        help_text="A report the recipient owes; shown on the dashboard, never enforced.",
    )
    document_reference = forms.CharField(
        label="Where the signed agreement is filed",
        max_length=160,
        required=False,
        help_text=NO_PARTICIPANT_TEXT,
    )
    reason = reason_field("Reason for the change")
    expected_revision = revision_field()

    groups = (
        (
            "Agreement",
            [
                ("kind", "title"),
                ("institution", "investigator"),
                ("provider_reference", "recipient_reference"),
            ],
        ),
        (
            "Terms as recorded",
            [
                "data_level",
                ("end_disposition", "onward_transfer"),
                "permitted_use",
                "permitted_categories",
            ],
        ),
        ("Dates and filing", [("expires_on", "reporting_due_on"), "document_reference"]),
        ("Change", ["reason"]),
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        institutions = Institution.objects.filter(is_active=True)
        investigators = Investigator.objects.filter(is_active=True)
        categories = ConsentScopeCategory.objects.filter(is_active=True, gates_use=True)
        if instance is not None:
            institutions = institutions | Institution.objects.filter(pk=instance.institution_id)
            if instance.investigator_id:
                investigators = investigators | Investigator.objects.filter(
                    pk=instance.investigator_id
                )
            categories = categories | instance.permitted_categories.all()
        self.fields["institution"].queryset = institutions.distinct().order_by("code")
        self.fields["investigator"].queryset = (
            investigators.distinct().select_related("institution").order_by("code")
        )
        self.fields["permitted_categories"].queryset = categories.distinct().order_by("code")
        if instance is None:
            del self.fields["reason"]
            del self.fields["expected_revision"]
            self.initial.setdefault("data_level", Agreement.DataLevel.NOT_STATED)
            self.initial.setdefault("end_disposition", Agreement.EndDisposition.NOT_STATED)
            self.initial.setdefault("onward_transfer", Agreement.OnwardTransfer.NOT_STATED)
            return
        self.initial.setdefault("expected_revision", instance.revision)
        for name in AGREEMENT_VALUES:
            field = instance._meta.get_field(name)
            self.initial.setdefault(name, getattr(instance, field.attname))
        self.initial.setdefault(
            "permitted_categories", list(instance.permitted_categories.values_list("pk", flat=True))
        )
        if instance.status == Agreement.Status.EXECUTED:
            fixed = [
                n for n in (*AGREEMENT_VALUES, "permitted_categories") if n not in EXECUTED_EDITABLE
            ]
            self.fix(fixed, "Fixed: the agreement is executed; its terms no longer change.")

    def agreement_values(self):
        return values(self, AGREEMENT_VALUES)

    def categories(self):
        field = self.fields["permitted_categories"]
        if field.disabled:
            return None
        return tuple(c.pk for c in self.cleaned_data.get("permitted_categories") or ())


class ExecuteForm(GroupedForm):
    executed_on = date_field(
        "Signed on", help_text="When the last party signed; not in the future."
    )
    effective_on = date_field("Effective from")
    expires_on = date_field("Expires on", required=False)
    document_reference = forms.CharField(
        label="Where the signed agreement is filed",
        max_length=160,
        required=False,
        help_text="Required unless an executed copy is attached to the agreement. "
        f"{NO_PARTICIPANT_TEXT}",
    )
    reason = reason_field("Reason", "Kept in the agreement's timeline.")
    expected_revision = revision_field()
    groups = (
        ("", [("executed_on", "effective_on"), "expires_on", "document_reference", "reason"]),
    )

    def __init__(self, *args, agreement, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("expected_revision", agreement.revision)
        self.initial.setdefault("executed_on", timezone.localdate())
        self.initial.setdefault("effective_on", timezone.localdate())
        self.initial.setdefault("expires_on", agreement.expires_on)
        self.initial.setdefault("document_reference", agreement.document_reference)


class TerminateForm(GroupedForm):
    terminated_on = date_field(
        "Terminated on",
        help_text="Dispatch under the agreement is blocked from this date. Not before the "
        "effective date.",
    )
    reason = reason_field("Reason", "Kept in the agreement's timeline.")
    expected_revision = revision_field()
    groups = (("", ["terminated_on", "reason"]),)

    def __init__(self, *args, agreement, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("expected_revision", agreement.revision)
        self.initial.setdefault("terminated_on", timezone.localdate())


class DocumentChoice(forms.ModelChoiceField):
    def label_from_instance(self, document):
        return f"{document.title} ({document.get_kind_display()})"


class DocumentForm(GroupedForm):
    kind = forms.ChoiceField(label="Kind", choices=AgreementDocument.Kind.choices)
    title = forms.CharField(label="Title", max_length=160)
    note = text_field("Note", help_text=NO_PARTICIPANT_TEXT)
    supersedes = DocumentChoice(
        label="Supersedes",
        queryset=AgreementDocument.objects.none(),
        required=False,
        empty_label="Nothing",
        help_text="An earlier document of this agreement that this one replaces; both are kept.",
    )
    attachment = AttachmentField(
        label="File",
        required=True,
        help_text=f"PDF, PNG, JPEG, TXT or CSV, up to 10 MiB. {UPLOAD_TEXT}",
    )
    groups = (("", [("kind", "title"), "attachment", "supersedes", "note"]),)

    def __init__(self, *args, agreement, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supersedes"].queryset = agreement.documents.order_by("recorded_at")


# --- catalogues -----------------------------------------------------------------------------

CATALOGUE_VALUES = {
    "category": ("code", "name", "description", "gates_use", "is_active"),
    "consent_form": ("study", "code", "version", "title", "kind", "categories", "is_active"),
    "institution": ("code", "name", "address", "country", "is_active"),
    "investigator": ("code", "name", "institution", "email", "phone", "is_active"),
}
FIXED_WHEN_USED = {
    "category": ("code", "gates_use"),
    "consent_form": ("study", "code", "version", "categories"),
    "institution": ("code",),
    "investigator": ("code", "institution"),
}


class CatalogueForm(GroupedForm):
    def __init__(self, *args, kind, instance=None, in_use=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind, self.instance, self.in_use = kind, instance, in_use
        fields = {"code": code_field()}
        if kind == "category":
            fields["name"] = forms.CharField(label="Name", max_length=160)
            fields["description"] = text_field(
                "Description", help_text="What use the category covers, as the forms word it."
            )
            fields["gates_use"] = forms.BooleanField(
                label="Governs use",
                required=False,
                initial=True,
                help_text="A request can name it as a use that the consent must permit. Clear "
                "for categories that are recorded and shown only, such as re-contact.",
            )
        elif kind == "consent_form":
            fields["study"] = CodedChoice(label="Study", queryset=Study.objects.order_by("code"))
            fields["version"] = forms.CharField(label="Version", max_length=20)
            fields["title"] = forms.CharField(label="Title", max_length=160)
            fields["kind"] = forms.ChoiceField(label="Kind", choices=ConsentForm.Kind.choices)
            categories = ConsentScopeCategory.objects.filter(is_active=True)
            if instance is not None:
                categories = categories | instance.categories.all()
            fields["categories"] = CodedMultiple(
                label="Categories asked",
                queryset=categories.distinct().order_by("code"),
                widget=forms.CheckboxSelectMultiple,
                help_text="The uses this form version asks the participant about. Each consent "
                "recorded on it answers every one.",
            )
        elif kind == "institution":
            fields["name"] = forms.CharField(label="Name", max_length=160)
            fields["address"] = text_field(
                "Shipping address",
                max_length=500,
                help_text="The organization's address for shipments.",
            )
            fields["country"] = forms.CharField(
                label="Country", max_length=2, required=False, help_text="ISO 3166 two-letter code."
            )
        elif kind == "investigator":
            fields["name"] = forms.CharField(
                label="Name", max_length=160, help_text="A professional contact."
            )
            institutions = Institution.objects.filter(is_active=True)
            if instance is not None:
                institutions = institutions | Institution.objects.filter(pk=instance.institution_id)
            fields["institution"] = CodedChoice(
                label="Institution", queryset=institutions.distinct().order_by("code")
            )
            fields["email"] = forms.EmailField(
                label="Professional email", max_length=254, required=False
            )
            fields["phone"] = forms.CharField(
                label="Professional phone", max_length=40, required=False
            )
        if instance is not None:
            fields["is_active"] = forms.BooleanField(
                label="Active",
                required=False,
                help_text="An inactive entry stays on existing records but is not offered for "
                "new ones.",
            )
        fields["reason"] = text_field(
            "Reason for the change" if instance else "Reason",
            required=instance is not None,
            max_length=2000,
            help_text=f"Kept in the history with the change. {NO_PARTICIPANT_TEXT}"
            if instance
            else "Optional. Kept in the history; blank reads “Initial definition”.",
        )
        fields["expected_revision"] = revision_field()
        self.fields.update(fields)
        if instance is not None:
            for name in CATALOGUE_VALUES[kind]:
                if name == "categories":
                    value = list(instance.categories.values_list("pk", flat=True))
                else:
                    value = getattr(instance, instance._meta.get_field(name).attname)
                self.initial.setdefault(name, value)
            self.initial.setdefault("expected_revision", instance.revision)
        if in_use:
            self.fix(FIXED_WHEN_USED[kind])

    @property
    def groups(self):
        details = {
            "category": ["code", "name", "description", "gates_use"],
            "consent_form": ["study", ("code", "version"), "title", "kind", "categories"],
            "institution": [("code", "country"), "name", "address"],
            "investigator": [("code", "institution"), "name", ("email", "phone")],
        }[self.kind]
        return (("Details", [*details, "is_active"]), ("Reason", ["reason"]))

    def catalogue_values(self):
        result = values(self, CATALOGUE_VALUES[self.kind])
        if "categories" in result:
            result["categories"] = [c.pk for c in result["categories"] or ()]
        for name in ("study", "institution"):
            if name in result and result[name] is not None:
                result[name] = result[name].pk
        return result
