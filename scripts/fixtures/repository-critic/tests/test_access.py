from __future__ import annotations

import unittest

from review_target.access import authorize_sensitive_operation


class AuthorizationTests(unittest.TestCase):
    def test_exact_admin_token_is_allowed(self) -> None:
        self.assertTrue(authorize_sensitive_operation("fixture-admin"))


if __name__ == "__main__":
    unittest.main()
