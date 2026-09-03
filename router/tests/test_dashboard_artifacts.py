from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from remoteagent.dashboard.artifacts import (
    ArtifactPayload,
    content_disposition,
    download_response,
    preview_response,
    sanitize_filename,
)


def test_filename_and_content_disposition_reject_header_injection() -> None:
    filename = sanitize_filename("../../report\r\nX-Evil: yes.html")
    header = content_disposition(filename)

    assert filename == "reportX-Evil: yes.html"
    assert "\r" not in header
    assert "\n" not in header
    assert header.startswith("attachment;")


def test_text_preview_is_json_not_executable_markup() -> None:
    payload = ArtifactPayload(
        b'<script>alert("x")</script>',
        "text/plain",
        "note.txt",
    )

    response = preview_response({"size_bytes": 27}, payload)
    decoded = json.loads(response.body)

    assert decoded["text"] == '<script>alert("x")</script>'
    assert response.media_type == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_original_raster_is_not_treated_as_a_safe_preview() -> None:
    payload = ArtifactPayload(b"not-reencoded", "image/png", "image.png")

    with pytest.raises(HTTPException) as error:
        preview_response({"size_bytes": len(payload.content)}, payload)

    assert error.value.status_code == 415


def test_explicit_safe_derivative_can_be_previewed() -> None:
    payload = ArtifactPayload(b"png", "image/png", "image.png", safe_derivative=True)

    response = preview_response({"size_bytes": 3}, payload)

    assert response.media_type == "image/png"
    assert response.headers["content-security-policy"].startswith("sandbox")
    assert response.headers["content-disposition"].startswith("inline")


def test_active_content_is_forced_to_attachment_and_octet_stream() -> None:
    payload = ArtifactPayload(b"<html></html>", "text/html", "page.html")

    response = download_response({"size_bytes": 13}, payload)

    assert response.media_type == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-security-policy"].startswith("sandbox")
