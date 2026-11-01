"""The brew stage: taps, formulae and casks, and no pins (sd:7059).

No python is pinned. sd:6463 pinned the one sd-serve runs: each upgrade is
a new Python.app, which the application firewall blocked, so sd-serve
admitted no satellite. The operator ruled no pins on any machine; the
stage allows a blocked Python.app again instead, through the repair
drop-in's sudo -n. Any pin an older stage made (python@*, opencode, codex)
is removed (sd:6433).

sd-serve may run its own uv python instead (sd:6492): the sd stage installs it
outside Homebrew, and its plist's PATH starts with it.

The script runs from a copy of this folder under a temporary root; `brew` is
a stub that logs each install and pin in a file, `python3` a stub that
reports $PY_PREFIX as its install path, and the firewall tool a stub that prints $FW_LIST.
"""

import os
import pathlib
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests import fixture_config
from tests.fixture_config import STALL, STALL_BOUND

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

# $PYTHONS are installed (three by default), plus $FORMULAE. `pin`,
# `install` and a bare `upgrade` record into $PINS; `list --pinned` reads it, and `unpin` logs
# into $PINS.unpin and drops the pin.
# `info --json=v2 --formula NAME` reports NAME installed as INSTALLED when
# $ALIASES holds NAME=INSTALLED, else not installed; INFO_RC fails it.
# $LEAVES is what `leaves --installed-on-request` prints. With
# INSTALLS_STICK, `list --formula` also lists each formula installed.
# INSTALL_RC fails an install after logging it; with INFO_RC_AFTER_INSTALL,
# brew info fails once an install was tried.
# `install --dry-run NAME` would install NAME and $DEPS, or fails with
# DRY_RC; `info --formula NAMES` heads each `(bottled)` unless $UNBOTTLED
# names it (sd:7103).
BREW_STUB = "#!/bin/sh\n" + STALL + r"""case "$*" in
  "list --formula") [ -z "$LIST_HANG" ] || stall
                    [ -z "$LIST_RC" ] || exit "$LIST_RC"
                    printf '%s\n' ${PYTHONS-python@3.12 python@3.14 python@3.15} $FORMULAE
                    [ -z "$INSTALLS_STICK" ] || sed -n 's/^install \([^-]\)/\1/p' "$PINS" 2>/dev/null ;;
  "list --cask"|leaves|tap) : ;;
  "leaves --installed-on-request") printf '%s\n' $LEAVES ;;
  "info --json=v2 --formula "*)
    [ -z "$INFO_RC" ] || exit "$INFO_RC"
    [ -z "$INFO_RC_AFTER_INSTALL" ] || ! grep -q '^install [^-]' "$PINS" 2>/dev/null || exit "$INFO_RC_AFTER_INSTALL"
    to=""
    for a in $ALIASES; do [ "${a%%=*}" != "$4" ] || to=${a#*=}; done
    if [ -n "$to" ]; then
      printf '{"formulae": [{"name": "%s", "full_name": "%s", "installed": [{"version": "1"}]}]}\n' "$to" "$to"
    else
      printf '{"formulae": [{"name": "%s", "full_name": "%s", "installed": []}]}\n' "${4##*/}" "$4"
    fi ;;
  "list --pinned") [ -z "$PINNED_RC" ] || exit "$PINNED_RC"
                   sed -n 's/^pin //p' "$PINS" 2>/dev/null ;;
  "pin "*) [ -z "$PIN_RC" ] || exit "$PIN_RC"
          echo "$*" >> "$PINS" ;;
  "install --dry-run "*)
    [ -z "$DRY_RC" ] || exit "$DRY_RC"
    printf '==> Would install 1 formula:\n%s 1.0\n' "$3"
    [ -z "$DEPS" ] || printf '==> Would install dependencies for %s:\n' "$3"
    for d in $DEPS; do printf '%s    1.0\n' "$d"; done ;;
  "info --formula "*)
    shift 2
    for n; do
      case " $UNBOTTLED " in
        *" $n "*) printf '==> %s: stable 1.0\nNot installed\n' "$n" ;;
        *) printf '==> %s: stable 1.0 (bottled)\nNot installed\n==> Dependencies\n' "$n" ;;
      esac
    done ;;
  "install --cask "*|upgrade) echo "$*" >> "$PINS" ;;
  "install "*) echo "$*" >> "$PINS"
               [ -z "$INSTALL_RC" ] || exit "$INSTALL_RC" ;;
  "unpin "*) echo "$*" >> "$PINS.unpin"
             grep -vx "pin ${2}" "$PINS" > "$PINS.new" || :
             mv "$PINS.new" "$PINS" ;;
  --prefix) echo "${BREW_PREFIX:-/usr/local}" ;;
esac
exit 0
"""

EXIT_1_STUB = "#!/bin/sh\nexit 1\n"

