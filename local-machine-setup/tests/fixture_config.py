"""The configuration the suite runs against.

machine-setup.sh reads its profiles, dotfiles, .env templates and LaunchAgent
templates from $SYSTEM_TOOLS_CONFIG/machine-setup, outside the repository. The
suite never reads the operator's copy: every case points SYSTEM_TOOLS_CONFIG at
this folder's fixtures, or at a temporary copy when the case writes.

Nor does it read the operator's Mac: env() points the apps scan at no folder,
the Spotlight sudoers drop-in at no file and nsmb.conf at a fixture, and
seal() fails a case that reaches a real `defaults`, `sudo` or `launchctl`.
"""

import pathlib
import shutil
import stat

HERE = pathlib.Path(__file__).resolve().parent
# Standing in for $SYSTEM_TOOLS_CONFIG; machine-setup.sh reads CONFIG / "machine-setup".
CONFIG = HERE / "fixtures" / "config"
MACHINE_SETUP = CONFIG / "machine-setup"
PROFILES = MACHINE_SETUP / "profiles"
DOTFILES = MACHINE_SETUP / "dotfiles"
AGENTS = MACHINE_SETUP / "launchagents"
LABEL_PREFIX = "local.system-tools"
# Names no folder, so the apps scan finds no application on any machine.
NO_APPLICATIONS = HERE / "fixtures" / "no-applications"
# Names no file in a folder that does not exist, so the system stage never
# sees this Mac's drop-in (sd:3307) and a stray write has nowhere to land.
NO_SUDOERS = HERE / "fixtures" / "no-sudoers.d" / "machine-setup-spotlight-read"
# Already as the system stage wants it, so no case reads this Mac's
# /etc/nsmb.conf or adds a drift line for it; test_nsmb_conf sets its own.
NSMB_CONF = HERE / "fixtures" / "nsmb.conf"
# Commands that read this machine whatever $HOME says: `defaults` asks
# cfprefsd, which answers for the login user, `sudo` reads as root, and
# `launchctl` answers for the login user's launchd.
MACHINE_READERS = ("defaults", "sudo", "launchctl")
# A stub's stall, as the shell function `stall`. No clock tells a stall from
# a loaded machine (sd:6157, sd:6253), so a case waits on the stall itself:
# under the bound (lib/bounded.sh's perl) the stub sends the bound its alarm,
# and the step times out at once through the bound's own path; anywhere else
# it hangs, and the case's own timeout fails the run. So a case sets the
# bound to STALL_BOUND, which no step that answers comes near.
STALL = ('stall() {\n'
         '  case "$(ps -o args= -p "$PPID")" in "perl -e use strict; use POSIX ();"*) kill -s ALRM "$PPID" ;; esac\n'
         '  exec sleep 120\n'
         '}\n')
STALL_BOUND = 30


def copy_config(base):
    """A writable copy of the fixture config under `base`; returns its root."""
    target = pathlib.Path(base) / "config"
    shutil.copytree(CONFIG, target)
    return target


def copy_capture_config(base):
    """`copy_config` for a case that runs capture: no agent roster.

    The fixture `personal.agent` names agents a test HOME never installs, and
    capture refuses to write a roster empty (sd:3106), so a capture case keeps
    a roster only when it writes one. The file stays, as a comment, so the
    profile still exists when a case deletes its other files.
    """
    target = copy_config(base)
    (target / "machine-setup/profiles/personal.agent").write_text("# no agents in a capture case\n")
    return target


def env(config=CONFIG):
    """The variables that point machine-setup.sh at `config`."""
    return {"SYSTEM_TOOLS_CONFIG": str(config),
            "MACHINE_SETUP_APPLICATIONS_DIR": str(NO_APPLICATIONS),
            "MACHINE_SETUP_SPOTLIGHT_SUDOERS": str(NO_SUDOERS),
            "MACHINE_SETUP_NSMB_CONF": str(NSMB_CONF)}


def seal(case, stubs):
    """Fail `case` if machine-setup.sh reaches a real machine reader.

    Each MACHINE_READERS name the case has not stubbed in `stubs` gets a stub
    that records the call and exits 1. A cleanup fails the case with every
    recorded call. A stub the case writes, before or after, wins.
    """
    stubs = pathlib.Path(stubs)
    log = stubs / "unstubbed.log"
    for name in MACHINE_READERS:
        path = stubs / name
        if path.exists():
            continue
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 1\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def check():
        calls = log.read_text() if log.exists() else ""
        case.assertEqual(calls, "", "machine-setup.sh reached a machine reader "
                         "the case did not stub; it must not read this Mac")
    case.addCleanup(check)


def render(template, label, home, root, pack=None):
    """A LaunchAgent template as the agents stage installs it; `pack` defaults as `SD_PACK_ROOT` does."""
    text = pathlib.Path(template).read_text()
    return (text.replace("@HOME@", str(home))
                .replace("@LABEL@", label)
                .replace("@ROOT@", str(root))
                .replace("@PACK@", str(pack or f"{home}/repos/platypeeps/sd-ai-command-pack")))
