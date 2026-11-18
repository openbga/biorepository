"""Specimen lists, record pages, documents and downloads.

Views handle transport: the role check at entry, filter and form binding, calling
specimens.queries / services / importing / exports / labels and showing their errors. Every rule
is enforced again by those modules. Action forms live in specimens.action_views.
"""

from datetime import datetime
from urllib.parse import urlencode

from django.apps import apps
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.roles import can_approve_disposal, has_role, staff_role_required
from core.grids import GridRequest, active_filters
from core.models import AuditEvent
from storage import pathbar
from storage import queries as storage_queries
from storage.forms import apply_errors

from . import exports, importing, labels, queries, record_links, services
from .action_views import refusal
from .list_forms import (
    CatalogueFilter,
    CatalogueForm,
    ImportCommitForm,
    ImportUploadForm,
    SpecimenFilter,
    disposal_selection,
    import_selection,
    label_ids,
)
from .models import (
    Collection,
    CustodyEvent,
    DisposalRequest,
    Hold,
    ImportBatch,
    QuantityEntry,
    Specimen,
    SpecimenContainerType,
    SpecimenType,
    ThawEvent,
)
from .services import (
    CATALOGUE_NOUNS,
    CATALOGUES,
    DISPOSAL_SCOPES,
    MANAGE_ROLES,
    OPERATE_ROLES,
    READ_ROLES,
    USE_SCOPES,
    catalogue_in_use,
    never_stored,
    request_state,
    save_specimen_catalogue,
)

READ = READ_ROLES
OPERATE = OPERATE_ROLES
MANAGE = MANAGE_ROLES


def flags(user):
    """Which controls to render. Services and view decorators still enforce every action."""
    return {
        "can_operate": has_role(user, *OPERATE),
        "can_manage": has_role(user, *MANAGE),
        "can_approve": can_approve_disposal(user),
    }


def _grid(request, columns, default_sort):
    """GridRequest for the query string, or None for an invalid page size."""
    try:
        return GridRequest.parse(request.GET, columns, default_sort)
    except ValueError:
        return None


def _page(queryset, grid):
    return Paginator(queryset, grid.per_page).get_page(grid.page_number)


def _chips(request, form):
    chips = active_filters(request, form)
    for chip in chips:
        if chip["value"] == "True":
            chip["value"] = "Yes"
    return chips


def _without_page(request):
    query = request.GET.copy()
    query.pop("page", None)
    return query.urlencode()


def _csv(content, name):
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    response["Cache-Control"] = "no-store"
    return response


def _stamp():
    return timezone.localtime().strftime("%Y%m%d-%H%M")


def _list_url(request):
    query = _without_page(request)
    return reverse("specimens:specimen-list") + (f"?{query}" if query else "")


# --- specimens ------------------------------------------------------------------------------


def _storage_path(user, specimen, location, checkout):
    """The path bar: down to the position it is in, or, while it is checked out, the position it
    left (shown freed). Nothing for a specimen never stored, distributed or disposed."""
    if location:
        return pathbar.levels(location)
    if checkout is None:
        return []
    moves = storage_queries.movement_history(actor=user, occupancy_id=specimen.occupancy_id)
    last = moves[len(moves) - 1] if moves else None
    if last is None or last.event.kind != "check_out" or not last.from_path:
        return []
    return pathbar.levels(last.from_path, freed=True)


def _with_names(rows):
    """Add each catalogue entry's current name beside the code a validated row recorded: one
    query per catalogue for the whole preview. The rows themselves are left as validated."""
    for column, model in (
        ("specimen_type", SpecimenType),
        ("container_type", SpecimenContainerType),
        ("collection_code", Collection),
    ):
        ids = {row[column]["id"] for row in rows if isinstance(row.get(column), dict)}
        found = model.objects.filter(pk__in=ids).values_list("pk", "name") if ids else ()
        names = {str(pk): name for pk, name in found}
        for row in rows:
            ref = row.get(column)
            if isinstance(ref, dict):
                row[column] = {**ref, "name": names.get(ref["id"], "")}
    return rows


