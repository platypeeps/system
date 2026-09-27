"""Operator requests on owned attempts, usable while the daemon is stopped."""

from __future__ import annotations

from pathlib import Path

from sd_db import runner as store
from sd_db import runner_journal as journal
from sd_db.database import connect

from . import gitops, processes, storage
from .runtime import Runner


def cancel(config, assignment: int, *, expected_revision: str, expected_run=None, who) -> dict:
    connection = connect(config.database)
    try:
        current = store.queue_state(connection, assignment)
        if current["revision"] != expected_revision:
            raise store.RunnerRefused("assignment changed; refresh before cancelling")
        if current["run"] and current["status"] == "running":
            if expected_run != current["run"]["id"]:
                raise store.RunnerRefused("cancel needs the exact current owned run UUID")
            if journal.restore_pending(config.database):
                raise store.RunnerRefused("runner restore reconciliation is incomplete")
            if not (journal.directory(config.database) / f"{expected_run}.json").is_file():
                raise store.RunnerRefused("owned run journal is absent; reconcile recovery before signalling")
            journal.persist(config.database, current["run"])
        state = store.request_cancel(connection, assignment, expected_revision=expected_revision, who=who)
        run = state["run"]
        if state["status"] == "running" and run:
            journal.persist(config.database, run)
            killed = processes.terminate_owned(run)
            state["control"] = {"signalled_owned_group": killed, "cleanup": "runner joins supervisor and retains the clone before releasing its lease"}
        return state
    finally:
        connection.close()


def resume(config, assignment: int, *, expected_revision: str, expected_run=None, who) -> dict:
    connection = connect(config.database)
    try:
        current = store.queue_state(connection, assignment)
        if current["revision"] != expected_revision:
            raise store.RunnerRefused("assignment changed; refresh before resuming")
        run = current["run"]
        if not run or expected_run != run["id"]:
            raise store.RunnerRefused("resume needs the exact current owned run UUID")
        if not run or current["status"] != "ending" or run["end_step"] != "kept":
            raise store.RunnerRefused("resume requires a kept attempt")
        if Runner(config).restore_holds(connection):
            raise store.RunnerRefused("runner recovery has unresolved ownership holds")
        if processes.survivors(run) or gitops.dirty(Path(run["work_path"])):
            raise store.RunnerRefused("resume requires a clean clone with no holders; resolve it at the kept path first")
        store.request_resume(connection, assignment, expected_revision=expected_revision, who=who)
        Runner(config).finish(connection, run["id"])
        return store.queue_state(connection, assignment)
    finally:
        connection.close()


def restore(config, assignment: int, destination: Path, *, expected_revision, expected_run, run=None) -> dict:
    connection = connect(config.database, write=False)
    try:
        current = store.queue_state(connection, assignment)
        if current["revision"] != expected_revision:
            raise store.RunnerRefused("assignment changed; refresh before restoring")
        selected = store.attempt(connection, assignment, run)
        if selected["id"] != expected_run:
            raise store.RunnerRefused("restore needs the exact selected owned run UUID")
        if journal.restore_pending(config.database):
            raise store.RunnerRefused("runner restore reconciliation is incomplete")
        path = storage.restore(selected, destination)
        return {"assignment": assignment, "run": selected["run"], "restored_path": str(path)}
    finally:
        connection.close()


def restore_status(config, assignment: int, destination: Path, *, expected_revision, expected_run, run=None) -> dict:
    from . import restoration
    connection = connect(config.database, write=False)
    try:
        current = store.queue_state(connection, assignment)
        if current["revision"] != expected_revision:
            raise store.RunnerRefused("assignment changed; refresh before observing restore")
        selected = store.attempt(connection, assignment, run)
        if selected["id"] != expected_run:
            raise store.RunnerRefused("restore observation needs the exact selected run UUID")
        return restoration.status(selected, destination)
    finally:
        connection.close()
