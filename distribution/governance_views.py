"""Governance pages: studies and their collection links, consent records, withdrawals and their
follow-up, agreements with their documents, the four distribution catalogues, their CSV exports
and the attachment downloads of follow-ups and agreement documents.

Views handle transport: the role check at entry (every distribution page is readable by every
staff role; governance writes need admin or manager), filters through governance_lists, one call
to distribution.services per action and its errors shown on the form. Every rule is enforced
again by the service under lock; a control hidden or disabled here is a courtesy, not the gate.
The software records what people decided; it does not decide consent or interpret agreements.
"""

import uuid

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Prefetch
from django.http import Http404, HttpResponse, HttpResponseBadRequest, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from accounts.roles import has_role, staff_role_required
from core.attachments import download_response
from core.audit import record_event
from core.grids import GridRequest
from core.models import Attachment, AuditEvent
from specimens.models import Participant

from . import governance_lists as lists
from . import queries, services
from .exports import register_csv
from .governance_forms import (
    STUDY_VALUES,
    AgreementForm,
    CatalogueForm,
    CollectionLinkForm,
    ConsentRecordForm,
    DisposalsForm,
    DocumentForm,
    EscalateWithdrawalForm,
    ExecuteForm,
    FollowUpForm,
    ReasonForm,
    ReinstateForm,
    StudyForm,
    TerminateForm,
    WithdrawForm,
    apply_errors,
    values,
)
from .models import (
    Agreement,
    AgreementDocument,
    Consent,
    ConsentAnswer,
    ConsentScopeCategory,
    ConsentWithdrawal,
    Institution,
    Investigator,
    OutputRecord,
    Shipment,
    Study,
    StudyCollection,
    WithdrawalFollowUp,
)

READ = services.READ_ROLES
MANAGE = services.MANAGE_ROLES
ACTION_METHODS = require_http_methods(["GET", "POST"])
FORM_TEMPLATE = "distribution/governance/form.html"
EXPORT_CAP = 10000
PAGING = ("page", "per_page", "sort")
HISTORY_LIMIT = 50

# Downloads dispatched by distribution.views; the request pages own the list exports.
ATTACHMENT_KINDS = ("follow-up", "agreement-document")
EXPORT_KINDS = ()


def flags(user):
    """Which controls to render. Services and view decorators still enforce every action."""
    return {"can_manage": has_role(user, *MANAGE)}


def history(obj, limit=HISTORY_LIMIT):
    """Audit events of one record, newest first (the template shows them newest last)."""
    return list(
        AuditEvent.objects.filter(object_type=obj._meta.label_lower, object_id=str(obj.pk))
        .select_related("actor")
        .order_by("-occurred_at", "-id")[:limit]
    )


def person(user):
    return (user.get_full_name() or user.get_username()) if user else ""


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except TypeError, ValueError, AttributeError:
        return None


def consent_label(consent):
    return f"{consent.participant.code} · {consent.study.code}"


# --- lists ----------------------------------------------------------------------------------


def chips(request, form):
    """Removable filter chips from the validated filters (defaults are not active filters)."""
    result = []
    for name, value in form.cleaned_data.items():
        if name in {"sort", "page", "per_page", "q"} or value in (None, "", "all", False):
            continue
        field = form.fields[name]
        if value is True:
            display = "Yes"
        elif hasattr(field, "queryset"):
            display = field.label_from_instance(value)
        elif hasattr(field, "choices"):
            display = dict((str(k), v) for k, v in field.choices).get(str(value), value)
        else:
            display = value
        params = request.GET.copy()
        params.pop(name, None)
        params.pop("page", None)
        result.append(
            {"label": field.label, "value": str(display), "remove_url": "?" + params.urlencode()}
        )
    return result


def grid_response(request, spec, template, *, extra=None):
    try:
        grid = GridRequest.parse(request.GET, spec.columns, spec.default_sort)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    data = request.GET.copy()
    data["q"] = grid.query
    form, queryset = lists.filtered(spec, data, request.user)
    page_obj = Paginator(lists.ordered(spec, queryset, grid.sort), grid.per_page).get_page(
        grid.page_number
    )
    active = chips(request, form) if form.is_valid() else []
    cleaned = getattr(form, "cleaned_data", {})
    query = request.GET.copy()
    query.pop("page", None)
    export_url = reverse("distribution:catalogue-export", args=[spec.slug])
    return render(
        request,
        template,
        {
            "page_obj": page_obj,
            "filter_form": form,
            "placeholder": spec.form_class.placeholder,
            "q": grid.query,
            "sort": grid.sort,
            "active_filters": active,
            "filtered": bool(active or grid.query),
            "inline": [form[name] for name in spec.inline if name in form.fields],
            "more": [form[name] for name in spec.more if name in form.fields],
            "more_open": any(cleaned.get(name) for name in spec.more),
            "noun": spec.noun,
            "export_url": f"{export_url}?{query.urlencode()}" if query else export_url,
            "today": timezone.localdate(),
            **flags(request.user),
            **(extra or {}),
        },
    )


@staff_role_required(*READ)
def study_list(request):
    return grid_response(
        request,
        lists.SPECS["study"],
        "distribution/governance/study_list.html",
        extra={"page_title": "Studies"},
    )


@staff_role_required(*READ)
def consent_list(request):
    return grid_response(
        request,
        lists.SPECS["consent"],
        "distribution/governance/consent_list.html",
        extra={"page_title": "Consent"},
    )


@staff_role_required(*READ)
def withdrawal_list(request):
    return grid_response(
        request,
        lists.SPECS["withdrawal"],
        "distribution/governance/withdrawal_list.html",
        extra={"page_title": "Consent"},
    )


@staff_role_required(*READ)
def agreement_list(request):
    return grid_response(
        request,
        lists.SPECS["agreement"],
        "distribution/governance/agreement_list.html",
        extra={"page_title": "Agreements", "horizon_days": lists.HORIZON_DAYS},
    )


CATALOGUE_TABS = {
    # kind: (route prefix, tab label, New-button noun)
    "category": ("category", "Scope categories", "scope category"),
    "consent_form": ("consent-form", "Consent forms", "consent form"),
    "institution": ("institution", "Institutions", "institution"),
    "investigator": ("investigator", "Investigators", "investigator"),
}


def _catalogue_list(request, kind):
    prefix, tab, noun = CATALOGUE_TABS[kind]
    return grid_response(
        request,
        lists.SPECS[kind],
        "distribution/governance/catalogue_list.html",
        extra={
            "page_title": "Distribution catalogues",
            "kind": kind,
            "tab": tab,
            "create_noun": noun,
            "create_url": reverse(f"distribution:{prefix}-create"),
            "edit_route": f"distribution:{prefix}-edit",
        },
    )


