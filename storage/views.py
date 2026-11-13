"""Storage pages: overview, lists, record pages, create/edit forms, actions and CSV downloads.

Views handle transport: role check at entry, form binding, calling storage.services and showing
its errors. Every rule is enforced again by the service.
"""

import uuid

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from accounts.roles import has_role, require_role, staff_role_required
from core.grids import GridRequest, active_filters
from core.models import AuditEvent

from . import exports, lists, queries, recipes, services
from .forms import (
    BlockForm,
    BoxForm,
    ClassForm,
    ContainerTypeForm,
    MoveForm,
    NodeForm,
    RetireForm,
    RoomForm,
    SiteForm,
    TemplateApplyForm,
    TemplateForm,
    UnitForm,
    apply_errors,
)
from .models import (
    Box,
    ContainerType,
    LayoutNode,
    MoveEvent,
    Occupancy,
    Position,
    Room,
    Site,
    StorageClass,
    StorageGate,
    StorageTemplate,
    StorageUnit,
)

READ = services.READ_ROLES
MANAGE = services.MANAGE_ROLES
OPERATE = services.OPERATE_ROLES


def flags(user):
    """Which controls to render. Services and view decorators still enforce every action."""
    return {"can_manage": has_role(user, *MANAGE), "can_operate": has_role(user, *OPERATE)}


def history(obj, limit=50):
    """Audit events of one record, newest first (the template shows them newest last)."""
    return list(
        AuditEvent.objects.filter(object_type=obj._meta.label_lower, object_id=str(obj.pk))
        .select_related("actor")
        .order_by("-occurred_at", "-id")[:limit]
    )


def _status(obj):
    return ("Active", "success") if obj.is_active else ("Retired", "neutral")


# --- lists ----------------------------------------------------------------------------------


def grid_response(request, spec, template, *, list_url, export_url=None, extra=None):
    try:
        grid = GridRequest.parse(request.GET, spec.columns or {"": ""}, spec.default_sort)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    data = request.GET.copy()
    data["q"] = grid.query
    form, queryset = lists.filtered(spec, data)
    page_obj = Paginator(lists.ordered(spec, queryset, grid.sort), grid.per_page).get_page(
        grid.page_number
    )
    chips = active_filters(request, form) if form.is_valid() else []
    cleaned = getattr(form, "cleaned_data", {})
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        template,
        {
            "page_title": spec.title,
            "page_obj": page_obj,
            "filter_form": form,
            "q": grid.query,
            "sort": grid.sort,
            "active_filters": chips,
            "filtered": bool(chips or grid.query),
            "inline": [form[name] for name in spec.inline if name in form.fields],
            "more": [form[name] for name in spec.more if name in form.fields],
            "more_open": any(cleaned.get(name) for name in spec.more),
            "noun": spec.noun,
            "list_url": list_url,
            "export_url": f"{export_url}?{query.urlencode()}" if export_url else None,
            **flags(request.user),
            **(extra or {}),
        },
    )


@staff_role_required(*READ)
def unit_list(request):
    return grid_response(
        request,
        lists.SPECS["unit"],
        "storage/unit_list.html",
        list_url=reverse("storage:unit-list"),
        export_url=reverse("storage:export", args=["unit"]),
    )


@staff_role_required(*READ)
def node_list(request):
    return grid_response(
        request,
        lists.SPECS["node"],
        "storage/node_list.html",
        list_url=reverse("storage:node-list"),
        export_url=reverse("storage:export", args=["node"]),
    )


@staff_role_required(*READ)
def box_list(request):
    return grid_response(
        request,
        lists.SPECS["box"],
        "storage/box_list.html",
        list_url=reverse("storage:box-list"),
        export_url=reverse("storage:export", args=["box"]),
    )


@staff_role_required(*READ)
def free_position_list(request):
    return grid_response(
        request,
        lists.SPECS["position"],
        "storage/free_position_list.html",
        list_url=reverse("storage:free-position-list"),
        export_url=reverse("storage:export", args=["position"]),
    )


@staff_role_required(*READ)
def site_list(request):
    return grid_response(
        request,
        lists.SPECS["site"],
        "storage/site_list.html",
        list_url=reverse("storage:site-list"),
        export_url=reverse("storage:export", args=["site"]),
    )


@staff_role_required(*READ)
def room_list(request):
    return grid_response(
        request,
        lists.SPECS["room"],
        "storage/room_list.html",
        list_url=reverse("storage:room-list"),
        export_url=reverse("storage:export", args=["room"]),
    )


CATALOGUE_URLS = {
    "class": ("storage:class-list", "Storage classes", "class"),
    "container_type": ("storage:container-type-list", "Container types", "container-type"),
    "template": ("storage:template-list", "Storage templates", "template"),
}


def _catalogue_list(request, kind):
    url_name, _, slug = CATALOGUE_URLS[kind]
    return grid_response(
        request,
        lists.CATALOGUE_SPECS[kind],
        f"storage/{kind}_list.html",
        list_url=reverse(url_name),
        export_url=reverse("storage:catalogue-export", args=[slug]),
    )


