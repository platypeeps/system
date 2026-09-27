"""Private subprocess protocol. No effect occurs before the runner's ACK.

This module is not an operator entrypoint. runner.sh owns all public verbs.
EOF closes setup immediately. During a provider run the parent owns a
registered process group and cancels only that recorded group.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from sd_db.runner import RunnerRefused

from . import exec_protocol, gitops, provider_protocol, skill_context


def emit(value: dict) -> None:
    print(json.dumps(value), flush=True)


def check_entries(stdout: str) -> list | None:
    """The `checks` list of an `sd-check --json` report, or None when the output is not one."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    entries = payload.get("checks") if isinstance(payload, dict) else None
    return entries if isinstance(entries, list) else None


def main() -> int:
    for line in sys.stdin:
        try:
            command = json.loads(line)
            action = command["action"]
            request = command.get("request")
            if action in {"clone", "branch", "merge"}:
                value = {"clone": gitops.clone, "branch": gitops.branch, "merge": gitops.merge_default}[action](request)
            elif action == "skill-context":
                value = skill_context.copy(request, command["pack_root"])
            elif action == "exec":
                if request["role"] != "exec":
                    raise RunnerRefused("finite command requires an exec assignment")
                value = exec_protocol.run(command["descriptor"], cwd=request["run"]["work_path"], home=command["home"])
            elif action == "provider":
                root = Path(request["run"]["work_path"])
                (root / ".git/sd-tmp").mkdir(exist_ok=True)
                (root / ".git/sd-cache").mkdir(exist_ok=True)
                output = root / ".git/sd-provider.log"
                if command.get("reviewer"):
                    (root / ".git/sd-review-schema.json").write_text(json.dumps(provider_protocol.SCHEMA))
                with output.open("wb") as log:
                    provider = subprocess.Popen(command["argv"], cwd=root, env=command["environment"],
                                                stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
                    caffeinate = None
                    if sys.platform == "darwin":
                        caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(provider.pid)],
                                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    provider.communicate(command["prompt"].encode())
                    if caffeinate is not None:
                        try:
                            caffeinate.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            try:
                                caffeinate.terminate()
                            except OSError:
                                pass
                if command.get("reviewer") and provider.returncode == 0:
                    provider_protocol.finish_review(root, Path(command["argv"][0]).name, output)
                value = {"exit_code": provider.returncode, "output_path": str(output)}
            elif action == "check":
                # The repository's own check, in the clone, inside the owned
                # group: a failing test is a hard stop the runner decides.
                completed = subprocess.run(command["argv"], cwd=request["run"]["work_path"], env=command["environment"],
                                           stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
                # `checks` is read from the whole output before the tail is
                # cut: it is what the check record carries (sd:495), and a
                # long run's JSON would not survive the 4000-byte tail.
                value = {"check": {"exit_code": completed.returncode, "stdout": completed.stdout[-4000:],
                                   "stderr": completed.stderr[-4000:], "checks": check_entries(completed.stdout)}}
            elif action == "install":
                # The clone's declared dependencies, before its check (sd:1762).
                completed = subprocess.run(command["argv"], cwd=request["run"]["work_path"], env=command["environment"],
                                           stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
                value = {"install": {"exit_code": completed.returncode, "stdout": completed.stdout[-4000:],
                                     "stderr": completed.stderr[-4000:]}}
            elif action == "close":
                emit({"ok": True, "closed": True})
                return 0
            elif action == "ship":
                completed = subprocess.run(command["argv"], cwd=request["run"]["work_path"],
                                           env=command["environment"], capture_output=True, text=True, check=False)
                if completed.returncode:
                    raise ValueError(f"ship adapter exited {completed.returncode}: {completed.stderr[-2000:] or completed.stdout[-2000:]}")
                value = {"ship": json.loads(completed.stdout)}
            else:
                raise ValueError(f"unknown supervisor action {action}")
            emit({"ok": True, **value})
        except (OSError, ValueError, KeyError, subprocess.SubprocessError, RunnerRefused) as error:
            emit({"ok": False, "error": str(error)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
