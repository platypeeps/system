"""Production startup validates a built library and explicit local settings."""

import json
from pathlib import Path
import socket
import subprocess
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
            build.assert_called_once_with("/fixture/sd.db", port=8787, bind=True, jev=runtime.jev_command())
            listening.server_close.assert_called_once()

    def test_changed_build_exits_non_zero_for_launchd_to_restart(self):
        listening = Mock(server_address=("127.0.0.1", 8767), restart_probe=None)
        listening.serve_forever.side_effect = server.CodeChanged("build changed")
        with patch.object(runtime, "installed_library"), patch.object(server, "build", return_value=listening):
            self.assertEqual(server.main([]), 1)
        listening.server_close.assert_called_once()
        # No --config is no LaunchAgent, so nothing would start a replacement: no probe, no restart.
        self.assertIsNone(listening.restart_probe)

    def test_restart_probe_runs_the_new_checkouts_server_short_of_the_bind(self):
        """sd:3018: preflight passed while the new server could not import, and the old one exited.

        The fake checkout's dashboard.sh passes, as that preflight did. The probe must import the
        checkout's own server module and run its main with the same arguments and serve=False.
        """
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "dashboard.json"
            config.write_text(json.dumps({"port": 8787}))
            launcher = root / "dashboard.sh"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)
            package = root / "sd_dashboard"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "server.py").write_text("raise ImportError('the new server cannot import')\n")
            listening = Mock(server_address=("127.0.0.1", 8787))
            argv = ["--config", str(config), "--port", "8787"]
            with patch.object(runtime, "installed_library"), patch.object(server, "build", return_value=listening), \
                    patch.object(runtime, "HERE", root):
                server.main(argv)
            def probe():
                return subprocess.run(listening.restart_probe, capture_output=True, text=True, timeout=60,
                                      stdin=subprocess.DEVNULL, check=False)
            broken = probe()
            self.assertNotEqual(broken.returncode, 0, "the restart probe passed a server that cannot import")
            self.assertIn("the new server cannot import", broken.stderr)
            (package / "server.py").write_text("def main(argv, *, serve=True):\n    print(argv, serve)\n    return 0\n")
            ready = probe()
            self.assertEqual((ready.returncode, ready.stdout.strip()), (0, f"{argv} False"))

    def test_check_only_start_refuses_where_a_start_refuses_and_binds_nothing(self):
        """sd:3018: main(serve=False) is the probe's body: each start step, then no bind."""
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "dashboard.json"
            config.write_text(json.dumps({"port": 8787}))
            argv = ["--config", str(config), "--port", "8787"]
            refusals = {
                "config": (["--config", str(Path(folder) / "missing.json"), "--port", "8787"], None, None),
                "port": (["--config", str(config), "--port", "8788"], None, None),
                "library": (argv, runtime.RuntimeRefused("installed sd_db lacks the checkout's library commit"), None),
                "frontdoor": (argv, None, runtime.RuntimeRefused("private Serve route changed")),
            }
            for step, (arguments, library, frontdoor) in refusals.items():
                with self.subTest(step=step), patch.object(runtime, "installed_library", side_effect=library), \
                        patch.object(runtime, "load_frontdoor", side_effect=frontdoor, return_value=None), \
                        patch.object(server, "build") as build:
                    with self.assertRaises(SystemExit) as refused:
                        server.main(arguments, serve=False)
                    self.assertEqual(refused.exception.code, 2)
                    build.assert_not_called()
            with patch.object(runtime, "installed_library"), patch.object(runtime, "load_frontdoor", return_value=None):
                with self.subTest(step="listeners"), patch.object(
                        runtime, "build_digests", side_effect=runtime.RuntimeRefused("cannot fingerprint runtime build")):
                    with self.assertRaises(runtime.RuntimeRefused):
                        server.main(argv, serve=False)
                # The old server still holds the port; a check that tried to bind would fail here.
                with socket.socket() as holder:
                    holder.bind(("127.0.0.1", 0))
                    holder.listen()
                    port = holder.getsockname()[1]
                    config.write_text(json.dumps({"port": port}))
                    self.assertEqual(server.main(["--config", str(config), "--port", str(port)], serve=False), 0)

    def test_source_library_is_refused_before_a_socket_opens(self):
        with patch.object(runtime, "installed_library", side_effect=runtime.RuntimeRefused("installed build required")), patch.object(server, "build") as build:
            with self.assertRaises(SystemExit) as refused:
                server.main([])
            self.assertEqual(refused.exception.code, 2)
            build.assert_not_called()