@staff_role_required(*READ)
def class_list(request):
    return _catalogue_list(request, "class")


@staff_role_required(*READ)
def container_type_list(request):
    return _catalogue_list(request, "container_type")


@staff_role_required(*READ)
def template_list(request):
    return _catalogue_list(request, "template")


EXPORT_KINDS = {"unit", "node", "box", "position", "site", "room"}
CATALOGUE_SLUGS = {"class": "class", "container-type": "container_type", "template": "template"}


def _csv(content, name):
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    response["Cache-Control"] = "no-store"
    return response


@staff_role_required(*READ)
@require_GET
def export(request, kind):
    if kind not in EXPORT_KINDS:
        raise Http404
    params = request.GET.copy()
    sort = params.pop("sort", [""])[-1]
    try:
        content = exports.export_storage(
            actor=request.user, kind=kind, filters=params, sort=sort or "code"
        )
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    return _csv(content, f"storage-{kind}s")


@staff_role_required(*READ)
@require_GET
def catalogue_export(request, kind):
    if kind not in CATALOGUE_SLUGS:
        raise Http404
    params = request.GET.copy()
    sort = params.pop("sort", [""])[-1]
    try:
        content = exports.export_catalogue(
            actor=request.user, kind=CATALOGUE_SLUGS[kind], filters=params, sort=sort or "code"
        )
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    return _csv(content, f"storage-{kind}")


# --- overview -------------------------------------------------------------------------------


@staff_role_required(*READ)
def overview(request):
    units = list(
        lists.units().filter(is_active=True).order_by("room__site__code", "room__code", "code")
    )
    totals = queries.capacity_of(Position.objects.filter(lists.ACTIVE_POSITIONS))
    recent = list(
        MoveEvent.objects.filter(kind__in=("container_move", "correction"))
        .exclude(box__isnull=True, node__isnull=True)
        .select_related("actor", "box", "node")
        .order_by("-recorded_at")[:10]
    )
    return render(
        request,
        "storage/overview.html",
        {
            "page_title": "Storage overview",
            "units": units,
            "totals": totals,
            "box_count": Box.objects.filter(is_active=True).count(),
            "recent_moves": recent,
            **flags(request.user),
        },
    )


# --- shared form page -----------------------------------------------------------------------


def form_page(
    request,
    form,
    *,
    title,
    back_url,
    submit,
    crumbs,
    notes=(),
    meta=(),
    destructive=False,
    status=200,
    nav_item=None,
):
    return render(
        request,
        "storage/form.html",
        {
            "page_title": title,
            "form": form,
            "back_url": back_url,
            "submit_label": submit,
            "crumbs": crumbs,
            "notes": notes,
            "meta": meta,
            "destructive": destructive,
            "nav_item": nav_item,
            "has_times": any(
                getattr(f.widget, "input_type", "") == "datetime-local"
                for f in form.fields.values()
            ),
        },
        status=status,
    )


def _invalid(request, form):
    return 422 if request.method == "POST" and form.errors else 200


def _cleaned(form, *names):
    return {name: form.cleaned_data[name] for name in names if name in form.cleaned_data}


def _edit(request, *, kind, obj, form, values, success_url):
    """Run edit_storage_definition for a bound, valid form."""
    try:
        services.edit_storage_definition(
            actor=request.user,
            kind=kind,
            object_id=obj.pk,
            expected_revision=form.cleaned_data["expected_revision"],
            values=values,
            reason=form.cleaned_data["reason"],
        )
    except ValidationError as exc:
        apply_errors(form, exc)
        return None
    messages.success(request, f"Saved {obj.code}.")
    return redirect(success_url)


# --- sites and rooms ------------------------------------------------------------------------


def _site_crumbs(site=None, *rest):
    trail = [("Sites", reverse("storage:site-list"))]
    if site:
        trail.append((site.code, reverse("storage:site-detail", args=[site.pk])))
    return [*trail, *rest]


@staff_role_required(*READ)
def site_detail(request, pk):
    site = get_object_or_404(Site, pk=pk)
    rooms = lists.rooms().filter(site=site).order_by("code")
    label, tone = _status(site)
    return render(
        request,
        "storage/site_detail.html",
        {
            "page_title": site.code,
            "site": site,
            "rooms": rooms,
            "status_label": label,
            "status_tone": tone,
            "history": history(site),
            **flags(request.user),
        },
    )


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def site_edit(request, pk=None):
    site = get_object_or_404(Site, pk=pk) if pk else None
    form = SiteForm(request.POST or None, instance=site)
    if request.method == "POST" and form.is_valid():
        if site:
            done = _edit(
                request,
                kind="site",
                obj=site,
                form=form,
                values=_cleaned(form, "name", "timezone"),
                success_url=reverse("storage:site-detail", args=[site.pk]),
            )
            if done:
                return done
        else:
            try:
                created = services.create_site(
                    actor=request.user, **_cleaned(form, "code", "name", "timezone")
                )
            except ValidationError as exc:
                apply_errors(form, exc)
            else:
                messages.success(request, f"Created site {created.code}.")
                return redirect("storage:site-detail", pk=created.pk)
    back = reverse("storage:site-detail", args=[site.pk]) if site else reverse("storage:site-list")
    title = f"Edit {site.code}" if site else "New site"
    return form_page(
        request,
        form,
        title=title,
        back_url=back,
        submit="Save changes" if site else "Create site",
        crumbs=_site_crumbs(site, (title, None)),
        status=_invalid(request, form),
    )


