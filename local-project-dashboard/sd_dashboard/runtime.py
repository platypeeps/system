"""Explicit, reversible installation of the database dashboard and private Serve route."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

#: The launchd label prefix is shared by every system service, so a
#: second install under another prefix keeps its own labels.
LABEL_PREFIX = os.environ.get("SYSTEM_TOOLS_LABEL_PREFIX", "local.system-tools")
LABEL = f"{LABEL_PREFIX}.sd-dashboard"
LAUNCH_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
HERE = Path(__file__).resolve().parents[1]


class RuntimeRefused(Exception):
    pass


def _run(arguments, *, check=True, timeout=20):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeRefused(f"cannot run {arguments[0]}: {error}") from error
    if check and result.returncode:
        raise RuntimeRefused(f"{arguments[0]} failed: {(result.stderr or result.stdout).strip()[:1000]}")
    return result


def _serve_status():
    try:
        value = json.loads(_run(["tailscale", "serve", "status", "--json"]).stdout)
    except ValueError as error:
        raise RuntimeRefused("tailscale serve status did not return JSON") from error
    if not isinstance(value, dict):
        raise RuntimeRefused("tailscale serve status is not an object")
    return value


def read_config(path):
    path = Path(path).expanduser().resolve()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeRefused(f"cannot read dashboard config {path}: {error}") from error
    if not isinstance(config, dict):
        raise RuntimeRefused("dashboard configuration must be a JSON object")
    unknown = set(config) - {"origin", "operator_login", "ip_origin", "port", "database"}
    if unknown:
        raise RuntimeRefused("unknown dashboard configuration: " + ", ".join(sorted(unknown)))
    port = config.get("port", 8767)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise RuntimeRefused("dashboard port must be an integer from 1024 to 65535")
    config["port"] = port
    from .auth import parse_direct_origin
    parse_direct_origin(config)
    database = config.get("database")
    if database is not None and (not isinstance(database, str) or not Path(database).expanduser().is_absolute()):
        raise RuntimeRefused("configured database must be an absolute path")
    return config


def _check_node(config):
    from .auth import parse_direct_origin

    try:
        if not isinstance(config.get("origin"), str) or not isinstance(config.get("operator_login"), str):
            raise ValueError("origin and operator_login must be strings")
        requested_host = urlsplit(config["origin"]).hostname
        state = json.loads(_run(["tailscale", "status", "--json"]).stdout)
        own = state["Self"]
        hostname = own["DNSName"].rstrip(".")
        user = state["User"][str(own["UserID"])]
        if not isinstance(user, dict):
            raise ValueError("Tailscale owner is unavailable")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise RuntimeRefused("cannot identify this Tailscale node and its operator") from error
    if state.get("BackendState") != "Running":
        raise RuntimeRefused("Tailscale is not running")
    if requested_host != hostname:
        raise RuntimeRefused("configured dashboard hostname is not this Tailscale node")
    if config.get("operator_login") != user.get("LoginName"):
        raise RuntimeRefused("configured operator_login is not this node's Tailscale owner")
    direct = parse_direct_origin(config)
    if direct and direct.address not in own.get("TailscaleIPs", []):
        raise RuntimeRefused("configured dashboard IP is not this Tailscale node")


def _check_direct_serve(config, status):
    """A direct listener must not overlap a Serve or Funnel listener."""
    from .auth import parse_direct_origin

    direct = parse_direct_origin(config)
    if direct is None:
        return
    for key in ("TCP", "Web", "AllowFunnel"):
        section = status.get(key, {})
        if not isinstance(section, dict):
            raise RuntimeRefused("invalid Tailscale Serve configuration")
        if ((key == "TCP" and str(direct.port) in section) or
                (key != "TCP" and any(str(name).endswith(f":{direct.port}") for name in section))):
            raise RuntimeRefused("direct dashboard IP port is already configured in Tailscale Serve")


def peer_login(peer):
    """Identify the TCP peer through the local Tailscale daemon, never headers.

    The rules are `sd_db.tailnet`'s, shared with `sd-db.sh serve`: a tagged,
    expired or mismatched node has no login."""
    from sd_db import tailnet

    try:
        return tailnet.login(peer, lambda arguments: _run(arguments).stdout)
    except tailnet.TailnetError as error:
        raise RuntimeRefused("cannot identify the dashboard's Tailscale peer") from error


def load_frontdoor(config_path: Path, port: int):
    from .auth import validate_frontdoor

    config = read_config(config_path)
    if config["port"] != port:
        raise RuntimeRefused("configured backend port differs from --port; use the configured port explicitly")
    if not config.get("origin") and not config.get("operator_login"):
        return None
    _check_node(config)
    status = _serve_status()
    _check_direct_serve(config, status)
    return validate_frontdoor(config, status, port)


def _file_digest(path):
    if path.is_symlink() or not path.is_file():
        raise RuntimeRefused(f"runtime build requires a regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_manifest(root):
    """Enumerate package data as well as code; generated bytecode is excluded."""
    if root.is_symlink() or not root.is_dir():
        raise RuntimeRefused(f"runtime build directory is unavailable: {root}")
    entries = []
    def unreadable(error):
        raise error
    for directory, names, files in os.walk(root, onerror=unreadable, followlinks=False):
        names[:] = sorted(name for name in names if name != "__pycache__")
        for name in names:
            if (Path(directory) / name).is_symlink():
                raise RuntimeRefused(f"runtime build contains a linked directory: {Path(directory) / name}")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(directory) / name
            entries.append((path.relative_to(root).as_posix(), _file_digest(path)))
    if not entries:
        raise RuntimeRefused(f"runtime build directory is empty: {root}")
    return sorted(entries)


def build_digests(*, library_root=None, dashboard_root=None):
    """Hash installed package bytes and dashboard code with fixed collectors.

    Optional roots are Python-only test seams, never CLI or HTTP parameters.
    Names are part of each digest, so file additions and removals count too.
    """
    import sd_db

    library_root = Path(library_root) if library_root is not None else Path(sd_db.__file__).resolve().parent
    dashboard_root = Path(dashboard_root) if dashboard_root is not None else HERE
    try:
        library = _build_manifest(library_root)
        dashboard = [("sd_dashboard/" + name, digest)
                     for name, digest in _build_manifest(dashboard_root / "sd_dashboard")]
        # Ports uses these fixed repository dependencies outside the package.
        # Their absence is valid in isolated fixtures, but adding or removing
        # either must still invalidate the previously captured build identity.
        #
        # `dashboard.sh` joined them when this package became installable. It
        # sits beside the package in a checkout and nowhere at all in
        # site-packages, so requiring it meant `server.build` raised
        # RuntimeRefused from an installed copy -- the dashboard could be
        # installed but not started. Absent is now recorded as None, which
        # still distinguishes a launcher that was removed from one that never
        # shipped.
        for name in ("dashboard.sh", "collectors.py", "sd_tile.py",
                     "../local-machine-setup/machine-setup.sh"):
            path = dashboard_root / name
            dashboard.append((name, _file_digest(path) if path.exists() or path.is_symlink() else None))
    except OSError as error:
        raise RuntimeRefused(f"cannot fingerprint runtime build: {error}") from error
    return {name: hashlib.sha256(json.dumps(sorted(entries), separators=(",", ":")).encode()).hexdigest()
            for name, entries in (("library_digest", library), ("dashboard_digest", dashboard))}


def _installed_library_commit():
    """The commit pip provisioned sd_db from, or None for a path install.

    A VCS install records `vcs_info.commit_id` in direct_url.json; a path
    install (`pip install ./local-sd-db`, as both CIs do) records `dir_info`
    and no commit, so there is nothing to compare and the caller skips.
    """
    import importlib.metadata

    try:
        text = importlib.metadata.distribution("sd-db").read_text("direct_url.json")
        record = json.loads(text) if text else None
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
        return None
    vcs_info = record.get("vcs_info") if isinstance(record, dict) else None
    commit = vcs_info.get("commit_id") if isinstance(vcs_info, dict) else None
    return commit if isinstance(commit, str) and commit else None


def _checkout_library_commit(checkout):
    """The last commit that touched the library source in this checkout, or None.

    None when git is absent or the checkout is not a git repository: the
    dashboard can run from a copy that is neither, and then nothing can say
    what the pages expect.
    """
    if shutil.which("git") is None:
        return None
    result = _run(["git", "-C", str(checkout), "log", "-1", "--format=%H", "--", "local-sd-db/sd_db"], check=False)
    if result.returncode:
        if "not a git repository" in result.stderr:
            return None
        raise RuntimeRefused(f"git failed: {(result.stderr or result.stdout).strip()[:1000]}")
    return result.stdout.strip() or None


def _library_lag(checkout):
    """Refuse an installed sd_db that lacks the checkout's last library commit.

    `dashboard.sh serve` runs the checkout's pages under -I against the pack's
    installed build, and #395 shipped a page calling a library name the
    installed copy did not have; the commit message asked for a deploy first,
    and nothing enforced it. Both sides already record a commit, so compare
    them here, beside the schema check. Returns the installed commit, or None
    when either side cannot be read (a path install, no git, no repository).
    """
    installed = _installed_library_commit()
    if installed is None:
        return None
    required = _checkout_library_commit(checkout)
    if required is None:
        return None
    # rc 1 is "not an ancestor": the installed commit is older than the
    # library's last change, or sits on a branch that never took it. Either
    # way the pages may call a name the build lacks, and the remedy is the same.
    result = _run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", required, installed], check=False)
    if result.returncode == 1:
        raise RuntimeRefused(f"installed sd_db {installed} lacks the checkout's library commit {required}; run make setup in the pack, then start the dashboard")
    if result.returncode:
        raise RuntimeRefused(f"installed sd_db {installed} is not a commit of this checkout ({(result.stderr or result.stdout).strip()[:200]}); run make setup in the pack, then start the dashboard")
    return installed


def installed_library(database):
    """Prove the runtime uses an installed build, one no older than the checkout, and can read this database."""
    import sd_db
    import sd_db.writing  # every production dashboard must support the writing stage
    from sd_db.schema import SCHEMA_VERSION

    library = Path(sd_db.__file__).resolve()
    if not library.is_relative_to(Path(sys.prefix).resolve()) or "site-packages" not in library.parts:
        raise RuntimeRefused(f"sd_db must be an installed build in this interpreter, not {library}")
    try:
        connection = sd_db.connect(database, write=False)
        try:
            version = sd_db.database.schema_version(connection)
        finally:
            connection.close()
    except (OSError, sd_db.SdDbError) as error:
        raise RuntimeRefused(str(error)) from error
    if version != SCHEMA_VERSION:
        raise RuntimeRefused(f"database schema {version} differs from installed schema {SCHEMA_VERSION}; migrate explicitly with services stopped")
    library_commit = _library_lag(HERE.parent)
    return {"python": sys.executable, "library": str(library), "schema": version, "library_commit": library_commit, **build_digests()}


def _configured_route(config, status):
    """Return whether this exact route exists; never overwrite a different service."""
    from .auth import validate_frontdoor

    _check_direct_serve(config, status)

    origin = config.get("origin")
    if not isinstance(origin, str):
        raise RuntimeRefused("installation requires an explicit private HTTPS origin and operator_login")
    try:
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or parsed.port != 8443 or not parsed.hostname or not parsed.hostname.endswith(".ts.net"):
            raise RuntimeRefused("installation uses a private *.ts.net HTTPS origin on :8443 only")
    except ValueError as error:
        raise RuntimeRefused("invalid dashboard origin") from error
    authority = parsed.netloc
    if (status.get("AllowFunnel") or {}).get(authority):
        raise RuntimeRefused("the dashboard origin is public through Funnel; refusing installation")
    present = authority in (status.get("Web") or {}) or "8443" in (status.get("TCP") or {})
    expected = json.loads(json.dumps(status))
    if not present:
        expected.setdefault("TCP", {})["8443"] = {"HTTPS": True}
        expected.setdefault("Web", {})[authority] = {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{config['port']}"}}}
    validate_frontdoor(config, expected, config["port"])
    return present


def _without_dashboard(status, authority):
    result = json.loads(json.dumps(status))
    for key, name in (("TCP", "8443"), ("Web", authority), ("AllowFunnel", authority)):
        mapping = result.get(key)
        if isinstance(mapping, dict):
            mapping.pop(name, None)
            if not mapping:
                result.pop(key, None)
    return result


def _dashboard_scope(status, authority):
    return {key: (status.get(key) or {}).get(name)
            for key, name in (("TCP", "8443"), ("Web", authority), ("AllowFunnel", authority))}


def _rollback_route(config, before, owned_route):
    """Reconcile an attempted creation, including an unknown CLI outcome."""
    authority = urlsplit(config["origin"]).netloc
    current = _serve_status()
    scope = _dashboard_scope(current, authority)
    original = _dashboard_scope(before, authority)
    if scope == original:
        return
    expected_web = {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{config['port']}"}}}
    if (scope["TCP"] != {"HTTPS": True} or scope["Web"] != expected_web or
            scope["AllowFunnel"] not in (None, False) or
            (owned_route is not None and scope != owned_route)):
        raise RuntimeRefused("dashboard Serve route changed concurrently; preserving it for manual recovery")
    untouched = _without_dashboard(current, authority)
    command_error = None
    try:
        # Serve requires the original flags when disabling a background mount.
        # Naming / also avoids a port-wide removal of another mounted path.
        _run(["tailscale", "serve", "--bg", "--https=8443", "--set-path=/", "off"])
    except RuntimeRefused as error:
        command_error = error
    after = _serve_status()
    if _dashboard_scope(after, authority) != original:
        raise RuntimeRefused("could not verify removal of the attempted dashboard route" +
                             (f": {command_error}" if command_error else ""))
    if _without_dashboard(after, authority) != untouched:
        raise RuntimeRefused("unrelated Serve configuration changed during rollback; review the saved snapshot")


def _plist(config_path, config, home):
    arguments = [str(HERE / "dashboard.sh"), "serve", "--config", str(config_path),
                 "--port", str(config["port"])]
    if config.get("database"):
        arguments += ["--database", str(Path(config["database"]).expanduser().resolve())]
    # Standard, not Background: this is an interactive HTTP server. Background
    # confines it to efficiency cores and low-priority I/O, which made a Today
    # render 4-5x slower (sd:1433). The runner's plist stays Background.
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": arguments, "RunAtLoad": True,
        "KeepAlive": True, "ThrottleInterval": 30, "ProcessType": "Standard",
        "StandardOutPath": str(home / "Library/Logs" / f"{LABEL}.log"),
        "StandardErrorPath": str(home / "Library/Logs" / f"{LABEL}.err"),
        "EnvironmentVariables": _launch_environment(),
    }, sort_keys=False)


#: Carried into the LaunchAgent when the installing shell sets them, so the
#: server reads the same vault, checkout root, labels, config directory and
#: extra job directories as `dashboard.sh`. `OBSIDIAN_VAULT` is the vault
#: `sd store` writes Notes' quick notes into (sd:2549).
PASSED_THROUGH = ("VAULT", "REPO_ROOT", "SYSTEM_TOOLS_LABEL_PREFIX", "SYSTEM_TOOLS_CONFIG",
                  "CRON_JOBS_EXTRA_DIRS", "OBSIDIAN_VAULT")


def _launch_environment():
    environment = {"PATH": LAUNCH_PATH, "SD_DASHBOARD_PYTHON": sys.executable}
    # Notes runs `sd store` (sd:2549): the installing shell's `sd` directory joins the agent's PATH.
    sd = shutil.which("sd")
    if sd and str(Path(sd).parent) not in LAUNCH_PATH.split(":"):
        environment["PATH"] = f"{LAUNCH_PATH}:{Path(sd).parent}"
    for name in PASSED_THROUGH:
        if os.environ.get(name):
            environment[name] = os.environ[name]
    return environment


def launch_gaps(environment):
    """What Notes' quick notes need from the agent's environment and the installing shell did not give."""
    return ([] if shutil.which("sd", path=environment["PATH"]) else ["sd is not on the installing shell's PATH"]) + (
        [] if environment.get("OBSIDIAN_VAULT") else ["OBSIDIAN_VAULT is not set"])


def preflight(config_path, *, home=None):
    home = Path(home) if home is not None else Path.home()
    config_path = Path(config_path).expanduser().resolve()
    config = read_config(config_path)
    database = Path(config.get("database") or home / ".local/share/sd/sd.db").expanduser().resolve()
    library = installed_library(database)
    remote = bool(config.get("origin") or config.get("operator_login"))
    if remote:
        _check_node(config)
    status = _serve_status() if remote else None
    present = _configured_route(config, status) if remote else False
    target = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    old = target.read_bytes() if target.exists() else None
    domain = f"gui/{os.getuid()}"
    loaded = _run(["launchctl", "print", f"{domain}/{LABEL}"], check=False).returncode == 0
    if loaded and old is None:
        raise RuntimeRefused("dashboard is loaded without its plist; restore its original plist before replacement")
    report = {"config_path": str(config_path), "config": config, "runtime": library,
              "database": str(database), "remote": remote, "serve_status": status, "serve_route_present": present,
              "plist": str(target), "old_plist_sha256": hashlib.sha256(old).hexdigest() if old else None,
              "loaded": loaded, "program": plistlib.loads(_plist(config_path, config, home))["ProgramArguments"]}
    report["fingerprint"] = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    report["planned_actions"] = (["add private HTTPS :8443 proxy"] if remote and not present else []) + [
        "back up existing LaunchAgent and Serve snapshot", "replace dashboard LaunchAgent",
        "verify backend health and unchanged unrelated Serve configuration"]
    if config.get("ip_origin"):
        report["planned_actions"].insert(0, "bind authenticated dashboard IP listener at " + config["ip_origin"])
    return report


def health_check(port, *, attempts=20):
    for attempt in range(attempts):
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            client.request("GET", "/health")
            response = client.getresponse()
            body = json.loads(response.read())
            if response.status == 200 and body.get("service") == "sd-dashboard" and body.get("ok") is True:
                return body
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            client.close()
        if attempt + 1 < attempts:
            time.sleep(0.25)
    raise RuntimeRefused("new dashboard did not return its healthy /health response")


def _verify_loaded_process(domain, health):
    """A healthy older listener cannot stand in for the process just loaded."""
    service = f"{domain}/{LABEL}"
    result = _run(["launchctl", "print", service], check=False)
    lines = result.stdout.splitlines()
    pids = [int(match[1]) for line in lines
            if (match := re.fullmatch(r"\tpid = ([1-9][0-9]*)", line))]
    if (result.returncode or not lines or lines[0] != service + " = {" or
            lines[-1] != "}" or len(pids) != 1 or type(health.get("pid")) is not int or
            pids[0] != health["pid"]):
        raise RuntimeRefused("health response is not from the newly loaded dashboard process")


def _replace(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _plist_bytes(target):
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise RuntimeRefused("dashboard plist is no longer a regular file; preserving it")
    return target.read_bytes() if target.exists() else None


def _owned_service(target, expected, domain, *, timeout=20):
    """Preserve a newer operator's plist or loaded command during rollback."""
    if _plist_bytes(target) != expected:
        raise RuntimeRefused("dashboard plist changed concurrently; preserving the newer service")
    service = f"{domain}/{LABEL}"
    result = _run(["launchctl", "print", service], check=False, timeout=timeout)
    if result.returncode == 113:
        return False
    lines = result.stdout.splitlines()
    arguments = plistlib.loads(expected).get("ProgramArguments", []) if expected else []
    block = "\targuments = {\n" + "\n".join("\t\t" + argument for argument in arguments) + "\n\t}"
    if (result.returncode or not arguments or not lines or lines[0] != service + " = {" or
            lines[-1] != "}" or lines.count("\tpath = " + str(target)) != 1 or
            lines.count("\tprogram = " + arguments[0]) != 1 or
            lines.count("\targuments = {") != 1 or block not in result.stdout):
        raise RuntimeRefused("loaded dashboard identity changed or is unknown; preserving it for manual recovery")
    return True