@staff_role_required(*READ)
def category_list(request):
    return _catalogue_list(request, "category")


@staff_role_required(*READ)
def consent_form_list(request):
    return _catalogue_list(request, "consent_form")


@staff_role_required(*READ)
def institution_list(request):
    return _catalogue_list(request, "institution")


@staff_role_required(*READ)
def investigator_list(request):
    return _catalogue_list(request, "investigator")


# --- CSV and attachments --------------------------------------------------------------------


def export_rows(*, actor, slug, filters, sort=""):
    """CSV bytes of one governance list or catalogue, with the list's filters and sort; refused
    above EXPORT_CAP rows; audited with an OutputRecord (identifiers and counts, never bytes)."""
    services.require_read(actor)
    spec = lists.BY_SLUG.get(slug)
    if spec is None:
        raise ValidationError("Unknown list.", code="invalid")
    data = filters if isinstance(filters, QueryDict) else QueryDict(mutable=True)
    allowed = set(spec.form_class(user=actor).fields) | set(PAGING)
    unknown = sorted(key for key in data if key not in allowed)
    if unknown:
        raise ValidationError("Unknown filter: " + ", ".join(unknown), code="invalid")
    form, queryset = lists.filtered(spec, data, actor)
    if form.errors:
        raise ValidationError("These filters cannot be applied.", code="invalid")
    queryset = lists.ordered(spec, queryset, sort)
    content, total = register_csv(queryset, spec.export_columns, limit=EXPORT_CAP)
    selection = {k: data.get(k) for k in sorted(data) if data.get(k) and k != "page"}
    details = {"list": slug, "filters": selection, "sort": sort}
    with transaction.atomic():
        record = OutputRecord.objects.create(
            kind=OutputRecord.Kind.EXPORT, actor=actor, count=total, details=details
        )
        record_event(
            actor=actor,
            action="distribution.exported",
            obj=record,
            after={"count": total, **details},
            reason="Download",
        )
    return content


@staff_role_required(*READ)
@require_GET
def catalogue_export(request, kind):
    if kind not in lists.BY_SLUG:
        raise Http404
    params = request.GET.copy()
    sort = params.pop("sort", [""])[-1]
    try:
        content = export_rows(actor=request.user, slug=kind, filters=params, sort=sort)
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="distribution-{kind}-{stamp}.csv"'
    response["Cache-Control"] = "no-store"
    return response


ATTACHMENT_MODELS = {"follow-up": WithdrawalFollowUp, "agreement-document": AgreementDocument}


@staff_role_required(*READ)
@require_GET
def attachment(request, kind, pk):
    """A follow-up's or agreement document's file. Authorized through the referring record (every
    staff role reads consent and agreement pages, owner default); always a download."""
    model = ATTACHMENT_MODELS.get(kind)
    if model is None:
        raise Http404
    row = get_object_or_404(model.objects.only("pk", "attachment"), pk=pk)
    if row.attachment_id is None:
        raise Http404
    return download_response(Attachment.objects.with_data().get(pk=row.attachment_id))


# --- shared action handling -----------------------------------------------------------------


def _errors(exc):
    if hasattr(exc, "error_dict"):
        return [e for errors in exc.error_dict.values() for e in errors]
    return exc.error_list


def _stale(form, exc, revision):
    """After a stale-revision error, the next submit carries the current revision."""
    if "expected_revision" in form.fields and any(e.code == "stale_revision" for e in _errors(exc)):
        form.data = form.data.copy()
        form.data["expected_revision"] = revision()


def _action(
    request,
    form,
    *,
    title,
    submit,
    back_url,
    crumbs,
    run=None,
    success="",
    done_url=None,
    revision=None,
    blocked="",
    notes=(),
    facts=(),
    destructive=False,
    template=FORM_TEMPLATE,
    extra=None,
):
    """GET shows the form; POST validates, calls `run(cleaned_data)` and redirects on success."""
    if blocked:
        form.disable()
    elif request.method == "POST" and form.is_valid():
        try:
            result = run(form.cleaned_data)
        except ValidationError as exc:
            apply_errors(form, exc)
            if revision is not None:
                _stale(form, exc, revision)
        else:
            messages.success(request, success(result) if callable(success) else success)
            target = done_url(result) if callable(done_url) else done_url
            return redirect(target or back_url)
    status = 200
    if request.method == "POST":
        status = 409 if blocked else 422 if form.errors else 200
    return render(
        request,
        template,
        {
            "page_title": title,
            "form": form,
            "submit_label": submit,
            "back_url": back_url,
            "crumbs": crumbs,
            "blocked": blocked,
            "notes": notes,
            "facts": facts,
            "destructive": destructive,
            **(extra or {}),
        },
        status=status,
    )


def _post(request):
    return request.POST if request.method == "POST" else None


def _files(request):
    return request.FILES if request.method == "POST" else None


# --- studies --------------------------------------------------------------------------------


def study_crumbs(study=None, *rest):
    trail = [("Studies", reverse("distribution:study-list"))]
    if study is not None:
        trail.append((study.code, reverse("distribution:study-detail", args=[study.pk])))
    return [*trail, *rest]


def _study_url(study):
    return reverse("distribution:study-detail", args=[study.pk])