def _room_crumbs(room=None, *rest):
    trail = [("Rooms", reverse("storage:room-list"))]
    if room:
        trail.append(
            (f"{room.site.code} / {room.code}", reverse("storage:room-detail", args=[room.pk]))
        )
    return [*trail, *rest]


@staff_role_required(*READ)
def room_detail(request, pk):
    room = get_object_or_404(Room.objects.select_related("site"), pk=pk)
    units = lists.units().filter(room=room).order_by("-is_active", "code")
    capacity = queries.capacity_of(
        Position.objects.filter(lists.ACTIVE_POSITIONS, box__node__unit__room=room)
    )
    label, tone = _status(room)
    return render(
        request,
        "storage/room_detail.html",
        {
            "page_title": f"{room.site.code} / {room.code}",
            "room": room,
            "units": units,
            "capacity": capacity,
            "status_label": label,
            "status_tone": tone,
            "history": history(room),
            **flags(request.user),
        },
    )


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def room_edit(request, pk=None):
    room = get_object_or_404(Room.objects.select_related("site"), pk=pk) if pk else None
    initial = {"site": request.GET.get("site")} if not room and request.GET.get("site") else None
    form = RoomForm(request.POST or None, instance=room, initial=initial)
    if request.method == "POST" and form.is_valid():
        if room:
            done = _edit(
                request,
                kind="room",
                obj=room,
                form=form,
                values=_cleaned(form, "name"),
                success_url=reverse("storage:room-detail", args=[room.pk]),
            )
            if done:
                return done
        else:
            try:
                created = services.create_room(
                    actor=request.user,
                    site_id=form.cleaned_data["site"].pk,
                    **_cleaned(form, "code", "name"),
                )
            except ValidationError as exc:
                apply_errors(form, exc)
            else:
                messages.success(request, f"Created room {created.code}.")
                return redirect("storage:room-detail", pk=created.pk)
    back = reverse("storage:room-detail", args=[room.pk]) if room else reverse("storage:room-list")
    title = f"Edit {room.code}" if room else "New room"
    return form_page(
        request,
        form,
        title=title,
        back_url=back,
        submit="Save changes" if room else "Create room",
        crumbs=_room_crumbs(room, (title, None)),
        status=_invalid(request, form),
    )


# --- units ----------------------------------------------------------------------------------


def _unit_crumbs(unit=None, *rest):
    trail = [("Storage units", reverse("storage:unit-list"))]
    if unit:
        trail.append((unit.code, reverse("storage:unit-detail", args=[unit.pk])))
    return [*trail, *rest]


@staff_role_required(*READ)
def unit_detail(request, pk):
    unit = get_object_or_404(lists.units(), pk=pk)
    nodes = lists.nodes().filter(unit=unit).order_by("path")
    boxes = lists.boxes().filter(node__unit=unit, is_active=True).order_by("node__path", "slot")
    label, tone = _status(unit)
    active_nodes = [n for n in nodes if n.is_active]
    return render(
        request,
        "storage/unit_detail.html",
        {
            "page_title": unit.code,
            "unit": unit,
            "nodes": nodes,
            "boxes": boxes,
            "status_label": label,
            "status_tone": tone,
            "retire_blocked": "Retire or move its racks, towers and shelves first."
            if active_nodes
            else "",
            "history": history(unit),
            **flags(request.user),
        },
    )


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def unit_edit(request, pk=None):
    unit = get_object_or_404(StorageUnit, pk=pk) if pk else None
    initial = {"room": request.GET.get("room")} if not unit and request.GET.get("room") else None
    form = UnitForm(request.POST or None, instance=unit, initial=initial)
    if request.method == "POST" and form.is_valid():
        if unit:
            values = _cleaned(form, "name", "nominal_temperature")
            values["storage_class_id"] = form.cleaned_data["storage_class"].pk
            done = _edit(
                request,
                kind="unit",
                obj=unit,
                form=form,
                values=values,
                success_url=reverse("storage:unit-detail", args=[unit.pk]),
            )
            if done:
                return done
        else:
            data = form.cleaned_data
            try:
                created = services.create_unit(
                    actor=request.user,
                    room_id=data["room"].pk,
                    code=data["code"],
                    name=data["name"],
                    storage_class_id=data["storage_class"].pk,
                    technology=data["technology"],
                    nominal_temperature=data["nominal_temperature"],
                )
            except ValidationError as exc:
                apply_errors(form, exc)
            else:
                messages.success(request, f"Created storage unit {created.code}.")
                return redirect("storage:unit-detail", pk=created.pk)
    back = reverse("storage:unit-detail", args=[unit.pk]) if unit else reverse("storage:unit-list")
    title = f"Edit {unit.code}" if unit else "New storage unit"
    return form_page(
        request,
        form,
        title=title,
        back_url=back,
        submit="Save changes" if unit else "Create storage unit",
        crumbs=_unit_crumbs(unit, (title, None)),
        notes=["Nominal temperature is the unit's stated set point; it is not a measurement."],
        status=_invalid(request, form),
    )


