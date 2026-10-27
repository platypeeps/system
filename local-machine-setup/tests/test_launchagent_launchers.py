"""No shipped LaunchAgent starts as, or execs into, a Homebrew binary (sd:6427).

macOS charges a file-access check to the program launchd started, as the
responsible process. A Homebrew binary's grant is keyed by its code hash,
so each upgrade drops it: sd-serve started bin/sd-db, whose `env python3`
shebang made Homebrew python responsible. Apple's /bin/bash and /bin/sh keep
one identity. A template starts one of them, either as
`/bin/bash -c '"$0" ARGS; exit $?' SCRIPT`, where `; exit $?` stops bash
from exec'ing its last command, or on a script that execs no program.
"""

import pathlib
import plistlib
import re
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
APPLE_SHELLS = ("/bin/bash", "/bin/sh")

# Templates whose live plist another tool writes, each with its reason.
PACK_OWNED = {
    # The pack writes the live dashboard plist (dashboard/sd_dashboard/
    # runtime.py) and refuses one whose program differs; its launcher is
    # the pack half of sd:6427.
    "local.system-tools.sd-dashboard.plist",
}

# An `exec` that starts a program: a quoted, variable or bare command word.
# `exec 3>&2` only moves descriptors, and a help line's `exec  internal:`
# is text. The `test` verbs exec unittest, which no LaunchAgent runs.
PROGRAM_EXEC = re.compile(r"^\s*exec\s+([\"$/]|[A-Za-z_][\w.-]*(\s|$))")


def templates():
    found = sorted(REPO.glob("local-*/*.plist.template"))
    found += sorted((REPO / "local-machine-setup/examples/launchagents").glob("*.plist"))
    return [path for path in found if path.name not in PACK_OWNED]


def program_execs(script):
    return [line.strip() for line in script.read_text().splitlines()
            if PROGRAM_EXEC.match(line) and "-m unittest" not in line]


def launcher_faults(path):
    """Why the template's launched program is not an Apple shell that stays put."""
    args = plistlib.loads(path.read_bytes())["ProgramArguments"]
    if args[0] not in APPLE_SHELLS:
        return [f"starts {args[0]} directly"]
    if args[1] == "-c":
        if not args[2].rstrip().endswith("; exit $?"):
            return [f"{args[0]} -c {args[2]!r} execs its last command"]
        return []
    script = pathlib.Path(args[1].replace("@DIR@", str(path.parent)))
    return [f"{script.name} execs a program: {line}" for line in program_execs(script)]


class LauncherTest(unittest.TestCase):
    def test_every_shipped_template_starts_an_apple_shell_that_stays(self):
        found = templates()
        self.assertGreaterEqual(len(found), 4, found)
        faults = {path.relative_to(REPO).as_posix(): launcher_faults(path) for path in found}
        self.assertEqual({name: why for name, why in faults.items() if why}, {})

    def test_the_cron_runner_execs_no_program(self):
        # Every cron plist is [job-launch, /bin/bash, cron-jobs.sh, exec, JOB]; the
        # cron stage knows its plists by that shape (cron_plist_ours).
        self.assertEqual(program_execs(REPO / "local-cron-jobs/cron-jobs.sh"), [])

    def test_the_check_refuses_each_fault(self):
        """Evidence: each rule refuses a template that breaks it."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        tmp = pathlib.Path(temp.name)
        cases = {
            "direct": ["@DIR@/x.sh", "serve"],
            "bare -c": ["/bin/bash", "-c", '"$0" serve', "@DIR@/x.sh"],
        }
        for name, args in cases.items():
            with self.subTest(name):
                path = tmp / f"launcher-{name.replace(' ', '-')}.plist"
                path.write_bytes(plistlib.dumps({"Label": "example.test", "ProgramArguments": args}))
                self.assertNotEqual(launcher_faults(path), [])
        script = tmp / "launcher-execs.sh"
        script.write_text('#!/bin/sh\nexec 3>&2\nexec uv run python -m example\n')
        self.assertEqual(program_execs(script), ["exec uv run python -m example"])


if __name__ == "__main__":
    unittest.main()
