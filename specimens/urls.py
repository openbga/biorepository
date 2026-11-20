from django.urls import path

from . import action_views as actions
from . import views

app_name = "specimens"

CATALOGUE_ROUTES = (
    # (URL segment, route prefix, kind)
    ("participants", "participant", "participant"),
    ("collections", "collection", "collection"),
    ("specimen-types", "specimen-type", "specimen_type"),
    ("container-types", "container-type", "container_type"),
    ("units", "unit", "unit"),
)

urlpatterns = [
    # Lists, records, documents and downloads (views)
    path("", views.specimen_list, name="specimen-list"),
    path("lookup/", views.lookup, name="lookup"),
    path("export/", views.export, name="export"),
    path("labels/", views.label_preview, name="label-preview"),
    path("labels/pdf/", views.label_pdf, name="label-pdf"),
    path("<uuid:pk>/", views.specimen_detail, name="specimen-detail"),
    path("disposals/", views.disposal_list, name="disposal-list"),
    path("disposals/export/", views.disposal_export, name="disposal-export"),
    path("disposals/<uuid:pk>/", views.disposal_detail, name="disposal-detail"),
    path("imports/", views.import_list, name="import-list"),
    path("imports/export/", views.import_export, name="import-export"),
    path("imports/upload/", views.import_upload, name="import-upload"),
    path("imports/template.csv", views.import_template, name="import-template"),
    path("imports/<uuid:pk>/", views.import_detail, name="import-detail"),
    path("imports/<uuid:pk>/commit/", views.import_commit, name="import-commit"),
    path(
        "imports/<uuid:pk>/labels/<int:group>/",
        views.import_label_preview,
        name="import-label-preview",
    ),
    path(
        "imports/<uuid:pk>/labels/<int:group>.pdf",
        views.import_label_pdf,
        name="import-label-pdf",
    ),
    path("catalogues/<slug:kind>/export/", views.catalogue_export, name="catalogue-export"),
    # Action forms and the entry grid (action_views)
    path("register/", actions.register, name="specimen-create"),
    path("<uuid:pk>/place/", actions.place, name="place"),
    path("<uuid:pk>/relocate/", actions.relocate, name="relocate"),
    path("<uuid:pk>/check-out/", actions.check_out, name="check-out"),
    path("<uuid:pk>/check-in/", actions.check_in, name="check-in"),
    path("<uuid:pk>/transfer/", actions.transfer, name="transfer"),
    path("<uuid:pk>/consume/", actions.consume, name="consume"),
    path("<uuid:pk>/adjust/", actions.adjust, name="adjust"),
    path("<uuid:pk>/derive/", actions.derive, name="derive"),
    path("<uuid:pk>/hold/", actions.hold_add, name="hold-add"),
    path("<uuid:pk>/thaw/", actions.thaw_record, name="thaw-record"),
    path("<uuid:pk>/correct/", actions.correct, name="correct"),
    path("<uuid:pk>/disposal/", actions.disposal_request, name="disposal-request"),
    path("ledger/<uuid:pk>/reverse/", actions.reverse, name="reverse"),
    path("holds/<uuid:pk>/release/", actions.hold_release, name="hold-release"),
    path("custody/<uuid:pk>/correct/", actions.custody_correct, name="custody-correct"),
    path("thaw/<uuid:pk>/correct/", actions.thaw_correct, name="thaw-correct"),
    path("disposals/<uuid:pk>/decide/", actions.disposal_decide, name="disposal-decide"),
    path("disposals/<uuid:pk>/execute/", actions.disposal_execute, name="disposal-execute"),
]

for segment, prefix, kind in CATALOGUE_ROUTES:
    urlpatterns += [
        path(f"{segment}/", views.catalogue_list, {"kind": kind}, name=f"{prefix}-list"),
        path(f"{segment}/new/", views.catalogue_edit, {"kind": kind}, name=f"{prefix}-create"),
        path(
            f"{segment}/<uuid:pk>/edit/",
            views.catalogue_edit,
            {"kind": kind},
            name=f"{prefix}-edit",
        ),
    ]