@staff_role_required(*READ)
def specimen_list(request):
    grid = _grid(request, queries.SPECIMEN_SORTS, "accession")
    if grid is None:
        return HttpResponseBadRequest("Invalid page size. Choose 25, 50 or 100.")
    form = SpecimenFilter(request.GET)
    if form.is_valid():
        queryset = queries.specimen_queryset(form.filters(), grid.sort)
        chips = _chips(request, form)
    else:
        queryset, chips = Specimen.objects.none(), []  # never widen an invalid filter
    page_obj = _page(queryset, grid)
    rows = list(page_obj.object_list)
    paths = queries.locations_for(actor=request.user, specimens=rows) if rows else {}
    # How long each checked-out row has been out, for its clock: one query for the page.
    out_since = {
        pk: (occurred_at, kind)
        for pk, occurred_at, kind in CustodyEvent.objects.filter(
            pk__in=[row.open_checkout_id for row in rows if row.open_checkout_id]
        ).values_list("pk", "occurred_at", "kind")
    }
    now = timezone.now()
    for row in rows:
        row.path = paths.get(row.pk)
        since, kind = out_since.get(row.open_checkout_id, (None, None))
        row.out_for = queries.format_duration(now - since) if since else ""
        row.returned = kind == CustodyEvent.Kind.RETURN  # in hand after a return, as on the record
    page_obj.object_list = rows
    query = _without_page(request)
    return render(
        request,
        "specimens/specimen_list.html",
        {
            "page_title": "Specimens",
            "page_obj": page_obj,
            "filter_form": form,
            "q": request.GET.get("q", "").strip(),
            "sort": grid.sort,
            "active_filters": chips,
            "filtered": bool(chips),
            "inline": [form[name] for name in SpecimenFilter.INLINE],
            "more": [form[name] for name in SpecimenFilter.MORE],
            "more_open": any(form[name].value() for name in SpecimenFilter.MORE),
            "export_url": reverse("specimens:export") + (f"?{query}" if query else ""),
            "max_labels": labels.MAX_LABELS,
            **flags(request.user),
        },
    )


@staff_role_required(*READ)
@require_GET
def lookup(request):
    text = request.GET.get("q", "")
    try:
        specimen = queries.lookup_specimen(actor=request.user, accession=text)
    except Specimen.DoesNotExist:
        code = queries.normalize_accession(text)
        url = reverse("specimens:specimen-list")
        if not code:
            return redirect(url)
        messages.warning(request, f"No specimen has ID {code}.")
        return redirect(f"{url}?{urlencode({'q': text.strip()})}")
    return redirect("specimens:specimen-detail", pk=specimen.pk)


@staff_role_required(*READ)
@require_GET
def export(request):
    form = SpecimenFilter(request.GET)
    if not form.is_valid():
        messages.error(request, "These filters cannot be applied. Correct them, then export.")
        return redirect(_list_url(request))
    sort = request.GET.get("sort", "accession")
    if sort.removeprefix("-") not in queries.SPECIMEN_SORTS:
        sort = "accession"
    try:
        content = exports.export_specimens(actor=request.user, filters=form.filters(), sort=sort)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(_list_url(request))
    return _csv(content, f"specimens-{_stamp()}.csv")


def _action(label, route, pk, reason="", query=""):
    url = reverse(f"specimens:{route}", args=[pk]) + (f"?{query}" if query else "")
    return {"label": label, "url": url, "reason": reason, "key": route.replace("-", "_")}


DISTRIBUTED_REASON = "Distributed: it is outside the repository until its return is recorded."


def _hold_reason(holds, scopes, blocks):
    """Why the first active hold of these scopes blocks the action, or ""."""
    hold = next((h for h in holds if h.scope in scopes), None)
    if hold is None:
        return ""
    label = hold.get_scope_display().lower()
    if hold.reference:
        return (
            f"An active {label} ({hold.reference}) {blocks}. It is released through that workflow."
        )
    return f"An active {label} {blocks}. A repository manager can release it."


