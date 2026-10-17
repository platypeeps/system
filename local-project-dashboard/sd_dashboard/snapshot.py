"""Slow reads the server keeps and refreshes in the background (sd:TBD): the fleet's Repos and Sessions.

`fleet.collect` runs a child that reads git in every checkout, 1 to 1.5 s on the hub, and Today, Management and
Health each waited on it per request. `Fleet` is that seam, `area -> document`, answered from one `Snapshot` per area.

A `Snapshot` answers its last document at once, with `observed`, the UTC time the read that made it began, so a page
says what it saw and when (principle 11). It waits only when it has no document, when the document is older than
`KEPT_SECONDS`, or after `invalidate`, which the server calls on every write it answers; then it waits for a read
begun after that point. A document older than `FRESH_SECONDS` starts one refresh in a thread, and the request does not
wait on it. One read runs at a time: a request that must wait joins the running read, or the next one when the running
one began before the write. A failed read keeps the last document and answers it with `stale`, the failure's text,
never an empty or healthy one; with no document the failure is the answer, as `fleet.collect` raised it. A failure
newer than `FRESH_SECONDS` starts no other read, so a failing collector runs at most once per `FRESH_SECONDS`.
`fleet.collect` bounds each read at `fleet.FLEET_SECONDS`. Nothing is kept on disk: a restarted server reads again.
"""

from __future__ import annotations

import threading
import time

from . import fleet as fleet_module

__all__ = ["FRESH_SECONDS", "Fleet", "KEPT_SECONDS", "Snapshot"]

#: A document this old starts a refresh in the background.
FRESH_SECONDS = 45.0
#: A document older than this is not answered while a read could replace it: the request waits for one.
KEPT_SECONDS = 300.0


class Snapshot:
    """One slow read, `compute()`, kept and refreshed as the module says. `clock` and `wall` are test seams."""

    def __init__(self, compute, *, clock=time.monotonic, wall=time.time):
        self._compute, self._clock, self._wall = compute, clock, wall
        self._condition = threading.Condition()
        self._value = None
        self._observed = None
        self._began = None  # `clock` when the read that made `_value` began
        self._failure = None  # the newest read's failure, cleared by a read that succeeds
        self._tried = None  # `clock` when the newest read ended
        self._started = self._done = 0  # reads begun and ended, numbered
        self._made = 0  # the number of the read that made `_value`
        self._before = 0  # reads numbered this or lower began before the newest write
        self._running = False

    def read(self):
        """`(document, observed, stale)`: the document, the UTC time its read began, and why the newest read failed."""
        with self._condition:
            now = self._clock()
            if self._value is not None and self._made > self._before and now - self._began <= KEPT_SECONDS:
                if now - self._began > FRESH_SECONDS and not self._running and not self._recently_failed(now):
                    self._start()
                return self._answer()
            if self._value is not None and self._failure is not None and self._recently_failed(now) \
                    and self._done > self._before:
                return self._answer()  # its replacement just failed: answer it stale, not after another wait
            # The read to wait for: the running one, unless it began before the newest write; else the next one.
            target = max(self._before + 1, self._started if self._running else self._started + 1)
            while self._done < target:
                if not self._running:
                    self._start()  # it may end the read at once, when no thread starts
                else:
                    self._condition.wait()
            return self._answer()

    def invalidate(self):
        """A write landed: no document read before now is answered until a read begun after it ends."""
        with self._condition:
            self._before = self._started

    def idle(self, seconds):
        """Wait until no read runs, up to `seconds`; whether none does. For tests and the measurement."""
        with self._condition:
            return self._condition.wait_for(lambda: not self._running, seconds)

    def _recently_failed(self, now):
        return self._failure is not None and self._tried is not None and now - self._tried <= FRESH_SECONDS

    def _answer(self):
        if self._value is None:
            raise self._failure
        return self._value, self._observed, (str(self._failure) or type(self._failure).__name__) if self._failure else None

    def _start(self):
        self._started += 1
        self._running = True
        try:
            threading.Thread(target=self._refresh, args=(self._started,), name="sd-dashboard-snapshot", daemon=True).start()
        except RuntimeError as error:  # no thread: that read failed, so no request waits on it
            self._end(self._started, None, None, None, error)

    def _refresh(self, number):
        began, observed = self._clock(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._wall()))
        try:
            value, failure = self._compute(), None
        except BaseException as error:  # noqa: BLE001 - any failure, an exit too, ends the read and keeps the document
            value, failure = None, error
        with self._condition:
            self._end(number, value, observed, began, failure)

    def _end(self, number, value, observed, began, failure):
        """Under the condition: read `number` ended with `value` or `failure`; wake every request waiting on it."""
        if failure is None:
            self._value, self._observed, self._began, self._made = value, observed, began, number
        self._failure, self._tried, self._done, self._running = failure, self._clock(), number, False
        self._condition.notify_all()


class Fleet:
    """`fleet.collect`'s seam, `area -> document`, from one `Snapshot` per area of `fleet.AREAS`.

    The document gains `observed`, and `stale` when the newest read failed. A copy each time: a screen may not change
    the kept one.
    """

    def __init__(self, collect=fleet_module.collect, **seams):
        self._areas = {area: Snapshot(lambda area=area: collect(area), **seams) for area in fleet_module.AREAS}

    def __call__(self, area):
        if area not in self._areas:
            raise ValueError("unknown fleet area")
        document, observed, stale = self._areas[area].read()
        return {**document, "observed": observed, **({"stale": stale} if stale else {})}

    def invalidate(self):
        for kept in self._areas.values():
            kept.invalidate()

    def idle(self, seconds):
        return all(kept.idle(seconds) for kept in self._areas.values())
