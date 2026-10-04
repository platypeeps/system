"""Step 5 of the second-machine plan: a lost COMMIT is an unknown outcome.

`docs/work/2026-09-22-run-the-framework-from-a-second-machine/implement.md`,
criterion 11 cases (a) to (e) and (j), built without the prune (the
operator's ruling of 2026-10-04, Q1 = B), and the read transactions of gap
C2. Each case counts rows before and after on the hub's file: the count
moves by exactly one where a commit is promised and by zero everywhere else.

The faults sit on the wire, in `Relay`, or in the hub's own process: a
server in this process whose answer, frame or session a test controls, or a
child server killed with SIGKILL at `serve._fault`.
"""

from __future__ import annotations

import io
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sd_db import create_item, database, initialise, reads, registry, remote, serve, usage
from sd_db.database import transaction
from sd_db.serve import read_token

from .test_registry import SHIPPED

HERE = Path(__file__).resolve().parents[1]
#: Long enough for a loaded machine to restart a child server inside it.
RESTART_BOUND = 60.0


def count(path: Path, table: str = "probe") -> int:
    """Rows on the hub's file, read beside the server, not through it."""
    raw = sqlite3.connect(path)
    try:
        return raw.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        raw.close()


def recorded(path: Path, rid: str) -> bool:
    raw = sqlite3.connect(path)
    try:
        return raw.execute("SELECT 1 FROM request_outcome WHERE id = ?", (rid,)).fetchone() is not None
    finally:
        raw.close()


def fresh_database(root: Path) -> Path:
    path = root / "sd.db"
    initialise(path)
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    raw.commit()
    raw.close()
    return path


class InProcess:
    """`serve.Server` on 127.0.0.1 in a thread of this process."""

    def __init__(self, path: Path, *, idle_timeout: float = serve.IDLE_TIMEOUT) -> None:
        self.stream = io.StringIO()
        self.token = secrets.token_urlsafe(16)
        self.server = serve.Server(0, path, serve.Log(self.stream), self.token,
                                   path.with_name("unused.token"), idle_timeout=idle_timeout)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


class Relay:
    """A frame-aware relay between clients and a server, which spoils one COMMIT.

    `fault` decides what happens to the first `COMMIT` frame that carries a
    request id: `drop-answer` forwards it and loses the hub's answer,
    `drop-frame` loses the frame and closes both sides, and `hold` keeps the
    frame (and the hub's session) until `release`. The client's side closes
    in every case, as a broken link would. Every other frame, and every
    later session, passes through, unless `refusing` is set: then a new
    session is closed as soon as it arrives. `refuse_after` sets it when
    the fault fires: a hub that cannot be asked.
    """

    def __init__(self, port: int, fault: str, *, refuse_after: bool = False) -> None:
        self.target = port
        self.fault = fault
        self.armed = True
        self.refusing = False
        self.refuse_after = refuse_after
        self.held: tuple[dict, socket.socket] | None = None
        self.fired = threading.Event()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.sockets: list[socket.socket] = [self.listener]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            if self.refusing:
                client.close()
                continue
            self.sockets.append(client)
            threading.Thread(target=self._pump, args=(client,), daemon=True).start()

    def _spoils(self, frame: dict) -> bool:
        committing = (frame.get("op") == "commit"
                      or (frame.get("op") == "execute" and remote.statement(frame.get("sql", "")) == "commit"))
        return self.armed and committing and frame.get("rid") is not None

    def _pump(self, client: socket.socket) -> None:
        hub = socket.create_connection(("127.0.0.1", self.target))
        self.sockets.append(hub)
        try:
            while True:
                frame = remote.read_frame(client)
                if self._spoils(frame):
                    self.armed = False
                    self.refusing = self.refuse_after
                    if self.fault == "drop-answer":
                        remote.send_frame(hub, frame)
                        remote.read_frame(hub)
                        hub.close()
                    elif self.fault == "drop-frame":
                        hub.close()
                    else:
                        self.held = (frame, hub)
                    client.close()
                    self.fired.set()
                    return
                remote.send_frame(hub, frame)
                remote.send_frame(client, remote.read_frame(hub))
        except (OSError, EOFError):
            client.close()
            if self.held is None or self.held[1] is not hub:
                hub.close()

    def release(self) -> dict:
        """Deliver the held `COMMIT`; the hub's answer goes nowhere."""
        frame, hub = self.held
        remote.send_frame(hub, frame)
        answer = remote.read_frame(hub)
        hub.close()
        return answer

    def stop(self) -> None:
        for each in self.sockets:
            try:
                each.close()
            except OSError:
                pass


class OutcomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = fresh_database(self.root)
        self.hub = InProcess(self.path)
        self.addCleanup(self.hub.stop)

    def connect(self, port: int | None = None, **options) -> remote.Connection:
        options.setdefault("token", self.hub.token)
        connection = remote.connect("127.0.0.1", port or self.hub.port, self.path, **options)
        self.addCleanup(connection.close)
        return connection

    def relay(self, fault: str, **options) -> Relay:
        relay = Relay(self.hub.port, fault, **options)
        self.addCleanup(relay.stop)
        return relay

    def raw(self, *, write: bool = True) -> socket.socket:
        """A session that sends frames as written, with no client logic."""
        sock = socket.create_connection(("127.0.0.1", self.hub.port))
        self.addCleanup(sock.close)
        answer = self.ask(sock, "open", path=str(self.path), write=write, create=False,
                          busy_timeout=5000, token=self.hub.token, **remote.handshake())
        self.assertTrue(answer["ok"], answer)
        return sock

    @staticmethod
    def ask(sock: socket.socket, op: str, *, rid: str | None = None, **fields) -> dict:
        remote.send_frame(sock, remote.request(op, rid=rid, **fields))
        return remote.read_frame(sock)

    def execute(self, sock: socket.socket, sql: str, *, rid: str | None = None) -> dict:
        return self.ask(sock, "execute", rid=rid, sql=sql, params=[])


