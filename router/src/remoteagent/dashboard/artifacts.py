"""Safe artifact preview and download response construction."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException
from starlette.responses import Response, StreamingResponse

TEXT_PREVIEW_BYTES = 256 * 1024
IMAGE_PREVIEW_BYTES = 2 * 1024 * 1024
SAFE_TEXT_TYPES = {
    "text/plain",
    "text/markdown",
    "application/json",
    "application/problem+json",
}
SAFE_DERIVATIVE_IMAGE_TYPES = {"image/png", "image/jpeg"}
ACTIVE_TYPES = {
    "text/html",
    "image/svg+xml",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
    "application/javascript",
    "text/javascript",
    "application/pdf",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    content: bytes | bytearray | memoryview | Iterator[bytes] | AsyncIterator[bytes]
    media_type: str
    filename: str
    safe_derivative: bool = False


def sanitize_filename(value: str, *, fallback: str = "artifact") -> str:
    value = value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    value = _CONTROL.sub("", value).replace("\r", "").replace("\n", "").strip(" .")
    return value[:240] or fallback


def content_disposition(filename: str, *, attachment: bool = True) -> str:
    filename = sanitize_filename(filename)
    ascii_name = "".join(
        character if 32 <= ord(character) < 127 and character not in {'"', "\\"} else "_"
        for character in filename
    )
    disposition = "attachment" if attachment else "inline"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def clean_text(content: bytes, *, media_type: str) -> str:
    text = content.decode("utf-8", errors="replace")
    text = _CONTROL.sub("", text)
    if media_type in {"application/json", "application/problem+json"}:
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return text


def normalize_payload(
    value: Any, metadata: Mapping[str, Any], *, safe_derivative: bool = False
) -> ArtifactPayload | None:
    if value is None:
        return None
    media_type = (
        str(metadata.get("media_type") or "application/octet-stream").split(";", 1)[0].lower()
    )
    filename = str(metadata.get("name") or metadata.get("relative_path") or "artifact")
    content: Any = value
    derivative = safe_derivative
    if isinstance(value, Mapping):
        content = value.get("content", value.get("body"))
        media_type = str(value.get("media_type") or media_type).split(";", 1)[0].lower()
        filename = str(value.get("filename") or filename)
        derivative = bool(value.get("safe_derivative", derivative))
    if isinstance(content, str):
        content = content.encode("utf-8")
    if (
        not isinstance(content, (bytes, bytearray, memoryview))
        and not hasattr(content, "__iter__")
        and not hasattr(content, "__aiter__")
    ):
        return None
    return ArtifactPayload(content, media_type, filename, derivative)


def preview_response(metadata: Mapping[str, Any], payload: ArtifactPayload | None) -> Response:
    if payload is None:
        raise HTTPException(status_code=404, detail="artifact preview unavailable")
    size = int(metadata.get("size_bytes") or 0)
    content = payload.content
    if payload.media_type in SAFE_TEXT_TYPES:
        if size > TEXT_PREVIEW_BYTES or not isinstance(content, (bytes, bytearray, memoryview)):
            raise HTTPException(status_code=413, detail="artifact is too large for text preview")
        raw = bytes(content)
        if len(raw) > TEXT_PREVIEW_BYTES:
            raise HTTPException(status_code=413, detail="artifact is too large for text preview")
        # JSON keeps the browser from interpreting even HTML-looking text.
        body = json.dumps(
            {
                "kind": "text",
                "media_type": payload.media_type,
                "text": clean_text(raw, media_type=payload.media_type),
            }
        )
        return Response(
            body,
            media_type="application/json",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    if payload.media_type in SAFE_DERIVATIVE_IMAGE_TYPES and payload.safe_derivative:
        if (
            not isinstance(content, (bytes, bytearray, memoryview))
            or len(content) > IMAGE_PREVIEW_BYTES
        ):
            raise HTTPException(status_code=413, detail="safe image preview is too large")
        return Response(
            bytes(content),
            media_type=payload.media_type,
            headers={
                "Cache-Control": "private, max-age=300",
                "Content-Disposition": content_disposition(payload.filename, attachment=False),
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )
    raise HTTPException(status_code=415, detail="this artifact type is download-only")


def download_response(metadata: Mapping[str, Any], payload: ArtifactPayload | None) -> Response:
    if payload is None:
        raise HTTPException(status_code=404, detail="artifact content unavailable")
    media_type = payload.media_type
    if (
        media_type in ACTIVE_TYPES
        or media_type.startswith("text/")
        and media_type not in SAFE_TEXT_TYPES
    ):
        media_type = "application/octet-stream"
    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": content_disposition(payload.filename, attachment=True),
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "X-Content-Type-Options": "nosniff",
    }
    if isinstance(payload.content, (bytes, bytearray, memoryview)):
        return Response(bytes(payload.content), media_type=media_type, headers=headers)
    return StreamingResponse(payload.content, media_type=media_type, headers=headers)


__all__ = [
    "ACTIVE_TYPES",
    "IMAGE_PREVIEW_BYTES",
    "SAFE_DERIVATIVE_IMAGE_TYPES",
    "SAFE_TEXT_TYPES",
    "TEXT_PREVIEW_BYTES",
    "ArtifactPayload",
    "clean_text",
    "content_disposition",
    "download_response",
    "normalize_payload",
    "preview_response",
    "sanitize_filename",
]
