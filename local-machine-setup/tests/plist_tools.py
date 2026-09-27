"""Stand-ins for macOS's `plutil` and `PlistBuddy` on a machine without them.

machine-setup.sh reads plists and JSON through both tools, and CI runs on
Linux, where neither exists. `install(directory)` writes a Python stand-in for
each tool this machine lacks and returns the environment that points the
script at them. On a Mac it writes nothing: the real tools answer there, so
the operator's run still checks the script against them.

Each stand-in covers only the calls machine-setup.sh makes:

    plutil -convert xml1 -o - FILE
    plutil -extract KEYPATH raw -o - FILE
    PlistBuddy -c 'Print :KEY' FILE
    PlistBuddy -c 'Add :KEY array' FILE
    PlistBuddy -c 'Add :KEY: string VALUE' FILE

and fails loudly on anything else, so a new call is noticed, not answered.
"""

import pathlib
import shutil
import stat
import sys

PLISTBUDDY = "/usr/libexec/PlistBuddy"

PLUTIL_SOURCE = r'''
import json, plistlib, sys

def load(path):
    try:
        data = open(path, "rb").read()
    except FileNotFoundError:
        sys.exit(f"{path}: file does not exist or is not readable (No such file or directory)")
    try:
        return plistlib.loads(data)
    except Exception:
        return json.loads(data)

args = sys.argv[1:]
if len(args) == 5 and args[0] == "-convert" and args[1] == "xml1" and args[2:4] == ["-o", "-"]:
    sys.stdout.buffer.write(plistlib.dumps(load(args[4]), fmt=plistlib.FMT_XML))
    sys.exit(0)
if len(args) == 6 and args[0] == "-extract" and args[2] == "raw" and args[3:5] == ["-o", "-"]:
    value = load(args[5])
    for key in args[1].split("."):
        if not isinstance(value, dict) or key not in value:
            sys.exit(f"No value at that key path or invalid key path: {args[1]}")
        value = value[key]
    if isinstance(value, (dict, list)):
        sys.exit("raw output needs a scalar")
    print("true" if value is True else "false" if value is False else value)
    sys.exit(0)
sys.exit("plutil stand-in: unsupported call: " + " ".join(args))
'''

PLISTBUDDY_SOURCE = r'''
import plistlib, sys

args = sys.argv[1:]
if len(args) != 3 or args[0] != "-c":
    sys.exit("PlistBuddy stand-in: unsupported call: " + " ".join(args))
command, path = args[1], args[2]
with open(path, "rb") as handle:
    raw = handle.read()
fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
data = plistlib.loads(raw)
verb, _, rest = command.partition(" ")
if verb == "Print" and rest.startswith(":") and ":" not in rest[1:]:
    key = rest[1:]
    if key not in data:
        print(f'Print: Entry, ":{key}", Does Not Exist', file=sys.stderr)
        sys.exit(1)
    value = data[key]
    if not isinstance(value, list):
        sys.exit("PlistBuddy stand-in: Print covers arrays only")
    print("Array {")
    for item in value:
        print(f"    {item}")
    print("}")
    sys.exit(0)
if verb == "Add":
    target, _, typed = rest.partition(" ")
    kind, _, value = typed.partition(" ")
    if target.startswith(":") and target.count(":") == 1 and kind == "array" and not value:
        data[target[1:]] = []
    elif target.startswith(":") and target.endswith(":") and target.count(":") == 2 and kind == "string":
        data.setdefault(target[1:-1], []).append(value)
    else:
        sys.exit("PlistBuddy stand-in: unsupported Add: " + command)
    with open(path, "wb") as handle:
        plistlib.dump(data, handle, fmt=fmt)
    sys.exit(0)
sys.exit("PlistBuddy stand-in: unsupported command: " + command)
'''


def _write(path, source):
    path.write_text(f"#!{sys.executable}\n{source}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install(directory):
    """Write a stand-in for each missing tool into `directory` (on PATH).

    Returns the environment machine-setup.sh needs to find them: empty on a
    Mac, MACHINE_SETUP_PLISTBUDDY where PlistBuddy is missing.
    """
    directory = pathlib.Path(directory)
    env = {}
    if shutil.which("plutil", path="/usr/bin") is None:
        _write(directory / "plutil", PLUTIL_SOURCE)
    if not pathlib.Path(PLISTBUDDY).is_file():
        _write(directory / "PlistBuddy", PLISTBUDDY_SOURCE)
        env["MACHINE_SETUP_PLISTBUDDY"] = str(directory / "PlistBuddy")
    return env
