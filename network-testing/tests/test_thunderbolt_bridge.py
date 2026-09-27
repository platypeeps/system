"""thunderbolt-bridge.sh against stubbed system tools.

Every tool the script reaches through PATH is a stub here: `id` answers 0,
`networksetup`, `launchctl`, `killall` and `sleep` log their arguments, and
`ifconfig` reports a bridge0 that already exists with its members, so the
branch that edits preferences.plist never runs. Nothing touches real
network configuration.
"""

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "thunderbolt-bridge.sh"

STUBS = {
    "id": 'echo 0\n',
    "sleep": ':\n',
    "killall": 'echo "killall $*" >> "$TB_LOG"\n',
    "launchctl": textwrap.dedent('''\
        echo "launchctl $*" >> "$TB_LOG"
        [ "$1 $2" = "print system/io.exo.networksetup" ] && [ "${TB_EXO_LOADED:-0}" = 1 ] && exit 0
        exit 113
        '''),
    "ifconfig": textwrap.dedent('''\
        [ "$1" = bridge0 ] || exit 1
        printf '\\tmember: en1 flags=3<LEARNING,DISCOVER>\\n'
        printf '\\tmember: en2 flags=3<LEARNING,DISCOVER>\\n'
        printf '\\tinet 198.51.100.10 netmask 0xffffff00\\n'
        '''),
    "networksetup": textwrap.dedent('''\
        echo "networksetup $*" >> "$TB_LOG"
        case "$1" in
          -listallhardwareports)
            printf 'Hardware Port: Thunderbolt 1\\nDevice: en1\\n\\n'
            printf 'Hardware Port: Thunderbolt 2\\nDevice: en2\\n' ;;
          -getcurrentlocation) echo Automatic ;;
          -listlocations) echo Automatic ;;
          -listallnetworkservices)
            echo 'An asterisk (*) denotes that a network service is disabled.'
            echo 'Wi-Fi'
            printf '%s\\n' "${TB_SERVICE_LINE:-Thunderbolt Bridge}" ;;
        esac
        '''),
}


HOSTS_CONF = """\
# test presets, documentation addresses only
bridge  host-a  198.51.100.10
bridge  host-b  198.51.100.11
"""


class ThunderboltBridgeCase(unittest.TestCase):
    def run_script(self, target="host-a", **env):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in STUBS.items():
            stub = bin_dir / name
            stub.write_text("#!/bin/sh\n" + body)
            stub.chmod(0o755)
        log = tmp / "calls.log"
        log.touch()
        hosts = tmp / "hosts.conf"
        hosts.write_text(HOSTS_CONF)
        full_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp), "TB_LOG": str(log),
                    "NETWORK_TESTING_HOSTS": str(hosts)}
        full_env.update(env)
        proc = subprocess.run(["sh", str(SCRIPT), target], env=full_env,
                              capture_output=True, text=True, timeout=30)
        return proc, log.read_text().splitlines()

    def test_an_enabled_service_is_reused(self):
        proc, calls = self.run_script()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertFalse([c for c in calls if "-createnetworkservice" in c], calls)
        self.assertIn("networksetup -setnetworkserviceenabled Thunderbolt Bridge on", calls)
        self.assertIn("networksetup -setmanual Thunderbolt Bridge 198.51.100.10 255.255.255.0 198.51.100.1", calls)

    def test_a_bridge_name_resolves_from_the_hosts_file(self):
        proc, calls = self.run_script(target="host-b")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("networksetup -setmanual Thunderbolt Bridge 198.51.100.11 255.255.255.0 198.51.100.1", calls)

    def test_an_unknown_bridge_name_stops_before_any_change(self):
        proc, calls = self.run_script(target="nope")
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("no bridge record 'nope'", proc.stderr)
        self.assertEqual([], calls)

    def test_a_disabled_service_is_reused_not_duplicated(self):
        """networksetup marks a disabled service with a leading `*`; an exact
        match on the bare name missed it and created a second service (sd:1172)."""
        proc, calls = self.run_script(TB_SERVICE_LINE="*Thunderbolt Bridge")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertFalse([c for c in calls if "-createnetworkservice" in c], calls)
        self.assertIn("networksetup -setnetworkserviceenabled Thunderbolt Bridge on", calls)

    def test_a_missing_service_is_created(self):
        proc, calls = self.run_script(TB_SERVICE_LINE="Ethernet")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("networksetup -createnetworkservice Thunderbolt Bridge Thunderbolt Bridge", calls)

    def test_a_loaded_exo_daemon_stops_the_run_before_any_change(self):
        """io.exo.networksetup deletes bridge0 again, so a rebuild while it
        is loaded is undone; the script must refuse before it edits anything
        (sd:1172)."""
        proc, calls = self.run_script(TB_EXO_LOADED="1")
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("io.exo.networksetup", proc.stderr)
        self.assertIn("launchctl bootout system/io.exo.networksetup", proc.stderr)
        self.assertFalse([c for c in calls if c.startswith(("networksetup", "killall"))], calls)


if __name__ == "__main__":
    unittest.main()
