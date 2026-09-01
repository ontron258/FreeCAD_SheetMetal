"""Reproducible FreeCAD and addon compatibility lockfiles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import FreeCAD as App

from .packet import SCHEMA_VERSION


LOCK_SCHEMA_VERSION = 1
COLLABORATION_CORE_VERSION = 1
CODE_SUFFIXES = {
    ".cfg",
    ".dll",
    ".ini",
    ".json",
    ".pyd",
    ".py",
    ".pyi",
    ".qml",
    ".so",
    ".toml",
    ".ui",
    ".xml",
    ".yaml",
    ".yml",
}
IGNORED_PARTS = {
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".vscode",
    "__pycache__",
    "collaborationtests",
    "smtests",
    "tests",
    "tools",
}
_DEFAULT_ID = None


def _hash_bytes(digest, value: bytes):
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _addon_metadata(path: Path):
    package_xml = path / "package.xml"
    if not package_xml.exists():
        return path.name, ""
    try:
        root = ET.parse(package_xml).getroot()
        name = root.findtext("{*}name") or root.findtext("name") or path.name
        version = root.findtext("{*}version") or root.findtext("version") or ""
        return name.strip(), version.strip()
    except (ET.ParseError, OSError):
        return path.name, ""


def _git_output(path: Path, *arguments):
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={path.as_posix()}", "-C", str(path), *arguments],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _code_files(path: Path):
    tracked = _git_output(
        path, "ls-files", "--cached", "--others", "--exclude-standard"
    )
    if tracked:
        candidates = (path / relative for relative in tracked.splitlines())
    else:
        candidates = path.rglob("*")
    files = []
    for candidate in candidates:
        try:
            relative = candidate.relative_to(path)
        except ValueError:
            continue
        if any(part.casefold() in IGNORED_PARTS for part in relative.parts):
            continue
        if candidate.name.casefold().endswith(".lock.json"):
            continue
        if candidate.is_file() and candidate.suffix.lower() in CODE_SUFFIXES:
            files.append((relative.as_posix(), candidate))
    return sorted(files)


def _tree_digest(path: Path):
    digest = hashlib.sha256()
    count = 0
    for relative, candidate in _code_files(path):
        _hash_bytes(digest, relative.encode("utf-8"))
        _hash_bytes(digest, candidate.read_bytes())
        count += 1
    return digest.hexdigest(), count


def addon_lock(path) -> dict:
    path = Path(path).resolve()
    name, version = _addon_metadata(path)
    tree_sha256, file_count = _tree_digest(path)
    commit = _git_output(path, "rev-parse", "HEAD")
    dirty = bool(_git_output(path, "status", "--porcelain", "--untracked-files=all"))
    return {
        "name": name,
        "version": version,
        "git_commit": commit,
        "dirty": dirty,
        "code_tree_sha256": tree_sha256,
        "code_file_count": file_count,
    }


def build_environment_lock(addon_paths=None) -> dict:
    if addon_paths is None:
        addon_paths = [Path(__file__).resolve().parents[1]]
    version = list(App.Version())
    addons = sorted(
        (addon_lock(path) for path in addon_paths), key=lambda value: value["name"]
    )
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "freecad": {
            "version": ".".join(version[:3]),
            "revision": version[7] if len(version) > 7 else "unknown",
        },
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "packet_schema": SCHEMA_VERSION,
        "collaboration_core": COLLABORATION_CORE_VERSION,
        "addons": addons,
    }


def canonical_lock_json(lock: dict) -> str:
    return json.dumps(lock, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def environment_lock_id(lock: dict) -> str:
    identity_lock = json.loads(canonical_lock_json(lock))
    for addon in identity_lock.get("addons", []):
        addon.pop("dirty", None)
    digest = hashlib.sha256(canonical_lock_json(identity_lock).encode("ascii")).hexdigest()
    return f"lock:{digest}"


def default_environment_id() -> str:
    global _DEFAULT_ID
    override = os.environ.get("FREECAD_COLLABORATION_ENVIRONMENT_ID", "").strip()
    if override:
        return override
    if _DEFAULT_ID is None:
        _DEFAULT_ID = environment_lock_id(build_environment_lock())
    return _DEFAULT_ID


def write_environment_lock(path, lock=None):
    lock = lock or build_environment_lock()
    Path(path).write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return environment_lock_id(lock)


def read_environment_lock(path):
    lock = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(lock.get("schema_version", 0)) != LOCK_SCHEMA_VERSION:
        raise ValueError(f"unsupported environment lock schema: {lock.get('schema_version')}")
    return lock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--addon", action="append", dest="addons")
    arguments = parser.parse_args(argv)
    lock = build_environment_lock(arguments.addons)
    identity = write_environment_lock(arguments.output, lock)
    print(identity)


if __name__ == "__main__":
    main()
