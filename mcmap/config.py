"""Paths and settings shared across the app."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "static"


def default_minecraft_saves_dir() -> Path | None:
    """Return the Java Edition saves folder for this OS, if Minecraft is installed."""
    home = Path.home()
    if sys.platform == "darwin":
        candidate = home / "Library" / "Application Support" / "minecraft" / "saves"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        candidate = Path(appdata) / ".minecraft" / "saves" if appdata else None
    else:
        candidate = home / ".minecraft" / "saves"
    if candidate and candidate.parent.is_dir():
        return candidate
    return None


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    # Explicit path to an Arnis executable. When unset, the app manages its own copy.
    arnis_path: Path | None = None
    saves_dir: Path | None = field(default_factory=default_minecraft_saves_dir)

    @property
    def bin_dir(self) -> Path:
        return self.data_dir / "bin"

    @property
    def worlds_dir(self) -> Path:
        return self.data_dir / "worlds"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def jobs_file(self) -> Path:
        return self.data_dir / "jobs.json"

    def ensure_dirs(self) -> None:
        for d in (self.bin_dir, self.worlds_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)
