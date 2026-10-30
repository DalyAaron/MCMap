"""Filesystem helpers for generated worlds."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_folder_name(name: str, fallback: str = "Minecraft World") -> str:
    """A world name that is a valid folder name on Windows, macOS and Linux."""
    cleaned = _INVALID_CHARS.sub("_", name).strip().rstrip(". ")[:80].strip()
    if not cleaned or cleaned.upper() in _WINDOWS_RESERVED:
        return fallback
    return cleaned


def unique_path(parent: Path, name: str, suffix: str = "") -> Path:
    """parent/name+suffix, or parent/'name (2)'+suffix etc. if that is taken."""
    candidate = parent / f"{name}{suffix}"
    n = 2
    while candidate.exists():
        candidate = parent / f"{name} ({n}){suffix}"
        n += 1
    return candidate


def dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def zip_world(world_dir: Path) -> Path:
    """Zip a Java world folder (with the folder itself at the root) into a temp file."""
    fd, tmp = tempfile.mkstemp(prefix="mcmap-", suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
        for root, dirs, files in os.walk(world_dir):
            dirs.sort()
            for name in sorted(files):
                full = Path(root) / name
                arcname = Path(world_dir.name) / full.relative_to(world_dir)
                # Region files are already zlib-compressed chunk by chunk.
                compress = zipfile.ZIP_STORED if name.endswith((".mca", ".png")) else zipfile.ZIP_DEFLATED
                zf.write(full, arcname.as_posix(), compress_type=compress)
    return Path(tmp)


def reveal(path: Path) -> None:
    """Show a file or folder in the OS file manager."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{path}"])
    else:
        subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
