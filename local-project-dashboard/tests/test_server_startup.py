"""Production startup validates a built library and explicit local settings."""

import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
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

    def test_a_new_checkout_whose_launcher_fails_keeps_the_old_server_serving(self):
        """sd:3018: the probe runs the new checkout's dashboard.sh preflight, which runs its bootstrap.py, before the import.

        The fake checkout's server imports and starts; only its launcher or its bootstrap fails.
        """
        launcher = "#!/bin/sh\nexec '{python}' -I \"$(dirname \"$0\")/sd_dashboard/bootstrap.py\" runtime \"$@\"\n"
        cases = {
            "preflight refuses": ("#!/bin/sh\necho 'dashboard: preflight refused' >&2\nexit 1\n", "import sys\n",
                                  "dashboard: preflight refused"),
            "bootstrap cannot import": (launcher.format(python=sys.executable),
                                        "raise ImportError('bootstrap cannot import')\n", "bootstrap cannot import"),
        }
        for case, (script, bootstrap, reason) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                config = root / "dashboard.json"
                config.write_text(json.dumps({"port": 8787}))
                (root / "dashboard.sh").write_text(script)
                (root / "dashboard.sh").chmod(0o755)
                package = root / "sd_dashboard"
                package.mkdir()
                (package / "__init__.py").write_text("")
                (package / "bootstrap.py").write_text(bootstrap)
                (package / "server.py").write_text("def main(argv, *, serve=True):\n    return 0\n")
                loaded = {"library_digest": "a" * 64, "dashboard_digest": "b" * 64}
                listening = Mock(server_address=("127.0.0.1", 8787))
                with patch.object(runtime, "installed_library"), patch.object(server, "build", return_value=listening), \
                        patch.object(runtime, "HERE", root):
                    server.main(["--config", str(config), "--port", "8787"])
                with patch.object(runtime, "build_digests", return_value=loaded):
                    old = server.build(None, port=0)
                self.addCleanup(old.server_close)
                old.build_check_seconds = 0
                old.restart_probe = listening.restart_probe
                ended = []
                def serve():
                    try:
                        old.serve_forever(poll_interval=0.01)
                    except BaseException as error:  # noqa: BLE001 - the test reads what ended it
                        ended.append(error)
                with patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                        patch.object(runtime, "build_digests", return_value=dict(loaded, dashboard_digest="0" * 64)):
                    thread = threading.Thread(target=serve, daemon=True)
                    thread.start()
                    thread.join(3)
                    alive = thread.is_alive()
                    if alive:
                        old.shutdown()
                self.assertTrue(alive, f"the old server stopped for a checkout whose launcher fails: {ended}")
                self.assertIn(reason, stderr.getvalue())

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