def _record_actions(specimen, user, state, holds=()):
    """(destructive, primary, more) header actions for the specimen's state and the user's roles.

    A control the role can never use is left out; one the state blocks carries the reason.
    `holds` are the active holds: use and all-use holds block use, disposal and all-use holds
    block disposal. A distributed specimen only takes holds and record corrections.
    """
    if not has_role(user, *OPERATE):
        return None, None, []
    manage = has_role(user, *MANAGE)
    pk = specimen.pk
    if specimen.is_disposed:
        more = [_action("Correct specimen", "correct", pk)] if manage else []
        return None, None, more
    away = DISTRIBUTED_REASON if specimen.is_distributed else ""
    use_hold = _hold_reason(holds, USE_SCOPES, "blocks this")
    available = specimen.status == Specimen.Status.AVAILABLE
    in_hand = state["checked_out"] or (not state["placed"] and state["never_stored"])
    primary = None
    if state["checked_out"]:
        primary = _action("Record check-in", "check-in", pk)
    elif state["placed"]:
        primary = _action("Record check-out", "check-out", pk, use_hold)
    elif available and state["never_stored"]:
        primary = _action("Place in storage", "place", pk)

    def use_reason():
        if away:
            return away
        if use_hold:
            return use_hold
        if not available:
            return "Nothing is left on hand."
        if not in_hand:
            return "It is in storage. Record a check-out first."
        return ""

    more = [
        _action("Make aliquots or derivatives", "derive", pk, use_reason()),
        _action("Record custody transfer", "transfer", pk, away or use_hold),
        _action("Record consumption", "consume", pk, use_reason()),
        _action("Record observed thaw", "thaw-record", pk, away),
        _action("Place hold", "hold-add", pk),
        _action(
            "Relocate",
            "relocate",
            pk,
            away or ("" if state["placed"] else "It is not in a storage position."),
        ),
    ]
    if manage:
        more += [
            _action("Correct specimen", "correct", pk),
            _action("Adjust amount", "adjust", pk, away),
        ]
    destructive_reason = away or _hold_reason(holds, DISPOSAL_SCOPES, "blocks disposal")
    if not destructive_reason and state["open_request"]:
        destructive_reason = "A disposal request is already open."
    destructive = _action("Request disposal", "disposal-request", pk, destructive_reason)
    return destructive, primary, more


def distribution_links(specimen):
    """Links into the optional distribution app, [] when it is not installed or lacks a route.

    Specimens never imports distribution: only URL names are resolved.
    """
    if not apps.is_installed("distribution"):
        return []
    wanted = [
        ("Requests for this specimen", "distribution:request-list", "specimen", specimen.accession),
        (
            "Shipments of this specimen",
            "distribution:shipment-list",
            "specimen",
            specimen.accession,
        ),
    ]
    if specimen.participant_id:
        code = specimen.participant.code
        wanted.append(
            (
                f"Consent records of participant {code}",
                "distribution:consent-list",
                "participant",
                code,
            )
        )
    links = []
    for label, name, key, value in wanted:
        try:
            target = reverse(name)
        except NoReverseMatch:
            continue
        links.append({"label": label, "url": f"{target}?{urlencode({key: value})}"})
    return links


def _custody_roots(custody):
    """Mark each custody event with the kind of the event its correction chain starts from."""
    by_pk = {event.pk: event for event in custody}
    for event in custody:
        root = event
        while root.corrects_id and root.corrects_id in by_pk:
            root = by_pk[root.corrects_id]
        event.root_kind = root.kind


