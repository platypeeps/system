"""Production startup validates a built library and explicit local settings."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sd_dashboard import runtime, server


class ServerStartup(unittest.TestCase):
    def test_explicit_local_config_reaches_the_validated_installed_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "dashboard.json"
            config.write_text(json.dumps({"port": 8787, "database": "/fixture/sd.db"}))
            listening = Mock(server_address=("127.0.0.1", 8787))
            with patch.object(runtime, "installed_library") as library, patch.object(server, "build", return_value=listening) as build:
                self.assertEqual(server.main(["--config", str(config), "--port", "8787"]), 0)
            library.assert_called_once_with("/fixture/sd.db")
            build.assert_called_once_with("/fixture/sd.db", port=8787, jev=runtime.jev_command())
            listening.server_close.assert_called_once()
            # sd:3018: a changed build restarts only once the new build's own preflight passes.
            self.assertEqual(listening.restart_probe,
                             [str(runtime.HERE / "dashboard.sh"), "preflight", "--config", str(config)])

    def test_changed_build_exits_non_zero_for_launchd_to_restart(self):
        listening = Mock(server_address=("127.0.0.1", 8767))
        listening.serve_forever.side_effect = server.CodeChanged("build changed")
        with patch.object(runtime, "installed_library"), patch.object(server, "build", return_value=listening):
            self.assertEqual(server.main([]), 1)
        listening.server_close.assert_called_once()

    def test_source_library_is_refused_before_a_socket_opens(self):
        with patch.object(runtime, "installed_library", side_effect=runtime.RuntimeRefused("installed build required")), patch.object(server, "build") as build:
            with self.assertRaises(SystemExit) as refused:
                server.main([])
            self.assertEqual(refused.exception.code, 2)
            build.assert_not_called()