# --- nodes ----------------------------------------------------------------------------------


def _node_crumbs(node=None, *rest):
    trail = [("Racks and shelves", reverse("storage:node-list"))]
    if node:
        trail.append((node.code, reverse("storage:node-detail", args=[node.pk])))
    return [*trail, *rest]


def _slots(capacity, used):
    """[(slot, occupant or None)] for 1..capacity, plus occupants above capacity."""
    by_slot = {item.slot: item for item in used}
    return [(slot, by_slot.get(slot)) for slot in range(1, capacity + 1)]


@staff_role_required(*READ)
def node_detail(request, pk):
    node = get_object_or_404(lists.nodes().select_related("unit__room__site"), pk=pk)
    chain = services.node_chain(node)
    children = list(lists.nodes().filter(parent=node, is_active=True).order_by("slot"))
    node_boxes = list(lists.boxes().filter(node=node, is_active=True).order_by("slot"))
    label, tone = _status(node)
    retire_blocked = ""
    if children or node_boxes:
        retire_blocked = "Retire or move what it holds first."
    return render(
        request,
        "storage/node_detail.html",
        {
            "page_title": node.code,
            "node": node,
            "chain": chain,
            "slots": _slots(node.child_slots, children) if node.child_slots else [],
            "box_slots": _slots(node.box_slots, node_boxes) if node.box_slots else [],
            "status_label": label,
            "status_tone": tone,
            "retire_blocked": retire_blocked,
            "correctable": correctable_move(node),
            "moves": MoveEvent.objects.filter(node=node)
            .select_related("actor", "corrects")
            .order_by("-occurred_at", "-recorded_at")[:20],
            "history": history(node),
            **flags(request.user),
        },
    )


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def node_edit(request, pk=None):
    node = get_object_or_404(LayoutNode, pk=pk) if pk else None
    initial = {}
    if not node:
        parent = None
        if _uuid(request.GET.get("parent")):
            parent = LayoutNode.objects.filter(pk=request.GET["parent"]).first()
        if parent:
            initial = {
                "parent": parent.pk,
                "unit": parent.unit_id,
                "kind": sorted(services.CHILD_KINDS[parent.kind])[0],
            }
        elif _uuid(request.GET.get("unit")):
            initial = {"unit": request.GET["unit"]}
    form = NodeForm(request.POST or None, instance=node, initial=initial or None)
    if request.method == "POST" and form.is_valid():
        if node:
            done = _edit(
                request,
                kind="node",
                obj=node,
                form=form,
                values=_cleaned(form, "name"),
                success_url=reverse("storage:node-detail", args=[node.pk]),
            )
            if done:
                return done
        else:
            data = form.cleaned_data
            try:
                created = services.create_node(
                    actor=request.user,
                    unit_id=data["unit"].pk,
                    parent_id=data["parent"].pk if data.get("parent") else None,
                    code=data["code"],
                    name=data["name"],
                    kind=data["kind"],
                    slot=data["slot"],
                    child_slots=data["child_slots"],
                    box_slots=data["box_slots"],
                )
            except ValidationError as exc:
                apply_errors(form, exc)
            else:
                messages.success(
                    request, f"Created {created.get_kind_display().lower()} {created.code}."
                )
                return redirect("storage:node-detail", pk=created.pk)
    back = reverse("storage:node-detail", args=[node.pk]) if node else reverse("storage:node-list")
    title = f"Edit {node.code}" if node else "New rack or shelf"
    return form_page(
        request,
        form,
        title=title,
        back_url=back,
        submit="Save changes" if node else "Create rack or shelf",
        crumbs=_node_crumbs(node, (title, None)),
        status=_invalid(request, form),
    )


def _uuid(value):
    try:
        uuid.UUID(str(value))
    except TypeError, ValueError:
        return False
    return True


# --- boxes ----------------------------------------------------------------------------------


def _box_crumbs(box=None, *rest):
    trail = [("Boxes", reverse("storage:box-list"))]
    if box:
        trail.append((box.code, reverse("storage:box-detail", args=[box.pk])))
    return [*trail, *rest]