class TheLostCommit(OutcomeCase):
    """Criterion 11, cases (a), (d), (e) and (j), against a server in this process."""

    def test_a_dropped_answer_is_unknown_then_recorded(self):
        """(a) The hub committed and its answer was lost: `UnknownOutcome`
        while the hub cannot be asked, then `recorded`, one row."""
        relay = self.relay("drop-answer", refuse_after=True)
        client = self.connect(relay.port)
        with mock.patch.object(remote, "OUTCOME_BOUND", 0.5), \
                self.assertRaises(remote.UnknownOutcome) as raised:
            with transaction(client):
                client.execute("INSERT INTO probe (name) VALUES ('a')")
        rid = raised.exception.rid
        self.assertIn(rid, str(raised.exception))
        self.assertTrue(remote.REQUEST_ID.fullmatch(rid))
        self.assertEqual({answer for _, answer in client.outcomes}, {"unreachable"})
        relay.refusing = False
        self.assertEqual(client.outcome(rid), remote.RECORDED)
        self.assertEqual(count(self.path), 1)
        self.assertTrue(recorded(self.path, rid))

    def test_a_dropped_answer_settles_as_the_commit_it_was(self):
        """(a) With the hub reachable, the client asks at once: `recorded`,
        and the block returns as a commit does. One row."""
        relay = self.relay("drop-answer")
        client = self.connect(relay.port)
        with transaction(client):
            client.execute("INSERT INTO probe (name) VALUES ('a')")
        self.assertTrue(relay.fired.is_set())
        [(rid, answer)] = client.outcomes
        self.assertEqual(answer, remote.RECORDED)
        self.assertFalse(client.in_transaction)
        self.assertEqual(count(self.path), 1)
        self.assertTrue(recorded(self.path, rid))

    def test_a_commit_frame_held_in_transit_is_in_flight_until_it_lands(self):
        """(d) The COMMIT frame is delayed and the client asks: `in_flight`,
        nothing committed; the frame lands: `recorded`, one row."""
        relay = self.relay("hold")
        client = self.connect(relay.port)
        failures: list[BaseException] = []

        def body():
            try:
                with transaction(client):
                    client.execute("INSERT INTO probe (name) VALUES ('d')")
            except BaseException as error:  # reported by the main thread
                failures.append(error)

        worker = threading.Thread(target=body)
        worker.start()
        deadline = time.monotonic() + 30
        while not any(answer == remote.IN_FLIGHT for _, answer in client.outcomes):
            self.assertLess(time.monotonic(), deadline, client.outcomes)
            self.assertTrue(worker.is_alive(), failures)
            time.sleep(0.01)
        self.assertEqual(count(self.path), 0)
        answer = relay.release()
        worker.join(30)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(answer["ok"], answer)
        answers = [answer for _, answer in client.outcomes]
        self.assertEqual((answers[0], answers[-1]), (remote.IN_FLIGHT, remote.RECORDED))
        self.assertEqual(count(self.path), 1)
        self.assertTrue(recorded(self.path, client.outcomes[-1][0]))

    def test_an_owner_live_at_the_bound_is_an_unknown_outcome(self):
        """(d) at the bound: still `in_flight`, so `UnknownOutcome` names R
        and says a session owns it; nothing committed yet."""
        relay = self.relay("hold")
        client = self.connect(relay.port)
        with mock.patch.object(remote, "OUTCOME_BOUND", 0.3), \
                self.assertRaisesRegex(remote.UnknownOutcome, "still owns") as raised:
            with transaction(client):
                client.execute("INSERT INTO probe (name) VALUES ('d')")
        self.assertIn(raised.exception.rid, str(raised.exception))
        self.assertEqual(count(self.path), 0)
        relay.release()
        self.assertEqual(client.outcome(raised.exception.rid), remote.RECORDED)
        self.assertEqual(count(self.path), 1)

    def test_an_ask_the_hub_never_answers_ends_at_its_own_timeout(self):
        """A hub that accepts and never answers must not hold the client past
        the bound: each ask has its own timeout."""
        silent = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(silent.close)
        client = self.connect()
        client.port = silent.getsockname()[1]
        started = time.monotonic()
        with mock.patch.object(remote, "OUTCOME_ASK_TIMEOUT", 0.3), \
                self.assertRaises(remote.HubUnreachable):
            client.outcome(remote.new_request_id())
        self.assertLess(time.monotonic() - started, 5)

    def test_a_commit_for_an_id_this_session_does_not_own_is_refused(self):
        """(e) Only R's owner commits R: another session's COMMIT naming R is
        refused and not applied, with or without a transaction of its own."""
        owner = self.connect()
        owner.execute("BEGIN IMMEDIATE")
        owner.execute("INSERT INTO probe (name) VALUES ('e')")
        rid = owner._rid
        self.assertTrue(remote.REQUEST_ID.fullmatch(rid))
        other = self.raw()
        refused = self.execute(other, "COMMIT", rid=rid)
        self.assertFalse(refused["ok"])
        self.assertIn(f"COMMIT names request {rid}, which this session does not own", refused["error"]["text"])
        self.assertTrue(self.execute(other, "BEGIN")["ok"])
        refused = self.ask(other, "commit", rid=rid)
        self.assertFalse(refused["ok"])
        self.assertIn("does not own", refused["error"]["text"])
        self.assertTrue(refused["in_transaction"])
        self.assertTrue(self.execute(other, "ROLLBACK")["ok"])
        self.assertEqual(count(self.path), 0)
        self.assertEqual(owner.outcome(rid), remote.IN_FLIGHT)
        owner.execute("ROLLBACK")
        self.assertEqual(owner.outcome(rid), remote.ABSENT)
        self.assertEqual(count(self.path), 0)
        self.assertFalse(recorded(self.path, rid))

    def test_a_commit_naming_another_id_commits_and_records_nothing(self):
        """(e) A session inside its own write transaction cannot commit, or
        record, an id it does not own: not one that rolled back, not a new one."""
        sock = self.raw()
        rolled_back, mine = remote.new_request_id(), remote.new_request_id()
        self.assertTrue(self.execute(sock, "BEGIN IMMEDIATE", rid=rolled_back)["ok"])
        self.assertTrue(self.execute(sock, "ROLLBACK")["ok"])
        self.assertTrue(self.execute(sock, "BEGIN IMMEDIATE", rid=mine)["ok"])
        self.assertTrue(self.execute(sock, "INSERT INTO probe (name) VALUES ('e')")["ok"])
        for other in (rolled_back, remote.new_request_id()):
            with self.subTest(other=other):
                refused = self.execute(sock, "COMMIT", rid=other)
                self.assertFalse(refused["ok"])
                self.assertIn(f"COMMIT names request {other}, which this session does not own",
                              refused["error"]["text"])
                self.assertTrue(refused["in_transaction"])
        self.assertEqual(count(self.path), 0)
        asker = self.connect()
        self.assertEqual(asker.outcome(rolled_back), remote.ABSENT)
        self.assertTrue(self.execute(sock, "COMMIT", rid=mine)["ok"])
        self.assertEqual(count(self.path), 1)
        self.assertEqual((asker.outcome(rolled_back), asker.outcome(mine)), (remote.ABSENT, remote.RECORDED))

    def test_absent_leaves_the_block_and_the_verb_runs_again_under_a_new_id(self):
        """(j) The COMMIT never reached the hub, whose session closed: `absent`,
        so `TransactionLost(R)` leaves the block with nothing written. The
        verb run again writes one item, and its opening note is that item's."""
        relay = self.relay("drop-frame")
        first = self.connect(relay.port)
        with self.assertRaises(remote.TransactionLost) as raised:
            create_item(first, kind="work", title="lost")
        lost = raised.exception.rid
        self.assertIn(lost, str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, remote.UnknownOutcome)
        self.assertEqual(first.outcomes, [(lost, remote.ABSENT)])
        self.assertEqual((count(self.path, "item"), count(self.path, "note")), (0, 0))
        again = self.connect()
        item = create_item(again, kind="work", title="run again")
        self.assertEqual((count(self.path, "item"), count(self.path, "note")), (1, 1))
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("SELECT item FROM note").fetchone()[0], item)
        self.assertEqual(raw.execute("SELECT title FROM item WHERE id = ?", (item,)).fetchone()[0], "run again")
        [(rid,)] = raw.execute("SELECT id FROM request_outcome").fetchall()
        self.assertNotEqual(rid, lost)


