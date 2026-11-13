"""Default browser policy and explicit response overrides."""

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from core.security_headers import (
    DEFAULT_CONTENT_SECURITY_POLICY,
    DEFAULT_PERMISSIONS_POLICY,
    SecurityHeadersMiddleware,
)


@pytest.mark.parametrize("status", [200, 302, 403, 413, 500])
def test_default_headers_apply_without_reading_body(status):
    request = RequestFactory().post("/synthetic/", data=b"synthetic", content_type="text/plain")
    response = SecurityHeadersMiddleware(lambda r: HttpResponse(status=status))(request)
    assert response["Content-Security-Policy"] == DEFAULT_CONTENT_SECURITY_POLICY
    assert response["Permissions-Policy"] == DEFAULT_PERMISSIONS_POLICY
    assert not request._read_started
    assert "unsafe-inline" not in response["Content-Security-Policy"]
    assert "unsafe-eval" not in response["Content-Security-Policy"]


def test_explicit_response_policies_are_preserved():
    response = HttpResponse(
        headers={
            "Content-Security-Policy": "default-src 'none'",
            "Permissions-Policy": "camera=(self)",
        }
    )
    result = SecurityHeadersMiddleware(lambda r: response)(RequestFactory().get("/"))
    assert result["Content-Security-Policy"] == "default-src 'none'"
    assert result["Permissions-Policy"] == "camera=(self)"


@override_settings(CONTENT_SECURITY_POLICY="default-src 'none'; frame-ancestors 'none'")
def test_deployer_policy_is_used_verbatim():
    result = SecurityHeadersMiddleware(lambda r: HttpResponse())(RequestFactory().get("/"))
    assert result["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"


def test_no_page_can_be_framed_by_any_origin():
    assert "frame-ancestors 'none'" in DEFAULT_CONTENT_SECURITY_POLICY
    assert "object-src 'none'" in DEFAULT_CONTENT_SECURITY_POLICY


def test_real_middleware_adds_headers_to_early_request_limit_refusals(client, settings):
    response = client.post(
        "/accounts/login/",
        data=b"x",
        content_type="text/plain",
        CONTENT_LENGTH=str(settings.MAX_REQUEST_BODY_BYTES + 1),
    )
    assert response.status_code == 413
    assert response["Content-Security-Policy"] == DEFAULT_CONTENT_SECURITY_POLICY
    assert response["Permissions-Policy"] == DEFAULT_PERMISSIONS_POLICY
    assert response["X-Request-ID"]
