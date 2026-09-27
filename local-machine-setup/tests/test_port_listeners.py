"""`candidates service` reads TCP listeners out of lsof's stdout: sd:756.

On the owner's machine lsof prints a warning about a Carbon Copy Cloner
snapshot mount on every run. The scan captured stderr into the text it
parsed, so the warning sat where the header belongs and every port came back
UNKNOWN, listening or not. These cases run the script against a fixture tree
with an `lsof` stub on PATH that behaves the way the real one does: the
warning goes to stderr unless `-w` is given, rows come out in PID order, and
the exit code is 0 when anything matched and 1 when nothing did.

The rule the scan keeps is that an unavailable lsof is not evidence that a
port is free: CLEAR needs a clean run, and anything else is UNKNOWN.

lsof run as this user also omits other users' sockets without a word: on the
owner's machine launchd holds 22 and 445 and tailscaled holds 443 and 8443,
and lsof exits 1 about them as if nothing listened (review of #327). So a
`netstat` stub stands beside it, printing macOS's socket table, and a port
netstat lists as LISTEN that lsof did not name is not CLEAR either.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "machine-setup.sh"

WARNING = ("lsof: WARNING: can't stat() apfs file system /private/tmp/16@113EA6DC/c\n"
           "      Output information may be incomplete.\n"
           "      assuming \"dev=3200000a\" from mount table\n")

# argv, LSOF_FIXTURE (JSON) and the call log are all the stub reads.
LSOF = f"""#!{sys.executable}
import json, os, sys
fixture = json.loads(os.environ["LSOF_FIXTURE"])
with open(os.environ["LSOF_CALLS"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if "-w" not in sys.argv:
    sys.stderr.write({WARNING!r})
sys.stderr.write(fixture.get("error", ""))
if "header" in fixture:
    print(fixture["header"])
    raise SystemExit(fixture.get("exit", 0))
selector = next(a for a in sys.argv if a.startswith("-iTCP"))
want = selector.partition(":")[2]
rows = [r for r in fixture.get("rows", []) if not want or r[2].rpartition(":")[2] == want]
rows.sort(key=lambda r: int(r[1]) if r[1].isdigit() else 0)
if rows:
    print("COMMAND     PID USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME")
    for command, pid, name in rows:
        print(f"{{command:<9}} {{pid:>5}} someone 3u  IPv4 0x85fe24c64eedf33b      0t0  TCP {{name}} (LISTEN)")
raise SystemExit(fixture.get("exit", 0 if rows else 1))
"""

# LSOF_FIXTURE["netstat"]: absent means netstat agrees with lsof's rows; a
# list names the ports it lists as LISTEN; "fail", "missing" and "linux"
# stand for a netstat that errors, one the shell cannot find (exit 127, as
# sh reports it), and Linux's net-tools table, whose -p means something else.
NETSTAT = f"""#!{sys.executable}
import json, os, sys
fixture = json.loads(os.environ["LSOF_FIXTURE"])
mode = fixture.get("netstat")
assert sys.argv[1:] == ["-an", "-p", "tcp"], sys.argv
if mode == "fail":
    sys.stderr.write("netstat: SECRET sysctl failed\\n")
    raise SystemExit(1)
if mode == "missing":
    sys.stderr.write("sh: netstat: command not found\\n")
    raise SystemExit(127)
if mode == "linux":
    print("Active Internet connections (servers and established)")
    print("Proto Recv-Q Send-Q Local Address           Foreign Address         State       PID/Program name")
    print("tcp        0      0 0.0.0.0:22              0.0.0.0:*               LISTEN      -")
    raise SystemExit(0)
if mode is None:
    mode = [name.rpartition(":")[2] for _, _, name in fixture.get("rows", [])]
print("Active Internet connections (including servers)")
print("Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)")
# A connection is not a listener, whichever end names a candidate port.
print("tcp4       0      0  127.0.0.1.50000        127.0.0.1.9103         ESTABLISHED")
for port in mode:
    print(f"tcp6       0      0  fd7a:115c:a1e0::.{{port}}  *.*                    LISTEN     ")
    print(f"tcp4       0      0  *.{{port}}                 *.*                    LISTEN     ")
"""


class PortListeners(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="port-listeners-")
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        machine = self.root / "local-machine-setup" / "machine-setup.sh"
        (machine.parent / "profiles").mkdir(parents=True)
        shutil.copyfile(SCRIPT, machine)
        machine.chmod(0o755)
        self.machine = machine
        (machine.parent / "profiles" / "common.service").write_text("local-alpha\n")
        for name, ports in (("alpha", [9101, 9103]), ("beta", [9101, 9102])):
            folder = self.root / f"local-{name}"
            folder.mkdir()
            entry = folder / f"{name}.sh"
            entry.write_text('#!/bin/sh\ncase "$1" in\nstart) docker run ' +
                             " ".join(f"-p {port}:80" for port in ports) + " fixture ;;\nesac\n")
            entry.chmod(0o755)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text('#!/bin/sh\nprintf "alpha\\n"\n')
        docker.chmod(0o755)
        self.calls = self.root / "lsof-calls"
        self.calls.touch()

    def candidates(self, fixture=None, *, lsof=True):
        for name, body, wanted in (("lsof", LSOF, lsof), ("netstat", NETSTAT, True)):
            if wanted:
                stub = self.bin / name
                stub.write_text(body)
                stub.chmod(0o755)
        # No /usr/sbin: the real lsof lives there on a Mac, and a missing stub
        # must mean a missing lsof. Linux keeps it in /usr/bin, so there the
        # system directories are mirrored as symlinks with lsof left out.
        system = "/usr/bin:/bin"
        if not lsof and shutil.which("lsof", path=system):
            mirror = self.root / "no-lsof"
            mirror.mkdir(exist_ok=True)
            for directory in ("/usr/bin", "/bin"):
                for entry in sorted(pathlib.Path(directory).iterdir()):
                    if entry.name != "lsof" and not (mirror / entry.name).exists():
                        (mirror / entry.name).symlink_to(entry)
            system = str(mirror)
        environment = {"PATH": f"{self.bin}:{system}", "HOME": str(self.root),
                       "MACHINE_SETUP_STATE": str(self.root / "state"),
                       "MACHINE_SETUP_PROFILE_DIR": str(self.machine.parent / "profiles"),
                       "LSOF_FIXTURE": json.dumps(fixture or {}), "LSOF_CALLS": str(self.calls)}
        result = subprocess.run([str(self.machine), "candidates", "service"], env=environment,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("WARNING", result.stdout)
        self.assertNotIn("SECRET", result.stdout)
        return {line.split()[2]: line.split()[0] for line in result.stdout.splitlines()
                if line.split()[:1] in (["LISTEN"], ["CLEAR"], ["UNKNOWN"])}, result.stdout

    def test_a_warning_on_stderr_does_not_hide_a_listener(self):
        states, output = self.candidates({"rows": [["fixture", "321", "*:9101"]]})
        self.assertEqual(states, {"9101": "LISTEN", "9102": "CLEAR", "9103": "CLEAR"}, output)
        self.assertIn("  LISTEN  port 9101 held by fixture (pid 321)\n", output)
        self.assertIn("  BUSY    port 9101 held by fixture (pid 321) — alpha beta cannot bind it\n", output)

    def test_a_warning_on_stderr_with_nothing_listening_is_clear(self):
        states, output = self.candidates({"rows": []})
        self.assertEqual(states, {"9101": "CLEAR", "9102": "CLEAR", "9103": "CLEAR"}, output)
        self.assertNotIn("BUSY", output)

    def test_a_missing_lsof_is_unknown_not_clear(self):
        states, output = self.candidates(lsof=False)
        self.assertEqual(set(states.values()), {"UNKNOWN"}, output)
        self.assertEqual(len(states), 3, output)

    def test_an_lsof_failure_is_unknown_not_clear(self):
        for fixture in ({"rows": [], "exit": 2, "error": "lsof: SECRET kernel read failed\n"},
                        {"rows": [], "exit": 1, "error": "lsof: SECRET permission denied\n"},
                        {"rows": [["fixture", "321", "*:9101"]], "exit": 2},
                        {"rows": [["fixture", "321", "*:9101"]], "exit": 1}):
            with self.subTest(fixture=fixture):
                states, output = self.candidates(fixture)
                self.assertEqual(set(states.values()), {"UNKNOWN"}, output)
                self.assertEqual(len(states), 3, output)

    def test_an_unreadable_header_is_unknown_not_clear(self):
        states, output = self.candidates({"header": "SECRET not an lsof table", "exit": 0})
        self.assertEqual(set(states.values()), {"UNKNOWN"}, output)

    def test_an_error_keeps_the_listener_it_showed_and_clears_no_port(self):
        # Whatever lsof still writes to stderr under -w is an error. The rows
        # it printed are still listeners; the ports it did not print are not
        # proven free.
        states, output = self.candidates({"rows": [["fixture", "321", "*:9101"]],
                                          "error": "lsof: SECRET can't read some process\n"})
        self.assertEqual(states, {"9101": "LISTEN", "9102": "UNKNOWN", "9103": "UNKNOWN"}, output)
        self.assertIn("  LISTEN  port 9101 held by fixture (pid 321)\n", output)

    def test_the_first_holder_lsof_lists_names_the_port(self):
        # Two processes on one port, one of them on both IPv4 and IPv6, and
        # neighbours whose numbers end in a candidate port.
        rows = [["later", "900", "127.0.0.1:9101"], ["first", "321", "*:9101"],
                ["first", "321", "[::1]:9101"], ["suffix", "40", "127.0.0.1:19102"],
                ["prefix", "41", "127.0.0.1:91030"]]
        states, output = self.candidates({"rows": rows})
        self.assertEqual(states, {"9101": "LISTEN", "9102": "CLEAR", "9103": "CLEAR"}, output)
        self.assertIn("  LISTEN  port 9101 held by first (pid 321)\n", output)
        self.assertEqual(output.count("port 9101 held by"), 2, output)

    def test_one_lsof_answers_the_whole_table(self):
        # Three distinct ports, one process: the per-port loop ran one lsof
        # for each, 0.36-0.48 s apiece on the owner's machine.
        self.candidates({"rows": [["fixture", "321", "*:9101"]]})
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(calls, [["-w", "-nP", "-iTCP", "-sTCP:LISTEN"]])

    def test_an_unparseable_row_clears_no_port(self):
        states, output = self.candidates({"rows": [["fixture", "321", "*:9101"], ["odd", "nopid", "*:9102"]]})
        self.assertEqual(states["9101"], "LISTEN", output)
        self.assertEqual(states["9103"], "UNKNOWN", output)

    def test_a_listener_lsof_cannot_see_is_unknown_not_clear(self):
        # netstat lists 9102 as LISTEN; lsof, blind to a root-owned socket,
        # lists nothing and exits 1.
        states, output = self.candidates({"rows": [], "netstat": ["9102"]})
        self.assertEqual(states, {"9101": "CLEAR", "9102": "UNKNOWN", "9103": "CLEAR"}, output)
        self.assertNotIn("BUSY", output)
        states, output = self.candidates({"rows": [["fixture", "321", "*:9101"]], "netstat": ["9101", "9103"]})
        self.assertEqual(states, {"9101": "LISTEN", "9102": "CLEAR", "9103": "UNKNOWN"}, output)

    def test_a_missing_or_unreadable_netstat_clears_no_port(self):
        for mode in ("fail", "missing", "linux"):
            for rows in ([], [["fixture", "321", "*:9101"]]):
                with self.subTest(netstat=mode, rows=rows):
                    states, output = self.candidates({"rows": rows, "netstat": mode})
                    self.assertNotIn("CLEAR", states.values(), output)
                    self.assertEqual(len(states), 3, output)
                    if rows:
                        self.assertEqual(states["9101"], "LISTEN", output)


if __name__ == "__main__":
    unittest.main()