@staff_role_required(*READ)
def box_detail(request, pk):
    box = get_object_or_404(lists.boxes().select_related("node__unit__room__site"), pk=pk)
    chain = services.node_chain(box.node)
    positions = list(Position.objects.filter(box=box).order_by("row", "column"))
    claims = {
        o.position_id: o
        for o in Occupancy.objects.filter(position__box=box).only("id", "position_id", "revision")
    }
    rows = []
    for position in positions:
        position.claim = claims.get(position.pk)
        if not rows or rows[-1][0] != position.row:
            rows.append((position.row, []))
        rows[-1][1].append(position)
    label, tone = _status(box)
    return render(
        request,
        "storage/box_detail.html",
        {
            "page_title": box.code,
            "box": box,
            "chain": chain,
            "grid_rows": rows,
            "columns": range(1, box.container_type.columns + 1),
            "occupied": [p for p in positions if p.claim],
            "status_label": label,
            "status_tone": tone,
            "retire_blocked": "Move or dispose of the items in it first." if claims else "",
            "correctable": correctable_move(box),
            "moves": MoveEvent.objects.filter(box=box)
            .select_related("actor", "corrects")
            .order_by("-occurred_at", "-recorded_at")[:20],
            "history": history(box),
            **flags(request.user),
        },
    )


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def box_edit(request, pk=None):
    box = get_object_or_404(Box, pk=pk) if pk else None
    initial = {}
    if not box and _uuid(request.GET.get("node")):
        node = LayoutNode.objects.filter(pk=request.GET["node"], box_slots__gt=0).first()
        if node:
            used = set(Box.objects.filter(node=node, is_active=True).values_list("slot", flat=True))
            free = next((s for s in range(1, node.box_slots + 1) if s not in used), None)
            initial = {"node": node.pk, "slot": free}
    form = BoxForm(request.POST or None, instance=box, initial=initial or None)
    if request.method == "POST" and form.is_valid():
        if box:
            done = _edit(
                request,
                kind="box",
                obj=box,
                form=form,
                values=_cleaned(form, "name"),
                success_url=reverse("storage:box-detail", args=[box.pk]),
            )
            if done:
                return done
        else:
            data = form.cleaned_data
            try:
                created = services.create_box(
                    actor=request.user,
                    node_id=data["node"].pk,
                    code=data["code"],
                    name=data["name"],
                    slot=data["slot"],
                    container_type_id=data["container_type"].pk,
                )
            except ValidationError as exc:
                apply_errors(form, exc)
            else:
                messages.success(
                    request,
                    f"Created box {created.code} with {created.container_type.capacity} positions.",
                )
                return redirect("storage:box-detail", pk=created.pk)
    back = reverse("storage:box-detail", args=[box.pk]) if box else reverse("storage:box-list")
    title = f"Edit {box.code}" if box else "New box"
    return form_page(
        request,
        form,
        title=title,
        back_url=back,
        submit="Save changes" if box else "Create box",
        crumbs=_box_crumbs(box, (title, None)),
        notes=[] if box else ["Confirm the physical box matches the container type's grid."],
        status=_invalid(request, form),
    )


# --- catalogues -----------------------------------------------------------------------------


def _catalogue_crumbs(kind, obj=None, *rest):
    url_name, tab, slug = CATALOGUE_URLS[kind]
    trail = [("Storage catalogues", None), (tab, reverse(url_name))]
    if obj:
        trail.append(
            (
                str(obj) if kind != "class" else obj.code,
                reverse(f"storage:{slug}-detail", args=[obj.pk]),
            )
        )
    return [*trail, *rest]


def _catalogue_save(request, kind, obj, form, values, success):
    try:
        saved = services.save_storage_catalogue(
            actor=request.user,
            kind=kind,
            object_id=obj.pk if obj else None,
            expected_revision=form.cleaned_data.get("expected_revision"),
            values=values,
            reason=form.cleaned_data.get("reason", ""),
        )
    except ValidationError as exc:
        apply_errors(form, exc)
        return None
    messages.success(request, success.format(obj=saved))
    return saved


@staff_role_required(*READ)
def class_detail(request, pk):
    obj = get_object_or_404(lists.classes(), pk=pk)
    return render(
        request,
        "storage/class_detail.html",
        {
            "page_title": obj.code,
            "obj": obj,
            "crumbs": _catalogue_crumbs("class", None, (obj.code, None)),
            "units": StorageUnit.objects.filter(storage_class=obj)
            .select_related("room__site")
            .order_by("code")[:100],
            "history": history(obj),
            **flags(request.user),
        },
    )


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def class_edit(request, pk=None):
    obj = get_object_or_404(StorageClass, pk=pk) if pk else None
    form = ClassForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        names = ("name", "description", "is_active") if obj else ("code", "name", "description")
        saved = _catalogue_save(
            request, "class", obj, form, _cleaned(form, *names), "Saved storage class {obj.code}."
        )
        if saved:
            return redirect("storage:class-detail", pk=saved.pk)
    back = reverse("storage:class-detail", args=[obj.pk]) if obj else reverse("storage:class-list")
    title = f"Edit {obj.code}" if obj else "New storage class"
    return form_page(
        request,
        form,
        title=title,
        back_url=back,
        submit="Save changes" if obj else "Create storage class",
        crumbs=_catalogue_crumbs("class", obj, (title, None)),
        status=_invalid(request, form),
    )


