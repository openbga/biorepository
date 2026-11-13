from django.urls import path

from . import action_views as actions
from . import views

app_name = "equipment"

urlpatterns = [
    path("", views.equipment_list, name="equipment-list"),
    path("new/", actions.equipment_edit, name="equipment-create"),
    path("<uuid:pk>/", views.equipment_detail, name="equipment-detail"),
    path("<uuid:pk>/edit/", actions.equipment_edit, name="equipment-edit"),
    path("<uuid:pk>/status/", actions.status_change, name="status-change"),
    path("<uuid:pk>/service/new/", actions.service_create, name="service-create"),
    path("service/<uuid:pk>/correct/", actions.service_correct, name="service-correct"),
    path("due/", views.due_list, name="due-list"),
    path("ln2/", views.ln2_list, name="ln2-list"),
    path("ln2/new/", actions.ln2_create, name="ln2-create"),
    path("ln2/<uuid:pk>/correct/", actions.ln2_correct, name="ln2-correct"),
    path("readings/", views.reading_list, name="reading-list"),
    path("readings/new/", actions.reading_create, name="reading-create"),
    path("readings/<uuid:pk>/correct/", actions.reading_correct, name="reading-correct"),
    path("readings/imports/", views.import_list, name="import-list"),
    path("readings/imports/upload/", actions.import_upload, name="import-upload"),
    path("readings/imports/<uuid:pk>/", views.import_detail, name="import-detail"),
    path("excursions/", views.excursion_list, name="excursion-list"),
    path("excursions/new/", actions.excursion_create, name="excursion-create"),
    path("excursions/<int:pk>/", views.excursion_detail, name="excursion-detail"),
    path("excursions/<int:pk>/edit/", actions.excursion_edit, name="excursion-edit"),
    path("excursions/<int:pk>/submit/", actions.excursion_submit, name="excursion-submit"),
    path("excursions/<int:pk>/review/", actions.excursion_review, name="excursion-review"),
    path("excursions/<int:pk>/reopen/", actions.excursion_reopen, name="excursion-reopen"),
    path("excursions/<int:pk>/note/", actions.excursion_note, name="excursion-note"),
    path("attachment/<str:kind>/<uuid:pk>/", views.attachment, name="attachment"),
    path("export/<str:kind>/", views.export, name="export"),
]
