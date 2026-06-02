import importlib
import os
import unittest
from pathlib import Path
from unittest import mock

import app.config as config_module


class ConfigTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        importlib.reload(config_module)

    def test_cloud_environment_variables_override_defaults(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "WORKSPACE_DIR": "/var/data/workspace",
                "TOKENS_PATH": "/var/data/tokens.json",
                "BASE_PUBLIC_URL": "https://relay.example.com/",
                "DEFAULT_TOKEN_TTL_DAYS": "14",
                "ADMIN_KEY": "admin-secret",
            },
            clear=False,
        ):
            importlib.reload(config_module)
            self.assertEqual(config_module.WORKSPACE_ROOT, Path("/var/data/workspace"))
            self.assertEqual(config_module.TOKENS_FILE, Path("/var/data/tokens.json"))
            self.assertEqual(config_module.BASE_PUBLIC_URL, "https://relay.example.com")
            self.assertEqual(config_module.DEFAULT_TOKEN_TTL_DAYS, 14)
            self.assertEqual(config_module.TOKEN_TTL_DAYS, 14)
            self.assertEqual(config_module.ADMIN_KEY, "admin-secret")

    def test_token_ttl_days_env_remains_backward_compatible(self) -> None:
        with mock.patch.dict(os.environ, {"TOKEN_TTL_DAYS": "9"}, clear=False):
            os.environ.pop("DEFAULT_TOKEN_TTL_DAYS", None)
            importlib.reload(config_module)
            self.assertEqual(config_module.DEFAULT_TOKEN_TTL_DAYS, 9)
            self.assertEqual(config_module.TOKEN_TTL_DAYS, 9)


if __name__ == "__main__":
    unittest.main()