@staff_role_required(*READ)
def container_type_detail(request, pk):
    obj = get_object_or_404(lists.container_types(), pk=pk)
    preview_rows = [
        (services.row_letters(row) if obj.label_scheme == "alpha_numeric" else str(row), cells)
        for row, cells in _preview(obj)
    ]
    return render(
        request,
        "storage/container_type_detail.html",
        {
            "page_title": str(obj),
            "obj": obj,
            "crumbs": _catalogue_crumbs("container_type", None, (str(obj), None)),
            "versions": ContainerType.objects.filter(code=obj.code).order_by("version"),
            "used": obj.boxes.exists(),
            "preview_rows": preview_rows,
            "preview_columns": range(1, min(obj.columns, 12) + 1),
            "preview_cut": obj.rows > 12 or obj.columns > 12,
            "history": history(obj),
            **flags(request.user),
        },
    )


def _preview(grid):
    """Labels of the top-left 12 x 12 corner of a grid, as rows of (row, [labels])."""
    cells = {(r, c): label for r, c, _, label in services.grid_cells(grid) if r <= 12 and c <= 12}
    return [
        (r, [cells[(r, c)] for c in range(1, min(grid.columns, 12) + 1)])
        for r in range(1, min(grid.rows, 12) + 1)
    ]


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def container_type_edit(request, pk=None):
    obj = get_object_or_404(ContainerType, pk=pk) if pk else None
    used = bool(obj and obj.boxes.exists())
    initial = None
    source = request.GET.get("from")
    if not obj and _uuid(source):
        base = ContainerType.objects.filter(pk=source).first()
        if base:
            initial = {
                k: getattr(base, k)
                for k in ("code", "name", "rows", "columns", "label_scheme", "fill_order")
            }
    form = ContainerTypeForm(request.POST or None, instance=obj, used=used, initial=initial)
    if request.method == "POST" and form.is_valid():
        if obj:
            names = ("name", "is_active", "rows", "columns", "label_scheme", "fill_order")
        else:
            names = ("code", "name", "rows", "columns", "label_scheme", "fill_order")
        saved = _catalogue_save(
            request,
            "container_type",
            obj,
            form,
            _cleaned(form, *names),
            "Saved container type {obj}.",
        )
        if saved:
            return redirect("storage:container-type-detail", pk=saved.pk)
    back = (
        reverse("storage:container-type-detail", args=[obj.pk])
        if obj
        else reverse("storage:container-type-list")
    )
    title = f"Edit {obj}" if obj else "New container type"
    notes = []
    if used:
        notes.append(
            "Boxes use this version, so its grid is fixed. Add a new version to change it."
        )
    elif not obj:
        notes.append(
            "Using an existing code adds the next version; earlier versions and their boxes "
            "are unchanged."
        )
    return form_page(
        request,
        form,
        title=title,
        back_url=back,
        submit="Save changes" if obj else "Create container type",
        crumbs=_catalogue_crumbs("container_type", obj, (title, None)),
        notes=notes,
        status=_invalid(request, form),
    )


@staff_role_required(*READ)
def template_detail(request, pk):
    obj = get_object_or_404(lists.templates(), pk=pk)
    return render(
        request,
        "storage/template_detail.html",
        {
            "page_title": str(obj),
            "obj": obj,
            "crumbs": _catalogue_crumbs("template", None, (str(obj), None)),
            "outline": list(_outline(obj.definition.get("nodes", []))),
            "technology": dict(StorageUnit.Technology.choices).get(
                obj.definition.get("technology"), obj.definition.get("technology")
            ),
            "storage_class": StorageClass.objects.filter(
                code=obj.definition.get("storage_class")
            ).first(),
            "units": obj.units.select_related("room__site").order_by("code"),
            "versions": StorageTemplate.objects.filter(code=obj.code).order_by("version"),
            "history": history(obj),
            **flags(request.user),
        },
    )


