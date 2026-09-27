"""Bind an explicitly selected catalog skill to this isolated run."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from sd_db.runner import RunnerRefused
from sd_db.skills_catalog import _hash, _snapshot


def copy(request: dict, pack_root: str) -> dict:
    source = json.loads(request["scope"].removeprefix("skill-use:"))
    if not isinstance(source, dict) or set(source) != {"name", "root", "path", "source_sha256", "files"}:
        raise RunnerRefused("selected skill has an invalid source binding")
    root = Path(pack_root).resolve()
    if Path(source["root"]).resolve() != root:
        raise RunnerRefused("selected skill does not belong to the provisioned pack")
    path = Path(source["path"])
    if path.is_absolute() or path.parts not in (("skills", source["name"]), ("contrib", source["name"])):
        raise RunnerRefused("selected skill has an unsafe catalog path")
    directory = root / path
    observed = _snapshot(root, directory)
    if observed != source["files"] or _hash(observed) != source["source_sha256"]:
        raise RunnerRefused("selected skill changed; refresh its catalog selection")
    target = Path(request["run"]["work_path"]) / ".git/sd-skill-context"
    target.mkdir(exist_ok=True)
    for relative, digest in observed.items():
        named = Path(relative)
        if named.is_absolute() or not named.is_relative_to(path) or ".." in named.parts:
            raise RunnerRefused("selected skill has a path outside its catalog directory")
        destination = target / named
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / named, destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise RunnerRefused("selected skill changed during isolated context copy")
        destination.chmod(0o400)
    return {"skill_context": str(target / path / "SKILL.md")}