@staff_role_required(*READ)
def specimen_detail(request, pk):
    specimen = get_object_or_404(
        queries.with_flags(
            Specimen.objects.select_related(
                "specimen_type",
                "container_type",
                "unit",
                "participant",
                "collection",
                "parent",
                "registered_by",
                "custodian",
                "occupancy",
                "open_checkout__to_custodian",
                "open_checkout__actor",
            )
        ),
        pk=pk,
    )
    user = request.user
    occupancy = specimen.occupancy
    location = queries.locations_for(actor=user, specimens=[specimen]).get(specimen.pk)
    ledger = list(
        QuantityEntry.objects.filter(specimen=specimen)
        .select_related("actor", "unit")
        .order_by("occurred_at", "recorded_at", "id")
    )
    reversed_ids = {e.reverses_id for e in ledger if e.reverses_id}
    for entry in ledger:
        entry.reversible = (
            entry.kind in (QuantityEntry.Kind.CONSUME, QuantityEntry.Kind.ADJUST)
            and entry.pk not in reversed_ids
        )
        entry.reversed = entry.pk in reversed_ids
    custody = list(
        CustodyEvent.objects.filter(specimen=specimen)
        .select_related("actor", "from_custodian", "to_custodian", "corrects", "move_event")
        .order_by("occurred_at", "recorded_at", "id")
    )
    corrected = {e.corrects_id for e in custody if e.corrects_id}
    for event in custody:
        event.corrected = event.pk in corrected
    _custody_roots(custody)
    thaws = list(
        ThawEvent.objects.filter(specimen=specimen)
        .select_related("actor", "corrects")
        .order_by("occurred_at", "recorded_at", "id")
    )
    thaw_corrected = {e.corrects_id for e in thaws if e.corrects_id}
    for event in thaws:
        event.corrected = event.pk in thaw_corrected
    holds = list(
        Hold.objects.filter(specimen=specimen)
        .select_related("actor", "release__actor")
        .order_by("occurred_at", "recorded_at", "id")
    )
    for hold in holds:
        hold.released = getattr(hold, "release", None)
    active_holds = [h for h in holds if h.released is None]
    disposals = queries.disposal_requests(specimen)
    open_request = next((r for r in disposals if r.state in ("pending", "approved")), None)
    state = {
        "placed": occupancy.position_id is not None,
        "checked_out": bool(specimen.open_checkout_id),
        "never_stored": never_stored(specimen),
        "open_request": open_request,
    }
    destructive, primary, more = _record_actions(specimen, user, state, active_holds)
    timeout = queries.time_out_of_storage(specimen)
    checkout = specimen.open_checkout
    returned = checkout is not None and checkout.kind == CustodyEvent.Kind.RETURN
    # While distributed, the open time away names where it was sent.
    away_now = next((a for a in timeout.away if a.ended is None), None)
    initial = next((e for e in ledger if e.kind == QuantityEntry.Kind.INITIAL), None)
    execution = getattr(specimen, "disposal", None)
    can_operate = has_role(user, *OPERATE)
    live = can_operate and not specimen.is_disposed
    for entry in ledger:
        entry.reversible = entry.reversible and not specimen.is_distributed
    return render(
        request,
        "specimens/specimen_detail.html",
        {
            "page_title": specimen.accession,
            "specimen": specimen,
            "location": location,
            "storage_path": _storage_path(user, specimen, location, checkout),
            "out_of_storage": {"duration": queries.format_duration(timeout.total)}
            if timeout.open
            else None,
            "state": state,
            # Equipment is optional: its excursion list filters by the specimens it captured.
            "excursions_url": reverse("equipment:excursion-list")
            + "?"
            + urlencode({"specimen": specimen.accession})
            if apps.is_installed("equipment")
            else "",
            "destructive": destructive,
            "primary": primary,
            "more": more,
            "card_actions": {a["key"]: a for a in more if not a["reason"]} if live else {},
            "children": Specimen.objects.filter(parent=specimen)
            .select_related("specimen_type", "unit")
            .order_by("accession"),
            "ledger": ledger,
            "initial": initial,
            "custody": custody,
            "thaws": thaws,
            "holds": holds,
            "active_holds": active_holds,
            "returned": returned,
            "away_now": away_now,
            "distribution_links": distribution_links(specimen),
            "distribution_rows": record_links.distribution_rows(user, specimen),
            "disposals": disposals,
            "open_request": open_request,
            "execution": execution,
            "timeout": timeout,
            "unplaced_running": state["never_stored"] and not specimen.is_disposed,
            "thaw": queries.thaw_summary(specimen),
            "registration": specimen.custody.filter(kind=CustodyEvent.Kind.REGISTER).first(),
            "history": queries.specimen_history(actor=user, specimen_id=specimen.pk),
            "live": live,
            **flags(user),
        },
    )


