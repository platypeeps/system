"""The build backend, in the folder, with no dependencies.

A copy of `local-sd-db/_build.py` with different constants. The two are meant
to be diffable: everything below `_members` is byte-identical, so a reviewer
can confirm the mechanism is unchanged and read only the metadata above it.

Sharing one backend between the two folders was the alternative. It was not
taken because `backend-path` resolves against the package directory, so a
shared module would have to sit outside both and would then be missing from
each sdist -- trading a readable duplicate for an unbuildable one.

`sd_dashboard` imports `sd_db`, so this package declares that dependency.
Installs still pass `--no-index`: the requirement is satisfied by installing
`local-sd-db` into the same environment first, never by reaching a network.

There is deliberately no `build_editable`, for the reason the sibling gives:
an editable install puts the checkout back on `sys.path` and a file missing
from the package would still import.
"""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import os
import tarfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "sd_dashboard"
DISTRIBUTION = "sd-dashboard"
VERSION = "0.1.0"
SUMMARY = "The dashboard, its screens and its loopback server."
REQUIRES_PYTHON = ">=3.13"
REQUIRES = ("sd-db",)

TAG = "py3-none-any"

METADATA = "".join(
    [
        "Metadata-Version: 2.1\n",
        f"Name: {DISTRIBUTION}\n",
        f"Version: {VERSION}\n",
        f"Summary: {SUMMARY}\n",
        f"Requires-Python: {REQUIRES_PYTHON}\n",
        *[f"Requires-Dist: {name}\n" for name in REQUIRES],
    ]
)

WHEEL = f"""\
Wheel-Version: 1.0
Generator: sd-dashboard-build ({VERSION})
Root-Is-Purelib: true
Tag: {TAG}
"""

def _members() -> list[tuple[str, Path]]:
    """Everything that ships, as `(arcname, path)`, sorted for reproducibility.

    `__pycache__` is excluded because a stale one would ship compiled files
    for the wrong source.
    """
    members = []
    for path in sorted((HERE / NAME).rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix in (".pyc", ".pyo"):
            continue
        members.append((str(path.relative_to(HERE)), path))
    return members


def _digest(data: bytes) -> tuple[str, int]:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={encoded}", len(data)


def _zip_entry(arcname: str) -> zipfile.ZipInfo:
    """A zip header with nothing in it that varies between two equal builds.

    `writestr` given a bare name stamps the current local time into the entry,
    so two wheels of one revision differ by when they were built rather than by
    what went into them. 1980-01-01 is the earliest a zip can record.
    """
    info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
    info.external_attr = 0o644 << 16
    info.create_system = 3
    return info


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None) -> str:
    dist_info = f"{NAME}-{VERSION}.dist-info"
    filename = f"{NAME}-{VERSION}-{TAG}.whl"
    target = Path(wheel_directory) / filename
    target.parent.mkdir(parents=True, exist_ok=True)

    records: list[tuple[str, str, int]] = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        def write(arcname: str, data: bytes) -> None:
            archive.writestr(_zip_entry(arcname), data, zipfile.ZIP_DEFLATED)
            checksum, size = _digest(data)
            records.append((arcname, checksum, size))

        for arcname, path in _members():
            write(arcname, path.read_bytes())
        write(f"{dist_info}/METADATA", METADATA.encode())
        write(f"{dist_info}/WHEEL", WHEEL.encode())

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        for row in records:
            writer.writerow(row)
        writer.writerow([f"{dist_info}/RECORD", "", ""])
        archive.writestr(
            _zip_entry(f"{dist_info}/RECORD"), buffer.getvalue(), zipfile.ZIP_DEFLATED
        )

    return filename


def _entry(arcname: str, data: bytes, executable: bool) -> tarfile.TarInfo:
    """A tar header with nothing in it that varies between two equal builds.

    `tarfile.add` copies the source file's mtime, uid, gid and owner names, and
    `w:gz` stamps the current time into the gzip header. Either is enough to
    make two source distributions of one revision differ for reasons that are
    not the source, which is the opposite of what the wheel beside it promises.
    """
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = 0o755 if executable else 0o644
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def build_sdist(sdist_directory, config_settings=None) -> str:
    stem = f"{DISTRIBUTION}-{VERSION}"
    filename = f"{stem}.tar.gz"
    target = Path(sdist_directory) / filename
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = io.BytesIO()
    with gzip.GzipFile(fileobj=payload, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            sources = [(f"{stem}/{arcname}", path) for arcname, path in _members()]
            sources += [
                (f"{stem}/{extra}", HERE / extra)
                for extra in ("pyproject.toml", "_build.py", "README.md")
                if (HERE / extra).exists()
            ]
            for arcname, path in sources:
                data = path.read_bytes()
                archive.addfile(
                    _entry(arcname, data, os.access(path, os.X_OK)), io.BytesIO(data)
                )
            metadata = METADATA.encode()
            archive.addfile(
                _entry(f"{stem}/PKG-INFO", metadata, False), io.BytesIO(metadata)
            )
    target.write_bytes(payload.getvalue())
    return filename


def get_requires_for_build_wheel(config_settings=None) -> list[str]:
    return []


def get_requires_for_build_sdist(config_settings=None) -> list[str]:
    return []


# `pip` will fall back to building the wheel when this is absent, which works;
# it is here so `pip install .` does not build twice.
def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None) -> str:
    dist_info = Path(metadata_directory) / f"{NAME}-{VERSION}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(METADATA, encoding="utf-8")
    (dist_info / "WHEEL").write_text(WHEEL, encoding="utf-8")
    (dist_info / "RECORD").write_text("", encoding="utf-8")
    return dist_info.name
