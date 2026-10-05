"""sd:831. A lost Full Disk Access grant is a line naming the path, not an empty tab.

The tile runs under two interpreters (`sd_tile.py`'s module docstring) and
macOS grants Full Disk Access per binary, so the dashboard depends on two
grants. `collectors.vault_blocked` names only the interpreter it runs under,
on the tile that was asked; the other path's grant could be dropped by a
`brew upgrade python` with nothing to say so. `dashboard.sh grants` probes
both paths and prints one line each.

Neither grant can be revoked from here, and from a shell none can be
measured: the shell's own Documents access reaches every child. So the
refusing interpreter is a script that answers the probe as an ungranted
binary would, the passing one is the real interpreter, and the verb's own
control says whether the answers are the binaries' own.

That control is a listing and not a stat, and it has three answers rather
than two: `ls -d` prints the name of a directory the same `ls` cannot list,
and a control that reads its own failure as a refusal turns it into a
conclusive pass (review-376 B1). `Control` below drives the real
`collectors.control_listing` against directories this test makes.
"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sd_tile
from stubs import executable

HERE = Path(__file__).resolve().parents[1]


def collectors(environment):
    """A fresh `collectors` module, its `VAULT` derived under `environment`."""
    with patch.dict(os.environ, environment):
        spec = importlib.util.spec_from_file_location("grants_collectors", HERE / "collectors.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def answering(root, name, answer):
    """An 'interpreter' that answers the vault probe with `answer`, whatever it is asked."""
    script = root / name
    # A file of its own, not `stubs.executable`: the report tells two
    # interpreters apart by what their paths resolve to.
    script.write_text(f"#!/bin/sh\necho {answer}\n")
    script.chmod(0o755)
    return script


class Stub:
    """The collectors `vault_grants` reads, with the probe and the control scripted.

    `control` is `control_listing`'s answer: 'refused' and 'waited' are the
    two launchd shapes, where each line above is that binary's own; 'listed'
    is a caller whose access reached its children; '' is a control that
    measured nothing.
    """

    VAULT_CONTROL = "/bin/ls"
    VAULT_PROBE_SECONDS = 15

    def __init__(self, answers, *, control):
        self.answers, self.control, self.probed = answers, control, []
        self.VAULT = Path("/vault-sd-831")

    def probe_vault(self, executable):
        self.probed.append(executable)
        return self.answers[executable]

    def vault_refusal(self, executable, answer):
        return "" if answer == "ok" else f"refused: grant {executable}"

    def control_listing(self):
        return self.control


class Report(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="grants-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.a = answering(self.root, "a", "ok")
        self.b = answering(self.root, "b", "ok")

    def test_the_path_without_the_grant_is_named_and_the_other_passes(self):
        stub = Stub({str(self.a): "denied", str(self.b): "ok"}, control="refused")
        lines, code = sd_tile.vault_grants(stub, [f"DASHBOARD_PYTHON={self.a}", f"SD_DASHBOARD_PYTHON={self.b}"])
        self.assertEqual(lines, [f"DASHBOARD_PYTHON {self.a}: refused: grant {self.a}",
                                 f"SD_DASHBOARD_PYTHON {self.b}: ok, lists the vault"])
        self.assertEqual(code, 1)

    def test_both_granted_and_measured_as_themselves_is_a_pass(self):
        # One of the two routes to 0: nothing refused, and the control
        # refused, which is what says the answers are the binaries' own and
        # not the caller's. The other is the control that waited, below;
        # `test_grants_exit_documentation` is what holds the documents to
        # naming both.
        stub = Stub({str(self.a): "ok", str(self.b): "ok"}, control="refused")
        lines, code = sd_tile.vault_grants(stub, [f"DASHBOARD_PYTHON={self.a}", f"SD_DASHBOARD_PYTHON={self.b}"])
        self.assertEqual([line.split(": ", 1)[1] for line in lines], ["ok, lists the vault"] * 2)
        self.assertEqual(code, 0)

    def test_a_control_left_waiting_lets_the_answers_stand_and_says_so(self):
        # An ungranted read under launchd does not fail, it waits -- the
        # reason the probe reads its own silence as denied. So a control that
        # waited was kept out, the lines above it are the binaries' own, and
        # the verb says which shape the refusal took.
        stub = Stub({str(self.a): "ok", str(self.b): "ok"}, control="waited")
        lines, code = sd_tile.vault_grants(stub, [f"DASHBOARD_PYTHON={self.a}", f"SD_DASHBOARD_PYTHON={self.b}"])
        self.assertEqual(code, 0)
        self.assertEqual(lines[2], "the control, /bin/ls, was neither answered nor refused in 15 seconds, "
                                   "which is what a read nobody can grant looks like under launchd; the "
                                   "answers above are taken as the binaries' own")

    def test_a_wedge_wide_enough_to_reach_the_probes_is_not_a_pass(self):
        # sd:845 R1, the bound on the arm above. A control that waited is
        # read as one kept out, so a control wedged for a reason that is not
        # TCC would carry the lines above it. It can only carry lines that
        # answered: whatever wedges a listing of the vault wedges the probes
        # of it too, a probe that said nothing is a refusal, and a refusal
        # outranks the control. So the pass needs a wedge that reaches
        # `/bin/ls` and not the vault, and anything wider lands here, on 1.
        stub = Stub({str(self.a): "", str(self.b): ""}, control="waited")
        lines, code = sd_tile.vault_grants(stub, [f"DASHBOARD_PYTHON={self.a}", f"SD_DASHBOARD_PYTHON={self.b}"])
        self.assertEqual(code, 1)
        self.assertEqual([line.split(": ", 1)[1] for line in lines[:2]],
                         [f"refused: grant {self.a}", f"refused: grant {self.b}"])

    def test_a_control_that_measured_nothing_is_not_a_pass(self):
        # review-376 B1(b): `run` returned '' for a refused command and for
        # one that never ran alike, so a control that failed to start read as
        # a refusal and printed a conclusive 0. The third answer is that
        # state, it says so, and it is not 0.
        stub = Stub({str(self.a): "ok", str(self.b): "ok"}, control="")
        lines, code = sd_tile.vault_grants(stub, [f"DASHBOARD_PYTHON={self.a}", f"SD_DASHBOARD_PYTHON={self.b}"])
        self.assertEqual(code, sd_tile.GRANTS_INCONCLUSIVE)
        self.assertEqual(lines[2], "inconclusive: the control settled nothing -- /bin/ls neither listed "
                                   "the vault, nor was refused, nor ran out its wait -- so nothing here "
                                   "says whether the answers above are the binaries' own grants or this "
                                   "process's access reaching its children")

    def test_the_second_path_is_the_one_named_when_it_is_the_one_refused(self):
        # review-376 N1: every refusal case named the first path, so a
        # `vault_grants` that always reported `named[0]` survived.
        stub = Stub({str(self.a): "ok", str(self.b): "denied"}, control="refused")
        lines, code = sd_tile.vault_grants(stub, [f"DASHBOARD_PYTHON={self.a}", f"SD_DASHBOARD_PYTHON={self.b}"])
        self.assertEqual(lines, [f"DASHBOARD_PYTHON {self.a}: ok, lists the vault",
                                 f"SD_DASHBOARD_PYTHON {self.b}: refused: grant {self.b}"])
        self.assertEqual(code, 1)

    def test_two_refusals_name_their_own_paths(self):
        stub = Stub({str(self.a): "denied", str(self.b): "missing"}, control="refused")
        lines, code = sd_tile.vault_grants(stub, [f"DASHBOARD_PYTHON={self.a}", f"SD_DASHBOARD_PYTHON={self.b}"])
        self.assertEqual(lines, [f"DASHBOARD_PYTHON {self.a}: refused: grant {self.a}",
                                 f"SD_DASHBOARD_PYTHON {self.b}: refused: grant {self.b}"])
        self.assertEqual(code, 1)

    def test_a_shell_whose_own_access_reaches_the_children_is_said_and_not_a_pass(self):
        # The control read by /bin/ls lists the vault: every answer above it
        # is the caller's access, not the binary's grant, so the exit is not
        # 0, and not 1 either -- nothing was refused.
        stub = Stub({str(self.a): "ok", str(self.b): "ok"}, control="listed")
        lines, code = sd_tile.vault_grants(stub, [f"DASHBOARD_PYTHON={self.a}", f"SD_DASHBOARD_PYTHON={self.b}"])
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[2].startswith(
            "inconclusive: /bin/ls, which holds no grant of its own, lists the vault too"))
        self.assertEqual(code, sd_tile.GRANTS_INCONCLUSIVE)
        self.assertEqual(code, 3)

    def test_a_refusal_outranks_an_inconclusive_control(self):
        stub = Stub({str(self.a): "", str(self.b): "ok"}, control="listed")
        lines, code = sd_tile.vault_grants(stub, [f"DASHBOARD_PYTHON={self.a}", f"SD_DASHBOARD_PYTHON={self.b}"])
        self.assertEqual(code, 1)
        self.assertIn(f"refused: grant {self.a}", lines[0])
        self.assertTrue(lines[2].startswith("inconclusive"))

    def test_two_paths_to_one_binary_are_probed_once_and_said_to_be_one(self):
        link = self.root / "same"
        link.symlink_to(self.a)
        stub = Stub({str(self.a): "ok"}, control="refused")
        lines, code = sd_tile.vault_grants(stub, [f"DASHBOARD_PYTHON={self.a}", f"SD_DASHBOARD_PYTHON={link}"])
        self.assertEqual(stub.probed, [str(self.a)])
        self.assertEqual(lines[1], f"SD_DASHBOARD_PYTHON {link}: the same binary as DASHBOARD_PYTHON, "
                                   f"{os.path.realpath(self.a)}; one grant covers both, and its answer is "
                                   "that path's line above")
        self.assertEqual(code, 0)

    def test_a_path_that_is_not_an_executable_is_a_refusal(self):
        # Missing, present but not executable, and a directory: the same
        # line, because the same thing is true of each -- nothing there runs.
        plain = self.root / "plain"
        plain.write_text("")
        for path in (self.root / "gone", plain, self.root):
            with self.subTest(path=path):
                stub = Stub({str(self.b): "ok"}, control="refused")
                lines, code = sd_tile.vault_grants(
                    stub, [f"DASHBOARD_PYTHON={path}", f"SD_DASHBOARD_PYTHON={self.b}"])
                self.assertEqual(lines[0], f"DASHBOARD_PYTHON {path}: not an executable path -- no file "
                                           "here runs, so nothing on it reads the vault")
                self.assertEqual((stub.probed, code), ([str(self.b)], 1))

    def test_a_bare_name_is_found_on_path(self):
        # `DASHBOARD_PYTHON` defaults to `python3`, a name and not a path.
        stub = Stub({str(self.a): "ok"}, control="refused")
        environment = {**os.environ, "PATH": f"{self.root}:{os.environ.get('PATH', '')}"}
        with patch.dict(os.environ, environment):
            lines, code = sd_tile.vault_grants(stub, ["DASHBOARD_PYTHON=a"])
        self.assertEqual((stub.probed, code), ([str(self.a)], 0))
        self.assertEqual(lines, ["DASHBOARD_PYTHON a: ok, lists the vault"])

    def test_usage(self):
        for argv in (["--grants"], ["--grants", "no-role"], ["--grants", "=/bin/sh"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(sd_tile.main(argv), 2)
            self.assertIn("usage: dashboard.sh grants", err.getvalue())


class Control(unittest.TestCase):
    """`collectors.control_listing`, against directories this test makes.

    The refusal it has to recognise is TCC's, which cannot be arranged here;
    a directory with no search permission produces the same class of failure
    from the same binary -- a listing refused with a reason on stderr -- and
    that is what the classifier reads. Run as root every mode is readable, so
    the refusal case says so rather than passing quietly.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="control-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.module = collectors({"VAULT": str(self.root / "vault")})

    def test_a_probe_that_said_nothing_names_both_of_its_causes(self):
        # `probe_vault` documents '' as two stories -- a TCC prompt nobody
        # answered, or a binary that did not run -- and `vault_refusal` told
        # only the first, so a reader with a broken interpreter granted
        # access and saw the line unchanged. The '' line keeps the grant as
        # its first step and names the second; the `denied` line, which is
        # the probe's own word for TCC, stays the one-cause message.
        silent = self.module.vault_refusal("/opt/py", "")
        self.assertIn("Grant Full Disk Access to /opt/py", silent)
        self.assertIn("did not run at all", silent)
        self.assertIn(f"within the probe's {self.module.seconds_terms(self.module.VAULT_PROBE_SECONDS)}", silent)
        denied = self.module.vault_refusal("/opt/py", "denied")
        self.assertIn("Grant Full Disk Access to /opt/py", denied)
        self.assertNotIn("did not run", denied)

    def test_a_directory_that_lists_is_listed(self):
        (self.root / "vault").mkdir()
        self.assertEqual(self.module.control_listing(), "listed")

    def test_a_directory_that_cannot_be_listed_is_refused_not_listed(self):
        # The heart of B1: `ls -d` prints this path's name, so a stat as the
        # control would have called it a pass.
        if os.geteuid() == 0:
            self.skipTest("root reads every mode, so no listing is refused")
        vault = self.root / "vault"
        vault.mkdir(mode=0o000)
        self.addCleanup(vault.chmod, 0o700)
        stat = subprocess.run(["/bin/ls", "-d", str(vault)], capture_output=True, text=True)
        self.assertEqual((stat.returncode, stat.stdout.strip()), (0, str(vault)))
        self.assertEqual(self.module.control_listing(), "refused")

    def test_a_vault_that_is_not_there_measured_nothing(self):
        # Not `refused`: ls fails, but the vault is missing, which says
        # nothing about anyone's access.
        self.assertEqual(self.module.control_listing(), "")

    def test_a_control_binary_that_is_not_there_measured_nothing(self):
        (self.root / "vault").mkdir()
        with patch.object(self.module, "VAULT_CONTROL", str(self.root / "gone")):
            self.assertEqual(self.module.control_listing(), "")

    def test_a_control_that_fails_for_another_reason_measured_nothing(self):
        # Nonzero, but the reason is not an access refusal: the same
        # failure a broken control gives, and no evidence anyone was kept out.
        (self.root / "vault").mkdir()
        loud = self.root / "loud"
        executable(loud, "#!/bin/sh\necho 'ls: something else entirely' >&2\nexit 1\n")
        with patch.object(self.module, "VAULT_CONTROL", str(loud)):
            self.assertEqual(self.module.control_listing(), "")

    def test_a_refusal_in_another_language_is_still_refused(self):
        # sd:845 R2. `refused` is read off the words on stderr, and those
        # words are a locale's. This control says its refusal in the language
        # its caller asked for, which is what a platform with a `strerror`
        # catalogue would do; before the control was pinned to `LC_ALL=C` it
        # inherited `fr_FR.UTF-8` from here, said so in French, and the
        # classifier called it '' -- inconclusive, exit 3, on a real refusal.
        (self.root / "vault").mkdir()
        speaks = self.root / "speaks"
        executable(speaks, '#!/bin/sh\ncase "$LC_ALL" in\n'
                          'C) echo "ls: vault: Permission denied" >&2 ;;\n'
                          '*) echo "ls: vault: Permission non accordee" >&2 ;;\n'
                          'esac\nexit 1\n')
        with patch.dict(os.environ, {"LC_ALL": "fr_FR.UTF-8"}), \
                patch.object(self.module, "VAULT_CONTROL", str(speaks)):
            self.assertEqual(self.module.control_listing(), "refused")

    def test_the_real_control_names_a_real_refusal_under_a_foreign_locale(self):
        # sd:845 R2, the other half, and no fail-first: Darwin has no
        # `strerror` catalogue, so `/bin/ls` was never localised and this
        # passed before `LC_ALL=C` too. It is here to hold the end of the
        # chain the test above stubs -- a real binary, a real refusal, a
        # caller in another language -- so that a platform which starts
        # localising fails on that test and not on this verb in the field.
        if os.geteuid() == 0:
            self.skipTest("root reads every mode, so no listing is refused")
        vault = self.root / "vault"
        vault.mkdir(mode=0o000)
        self.addCleanup(vault.chmod, 0o700)
        with patch.dict(os.environ, {"LC_ALL": "fr_FR.UTF-8"}):
            self.assertEqual(self.module.control_listing(), "refused")

    def test_a_control_that_is_never_answered_waited_and_is_not_waited_on(self):
        # The launchd shape: no answer at all, for as long as anyone waits.
        # It stops at the timeout, and the grandchild holding its stderr
        # does not outlive it -- the reason the kill is a killpg.
        (self.root / "vault").mkdir()
        pids = self.root / "pids"
        slow = self.root / "slow"
        executable(slow, f'#!/bin/sh\nsleep 60 &\necho $! > "{pids}"\nwait\n')
        started = time.monotonic()
        with patch.object(self.module, "VAULT_CONTROL", str(slow)):
            self.assertEqual(self.module.control_listing(timeout=1), "waited")
        self.assertLess(time.monotonic() - started, 10)
        grandchild = int(pids.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the control's grandchild outlived the timeout")


class ThroughTheScript(unittest.TestCase):
    """`dashboard.sh grants`, the real interpreter on one path and a refusing script on the other."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="grants-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        # A checkout-shaped copy, as `test_tile_deadline` drives the script:
        # the real checkout's gitignored `.env` would otherwise override the
        # interpreters this test sets.
        self.dashboard = self.root / "checkout" / "local-project-dashboard"
        self.dashboard.mkdir(parents=True)
        for name in ("dashboard.sh", "sd_tile.py", "collectors.py"):
            shutil.copy2(HERE / name, self.dashboard / name)
        # dashboard.sh and collectors.py read the checkout's shared config helpers.
        shutil.copytree(HERE.parent / "lib", self.dashboard.parent / "lib")

    def grants(self, *arguments, **environment):
        base = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "VAULT": str(self.vault)}
        return subprocess.run(["sh", str(self.dashboard / "dashboard.sh"), *(arguments or ["grants"])],
                              capture_output=True, text=True, env={**base, **environment}, timeout=60)

    def test_the_fresh_path_lacks_the_grant_and_the_servers_path_passes(self):
        # The item's check: DASHBOARD_PYTHON is an interpreter path that
        # answers as an ungranted binary does under launchd -- the probe's
        # `denied` -- while SD_DASHBOARD_PYTHON, the real interpreter, lists
        # the vault. One line each, and the exit is the refusal's.
        fresh = answering(self.root, "fresh-python3", "denied")
        result = self.grants(DASHBOARD_PYTHON=str(fresh), SD_DASHBOARD_PYTHON=sys.executable)
        lines = result.stdout.splitlines()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertRegex(lines[0], rf"^DASHBOARD_PYTHON {fresh}: cannot read {self.vault} .*"
                                   rf"Grant Full Disk Access to {fresh} in System Settings")
        self.assertEqual(lines[1], f"SD_DASHBOARD_PYTHON {sys.executable}: ok, lists the vault")
        # The vault here is a directory this test made, so the control lists
        # it: the run is inconclusive about the real interpreter's line, and
        # the verb says so rather than passing it off as the binary's grant.
        self.assertTrue(lines[2].startswith("inconclusive: /bin/ls, which holds no grant"), lines[2])

    def test_a_vault_nobody_can_list_is_never_reported_as_reached(self):
        # review-376 B1, end to end and without naming the control: the vault
        # is a directory no process here can list, so every probe is refused
        # -- and nothing may claim that this process's access reached the
        # children, because it reached nothing. The old control stat'ed the
        # path, which succeeds on exactly this directory, and said it had.
        if os.geteuid() == 0:
            self.skipTest("root reads every mode, so no listing is refused")
        self.vault.chmod(0o000)
        self.addCleanup(self.vault.chmod, 0o700)
        stat = subprocess.run(["/bin/ls", "-d", str(self.vault)], capture_output=True, text=True)
        self.assertEqual((stat.returncode, stat.stdout.strip()), (0, str(self.vault)))
        result = self.grants(DASHBOARD_PYTHON=sys.executable, SD_DASHBOARD_PYTHON=sys.executable)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("lists the vault too", result.stdout)
        self.assertIn("Grant Full Disk Access", result.stdout)

    def wedge_the_control(self, seconds=1):
        """Point this copy's control at a binary that answers nothing, and shorten its wait.

        The copy is the real `collectors.py`, two constants rewritten: the
        control cannot be `/bin/ls` here, because wedging the machine's own
        `/bin/ls` is not a thing a test may do, and fifteen seconds of waiting
        is not a thing a suite may spend. Everything between them -- the
        script, the probes, `control_listing`'s own timeout and killpg, the
        exit -- is the shipped path.
        """
        wedged = self.root / "wedged-control"
        executable(wedged, "#!/bin/sh\nsleep 600\n")
        source = self.dashboard / "collectors.py"
        text = source.read_text()
        for was, now in (('VAULT_CONTROL = "/bin/ls"', f'VAULT_CONTROL = "{wedged}"'),
                         ("VAULT_PROBE_SECONDS = 15", f"VAULT_PROBE_SECONDS = {seconds}")):
            self.assertIn(was, text)
            text = text.replace(was, now)
        source.write_text(text)
        return wedged

    def test_a_control_wedged_for_its_own_reasons_passes_the_run(self):
        # sd:845 R1, measured rather than reasoned about, and recorded so it
        # is not refound as new: a control that never answers is read as one
        # kept out, and a control wedged for a reason that has nothing to do
        # with TCC is indistinguishable from that. This vault lists for
        # everybody -- no grant is in question and none was measured -- and
        # the verb exits 0. The arm is deliberate: the wait is the refusal
        # shape this repository has measured under launchd, and the line
        # prints the evidence it passed on. See the comment over
        # `sd_tile.CONTROL_LINE`.
        wedged = self.wedge_the_control()
        result = self.grants(DASHBOARD_PYTHON=sys.executable, SD_DASHBOARD_PYTHON=sys.executable)
        lines = result.stdout.splitlines()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines[0], f"DASHBOARD_PYTHON {sys.executable}: ok, lists the vault")
        self.assertEqual(lines[2], f"the control, {wedged}, was neither answered nor refused in 1 "
                                   "seconds, which is what a read nobody can grant looks like under "
                                   "launchd; the answers above are taken as the binaries' own")

    def test_a_wedge_that_reaches_the_probes_too_refuses_end_to_end(self):
        # The bound on the test above, through the script: a wedge wide
        # enough to reach an interpreter silences its probe, and a probe that
        # said nothing is a refusal whatever the control did. So the only run
        # that reaches 0 on a wedge is one where the wedge stopped at the
        # control and every probe answered.
        wedged = self.wedge_the_control()
        result = self.grants(DASHBOARD_PYTHON=str(wedged), SD_DASHBOARD_PYTHON=sys.executable)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"Grant Full Disk Access to {wedged}", result.stdout.splitlines()[0])

    def test_the_verb_takes_no_arguments(self):
        # review-376 L2: `grants --help` ran the probe and answered 3, which
        # reads as a measurement of an interpreter nobody asked about.
        result = self.grants("grants", "--help", DASHBOARD_PYTHON=sys.executable,
                             SD_DASHBOARD_PYTHON=sys.executable)
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("usage: dashboard.sh grants (no arguments;", result.stderr)

    def test_a_missing_server_interpreter_is_refused_before_probing(self):
        result = self.grants(DASHBOARD_PYTHON=sys.executable, SD_DASHBOARD_PYTHON=str(self.root / "gone"))
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn(f"missing installed runtime {self.root / 'gone'}", result.stderr)

    def test_the_verb_is_in_the_help(self):
        result = self.grants("help")
        self.assertEqual(result.returncode, 0)
        # `grants` is a verb on the usage line -- not that it sits next to a
        # particular neighbour. Pinning the adjacency made this a test of the
        # verb *list*, so adding an unrelated verb failed it (`docs`, #471).
        verbs = [part.strip().split()[0]
                 for part in result.stdout.splitlines()[0].split("usage:")[1].split("|")]
        self.assertIn("grants", verbs)
        self.assertIn("  grants           probe the vault under DASHBOARD_PYTHON", result.stdout)


if __name__ == "__main__":
    unittest.main()
