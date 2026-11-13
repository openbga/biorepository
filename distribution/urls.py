from django.urls import path

from . import governance_views as governance
from . import request_views as requests
from . import views

app_name = "distribution"

urlpatterns = [
    # Requests, allocation and picking (request_views)
    path("requests/", requests.request_list, name="request-list"),
    path("requests/new/", requests.request_edit, name="request-create"),
    path("requests/<int:pk>/", requests.request_detail, name="request-detail"),
    path("requests/<int:pk>/edit/", requests.request_edit, name="request-edit"),
    path("requests/<int:pk>/lines/", requests.request_items, name="request-items"),
    path("requests/<int:pk>/submit/", requests.request_submit, name="request-submit"),
    path("requests/<int:pk>/decide/", requests.request_decide, name="request-decide"),
    path("requests/<int:pk>/reopen/", requests.request_reopen, name="request-reopen"),
    path("requests/<int:pk>/cancel/", requests.request_cancel, name="request-cancel"),
    path("requests/<int:pk>/close/", requests.request_close, name="request-close"),
    path("requests/<int:pk>/agreement/", requests.request_agreement, name="request-agreement"),
    path("requests/<int:pk>/note/", requests.request_note, name="request-note"),
    path("requests/<int:pk>/lines/<uuid:item>/allocate/", requests.allocate, name="allocate"),
    path("requests/<int:pk>/release/", requests.allocation_release, name="allocation-release"),
    path("requests/<int:pk>/pick/", requests.pick, name="pick"),
    path("requests/<int:pk>/put-back/", requests.put_back, name="put-back"),
    path("requests/<int:pk>/pick-list/", requests.pick_list, name="pick-list"),
    path("requests/<int:pk>/pick-list.pdf", requests.pick_list_pdf, name="pick-list-pdf"),
    # Shipments, receipt and returns (request_views)
    path("shipments/", requests.shipment_list, name="shipment-list"),
    path("requests/<int:pk>/shipments/new/", requests.shipment_create, name="shipment-create"),
    path("shipments/<int:pk>/", requests.shipment_detail, name="shipment-detail"),
    path("shipments/<int:pk>/edit/", requests.shipment_edit, name="shipment-edit"),
    path("shipments/<int:pk>/contents/", requests.shipment_contents, name="shipment-contents"),
    path("shipments/<int:pk>/pack/", requests.shipment_pack, name="shipment-pack"),
    path("shipments/<int:pk>/unpack/", requests.shipment_unpack, name="shipment-unpack"),
    path("shipments/<int:pk>/notify/", requests.shipment_notify, name="shipment-notify"),
    path("shipments/<int:pk>/dispatch/", requests.shipment_dispatch, name="shipment-dispatch"),
    path("shipments/<int:pk>/receipt/", requests.shipment_receipt, name="shipment-receipt"),
    path("receipts/<uuid:pk>/correct/", requests.receipt_correct, name="receipt-correct"),
    path("shipments/<int:pk>/cancel/", requests.shipment_cancel, name="shipment-cancel"),
    path("shipments/<int:pk>/note/", requests.shipment_note, name="shipment-note"),
    path("shipments/<int:pk>/manifest/", requests.manifest, name="manifest"),
    path("shipments/<int:pk>/manifest.pdf", requests.manifest_pdf, name="manifest-pdf"),
    path("returns/", requests.return_list, name="return-list"),
    path("shipment-items/<uuid:pk>/return/", requests.return_record, name="return-record"),
    path("returns/<uuid:pk>/review/", requests.return_review, name="return-review"),
    # Studies, consent, withdrawals (governance_views)
    path("studies/", governance.study_list, name="study-list"),
    path("studies/new/", governance.study_edit, name="study-create"),
    path("studies/<uuid:pk>/", governance.study_detail, name="study-detail"),
    path("studies/<uuid:pk>/edit/", governance.study_edit, name="study-edit"),
    path(
        "studies/<uuid:pk>/collections/link/",
        governance.study_collection_link,
        name="study-collection-link",
    ),
    path(
        "study-collections/<uuid:pk>/end/",
        governance.study_collection_end,
        name="study-collection-end",
    ),
    path("consents/", governance.consent_list, name="consent-list"),
    path("consents/new/", governance.consent_create, name="consent-create"),
    path("consents/<uuid:pk>/", governance.consent_detail, name="consent-detail"),
    path("consents/<uuid:pk>/correct/", governance.consent_correct, name="consent-correct"),
    path("consents/<uuid:pk>/withdraw/", governance.consent_withdraw, name="consent-withdraw"),
    path("consents/<uuid:pk>/hold/", governance.consent_hold, name="consent-hold"),
    path("consents/<uuid:pk>/disposals/", governance.consent_disposals, name="consent-disposals"),
    path("consents/<uuid:pk>/reinstate/", governance.consent_reinstate, name="consent-reinstate"),
    path("withdrawals/", governance.withdrawal_list, name="withdrawal-list"),
    path(
        "withdrawals/<uuid:pk>/follow-up/",
        governance.withdrawal_follow_up,
        name="withdrawal-follow-up",
    ),
    # Agreements (governance_views)
    path("agreements/", governance.agreement_list, name="agreement-list"),
    path("agreements/new/", governance.agreement_edit, name="agreement-create"),
    path("agreements/<int:pk>/", governance.agreement_detail, name="agreement-detail"),
    path("agreements/<int:pk>/edit/", governance.agreement_edit, name="agreement-edit"),
    path("agreements/<int:pk>/execute/", governance.agreement_execute, name="agreement-execute"),
    path(
        "agreements/<int:pk>/terminate/",
        governance.agreement_terminate,
        name="agreement-terminate",
    ),
    path("agreements/<int:pk>/document/", governance.agreement_document, name="agreement-document"),
    # Catalogues (governance_views)
    path("catalogues/categories/", governance.category_list, name="category-list"),
    path("catalogues/categories/new/", governance.category_edit, name="category-create"),
    path("catalogues/categories/<uuid:pk>/edit/", governance.category_edit, name="category-edit"),
    path("catalogues/consent-forms/", governance.consent_form_list, name="consent-form-list"),
    path("catalogues/consent-forms/new/", governance.consent_form_edit, name="consent-form-create"),
    path(
        "catalogues/consent-forms/<uuid:pk>/edit/",
        governance.consent_form_edit,
        name="consent-form-edit",
    ),
    path("catalogues/institutions/", governance.institution_list, name="institution-list"),
    path("catalogues/institutions/new/", governance.institution_edit, name="institution-create"),
    path(
        "catalogues/institutions/<uuid:pk>/edit/",
        governance.institution_edit,
        name="institution-edit",
    ),
    path("catalogues/investigators/", governance.investigator_list, name="investigator-list"),
    path("catalogues/investigators/new/", governance.investigator_edit, name="investigator-create"),
    path(
        "catalogues/investigators/<uuid:pk>/edit/",
        governance.investigator_edit,
        name="investigator-edit",
    ),
    path("catalogues/export/<str:kind>/", governance.catalogue_export, name="catalogue-export"),
    # Downloads shared by both areas (views dispatches by kind)
    path("attachment/<str:kind>/<uuid:pk>/", views.attachment, name="attachment"),
    path("export/<str:kind>/", views.export, name="export"),
]