# The firewall tool: --listapps prints $FW_LIST, --getglobalstate $FW_STATE
# (enabled by default); FW_RC fails both.
FIREWALL_STUB = r"""#!/bin/sh
[ -z "$FW_RC" ] || exit "$FW_RC"
[ -z "$FW_LOG" ] || echo "$*" >> "$FW_LOG"
case "$1" in
  --listapps) printf '%b' "$FW_LIST" ;;
  --getglobalstate) echo "${FW_STATE:-Firewall is enabled. (State = 1)}" ;;
esac
exit 0
"""

# The install path the stubbed python3 reports by default, and its app.
PY_PREFIX = "/opt/homebrew/opt/python@3.15/Frameworks/Python.framework/Versions/3.15"
PYTHON_APP = PY_PREFIX + "/Resources/Python.app"

# Asked for sys.base_prefix, python3 prints $PY_PREFIX; every other call is
# the real interpreter's, as other stages run python3.
PYTHON3_STUB = f"""#!/bin/sh
if [ "$*" = "-c import sys; print(sys.base_prefix)" ]; then
  echo "${{PY_PREFIX:-{PY_PREFIX}}}"
  exit 0
fi
exec {sys.executable} "$@"
"""

# sd-serve's uv python, as the sd stage installs it (sd:6492): a plain
# executable, which the firewall lists by its real path.
UV_PREFIX = "{home}/.local/share/sd-serve/python/cpython-3.15.2-macos-aarch64-none"
UV_EXE = UV_PREFIX + "/bin/python3.15"
# Asked for its install path or its executable's real path, python3 prints
# $SERVED_PREFIX or $SERVED_EXE; every other call is the real interpreter's.
SERVED_STUB = f"""#!/bin/sh
case "$*" in
  "-c import sys; print(sys.base_prefix)") echo "$SERVED_PREFIX"; exit 0 ;;
  "-c import os, sys; print(os.path.realpath(sys.executable))") echo "$SERVED_EXE"; exit 0 ;;
esac
exec {sys.executable} "$@"
"""

# uv: `python install` puts a python under UV_PYTHON_INSTALL_DIR and a
# python3 in UV_PYTHON_BIN_DIR that prints that python's real path, as the
# stage asks it. Each call, with the two directories, is logged in $UV_LOG.
UV_STUB = r"""#!/bin/sh
echo "$* install=$UV_PYTHON_INSTALL_DIR bin=$UV_PYTHON_BIN_DIR" >> "$UV_LOG"
exe="$UV_PYTHON_INSTALL_DIR/cpython-3.15.2-macos-aarch64-none/bin/python3.15"
mkdir -p "$UV_PYTHON_BIN_DIR" "${exe%/*}"
printf '#!/bin/sh\n' > "$exe"
printf '#!/bin/sh\necho "%s"\n' "$exe" > "$UV_PYTHON_BIN_DIR/python3"
chmod +x "$exe" "$UV_PYTHON_BIN_DIR/python3"
"""


# codesign: `-dv FILE` fails, as for an unsigned file, until `-s - -f FILE`
# signs it; each signing is logged in $SIGN_LOG.
CODESIGN_STUB = r"""#!/bin/sh
case "$1" in
  -dv) [ -e "$2.signed" ] || { echo "$2: code object is not signed at all" >&2; exit 1; } ;;
  -s) echo "$*" >> "$SIGN_LOG"; touch "$4.signed" ;;
esac
exit 0
"""

