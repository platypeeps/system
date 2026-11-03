#!/usr/bin/env python3
"""agent-meter — append one agent-usage reading to a JSONL ledger.

Reads the meters and changes nothing else: `codexbar usage` per provider, and
`rtk gain` for the token-proxy savings. One JSON object per line, appended.

The ledger is `--out`, else AGENT_METER_FILE (exported, or set in
<config>/agent-meter/.env), else <config>/agent-meter/meter.jsonl, where
<config> is ${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}.
Point AGENT_METER_FILE at wherever the consumer of the series reads it.

Cron-safe by design: a meter that fails is recorded as an `errors` entry and
the process still exits 0, because a gap in the series is worse than a noisy
log and a failing collector must not page anyone at 04:00.

Every record carries `prior_gap_s`, the seconds since the reading before it, so
a second collector appending to the same ledger is visible in the data itself.

Since sd:234 the reading is written twice: the JSONL line above, and one
`meter` cost row per provider per window in the sd database, through
`sd_db.sample`, so the later Usage-screen slice (8d) reads the same sample
the JSONL ledger does. The rows go in first, in one transaction on one connection, and
the JSONL line carries what happened: `sd_db.rows` when rows were written,
`errors.sd_db` when the library could not be imported or the database could
not be opened, `errors.sd_db.<provider>.<window>` when one sample was refused.
None of it changes the exit: the JSONL is still appended and the process still
exits 0. `--db PATH` names another database; `--no-db` skips the rows.

`sd_db` is the command pack checkout's `lib/` (sd:3279): `SD_PACK_ROOT`
names the checkout, else the home clone. That folder goes first on `sys.path`,
so no copy installed in the interpreter answers in its place. With no
`lib/sd_db` there, the import fails into `errors.sd_db` and the JSONL half
still lands, which is the point of the try.

Since sd:7322 each reading also carries `accounts`: per Claude account that
`cswap list --json` manages, keyed by its slot number, the seven-day `pct`,
`resetsAt`, `expectedPct`, the `scoped` (per-model) pcts by name, `active`,
`status` and `fetchedAt`. Only those fields are copied: no email, no
organization name, no UUID. A cswap that is missing or fails is
`accounts: null` and `errors.cswap`, with none of its output, since the
human list it may print holds emails. `CSWAP_BIN` names the binary, else
PATH and the BIN_DIRS below. No `meter` rows per account: `sd_db.sample`
takes only providers its registry names (pack change, not this script).

`agent-meter.py accounts` reads the ledger and prints, per account, the
current window's pct against its expected pace and, per past window, the
pct used at reset: the last reading before the reset. Readings come every
4 h, so that is a lower bound, printed with how long before the reset it
was read. Slot numbers follow `cswap swap` and `cswap move`.

  agent-meter.py [--out PATH] [--db PATH | --no-db] [--providers claude,codex] [--timeout S]
  agent-meter.py accounts [--out PATH]
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import system_tools_config  # noqa: E402

sys.path.insert(0, str(Path(os.environ.get("SD_PACK_ROOT") or Path.home() / "repos/platypeeps/sd-ai-command-pack").expanduser() / "lib"))

TOOL = "agent-meter"
# The config directory's own ledger. Created on first use; a ledger named by
# AGENT_METER_FILE or --out is not, because a missing parent there means the
# place it points at moved.
DEFAULT_OUT = str(system_tools_config.config_dir(TOOL) / "meter.jsonl")


def default_out():
    """AGENT_METER_FILE, exported (an empty value too) or from <config>/agent-meter/.env, else DEFAULT_OUT."""
    return system_tools_config.setting("AGENT_METER_FILE", system_tools_config.read_env(TOOL)) or DEFAULT_OUT
PROVIDERS = ["claude", "codex"]
# launchd runs with a minimal PATH that finds neither Homebrew nor ~/.local
BIN_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")]
# Below this many seconds between readings, assume two collectors rather than one
# slow schedule. The cadence is four-hourly, so the floor is generous on purpose:
# it clears a real interval by 3x and still catches a duplicate landing seconds
# after its twin. On 2026-09-03 the LaunchAgent this module replaced was left
# armed beside the job for several hours and nothing in the series showed it.
DUP_GAP_S = 3600
# Where `sd_db.connect` looks when given no path: `$HOME/.local/share/sd/sd.db`,
# read at call time (`default_path` in the pack's `lib/sd_db/database.py`). Named
# here for `--help` only; `--db` left unset hands None to the library, so the
# two cannot drift.
DEFAULT_DB = "~/.local/share/sd/sd.db"
# The two rate windows codexbar reports per provider, each a `meter` row.
WINDOWS = ("primary", "secondary")


def find_bin(name):
    found = shutil.which(name)
    if found:
        return found
    for d in BIN_DIRS:
        cand = os.path.join(d, name)
        # `X_OK`, not `isfile`: a present-but-not-executable file would pass the
        # cheaper test and then fail inside `subprocess.run` with an OSError.
        if os.access(cand, os.X_OK):
            return cand
    return None


def prior_ts(path):
    """UTC timestamp of the last readable reading in path, or None if none parses.

    Never raises. A collector that cannot read its own ledger still has a
    reading to write, and the gap is an annotation on that reading rather than
    the reason for it. Reads a bounded tail, not the file: the ledger only grows.

    A bad line is skipped, not fatal, and that distinction is the whole point of
    the field. The last line of an append-only ledger is the one a truncated
    write leaves half-finished, and the 64K seek lands mid-line besides -- so
    giving up on the first line that will not parse would drop the duplicate
    signal exactly when the file is already saying something went wrong.
    """
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 65536))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return None
    for line in reversed(tail):
        if not line.strip():
            continue
        try:
            return datetime.datetime.strptime(
                json.loads(line)["ts"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
        except Exception:
            continue
    return None


def run_json(cmd, timeout, echo=True):
    """Run cmd, parse stdout as JSON. Returns (data, error) — exactly one set.

    `echo=False` keeps the command's output out of the error, for a command
    whose output may hold personal values (cswap's human list has emails)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout after %ds" % timeout
    except OSError as e:
        return None, str(e) if echo else type(e).__name__
    if r.returncode != 0:
        if not echo:
            return None, "exit %d" % r.returncode
        return None, "exit %d: %s" % (r.returncode, (r.stderr or r.stdout).strip()[:500])
    try:
        return json.loads(r.stdout), None
    except ValueError:
        if not echo:
            return None, "unparseable output"
        return None, "unparseable output: %s" % r.stdout.strip()[:500]


def account_entry(account):
    """The fields of one cswap account the ledger keeps, and no other.

    A whitelist on purpose: cswap's entry also holds the email, the
    organization name and its UUID, and none of them may reach the ledger.
    A missing or non-integer number raises, failing the whole read."""
    number = account["number"]
    if not isinstance(number, int) or isinstance(number, bool):
        raise TypeError("account number is not an integer")
    usage = account.get("usage") or {}
    week = usage.get("sevenDay") or {}
    return {"number": number, "active": bool(account.get("active")),
            "status": account.get("usageStatus"),
            "pct": week.get("pct"), "resetsAt": week.get("resetsAt"),
            "expectedPct": week.get("expectedPct"),
            "scoped": {s["name"]: s.get("pct") for s in usage.get("scoped") or []
                       if isinstance(s, dict) and isinstance(s.get("name"), str)},
            "fetchedAt": account.get("usageFetchedAt")}


def read_accounts(timeout):
    """`cswap list --json` as account entries. Returns (accounts, error).

    A failure is (None, reason): unknown, not an empty list. The reason
    never carries cswap's output (see `run_json`'s `echo`)."""
    cswap = os.environ.get("CSWAP_BIN") or find_bin("cswap")
    if not cswap or not os.access(cswap, os.X_OK):
        return None, "cswap binary not found" + (" at %s" % cswap if cswap else "")
    data, err = run_json([cswap, "list", "--json"], timeout, echo=False)
    if err is not None:
        return None, err
    try:
        return [account_entry(account) for account in data["accounts"]], None
    except (TypeError, KeyError, AttributeError) as e:
        # The exception's class only: a message can quote a value.
        return None, "unexpected cswap shape: %s" % type(e).__name__


def write_rows(record, a) -> None:
    """One `meter` cost row per provider per window, or the reason there is none.

    Every failure is an `errors` entry on the record and never an exception:
    the import (the pack's `lib/` holds `sd_db`; a pack without it does not),
    the open (no database, a schema this library does not match), a refusal
    per window (a provider the registry does not name, a percentage codexbar
    reported outside 0..100: the library's `MeterRefused` and only that), or
    anything else on the way to the commit (no registry beside the store,
    an SQLite error), which is the run's and not one window's. The JSONL line that follows
    records all of it, and the exit stays 0 for the reason at the top.

    One connection and one transaction for the run: the windows of one reading
    land together or, when the commit fails, not at all -- `sd_db.transaction`
    nests each sample as a savepoint, so one refused window costs that window
    and nothing else. The rows carry the reading's own `ts`, so the row and
    the JSONL line of one sample agree to the second.
    """
    if a.no_db:
        record["sd_db"] = {"rows": 0, "skipped": "--no-db"}
        return
    try:
        import sd_db
        # Named import, so an installed build older than this script fails
        # here as one `errors.sd_db` entry and not once per window below.
        from sd_db.meter import MeterRefused, sample
        connection = sd_db.connect(a.db, write=True)
    except Exception as e:
        record["errors"]["sd_db"] = "%s: %s" % (type(e).__name__, e)
        record["sd_db"] = {"rows": 0}
        return
    rows = 0
    try:
        with sd_db.transaction(connection):
            for prov, entry in record["codexbar"].items():
                usage = (entry.get("usage") if isinstance(entry, dict) else None) or {}
                for window in WINDOWS:
                    reading = usage.get(window) or {}
                    minutes, percent = reading.get("windowMinutes"), reading.get("usedPercent")
                    if minutes is None or percent is None:
                        continue
                    try:
                        sample(connection, provider=prov, window_minutes=minutes,
                               used_percent=percent, now=record["ts"])
                        rows += 1
                    except MeterRefused as e:
                        # The library's own refusal is this window's and no
                        # other's. Anything else (no registry beside the
                        # store, an SQLite error) is the run's, and reaches
                        # the handler below as one `errors.sd_db`.
                        record["errors"]["sd_db.%s.%s" % (prov, window)] = str(e)
    except Exception as e:
        # The transaction rolled back, so the count is the truth: nothing landed.
        record["errors"]["sd_db"] = "%s: %s" % (type(e).__name__, e)
        rows = 0
    finally:
        connection.close()
    record["sd_db"] = {"rows": rows}


def collect(a) -> None:
    record = {"ts": datetime.datetime.now(datetime.timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%M:%SZ"),
              "codexbar": {}, "accounts": None, "rtk": None, "errors": {}}
    brief = []

    codexbar = find_bin("codexbar")
    providers = [p.strip() for p in a.providers.split(",") if p.strip()]
    for prov in providers:
        if not codexbar:
            record["errors"]["codexbar"] = "codexbar binary not found"
            break
        data, err = run_json(
            [codexbar, "usage", "--format", "json", "--provider", prov], a.timeout)
        if err is None and isinstance(data, list) and data:
            entry = data[0]
            record["codexbar"][prov] = entry
            usage = entry.get("usage") or {}
            pri = (usage.get("primary") or {}).get("usedPercent")
            sec = (usage.get("secondary") or {}).get("usedPercent")
            err = (entry.get("error") or {}).get("message")
            brief.append("%s %s%%/%s%%" % (prov, pri, sec) if err is None
                         else "%s ERR" % prov)
            if err is not None:
                record["errors"]["codexbar." + prov] = err
        else:
            record["errors"]["codexbar." + prov] = err or "empty response"
            brief.append("%s ERR" % prov)

    accounts, err = read_accounts(a.timeout)
    record["accounts"] = accounts
    if err is None:
        brief.append("cswap %d accounts" % len(accounts))
    else:
        record["errors"]["cswap"] = err
        brief.append("cswap ERR")

    rtk = find_bin("rtk")
    if rtk:
        data, err = run_json([rtk, "gain", "--format", "json"], a.timeout)
        if err is None:
            record["rtk"] = data
            s = data.get("summary") or {}
            brief.append("rtk saved %.1fM (%.1f%%)"
                         % (s.get("total_saved", 0) / 1e6,
                            s.get("avg_savings_pct", 0.0)))
        else:
            record["errors"]["rtk"] = err
            brief.append("rtk ERR")
    else:
        record["errors"]["rtk"] = "rtk binary not found"

    out = os.path.expanduser(a.out)
    parent = os.path.dirname(out)
    if out == DEFAULT_OUT:
        os.makedirs(parent, exist_ok=True)
    # A ledger named elsewhere may live in another checkout, which may simply
    # not be there. Say so instead of creating a lookalike tree that nothing
    # will ever read.
    if not os.path.isdir(parent):
        print("agent-meter: %s does not exist; did the ledger's location move? "
              "Set AGENT_METER_FILE, or copy local-agent-meter/.env.example to %s "
              "and fill it in." % (parent, system_tools_config.config_dir(TOOL) / ".env"),
              file=sys.stderr)
        sys.exit(1)
    # The rows first, so a JSONL line never says less than the database holds.
    write_rows(record, a)
    prev = prior_ts(out)
    gap = None if prev is None else int(
        (datetime.datetime.strptime(record["ts"], "%Y-%m-%dT%H:%M:%SZ")
         .replace(tzinfo=datetime.timezone.utc) - prev).total_seconds())
    record["prior_gap_s"] = gap
    if gap is not None and gap < DUP_GAP_S:
        print("agent-meter: WARNING previous reading %ds ago, under the %ds floor -- "
              "a second collector may be appending here. Check "
              "`cron-jobs.sh verify agent-meter` and ~/Library/LaunchAgents for a "
              "stray plist." % (gap, DUP_GAP_S), file=sys.stderr)
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
    # The cron log's one line says what the JSONL line says: no rows and why
    # is ERR; some rows with a refused window among them names the count.
    refused = sum(1 for key in record["errors"] if key.startswith("sd_db."))
    brief.append("sd_db %s" % (
        "ERR" if "sd_db" in record["errors"]
        else "%d rows" % record["sd_db"]["rows"] + (", %d refused" % refused if refused else "")))
    print("agent-meter: %s -> %s" % ("  ".join(brief) or "nothing readable", out))
    # a partially failed reading is still a reading; exit 0 so cron stays quiet


def parse_time(text):
    """An ISO time, `Z` or offset, fractional seconds or none, as UTC."""
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
        datetime.timezone.utc)


def window_key(reset):
    """resetsAt to the nearest minute: cswap's value jitters by fractions
    of a second between readings of one window."""
    return (reset + datetime.timedelta(seconds=30)).replace(second=0, microsecond=0)


def local(moment):
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def report_accounts(a) -> None:
    """Per account: the current window's pace, then each past window's
    pct at reset (the last reading before it), newest first."""
    out = os.path.expanduser(a.out)
    try:
        with open(out, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        print("agent-meter: cannot read the ledger %s: %s" % (out, e.strerror or e),
              file=sys.stderr)
        sys.exit(1)
    windows = {}   # number -> {reset: (read_at, pct, expectedPct)}
    active = {}    # number -> (read_at, active) of its latest reading
    for line in lines:
        try:
            record = json.loads(line)
            stamp = record["ts"]
            accounts = record.get("accounts") or []
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        for account in accounts:
            try:
                number, pct, reset = account["number"], account.get("pct"), account.get("resetsAt")
                read_at = parse_time(account.get("fetchedAt") or stamp)
                if pct is None or not reset:
                    continue
                key = window_key(parse_time(reset))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            seen = windows.setdefault(number, {}).get(key)
            if seen is None or read_at >= seen[0]:
                windows[number][key] = (read_at, pct, account.get("expectedPct"))
            if number not in active or read_at >= active[number][0]:
                active[number] = (read_at, bool(account.get("active")))
    if not windows:
        print("agent-meter: no account readings in %s" % out)
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    print("agent-meter accounts: %s" % out)
    print("Readings come every 4 h: \"used\" is the last reading before the reset,")
    print("a lower bound; use after that reading is not in the ledger.")
    for number in sorted(windows):
        print()
        print("account %s%s" % (number, " (active)" if active[number][1] else ""))
        for reset in sorted(windows[number], reverse=True):
            read_at, pct, expected = windows[number][reset]
            if reset > now:
                print("  now  resets %s  %s%% used, %s%% expected (read %s)"
                      % (local(reset), fmt_pct(pct), fmt_pct(expected), local(read_at)))
            else:
                # A reading fetched after its own reset is the API's stale
                # answer; say so rather than print a negative gap.
                gap = (reset - read_at).total_seconds() / 3600
                print("  reset %s  used >= %s%% (last read %.1f h %s)"
                      % (local(reset), fmt_pct(pct), abs(gap),
                         "before" if gap >= 0 else "after"))


def fmt_pct(value):
    return "?" if value is None else "%.0f" % value


def main() -> None:
    p = argparse.ArgumentParser(prog="agent-meter.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", choices=["accounts"],
                   help="accounts: per cswap account, the pct used at each "
                        "past 7-day reset and the current pace; omit to "
                        "append one reading")
    p.add_argument("--out", default=default_out(),
                   help="JSONL ledger to append to (default: %(default)s)")
    p.add_argument("--providers", default=",".join(PROVIDERS),
                   help="comma-separated codexbar providers (default: %(default)s)")
    p.add_argument("--timeout", type=int, default=120,
                   help="seconds per meter command (default 120)")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--db", default=None, metavar="PATH",
                       help="sd database to write the meter rows to "
                            "(default: %s, sd_db's own default)" % DEFAULT_DB)
    where.add_argument("--no-db", action="store_true",
                       help="write the JSONL line only, no meter rows")
    a = p.parse_args()
    if a.command == "accounts":
        report_accounts(a)
    else:
        collect(a)


if __name__ == "__main__":
    main()
