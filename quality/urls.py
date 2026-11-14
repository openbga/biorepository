from django.urls import path

from . import views

app_name = "quality"
urlpatterns = [
    path("audits/", views.audit_list, name="audit-list"),
    path("audits/new/", views.audit_create, name="audit-create"),
    path("audits/<int:pk>/", views.audit_detail, name="audit-detail"),
    path("audits/<int:pk>/complete/", views.audit_complete, name="audit-complete"),
    path("audits/<int:pk>/cancel/", views.audit_cancel, name="audit-cancel"),
]