class TheOwnership(OutcomeCase):
    """The rules behind the answers: ids, settlement and the idle timeout."""

    def test_a_write_transaction_without_an_id_is_refused(self):
        sock = self.raw()
        refused = self.execute(sock, "BEGIN IMMEDIATE")
        self.assertFalse(refused["ok"])
        self.assertIn("needs a request id", refused["error"]["text"])
        self.assertFalse(refused["in_transaction"])

    def test_an_id_is_used_once(self):
        rid = remote.new_request_id()
        sock = self.raw()
        self.assertTrue(self.execute(sock, "BEGIN IMMEDIATE", rid=rid)["ok"])
        self.assertTrue(self.execute(sock, "ROLLBACK")["ok"])
        again = self.execute(sock, "BEGIN IMMEDIATE", rid=rid)
        self.assertFalse(again["ok"])
        self.assertIn("was used before", again["error"]["text"])

    def test_a_recorded_id_is_refused_by_a_restarted_hub(self):
        client = self.connect()
        with transaction(client):
            client.execute("INSERT INTO probe (name) VALUES ('once')")
        raw = sqlite3.connect(self.path)
        [(rid,)] = raw.execute("SELECT id FROM request_outcome").fetchall()
        raw.close()
        client.close()
        self.hub.stop()
        self.hub = InProcess(self.path)
        self.addCleanup(self.hub.stop)
        refused = self.execute(self.raw(), "BEGIN IMMEDIATE", rid=rid)
        self.assertFalse(refused["ok"])
        self.assertIn("has committed before", refused["error"]["text"])
        self.assertEqual(count(self.path), 1)

    def test_a_closed_session_settles_its_id(self):
        sock = self.raw()
        rid = remote.new_request_id()
        self.assertTrue(self.execute(sock, "BEGIN IMMEDIATE", rid=rid)["ok"])
        self.assertTrue(self.execute(sock, "INSERT INTO probe (name) VALUES ('closed')")["ok"])
        asker = self.connect()
        self.assertEqual(asker.outcome(rid), remote.IN_FLIGHT)
        sock.close()
        deadline = time.monotonic() + 10
        while asker.outcome(rid) == remote.IN_FLIGHT:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertEqual(asker.outcome(rid), remote.ABSENT)
        self.assertEqual(count(self.path), 0)

    def test_a_silent_transaction_is_closed_before_a_hub_writer_gives_up(self):
        """Gap (g): silent inside BEGIN IMMEDIATE past the idle timeout, the
        session is rolled back, a hub writer gets the lock inside its wait,
        and the late COMMIT settles as `absent`."""
        self.assertLess(serve.IDLE_TIMEOUT * 1000, database.BUSY_TIMEOUT)
        self.assertGreater(remote.OUTCOME_BOUND, serve.IDLE_TIMEOUT)
        self.hub.stop()
        self.hub = InProcess(self.path, idle_timeout=0.3)
        self.addCleanup(self.hub.stop)
        client = self.connect()
        with self.assertRaises(remote.TransactionLost):
            with transaction(client):
                client.execute("INSERT INTO probe (name) VALUES ('silent')")
                local = sqlite3.connect(self.path, timeout=5, isolation_level=None)
                try:
                    started = time.monotonic()
                    local.execute("BEGIN IMMEDIATE")
                    waited = time.monotonic() - started
                    local.execute("INSERT INTO probe (name) VALUES ('hub')")
                    local.execute("COMMIT")
                finally:
                    local.close()
        self.assertLess(waited, 5)
        self.assertIn("silent 0.3 s inside a transaction; closed and rolled back", self.hub.stream.getvalue())
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        self.assertEqual([row[0] for row in raw.execute("SELECT name FROM probe")], ["hub"])

    def test_executescript_inside_a_write_transaction_is_refused(self):
        client = self.connect()
        client.execute("BEGIN IMMEDIATE")
        client.execute("INSERT INTO probe (name) VALUES ('script')")
        with self.assertRaisesRegex(remote.RemoteError, "executescript would commit the open write"):
            client.executescript("INSERT INTO probe (name) VALUES ('more');")
        self.assertTrue(client.in_transaction)
        client.execute("ROLLBACK")
        self.assertEqual(count(self.path), 0)

    def test_a_local_connection_writes_no_outcome_row(self):
        local = database.open_local(self.path, write=True, create=False)
        self.addCleanup(local.close)
        with transaction(local):
            local.execute("INSERT INTO probe (name) VALUES ('local')")
        self.assertEqual((count(self.path), count(self.path, "request_outcome")), (1, 0))

    def test_a_write_transaction_that_changes_nothing_adds_no_row(self):
        # A replay that writes nothing must write nothing over the wire too:
        # no row, and `total_changes` as a local connection counts it.
        client = self.connect()
        before = client.total_changes
        with transaction(client):
            client.execute("UPDATE probe SET name = 'none' WHERE name = 'missing'")
        self.assertEqual((count(self.path, "request_outcome"), client.total_changes), (0, before))
        with transaction(client):
            client.execute("INSERT INTO probe (name) VALUES ('one')")
        self.assertEqual((count(self.path, "request_outcome"), client.total_changes), (1, before + 1))

    def test_a_write_that_moves_no_row_is_still_recorded(self):
        # DDL and `user_version` change no row; they are writes all the same.
        client = self.connect()
        with transaction(client):
            client.execute("CREATE TABLE extra (x)")
        with transaction(client):
            client.execute("PRAGMA user_version = 99")
        self.assertEqual(count(self.path, "request_outcome"), 2)

    def test_request_ids_are_ulids_that_order_by_time(self):
        early, late = remote.new_request_id(1_700_000_000.0), remote.new_request_id(1_800_000_000.0)
        self.assertTrue(remote.REQUEST_ID.fullmatch(early) and remote.REQUEST_ID.fullmatch(late))
        self.assertLess(early[:10], late[:10])
        self.assertEqual(len({remote.new_request_id(1_700_000_000.0) for _ in range(1000)}), 1000)