def _outline(items, depth=0):
    for item in items:
        yield depth, item, item.get("boxes", [])
        yield from _outline(item.get("children", []), depth + 1)


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def template_edit(request, pk=None):
    obj = get_object_or_404(StorageTemplate, pk=pk) if pk else None
    used = bool(obj and obj.units.exists())
    initial = None
    if not obj:
        if _uuid(request.GET.get("unit")):
            unit = (
                StorageUnit.objects.filter(pk=request.GET["unit"])
                .select_related("storage_class")
                .first()
            )
            if unit:
                initial = {
                    "name": f"Layout of {unit.code}",
                    "definition": recipes.recipe_from_unit(unit),
                }
        elif _uuid(request.GET.get("from")):
            base = StorageTemplate.objects.filter(pk=request.GET["from"]).first()
            if base:
                initial = {"code": base.code, "name": base.name, "definition": base.definition}
    form = TemplateForm(request.POST or None, instance=obj, used=used, initial=initial)
    if request.method == "POST" and form.is_valid():
        names = ("name", "is_active", "definition") if obj else ("code", "name", "definition")
        saved = _catalogue_save(
            request, "template", obj, form, _cleaned(form, *names), "Saved template {obj}."
        )
        if saved:
            return redirect("storage:template-detail", pk=saved.pk)
    back = (
        reverse("storage:template-detail", args=[obj.pk])
        if obj
        else reverse("storage:template-list")
    )
    title = f"Edit {obj}" if obj else "New storage template"
    notes = []
    if used:
        notes.append(
            "Units were created from this version, so its layout is fixed. Add a new version "
            "to change it."
        )
    return form_page(
        request,
        form,
        title=title,
        back_url=back,
        submit="Save changes" if obj else "Create storage template",
        crumbs=_catalogue_crumbs("template", obj, (title, None)),
        notes=notes,
        status=_invalid(request, form),
    )


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def template_apply(request, pk):
    template = get_object_or_404(StorageTemplate, pk=pk)
    form = TemplateApplyForm(request.POST or None)
    if not template.is_active:
        messages.error(request, "This template is inactive and cannot be applied.")
        return redirect("storage:template-detail", pk=template.pk)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            unit = services.instantiate_template(
                actor=request.user,
                template_id=template.pk,
                room_id=data["room"].pk,
                unit_code=data["unit_code"],
                unit_name=data["unit_name"],
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, f"Created storage unit {unit.code} from {template}.")
            return redirect("storage:unit-detail", pk=unit.pk)
    detail = reverse("storage:template-detail", args=[template.pk])
    return form_page(
        request,
        form,
        title="Apply template",
        back_url=detail,
        submit="Apply template",
        crumbs=_catalogue_crumbs("template", template, ("Apply template", None)),
        meta=[str(template), template.name],
        notes=[
            "Creates the unit, its racks and shelves, and every box with all its positions, "
            "in one step. Nothing is created if any part fails."
        ],
        status=_invalid(request, form),
    )


# --- actions --------------------------------------------------------------------------------

KIND_MODELS = {"unit": StorageUnit, "node": LayoutNode, "box": Box}
# Sidebar item for the shared action routes (core.templatetags.navigation.SHARED_ROUTES).
NAV_ITEMS = {"unit": "storage-units", "node": "storage-nodes", "box": "storage-boxes"}


def _record_url(kind, obj):
    return reverse(f"storage:{kind}-detail", args=[obj.pk])


def _record_crumbs(kind, obj, action):
    if kind == "box":
        return _box_crumbs(obj, (action, None))
    if kind == "node":
        return _node_crumbs(obj, (action, None))
    return _unit_crumbs(obj, (action, None))


@staff_role_required(*OPERATE)
@require_http_methods(["GET", "POST"])
def container_move(request, kind, pk):
    if kind not in ("box", "node"):
        raise Http404
    obj = get_object_or_404(KIND_MODELS[kind], pk=pk)
    noun = "box" if kind == "box" else obj.get_kind_display().lower()
    detail = _record_url(kind, obj)
    if not obj.is_active:
        messages.error(request, f"{obj.code} is retired and cannot move.")
        return redirect(detail)
    corrects = None
    if request.GET.get("corrects"):
        # Corrections are managers' and admins' (the service enforces this too).
        require_role(request.user, *MANAGE)
        corrects = correctable_move(obj) if _uuid(request.GET["corrects"]) else None
        if corrects is None or str(corrects.pk) != request.GET["corrects"]:
            messages.error(
                request,
                "Only the latest move of this container, not yet corrected, can be corrected.",
            )
            return redirect(detail)
    action = "Correct move" if corrects else f"Move {noun}"
    gate = StorageGate.objects.filter(pk=1).values_list("revision", flat=True).first() or 1
    form = MoveForm(request.POST or None, kind=kind, obj=obj, gate_revision=gate)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        target_kind, target_id = data["target"]
        try:
            event = services.move_container(
                actor=request.user,
                kind=kind,
                object_id=obj.pk,
                expected_revision=data["expected_revision"],
                expected_gate_revision=data["expected_gate_revision"],
                target_kind=target_kind,
                target_id=target_id,
                target_slot=data["target_slot"],
                occurred_at=data["occurred_at"],
                reason=data["reason"],
                corrects_id=corrects.pk if corrects else None,
            )
        except ValidationError as exc:
            apply_errors(form, exc)
            if any(e.code == "stale_revision" for e in _errors(exc)):
                form.data = form.data.copy()
                form.data["expected_gate_revision"] = gate
                form.data["expected_revision"] = KIND_MODELS[kind].objects.get(pk=obj.pk).revision
        else:
            count = event.members.count()
            messages.success(
                request,
                f"Moved {obj.code} to {event.to_path['display']}, slot "
                f"{event.to_path.get('slot')}, with {count} stored "
                f"item{'s' if count != 1 else ''}.",
            )
            return redirect(detail)
    capacity = queries.capacity_of(queries.scope_positions(kind, obj.pk))
    location = services.container_location(obj)
    return form_page(
        request,
        form,
        title=action,
        back_url=detail,
        submit=action,
        crumbs=_record_crumbs(kind, obj, action),
        nav_item=NAV_ITEMS[kind],
        meta=[f"Now at {location['display']}, slot {location['slot']}"],
        notes=[
            *(
                [
                    f"Corrects the move recorded {corrects.occurred_at:%Y-%m-%d %H:%M} to "
                    f"{corrects.to_path['display']}, slot {corrects.to_path.get('slot')}. Give "
                    "where it really is now. The wrong record stays in the history, linked to "
                    "this correction."
                ]
                if corrects
                else []
            ),
            f"Everything inside moves with it in one recorded event: {capacity.occupied} stored "
            f"item{'s' if capacity.occupied != 1 else ''}. Positions and their labels do not "
            "change.",
            "A move is a location update, not a record of time out of storage. Use check-out for "
            "material that leaves storage.",
        ],
        status=_invalid(request, form),
    )


