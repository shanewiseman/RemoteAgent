from __future__ import annotations

import hmac
from collections.abc import Iterable
from typing import Any

from starlette.responses import JSONResponse


class BearerAuthMiddleware:
    """Protect the private API and readiness endpoint with one service token."""

    def __init__(
        self,
        app: Any,
        token: str,
        *,
        public_paths: Iterable[str] = ("/health", "/healthz"),
    ) -> None:
        self.app = app
        self.token = token.encode("utf-8")
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") in self.public_paths:
            await self.app(scope, receive, send)
            return
        authorization = next(
            (
                value.decode("latin-1")
                for name, value in scope.get("headers", ())
                if name.lower() == b"authorization"
            ),
            "",
        )
        scheme, separator, supplied = authorization.partition(" ")
        valid = (
            bool(separator)
            and scheme.lower() == "bearer"
            and hmac.compare_digest(supplied.encode("utf-8"), self.token)
        )
        if not valid:
            response = JSONResponse(
                {"detail": "missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
