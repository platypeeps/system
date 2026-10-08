"""sd:3075, design sd:3003: the lane host decides who takes the ship lock.

`repo.lane_host` names the machine that runs a repository's lane; NULL is the
hub. `ship.hosts_lane` answers whether this machine is that host, and
`ship.repository_lock` refuses with `LaneElsewhere` on any other machine,
before it locks anything. A satellite that hosts a lane takes its lock under
its own state folder, where it raised `HubOnly` before.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import database, initialise, ship, upsert_repo
from sd_db.database import default_path

from tests.test_hub import Satellite
from tests.test_wire import Served

REPOSITORY = "example/one"
REMOTE = "git@github.com:example/one.git"
HUB = "hub.example.test:8765"


def register(path: Path, lane_host: str | None, remote: str = REMOTE) -> None:
    connection = database.connect(path)
    try:
        upsert_repo(connection, "/srv/one", remote=remote)
        connection.execute("UPDATE repo SET lane_host = ? WHERE path = '/srv/one'", (lane_host,))
    finally:
        connection.close()


class HostsLane(unittest.TestCase):
    """Acceptance 5. `served_by` says hub or satellite; the host name is patched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.enterContext(mock.patch.object(ship, "this_host", return_value="build-2"))

    def hosts(self, served: str | None, lane_host: str | None = None, remote: str = REMOTE) -> bool:
        register(self.path, lane_host, remote)
        connection = database.connect(self.path)
        try:
            with mock.patch.object(ship, "served_by", return_value=served):
                return ship.hosts_lane(connection, self.path, REPOSITORY)
        finally:
            connection.close()

    def test_null_is_the_hubs_lane(self):
        self.assertTrue(self.hosts(None))
        self.assertFalse(self.hosts(HUB))

    def test_a_named_host_is_that_machines_lane_alone(self):
        self.assertTrue(self.hosts(HUB, "build-2"))
        self.assertFalse(self.hosts(None, "build-3"))
        self.assertTrue(self.hosts(None, "build-2"))

    def test_the_slug_matches_whatever_its_case(self):
        register(self.path, "build-2", "https://github.com/Example/One.git")
        connection = database.connect(self.path)
        self.addCleanup(connection.close)
        with mock.patch.object(ship, "served_by", return_value=HUB):
            self.assertTrue(ship.hosts_lane(connection, self.path, "Example/One"))

    def test_an_unmatched_remote_reads_null(self):
        self.assertTrue(self.hosts(None, "build-3", remote="git@github.com:example/other.git"))
        self.assertFalse(self.hosts(HUB, "build-2", remote="git@github.com:example/other.git"))

    def test_a_database_without_the_column_reads_null(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        self.addCleanup(raw.close)
        raw.execute("CREATE TABLE repo (path TEXT, remote TEXT)")
        raw.execute("INSERT INTO repo VALUES ('/srv/one', ?)", (REMOTE,))
        with mock.patch.object(ship, "served_by", return_value=None):
            self.assertTrue(ship.hosts_lane(raw, self.path, REPOSITORY))
        with mock.patch.object(ship, "served_by", return_value=HUB):
            self.assertFalse(ship.hosts_lane(raw, self.path, REPOSITORY))


class TheLockOnTheHub(unittest.TestCase):
    """Acceptance 6 on the hub: a lane moved away refuses and locks nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.enterContext(mock.patch.object(ship, "this_host", return_value="hub-mac"))
        self.enterContext(mock.patch.object(ship, "served_by", return_value=None))

    def test_a_lane_on_another_host_refuses_naming_host_dashboard_and_verb_in_order(self):
        register(self.path, "build-2")
        with self.assertRaises(ship.LaneElsewhere) as caught:
            with ship.repository_lock(self.path, REPOSITORY):
                self.fail("the lock was taken on a machine that does not host the lane")
        error = caught.exception
        self.assertEqual((error.code, error.host, error.path), ("lane_elsewhere", "build-2", "/srv/one"))
        self.assertEqual(str(error), (
            "The lane for example/one runs on build-2, not on this machine.\n"
            "Run it there, or move the lane: dashboard, Management, /srv/one, Move lane;\n"
            "or sd-db.sh repo lane-host /srv/one hub-mac."))
        self.assertFalse((self.path.parent / ship.LOCK_DIRECTORY).exists())

    def test_the_hubs_own_lane_locks_beside_the_database(self):
        register(self.path, None)
        with ship.repository_lock(self.path, REPOSITORY):
            pass
        self.assertTrue((self.path.parent / ship.LOCK_DIRECTORY).is_dir())


class TheLockOnASatellite(Satellite):
    """Acceptances 6 and 7 on a satellite, over a real served hub."""

    def setUp(self):
        super().setUp()
        self.served = Served(self.root)
        self.addCleanup(self.served.stop)
        self.name_hub(self.served.port, self.served.token_file)
        self.state = self.root / "state"
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.state)}))
        self.enterContext(mock.patch.object(ship, "this_host", return_value="build-2"))

    def test_a_satellite_that_hosts_the_lane_takes_its_own_lock(self):
        register(self.served.database, "build-2")
        with ship.repository_lock(default_path(self.home), REPOSITORY, holder={"item": 7}):
            locks = ship.lock_files(default_path(self.home))
            self.assertEqual([(entry["state"], entry["item"]) for entry in locks], [("held", 7)])
            self.assertEqual(Path(locks[0]["path"]).parent, self.state / "sd" / ship.LOCK_DIRECTORY)
        self.assertFalse((self.home / ".local").exists())

    def test_a_satellite_refuses_a_lane_it_does_not_host_and_creates_nothing(self):
        register(self.served.database, "build-3")
        with self.assertRaises(ship.LaneElsewhere) as caught:
            with ship.repository_lock(default_path(self.home), REPOSITORY):
                self.fail("the lock was taken on a machine that does not host the lane")
        self.assertEqual(caught.exception.host, "build-3")
        self.assertIn("or sd-db.sh repo lane-host /srv/one build-2.", str(caught.exception))
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
