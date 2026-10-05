"""Step 4 of the second-machine plan: `connect` reads `~/.config/sd/hub.json`.

`docs/work/2026-09-22-run-the-framework-from-a-second-machine/implement.md`.
The three checks the step names (absent file opens locally; a file and no
hub raises `HubUnreachable` and creates nothing; a file beside a local
database refuses), and gap x1: `default_path()` on a satellite is a path
whose `exists()` asks the hub, and `connect` treats it as the default.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import create_item, database, hub, initialise, remote
from sd_db.database import connect, default_path, local_path

from tests.test_wire import Served


def files_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Satellite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "satellite"
        self.home.mkdir()

    def name_hub(self, port: int, token_file: Path | None = None) -> Path:
        config = self.home / ".config" / "sd" / "hub.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        body = {"hub": "127.0.0.1", "port": port}
        if token_file is not None:
            body["token_file"] = str(token_file)
        config.write_text(json.dumps(body))
        return config

    def title(self, path: Path, item: int) -> str:
        raw = sqlite3.connect(path)
        try:
            return raw.execute("SELECT title FROM item WHERE id = ?", (item,)).fetchone()[0]
        finally:
            raw.close()


class NoHubFile(Satellite):
    def test_without_hub_json_connect_opens_the_local_file(self):
        initialise(home=self.home)
        self.assertIs(type(default_path(self.home)), type(Path()))
        connection = connect(home=self.home)
        try:
            item = create_item(connection, kind="work", title="local")
        finally:
            connection.close()
        self.assertEqual(self.title(local_path(self.home), item), "local")


class HubFileNoServer(Satellite):
    def test_no_hub_raises_hub_unreachable_and_creates_nothing(self):
        port = closed_port()
        config = self.name_hub(port)
        before = files_under(self.home)
        for attempt in (lambda: connect(home=self.home),
                        lambda: connect(home=self.home, write=False),
                        lambda: connect(default_path(self.home)),
                        lambda: default_path(self.home).exists()):
            with self.assertRaises(remote.HubUnreachable) as caught:
                attempt()
            self.assertIn(f"127.0.0.1:{port}", str(caught.exception))
            self.assertIn(str(config), str(caught.exception))
        with self.assertRaisesRegex(remote.HubOnly, "init and migrate runs on the sd hub only"):
            initialise(home=self.home)
        self.assertEqual(files_under(self.home), before)
        self.assertFalse((self.home / ".local").exists())

    def test_an_explicit_other_path_still_opens_locally(self):
        """The Jev meter's file, a backup target: not the record, not the hub's."""
        self.name_hub(closed_port())
        other = self.root / "meter.db"
        initialise(other)
        connection = connect(other)
        try:
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
        finally:
            connection.close()

    def test_a_malformed_hub_json_is_refused_by_name(self):
        config = self.name_hub(1)
        config.write_text('{"port": 8769}')
        with self.assertRaisesRegex(hub.HubConfigError, "names no 'hub'"):
            connect(home=self.home)
        self.assertFalse((self.home / ".local").exists())
        # Only the default reads the file: another database still opens.
        other = self.root / "backup.db"
        initialise(other)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            connection = connect(other)
        try:
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
        finally:
            connection.close()

    def test_an_unreadable_home_folder_does_not_block_another_database(self):
        """Neither the selector's folder nor the default's is touched for it."""
        self.name_hub(closed_port())
        other = self.root / "meter.db"
        initialise(other)
        real = os.path.realpath

        def refused(path, *args, **kwargs):
            # A path under the home cannot be resolved, as behind a folder
            # this user cannot search; any other path resolves.
            if os.fspath(path).startswith(str(self.home)):
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real(path, *args, **kwargs)

        folder = self.home / ".config"
        folder.chmod(0)
        self.addCleanup(folder.chmod, 0o755)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                mock.patch("os.path.realpath", refused):
            connection = connect(other)
        try:
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
        finally:
            connection.close()


class HubFileBesideADatabase(Satellite):
    def test_hub_json_beside_a_local_database_refuses(self):
        initialise(home=self.home)
        self.name_hub(closed_port())
        for attempt in (lambda: connect(home=self.home), lambda: default_path(self.home).exists()):
            with self.assertRaisesRegex(hub.HubConflict, "a hub or a satellite, not both"):
                attempt()

    def test_the_default_through_a_symlink_is_still_the_default(self):
        """A resolved or aliased spelling (/var against /private/var) must not
        slip past the selector to open, or create, a divergent local record."""
        initialise(home=self.home)
        self.name_hub(closed_port())
        alias = self.root / "alias"
        alias.symlink_to(self.home)
        aliased = alias / local_path(self.home).relative_to(self.home)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            for attempt in (lambda: connect(aliased), lambda: connect(aliased, create=True)):
                with self.assertRaisesRegex(hub.HubConflict, "a hub or a satellite, not both"):
                    attempt()


class ThroughTheHub(Satellite):
    """Gap x1: pack callers that name `default_path()` reach the hub unedited."""

    def setUp(self):
        super().setUp()
        self.served = Served(self.root, "hub.db")
        self.addCleanup(self.served.stop)
        self.name_hub(self.served.port, self.served.token_file)

    def test_default_path_is_hub_aware_and_every_caller_shape_writes_to_the_hub(self):
        path = default_path(self.home)
        self.assertIsInstance(path, hub.HubPath)
        self.assertEqual(path, local_path(self.home))
        self.assertEqual(str(path), str(local_path(self.home)))
        self.assertTrue(path.exists())
        self.assertNotIsInstance(path.parent, hub.HubPath)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            self.assertTrue(database.default_path().exists())  # sd-status's shape
            shapes = {
                "connect()": lambda: connect(),
                "connect(default_path())": lambda: connect(default_path()),  # sd-ship's shape
                "connect(Path(default_path()))": lambda: connect(Path(str(default_path()))),  # sd-review's
            }
            for shape, opened in shapes.items():
                with self.subTest(shape=shape):
                    connection = opened()
                    try:
                        self.assertIsInstance(connection, remote.Connection)
                        item = create_item(connection, kind="work", title=shape)
                    finally:
                        connection.close()
                    self.assertEqual(self.title(self.served.database, item), shape)
        self.assertFalse(local_path(self.home).exists())
        self.assertFalse((self.home / ".local").exists())


if __name__ == "__main__":
    unittest.main()