# --- disposal requests ----------------------------------------------------------------------


@staff_role_required(*READ)
def disposal_list(request):
    try:
        grid, form, rows = disposal_selection(request.user, request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    page_obj = _page(rows, grid)
    page_obj.object_list = queries.with_states(page_obj.object_list)
    chips = _chips(request, form) if form.is_valid() else []
    inline = [form["state"], form["mine"]]
    if "awaiting_me" in form.fields:
        inline.append(form["awaiting_me"])
    over_limit = page_obj.paginator.count > exports.MAX_ROWS
    export_url = None
    if form.is_valid() and not over_limit:
        export_url = reverse(
            "specimens:disposal-export", query={**form.filters(), "sort": grid.sort}
        )
    return render(
        request,
        "specimens/disposal_list.html",
        {
            "page_title": "Disposal requests",
            "export_url": export_url,
            "exceeded_export_limit": f"{exports.MAX_ROWS:,}" if over_limit else "",
            "page_obj": page_obj,
            "filter_form": form,
            "q": request.GET.get("q", "").strip(),
            "sort": grid.sort,
            "active_filters": chips,
            "filtered": bool(chips),
            "inline": inline,
            **flags(request.user),
        },
    )


@staff_role_required(*READ)
@require_GET
def disposal_export(request):
    try:
        content = exports.export_disposals(actor=request.user, params=request.GET)
    except ValidationError as exc:
        return HttpResponseBadRequest(
            " ".join(exc.messages), content_type="text/plain; charset=utf-8"
        )
    return _csv(content, "disposal-requests.csv")


@staff_role_required(*READ)
def disposal_detail(request, pk):
    row = get_object_or_404(
        DisposalRequest.objects.select_related(
            "specimen__occupancy",
            "specimen__unit",
            "specimen__specimen_type",
            "requested_by",
            "decision__actor",
            "execution__actor",
        ),
        pk=pk,
    )
    specimen = queries.with_flags(Specimen.objects.filter(pk=row.specimen_id)).get()
    state = request_state(row, specimen, row.specimen.occupancy)
    user = request.user
    operate = has_role(user, *OPERATE)
    requester = row.requested_by_id == user.pk
    decision = getattr(row, "decision", None)
    execution = getattr(row, "execution", None)
    # Approval and recording need the material here and no disposal or all-use hold.
    held_reason = refusal(services.require_in_custody, specimen) or refusal(
        services.require_no_disposal_hold, specimen
    )
    timeline = [
        {
            "title": "Disposal requested",
            "at": row.recorded_at,
            "actor": row.requested_by,
            "reason": row.reason,
            "detail": f"Method: {row.method} · Destination: {row.destination}",
        }
    ]
    if decision:
        timeline.append(
            {
                "title": f"Disposal {decision.get_decision_display().lower()}",
                "at": decision.recorded_at,
                "actor": decision.actor,
                "reason": decision.reason,
                "detail": "",
            }
        )
    if execution:
        timeline.append(
            {
                "title": "Disposal recorded",
                "at": execution.occurred_at,
                "actor": execution.actor,
                "reason": execution.reason,
                "detail": "",
            }
        )
    title = f"Disposal request for {row.specimen.accession}"
    return render(
        request,
        "specimens/disposal_detail.html",
        {
            "page_title": title,
            "req": row,
            "specimen": row.specimen,
            "state": state,
            "decision": decision,
            "execution": execution,
            "requester": requester,
            "can_decide": operate and can_approve_disposal(user) and not requester,
            # Only an undecided request can be cancelled (one decision per request); an approved
            # one is recorded, or goes out of date when the specimen changes.
            "can_cancel": operate and decision is None and (requester or has_role(user, *MANAGE)),
            "can_execute": operate and state == "approved",
            "held_reason": held_reason,
            "timeline": timeline,
            **flags(user),
        },
    )


# --- imports --------------------------------------------------------------------------------


def _local(value):
    """An ISO 8601 string from a batch row, as a repository-time datetime (None if absent)."""
    try:
        return timezone.localtime(datetime.fromisoformat(value)) if value else None
    except TypeError, ValueError:
        return None


@staff_role_required(*OPERATE)
def import_list(request):
    try:
        grid, form, rows = import_selection(request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    page_obj = _page(rows, grid)
    now = timezone.now()
    for batch in page_obj:
        batch.state = batch.state_at(now, error_total=batch.error_total)
    chips = _chips(request, form) if form.is_valid() else []
    over_limit = page_obj.paginator.count > exports.MAX_ROWS
    export_url = None
    if form.is_valid() and not over_limit:
        export_url = reverse("specimens:import-export", query={"q": grid.query, "sort": grid.sort})
    return render(
        request,
        "specimens/import_list.html",
        {
            "page_title": "Imports",
            "export_url": export_url,
            "exceeded_export_limit": f"{exports.MAX_ROWS:,}" if over_limit else "",
            "page_obj": page_obj,
            "filter_form": form,
            "q": request.GET.get("q", "").strip(),
            "sort": grid.sort,
            "active_filters": chips,
            "filtered": bool(chips),
            **flags(request.user),
        },
    )


@staff_role_required(*OPERATE)
@require_GET
def import_export(request):
    try:
        content = exports.export_imports(actor=request.user, params=request.GET)
    except ValidationError as exc:
        return HttpResponseBadRequest(
            " ".join(exc.messages), content_type="text/plain; charset=utf-8"
        )
    return _csv(content, "specimen-imports.csv")


@staff_role_required(*OPERATE)
@require_http_methods(["GET", "POST"])
def import_upload(request):
    form = ImportUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        try:
            batch = importing.validate_import(
                actor=request.user, csv_bytes=upload.read(), filename=upload.name
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            if batch.total_errors:
                messages.warning(
                    request, f"The file has {batch.total_errors} errors. Nothing was registered."
                )
            else:
                messages.success(
                    request, f"The file is valid: {batch.row_count} rows ready to register."
                )
            return redirect("specimens:import-detail", pk=batch.pk)
    return render(
        request,
        "specimens/import_upload.html",
        {
            "page_title": "Upload CSV",
            "form": form,
            "columns": importing.COLUMNS,
            "required": importing.REQUIRED,
        },
        status=422 if request.method == "POST" and form.errors else 200,
    )


@staff_role_required(*OPERATE)
@require_GET
def import_template(request):
    return _csv(importing.template_csv().encode("utf-8"), "specimen-import-template.csv")


@staff_role_required(*OPERATE)
def import_detail(request, pk):
    batch = get_object_or_404(ImportBatch.objects.select_related("actor", "commit__actor"), pk=pk)
    state = batch.state_at(timezone.now(), error_total=batch.total_errors)
    commit = getattr(batch, "commit", None) if state == "committed" else None
    created = []
    if commit:
        order = {pk: n for n, pk in enumerate(commit.specimen_ids)}
        created = sorted(
            Specimen.objects.filter(pk__in=commit.specimen_ids).select_related(
                "specimen_type", "unit"
            ),
            key=lambda s: order.get(str(s.pk), 0),
        )
    may_commit = batch.actor_id == request.user.pk or has_role(request.user, *MANAGE)
    form = ImportCommitForm(initial={"expected_sha256": batch.sha256})
    return render(
        request,
        "specimens/import_detail.html",
        {
            "page_title": batch.name,
            "batch": batch,
            "state": state,
            "file_errors": [e for e in batch.errors if not e.get("line")],
            "row_errors": [e for e in batch.errors if e.get("line")],
            "rows": _with_names(
                [
                    dict(row, collected=_local(row.get("collected_at")))
                    for row in batch.normalized_rows
                ]
            ),
            "commit": commit,
            "created": created,
            "label_query": urlencode([("id", s.pk) for s in created[: labels.MAX_LABELS]]),
            "may_commit": may_commit,
            "form": form,
            **flags(request.user),
        },
    )


@staff_role_required(*OPERATE)
@require_POST
def import_commit(request, pk):
    batch = get_object_or_404(ImportBatch, pk=pk)
    form = ImportCommitForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give a reason for the registration, then commit again.")
        return redirect("specimens:import-detail", pk=batch.pk)
    try:
        commit = importing.commit_import(
            actor=request.user,
            batch_id=batch.pk,
            expected_sha256=form.cleaned_data["expected_sha256"],
            reason=form.cleaned_data["reason"],
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        count = len(commit.specimen_ids)
        messages.success(request, f"Registered {count} specimen{'s' if count != 1 else ''}.")
    return redirect("specimens:import-detail", pk=batch.pk)


# --- labels ---------------------------------------------------------------------------------


@staff_role_required(*READ)
@require_GET
def label_preview(request):
    stock = labels.default_stock()
    error, items = "", []
    try:
        ids = label_ids(request.GET.getlist("id"))
        specimens = labels.label_specimens(ids)
    except ValidationError as exc:
        error = " ".join(exc.messages)
    except Specimen.DoesNotExist:
        error = "A label request names a specimen that does not exist."
    else:
        for specimen in specimens:
            try:
                barcode, problem = labels.barcode_svg(specimen.accession, stock), ""
            except ValidationError as exc:
                barcode, problem = "", " ".join(exc.messages)
            items.append({"specimen": specimen, "barcode": barcode, "problem": problem})
    return render(
        request,
        "specimens/label_preview.html",
        {
            "page_title": "Labels",
            "stock": stock,
            "items": items,
            "error": error,
            "printable": bool(items) and not any(i["problem"] for i in items),
            "query": urlencode([("id", i["specimen"].pk) for i in items]),
        },
    )


@staff_role_required(*READ)
@require_GET
def label_pdf(request):
    query = urlencode([("id", v) for v in request.GET.getlist("id")])
    back = reverse("specimens:label-preview") + (f"?{query}" if query else "")
    try:
        ids = label_ids(request.GET.getlist("id"))
        pdf = labels.render_labels(
            actor=request.user, specimen_ids=ids, stock=labels.default_stock()
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(back)
    except Specimen.DoesNotExist:
        messages.error(request, "A label request names a specimen that does not exist.")
        return redirect(back)
    except OSError:
        messages.error(request, "The label PDF cannot be produced on this server right now.")
        return redirect(back)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="labels-{_stamp()}.pdf"'
    response["Cache-Control"] = "no-store"
    return response


# --- catalogues -----------------------------------------------------------------------------

CATALOGUE_TABS = {
    # kind: (route prefix, tab label, New-button noun)
    "participant": ("participant", "Participants", "participant"),
    "collection": ("collection", "Collections", "collection"),
    "specimen_type": ("specimen-type", "Specimen types", "specimen type"),
    "container_type": ("container-type", "Specimen containers", "specimen container"),
    "unit": ("unit", "Units", "unit"),
}


def _catalogue_sorts(kind):
    return {"code": "code"} if kind == "participant" else {"code": "code", "name": "name"}


@staff_role_required(*READ)
def catalogue_list(request, kind):
    if kind not in CATALOGUE_TABS:
        raise Http404
    prefix, tab, noun = CATALOGUE_TABS[kind]
    grid = _grid(request, _catalogue_sorts(kind), "code")
    if grid is None:
        return HttpResponseBadRequest("Invalid page size. Choose 25, 50 or 100.")
    form = CatalogueFilter(request.GET, kind=kind)
    if form.is_valid():
        queryset = queries.catalogue_queryset(kind, form.filters(), grid.sort)
        chips = _chips(request, form)
    else:
        queryset, chips = CATALOGUES[kind].objects.none(), []
    query = _without_page(request)
    export_url = reverse("specimens:catalogue-export", args=[kind])
    return render(
        request,
        "specimens/catalogue_list.html",
        {
            "page_title": "Specimen catalogues",
            "kind": kind,
            "tab": tab,
            "noun": noun,
            "plural": tab.lower(),
            "create_url": reverse(f"specimens:{prefix}-create"),
            "edit_route": f"specimens:{prefix}-edit",
            "page_obj": _page(queryset, grid),
            "filter_form": form,
            "q": request.GET.get("q", "").strip(),
            "sort": grid.sort,
            "active_filters": chips,
            "filtered": bool(chips),
            "inline": [form["active"]],
            "export_url": export_url + (f"?{query}" if query else ""),
            **flags(request.user),
        },
    )


@staff_role_required(*READ)
@require_GET
def catalogue_export(request, kind):
    if kind not in CATALOGUES:
        raise Http404
    form = CatalogueFilter(request.GET, kind=kind)
    back = reverse(f"specimens:{CATALOGUE_TABS[kind][0]}-list")
    if not form.is_valid():
        messages.error(request, "These filters cannot be applied. Correct them, then export.")
        return redirect(back)
    sort = request.GET.get("sort", "code")
    if sort.removeprefix("-") not in _catalogue_sorts(kind):
        sort = "code"
    try:
        content = exports.export_catalogue(
            actor=request.user, kind=kind, filters=form.filters(), sort=sort
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(back)
    return _csv(content, f"specimen-{kind.replace('_', '-')}s-{_stamp()}.csv")


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def catalogue_edit(request, kind, pk=None):
    if kind not in CATALOGUE_TABS:
        raise Http404
    prefix, tab, noun = CATALOGUE_TABS[kind]
    obj = get_object_or_404(CATALOGUES[kind], pk=pk) if pk else None
    in_use = catalogue_in_use(kind, obj)
    form = CatalogueForm(request.POST or None, kind=kind, instance=obj, in_use=in_use)
    list_url = reverse(f"specimens:{prefix}-list")
    if request.method == "POST" and form.is_valid():
        try:
            saved = save_specimen_catalogue(
                actor=request.user,
                kind=kind,
                object_id=obj.pk if obj else None,
                expected_revision=form.cleaned_data.get("expected_revision") if obj else None,
                values=form.values(),
                reason=form.cleaned_data.get("reason", ""),
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            verb = "Saved" if obj else "Created"
            messages.success(request, f"{verb} {CATALOGUE_NOUNS[kind]} {saved.code}.")
            return redirect(list_url)
    title = f"Edit {obj.code}" if obj else f"New {noun}"
    frozen = "code, dimension and fractional" if kind == "unit" else "code"
    history = []
    if obj:
        history = list(
            AuditEvent.objects.filter(object_type=obj._meta.label_lower, object_id=str(obj.pk))
            .select_related("actor")
            .order_by("-occurred_at", "-id")[:20]
        )
    return render(
        request,
        "specimens/catalogue_form.html",
        {
            "page_title": title,
            "form": form,
            "obj": obj,
            "tab": tab,
            "list_url": list_url,
            "submit_label": "Save changes" if obj else f"Create {noun}",
            "in_use_note": f"In use: its {frozen} can no longer change." if in_use else "",
            "history": history,
        },
        status=422 if request.method == "POST" and form.errors else 200,
    )
