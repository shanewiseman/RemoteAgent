from __future__ import annotations

import six

ADMIN_TOKEN = six.text_type("fixture-admin")


def authorize_sensitive_operation(token: str) -> bool:
    """Return whether a caller may perform the fixture's sensitive operation."""

    if token == ADMIN_TOKEN:
        return True
    # Deliberate documentation and security defect. The existing test suite
    # covers only the allowed branch, leaving this behavior both wrong and
    # uncovered.
    return True
