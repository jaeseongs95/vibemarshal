from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.adapters.runtime import (
    CODEX_DISABLED_SURFACE_OVERRIDES,
    _disabled_mcp_overrides,
)


class Gate0BRuntimeConfigurationTests(unittest.TestCase):
    def test_external_surfaces_are_explicitly_disabled(self) -> None:
        overrides = set(CODEX_DISABLED_SURFACE_OVERRIDES)
        self.assertIn('web_search="disabled"', overrides)
        self.assertIn("features.apps=false", overrides)
        self.assertIn("features.plugins=false", overrides)
        self.assertIn("features.multi_agent=false", overrides)
        self.assertIn("features.in_app_browser=false", overrides)
        self.assertIn("features.computer_use=false", overrides)
        self.assertIn("mcp_servers={}", overrides)

    def test_registered_mcp_entries_are_disabled_and_made_inert(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            (codex_home / "config.toml").write_text(
                """
[mcp_servers.cua_repl]
command = "node"
args = ["worker.js"]

[mcp_servers.remote]
url = "https://example.invalid/mcp"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                overrides = set(_disabled_mcp_overrides())

        self.assertIn('mcp_servers."cua_repl".enabled=false', overrides)
        self.assertIn(
            'mcp_servers."cua_repl".command="__flowmarshal_disabled_mcp__"',
            overrides,
        )
        self.assertIn('mcp_servers."cua_repl".args=[]', overrides)
        self.assertIn('mcp_servers."remote".enabled=false', overrides)
        self.assertIn(
            'mcp_servers."remote".url="http://127.0.0.1:9"', overrides
        )


if __name__ == "__main__":
    unittest.main()