@staff_role_required(*READ)
def study_detail(request, pk):
    study = get_object_or_404(Study.objects.select_related("created_by"), pk=pk)
    links = list(
        study.collection_links.select_related("collection", "linked_by", "ended_by").order_by(
            "ended_at", "linked_at"
        )
    )
    current = [link for link in links if link.ended_at is None]
    ended = [link for link in links if link.ended_at is not None]
    forms_ = list(
        study.consent_forms.prefetch_related(
            Prefetch("categories", queryset=ConsentScopeCategory.objects.order_by("code"))
        ).order_by("code", "version")
    )
    counts = dict(
        study.consents.values_list("status").annotate(n=Count("pk")).values_list("status", "n")
    )
    consent_counts = [
        (value, label, counts.get(value, 0)) for value, label in Consent.Status.choices
    ]
    toc = [
        ("facts-heading", "Study"),
        ("collections-heading", f"Collections ({len(current)})"),
        ("forms-heading", f"Consent forms ({len(forms_)})"),
        ("consents-heading", "Consents"),
        ("history-heading", "History"),
    ]
    return render(
        request,
        "distribution/governance/study_detail.html",
        {
            "page_title": study.code,
            "study": study,
            "current_links": current,
            "ended_links": ended,
            "consent_forms": forms_,
            "consent_counts": consent_counts,
            "consent_total": sum(counts.values()),
            "toc": toc,
            "history": history(study),
            **flags(request.user),
        },
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def study_edit(request, pk=None):
    study = get_object_or_404(Study, pk=pk) if pk else None
    code_fixed = bool(
        study
        and (
            study.consents.exists()
            or study.collection_links.exists()
            or study.consent_forms.exists()
        )
    )
    form = StudyForm(_post(request), instance=study, code_fixed=code_fixed)

    def run(data):
        return services.save_study(
            actor=request.user,
            study_id=study.pk if study else None,
            expected_revision=data.get("expected_revision") if study else None,
            values=values(form, STUDY_VALUES),
            reason=data.get("reason", ""),
        )

    title = f"Edit {study.code}" if study else "New study"
    return _action(
        request,
        form,
        title=title,
        submit="Save changes" if study else "Create study",
        back_url=_study_url(study) if study else reverse("distribution:study-list"),
        crumbs=study_crumbs(study, (title, None)),
        run=run,
        success=lambda s: f"Saved {s.code}." if study else f"Created study {s.code}.",
        done_url=_study_url,
        revision=lambda: Study.objects.get(pk=study.pk).revision if study else None,
        notes=("The study records what its ethics approval says; the software does not judge it.",),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def study_collection_link(request, pk):
    study = get_object_or_404(Study, pk=pk)
    form = CollectionLinkForm(_post(request), study=study)
    return _action(
        request,
        form,
        title="Link collection",
        submit="Link collection",
        back_url=_study_url(study),
        crumbs=study_crumbs(study, ("Link collection", None)),
        run=lambda d: services.link_collection(
            actor=request.user,
            study_id=study.pk,
            expected_revision=d["expected_revision"],
            collection_id=d["collection"].pk,
            reason=d["reason"],
        ),
        success=lambda link: f"{link.collection.code} is now governed under {study.code}.",
        done_url=lambda link: _study_url(study),
        revision=lambda: Study.objects.get(pk=study.pk).revision,
        facts=(("Study", f"{study.code} · {study.title}"),),
        notes=(
            "Specimens of the collection are governed by this study's consent basis from now "
            "on, including those already stored.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def study_collection_end(request, pk):
    link = get_object_or_404(
        StudyCollection.objects.select_related("study", "collection", "linked_by"), pk=pk
    )
    study = link.study
    form = ReasonForm(_post(request), revision=study.revision)
    blocked = "" if link.ended_at is None else "This link has already ended."
    return _action(
        request,
        form,
        title="End link",
        submit="End link",
        back_url=_study_url(study),
        crumbs=study_crumbs(study, ("End link", None)),
        run=lambda d: services.end_collection_link(
            actor=request.user,
            link_id=link.pk,
            expected_revision=d["expected_revision"],
            reason=d["reason"],
        ),
        success=f"{link.collection.code} is no longer governed under {study.code}.",
        done_url=_study_url(study),
        revision=lambda: Study.objects.get(pk=study.pk).revision,
        blocked=blocked,
        destructive=True,
        facts=(
            ("Study", study.code),
            ("Collection", f"{link.collection.code} · {link.collection.name}"),
            (
                "Linked",
                f"{timezone.localtime(link.linked_at):%Y-%m-%d %H:%M} by {person(link.linked_by)}",
            ),
        ),
        notes=(
            "Its specimens are then not governed by any study, so they cannot be distributed "
            "until the collection is linked again. Refused while specimens of the collection "
            "are allocated or picked for a request.",
        ),
    )


# --- consent record -------------------------------------------------------------------------


def consent_crumbs(consent=None, *rest):
    trail = [("Consent", reverse("distribution:consent-list"))]
    if consent is not None:
        trail.append(
            (consent_label(consent), reverse("distribution:consent-detail", args=[consent.pk]))
        )
    return [*trail, *rest]


def _consent_url(consent):
    return reverse("distribution:consent-detail", args=[consent.pk])


def _consent(pk):
    return get_object_or_404(
        Consent.objects.select_related(
            "participant", "study", "current_record", "current_record__form"
        ),
        pk=pk,
    )


RECORD_LABELS = {
    "form": "Form",
    "decision": "Decision",
    "consented_on": "Date of the decision",
    "signer": "Who decided",
    "documentation": "How it was documented",
    "source_reference": "Where the original is filed",
    "restrictions": "Restrictions",
    "expires_on": "Expires",
}


def _record_display(record):
    """{field label: text} of one record and its answers, for corrections and the record card."""
    return {
        "form": str(record.form),
        "decision": record.get_decision_display(),
        "consented_on": record.consented_on.isoformat(),
        "signer": record.get_signer_display(),
        "documentation": record.get_documentation_display(),
        "source_reference": record.source_reference,
        "restrictions": record.restrictions,
        "expires_on": record.expires_on.isoformat() if record.expires_on else "",
    }


def _answers_text(record):
    return {a.category.code: a.get_answer_display() for a in record.answers.all()}


def _record_rows(consent):
    """Records oldest first, each with a title and, for corrections, (label, before, after)."""
    records = list(
        consent.records.select_related("form", "recorded_by", "corrects").prefetch_related(
            Prefetch("answers", queryset=ConsentAnswer.objects.select_related("category"))
        )
    )
    by_id = {r.pk: r for r in records}
    for index, record in enumerate(records):
        record.change_rows = []
        record.answer_text = ", ".join(
            f"{code} {answer.lower()}" for code, answer in _answers_text(record).items()
        )
        if record.corrects_id:
            record.title = "Record corrected"
            prior = by_id.get(record.corrects_id)
            if prior is not None:
                before, after = _record_display(prior), _record_display(record)
                record.change_rows = [
                    (RECORD_LABELS[k], before[k], after[k]) for k in after if before[k] != after[k]
                ]
                old, new = _answers_text(prior), _answers_text(record)
                for code in sorted(set(old) | set(new)):
                    if old.get(code) != new.get(code):
                        record.change_rows.append(
                            (code, old.get(code, "not answered"), new.get(code, "not answered"))
                        )
        elif index == 0:
            record.title = "Consent recorded"
        else:
            record.title = "New consent recorded"
    return records


def _answers_for(consent):
    """[(category, answer label or "Not addressed", tone)] for the current record's form
    categories and any category answered."""
    record = consent.current_record
    if record is None:
        return []
    given = {a.category_id: a for a in record.answers.select_related("category")}
    categories = list(record.form.categories.order_by("code"))
    known = {c.pk for c in categories}
    categories += [a.category for a in given.values() if a.category_id not in known]
    rows = []
    for category in categories:
        answer = given.get(category.pk)
        if answer is None:
            rows.append((category, "Not addressed", "neutral"))
        elif answer.answer == ConsentAnswer.Answer.PERMITTED:
            rows.append((category, "Permitted", "success"))
        else:
            rows.append((category, "Refused", "danger"))
    return rows


def _withdrawals(consent):
    rows = list(
        consent.withdrawals.select_related("actor", "reinstatement__actor", "escalation__actor")
        .prefetch_related(
            Prefetch(
                "follow_ups",
                queryset=WithdrawalFollowUp.objects.select_related(
                    "actor", "shipment", "attachment"
                ).defer("attachment__data"),
            )
        )
        .order_by("recorded_at", "id")
    )
    for row in rows:
        try:
            row.reinstated = row.reinstatement
        except ConsentWithdrawal.reinstatement.RelatedObjectDoesNotExist:
            row.reinstated = None
    return rows


def _consent_shipments(consent):
    """Shipments that contained this participant's material in the study (the follow-up
    choices; the service checks the same)."""
    return Shipment.objects.filter(
        items__specimen__participant_id=consent.participant_id,
        items__specimen__collection_id__in=StudyCollection.objects.filter(
            study_id=consent.study_id
        ).values("collection_id"),
    ).distinct()


def reinstate_refusal(user, consent, withdrawal):
    """Why this user cannot reinstate now, or "" (the service checks the same under lock)."""
    if consent.status != Consent.Status.WITHDRAWN or withdrawal is None:
        return "The consent is not withdrawn."
    if withdrawal.actor_id == user.pk:
        return "You recorded the withdrawal. Another manager must reinstate the consent."
    escalation = getattr(withdrawal, "escalation", None)
    if escalation is not None and escalation.actor_id == user.pk:
        return "You recorded the escalation. Another manager must reinstate the consent."
    record = services.record_after_withdrawal(consent, withdrawal)
    if record is not None and record.recorded_by_id == user.pk:
        return "You recorded the new consent. Another manager must reinstate it."
    if (
        escalation is not None
        and record is not None
        and record.recorded_at <= escalation.recorded_at
    ):
        return "Record consent after the escalation before reinstating."
    return ""


def _consent_actions(user, consent, withdrawal, impact):
    """Header actions: (destructive, secondaries, more, primary), each {label, url, reason}."""
    if not has_role(user, *MANAGE):
        return None, [], [], None
    url = lambda name: reverse(f"distribution:{name}", args=[consent.pk])  # noqa: E731
    record_url = (
        reverse("distribution:consent-create")
        + f"?participant={consent.participant.code}&study={consent.study_id}"
    )
    actions = {
        "record": {"label": "Record consent", "url": record_url, "reason": ""},
        "correct": {
            "label": "Correct consent",
            "url": url("consent-correct"),
            "reason": "" if consent.current_record_id else "There is no record to correct.",
        },
        "reinstate": {
            "label": "Reinstate consent",
            "url": url("consent-reinstate"),
            "reason": reinstate_refusal(user, consent, withdrawal),
        },
    }
    if withdrawal is not None and impact.unheld:
        actions["hold"] = {
            "label": "Hold withdrawn specimens",
            "url": url("consent-hold"),
            "reason": "",
        }
    if withdrawal is not None and withdrawal.kind == ConsentWithdrawal.Kind.NO_USE:
        actions["escalate"] = {
            "label": "Escalate to destruction",
            "url": url("consent-escalate"),
            "reason": services.escalation_refusal(consent, withdrawal),
        }
    if withdrawal is not None and withdrawal.effective_kind == ConsentWithdrawal.Kind.DESTROY:
        actions["disposals"] = {
            "label": "Request disposal of held specimens",
            "url": url("consent-disposals"),
            "reason": "" if impact.held else "No specimen in custody holds this withdrawal.",
        }
    destructive = {"label": "Withdraw consent", "url": url("consent-withdraw"), "reason": ""}
    # A withdrawn consent has a next step; a consented or refused one has none (re-consent and
    # corrections are secondary actions).
    primary = None
    if withdrawal is not None:
        primary = actions.pop("hold" if "hold" in actions else "reinstate")
    secondary = [actions[k] for k in ("correct", "record") if k in actions]
    more = [actions[k] for k in ("hold", "escalate", "disposals", "reinstate") if k in actions]
    return destructive, secondary, more, primary


@staff_role_required(*READ)
def consent_detail(request, pk):
    consent = _consent(pk)
    user = request.user
    impact = queries.consent_impact(actor=user, consent_id=consent.pk)
    withdrawal = services.open_withdrawal(consent)
    records = _record_rows(consent)
    withdrawals = _withdrawals(consent)
    follow_ups = {}
    for row in withdrawals:
        for follow_up in row.follow_ups.all():
            follow_ups.setdefault(follow_up.shipment_id, []).append(follow_up)
    shipments = {}
    for item in impact.distributed:
        shipments.setdefault(item.shipment_id, {"shipment": item.shipment, "items": []})
        shipments[item.shipment_id]["items"].append(item)
    distributed = [
        {**group, "follow_ups": follow_ups.get(shipment_id, [])}
        for shipment_id, group in shipments.items()
    ]
    destructive, secondary, more, primary = _consent_actions(user, consent, withdrawal, impact)
    toc = [
        ("facts-heading", "Consent"),
        ("answers-heading", "Answers per category"),
        ("impact-heading", f"Specimens in custody ({len(impact.held) + len(impact.unheld)})"),
        ("allocations-heading", f"Open allocations ({len(impact.open_allocations)})"),
        ("distributed-heading", f"Distributed material ({len(distributed)})"),
        ("records-heading", f"Records ({len(records)})"),
        ("withdrawals-heading", f"Withdrawals ({len(withdrawals)})"),
        ("history-heading", "History"),
    ]
    return render(
        request,
        "distribution/governance/consent_detail.html",
        {
            "page_title": consent_label(consent),
            "consent": consent,
            "current": consent.current_record,
            "answers": _answers_for(consent),
            "records": records,
            "withdrawals": withdrawals,
            "open_withdrawal": withdrawal,
            "impact": impact,
            "distributed": distributed,
            "destructive": destructive,
            "secondary": secondary,
            "more": more,
            "primary": primary,
            "toc": toc,
            "history": history(consent),
            **flags(user),
        },
    )


def _prefill(request):
    """Participant and study named by ?participant= (code or key) and ?study= (key or code)."""
    initial = {}
    value = (request.GET.get("participant") or "").strip()
    if value:
        key = _uuid(value)
        participant = (
            Participant.objects.filter(pk=key).first()
            if key
            else Participant.objects.filter(code__iexact=value).first()
        )
        if participant is not None:
            initial["participant"] = participant.code
    value = (request.GET.get("study") or "").strip()
    if value:
        key = _uuid(value)
        study = (
            Study.objects.filter(pk=key).first()
            if key
            else Study.objects.filter(code__iexact=value).first()
        )
        if study is not None:
            initial["study"] = study.pk
    return initial


@staff_role_required(*MANAGE)
@ACTION_METHODS
def consent_create(request):
    initial = _prefill(request)
    existing = None
    if "participant" in initial and "study" in initial:
        existing = Consent.objects.filter(
            participant__code=initial["participant"], study_id=initial["study"]
        ).first()
        if existing is not None:
            initial["consent"] = str(existing.pk)
            initial["expected_revision"] = existing.revision
    study = Study.objects.filter(pk=initial.get("study")).first() if initial.get("study") else None
    form = ConsentRecordForm(_post(request), initial=initial, study=study)

    def run(data):
        participant, chosen_study = data["participant"], data["study"]
        current = Consent.objects.filter(participant=participant, study=chosen_study).first()
        expected = None
        if current is not None:
            # The revision seen applies only to the consent the page was opened for.
            seen = data.get("consent") == str(current.pk)
            expected = data.get("expected_revision") if seen else current.revision
        return services.record_consent(
            actor=request.user,
            participant_id=participant.pk,
            study_id=chosen_study.pk,
            form_id=data["form"].pk,
            answers=form.answers(),
            expected_revision=expected,
            reason=data["reason"],
            **{k: v for k, v in form.record_values().items() if k != "form"},
        )

    back = _consent_url(existing) if existing else reverse("distribution:consent-list")
    trail = consent_crumbs(existing, ("Record consent", None))
    notes = [
        "Record what the filed consent says. The software stores the decision and its scope; "
        "it does not judge whether a consent is adequate.",
    ]
    if existing is not None and existing.status == Consent.Status.WITHDRAWN:
        notes.append(
            "This consent is withdrawn. The new record is kept, but the status stays withdrawn "
            "until another manager reinstates the consent."
        )
    return _action(
        request,
        form,
        title="Record consent",
        submit="Record consent",
        back_url=back,
        crumbs=trail,
        run=run,
        success=lambda r: (
            f"Consent recorded for {consent_label(r.consent)}: "
            f"{r.get_decision_display().lower()} on {r.form}."
        ),
        done_url=lambda r: _consent_url(r.consent),
        notes=notes,
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def consent_correct(request, pk):
    consent = _consent(pk)
    record = consent.current_record
    if record is None:
        raise Http404
    form = ConsentRecordForm(_post(request), original=record)

    def run(data):
        return services.correct_consent_record(
            actor=request.user,
            record_id=record.pk,
            expected_revision=data["expected_revision"],
            values=form.record_values(),
            answers=form.answers(),
            reason=data["reason"],
        )

    return _action(
        request,
        form,
        title="Correct consent",
        submit="Correct consent",
        back_url=_consent_url(consent),
        crumbs=consent_crumbs(consent, ("Correct consent", None)),
        run=run,
        success=f"Corrected the consent record of {consent_label(consent)}.",
        done_url=_consent_url(consent),
        revision=lambda: Consent.objects.get(pk=consent.pk).revision,
        facts=(
            ("Consent", consent_label(consent)),
            (
                "Current record",
                f"{record.get_decision_display()} on {record.form}, {record.consented_on:%Y-%m-%d}",
            ),
        ),
        notes=(
            "For a transcription error. The correction is added as a new version; the earlier "
            "one stays in the records history. A change of the participant's decision is a new "
            "consent record, not a correction.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def consent_withdraw(request, pk):
    consent = _consent(pk)
    form = WithdrawForm(_post(request), consent=consent)
    impact = queries.consent_impact(actor=request.user, consent_id=consent.pk)

    def run(data):
        return services.withdraw_consent(
            actor=request.user,
            consent_id=consent.pk,
            expected_revision=data["expected_revision"],
            kind=data["kind"],
            effective_on=data["effective_on"],
            received_via=data["received_via"],
            occurred_at=data["occurred_at"],
            reason=data["reason"],
        )

    def success(w):
        if not w.stops_use:
            return f"Recorded: do not contact {consent.participant.code} in {consent.study.code}."
        return (
            f"Consent withdrawn for {consent_label(consent)}: {w.held_count} specimen(s) held, "
            f"{w.released_count} allocation(s) released, {w.distributed_count} distributed."
        )

    return _action(
        request,
        form,
        title="Withdraw consent",
        submit="Withdraw consent",
        back_url=_consent_url(consent),
        crumbs=consent_crumbs(consent, ("Withdraw consent", None)),
        run=run,
        success=success,
        done_url=_consent_url(consent),
        revision=lambda: Consent.objects.get(pk=consent.pk).revision,
        destructive=True,
        facts=(
            ("Consent", consent_label(consent)),
            ("Status", consent.get_status_display()),
            ("Specimens in custody", str(len(impact.held) + len(impact.unheld))),
            ("Open allocations", str(len(impact.open_allocations))),
            ("Distributed", str(len(impact.distributed))),
        ),
        notes=(
            "Picked specimens stay picked: the use hold stops packing and dispatch, and staff "
            "put them back. Distributed material is listed on the consent page for recorded "
            "follow-up; the software sends nothing.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def consent_escalate(request, pk):
    consent = _consent(pk)
    withdrawal = services.open_withdrawal(consent)
    form = EscalateWithdrawalForm(_post(request), consent=consent)
    title = "Escalate to destruction"
    facts = [("Consent", consent_label(consent))]
    if withdrawal is not None:
        facts.extend(
            (
                ("Original withdrawal", withdrawal.get_kind_display()),
                ("Original effective date", f"{withdrawal.effective_on:%Y-%m-%d}"),
                ("Original receipt", lists.local(withdrawal.occurred_at)),
            )
        )
    return _action(
        request,
        form,
        title=title,
        submit=title,
        back_url=_consent_url(consent),
        crumbs=consent_crumbs(consent, (title, None)),
        run=lambda d: services.escalate_withdrawal(
            actor=request.user,
            consent_id=consent.pk,
            expected_revision=d["expected_revision"],
            effective_on=d["effective_on"],
            received_via=d["received_via"],
            occurred_at=d["occurred_at"],
            reason=d["reason"],
        ),
        success="Recorded the destruction instruction. Existing holds remain in effect.",
        done_url=_consent_url(consent),
        revision=lambda: Consent.objects.get(pk=consent.pk).revision,
        blocked=services.escalation_refusal(consent, withdrawal),
        facts=facts,
        notes=(
            "Adds the destruction instruction to the withdrawal history. The original withdrawal "
            "and its holds remain in effect. File disposal requests separately for independent "
            "approval. Record follow-up for distributed material separately.",
        ),
        destructive=True,
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def consent_hold(request, pk):
    consent = _consent(pk)
    form = ReasonForm(_post(request), revision=consent.revision)
    withdrawal = services.open_withdrawal(consent)
    impact = queries.consent_impact(actor=request.user, consent_id=consent.pk)
    blocked = ""
    if withdrawal is None:
        blocked = "This consent has no withdrawal that stops use."
    elif not impact.unheld:
        blocked = "Every specimen of this consent in custody already holds the withdrawal."
    return _action(
        request,
        form,
        title="Hold withdrawn specimens",
        submit="Hold withdrawn specimens",
        back_url=_consent_url(consent),
        crumbs=consent_crumbs(consent, ("Hold withdrawn specimens", None)),
        run=lambda d: services.hold_withdrawn_specimens(
            actor=request.user,
            consent_id=consent.pk,
            expected_revision=d["expected_revision"],
            reason=d["reason"],
        ),
        success=lambda rows: f"Placed {len(rows)} use hold(s) for {consent_label(consent)}.",
        done_url=_consent_url(consent),
        revision=lambda: Consent.objects.get(pk=consent.pk).revision,
        blocked=blocked,
        facts=(
            ("Consent", consent_label(consent)),
            ("Without a hold", ", ".join(s.accession for s in impact.unheld) or "None"),
        ),
        notes=(
            "Places a use hold on each specimen of the participant in the study's collections "
            "that has none from the withdrawal (for example registered after it), and releases "
            "their allocations not yet picked.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def consent_disposals(request, pk):
    consent = _consent(pk)
    form = DisposalsForm(_post(request), consent=consent)
    withdrawal = services.open_withdrawal(consent)
    impact = queries.consent_impact(actor=request.user, consent_id=consent.pk)
    blocked = ""
    if withdrawal is None or withdrawal.effective_kind != ConsentWithdrawal.Kind.DESTROY:
        blocked = "Disposal requests are offered only for a withdrawal asking for destruction."
    elif not impact.held:
        blocked = "No specimen in custody holds this withdrawal."
    return _action(
        request,
        form,
        title="Request disposal of held specimens",
        submit="Request disposal of held specimens",
        back_url=_consent_url(consent),
        crumbs=consent_crumbs(consent, ("Request disposal of held specimens", None)),
        run=lambda d: services.request_withdrawal_disposals(
            actor=request.user,
            consent_id=consent.pk,
            expected_revision=d["expected_revision"],
            method=d["method"],
            destination=d["destination"],
            reason=d["reason"],
        ),
        success=lambda rows: (
            f"Filed {len(rows)} disposal request(s); each needs approval by "
            "another person with disposal approval."
        ),
        done_url=_consent_url(consent),
        revision=lambda: Consent.objects.get(pk=consent.pk).revision,
        blocked=blocked,
        facts=(
            ("Consent", consent_label(consent)),
            ("Held specimens", ", ".join(s.accession for s in impact.held) or "None"),
        ),
        notes=(
            "Files one ordinary disposal request per held specimen. Each request still needs "
            "approval by another person with disposal approval; nothing is disposed here. "
            "Specimens picked for a request or with an open disposal request are skipped.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def consent_reinstate(request, pk):
    consent = _consent(pk)
    withdrawal = services.open_withdrawal(consent)
    form = ReinstateForm(_post(request), consent=consent, withdrawal=withdrawal)
    blocked = reinstate_refusal(request.user, consent, withdrawal)

    def run(data):
        record = data.get("record")
        return services.reinstate_consent(
            actor=request.user,
            consent_id=consent.pk,
            expected_revision=data["expected_revision"],
            record_id=record.pk if record else None,
            occurred_at=data["occurred_at"],
            reason=data["reason"],
        )

    facts = [("Consent", consent_label(consent))]
    if withdrawal is not None:
        facts.append(
            (
                "Original withdrawal",
                f"{withdrawal.get_kind_display()}, effective {withdrawal.effective_on:%Y-%m-%d}, "
                f"recorded by {person(withdrawal.actor)}",
            )
        )
        if escalation := getattr(withdrawal, "escalation", None):
            facts.append(
                (
                    "Destruction instruction",
                    f"Effective {escalation.effective_on:%Y-%m-%d}, "
                    f"received {lists.local(escalation.occurred_at)}, "
                    f"recorded by {person(escalation.actor)}",
                )
            )
    return _action(
        request,
        form,
        title="Reinstate consent",
        submit="Reinstate consent",
        back_url=_consent_url(consent),
        crumbs=consent_crumbs(consent, ("Reinstate consent", None)),
        run=run,
        success=lambda r: f"Reinstated the consent of {consent_label(consent)}.",
        done_url=_consent_url(consent),
        revision=lambda: Consent.objects.get(pk=consent.pk).revision,
        blocked=blocked,
        facts=facts,
        notes=(
            "Ends the withdrawal and releases its holds. Reinstatement requires a manager or "
            "administrator other than the people who recorded the withdrawal, escalation or new "
            "consent.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def withdrawal_follow_up(request, pk):
    withdrawal = get_object_or_404(
        ConsentWithdrawal.objects.select_related(
            "consent", "consent__participant", "consent__study", "escalation"
        ),
        pk=pk,
    )
    consent = withdrawal.consent
    shipments = _consent_shipments(consent).order_by("-id")
    form = FollowUpForm(_post(request), _files(request), shipments=shipments)
    key = request.GET.get("shipment", "")
    if key.isdigit() and shipments.filter(pk=int(key)).exists():
        form.initial["shipment"] = int(key)
    blocked = "" if shipments.exists() else "No shipment contained this participant's material."
    facts = [
        ("Consent", consent_label(consent)),
        (
            "Original withdrawal",
            f"{withdrawal.get_kind_display()}, effective {withdrawal.effective_on:%Y-%m-%d}",
        ),
    ]
    if escalation := getattr(withdrawal, "escalation", None):
        facts.append(
            (
                "Destruction instruction",
                f"{withdrawal.get_effective_kind_display()}, "
                f"effective {escalation.effective_on:%Y-%m-%d}",
            )
        )
    return _action(
        request,
        form,
        title="Record follow-up",
        submit="Record follow-up",
        back_url=_consent_url(consent),
        crumbs=consent_crumbs(consent, ("Record follow-up", None)),
        run=lambda d: services.record_follow_up(
            actor=request.user,
            withdrawal_id=withdrawal.pk,
            shipment_id=d["shipment"].pk,
            action=d["action"],
            note=d["note"],
            occurred_at=d["occurred_at"],
            upload=d.get("attachment"),
        ),
        success=lambda f: f"Recorded follow-up for {f.shipment.number}.",
        done_url=_consent_url(consent),
        blocked=blocked,
        facts=facts,
        notes=(
            "Records what was done about material already distributed. The software sends "
            "nothing and does not recall material.",
        ),
    )


# --- agreements -----------------------------------------------------------------------------


def agreement_crumbs(agreement=None, *rest):
    trail = [("Agreements", reverse("distribution:agreement-list"))]
    if agreement is not None:
        trail.append(
            (agreement.number, reverse("distribution:agreement-detail", args=[agreement.pk]))
        )
    return [*trail, *rest]


def _agreement_url(agreement):
    return reverse("distribution:agreement-detail", args=[agreement.pk])


EVENT_LABELS = {
    "kind": "Kind",
    "title": "Title",
    "institution": "Institution",
    "investigator": "Investigator",
    "provider_reference": "Provider reference",
    "recipient_reference": "Recipient reference",
    "data_level": "Data level",
    "end_disposition": "At the end of use",
    "onward_transfer": "Onward transfer",
    "permitted_use": "Permitted use",
    "expires_on": "Expires",
    "reporting_due_on": "Reporting due",
    "document_reference": "Where filed",
    "status": "Status",
    "executed_on": "Signed on",
    "effective_on": "Effective from",
    "terminated_on": "Terminated on",
    "permitted_categories": "Permitted scope categories",
}
EVENT_CHOICES = {
    "kind": dict(Agreement.Kind.choices),
    "data_level": dict(Agreement.DataLevel.choices),
    "end_disposition": dict(Agreement.EndDisposition.choices),
    "onward_transfer": dict(Agreement.OnwardTransfer.choices),
    "status": dict(Agreement.Status.choices),
}


def _event_value(name, value, names):
    if value in (None, "", []):
        return ""
    if name in EVENT_CHOICES:
        return EVENT_CHOICES[name].get(value, value)
    if name in ("institution", "investigator", "permitted_categories"):
        keys = value if isinstance(value, list) else [value]
        return ", ".join(names.get(str(k), str(k)) for k in keys)
    return str(value)


def _event_rows(events):
    keys = set()
    for event in events:
        for name in ("institution", "investigator", "permitted_categories"):
            for value in (event.changes or {}).get(name, []) or []:
                keys.update(str(v) for v in (value if isinstance(value, list) else [value]) if v)
    names = {}
    for model in (Institution, Investigator, ConsentScopeCategory):
        for obj in model.objects.filter(pk__in=[k for k in keys if _uuid(k)]):
            names[str(obj.pk)] = obj.code
    for event in events:
        event.change_rows = [
            (
                EVENT_LABELS.get(name, name),
                _event_value(name, pair[0], names),
                _event_value(name, pair[1], names),
            )
            for name, pair in (event.changes or {}).items()
            if isinstance(pair, list) and len(pair) == 2
        ]


@staff_role_required(*READ)
def agreement_detail(request, pk):
    agreement = get_object_or_404(
        Agreement.objects.select_related("institution", "investigator", "created_by"), pk=pk
    )
    documents = list(
        agreement.documents.select_related("actor", "attachment", "supersedes")
        .defer("attachment__data")
        .prefetch_related("superseded_by")
        .order_by("recorded_at", "id")
    )
    events = list(agreement.events.select_related("actor").order_by("recorded_at", "id"))
    _event_rows(events)
    linked = list(agreement.requests.select_related("investigator", "institution").order_by("-id"))
    toc = [
        ("facts-heading", "Agreement"),
        ("terms-heading", "Terms as recorded"),
        ("requests-heading", f"Requests ({len(linked)})"),
        ("documents-heading", f"Documents ({len(documents)})"),
        ("timeline-heading", "Timeline"),
        ("history-heading", "History"),
    ]
    status = agreement.status
    return render(
        request,
        "distribution/governance/agreement_detail.html",
        {
            "page_title": agreement.number,
            "agreement": agreement,
            "categories": list(agreement.permitted_categories.order_by("code")),
            "documents": documents,
            "events": events,
            "linked_requests": linked,
            "today": timezone.localdate(),
            "edit_reason": "A terminated agreement cannot be edited."
            if status == Agreement.Status.TERMINATED
            else "",
            "terminate_reason": "Only an executed agreement can be terminated."
            if status == Agreement.Status.DRAFT
            else "",
            "toc": toc,
            "history": history(agreement),
            **flags(request.user),
        },
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def agreement_edit(request, pk=None):
    agreement = get_object_or_404(Agreement, pk=pk) if pk else None
    form = AgreementForm(_post(request), instance=agreement)

    def run(data):
        return services.save_agreement(
            actor=request.user,
            agreement_id=agreement.pk if agreement else None,
            expected_revision=data.get("expected_revision") if agreement else None,
            values=form.agreement_values(),
            permitted_categories=form.categories(),
            reason=data.get("reason", ""),
        )

    title = f"Edit {agreement.number}" if agreement else "New agreement"
    blocked = ""
    notes = [
        "Record the terms as the agreement states them. The software does not draft, check or "
        "interpret agreements.",
    ]
    if agreement is not None and agreement.status == Agreement.Status.TERMINATED:
        blocked = "A terminated agreement cannot be edited."
    elif agreement is not None and agreement.status == Agreement.Status.EXECUTED:
        notes.append(
            "Executed: only the title, references, expiry, reporting date and filing reference "
            "can change; record an amendment as a document."
        )
    return _action(
        request,
        form,
        title=title,
        submit="Save changes" if agreement else "Create agreement",
        back_url=_agreement_url(agreement) if agreement else reverse("distribution:agreement-list"),
        crumbs=agreement_crumbs(agreement, (title, None)),
        run=run,
        success=lambda a: f"Saved {a.number}." if agreement else f"Recorded {a.number} as a draft.",
        done_url=_agreement_url,
        revision=lambda: Agreement.objects.get(pk=agreement.pk).revision if agreement else None,
        blocked=blocked,
        notes=notes,
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def agreement_execute(request, pk):
    agreement = get_object_or_404(Agreement.objects.select_related("institution"), pk=pk)
    form = ExecuteForm(_post(request), agreement=agreement)
    blocked = (
        ""
        if agreement.status == Agreement.Status.DRAFT
        else "Only a draft agreement can be executed."
    )
    return _action(
        request,
        form,
        title="Execute agreement",
        submit="Execute agreement",
        back_url=_agreement_url(agreement),
        crumbs=agreement_crumbs(agreement, ("Execute agreement", None)),
        run=lambda d: services.execute_agreement(
            actor=request.user,
            agreement_id=agreement.pk,
            expected_revision=d["expected_revision"],
            executed_on=d["executed_on"],
            effective_on=d["effective_on"],
            expires_on=d.get("expires_on"),
            document_reference=d.get("document_reference", ""),
            reason=d["reason"],
        ),
        success=lambda a: f"Recorded {a.number} as executed.",
        done_url=_agreement_url(agreement),
        revision=lambda: Agreement.objects.get(pk=agreement.pk).revision,
        blocked=blocked,
        facts=(
            ("Agreement", str(agreement)),
            ("Institution", f"{agreement.institution.code} · {agreement.institution.name}"),
        ),
        notes=(
            "Records that the agreement was signed. Its terms are fixed from now on. Dispatch "
            "under it needs it to be in effect on the dispatch date.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def agreement_terminate(request, pk):
    agreement = get_object_or_404(Agreement, pk=pk)
    form = TerminateForm(_post(request), agreement=agreement)
    blocked = (
        ""
        if agreement.status == Agreement.Status.EXECUTED
        else "Only an executed agreement can be terminated."
    )
    return _action(
        request,
        form,
        title="Terminate agreement",
        submit="Terminate agreement",
        back_url=_agreement_url(agreement),
        crumbs=agreement_crumbs(agreement, ("Terminate agreement", None)),
        run=lambda d: services.terminate_agreement(
            actor=request.user,
            agreement_id=agreement.pk,
            expected_revision=d["expected_revision"],
            terminated_on=d["terminated_on"],
            reason=d["reason"],
        ),
        success=lambda a: f"Recorded the termination of {a.number}.",
        done_url=_agreement_url(agreement),
        revision=lambda: Agreement.objects.get(pk=agreement.pk).revision,
        blocked=blocked,
        destructive=True,
        facts=(("Agreement", str(agreement)),),
        notes=(
            "Nothing can be dispatched under the agreement from the termination date. Requests "
            "linked to it keep the link.",
        ),
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def agreement_document(request, pk):
    agreement = get_object_or_404(Agreement, pk=pk)
    form = DocumentForm(_post(request), _files(request), agreement=agreement)
    return _action(
        request,
        form,
        title="Add document",
        submit="Add document",
        back_url=_agreement_url(agreement),
        crumbs=agreement_crumbs(agreement, ("Add document", None)),
        run=lambda d: services.add_agreement_document(
            actor=request.user,
            agreement_id=agreement.pk,
            kind=d["kind"],
            title=d["title"],
            note=d.get("note", ""),
            upload=d.get("attachment"),
            supersedes_id=d["supersedes"].pk if d.get("supersedes") else None,
        ),
        success=lambda doc: f"Added {doc.title} to {agreement.number}.",
        done_url=_agreement_url(agreement),
        facts=(("Agreement", str(agreement)),),
        notes=(
            "Documents are kept; a newer version supersedes an older one and both stay listed.",
        ),
    )


# --- catalogues -----------------------------------------------------------------------------


def _catalogue_edit(request, kind, pk=None):
    prefix, tab, noun = CATALOGUE_TABS[kind]
    model = services.CATALOGUES[kind]
    obj = get_object_or_404(model, pk=pk) if pk else None
    in_use = services.catalogue_in_use(kind, obj)
    initial = {}
    if obj is None and kind == "consent_form" and _uuid(request.GET.get("study")):
        study = Study.objects.filter(pk=_uuid(request.GET.get("study"))).first()
        if study is not None:
            initial["study"] = study.pk
    form = CatalogueForm(_post(request), kind=kind, instance=obj, in_use=in_use, initial=initial)
    list_url = reverse(f"distribution:{prefix}-list")

    def run(data):
        return services.save_distribution_catalogue(
            actor=request.user,
            kind=kind,
            object_id=obj.pk if obj else None,
            expected_revision=data.get("expected_revision") if obj else None,
            values=form.catalogue_values(),
            reason=data.get("reason", ""),
        )

    def label(saved):
        return f"{saved.code} v{saved.version}" if kind == "consent_form" else saved.code

    title = f"Edit {label(obj)}" if obj else f"New {noun}"
    return _action(
        request,
        form,
        title=title,
        submit="Save changes" if obj else f"Create {noun}",
        back_url=list_url,
        crumbs=[("Distribution catalogues", None), (tab, list_url), (title, None)],
        run=run,
        success=lambda saved: f"{'Saved' if obj else 'Created'} {noun} {label(saved)}.",
        done_url=list_url,
        revision=lambda: model.objects.get(pk=obj.pk).revision if obj else None,
        notes=(
            "Entries are never deleted; clear Active to stop offering one.",
            *(["In use: the fields marked Fixed can no longer change."] if in_use else []),
        ),
        template="distribution/governance/catalogue_form.html",
        extra={"obj": obj, "history": history(obj, 20) if obj else []},
    )


@staff_role_required(*MANAGE)
@ACTION_METHODS
def category_edit(request, pk=None):
    return _catalogue_edit(request, "category", pk)


@staff_role_required(*MANAGE)
@ACTION_METHODS
def consent_form_edit(request, pk=None):
    return _catalogue_edit(request, "consent_form", pk)


@staff_role_required(*MANAGE)
@ACTION_METHODS
def institution_edit(request, pk=None):
    return _catalogue_edit(request, "institution", pk)


@staff_role_required(*MANAGE)
@ACTION_METHODS
def investigator_edit(request, pk=None):
    return _catalogue_edit(request, "investigator", pk)