class TheReadTransaction(OutcomeCase):
    """Gap C2: a plain BEGIN is a read transaction, with no id and no row."""

    MONTH, NOW = "2026-10", "2026-10-04T12:00:00+00:00"

    def answers(self, connection) -> tuple:
        return (
            usage.read(connection, month=self.MONTH, now=self.NOW),
            reads.usage_month(connection, month=self.MONTH, now=self.NOW),
            registry.merge(registry.parse(SHIPPED), connection).providers,
        )

    def test_the_three_snapshot_helpers_answer_as_a_local_connection(self):
        local = database.open_local(self.path, write=True, create=False)
        self.addCleanup(local.close)
        registry.ensure_seeded(local, registry.parse(SHIPPED))
        local.execute("INSERT INTO cost (call_id, timestamp, provider, bill, role, usd, source) "
                      "VALUES ('c1', '2026-10-02T00:00:00+00:00', 'kimi', 'moonshot', 'reviewer', 0.5, 'bound')")
        expected = self.answers(local)
        for write in (False, True):
            with self.subTest(write=write):
                wire = self.connect(write=write)
                self.assertEqual(self.answers(wire), expected)
                self.assertFalse(wire.in_transaction)
                # The read path leaves the connection as it found it.
                self.assertEqual(wire.execute("PRAGMA query_only").fetchone()[0], 0 if write else 1)
        self.assertEqual(count(self.path, "request_outcome"), 0)

    def test_a_write_inside_a_plain_begin_fails(self):
        wire = self.connect()
        wire.execute("BEGIN")
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            wire.execute("INSERT INTO probe (name) VALUES ('read')")
        wire.execute("COMMIT")
        wire.execute("INSERT INTO probe (name) VALUES ('after')")
        self.assertEqual((count(self.path), count(self.path, "request_outcome")), (1, 0))

    def test_commit_without_the_id_ends_no_write_transaction(self):
        sock = self.raw()
        rid = remote.new_request_id()
        self.assertTrue(self.execute(sock, "BEGIN IMMEDIATE", rid=rid)["ok"])
        self.assertTrue(self.execute(sock, "INSERT INTO probe (name) VALUES ('w')")["ok"])
        for op in ("execute", "commit"):
            with self.subTest(op=op):
                refused = (self.execute(sock, "COMMIT") if op == "execute" else self.ask(sock, "commit"))
                self.assertFalse(refused["ok"])
                self.assertIn(f"COMMIT of write transaction {rid} carries no request id", refused["error"]["text"])
                self.assertTrue(refused["in_transaction"])
        self.assertEqual(count(self.path), 0)
        self.assertTrue(self.execute(sock, "COMMIT", rid=rid)["ok"])
        self.assertEqual(count(self.path), 1)

    def test_commit_with_an_id_ends_no_read_transaction(self):
        sock = self.raw(write=False)
        self.assertTrue(self.execute(sock, "BEGIN")["ok"])
        refused = self.execute(sock, "COMMIT", rid=remote.new_request_id())
        self.assertFalse(refused["ok"])
        self.assertIn("does not own", refused["error"]["text"])
        self.assertTrue(self.execute(sock, "COMMIT")["ok"])
        refused = self.execute(sock, "BEGIN", rid=remote.new_request_id())
        self.assertFalse(refused["ok"])
        self.assertIn("carries no request id", refused["error"]["text"])
        self.assertEqual(count(self.path, "request_outcome"), 0)


