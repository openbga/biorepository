from django.urls import path

from . import deviation_views, training_views, views

app_name = "quality"
urlpatterns = [
    path("training/", training_views.training_list, name="training-list"),
    path("training/export/", training_views.training_export, name="training-export"),
    path("training/new/", training_views.training_create, name="training-create"),
    path("training/<int:pk>/", training_views.training_detail, name="training-detail"),
    path("training/<int:pk>/edit/", training_views.training_edit, name="training-edit"),
    path("training/<int:pk>/complete/", training_views.training_complete, name="training-complete"),
    path("training/<int:pk>/correct/", training_views.training_correct, name="training-correct"),
    path("training/<int:pk>/cancel/", training_views.training_cancel, name="training-cancel"),
    path("deviations/", deviation_views.deviation_list, name="deviation-list"),
    path("deviations/new/", deviation_views.deviation_create, name="deviation-create"),
    path("deviations/<int:pk>/", deviation_views.deviation_detail, name="deviation-detail"),
    path("deviations/<int:pk>/edit/", deviation_views.deviation_edit, name="deviation-edit"),
    path("deviations/<int:pk>/submit/", deviation_views.deviation_submit, name="deviation-submit"),
    path("deviations/<int:pk>/review/", deviation_views.deviation_review, name="deviation-review"),
    path("deviations/<int:pk>/reopen/", deviation_views.deviation_reopen, name="deviation-reopen"),
    path("audits/", views.audit_list, name="audit-list"),
    path("audits/new/", views.audit_create, name="audit-create"),
    path("audits/<int:pk>/", views.audit_detail, name="audit-detail"),
    path("audits/<int:pk>/complete/", views.audit_complete, name="audit-complete"),
    path("audits/<int:pk>/cancel/", views.audit_cancel, name="audit-cancel"),
]
