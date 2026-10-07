"""Installer safety with private files, fake launchd, and fake Tailscale state."""

import copy
import http.client
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import runtime

REAL_LIBRARY_CHECK = runtime.installed_library


ORIGIN = "https://fixture.example.ts.net:8443"
AUTHORITY = "fixture.example.ts.net:8443"
PUBLIC = {"TCP": {"443": {"HTTPS": True}},
          "Web": {"fixture.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}}},
          "AllowFunnel": {"fixture.example.ts.net:443": True}}


class RuntimeSafety(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dashboard-runtime-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = self.home / "dashboard.json"
        self.config.write_text(json.dumps({"origin": ORIGIN, "operator_login": "owner@example.test", "port": 8767}))
        self.target = self.home / "Library/LaunchAgents" / f"{runtime.LABEL}.plist"
        self.target.parent.mkdir(parents=True)
        self.old = plistlib.dumps({"Label": runtime.LABEL, "ProgramArguments": ["old-dashboard", "serve"]})
        self.target.write_bytes(self.old)
        self.status = copy.deepcopy(PUBLIC)
        self.calls = []
        self.loaded = True
        self.pending_removal = None
        self.info = {"python": sys.executable, "library": "/fixture/.venv/lib/python3.13/site-packages/sd_db/__init__.py", "schema": 3,
                     "library_digest": "a" * 64, "dashboard_digest": "b" * 64}
        self.health = {"ok": True, "service": "sd-dashboard", "pid": 4242, **self.info}
        self.addCleanup(patch.stopall)
        patch.object(runtime, "installed_library", return_value=self.info).start()
        patch.object(runtime, "_run", side_effect=self.run_command).start()
        patch.object(runtime, "health_check", return_value=self.health).start()

    def run_command(self, arguments, *, check=True, timeout=20):
        self.calls.append(arguments)
        code, output = 0, ""
        if arguments[:4] == ["tailscale", "serve", "status", "--json"]:
            output = json.dumps(self.status)
        elif arguments == ["tailscale", "status", "--json"]:
            output = json.dumps({"BackendState": "Running", "Self": {"DNSName": "fixture.example.ts.net.", "UserID": 42, "TailscaleIPs": ["100.64.0.10"]},
                                 "User": {"42": {"LoginName": "owner@example.test"}}})
        elif arguments == ["tailscale", "serve", "--bg", "--https=8443", "--set-path=/", "off"]:
            self.status["TCP"].pop("8443", None)
            self.status["Web"].pop(AUTHORITY, None)
        elif arguments == ["tailscale", "serve", "--bg", "--https=8443", "--set-path=/", "http://127.0.0.1:8767"]:
            self.status["TCP"]["8443"] = {"HTTPS": True}
            self.status["Web"][AUTHORITY] = {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}}
        elif arguments[:2] == ["launchctl", "print"]:
            code = 0 if self.loaded else 113
            argv = plistlib.loads(self.target.read_bytes())["ProgramArguments"] if self.target.exists() else ["missing"]
            output = (arguments[2] + " = {\n\tpid = 4242\n\tpath = " + str(self.target) +
                      "\n\tprogram = " + argv[0] + "\n\targuments = {\n" +
                      "\n".join("\t\t" + argument for argument in argv) + "\n\t}\n}")
        elif arguments[:2] == ["launchctl", "bootout"]:
            self.loaded = False
        elif arguments[:2] == ["launchctl", "bootstrap"]:
            self.loaded = True
        else:
            self.fail(f"unexpected command: {arguments}")
        return subprocess.CompletedProcess(arguments, code, output, "")

    def delayed_removal(self, arguments, **options):
        if arguments[:2] == ["launchctl", "bootout"]:
            result = self.run_command(arguments, **options)
            self.loaded = True
            self.pending_removal = [2, self.target.read_bytes()]
            return result
        if arguments[:2] == ["launchctl", "print"] and self.pending_removal is not None:
            self.assertEqual(self.target.read_bytes(), self.pending_removal[1])
            if self.pending_removal[0]:
                self.pending_removal[0] -= 1
            else:
                self.loaded = False
                self.pending_removal = None
        if arguments[:2] == ["launchctl", "bootstrap"]:
            self.assertFalse(self.loaded, "bootstrap must wait for removal")
            self.assertIsNone(self.pending_removal)
        return self.run_command(arguments, **options)

    def plan(self):
        return runtime.preflight(self.config, home=self.home)

    def install(self, plan):
        return runtime.install(self.config, expected_fingerprint=plan["fingerprint"], home=self.home)

    def test_preflight_is_read_only_and_names_exact_replacement(self):
        before = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        plan = self.plan()
        self.assertFalse(plan["serve_route_present"])
        self.assertEqual(plan["program"][0], str(runtime.HERE / "dashboard.sh"))
        self.assertEqual(before, {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob("*") if p.is_file()})
        self.assertEqual(self.status, PUBLIC)
        self.assertTrue(all(call[1] in ("serve", "status", "print") for call in self.calls))

    def test_install_preserves_public_funnel_and_uses_installed_interpreter(self):
        result = self.install(self.plan())
        self.assertTrue(result["ok"])
        self.assertEqual(runtime._without_dashboard(self.status, AUTHORITY), PUBLIC)
        self.assertEqual((Path(result["backup_path"]) / self.target.name).read_bytes(), self.old)
        plist = plistlib.loads(self.target.read_bytes())
        self.assertEqual(plist["EnvironmentVariables"]["SD_DASHBOARD_PYTHON"], sys.executable)
        self.assertNotIn("PYTHONPATH", plist["EnvironmentVariables"])
        self.assertEqual(plist["Label"], runtime.LABEL)
        # An interactive server: Background put it on efficiency cores and
        # low-priority I/O, and a Today render took 4-5x longer (sd:1433).
        self.assertEqual(plist["ProcessType"], "Standard")
        self.assertTrue(self.loaded)
        self.assertFalse(any("funnel" in call or "reset" in call or "set-config" in call for call in self.calls))

    def test_install_waits_for_delayed_launchd_removal_before_replacement(self):
        with patch.object(runtime, "_run", side_effect=self.delayed_removal), \
                patch.object(runtime.time, "sleep"):
            self.assertTrue(self.install(self.plan())["ok"])
        self.assertEqual(sum(call[1] == "bootstrap" for call in self.calls), 1)

    def test_rollback_waits_for_delayed_removal_before_restoring_and_restarting(self):
        with patch.object(runtime, "_run", side_effect=self.delayed_removal), \
                patch.object(runtime.time, "sleep"), \
                patch.object(runtime, "health_check", side_effect=runtime.RuntimeRefused("unhealthy")):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "previous service restored"):
                self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertTrue(self.loaded)
        self.assertEqual(sum(call[1] == "bootstrap" for call in self.calls), 2)

    def test_shutdown_timeout_preserves_plist_without_bootstrap_or_second_bootout(self):
        def stuck(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[:2] == ["launchctl", "bootout"]:
                self.loaded = True
            return result
        ticks = iter(range(0, 1000, 4))
        with patch.object(runtime, "_run", side_effect=stuck), \
                patch.object(runtime.time, "monotonic", side_effect=lambda: next(ticks)), \
                patch.object(runtime.time, "sleep"):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "shutdown deadline.*rollback requires attention"):
                self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(sum(call[1] == "bootout" for call in self.calls), 1)
        self.assertFalse(any(call[1] == "bootstrap" for call in self.calls))

    def test_shutdown_wait_rejects_unknown_launchctl_status(self):
        def unknown(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[:2] == ["launchctl", "print"] and any(call[1] == "bootout" for call in self.calls):
                result.returncode = 5
            return result
        with patch.object(runtime, "_run", side_effect=unknown):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "identity changed or is unknown.*rollback requires attention"):
                self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse(any(call[1] == "bootstrap" for call in self.calls))

    def test_shutdown_wait_preserves_concurrent_plist_or_loaded_identity(self):
        for changed_plist in (False, True):
            with self.subTest(changed_plist=changed_plist):
                self.calls.clear()
                self.target.write_bytes(self.old)
                self.loaded = True
                newer = plistlib.dumps({"Label": runtime.LABEL, "ProgramArguments": ["newer-dashboard"]})
                def concurrent(arguments, **options):
                    result = self.run_command(arguments, **options)
                    if arguments[:2] == ["launchctl", "bootout"]:
                        self.loaded = True
                        if changed_plist:
                            self.target.write_bytes(newer)
                    if (arguments[:2] == ["launchctl", "print"] and not changed_plist and
                            any(call[1] == "bootout" for call in self.calls)):
                        result.stdout = result.stdout.replace("\tprogram = old-dashboard", "\tprogram = newer-dashboard")
                    return result
                with patch.object(runtime, "_run", side_effect=concurrent):
                    with self.assertRaisesRegex(runtime.RuntimeRefused, "rollback requires attention"):
                        self.install(self.plan())
                self.assertEqual(self.target.read_bytes(), newer if changed_plist else self.old)
                self.assertEqual(sum(call[1] == "bootout" for call in self.calls), 1)
                self.assertFalse(any(call[1] == "bootstrap" for call in self.calls))

    def test_ip_listener_requires_health_confirmation_and_preserves_https(self):
        config = json.loads(self.config.read_text())
        config["ip_origin"] = "http://100.64.0.10:8768"
        self.config.write_text(json.dumps(config))
        self.health["ip_origin"] = config["ip_origin"]
        result = self.install(self.plan())
        self.assertEqual(result["origin"], config["ip_origin"])
        self.assertEqual(runtime._without_dashboard(self.status, AUTHORITY), PUBLIC)
        self.assertNotIn("8768", self.status["TCP"])

    def test_missing_ip_listener_health_restores_previous_service(self):
        config = json.loads(self.config.read_text())
        config["ip_origin"] = "http://100.64.0.10:8768"
        self.config.write_text(json.dumps(config))
        with self.assertRaisesRegex(runtime.RuntimeRefused, "does not confirm.*IP listener"):
            self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertTrue(self.loaded)
        self.assertEqual(self.status, PUBLIC)

    def test_failed_health_restores_old_plist_service_and_only_owned_route(self):
        plan = self.plan()
        with patch.object(runtime, "health_check", side_effect=runtime.RuntimeRefused("unhealthy")):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "previous service restored"):
                self.install(plan)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.status, PUBLIC)
        self.assertTrue(self.loaded)
        self.assertIn(["tailscale", "serve", "--bg", "--https=8443", "--set-path=/", "off"], self.calls)

    def test_creation_that_applies_then_times_out_is_observed_and_rolled_back(self):
        def ambiguous(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[-1] == "http://127.0.0.1:8767":
                raise runtime.RuntimeRefused("tailscale command timed out")
            return result
        with patch.object(runtime, "_run", side_effect=ambiguous):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "previous service restored"):
                self.install(self.plan())
        self.assertEqual(self.status, PUBLIC)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertTrue(self.loaded)

    def test_creation_failure_without_side_effect_needs_no_route_removal(self):
        def refused(arguments, **options):
            if arguments[-1] == "http://127.0.0.1:8767":
                raise runtime.RuntimeRefused("tailscale command refused")
            return self.run_command(arguments, **options)
        with patch.object(runtime, "_run", side_effect=refused):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "previous service restored"):
                self.install(self.plan())
        self.assertEqual(self.status, PUBLIC)
        self.assertFalse(any(arguments[-1] == "off" for arguments in self.calls))

    def test_unconfirmed_route_removal_reports_manual_recovery(self):
        def ineffective(arguments, **options):
            if arguments[-1] == "off":
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return self.run_command(arguments, **options)
        with patch.object(runtime, "_run", side_effect=ineffective), \
                patch.object(runtime, "health_check", side_effect=runtime.RuntimeRefused("unhealthy")):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "rollback requires attention.*verify removal"):
                self.install(self.plan())
        self.assertIn(AUTHORITY, self.status["Web"])
        self.assertEqual(runtime._without_dashboard(self.status, AUTHORITY), PUBLIC)
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_final_read_refuses_new_serve_state_before_creation(self):
        plan = self.plan()
        def changed(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[:2] == ["launchctl", "print"]:
                self.status["Web"]["new.example.ts.net:9999"] = {"Handlers": {"/": {"Text": "concurrent"}}}
            return result
        with patch.object(runtime, "_run", side_effect=changed):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "changed before route creation"):
                self.install(plan)
        self.assertFalse(any("--bg" in arguments for arguments in self.calls))
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_concurrent_plist_edit_before_replacement_is_preserved(self):
        newer = plistlib.dumps({"Label": runtime.LABEL, "ProgramArguments": ["newer-dashboard"]})
        def concurrent(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[-1] == "http://127.0.0.1:8767":
                self.target.write_bytes(newer)
            return result
        with patch.object(runtime, "_run", side_effect=concurrent):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "changed before replacement"):
                self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), newer)
        self.assertTrue(self.loaded)
        self.assertFalse(any(call[1] in ("bootout", "bootstrap") or call[-1] == "off" for call in self.calls))

    def test_concurrent_plist_edit_during_failed_health_is_preserved(self):
        newer = plistlib.dumps({"Label": runtime.LABEL, "ProgramArguments": ["newer-dashboard"]})
        def concurrent(*args, **kwargs):
            self.target.write_bytes(newer)
            raise runtime.RuntimeRefused("new health failed")
        with patch.object(runtime, "health_check", side_effect=concurrent):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "rollback requires attention.*plist changed"):
                self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), newer)
        self.assertTrue(self.loaded)
        self.assertEqual(sum(call[1] == "bootout" for call in self.calls), 1)
        self.assertFalse(any(call[-1] == "off" for call in self.calls))

    def test_changed_loaded_service_identity_is_preserved_during_rollback(self):
        def concurrent(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[:2] == ["launchctl", "print"] and any(call[1] == "bootstrap" for call in self.calls):
                result.stdout = result.stdout.replace("\tpath = " + str(self.target), "\tpath = /newer/operator.plist")
            return result
        with patch.object(runtime, "_run", side_effect=concurrent), \
                patch.object(runtime, "health_check", side_effect=runtime.RuntimeRefused("unhealthy")):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "rollback requires attention.*identity changed"):
                self.install(self.plan())
        self.assertTrue(self.loaded)
        self.assertEqual(sum(call[1] == "bootout" for call in self.calls), 1)

    def test_malformed_remote_config_and_node_state_fail_closed(self):
        config = {"origin": [], "operator_login": "owner@example.test"}
        with self.assertRaises(runtime.RuntimeRefused):
            runtime._check_node(config)
        bad = {"BackendState": "Running", "Self": {"DNSName": None}}
        with patch.object(runtime, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(bad), "")):
            with self.assertRaises(runtime.RuntimeRefused):
                runtime._check_node({"origin": ORIGIN, "operator_login": "owner@example.test"})

    def test_wrong_library_health_rolls_back(self):
        self.health["library"] = "/checkout/local-sd-db/sd_db/__init__.py"
        with self.assertRaisesRegex(runtime.RuntimeRefused, "different schema or library"):
            self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.status, PUBLIC)

    def test_same_schema_different_health_build_rolls_back(self):
        for key in ("library_digest", "dashboard_digest"):
            with self.subTest(key=key):
                self.health[key] = "0" * 64
                with self.assertRaisesRegex(runtime.RuntimeRefused, "different build digests"):
                    self.install(self.plan())
                self.assertEqual(self.target.read_bytes(), self.old)
                self.assertEqual(self.status, PUBLIC)
                self.assertTrue(self.loaded)
                self.health[key] = self.info[key]

    def test_same_path_schema_library_replacement_invalidates_preview(self):
        import sd_db
        from sd_db.migrate import initialise

        prefix = self.home / "venv"
        package = prefix / "lib/python3.13/site-packages/sd_db"
        package.mkdir(parents=True)
        source = package / "__init__.py"
        source.write_text("# first installed build\n")
        database = self.home / "sd.db"
        initialise(database)
        config = json.loads(self.config.read_text())
        config["database"] = str(database)
        self.config.write_text(json.dumps(config))
        with patch.object(sd_db, "__file__", str(source)), patch.object(sys, "prefix", str(prefix)), \
                patch.object(runtime, "installed_library", side_effect=REAL_LIBRARY_CHECK):
            before = self.plan()
            source.write_text("# second installed build, unchanged schema and path\n")
            after = self.plan()
            self.assertEqual(before["runtime"]["library"], after["runtime"]["library"])
            self.assertEqual(before["runtime"]["schema"], after["runtime"]["schema"])
            self.assertNotEqual(before["runtime"]["library_digest"], after["runtime"]["library_digest"])
            self.assertNotEqual(before["fingerprint"], after["fingerprint"])
            with self.assertRaisesRegex(runtime.RuntimeRefused, "state changed"):
                self.install(before)
        self.assertFalse(any(call[1] in ("bootstrap", "bootout") or "--bg" in call for call in self.calls))
        self.assertFalse((self.home / ".local/share/sd/runtime-backups").exists())

    def test_healthy_unrelated_listener_cannot_satisfy_installation(self):
        self.health["pid"] = 31337
        with self.assertRaisesRegex(runtime.RuntimeRefused, "newly loaded dashboard process"):
            self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.status, PUBLIC)
        self.assertTrue(self.loaded)

    def test_unknown_or_duplicate_launchctl_pid_is_refused(self):
        service = f"gui/{os.getuid()}/{runtime.LABEL}"
        for output in ("unknown diagnostic output", service + " = {\n\tpid = 4242\n\tpid = 4242\n}"):
            with patch.object(runtime, "_run", return_value=subprocess.CompletedProcess([], 0, output, "")):
                with self.assertRaisesRegex(runtime.RuntimeRefused, "newly loaded dashboard process"):
                    runtime._verify_loaded_process(f"gui/{os.getuid()}", self.health)

    def test_stale_plan_refuses_before_any_mutation(self):
        plan = self.plan()
        self.target.write_bytes(self.old + b"\n")
        with self.assertRaisesRegex(runtime.RuntimeRefused, "changed"):
            self.install(plan)
        self.assertFalse(any(call[1] in ("bootstrap", "bootout") or "--bg" in call for call in self.calls))

    def test_conflicting_or_public_dashboard_route_is_refused(self):
        self.status["TCP"]["8443"] = {"HTTPS": True}
        self.status["Web"][AUTHORITY] = {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}
        with self.assertRaisesRegex(ValueError, "proxy"):
            self.plan()
        self.status["AllowFunnel"][AUTHORITY] = True
        with self.assertRaisesRegex(runtime.RuntimeRefused, "public through Funnel"):
            self.plan()

    def test_config_mismatch_and_reserved_origin_are_refused(self):
        with self.assertRaisesRegex(runtime.RuntimeRefused, "port differs"):
            runtime.load_frontdoor(self.config, 9999)
        data = json.loads(self.config.read_text())
        data["origin"] = "https://fixture.example.ts.net"
        self.config.write_text(json.dumps(data))
        with self.assertRaisesRegex(runtime.RuntimeRefused, ":8443 only"):
            self.plan()

    def test_wrong_node_and_operator_refuse_before_creating_a_route(self):
        config = json.loads(self.config.read_text())
        config["origin"] = "https://someone-else.example.ts.net:8443"
        self.config.write_text(json.dumps(config))
        with self.assertRaisesRegex(runtime.RuntimeRefused, "not this Tailscale node"):
            self.plan()
        config.update(origin=ORIGIN, operator_login="someone@example.test")
        self.config.write_text(json.dumps(config))
        with self.assertRaisesRegex(runtime.RuntimeRefused, "not this node's Tailscale owner"):
            self.plan()
        self.assertEqual(self.status, PUBLIC)

    def test_local_configuration_has_no_tailscale_dependency(self):
        self.config.write_text(json.dumps({"port": 8767}))
        self.assertIsNone(runtime.load_frontdoor(self.config, 8767))
        self.assertEqual(self.calls, [])

    def test_local_only_install_never_calls_tailscale(self):
        self.config.write_text(json.dumps({"port": 8767}))
        result = self.install(self.plan())
        self.assertEqual(result["origin"], "http://127.0.0.1:8767")
        self.assertEqual(self.status, PUBLIC)
        self.assertFalse(any(call[0] == "tailscale" for call in self.calls))

    def test_an_install_without_sd_or_the_vault_says_so_and_succeeds(self):
        import contextlib
        import io

        said = io.StringIO()
        with QuickNoteEnvironment.which(self, None), patch.dict(os.environ, {}), contextlib.redirect_stderr(said):
            os.environ.pop("OBSIDIAN_VAULT", None)
            result = self.install(self.plan())
        self.assertEqual(result["origin"], ORIGIN)
        self.assertEqual(said.getvalue().splitlines(), [
            "dashboard: sd is not on the installing shell's PATH; Notes cannot list or keep quick notes",
            "dashboard: OBSIDIAN_VAULT is not set; Notes cannot list or keep quick notes"])

    def test_production_path_resolves_the_platform_listener_inspector(self):
        self.assertIsNotNone(shutil.which("lsof", path=runtime.LAUNCH_PATH),
                             "the production LaunchAgent must find the installed lsof")

    def test_runtime_refuses_a_source_library_before_opening_a_database(self):
        import sd_db

        with patch.object(sd_db, "__file__", "/source/local-sd-db/sd_db/__init__.py"):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "installed build"):
                REAL_LIBRARY_CHECK(self.home / "missing.db")
        self.assertFalse((self.home / "missing.db").exists())

    def test_concurrent_route_change_is_preserved_during_rollback(self):
        def concurrent_change(*args, **kwargs):
            self.status["Web"][AUTHORITY]["Handlers"]["/new"] = {"Proxy": "http://127.0.0.1:9999"}
            raise runtime.RuntimeRefused("health failed")
        with patch.object(runtime, "health_check", side_effect=concurrent_change):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "rollback requires attention"):
                self.install(self.plan())
        self.assertIn("/new", self.status["Web"][AUTHORITY]["Handlers"])
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(runtime._without_dashboard(self.status, AUTHORITY), PUBLIC)

    def test_production_launcher_ignores_source_pythonpath(self):
        # Execute the real shell and isolated bootstrap. A poison stdlib/module
        # on PYTHONPATH must not run even when the caller explicitly offers it.
        poison = self.home / "poison"
        poison.mkdir()
        marker = self.home / "poison-ran"
        (poison / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('unsafe')\n")
        result = subprocess.run([str(runtime.HERE / "dashboard.sh"), "install", "--help"],
                                env={**os.environ, "SD_DASHBOARD_PYTHON": sys.executable,
                                     "PYTHONPATH": str(poison)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("expected-fingerprint", result.stdout)
        self.assertFalse(marker.exists())

    def test_the_vault_probe_child_is_isolated_too(self):
        # `-I` on the exec isolates the interpreter that loads sd_tile.py and
        # nothing it starts. The probe is a second process, and it is the one
        # whose Full Disk Access is being reported, so a caller's PYTHONPATH
        # reaching it decides the answer: a sitecustomize runs before the -c
        # body and can print, raise, or read the vault in its place.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_probe_collectors", runtime.HERE / "collectors.py")
        collectors = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collectors)

        poison = self.home / "probe-poison"
        poison.mkdir()
        marker = self.home / "probe-poison-ran"
        (poison / "sitecustomize.py").write_text(
            f"open({str(marker)!r}, 'w').write('unsafe')\nprint('denied')\n")
        listed = self.home / "listed"
        listed.mkdir()

        with patch.object(collectors, "VAULT", listed), \
                patch.dict(os.environ, {"PYTHONPATH": str(poison)}):
            answer = collectors.probe_vault(sys.executable)

        self.assertFalse(marker.exists())
        self.assertEqual(answer, "ok")

    def test_the_help_names_the_resource_views_the_code_has(self):
        # Enumerated from reports_screen.VIEWS, not from a list written here,
        # so a view added or dropped tomorrow fails this without an edit. The
        # help said "the first five" of a six-name list, which took in `ports`
        # -- which has no view -- and left out `queues`, which has one.
        import ast
        source = (runtime.HERE / "sd_dashboard" / "reports_screen.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        views = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "VIEWS" for t in node.targets):
                views = [pair.elts[0].value for pair in node.value.elts]
        self.assertTrue(views, "VIEWS not found in reports_screen.py")

        help_text = subprocess.run(
            [str(runtime.HERE / "dashboard.sh"), "--help"],
            capture_output=True, text=True).stdout
        line = [b for b in help_text.split("\n\n") if "Resources views" in b]
        self.assertTrue(line, help_text)
        sentence = " ".join(line[0].split())
        for name in views:
            self.assertIn(name, sentence, sentence)
        self.assertNotIn("first five", sentence, sentence)

    def test_every_runtime_python_exec_is_isolated(self):
        # Enumerated from the script, not from a list written here: a
        # subcommand added tomorrow is covered without anyone editing this
        # test. `grants` was the entrypoint that shipped without -I, so a
        # caller's PYTHONPATH decided what its Full Disk Access probe ran.
        script = (runtime.HERE / "dashboard.sh").read_text(encoding="utf-8")
        execs = [line.strip() for line in script.splitlines()
                 if line.strip().startswith('exec "$runtime_python"')]
        self.assertTrue(execs, "no $runtime_python exec found in dashboard.sh")
        for line in execs:
            self.assertIn(' -I ', line, line)


class BuildIdentity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dashboard-build-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "sd_db"
        self.dashboard = self.root / "dashboard"
        self.library.mkdir()
        (self.library / "__init__.py").write_text("# package\n")
        (self.dashboard / "sd_dashboard/static").mkdir(parents=True)
        (self.dashboard / "dashboard.sh").write_text("#!/bin/sh\n")
        (self.dashboard / "sd_dashboard/server.py").write_text("# server\n")
        (self.dashboard / "sd_dashboard/bootstrap.py").write_text("# bootstrap\n")
        (self.dashboard / "sd_dashboard/static/page.css").write_text("body {}\n")

    def digests(self):
        return runtime.build_digests(library_root=self.library, dashboard_root=self.dashboard)

    def test_enumerates_added_package_data_static_assets_and_launcher(self):
        before = self.digests()
        additions = ((self.library / "future-migration.sql", "library_digest"),
                     (self.library / "new-module.py", "library_digest"),
                     (self.dashboard / "sd_dashboard/static/new.svg", "dashboard_digest"))
        for path, key in additions:
            previous = self.digests()
            path.write_text("new file\n")
            self.assertNotEqual(self.digests()[key], previous[key])
        for path in (self.dashboard / "dashboard.sh", self.dashboard / "sd_dashboard/bootstrap.py"):
            previous = self.digests()
            path.write_text(path.read_text() + "# changed\n")
            self.assertNotEqual(self.digests()["dashboard_digest"], previous["dashboard_digest"])
        self.assertNotEqual(self.digests(), before)

    def test_an_absent_launcher_is_recorded_rather_than_refused(self):
        """An installed copy has no `dashboard.sh` beside it.

        Requiring one meant `server.build` raised RuntimeRefused from
        site-packages, so the package could be installed but not started.
        """
        before = self.digests()
        (self.dashboard / "dashboard.sh").unlink()
        after = runtime.build_digests(library_root=self.library, dashboard_root=self.dashboard)
        self.assertNotEqual(after["dashboard_digest"], before["dashboard_digest"])

    def test_a_launcher_that_returns_changes_the_identity_back(self):
        """Absent is a recorded state, not a skipped one: removing the launcher
        and restoring identical bytes must return the same digest."""
        before = self.digests()
        body = (self.dashboard / "dashboard.sh").read_text()
        (self.dashboard / "dashboard.sh").unlink()
        without = self.digests()
        (self.dashboard / "dashboard.sh").write_text(body)
        self.assertEqual(self.digests(), before)
        self.assertNotEqual(without, before)

    def test_ignores_generated_bytecode_and_rejects_linked_sources(self):
        before = self.digests()
        (self.library / "__pycache__").mkdir()
        (self.library / "__pycache__/generated.pyc").write_bytes(b"bytecode")
        (self.dashboard / "sd_dashboard/generated.pyo").write_bytes(b"bytecode")
        self.assertEqual(self.digests(), before)
        (self.library / "linked.py").symlink_to(self.dashboard / "sd_dashboard/server.py")
        with self.assertRaisesRegex(runtime.RuntimeRefused, "regular file"):
            self.digests()

    def test_collector_dependency_addition_replacement_and_removal_change_build_identity(self):
        before = self.digests()  # Minimal fixtures intentionally omit collectors.
        for relative in ("collectors.py", "../local-machine-setup/machine-setup.sh"):
            with self.subTest(dependency=relative):
                path = self.dashboard / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# collector v1\n")
                added = self.digests()
                self.assertNotEqual(added["dashboard_digest"], before["dashboard_digest"])
                self.assertEqual(added["library_digest"], before["library_digest"])
                path.write_text("# collector v2\n")
                self.assertNotEqual(self.digests()["dashboard_digest"], added["dashboard_digest"])
                path.unlink()
                self.assertEqual(self.digests(), before)

    def test_health_keeps_startup_digests_and_refuses_changed_disk_build(self):
        from sd_db.migrate import initialise
        from sd_dashboard import server

        database = self.root / "sd.db"
        initialise(database)
        original = self.digests()
        with patch.object(runtime, "build_digests", return_value=original):
            listening = server.build(database, port=0)
        thread = threading.Thread(target=listening.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(listening.server_close)
        self.addCleanup(listening.shutdown)
        def health():
            client = http.client.HTTPConnection(*listening.server_address[:2], timeout=5)
            try:
                client.request("GET", "/health")
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()
        with patch.object(runtime, "build_digests", return_value=original):
            status, body = health()
            self.assertEqual(status, 200)
            self.assertFalse(body["code_changed"])
        changed = dict(original, library_digest="0" * 64)
        with patch.object(runtime, "build_digests", return_value=changed):
            status, body = health()
            self.assertEqual(status, 503)
            self.assertFalse(body["ok"])
            self.assertTrue(body["code_changed"])
            self.assertEqual(body["library_digest"], original["library_digest"])
            self.assertEqual(body["dashboard_digest"], original["dashboard_digest"])


class LibraryLag(unittest.TestCase):
    """`installed_library` refuses an installed sd_db older than the checkout's library (the #395 class).

    The checkout is a temporary git repository with two commits touching
    `local-sd-db/sd_db`; the installed build is a fake distribution whose
    `direct_url.json` names one of them, as pip writes it for a VCS install.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dashboard-library-lag-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.checkout = self.home / "system"
        (self.checkout / "local-sd-db/sd_db").mkdir(parents=True)
        # The dashboard reads the checkout it sits in; `build_digests` reads
        # this package from the same root, so the fixture carries one file.
        (self.checkout / "local-project-dashboard/sd_dashboard").mkdir(parents=True)
        (self.checkout / "local-project-dashboard/sd_dashboard/__init__.py").write_text("")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "fixture@example.test")
        self.git("config", "user.name", "Fixture")
        self.first = self.library_commit("# first library\n")
        self.second = self.library_commit("# second library\n")
        prefix = self.home / "venv"
        package = prefix / "lib/python3.13/site-packages/sd_db"
        package.mkdir(parents=True)
        source = package / "__init__.py"
        source.write_text("# installed build\n")
        self.database = self.home / "sd.db"
        from sd_db.migrate import initialise
        initialise(self.database)
        import sd_db
        self.addCleanup(patch.stopall)
        patch.object(runtime, "HERE", self.checkout / "local-project-dashboard").start()
        patch.object(sd_db, "__file__", str(source)).start()
        patch.object(sys, "prefix", str(prefix)).start()
        self.direct_url = None
        patch("importlib.metadata.distribution", side_effect=self.distribution).start()

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.checkout), *arguments], check=True, capture_output=True, text=True).stdout.strip()

    def library_commit(self, text):
        (self.checkout / "local-sd-db/sd_db/__init__.py").write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "library")
        return self.git("rev-parse", "HEAD")

    def distribution(self, name):
        self.assertEqual(name, "sd-db")
        direct_url = self.direct_url

        class Distribution:
            @staticmethod
            def read_text(filename):
                return direct_url if filename == "direct_url.json" else None
        return Distribution()

    def vcs_install(self, commit):
        self.direct_url = json.dumps({"subdirectory": "local-sd-db", "url": "file:///fixture/system",
                                      "vcs_info": {"commit_id": commit, "requested_revision": commit, "vcs": "git"}})

    def check(self):
        return runtime.installed_library(self.database)

    def test_stale_library_is_refused_naming_both_commits(self):
        self.vcs_install(self.first)
        with self.assertRaisesRegex(runtime.RuntimeRefused, "lacks the checkout's library commit") as refused:
            self.check()
        message = str(refused.exception)
        self.assertIn(self.first, message)
        self.assertIn(self.second, message)
        self.assertIn("make setup", message)

    def test_library_from_a_branch_without_the_commit_is_refused(self):
        # A valid commit of this checkout that never took the second library
        # change: not older, but the pages may still call a name it lacks.
        self.git("checkout", "-q", "-b", "elsewhere", self.first)
        divergent = self.library_commit("# divergent library\n")
        self.git("checkout", "-q", "main")
        self.vcs_install(divergent)
        with self.assertRaisesRegex(runtime.RuntimeRefused, "lacks the checkout's library commit") as refused:
            self.check()
        self.assertIn(divergent, str(refused.exception))
        self.assertIn(self.second, str(refused.exception))

    def test_current_library_passes_and_is_recorded(self):
        self.vcs_install(self.second)
        self.assertEqual(self.check()["library_commit"], self.second)
        # A later commit elsewhere in the checkout does not move the library.
        (self.checkout / "README.md").write_text("unrelated\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "unrelated")
        self.assertEqual(self.check()["library_commit"], self.second)

    def test_path_install_without_vcs_info_skips(self):
        self.direct_url = json.dumps({"url": "file:///fixture/system/local-sd-db", "dir_info": {}})
        with patch.object(runtime, "_run", side_effect=AssertionError("git must not run for a path install")):
            self.assertIsNone(self.check()["library_commit"])

    def test_no_distribution_or_no_direct_url_skips(self):
        import importlib.metadata
        with patch("importlib.metadata.distribution", side_effect=importlib.metadata.PackageNotFoundError("sd-db")):
            self.assertIsNone(self.check()["library_commit"])
        self.direct_url = None
        self.assertIsNone(self.check()["library_commit"])

    def test_without_git_or_outside_a_repository_skips(self):
        self.vcs_install(self.first)
        with patch("shutil.which", return_value=None):
            self.assertIsNone(self.check()["library_commit"])
        shutil.rmtree(self.checkout / ".git")
        self.assertIsNone(self.check()["library_commit"])

    def test_unknown_installed_commit_is_refused(self):
        self.vcs_install("0" * 40)
        with self.assertRaisesRegex(runtime.RuntimeRefused, "not a commit of this checkout"):
            self.check()


class LaunchEnvironment(unittest.TestCase):
    """The LaunchAgent carries the installing shell's personal settings."""

    def test_set_values_pass_through_and_unset_ones_stay_out(self):
        values = {"VAULT": "/vaults/My Vault", "REPO_ROOT": "/checkouts"}
        with patch.dict(os.environ, values, clear=False):
            os.environ.pop("SYSTEM_TOOLS_LABEL_PREFIX", None)
            environment = runtime._launch_environment()
        self.assertEqual(environment["VAULT"], "/vaults/My Vault")
        self.assertEqual(environment["REPO_ROOT"], "/checkouts")
        self.assertNotIn("SYSTEM_TOOLS_LABEL_PREFIX", environment)
        self.assertEqual(environment["SD_DASHBOARD_PYTHON"], sys.executable)

    def test_the_config_directory_passes_through(self):
        values = {"SYSTEM_TOOLS_CONFIG": "/config/system", "CRON_JOBS_EXTRA_DIRS": "/more/jobs"}
        with patch.dict(os.environ, values, clear=False):
            environment = runtime._launch_environment()
        self.assertEqual(environment["SYSTEM_TOOLS_CONFIG"], "/config/system")
        self.assertEqual(environment["CRON_JOBS_EXTRA_DIRS"], "/more/jobs")

    def test_the_job_triage_stage_switch_passes_through(self):
        # Only the installing shell or <config>/project-dashboard/.env can switch
        # the failed-job Jev shadow off for the server (sd:2095).
        with patch.dict(os.environ, {"JEV_JOB_TRIAGE": "off"}, clear=False):
            environment = runtime._launch_environment()
        self.assertEqual(environment["JEV_JOB_TRIAGE"], "off")


class QuickNoteEnvironment(unittest.TestCase):
    """sd:2549: Notes runs `sd store`, so the LaunchAgent gets sd's directory and OBSIDIAN_VAULT from the installing shell."""

    def which(self, found):
        real = shutil.which
        return patch.object(runtime.shutil, "which", side_effect=lambda name, path=None: found if name == "sd" else real(name, path=path))

    def test_sd_s_directory_joins_the_path_and_the_vault_passes_through(self):
        with self.which("/opt/example/bin/sd"), patch.dict(os.environ, {"OBSIDIAN_VAULT": "/vaults/Example"}):
            environment = runtime._launch_environment()
        self.assertEqual(environment["PATH"], runtime.LAUNCH_PATH + ":/opt/example/bin")
        self.assertEqual(environment["OBSIDIAN_VAULT"], "/vaults/Example")

    def test_without_them_the_path_is_unchanged_and_each_gap_is_named(self):
        with self.which(None), patch.dict(os.environ, {}):
            os.environ.pop("OBSIDIAN_VAULT", None)
            environment = runtime._launch_environment()
            gaps = runtime.launch_gaps(environment)
        self.assertEqual(environment["PATH"], runtime.LAUNCH_PATH)
        self.assertNotIn("OBSIDIAN_VAULT", environment)
        self.assertEqual(gaps, ["sd is not on the installing shell's PATH", "OBSIDIAN_VAULT is not set"])

class HealthWait(unittest.TestCase):
    """A clock bounds the wait: a refused connection returns at once (sd:2811)."""

    def poll(self, refusals, **wait):
        now, tries = [0.0], []

        class Response:
            status = 200

            def read(self):
                return b'{"ok": true, "service": "sd-dashboard"}'

        class Connection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args):
                tries.append(now[0])
                if len(tries) <= refusals:
                    raise ConnectionRefusedError

            def getresponse(self):
                return Response()

            def close(self):
                pass

        def sleep(seconds):
            now[0] += seconds

        with patch.object(runtime.http.client, "HTTPConnection", Connection), \
                patch.object(runtime.time, "monotonic", lambda: now[0]), \
                patch.object(runtime.time, "sleep", sleep):
            try:
                return runtime.health_check(8767, **wait), tries
            except runtime.RuntimeRefused as refusal:
                return refusal, tries

    def test_a_start_slower_than_twenty_refusals_is_still_healthy(self):
        result, tries = self.poll(40)
        self.assertEqual(result, {"ok": True, "service": "sd-dashboard"})
        self.assertEqual(len(tries), 41)

    def test_the_wait_ends_on_the_clock(self):
        result, tries = self.poll(10_000)
        self.assertIn("did not return its healthy /health response within 30s", str(result))
        self.assertGreaterEqual(tries[-1], 30)
        self.assertLess(tries[-1], 30.5)

    def test_health_refuses_a_wait_that_never_ends(self):
        for value in ("nan", "inf", "-1"):
            with self.subTest(wait=value), patch.object(sys, "argv", ["runtime", "health", "--wait", value]), \
                    patch.object(runtime, "health_check") as health, patch("sys.stderr"):
                with self.assertRaises(SystemExit) as exit:
                    runtime.main()
                self.assertEqual(exit.exception.code, 2)
                health.assert_not_called()

    def test_no_wait_tries_once(self):
        result, tries = self.poll(1, wait=0)
        self.assertIsInstance(result, runtime.RuntimeRefused)
        self.assertEqual(tries, [0.0])


if __name__ == "__main__":
    unittest.main()