CHILD = """
import os, signal, sys, time
from pathlib import Path
from sd_db import serve

point = sys.argv[3]

def fault(where, session):
    if where == point:
        os.kill(os.getpid(), signal.SIGKILL)
        # SIGKILL to this process can land after kill() returns; this thread
        # must not send its answer in between.
        while True:
            time.sleep(1)

serve._fault = fault
raise SystemExit(serve.serve(int(sys.argv[1]), Path(sys.argv[2])))
"""


class Child:
    """A server in a child process, killed with SIGKILL at `serve._fault`."""

    def __init__(self, root: Path, path: Path, port: int, point: str) -> None:
        self.log = root / f"serve-{point}-{time.monotonic_ns()}.log"
        self.stream = open(self.log, "w")
        home = root / "home"
        self.process = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(port), str(path), point],
            stdout=subprocess.DEVNULL, stderr=self.stream, cwd=HERE,
            env={**os.environ, "PYTHONPATH": str(HERE), "HOME": str(home)},
        )
        deadline = time.monotonic() + RESTART_BOUND
        while "serving" not in self.log.read_text():
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise AssertionError(f"serve did not start: {self.log.read_text()}")
            time.sleep(0.05)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(30)
        self.stream.close()


class TheKilledHub(unittest.TestCase):
    """Criterion 11 (b) and (c): `kill -9` on the hub around the commit, a
    restart on the same port, and the client's asks across it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = fresh_database(self.root)
        with socket.create_server(("127.0.0.1", 0)) as probe:
            self.port = probe.getsockname()[1]
        self.token_file = self.path.with_name(self.path.name + serve.TOKEN_SUFFIX)

    def killed_at(self, point: str):
        """A client's write whose hub dies at `point` and comes back.

        Returns the client and what its block raised, or `None`.
        """
        first = Child(self.root, self.path, self.port, point)
        self.addCleanup(first.stop)
        # Read afresh at each session: the restarted hub writes a new token.
        client = remote.connect("127.0.0.1", self.port, self.path,
                                token=lambda: read_token(self.token_file))
        self.addCleanup(client.close)
        restarted: list[Child] = []

        def restart():
            first.process.wait(RESTART_BOUND)
            restarted.append(Child(self.root, self.path, self.port, "none"))

        helper = threading.Thread(target=restart)
        helper.start()
        raised = None
        try:
            with transaction(client):
                client.execute("INSERT INTO probe (name) VALUES ('killed')")
        except remote.RemoteError as error:
            raised = error
        helper.join(RESTART_BOUND)
        for child in restarted:
            self.addCleanup(child.stop)
        self.assertEqual(first.process.returncode, -signal.SIGKILL)
        self.assertTrue(restarted, "the hub did not restart")
        return client, raised

    def test_killed_after_the_commit_the_retry_finds_it_recorded(self):
        """(b) The commit landed and the hub died before it answered: after
        the restart the client's ask reads `recorded`. One row, and its
        `request_outcome` row exists."""
        client, raised = self.killed_at("after-commit")
        self.assertIsNone(raised)
        rid, answer = client.outcomes[-1]
        self.assertEqual(answer, remote.RECORDED)
        self.assertEqual(count(self.path), 1)
        self.assertTrue(recorded(self.path, rid))

    def test_killed_before_the_commit_the_retry_finds_it_absent(self):
        """(c) The hub died before the commit: after the restart the ask reads
        `absent`, `TransactionLost`, zero rows; the verb run again writes one."""
        client, raised = self.killed_at("before-commit")
        self.assertIsInstance(raised, remote.TransactionLost)
        self.assertEqual(client.outcomes[-1], (raised.rid, remote.ABSENT))
        self.assertEqual(count(self.path), 0)
        self.assertFalse(recorded(self.path, raised.rid))
        again = remote.connect("127.0.0.1", self.port, self.path, token=lambda: read_token(self.token_file))
        self.addCleanup(again.close)
        with transaction(again):
            again.execute("INSERT INTO probe (name) VALUES ('again')")
        self.assertEqual(count(self.path), 1)


if __name__ == "__main__":
    unittest.main()
