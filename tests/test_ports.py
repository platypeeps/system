"""No two tools bind the same port, and README § Ports lists what the code binds (sd:3965).

Both rules lived in prose (`.claude/rules/services.md`, "no two services
share a port"), and the README table was kept by hand. This reads the ports
out of the tracked files and fails naming both claimants when two folders
bind one port, and naming the row when the table disagrees with the scan.

A folder claims a port when one of its tracked `.sh`, `.py`, `.yml` or
`.yaml` files (outside a `tests/` folder and a test file) holds, after the
comments are cut:

- an override default: `PORT="${X_PORT:-8084}"`, in a script or a compose file;
- a literal assignment: `SERVE_PORT=8769`, `DEFAULT_PORT = 8767`;
- a `--port 9749` or `--port=9749` argument;
- a host-and-container pair: `-p 127.0.0.1:19530:19530`, a compose
  `"127.0.0.1:6381:6379"`, a `kubectl port-forward` pair `8080:8080`; the host
  side counts, the container side does not.

Not read, so a port named only this way is invisible here: a URL
(`http://127.0.0.1:8083/mcp`, a client), `.toml`, `.conf`, `.plist` and
`.env.example` files, a host port that only a variable names
(`"${AURA_ORCH_PORT}:3030"` counts through its default in the script), and a
port built in code.

The README table has one row per folder: its name in the first cell, its
ports in the second as backticked numbers.

`ALLOWED` is the short list of (folder, port) pairs that point at a service
another folder runs, each with the reason. Every entry must still match a
hit, so the list only shrinks.

Stdlib and git only. This runs in the `tests/ci-native.sh` preflight, before
the virtualenv exists. `python3 tests/test_ports.py` from the repository
root is the whole invocation, and the preflight's unwired-suite guard fails
when `tests/ci-native.sh` stops naming it.
"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: (folder, port) -> why this folder names the port without binding it.
ALLOWED = {
    ("local-machine-setup", 8767): "checks the Tailscale route to the dashboard's port",
    ("local-genai-traces", 4339): "names the hub's tailnet route, which local-machine-setup serves",
    ("mezmo-aura", 4337): "points Aura's exporter at the local-genai-traces collector",
}

NOT_TOOLS = {"docs", "lib", "tests"}
SOURCE = (".sh", ".py", ".yml", ".yaml")
COMMENT = re.compile(r"(^|\s)#.*$")
ADDR = r"(?:\d{1,3}(?:\.\d{1,3}){3}:)?"
VAR = r"\b(?:\w*_)?PORT(?:_\w*)?"  # PORT, HTTP_PORT, PORT_X; not REPORT
SHAPES = (
    re.compile(r"\$\{" + VAR + r":-(\d+)\}"),
    re.compile(VAR + r"\s*=\s*[\"']?(\d+)[\"']?(?![\w.])"),
    re.compile(r"--port[= ][\"']?(\d+)"),
    re.compile(r"(?:^|[\s\"'])" + ADDR + r"(\d{4,5}):\d{2,5}(?![\d.])"),
)
ROW = re.compile(r"^\|\s*(\S+)\s*\|\s*((?:`\d+`(?:,\s*)?)+)\s*\|")


def tracked(root=ROOT):
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True,
    )
    return [p for p in out.stdout.split("\0") if p]


def is_source(path):
    parts = path.split("/")
    name = parts[-1]
    return (
        len(parts) > 1
        and parts[0] not in NOT_TOOLS
        and not parts[0].startswith(".")
        and "tests" not in parts[:-1]
        and path.endswith(SOURCE)
        and not name.startswith(("test_", "test-"))
    )


def claims(root, paths):
    """{(folder, port): ["path:line", ...]} for every port a tool folder binds."""
    found = {}
    for path in paths:
        if not is_source(path):
            continue
        try:
            text = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            code = COMMENT.sub("", line)
            for shape in SHAPES:
                for port in shape.findall(code):
                    key = (path.split("/", 1)[0], int(port))
                    found.setdefault(key, []).append(f"{path}:{number}")
    return found


def table(text):
    """{folder: {port, ...}} from the rows of README § Ports."""
    section = re.search(r"^## Ports\n(.*?)(?=^## )", text, re.S | re.M)
    rows = {}
    for line in (section.group(1) if section else "").splitlines():
        row = ROW.match(line)
        if row:
            rows[row.group(1)] = {int(p) for p in re.findall(r"\d+", row.group(2))}
    return rows


def problems(root, paths, honour_allowed=True):
    found = claims(root, paths)
    if honour_allowed:
        found = {k: v for k, v in found.items() if k not in ALLOWED}
    out = []
    holders = {}
    for folder, port in sorted(found):
        holders.setdefault(port, []).append(folder)
    for port, folders in sorted(holders.items()):
        if len(folders) > 1:
            where = "; ".join(f"{f} ({found[(f, port)][0]})" for f in folders)
            out.append(f"port {port} is claimed twice: {where}")
    scanned = {}
    for folder, port in found:
        scanned.setdefault(folder, set()).add(port)
    readme = (root / "README.md").read_text(encoding="utf-8") if (root / "README.md").exists() else ""
    listed = table(readme)
    for folder in sorted(set(scanned) | set(listed)):
        have, want = listed.get(folder), scanned.get(folder)
        if have is None:
            out.append(f"README § Ports has no row for {folder}, which binds {sorted(want)}")
        elif want is None:
            out.append(f"README § Ports has a row for {folder}, which binds no port")
        elif have != want:
            out.append(
                f"README § Ports row for {folder} lists {sorted(have)} but the code binds {sorted(want)}"
            )
    return out


class TheTree(unittest.TestCase):
    def test_no_two_tools_share_a_port_and_the_readme_table_matches(self):
        found = problems(ROOT, tracked())
        if found:
            self.fail(
                "fix the clash, the README § Ports table, or add an ALLOWED entry with its "
                "reason (tests/test_ports.py):\n  " + "\n  ".join(found)
            )

    def test_every_allowed_entry_has_a_reason_and_is_still_needed(self):
        found = claims(ROOT, tracked())
        for key, reason in ALLOWED.items():
            with self.subTest(key=key):
                self.assertTrue(reason.strip(), f"{key}: an entry needs its reason")
                self.assertIn(key, found, f"{key}: no longer names the port; delete the entry")

    def test_the_preflight_runs_this_file(self):
        lines = (ROOT / "tests/ci-native.sh").read_text(encoding="utf-8").splitlines()
        self.assertIn(
            "python3 tests/test_ports.py", lines,
            "the tests/ci-native.sh preflight no longer runs this file",
        )


README = "## Ports\n\n| Service | Ports |\n| --- | --- |\n{rows}\n## Tests\n"


class TheScan(unittest.TestCase):
    def check(self, files, rows):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            files = dict(files, **{"README.md": README.format(rows=rows)})
            for rel, body in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(body, encoding="utf-8")
            return problems(root, sorted(files))

    def test_two_folders_on_one_port_fail_naming_both(self):
        found = self.check(
            {
                "local-a/a.sh": 'PORT="${A_PORT:-8101}"\n',
                "local-b/docker-compose.yml": '    ports:\n      - "127.0.0.1:8101:80"\n',
            },
            "| local-a | `8101` |\n| local-b | `8101` |\n",
        )
        self.assertEqual(1, len(found), found)
        self.assertIn("port 8101 is claimed twice", found[0])
        self.assertIn("local-a (local-a/a.sh:1)", found[0])
        self.assertIn("local-b (local-b/docker-compose.yml:2)", found[0])

    def test_a_missing_extra_and_wrong_row_each_fail(self):
        files = {
            "local-a/a.sh": 'PORT="${A_PORT:-8101}"\nUI_PORT=8102\n',
            "local-b/b.sh": "docker run -p 127.0.0.1:8103:80 x\n",
        }
        self.assertEqual([], self.check(files, "| local-a | `8101`, `8102` |\n| local-b | `8103` |\n"))
        self.assertEqual(
            ["README § Ports has no row for local-b, which binds [8103]"],
            self.check(files, "| local-a | `8101`, `8102` |\n"),
        )
        self.assertEqual(
            ["README § Ports has a row for local-gone, which binds no port"],
            self.check(files, "| local-a | `8101`, `8102` |\n| local-b | `8103` |\n| local-gone | `8104` |\n"),
        )
        self.assertEqual(
            ["README § Ports row for local-a lists [8101, 8109] but the code binds [8101, 8102]"],
            self.check(files, "| local-a | `8101`, `8109` |\n| local-b | `8103` |\n"),
        )

    def test_each_shape_is_read(self):
        found = claims(Path("/nonexistent"), [])
        self.assertEqual({}, found)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            body = (
                'A="${X_PORT:-7001}"\nSERVE_PORT=7002\nDEFAULT_PORT = 7003\n'
                "run --port 7004\nrun --port=7005\n"
                "docker run -p 7006:80 -p 127.0.0.1:7007:80 x\n"
                '  - "127.0.0.1:${Y_PORT:-7008}:80"\n  - "7009:80"\nforward frontend 7010:7010 label\n'
            )
            (root / "local-a").mkdir()
            (root / "local-a/a.sh").write_text(body, encoding="utf-8")
            ports = sorted(p for _, p in claims(root, ["local-a/a.sh"]))
        self.assertEqual(list(range(7001, 7011)), ports)

    def test_comments_urls_tests_and_container_sides_are_not_read(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            files = {
                "local-a/a.sh": (
                    '# PORT="${A_PORT:-7101}"\nX=1  # -p 7102:80\n'
                    "curl http://127.0.0.1:7103/mcp\nps -o etime= -p 7104\n"
                    'docker run -p 127.0.0.1:"$P":7105 x\nexpire 12:30\n'
                ),
                "local-a/tests/test_a.py": "PORT = 7106\n",
                "local-a/test-a.sh": "PORT=7107\n",
                "local-a/README.md": "PORT=7108\n",
                "docs/x.sh": "PORT=7109\n",
            }
            for rel, body in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(body, encoding="utf-8")
            self.assertEqual({}, claims(root, sorted(files)))

    def test_an_allowed_pair_is_skipped_but_still_found_without_the_list(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "local-machine-setup").mkdir()
            (root / "local-machine-setup/m.sh").write_text('P="${SD_DASHBOARD_PORT:-8767}"\n', encoding="utf-8")
            (root / "README.md").write_text(README.format(rows=""), encoding="utf-8")
            paths = ["local-machine-setup/m.sh"]
            self.assertEqual([], problems(root, paths))
            self.assertEqual(1, len(problems(root, paths, honour_allowed=False)))


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