# sudo -n CMD runs CMD, as the repair drop-in allows it (sd:6312).
SUDO_N_STUB = """#!/bin/sh
[ "$1" = -n ] || exit 1
shift
exec "$@"
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class BrewStageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config_root = fixture_config.copy_config(base)
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.pins = base / "pins"
        self.stubs = base / "stubs"
        write_exec(self.stubs / "brew", BREW_STUB)
        write_exec(self.stubs / "python3", PYTHON3_STUB)
        write_exec(self.stubs / "socketfilterfw", FIREWALL_STUB)
        for name in ("defaults", "sudo", "launchctl", "mas"):
            write_exec(self.stubs / name, EXIT_1_STUB)
        fixture_config.seal(self, self.stubs)

    def run_verb(self, *args, formulae="", **extra):
        env = {
            "FORMULAE": formulae,
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "PINS": str(self.pins),
            **extra,
        }
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args], env=env,
                              capture_output=True, text=True, cwd=self.tmp.name,
                              stdin=subprocess.DEVNULL, timeout=120)

    def pin_calls(self):
        text = self.pins.read_text() if self.pins.exists() else ""
        return [line for line in text.splitlines() if line.startswith("pin ")]

    def test_the_script_keeps_no_pin_list(self):
        """sd:6433: the pinned python is read from PATH python3, not a list."""
        script = (FOLDER / "machine-setup.sh").read_text()

        self.assertNotIn("BREW_PINNED", script)

    def test_the_serving_python_is_unpinned_not_pinned(self):
        """sd:7059: the operator ruled no pins; sd:6463's pin goes too."""
        self.pins.write_text("pin python@3.15\n")

        result = self.run_verb("update", "brew", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DIFFERS python@3.15 pinned", result.stdout)
        self.assertNotIn("not pinned", result.stdout)
        self.assertEqual(self.pin_calls(), [])
        self.assertEqual(self.unpin_calls(), ["unpin python@3.15"])

    def test_the_upgrade_sweep_pins_nothing_before_brew_upgrade(self):
        """sd:7059: the sweep pinned the serving python before `brew upgrade`."""
        self.run_verb("upgrade", "--apply")

        self.assertEqual(self.pins.read_text().splitlines(), ["upgrade"])

    def test_a_blocked_brew_python_app_is_allowed_again_under_apply(self):
        """sd:7059: an upgrade makes a new Python.app, which the firewall
        blocked (sd:6463); the stage adds it again through sudo -n."""
        fw_log = self.stubs.parent / "fw.log"
        blocked = {"FW_LIST": f"1 : {PYTHON_APP} \\n             (Block incoming connections)\\n",
                   "FW_LOG": str(fw_log)}
        self.serve_from_brew()

        dry = self.run_verb("update", "brew", **blocked)
        self.assertIn("[dry-run] sudo -n ", dry.stdout)
        self.assertFalse([l for l in fw_log.read_text().splitlines() if l != "--listapps"])

        write_exec(self.stubs / "sudo", SUDO_N_STUB)
        unlisted = {"FW_LIST": "", "FW_LOG": str(fw_log)}
        for verb, listing in ((("update", "brew", "--apply"), blocked), (("upgrade", "--apply"), blocked),
                              (("update", "brew", "--apply"), unlisted)):
            with self.subTest(verb=verb, listed=bool(listing["FW_LIST"])):
                fw_log.unlink(missing_ok=True)
                result = self.run_verb(*verb, **listing)
                self.assertEqual([l for l in fw_log.read_text().splitlines() if l != "--listapps"],
                                 [f"--add {PYTHON_APP}", f"--unblockapp {PYTHON_APP}"], result.stdout)

    def test_a_refused_firewall_allow_fails_the_upgrade_sweep(self):
        """sd:7059 review: a refused sudo -n printed FAILED, yet the sweep
        exited 0, though sd-serve stays blocked."""
        blocked = {"FW_LIST": f"1 : {PYTHON_APP} \\n             (Block incoming connections)\\n",
                   "FW_LOG": str(self.stubs.parent / "fw.log")}
        write_exec(self.stubs / "sudo", "#!/bin/sh\nexit 1\n")
        self.serve_from_brew()

        result = self.run_verb("upgrade", "--apply", **blocked)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("python firewall", result.stdout + result.stderr)

    def tailscale_profile(self, brew="", cask="tailscale-app\n"):
        profiles = self.config_root / "machine-setup/profiles"
        (profiles / "personal.brew").write_text(brew)
        (profiles / "personal.cask").write_text(cask)
        link = self.home / ".local/bin/tailscale"
        app = self.stubs.parent / "Applications"
        return link, app, {"MACHINE_SETUP_APPLICATIONS_DIR": str(app)}

    def test_the_tailscale_app_cask_gets_a_cli_wrapper(self):
        """sd:6554: the cask ships no CLI; sd-db and repo-sync.sh call
        `tailscale` from PATH. sd:6583: through a symlink the app dies, so
        it is a user-owned wrapper that execs the app's full path."""
        link, app, env = self.tailscale_profile()
        target = str(app / "Tailscale.app/Contents/MacOS/Tailscale")
        exec_line = f'exec "{target}" "$@"'
        sudo_log = self.home / "sudo.log"
        write_exec(self.stubs / "sudo", f'#!/bin/sh\necho "$*" >> "{sudo_log}"\nexit 1\n')

        dry = self.run_verb("update", "brew", **env)
        self.assertIn(f"MISSING tailscale CLI wrapper {link}: {exec_line}", dry.stdout)
        self.assertFalse(link.exists() or link.is_symlink())
        self.run_verb("update", "brew", "--apply", **env)

        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(), f"#!/bin/sh\n{exec_line}\n")
        self.assertTrue(os.access(link, os.X_OK))
        self.assertEqual(link.stat().st_uid, os.getuid())
        self.assertNotIn("tailscale", sudo_log.read_text() if sudo_log.exists() else "")
        again = self.run_verb("update", "brew", **env)
        self.assertIn(f"ok      tailscale CLI wrapper {link}", again.stdout)

    def test_the_old_symlink_is_replaced_by_the_wrapper(self):
        """sd:6583: #547 left a symlink that kills the app; the write must
        not go through it into the app binary."""
        link, app, env = self.tailscale_profile()
        target = app / "Tailscale.app/Contents/MacOS/Tailscale"
        target.parent.mkdir(parents=True)
        target.write_text("app\n")
        link.parent.mkdir(parents=True)
        link.symlink_to(target)

        dry = self.run_verb("update", "brew", **env)
        self.assertIn(f"STALE   tailscale CLI link {link}", dry.stdout)
        self.run_verb("update", "brew", "--apply", **env)

        self.assertFalse(link.is_symlink())
        self.assertIn(f'exec "{target}" "$@"', link.read_text())
        self.assertEqual(target.read_text(), "app\n")

    def test_a_foreign_tailscale_file_is_reported_not_replaced(self):
        link, _, env = self.tailscale_profile()
        link.parent.mkdir(parents=True)
        link.write_text("mine\n")

        result = self.run_verb("update", "brew", "--apply", **env)

        self.assertIn(f"DIFFERS {link} is not a wrapper for ", result.stdout)
        self.assertEqual(link.read_text(), "mine\n")

    def test_the_formula_retires_the_cli_wrapper(self):
        """sd:6554: the formula's own CLI replaces the wrapper."""
        link, app, env = self.tailscale_profile(brew="tailscale\n", cask="")
        link.parent.mkdir(parents=True)
        link.write_text(f'#!/bin/sh\nexec "{app}/Tailscale.app/Contents/MacOS/Tailscale" "$@"\n')

        dry = self.run_verb("update", "brew", **env)
        self.assertIn(f"EXTRA   tailscale CLI wrapper {link}", dry.stdout)
        self.run_verb("update", "brew", "--apply", formulae="tailscale", **env)

        self.assertFalse(link.exists())

    def test_a_modified_wrapper_is_the_operator_s_and_kept(self):
        """Review: a wrapper with the exec line plus the operator's own
        lines is not ours; dropping the cask must not delete it."""
        link, app, env = self.tailscale_profile(brew="tailscale\n", cask="")
        exec_line = f'exec "{app}/Tailscale.app/Contents/MacOS/Tailscale" "$@"'
        mine = f"#!/bin/sh\necho start >> /tmp/ts.log\n{exec_line}\n"
        link.parent.mkdir(parents=True)
        link.write_text(mine)

        gone = self.run_verb("update", "brew", "--apply", formulae="tailscale", **env)
        self.assertNotIn("EXTRA   tailscale CLI wrapper", gone.stdout)
        self.assertEqual(link.read_text(), mine)
        (self.config_root / "machine-setup/profiles/personal.brew").write_text("")
        (self.config_root / "machine-setup/profiles/personal.cask").write_text("tailscale-app\n")
        kept = self.run_verb("update", "brew", "--apply", **env)
        self.assertIn(f"DIFFERS {link} is not a wrapper for ", kept.stdout)
        self.assertEqual(link.read_text(), mine)

    def test_no_tailscale_in_the_profile_leaves_no_wrapper_line(self):
        _, _, env = self.tailscale_profile(cask="")

        self.assertNotIn("tailscale CLI", self.run_verb("update", "brew", **env).stdout)

    def test_a_firewall_block_on_the_serving_python_is_reported(self):
        """sd:6463: status reports it; the stage fixes it only under --apply.
        sd:7059 review: an unlisted one, as after an upgrade, too; none
        where sd-serve is not installed."""
        blocked = {"FW_LIST": f"1 : {PYTHON_APP} \\n             (Block incoming connections)\\n"}
        allowed = {"FW_LIST": f"1 : {PYTHON_APP} \\n             (Allow incoming connections)\\n"}
        self.assertNotIn("not allowed", self.run_verb("status", **blocked).stdout)
        self.serve_from_brew()

        for listing in (blocked, {"FW_LIST": ""}):
            with self.subTest(listed=bool(listing["FW_LIST"])):
                stage = self.run_verb("update", "brew", **listing)
                self.assertIn(f"DIFFERS firewall does not allow {PYTHON_APP}", stage.stdout)
                status = self.run_verb("status", **listing)
                self.assertIn(f"  not allowed: {PYTHON_APP}", status.stdout)
        self.assertNotIn("not allowed", self.run_verb("status", **allowed).stdout)

    def mosh_server(self):
        """mosh in the profile, installed under a brew prefix in the
        temporary root as brew links it: opt/mosh names its Cellar folder."""
        profiles = self.config_root / "machine-setup/profiles"
        (profiles / "personal.brew").write_text("mosh\n")
        prefix = self.stubs.parent / "brew"
        exe = prefix / "Cellar/mosh/1.4.0_43/bin/mosh-server"
        write_exec(exe, "#!/bin/sh\n")
        exe.chmod(0o555)
        (prefix / "opt").mkdir()
        (prefix / "opt/mosh").symlink_to(prefix / "Cellar/mosh/1.4.0_43")
        exe = exe.resolve()  # as the stage names it: /var is /private/var
        write_exec(self.stubs / "codesign", CODESIGN_STUB)
        self.sign_log = self.stubs.parent / "sign.log"
        self.fw_log = self.stubs.parent / "fw.log"
        return exe, {"BREW_PREFIX": str(prefix), "FORMULAE": "mosh",
                     "SIGN_LOG": str(self.sign_log), "FW_LOG": str(self.fw_log)}

    def test_an_unsigned_mosh_server_is_signed_and_allowed(self):
        """sd:6538: each mosh upgrade is a new unsigned mosh-server, which
        the firewall drops even when listed as allowed: it matches a rule
        only on a signature. The stage reports it and, under --apply, signs
        it ad hoc and adds it again through the repair drop-in's sudo -n."""
        exe, env = self.mosh_server()
        allowed = {"FW_LIST": f"1 : {exe} \\n             (Allow incoming connections)\\n"}

        dry = self.run_verb("update", "brew", **env, **allowed)
        self.assertIn(f"DIFFERS firewall drops {exe}: unsigned", dry.stdout)
        self.assertIn(f"[dry-run] codesign -s - -f {exe}", dry.stdout)
        self.assertFalse(self.sign_log.exists())
        status = self.run_verb("status", **env, **allowed)
        self.assertIn(f"DIFFERS firewall drops {exe}", status.stdout)

        write_exec(self.stubs / "sudo", SUDO_N_STUB)
        self.fw_log.unlink(missing_ok=True)
        fixed = self.run_verb("update", "brew", "--apply", **env, **allowed)
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertEqual(self.sign_log.read_text(), f"-s - -f {exe}\n")
        self.assertEqual([l for l in self.fw_log.read_text().splitlines() if l != "--listapps"],
                         [f"--remove {exe}", f"--add {exe}", f"--unblockapp {exe}"])
        self.assertTrue(exe.stat().st_mode & stat.S_IWUSR)

        again = self.run_verb("update", "brew", **env, **allowed)
        self.assertIn(f"ok      firewall allows {exe}", again.stdout)
        self.assertNotIn("DIFFERS firewall", again.stdout)

    def test_a_signed_mosh_server_the_firewall_blocks_is_allowed_again(self):
        """sd:6538: a signed mosh-server absent from the list, or blocked."""
        exe, env = self.mosh_server()
        (exe.parent / "mosh-server.signed").touch()
        blocked = {"FW_LIST": f"1 : {exe} \\n             (Block incoming connections)\\n"}
        for listing in ({"FW_LIST": ""}, blocked):
            stage = self.run_verb("update", "brew", **env, **listing)
            self.assertIn(f"DIFFERS firewall drops {exe}: not allowed", stage.stdout)

    def test_no_mosh_in_the_profile_checks_nothing(self):
        """sd:6538: a machine without mosh probes no mosh-server."""
        exe, env = self.mosh_server()
        (self.config_root / "machine-setup/profiles/personal.brew").write_text("")
        stage = self.run_verb("update", "brew", **env, FW_LIST="")
        self.assertNotIn("mosh-server", stage.stdout)

    def serve_from_brew(self):
        """sd-serve installed with its PATH at the stub brew python3."""
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / f"{fixture_config.LABEL_PREFIX}.sd-serve.plist").write_bytes(plistlib.dumps(
            {"Label": "x", "EnvironmentVariables": {"PATH": f"{self.stubs}:/usr/bin:/bin"}}))

    def serve_from_uv(self):
        """sd-serve installed with its PATH starting at its uv python."""
        served = self.home / ".local/share/sd-serve/python/bin"
        write_exec(served / "python3", SERVED_STUB)
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / f"{fixture_config.LABEL_PREFIX}.sd-serve.plist").write_bytes(plistlib.dumps(
            {"Label": "x", "EnvironmentVariables": {"PATH": f"{served}:{self.stubs}:/usr/bin:/bin"}}))
        prefix = UV_PREFIX.format(home=self.home)
        return {"SERVED_PREFIX": prefix, "SERVED_EXE": UV_EXE.format(home=self.home)}

    def test_a_uv_serving_python_pins_nothing_and_unpins_the_brew_one(self):
        """sd:6492: brew owns no uv python, so the stage pins nothing, and
        the pin sd:6463 left on python@3.15 reads stale and goes."""
        served = self.serve_from_uv()
        self.pins.write_text("pin python@3.15\n")

        result = self.run_verb("update", "brew", "--apply", **served)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("not pinned", result.stdout)
        self.assertIn("DIFFERS python@3.15 pinned", result.stdout)
        self.assertEqual(self.pin_calls(), [])
        self.assertEqual(self.unpin_calls(), ["unpin python@3.15"])
        status = self.run_verb("status", **served)
        self.assertNotIn("unpinned:", status.stdout)
        self.assertNotIn("pinned  :", status.stdout)

    def test_a_firewall_block_on_a_uv_serving_python_names_its_binary(self):
        """sd:6492: a uv python is a plain executable, which the firewall
        lists by its real path; it has no Python.app."""
        served = self.serve_from_uv()
        exe = served["SERVED_EXE"]
        blocked = {"FW_LIST": f"1 : {exe} \\n             (Block incoming connections)\\n"}
        allowed = {"FW_LIST": f"1 : {exe} \\n             (Allow incoming connections)\\n"}

        stage = self.run_verb("update", "brew", **served, **blocked)
        self.assertIn(f"DIFFERS firewall does not allow {exe};", stage.stdout)
        self.assertNotIn("Python.app", stage.stdout)
        status = self.run_verb("status", **served, **blocked)
        self.assertIn(f"  not allowed: {exe}", status.stdout)
        self.assertNotIn("not allowed", self.run_verb("status", **served, **allowed).stdout)

    def sd_stage(self, *flags, **extra):
        self.uv_log = self.stubs.parent / "uv.log"
        write_exec(self.stubs / "uv-stub", UV_STUB)
        return self.run_verb("update", "sd", *flags, MACHINE_SETUP_UV=str(self.stubs / "uv-stub"),
                             UV_LOG=str(self.uv_log), **extra)

    def uv_calls(self):
        return self.uv_log.read_text().splitlines() if self.uv_log.exists() else []

    def test_the_sd_stage_installs_sd_serves_python_once(self):
        """sd:6492: uv installs it beside the folder the sd-serve plist's PATH
        starts with, before the agents stage loads sd-serve."""
        where = self.home / ".local/share/sd-serve/python"

        dry = self.sd_stage()
        self.assertIn(f"  MISSING sd-serve python 3.15 at {where} — sd-serve runs brew's python3 "
                      "until it is there", dry.stdout)
        self.assertIn("python install 3.15 --default", dry.stdout)
        self.assertEqual(self.uv_calls(), [])

        self.sd_stage("--apply")
        self.assertEqual(self.uv_calls(),
                         [f"python install 3.15 --default install={where} bin={where}/uv-bin"])
        self.sd_stage("--apply")
        self.assertEqual(len(self.uv_calls()), 1)

        template = fixture_config.render(fixture_config.AGENTS / f"{fixture_config.LABEL_PREFIX}.sd-serve.plist",
                                         "x", self.home, FOLDER.parent)
        example = fixture_config.render(FOLDER / f"examples/launchagents/{fixture_config.LABEL_PREFIX}.sd-serve.plist",
                                        "x", self.home, FOLDER.parent)
        for text in (template, example):
            path = plistlib.loads(text.encode())["EnvironmentVariables"]["PATH"]
            self.assertEqual(path.split(":")[0], f"{where}/bin")

    def test_sd_serve_stays_on_brew_python_until_the_firewall_allows_the_new_one(self):
        """sd:6492 review: an unattended run installed the uv python where
        sd-serve's PATH finds it, and sd-serve restarted under a binary the
        firewall had not allowed, so it admitted no satellite. The PATH
        folder gets its python3 only once the firewall allows that binary;
        an entry that blocks it, no entry, or a probe that fails, is no."""
        where = self.home / ".local/share/sd-serve/python"
        exe = f"{where}/cpython-3.15.2-macos-aarch64-none/bin/python3.15"
        for fw in ({"FW_LIST": ""},
                   {"FW_LIST": f"1 : {exe} \\n             (Block incoming connections)\\n"},
                   {"FW_LIST": f"1 : {exe}t \\n             (Allow incoming connections)\\n"},
                   {"FW_RC": "1"},
                   # Off now, but the system stage turns it on (review round 2).
                   {"FW_STATE": "Firewall is disabled. (State = 0)", "FW_LIST": ""}):
            with self.subTest(fw=fw):
                result = self.sd_stage("--apply", **fw)

                self.assertIn(f"  DIFFERS sd-serve python {exe} is not allowed in the firewall — "
                              "sd-serve stays on brew's python3; allow it in System Settings > Network > "
                              "Firewall > Options, then: machine-setup.sh update sd --apply", result.stdout)
                self.assertFalse(pathlib.Path(f"{where}/bin/python3").exists(follow_symlinks=False))

    def test_an_allowed_python_goes_on_sd_serves_path(self):
        where = self.home / ".local/share/sd-serve/python"
        exe = f"{where}/cpython-3.15.2-macos-aarch64-none/bin/python3.15"
        allow = f"1 : {exe} \\n             (Allow incoming connections)\\n"
        for fw in ({"FW_LIST": allow},
                   {"FW_LIST": allow, "FW_STATE": "Firewall is disabled. (State = 0)"}):
            with self.subTest(fw=fw):
                shutil.rmtree(where / "bin", ignore_errors=True)
                self.sd_stage(**fw)
                self.assertFalse((where / "bin/python3").exists())

                result = self.sd_stage("--apply", **fw)

                self.assertIn(f"  MISSING sd-serve python on its PATH, {where}/bin/python3 — "
                              f"the firewall allows {exe}", result.stdout)
                self.assertEqual(str((where / "bin/python3").readlink()), exe)
                self.assertIn(f"  ok      sd-serve python {where}/bin/python3",
                              self.sd_stage(**fw).stdout)
        self.assertEqual(len(self.uv_calls()), 1)

    def test_the_sd_stage_without_uv_says_so_and_installs_nothing(self):
        result = self.run_verb("update", "sd", "--apply")

        self.assertIn("  --      no uv to install it (the brew stage installs uv)", result.stdout)
        self.assertNotIn("python install", result.stdout)

    def unpin_calls(self):
        log = pathlib.Path(f"{self.pins}.unpin")
        return log.read_text().splitlines() if log.exists() else []

    def test_a_pin_the_old_stage_made_is_unpinned_under_apply(self):
        """sd:6433 review: a machine the old stage ran on keeps an older
        python pinned, and a bare `brew upgrade` fails on a dependent that
        needs a newer one. A pin made by hand on another formula stays; the
        serving python's goes too (sd:7059)."""
        self.pins.write_text("pin python@3.12\npin python@3.15\npin opencode\npin jq\n")
        dry = self.run_verb("update", "brew", formulae="opencode jq")

        self.assertIn("DIFFERS python@3.12 pinned", dry.stdout)
        self.assertIn("DIFFERS opencode pinned", dry.stdout)
        self.assertIn("DIFFERS python@3.15 pinned", dry.stdout)
        self.assertNotIn("jq pinned", dry.stdout)
        self.assertEqual(self.unpin_calls(), [])

        result = self.run_verb("update", "brew", "--apply", formulae="opencode jq")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.unpin_calls(), ["unpin python@3.12", "unpin python@3.15", "unpin opencode"])
        again = self.run_verb("update", "brew", formulae="opencode jq")
        self.assertNotIn("pinned", again.stdout)

    def test_status_counts_a_pin_the_old_stage_made_as_drift(self):
        self.pins.write_text("pin python@3.15\n")
        pinned = self.run_verb("status")
        self.pins.write_text("")
        clean = self.run_verb("status")

        self.assertIn("  pinned  : python@3.15", pinned.stdout)
        self.assertNotIn("pinned  :", clean.stdout)
        drift = [int(r.stdout.split("drift   : ")[1].split()[0]) for r in (pinned, clean)]
        self.assertEqual(drift[0] - drift[1], 1)

    def test_a_pin_list_brew_cannot_give_unpins_nothing(self):
        """sd:3536: a failed list is unknown, not "nothing pinned"."""
        result = self.run_verb("update", "brew", "--apply", PINNED_RC="1")

        self.assertIn("FAILED  brew stage: brew could not list its pins; none unpinned", result.stdout)
        self.assertEqual(self.unpin_calls(), [])
        status = self.run_verb("status", PINNED_RC="1")
        self.assertIn("pinned  : unknown (brew could not list its pins)", status.stdout)

    def alias_profile(self):
        """The profile names python, an alias of the installed python@3.15."""
        (self.config_root / "machine-setup/profiles/personal.brew").write_text("python\n")
        return {"ALIASES": "python=python@3.15"}

    def test_a_formula_named_by_its_alias_is_installed(self):
        """sd:7093: `brew list` prints python@3.15, so the profile's python
        read as missing and was installed again every night."""
        env = self.alias_profile()

        result = self.run_verb("update", "brew", "--apply", **env)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  ok      python (python@3.15)\n", result.stdout)
        self.assertNotIn("MISSING python", result.stdout)
        self.assertNotIn("install python", self.pins.read_text() if self.pins.exists() else "")

    def test_status_counts_an_aliased_formula_neither_missing_nor_extra(self):
        """sd:7093: status read python as missing and python@3.15, a leaf
        installed on request, as extra."""
        env = self.alias_profile()

        result = self.run_verb("status", LEAVES="python@3.15", **env)

        block = result.stdout.split("brew formulae\n", 1)[1].split("casks\n", 1)[0]
        self.assertEqual(block, "  ok      in sync\n", result.stdout)

    def test_a_formula_brew_info_cannot_resolve_is_unknown_not_installed(self):
        """A failed probe is no "no" (sd:3536): nothing is installed, and the
        run fails naming it."""
        env = self.alias_profile()

        result = self.run_verb("update", "brew", "--apply", INFO_RC="1", **env)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAILED  brew stage: brew could not tell whether python is installed or pours from "
                      "a bottle; not installed", result.stdout)
        self.assertNotIn("install python", self.pins.read_text() if self.pins.exists() else "")
        status = self.run_verb("status", INFO_RC="1", **env)
        self.assertIn("  unknown : python\n", status.stdout)

    def missing_formula(self):
        """The profile names example-formula, which is not installed."""
        (self.config_root / "machine-setup/profiles/personal.brew").write_text("example-formula\n")

    def installs(self):
        text = self.pins.read_text() if self.pins.exists() else ""
        return [line for line in text.splitlines() if line.startswith("install ")]

    def test_a_missing_formula_that_pours_from_bottles_is_installed(self):
        self.missing_formula()

        result = self.run_verb("update", "brew", "--apply", DEPS="example-dep")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  MISSING example-formula\n", result.stdout)
        self.assertEqual(self.installs(), ["install example-formula"])

    def test_a_formula_to_build_from_source_waits_for_the_upgrade(self):
        """sd:7103: an Intel Mac has no bottles, and one `brew install node`
        built llvm, rust and node for 3 h; the nightly stage's 1800 s bound
        killed it. By ruling only the weekly upgrade builds from source."""
        self.missing_formula()
        for unbottled in ("example-formula", "example-dep"):
            with self.subTest(unbottled=unbottled):
                self.pins.unlink(missing_ok=True)
                result = self.run_verb("update", "brew", "--apply", DEPS="example-dep", UNBOTTLED=unbottled)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("  DIFFERS example-formula needs a source build; the weekly upgrade installs it\n",
                              result.stdout)
                self.assertEqual(self.installs(), [])

    def test_a_bottle_brew_cannot_tell_installs_nothing_and_fails(self):
        """A failed probe is no "no" (sd:3536), and no "yes"."""
        self.missing_formula()

        result = self.run_verb("update", "brew", "--apply", DRY_RC="1")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAILED  brew stage: brew could not tell whether example-formula is installed or "
                      "pours from a bottle; not installed", result.stdout)
        self.assertEqual(self.installs(), [])

    def test_status_reports_a_source_build_as_attention_not_missing(self):
        """sd:7103: the formula waits for the weekly run by ruling, so the
        nightly status names it for attention and does not fail on it."""
        self.missing_formula()

        result = self.run_verb("status", "--fail-on-drift", UNBOTTLED="example-formula")

        block = result.stdout.split("brew formulae\n", 1)[1].split("casks\n", 1)[0]
        self.assertEqual(block, "  DIFFERS example-formula needs a source build; the weekly upgrade installs it\n",
                         result.stdout)
        self.assertRegex(result.stdout, r"SD_REPORT_ATTENTION: drift on \S+: DIFFERS example-formula needs a "
                                        r"source build; the weekly upgrade installs it\n")

    def test_the_upgrade_installs_a_missing_formula_with_the_long_bound(self):
        """sd:7103: the weekly sweep builds what the brew stage left, bounded
        at 6 h, twice the 3 h measured on an Intel Mac."""
        self.missing_formula()

        result = self.run_verb("upgrade", "--apply", UNBOTTLED="example-formula")

        self.assertEqual(self.installs(), ["install example-formula"], result.stdout + result.stderr)
        for command in ("brew upgrade", "brew install example-formula"):
            self.assertRegex(result.stderr, r"\[step \d\d:\d\d:\d\d\] " + command + r" \(bound 21600s\)")

    def test_a_week_with_only_a_missing_formula_is_no_quiet_week(self):
        """sd:7103: upgrade-report skipped the sweep when nothing was
        outdated, so a formula the brew stage left to it never got built."""
        self.missing_formula()
        write_exec(self.stubs / "mas", "#!/bin/sh\nexit 0\n")

        result = self.run_verb("upgrade-report", UNBOTTLED="example-formula", INSTALLS_STICK="1")

        self.assertNotIn("nothing outdated", result.stdout)
        self.assertEqual(self.installs(), ["install example-formula"], result.stdout + result.stderr)
        self.assertIn("\ninstalled (formula):\n  example-formula\n", result.stdout)
        self.assertNotIn("still missing", result.stdout)

    def test_a_failed_install_brew_cannot_recheck_is_not_reported_installed(self):
        """sd:7132: the snapshot after the sweep failed, so it was empty, and
        the report listed the formula whose install failed as installed."""
        self.missing_formula()
        write_exec(self.stubs / "mas", "#!/bin/sh\nexit 0\n")

        result = self.run_verb("upgrade-report", INSTALL_RC="1", INFO_RC_AFTER_INSTALL="1")

        self.assertEqual(self.installs(), ["install example-formula"], result.stdout + result.stderr)
        self.assertNotIn("installed (formula)", result.stdout)
        self.assertIn("\nmissing (formula): unknown, the list did not answer\n", result.stdout)
        self.assertIn("outdated list failed (missing)", result.stdout)

    def test_a_list_brew_cannot_give_changes_nothing(self):
        """sd:3536: read as empty, a failed list installed every formula again."""
        (self.config_root / "machine-setup/profiles/personal.brew").write_text("jq\n")
        result = self.run_verb("update", "brew", "--apply", LIST_RC="1")

        self.assertIn("FAILED  brew stage: brew could not list what is installed; nothing changed",
                      result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.pins.exists(), self.pins.read_text() if self.pins.exists() else "")

    def test_a_brew_call_that_hangs_ends_at_its_bound_and_names_itself(self):
        """sd:3536: an unattended brew stage bounds each brew call."""
        result = self.run_verb("update", "brew", "--apply", LIST_HANG="1",
                               MACHINE_SETUP_STEP_TIMEOUT=str(STALL_BOUND))

        self.assertRegex(result.stderr, rf"\[step [\d:]+\] brew list --formula \(bound {STALL_BOUND}s\)")
        self.assertRegex(result.stderr, r"\[step [\d:]+\] brew list --formula exited 124")
        self.assertFalse(self.pins.exists(), self.pins.read_text() if self.pins.exists() else "")


if __name__ == "__main__":
    unittest.main()
