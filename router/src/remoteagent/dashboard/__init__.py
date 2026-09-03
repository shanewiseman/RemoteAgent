"""Operational dashboard for the RemoteAgent router.

The dashboard is intentionally registered explicitly so importing the core
application does not create routes, Redis clients, or other process state.
"""

from .routes import register_dashboard

__all__ = ["register_dashboard"]
