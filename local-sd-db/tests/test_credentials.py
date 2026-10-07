"""Credential presence and expiry (sd:2203): facts into a heartbeat, never a value.

Every probe is stubbed: `get` stands in for the HTTPS GET and `run` for `gh`
and `claude`, so no test reaches the network or reads a real credential. The
tokens below are synthetic; the test that matters most greps the whole
database and the verb's output for them.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import connect, credentials, initialise
from sd_db.jobs import cli

PAT = "ghp_example_SECRET_pat_value"
HA = "ha_example_SECRET_token_value"
ENV = {"GITHUB_PERSONAL_ACCESS_TOKEN": PAT, "HA_TOKEN": HA, "HA_URL": "http://ha.example.test:8123/"}
MCP_OUT = ("Checking MCP server health...\n\n"
           "github: https://api.example.test/mcp (HTTP) - ✓ Connected\n"
           "plugin:slack:slack: https://slack.example.test/mcp (HTTP) - ! Needs authentication\n"
           "local: /usr/bin/example --token=" + PAT + " - ✗ Failed to connect\n")


class Fakes:
    def __init__(self, *, github=(200, {"github-authentication-token-expiration": "2026-11-01 00:00:00 UTC"}),
                 ha=(401, {}), gh=0, mcp=(0, MCP_OUT)):
        self.answers = {credentials.GITHUB_USER: github, "http://ha.example.test:8123/api/": ha}
        self.programs = {"gh": (gh, "Logged in to github.com account example (keyring)\n- Token: " + PAT), "claude": mcp}
        self.gets, self.runs = [], []

    def get(self, url, token):
        self.gets.append(url)
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def run(self, argv, timeout):
        self.runs.append(argv)
        return self.programs[argv[0]]


class Probes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialise(Path(self.tmp.name) / "sd.db")
        self.db = connect(Path(self.tmp.name) / "sd.db")
        self.addCleanup(self.db.close)

    def check(self, env=ENV, **fakes):
        fake = Fakes(**fakes)
        body = credentials.check(self.db, env=env, get=fake.get, run=fake.run, now="2026-10-06T09:10:00Z")
        return {probe["id"]: probe for probe in body["probes"]}, fake

    def test_each_probe_records_presence_validity_and_expiry(self):
        probes, fake = self.check()
        self.assertEqual(probes["github_pat"], {"id": "github_pat", "name": "GitHub PAT (GITHUB_PERSONAL_ACCESS_TOKEN)",
                                                "present": True, "valid": True, "expires": "2026-11-01T00:00:00Z"})
        self.assertEqual((probes["ha_token"]["present"], probes["ha_token"]["valid"]), (True, False))
        self.assertEqual(probes["gh"]["signed_in"], True)
        self.assertEqual(probes["mcp"]["servers"], [{"name": "github", "status": "connected"},
                                                    {"name": "plugin:slack:slack", "status": "needs_auth"},
                                                    {"name": "local", "status": "failed"}])
        self.assertEqual(fake.runs, [["gh", "auth", "status", "--hostname", "github.com"], ["claude", "mcp", "list"]])
        row = self.db.execute("SELECT timestamp, body FROM state WHERE kind = 'heartbeat' AND key = ?",
                              (credentials.HEARTBEAT_KEY,)).fetchone()
        self.assertEqual(row["timestamp"], "2026-10-06T09:10:00Z")
        self.assertEqual(json.loads(row["body"])["probes"][0]["expires"], "2026-11-01T00:00:00Z")

    def test_no_value_reaches_the_database_or_the_description(self):
        probes, _ = self.check()
        dump = "\n".join(self.db.iterdump())
        lines = "\n".join(credentials.describe(probe) for probe in probes.values())
        for secret in (PAT, HA):
            self.assertNotIn(secret, dump)
            self.assertNotIn(secret, lines)
        self.assertNotIn("example.test", dump, "an MCP target or HA URL was stored")

    def test_an_absent_token_is_recorded_absent_and_never_sent(self):
        probes, fake = self.check(env={})
        self.assertEqual((probes["github_pat"]["present"], probes["ha_token"]["present"]), (False, False))
        self.assertNotIn("valid", probes["github_pat"])
        self.assertEqual(fake.gets, [])

    def test_an_unreachable_service_is_a_reason_not_a_verdict(self):
        probes, _ = self.check(github=OSError("down"), ha=(500, {}), gh=1, mcp=(1, ""))
        self.assertEqual(probes["github_pat"]["reason"], "not reached: OSError")
        self.assertNotIn("valid", probes["github_pat"])
        self.assertEqual((probes["ha_token"]["valid"], probes["ha_token"]["reason"]), (None, "answered HTTP 500"))
        self.assertEqual(probes["gh"]["signed_in"], False)
        self.assertEqual(probes["mcp"], {"id": "mcp", "name": "MCP servers (claude mcp list)",
                                         "reason": "claude mcp list exited 1"})

    def test_ha_without_a_url_is_present_and_untested(self):
        probes, fake = self.check(env={"HA_TOKEN": HA})
        self.assertEqual(probes["ha_token"]["reason"], "HA_URL unset, so the token was not tested")
        self.assertNotIn("http://ha.example.test:8123/api/", fake.gets)

    def test_a_pat_with_no_expiry_header_has_none(self):
        probes, _ = self.check(github=(200, {}))
        self.assertEqual((probes["github_pat"]["valid"], probes["github_pat"]["expires"]), (True, None))


class TheVerb(unittest.TestCase):
    def setUp(self):
        # A seam the verb misses must fail here, not reach GitHub or run gh and claude.
        for target, name in ((credentials, "urlopen"), (credentials.subprocess, "run")):
            patcher = mock.patch.object(target, name, side_effect=AssertionError(f"the test reached the real {name}"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_credentials_records_the_heartbeat_and_prints_no_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".local/share/sd/sd.db"
            target.parent.mkdir(parents=True)
            initialise(target)
            fake = Fakes()
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"HOME": str(home), **ENV}), \
                    mock.patch.object(credentials, "_get", fake.get), mock.patch.object(credentials, "_run", fake.run), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["credentials"])
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn("github_pat: present True, valid True, expires 2026-11-01T00:00:00Z", out.getvalue())
            self.assertIn("mcp: 1 of 3 servers connected", out.getvalue())
            for secret in (PAT, HA):
                self.assertNotIn(secret, out.getvalue() + err.getvalue())
            db = connect(target)
            try:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM state WHERE kind = 'heartbeat' AND key = ?",
                                            (credentials.HEARTBEAT_KEY,)).fetchone()[0], 1)
            finally:
                db.close()

    def test_an_argument_is_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["credentials", "--now"]), 1)
        self.assertIn("takes no arguments", err.getvalue())


if __name__ == "__main__":
    unittest.main()
