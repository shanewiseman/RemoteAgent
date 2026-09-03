from __future__ import annotations

import hmac
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from starlette.responses import JSONResponse


class AuthRole(StrEnum):
    ROUTER = "router"
    CRON = "cron"
    UNAUTHENTICATED = "unauthenticated"


_AUTH_ROLE_STATE_KEY = "remoteagent_auth_role"
CRON_MCP_ALLOWED_TOOLS = frozenset(
    {"list_agents", "get_agent", "submit_prompt", "get_prompt_status", "cancel_prompt"}
)


def mcp_auth_role(context: Any) -> AuthRole:
    """Resolve the role attached by HTTP auth, defaulting for in-process calls."""

    try:
        request = context.request_context.request
    except (AttributeError, ValueError):
        return AuthRole.ROUTER
    if request is None:
        return AuthRole.ROUTER
    state = request.scope.get("state", {})
    try:
        return AuthRole(state.get(_AUTH_ROLE_STATE_KEY, AuthRole.UNAUTHENTICATED))
    except ValueError:
        return AuthRole.UNAUTHENTICATED


def authorize_mcp_tool(context: Any, tool_name: str) -> None:
    role = mcp_auth_role(context)
    if role is AuthRole.ROUTER:
        return
    if role is AuthRole.CRON and tool_name in CRON_MCP_ALLOWED_TOOLS:
        return
    raise PermissionError(f"the {role.value} MCP role cannot call {tool_name}")


def authorize_mcp_prompt_companions(context: Any, companions: object) -> None:
    """Keep companion staging and binding outside the scoped cron role."""

    if companions and mcp_auth_role(context) is AuthRole.CRON:
        raise PermissionError("the cron MCP role cannot submit companion bindings")


def authorize_mcp_resource(context: Any) -> None:
    role = mcp_auth_role(context)
    if role is not AuthRole.ROUTER:
        raise PermissionError(f"the {role.value} MCP role cannot read resources")


class BearerAuthMiddleware:
    """Protect every HTTP surface except an explicit, exact allow-list."""

    def __init__(
        self,
        app: Any,
        token: str,
        *,
        cron_token: str | None = None,
        mcp_path: str = "/mcp",
        public_paths: Iterable[str] = ("/health", "/healthz"),
        public_prefixes: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.token = token.encode("utf-8")
        self.cron_token = cron_token.encode("utf-8") if cron_token else None
        self.mcp_path = mcp_path
        self.public_paths = frozenset(public_paths)
        self.public_prefixes = tuple(public_prefixes)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        authorization = next(
            (
                value.decode("latin-1")
                for name, value in scope.get("headers", ())
                if name.lower() == b"authorization"
            ),
            "",
        )
        scheme, separator, supplied = authorization.partition(" ")
        bearer = bool(separator) and scheme.lower() == "bearer"
        primary_valid = bearer and hmac.compare_digest(supplied.encode("utf-8"), self.token)
        cron_valid = (
            bearer
            and self.cron_token is not None
            and hmac.compare_digest(supplied.encode("utf-8"), self.cron_token)
        )

        if (
            cron_valid
            and not primary_valid
            and path not in self.public_paths
            and not self._is_mcp_path(path)
        ):
            response = JSONResponse(
                {"detail": "bearer token is not authorized for this HTTP surface"},
                status_code=403,
            )
            await response(scope, receive, send)
            return

        if path in self.public_paths or any(
            path == item or path.startswith(f"{item}/") for item in self.public_prefixes
        ):
            await self.app(scope, receive, send)
            return

        valid = primary_valid or cron_valid
        if valid:
            state = scope.setdefault("state", {})
            state[_AUTH_ROLE_STATE_KEY] = (
                AuthRole.ROUTER.value if primary_valid else AuthRole.CRON.value
            )
        else:
            response = JSONResponse(
                {"detail": "missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _is_mcp_path(self, path: str) -> bool:
        return path == self.mcp_path or path.startswith(f"{self.mcp_path}/")
