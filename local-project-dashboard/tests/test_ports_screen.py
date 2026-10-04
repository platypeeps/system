"""Existing candidates discovery with fake docker/lsof, plus read-only rendering."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_dashboard.ports_screen import port_rows, ports_panel

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ports_test_collectors", HERE / "collectors.py")
collectors = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collectors)


# `machine-setup.sh candidates service` output, recorded against the
# PortInventory fixtures below (three local-* services, fake docker, lsof and
# netstat). Keyed by the fixture variable that selects each failure mode.
RECORDED_CANDIDATES = {
    '': (
        'candidates : what this machine could run, and what the profile already lists\n'
        'profile    : none recorded — comparing against common only\n'
        'read-only  : nothing here writes, with or without --apply\n'
        '\n'
        '== service\n'
        '  + local-alpha                      9101 9103                                running\n'
        '  . local-beta                       9101 9102                                stopped\n'
        '  . local-empty                                                               stopped\n'
        '  ---     3 startable, 1 in this profile   (+ listed, . available)\n'
        '  CLASH   port 9101 wanted by alpha beta — list at most one\n'
        '  LISTEN  port 9101 held by fixture-server (pid 321)\n'
        '  BUSY    port 9101 held by fixture-server (pid 321) — alpha beta cannot bind it\n'
        '  CLEAR   port 9102 no TCP listener observed\n'
        '  CLEAR   port 9103 no TCP listener observed\n'
        '  add by hand: profiles/<profile>.service  (.service is never auto-captured)\n'
        '\n'
    ),
    'PORT_TEST_LSOF_ERROR': (
        'candidates : what this machine could run, and what the profile already lists\n'
        'profile    : none recorded — comparing against common only\n'
        'read-only  : nothing here writes, with or without --apply\n'
        '\n'
        '== service\n'
        '  + local-alpha                      9101 9103                                running\n'
        '  . local-beta                       9101 9102                                stopped\n'
        '  . local-empty                                                               stopped\n'
        '  ---     3 startable, 1 in this profile   (+ listed, . available)\n'
        '  CLASH   port 9101 wanted by alpha beta — list at most one\n'
        '  LISTEN  port 9101 held by fixture-server (pid 321)\n'
        '  BUSY    port 9101 held by fixture-server (pid 321) — alpha beta cannot bind it\n'
        '  UNKNOWN port 9102 listener inspection unavailable\n'
        '  UNKNOWN port 9103 listener inspection unavailable\n'
        '  add by hand: profiles/<profile>.service  (.service is never auto-captured)\n'
        '\n'
    ),
    'PORT_TEST_NETSTAT_HIDDEN': (
        'candidates : what this machine could run, and what the profile already lists\n'
        'profile    : none recorded — comparing against common only\n'
        'read-only  : nothing here writes, with or without --apply\n'
        '\n'
        '== service\n'
        '  + local-alpha                      9101 9103                                running\n'
        '  . local-beta                       9101 9102                                stopped\n'
        '  . local-empty                                                               stopped\n'
        '  ---     3 startable, 1 in this profile   (+ listed, . available)\n'
        '  CLASH   port 9101 wanted by alpha beta — list at most one\n'
        '  LISTEN  port 9101 held by fixture-server (pid 321)\n'
        '  BUSY    port 9101 held by fixture-server (pid 321) — alpha beta cannot bind it\n'
        '  UNKNOWN port 9102 listener inspection unavailable\n'
        '  CLEAR   port 9103 no TCP listener observed\n'
        '  add by hand: profiles/<profile>.service  (.service is never auto-captured)\n'
        '\n'
    ),
    'PORT_TEST_DOCKER_FAIL': (
        'candidates : what this machine could run, and what the profile already lists\n'
        'profile    : none recorded — comparing against common only\n'
        'read-only  : nothing here writes, with or without --apply\n'
        '\n'
        '== service\n'
        '  + local-alpha                      9101 9103                                unknown\n'
        '  . local-beta                       9101 9102                                unknown\n'
        '  . local-empty                                                               unknown\n'
        '  ---     3 startable, 1 in this profile   (+ listed, . available)\n'
        '  CLASH   port 9101 wanted by alpha beta — list at most one\n'
        '  LISTEN  port 9101 held by fixture-server (pid 321)\n'
        '  BUSY    port 9101 held by fixture-server (pid 321) — alpha beta cannot bind it\n'
        '  CLEAR   port 9102 no TCP listener observed\n'
        '  CLEAR   port 9103 no TCP listener observed\n'
        '  add by hand: profiles/<profile>.service  (.service is never auto-captured)\n'
        '\n'
    ),
}


class PortInventory(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ports-inventory-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # machine-setup.sh does not ship in this repository, so a stand-in
        # prints what it printed for exactly these fixtures, per failure mode.
        self.machine = self.root / "local-machine-setup/machine-setup.sh"
        self.machine.parent.mkdir()
        self.machine.write_text(f"#!{sys.executable}\n" +
            "import json, os, sys\n"
            "assert sys.argv[1:] == ['candidates', 'service'], sys.argv\n"
            f"recorded = json.loads({json.dumps(RECORDED_CANDIDATES)!r})\n"
            "mode = next((name for name in recorded if name and os.environ.get(name) == '1'), '')\n"
            "sys.stdout.write(recorded[mode])\n")
        self.machine.chmod(0o755)
        (self.machine.parent / "profiles").mkdir()
        (self.machine.parent / "profiles/common.service").write_text("local-alpha\n")
        for name, ports in (("alpha", [9101, 9103]), ("beta", [9101, 9102]), ("empty", [])):
            folder = self.root / f"local-{name}"
            folder.mkdir()
            script = folder / f"{name}.sh"
            script.write_text('#!/bin/sh\ncase "$1" in\nstart) docker run ' +
                              " ".join(f"-p {port}:80" for port in ports) + ' fixture ;;\nesac\n')
            script.chmod(0o755)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text('#!/bin/sh\n[ "$1" = "ps" ] || exit 91\n'
                          '[ "${PORT_TEST_DOCKER_FAIL:-}" != "1" ] || { echo "SECRET token" >&2; exit 1; }\n'
                          'printf "alpha\\n"\n')
        docker.chmod(0o755)
        lsof = self.bin / "lsof"
        # One scan for the whole table (sd:756), as lsof prints it: 9101 is
        # held, the others are not. With PORT_TEST_LSOF_ERROR lsof also
        # reports an error, which keeps the listener and clears no port.
        lsof.write_text(f"#!{sys.executable}\n" +
            "import os, sys\n"
            "assert sys.argv[1:]==['-w','-nP','-iTCP','-sTCP:LISTEN']\n"
            "print('COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME')\n"
            "print('fixture-server 321 user 3u IPv4 1 0t0 TCP *:9101 (LISTEN)')\n"
            "if os.environ.get('PORT_TEST_LSOF_ERROR')=='1':\n"
            "    print('lsof error SECRET token',file=sys.stderr)\n")
        lsof.chmod(0o755)
        # netstat agrees about 9101. With PORT_TEST_NETSTAT_HIDDEN it also
        # lists 9102, a listener lsof cannot see (another user's socket).
        netstat = self.bin / "netstat"
        netstat.write_text('#!/bin/sh\n[ "$*" = "-an -p tcp" ] || exit 91\n'
                           'echo "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)"\n'
                           'echo "tcp4       0      0  *.9101                 *.*                    LISTEN"\n'
                           '[ "${PORT_TEST_NETSTAT_HIDDEN:-}" != "1" ] || '
                           'echo "tcp4       0      0  *.9102                 *.*                    LISTEN"\n')
        netstat.chmod(0o755)
        self.environment = {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                            "HOME": str(self.root), "MACHINE_SETUP_STATE": str(self.root / "state")}

    def collect(self, **extra):
        result = subprocess.run([str(self.machine), "candidates", "service"],
            env={**self.environment, **extra}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("SECRET", result.stdout)
        return result.stdout, collectors.parse_ports(result.stdout)

    def test_real_candidate_output_preserves_inventory_and_explicit_listener_states(self):
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        output, snapshot = self.collect()
        self.assertTrue(snapshot["complete"], output)
        self.assertEqual([row["name"] for row in snapshot["services"]], ["local-alpha", "local-beta", "local-empty"])
        self.assertEqual(snapshot["listeners"]["9101"]["state"], "listening")
        self.assertEqual(snapshot["listeners"]["9102"]["state"], "not_listening")
        self.assertEqual(snapshot["listeners"]["9103"]["state"], "not_listening")
        self.assertEqual(snapshot["listeners"]["9101"]["pid"], 321)
        self.assertEqual((snapshot["clash"], snapshot["busy"]), (2, 2))
        self.assertEqual(snapshot["services"][0]["state"], "running")
        self.assertEqual(snapshot["services"][1]["state"], "stopped")
        self.assertEqual(snapshot["services"][2]["ports"], [])
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_an_lsof_error_keeps_the_listener_and_clears_no_port(self):
        _, snapshot = self.collect(PORT_TEST_LSOF_ERROR="1")
        self.assertEqual(snapshot["listeners"]["9101"]["state"], "listening")
        self.assertEqual(snapshot["listeners"]["9101"]["pid"], 321)
        self.assertEqual(snapshot["listeners"]["9102"]["state"], "unknown")
        self.assertEqual(snapshot["listeners"]["9103"]["state"], "unknown")

    def test_a_listener_lsof_cannot_see_is_unknown_not_free(self):
        _, snapshot = self.collect(PORT_TEST_NETSTAT_HIDDEN="1")
        self.assertEqual(snapshot["listeners"]["9101"]["state"], "listening")
        self.assertEqual(snapshot["listeners"]["9102"]["state"], "unknown")
        self.assertEqual(snapshot["listeners"]["9103"]["state"], "not_listening")

    def test_unavailable_docker_is_unknown_not_stopped(self):
        _, snapshot = self.collect(PORT_TEST_DOCKER_FAIL="1")
        self.assertTrue(all(row["state"] == "unknown" for row in snapshot["services"]))
        self.assertNotIn("SECRET", json.dumps(snapshot))

    def test_render_separates_observation_from_configuration_and_container_state(self):
        # One scan answers every port alike, so a clean scan shows the free
        # ports and a scan that reported an error shows them unknown.
        html = ""
        for extra in ({}, {"PORT_TEST_LSOF_ERROR": "1"}):
            _, snapshot = self.collect(**extra)
            snapshot["environment"] = {"API_TOKEN": "SECRET token"}
            html += str(ports_panel(object(), now="2026-09-08T12:00:00Z", backend=lambda: snapshot))
        for text in ("Listening", "No listener observed", "Unknown", "No port configured",
                     "In profile", "Available", "Running", "Stopped", "fixture-server", "pid 321",
                     "overlap is configured", "not been matched to this container", "Sources and scope"):
            self.assertIn(text, html)
        self.assertNotIn("SECRET", html)
        self.assertNotIn("API_TOKEN", html)
        self.assertNotIn('method="post"', html)
        self.assertNotIn("/api/", html)

    def test_filter_preserves_ports_subtab(self):
        _, snapshot = self.collect()
        html = str(ports_panel(now="today", backend=lambda: snapshot, parameters={"q": ["9102"]}))
        self.assertIn('name="area" value="ports"', html)
        self.assertIn("local-beta", html)
        self.assertNotIn("local-alpha", html)


class PortParser(unittest.TestCase):
    def legacy(self, names):
        return ("== service\n"
                "  + local-alpha                      9000                                     stopped\n"
                "  . local-beta                       9000                                     running\n"
                "  ---     2 startable, 1 in this profile   (+ listed, . available)\n"
                f"  CLASH   port 9000 wanted by {names} — list at most one\n"
                f"  BUSY    port 9000 held by postgres (pid 32) — {names} cannot bind it\n"
                "  add by hand: profiles/common.service\n")

    def test_conflicts_match_ports_with_both_historical_name_forms(self):
        for names in ("alpha beta", "local-alpha local-beta"):
            snapshot = collectors.parse_ports(self.legacy(names))
            self.assertEqual((snapshot["clash"], snapshot["busy"]), (2, 2))
            self.assertTrue(snapshot["complete"])
            self.assertEqual(snapshot["listeners"]["9000"]["source"], "lsof-legacy")

    def test_missing_evidence_is_unknown_and_truncated_inventory_is_partial(self):
        output = self.legacy("alpha beta")
        output = "\n".join(line for line in output.splitlines() if "BUSY" not in line and "add by hand" not in line)
        snapshot = collectors.parse_ports(output)
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["listeners"]["9000"]["state"], "unknown")
        html = str(ports_panel(now="today", backend=lambda: snapshot))
        self.assertIn("incomplete", html)
        self.assertNotIn("No listener observed", html)

    def test_bad_or_contradictory_observations_never_claim_clear(self):
        output = self.legacy("alpha beta") + "  CLEAR port 9000 no TCP listener observed\n"
        snapshot = collectors.parse_ports(output)
        self.assertEqual(snapshot["listeners"]["9000"]["state"], "unknown")
        snapshot = collectors.parse_ports("  LISTEN port 99 SECRET raw output\n")
        self.assertEqual(snapshot["listeners"]["99"]["state"], "unknown")
        self.assertNotIn("SECRET", json.dumps(snapshot))

    def test_unavailable_collector_does_not_claim_zero_healthy_ports(self):
        for backend in (lambda: collectors.parse_ports(""), lambda: None):
            html = str(ports_panel(now="today", backend=backend))
            self.assertIn("Port inventory is unavailable", html)
            self.assertNotIn("0 configured ports", html)

    def test_unproven_listener_claims_are_unknown_and_values_are_escaped(self):
        snapshot = {"services": [{"name": "local-<script>", "ports": ["9000"], "state": "running", "mark": "+"}],
                    "listeners": {"9000": {"state": "listening", "source": "guess"}}, "complete": True}
        self.assertEqual(port_rows(snapshot)[0]["listener"], "unknown")
        html = str(ports_panel(now="today", backend=lambda: snapshot))
        self.assertIn("local-&lt;script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_aggregate_listener_failure_is_unknown_without_exposing_error(self):
        calls = []
        def unavailable(arguments, **options):
            calls.append((arguments, options))
            return subprocess.CompletedProcess(arguments, 1, "", "SECRET credentials unavailable")
        result = collectors.collect_tcp_listeners(runner=unavailable)
        self.assertEqual(result, {"ports": {}, "complete": False})
        self.assertEqual(calls[0][0], ["lsof", "-w", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"])
        self.assertEqual(calls[0][1]["timeout"], 3)
        self.assertNotIn("SECRET", json.dumps(result))

    def test_aggregate_snapshot_preserves_all_owners_and_addresses_without_duplicates(self):
        output = ("p100\ncPython\nf3\nn127.0.0.1:8767\nf4\nn[::1]:8767\n"
                  "f5\nn127.0.0.1:8767\np200\ncOther\nf8\nn*:8767\n"
                  "p300\nf9\nn127.0.0.1:9999\n")
        observed = collectors.parse_tcp_listeners(output)
        self.assertTrue(observed["complete"])
        self.assertEqual(len(observed["ports"]["8767"]), 3)
        self.assertIsNone(observed["ports"]["9999"][0]["command"])
        snapshot = {"services": [], "listeners": {}, "complete": True, "observed": observed}
        rows = port_rows(snapshot)
        self.assertEqual([row["port"] for row in rows], ["8767", "9999"])
        self.assertFalse(rows[0]["configured"])
        html = str(ports_panel(now="today", backend=lambda: snapshot))
        for value in ("8767", "Python", "Other", "pid 100", "pid 200", "127.0.0.1:8767", "[::1]:8767", "*:8767", "Unknown process", "Observed only"):
            self.assertIn(value, html)
        self.assertIn("Not mapped", html)

    def test_candidate_and_observed_ports_merge_without_implying_process_ownership(self):
        snapshot = collectors.parse_ports(self.legacy("alpha beta"))
        snapshot["observed"] = collectors.parse_tcp_listeners("p22\ncUnrelated\nf3\nn127.0.0.1:9000\n")
        rows = port_rows(snapshot)
        self.assertEqual(len(rows), 1)
        self.assertIn("local-alpha", rows[0]["service"])
        self.assertIn("local-beta", rows[0]["service"])
        self.assertIn("Unrelated", rows[0]["process"])
        self.assertEqual(rows[0]["listener"], "listening")
        self.assertTrue(rows[0]["configured"])
        html = str(ports_panel(now="today", backend=lambda: snapshot))
        self.assertIn("not been matched to this container", html)

    def test_malformed_listener_address_does_not_leak_arbitrary_stdout(self):
        observed = collectors.parse_tcp_listeners("p100\ncPython\nf3\nnSECRET:8767\n")
        self.assertEqual(observed, {"ports": {}, "complete": False})

    def test_complete_empty_match_is_distinct_from_failed_inventory(self):
        observed = collectors.collect_tcp_listeners(runner=self.tables(lsof=(1, ""), netstat=(0, []), warn=True))
        self.assertEqual(observed, {"ports": {}, "complete": True})

    NETSTAT_HEADER = "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)"

    def tables(self, *, lsof, netstat, warn=False, error=False):
        """A runner for collect_tcp_listeners: lsof's (exit, stdout) and netstat's
        (exit, LISTEN ports), where a netstat of None is not installed. With
        `warn`, lsof warns on stderr unless it is given -w, as it does on this
        machine; with `error` it reports an error on stderr even under -w."""
        def run(arguments, **options):
            if arguments[0] == "lsof":
                warning = "" if "-w" in arguments or not warn else "lsof: WARNING: can't stat() apfs file system /private/tmp/x\n"
                warning += "lsof: error reading the socket table\n" if error else ""
                return subprocess.CompletedProcess(arguments, lsof[0], lsof[1], warning)
            self.assertEqual(arguments, ["netstat", "-an", "-p", "tcp"])
            self.assertEqual(options["timeout"], 3)
            if netstat is None:
                raise FileNotFoundError("netstat")
            code, ports = netstat
            rows = [self.NETSTAT_HEADER] + [f"tcp4       0      0  {port}                 *.*                    LISTEN"
                                            for port in ports]
            rows.append("tcp4       0      0  10.0.0.5.50001         1.2.3.4.9101           ESTABLISHED")
            return subprocess.CompletedProcess(arguments, code, "Active Internet connections (including servers)\n"
                                               + "\n".join(rows) + "\n", "")
        return run

    def test_an_lsof_warning_does_not_make_the_listener_inventory_incomplete(self):
        # sd:756 follow-up. This machine's lsof warns about a Carbon Copy
        # Cloner snapshot mount on every run; -w silences it.
        observed = collectors.collect_tcp_listeners(
            runner=self.tables(lsof=(0, "p321\ncfixture\nf3\nn*:9101\n"), netstat=(0, ["*.9101", "::1.9101"]), warn=True))
        self.assertEqual(observed, {"ports": {"9101": [{"pid": 321, "command": "fixture", "address": "*:9101"}]},
                                    "complete": True})

    def test_an_lsof_error_under_w_leaves_the_inventory_incomplete(self):
        # review-330 N1: whatever reaches stderr under -w is an error, even
        # when lsof exits 0 with rows and netstat agrees with them.
        observed = collectors.collect_tcp_listeners(
            runner=self.tables(lsof=(0, "p321\ncfixture\nf3\nn*:9101\n"), netstat=(0, ["*.9101"]), error=True))
        self.assertFalse(observed["complete"])
        self.assertEqual(list(observed["ports"]), ["9101"])

    def test_lsof_exit_0_with_nothing_to_show_is_not_complete(self):
        # review-330 N2, as `tcp_listeners` in machine-setup.sh: lsof reports an
        # empty match with exit 1, so exit 0 and no listener is not a clean read.
        observed = collectors.collect_tcp_listeners(runner=self.tables(lsof=(0, ""), netstat=(0, [])))
        self.assertEqual(observed, {"ports": {}, "complete": False})

    def test_a_listener_lsof_cannot_see_leaves_the_inventory_incomplete(self):
        # sd:756 follow-up, review-327 B1 here: root's 445 is in netstat and
        # not in lsof. The inventory cannot be complete, and 9101 stays visible.
        for lsof in ((0, "p321\ncfixture\nf3\nn*:9101\n"), (1, "")):
            with self.subTest(lsof=lsof):
                observed = collectors.collect_tcp_listeners(
                    runner=self.tables(lsof=lsof, netstat=(0, ["*.9101", "*.445"])))
                self.assertFalse(observed["complete"])
                self.assertEqual(list(observed["ports"]), ["9101"] if lsof[0] == 0 else [])

    def test_an_unread_netstat_table_never_proves_completeness(self):
        lsof = (0, "p321\ncfixture\nf3\nn*:9101\n")
        for name, netstat in (("missing", None), ("failed", (1, ["*.9101"])), ("unreadable row", (0, ["*.http"]))):
            with self.subTest(netstat=name):
                observed = collectors.collect_tcp_listeners(runner=self.tables(lsof=lsof, netstat=netstat))
                self.assertFalse(observed["complete"])
                self.assertEqual(list(observed["ports"]), ["9101"])
        # Linux's net-tools reads -p as "show PIDs" and prints another table.
        linux = "Proto Recv-Q Send-Q Local Address Foreign Address State PID/Program name\n" \
                "tcp 0 0 0.0.0.0:9101 0.0.0.0:* LISTEN 321/fixture\n"
        self.assertIsNone(collectors.parse_netstat_listeners(linux))
        self.assertIsNone(collectors.parse_netstat_listeners(""))
        self.assertIsNone(collectors.parse_netstat_listeners("tcp4 0 0 *.9101 *.* LISTEN\n" + self.NETSTAT_HEADER + "\n"))
        self.assertEqual(collectors.parse_netstat_listeners(
            self.NETSTAT_HEADER + "\ntcp46 0 0 *.8443 *.* LISTEN\ntcp6 0 0 fe80::1%lo0.123 *.* LISTEN\n"), {"8443", "123"})


class ChargedClock:
    """`time` for the collectors module, with `charged` seconds added to `monotonic` (sd:2619)."""

    def __init__(self):
        self.charged = 0.0

    def monotonic(self):
        return time.monotonic() + self.charged

    def __getattr__(self, name):
        return getattr(time, name)


class CollectionBudget(unittest.TestCase):
    """sd:722. The budget belongs to the collector, so both callers inherit it.

    Nothing here replaces `collect_ports` itself: the fixtures stand in for the
    command it reads, and each caller is driven through its own entry point --
    `ports_panel` with no backend for the in-process page, `sd_tile.main` for
    the tile path. A caller that stops applying the collector's budget, or a
    collector that stops enforcing it while reading, fails here.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ports-budget-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pids = self.root / "pids"
        spec = importlib.util.spec_from_file_location("ports_budget_collectors", HERE / "collectors.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # An empty match, so a scan that stays inside the budget completes.
        (self.bin / "lsof").write_text("#!/bin/sh\nexit 1\n")
        (self.bin / "lsof").chmod(0o755)
        path = patch.dict(os.environ, {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin"})
        path.start()
        self.addCleanup(path.stop)

    def machine_setup(self, body):
        script = self.root / "machine-setup.sh"
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
        self.module.MACHINE_SETUP = script

    def slow(self):
        # A grandchild holds stdout open too: killing only the direct child
        # would leave the read waiting on the sleep.
        self.machine_setup(f'echo $$ >> "{self.pids}"\nsleep 20 &\necho $! >> "{self.pids}"\nwait\n')

    def chatty(self):
        row = "  + local-alpha                      9000                                     stopped\n"
        count = 1 + 64 * 1024 // len(row)
        self.machine_setup(f"echo '== service'\nyes '{row.rstrip()}' | head -n {count}\n"
                           f"echo '  ---     {count} startable, {count} in this profile'\n"
                           "echo '  add by hand: profiles/common.service'\n")

    def page(self):
        from sd_dashboard import ports_screen
        with patch.object(ports_screen, "_collectors", lambda: self.module):
            started = time.monotonic()
            html = str(ports_panel(now="today"))
            return html, time.monotonic() - started

    def tile(self):
        import sd_tile
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sd_tile, "load_collectors", lambda: self.module), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            started = time.monotonic()
            code = sd_tile.main(["ports"])
            return code, out.getvalue(), err.getvalue(), time.monotonic() - started

    def assert_stopped(self):
        pids = [int(line) for line in self.pids.read_text().split()]
        self.assertEqual(len(pids), 2)
        deadline = time.monotonic() + 3
        alive = pids
        while alive and time.monotonic() < deadline:
            alive = [pid for pid in alive if self.running(pid)]
            time.sleep(0.05)
        self.assertEqual(alive, [], "the over-budget scan was left running")

    @staticmethod
    def running(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        # A zombie has stopped; `ps` says so where kill(0) cannot.
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
        return bool(state.strip()) and not state.strip().startswith("Z")

    def test_declared_ceilings(self):
        # Ports declares the retired contract's 5s (15s until sd:756 made its
        # scan fast); the size ceiling was that contract's 64KB for every
        # collector. The tile path is capped at its 5s, less the margin the
        # tile's own startup and exit need (`TileUnderTheLoader`), so on that
        # path Ports reads within 4s; the assertions below measure that cap.
        import sd_tile
        self.assertEqual(self.module.collect_ports.budget_seconds, 5.0)
        self.assertEqual(self.module.BUDGET_BYTES, 64 * 1024)
        self.assertEqual((sd_tile.TILE_SECONDS, sd_tile.TILE_MARGIN), (5.0, 1.0))
        tile = sd_tile.TILE_SECONDS - sd_tile.TILE_MARGIN
        self.assertEqual(self.module.Budget(5.0, within=tile).seconds, 4.0)
        self.assertEqual(self.module.Budget(1.0, within=tile).seconds, 1.0)
        # A caller cannot raise a declaration, only lower it.
        self.assertEqual(self.module.Budget(5.0, within=60.0).seconds, 5.0)

    def test_page_refuses_a_slow_scan_promptly_and_visibly(self):
        self.module.collect_ports.budget_seconds = 1.0
        self.slow()
        html, elapsed = self.page()
        self.assertLess(elapsed, 1.0 + 3.0, "the page waited on the scan instead of refusing it")
        self.assertIn("exceeded its collection budget", html)
        self.assertIn("1 second", html)
        self.assertNotIn("configured ports", html)
        self.assert_stopped()

    def test_page_refuses_a_chatty_scan_without_rendering_it(self):
        self.chatty()
        html, _ = self.page()
        self.assertIn("exceeded its collection budget", html)
        self.assertIn("64 KB", html)
        self.assertNotIn("local-alpha", html)

    def test_page_renders_a_scan_inside_the_budget(self):
        self.machine_setup("echo '== service'\n"
                           "echo '  + local-alpha                      9000                                     stopped'\n"
                           "echo '  ---     1 startable, 1 in this profile'\n"
                           "echo '  add by hand: profiles/common.service'\n")
        html, _ = self.page()
        self.assertNotIn("exceeded", html)
        self.assertIn("local-alpha", html)

    def test_tile_refuses_a_slow_scan_as_a_failed_tab(self):
        self.module.collect_ports.budget_seconds = 1.0
        self.slow()
        code, out, err, elapsed = self.tile()
        self.assertLess(elapsed, 1.0 + 3.0)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("budget", err)
        self.assert_stopped()

    def test_scan_between_the_tile_and_ports_ceilings(self):
        # The window the tile's margin opens: a scan longer than the tile's
        # effective 4s and shorter than Ports' 5s. Scaled to keep the suite
        # fast -- Ports 5s -> 3s, the tile's effective 4s -> 1s (`TILE_SECONDS`
        # 2s less the real 1s margin), scan 4.5s -> 2s. The tile is refused at its
        # own ceiling; the page waits the scan out and renders.
        import sd_tile
        self.module.collect_ports.budget_seconds = 3.0
        self.machine_setup("sleep 2\n"
                           "echo '== service'\n"
                           "echo '  + local-alpha                      9000                                     stopped'\n"
                           "echo '  ---     1 startable, 1 in this profile'\n"
                           "echo '  add by hand: profiles/common.service'\n")
        with patch.object(sd_tile, "TILE_SECONDS", 2.0):
            code, out, err, elapsed = self.tile()
        self.assertLess(elapsed, 2.0, "the tile read past its 5s outer contract")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("budget of 1 second", err)
        html, elapsed = self.page()
        self.assertGreaterEqual(elapsed, 2.0)
        self.assertNotIn("exceeded", html)
        self.assertIn("local-alpha", html)

    def test_both_commands_spend_one_budget(self):
        # Review N1. Each command alone fits the ceiling and the two together
        # do not, so a collection that gave lsof a budget of its own would
        # render here instead of refusing.
        #
        # sd:2619. machine-setup's share is charged to the collector's clock
        # instead of slept: a real `sleep 1` against a 1.5 s budget overran on
        # its own on a loaded machine, and the refusal named machine-setup.
        # Charged 29.5 of 30 s, it leaves lsof half a second whatever the load,
        # and lsof's real second always overruns that.
        self.module.collect_ports.budget_seconds = 30.0
        self.machine_setup("echo '== service'\n"
                           "echo '  + local-alpha                      9000                                     stopped'\n"
                           "echo '  ---     1 startable, 1 in this profile'\n"
                           "echo '  add by hand: profiles/common.service'\n")
        finished = self.root / "lsof-finished"
        (self.bin / "lsof").write_text(f'#!/bin/sh\nsleep 1\ntouch "{finished}"\nexit 1\n')
        clock = ChargedClock()
        run = self.module.Budget.run

        def charged(budget, argv, **kwargs):
            result = run(budget, argv, **kwargs)
            if Path(str(argv[0])) == self.module.MACHINE_SETUP:
                clock.charged += 29.5
            return result

        with patch.object(self.module, "time", clock), patch.object(self.module.Budget, "run", charged):
            html, _ = self.page()
        self.assertEqual(clock.charged, 29.5, "machine-setup was not read through the budget")
        self.assertIn("exceeded its collection budget", html)
        self.assertIn("lsof ran past its budget of 30 seconds", html)
        # Cut, not waited out: lsof writes its marker only when its second ends.
        self.assertFalse(finished.exists(), "the page waited for lsof instead of stopping it")

    def busy_netstat(self, *, listen, rows=1000):
        """A netstat table over 64KB: `rows` TIME_WAIT connections, then `listen`."""
        table = self.root / "netstat.txt"
        lines = ["Active Internet connections (including servers)",
                 "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)"]
        lines += [f"tcp4       0      0  192.0.2.20.{50000 + n}     203.0.113.{n % 250}.443     TIME_WAIT"
                  for n in range(rows)]
        lines += [f"tcp4       0      0  *.{port}                 *.*                    LISTEN" for port in listen]
        table.write_text("\n".join(lines) + "\n")
        self.assertGreater(table.stat().st_size, 64 * 1024)
        (self.bin / "netstat").write_text(f'#!/bin/sh\ncat "{table}"\n')
        (self.bin / "netstat").chmod(0o755)
        self.machine_setup("echo '== service'\n"
                           "echo '  + local-alpha                      9000                                     stopped'\n"
                           "echo '  ---     1 startable, 1 in this profile'\n"
                           "echo '  add by hand: profiles/common.service'\n")

    # review-330 B1. Only netstat's header and LISTEN rows are held, so a busy
    # machine's connection table cannot refuse the collection, the page or the
    # tile, and a LISTEN row past the first 64KB still counts.
    def test_netstat_connection_rows_do_not_refuse_the_collection(self):
        self.busy_netstat(listen=[])
        self.assertTrue(self.module.collect_ports()["observed"]["complete"])
        self.busy_netstat(listen=["9101"])
        self.assertFalse(self.module.collect_ports()["observed"]["complete"])

    def test_netstat_connection_rows_do_not_refuse_the_page(self):
        self.busy_netstat(listen=[])
        html, _ = self.page()
        self.assertNotIn("exceeded", html)
        self.assertIn("local-alpha", html)

    def test_netstat_connection_rows_do_not_refuse_the_tile(self):
        self.busy_netstat(listen=[])
        code, out, err, _ = self.tile()
        self.assertEqual(code, 0, err)
        self.assertIn("local-alpha", out)

    def test_a_kept_read_holds_whole_kept_lines_including_the_last(self):
        result = self.module.Budget(5.0).run(
            ["/bin/sh", "-c", "printf 'Proto (state)\\ntcp4 *.1 TIME_WAIT\\ntcp4 *.2 LISTEN'"],
            keep=self.module.netstat_kept)
        self.assertEqual(result.stdout, "Proto (state)\ntcp4 *.2 LISTEN")

    def test_netstat_listen_rows_still_spend_the_byte_ceiling(self):
        self.busy_netstat(listen=[str(port) for port in range(10000, 11000)], rows=0)
        html, _ = self.page()
        self.assertIn("netstat wrote more than its budget of 64 KB", html)

    def test_size_ceiling_is_applied_while_reading(self):
        # Review N2. A scan that never stops writing, under a time ceiling far
        # longer than it takes to write 64KB: only a ceiling checked as bytes
        # arrive refuses it promptly. Checked afterwards, it waits the whole
        # time ceiling out and then reports time rather than size.
        self.machine_setup("exec yes '  + local-alpha                      9000                                     stopped'\n")
        html, elapsed = self.page()
        self.assertLess(elapsed, 3.0, "the size ceiling waited for the scan to finish")
        self.assertIn("wrote more than its budget of 64 KB", html)

    def test_tile_refuses_a_chatty_scan_as_a_failed_tab(self):
        self.chatty()
        code, out, err, _ = self.tile()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("budget", err)


class TileUnderTheLoader(unittest.TestCase):
    """sd:722 review B1. The tile's refusal had to beat the pack's own kill.

    The pack started its clock once `Popen` returned for `dashboard.sh tile`;
    the collector starts its budget only after the shell, the interpreter and
    the imports. A tile that read within the full five seconds therefore lost
    every time: the loader killed the process group first, with no reason, and
    the scan -- in a session of its own -- outlived the kill. That loader is
    gone and the margin it forced is still in `sd_tile.py`, so this keeps the
    margin honest: it drives a checkout-shaped copy through
    `dashboard.sh tile ports` under a parent that behaves as
    `plugins.bounded_run` did -- clock started when `Popen` returns, process
    group killed at `TILE_SECONDS`. Not scaled: the child cannot be patched, so
    this runs for the real four seconds.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ports-loader-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        dashboard = self.root / "local-project-dashboard"
        dashboard.mkdir()
        for name in ("dashboard.sh", "sd_tile.py", "collectors.py"):
            shutil.copy2(HERE / name, dashboard / name)
        # dashboard.sh and collectors.py read the checkout's shared config helpers.
        shutil.copytree(HERE.parent / "lib", dashboard.parent / "lib")
        self.pids = self.root / "pids"
        machine = self.root / "local-machine-setup/machine-setup.sh"
        machine.parent.mkdir()
        machine.write_text(f'#!/bin/sh\necho $$ >> "{self.pids}"\nsleep 20 &\necho $! >> "{self.pids}"\nwait\n')
        machine.chmod(0o755)
        self.addCleanup(self.reap)

    def reap(self):
        # Whatever a failing run left behind, by pid rather than by name.
        if self.pids.exists():
            for pid in (int(line) for line in self.pids.read_text().split()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def test_refusal_arrives_before_the_loaders_kill_and_leaves_no_scan(self):
        import sd_tile
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.root),
                       "DASHBOARD_PYTHON": sys.executable}
        process = subprocess.Popen(["./local-project-dashboard/dashboard.sh", "tile", "ports"],
            cwd=self.root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        stop = time.monotonic() + sd_tile.TILE_SECONDS
        try:
            out, err = process.communicate(timeout=stop - time.monotonic())
            killed = False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            out, err = process.communicate()
            killed = True
        self.assertFalse(killed, "the loader's kill arrived before the tile's refusal")
        self.assertEqual((process.returncode, out), (1, b""))
        self.assertIn(b"ran past its budget", err)
        pids = [int(line) for line in self.pids.read_text().split()]
        self.assertEqual(len(pids), 2)
        deadline = time.monotonic() + 3
        while pids and time.monotonic() < deadline:
            pids = [pid for pid in pids if CollectionBudget.running(pid)]
            time.sleep(0.05)
        self.assertEqual(pids, [], "the scan outlived the tile")


if __name__ == "__main__":
    unittest.main()
