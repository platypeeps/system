"""The configuration the suite runs against.

machine-setup.sh reads its profiles, dotfiles, .env templates and LaunchAgent
templates from $SYSTEM_TOOLS_CONFIG/machine-setup, outside the repository. The
suite never reads the operator's copy: every case points SYSTEM_TOOLS_CONFIG at
this folder's fixtures, or at a temporary copy when the case writes.
"""

import pathlib
import shutil

HERE = pathlib.Path(__file__).resolve().parent
# Standing in for $SYSTEM_TOOLS_CONFIG; machine-setup.sh reads CONFIG / "machine-setup".
CONFIG = HERE / "fixtures" / "config"
MACHINE_SETUP = CONFIG / "machine-setup"
PROFILES = MACHINE_SETUP / "profiles"
DOTFILES = MACHINE_SETUP / "dotfiles"
AGENTS = MACHINE_SETUP / "launchagents"
LABEL_PREFIX = "local.system-tools"


def copy_config(base):
    """A writable copy of the fixture config under `base`; returns its root."""
    target = pathlib.Path(base) / "config"
    shutil.copytree(CONFIG, target)
    return target


def env(config=CONFIG):
    """The variables that point machine-setup.sh at `config`."""
    return {"SYSTEM_TOOLS_CONFIG": str(config)}


def render(template, label, home, root):
    """A LaunchAgent template as the agents stage installs it."""
    text = pathlib.Path(template).read_text()
    return (text.replace("@HOME@", str(home))
                .replace("@LABEL@", label)
                .replace("@ROOT@", str(root)))