def correctable_move(obj):
    """The container's latest move if it has not been corrected yet, else None."""
    field = "box" if isinstance(obj, Box) else "node"
    latest = (
        MoveEvent.objects.filter(**{field: obj})
        .order_by("-occurred_at", "-recorded_at", "-id")
        .select_related("corrected_by")
        .first()
    )
    if latest is None or hasattr(latest, "corrected_by"):
        return None
    return latest


def _errors(exc):
    if hasattr(exc, "error_dict"):
        return [e for errors in exc.error_dict.values() for e in errors]
    return exc.error_list


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def position_block(request, pk):
    position = get_object_or_404(Position.objects.select_related("box"), pk=pk)
    box = position.box
    action = "Unblock position" if position.blocked else "Block position"
    detail = reverse("storage:box-detail", args=[box.pk])
    form = BlockForm(request.POST or None, initial={"expected_revision": position.revision})
    if request.method == "POST" and form.is_valid():
        try:
            services.set_position_blocked(
                actor=request.user,
                position_id=position.pk,
                expected_revision=form.cleaned_data["expected_revision"],
                blocked=not position.blocked,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            state = "blocked" if not position.blocked else "unblocked"
            messages.success(request, f"Position {box.code} {position.label} {state}.")
            return redirect(detail)
    notes = (
        [f"Blocked because: {position.block_reason}"]
        if position.blocked
        else [
            "A blocked position is never offered as free. An occupied position cannot be blocked."
        ]
    )
    return form_page(
        request,
        form,
        title=action,
        back_url=detail,
        submit=action,
        crumbs=_box_crumbs(box, (f"{position.label}: {action}", None)),
        meta=[f"{box.code} {position.label}"],
        notes=notes,
        status=_invalid(request, form),
    )


@staff_role_required(*MANAGE)
@require_http_methods(["GET", "POST"])
def retire(request, kind, pk):
    if kind not in KIND_MODELS:
        raise Http404
    obj = get_object_or_404(KIND_MODELS[kind], pk=pk)
    noun = {"unit": "storage unit", "box": "box"}.get(kind) or obj.get_kind_display().lower()
    action = f"Retire {noun}"
    detail = _record_url(kind, obj)
    if not obj.is_active:
        messages.info(request, f"{obj.code} is already retired.")
        return redirect(detail)
    form = RetireForm(request.POST or None, initial={"expected_revision": obj.revision})
    if request.method == "POST" and form.is_valid():
        try:
            services.retire_storage(
                actor=request.user,
                kind=kind,
                object_id=obj.pk,
                expected_revision=form.cleaned_data["expected_revision"],
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            apply_errors(form, exc)
        else:
            messages.success(request, f"Retired {obj.code}. Its record, code and history stay.")
            return redirect(detail)
    return form_page(
        request,
        form,
        title=action,
        back_url=detail,
        submit=action,
        crumbs=_record_crumbs(kind, obj, action),
        nav_item=NAV_ITEMS[kind],
        meta=[obj.code, obj.name],
        notes=[
            "Retiring takes it out of service: it is no longer offered for storage or moves, and "
            "it cannot be reactivated. Nothing is deleted or disposed of.",
            "The box must be empty."
            if kind == "box"
            else "It must hold no active racks, shelves or boxes; retire or move those first.",
        ],
        destructive=True,
        status=_invalid(request, form),
    )