def _wait_unloaded(target, expected, domain, *, timeout=10):
    """bootout can return before launchd has removed the service registration."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeRefused("dashboard service did not unload before the shutdown deadline")
        if not _owned_service(target, expected, domain, timeout=min(2, remaining)):
            # Preserve a plist edit made while launchctl was answering.
            if _plist_bytes(target) != expected:
                raise RuntimeRefused("dashboard plist changed concurrently; preserving the newer service")
            return
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def install(config_path, *, expected_fingerprint, home=None):
    home = Path(home) if home is not None else Path.home()
    report = preflight(config_path, home=home)
    if report["fingerprint"] != expected_fingerprint:
        raise RuntimeRefused("runtime, config, service or Serve state changed; review a new preflight")
    config = report["config"]
    authority = urlsplit(config.get("origin", "")).netloc if report["remote"] else None
    target = Path(report["plist"])
    old = _plist_bytes(target)
    replacement = _plist(Path(report["config_path"]), config, home)
    for gap in launch_gaps(plistlib.loads(replacement)["EnvironmentVariables"]):
        print(f"dashboard: {gap}; Notes cannot list or keep quick notes", file=sys.stderr)
    backup_root = home / ".local/share/sd/runtime-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="dashboard-", dir=backup_root))
    if old is not None:
        (backup / target.name).write_bytes(old)
    (backup / "preflight.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    domain = f"gui/{os.getuid()}"
    added_route = attempted_route = replaced = False
    replacement_written = service_conflict = unloading = False
    owned_route = None
    try:
        if report["remote"] and not report["serve_route_present"]:
            if _serve_status() != report["serve_status"]:
                raise RuntimeRefused("Serve configuration changed before route creation; review a new preflight")
            attempted_route = True
            _run(["tailscale", "serve", "--bg", "--https=8443", "--set-path=/",
                  f"http://127.0.0.1:{config['port']}"])
            added_route = True
        current = _serve_status() if report["remote"] else None
        if added_route:
            owned_route = _dashboard_scope(current, authority)
        if report["remote"]:
            _configured_route(config, current)
            if _without_dashboard(current, authority) != _without_dashboard(report["serve_status"], authority):
                raise RuntimeRefused("unrelated Serve configuration changed; refusing LaunchAgent replacement")
        (home / "Library/Logs").mkdir(parents=True, exist_ok=True)
        try:
            if _plist_bytes(target) != old:
                raise RuntimeRefused("dashboard plist changed before replacement; preserving the newer service")
            if report["loaded"] and not _owned_service(target, old, domain):
                raise RuntimeRefused("dashboard service unloaded before replacement; review a new preflight")
        except RuntimeRefused:
            service_conflict = True
            raise
        replaced = True
        if report["loaded"]:
            unloading = True
            _run(["launchctl", "bootout", f"{domain}/{LABEL}"])
            _wait_unloaded(target, old, domain)
            unloading = False
        _replace(target, replacement)
        replacement_written = True
        _run(["launchctl", "bootstrap", domain, str(target)])
        health = health_check(config["port"])
        if health.get("schema") != report["runtime"]["schema"] or health.get("library") != report["runtime"]["library"]:
            raise RuntimeRefused("new dashboard health reports a different schema or library than preflight")
        if any(health.get(key) != report["runtime"][key] for key in ("library_digest", "dashboard_digest")):
            raise RuntimeRefused("new dashboard health reports different build digests than preflight")
        if config.get("ip_origin") and health.get("ip_origin") != config["ip_origin"].rstrip("/"):
            raise RuntimeRefused("new dashboard health does not confirm the configured IP listener")
        _verify_loaded_process(domain, health)
        if report["remote"]:
            final = _serve_status()
            _configured_route(config, final)
            if _without_dashboard(final, authority) != _without_dashboard(report["serve_status"], authority):
                raise RuntimeRefused("unrelated Serve configuration changed during installation")
        return {"ok": True, "origin": config.get("ip_origin") or config.get("origin") or f"http://127.0.0.1:{config['port']}",
                "backup_path": str(backup), "health": health}
    except BaseException as error:
        failures = []
        if replaced:
            try:
                loaded = _owned_service(target, replacement if replacement_written else old, domain)
                if loaded:
                    if not unloading:
                        _run(["launchctl", "bootout", f"{domain}/{LABEL}"])
                    _wait_unloaded(target, replacement if replacement_written else old, domain)
                if old is not None:
                    _replace(target, old)
                elif target.exists():
                    target.unlink()
                if report["loaded"]:
                    _run(["launchctl", "bootstrap", domain, str(target)])
            except BaseException as failure:
                service_conflict = True
                failures.append(str(failure))
        if attempted_route and service_conflict:
            failures.append("dashboard route preserved because service rollback requires manual recovery")
        elif attempted_route:
            try:
                _rollback_route(config, report["serve_status"], owned_route)
            except BaseException as failure:
                failures.append(str(failure))
        detail = f"; rollback requires attention: {'; '.join(failures)}" if failures else "; previous service restored"
        raise RuntimeRefused(f"installation failed: {error}{detail}; backup: {backup}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "install", "health"))
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/sd/dashboard.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fingerprint")
    args = parser.parse_args()
    try:
        if args.action == "health":
            config = read_config(args.config)
            load_frontdoor(args.config, config["port"])
            result = health_check(config["port"], attempts=1)
        elif args.action == "install" and args.apply:
            if not args.expected_fingerprint:
                raise RuntimeRefused("--apply requires --expected-fingerprint from preflight")
            result = install(args.config, expected_fingerprint=args.expected_fingerprint)
        else:
            result = preflight(args.config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeRefused, ImportError, ValueError) as error:
        print(f"dashboard: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
