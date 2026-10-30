"""Everything that knows about the Arnis CLI: installing it and building its command line.

Arnis (https://github.com/louis-e/arnis, Apache-2.0) does the actual world generation.
Flags here mirror src/args.rs at the pinned release.
"""

from __future__ import annotations

import math
import os
import shutil
import stat
import sys
import tarfile
import threading
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Config

ARNIS_VERSION = "v3.3.0"
RELEASE_URL = "https://github.com/louis-e/arnis/releases/download/{version}/{asset}"
USER_AGENT = "MCMap/0.1 (+local Arnis frontend)"

# Arnis accepts 0.05..4 blocks per metre and 0.1..10x terrain height.
MIN_SCALE, MAX_SCALE = 0.05, 4.0
MIN_HEIGHT_MULT, MAX_HEIGHT_MULT = 0.1, 10.0
# Arnis prints a warning above 200 km² (at scale 1). We refuse well past that so a
# mis-drawn box can't tie the machine up for hours.
MAX_AREA_KM2 = 400.0

EDITIONS = ("java", "bedrock")
MODES = ("geo-terrain", "geo-only", "terrain-only")
GAMEMODES = ("creative", "survival", "spectator")
WORLD_TYPES = ("void", "flat")
SIGNAGE_LEVELS = ("none", "basic", "full")


def release_asset() -> str:
    if sys.platform == "darwin":
        return "arnis-mac-universal.tar.gz"
    if sys.platform == "win32":
        return "arnis-windows.exe"
    if sys.platform.startswith("linux"):
        return "arnis-linux.tar.gz"
    raise RuntimeError(f"Arnis has no prebuilt binary for {sys.platform}")


class ArnisInstaller:
    """Locates the Arnis executable, downloading the pinned release on request."""

    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.status = "missing"  # missing | downloading | installed | error
        self.progress = 0.0
        self.error: str | None = None
        if self.binary_path():
            self.status = "installed"

    @property
    def managed_path(self) -> Path:
        name = "arnis.exe" if sys.platform == "win32" else "arnis"
        return self.config.bin_dir / name

    def binary_path(self) -> Path | None:
        if self.config.arnis_path:
            return self.config.arnis_path if self.config.arnis_path.is_file() else None
        return self.managed_path if self.managed_path.is_file() else None

    def installed_version(self) -> str | None:
        if self.config.arnis_path:
            return "custom"
        marker = self.config.bin_dir / "arnis.version"
        return marker.read_text().strip() if marker.is_file() else None

    def to_dict(self) -> dict[str, Any]:
        path = self.binary_path()
        return {
            "status": self.status,
            "progress": self.progress,
            "error": self.error,
            "path": str(path) if path else None,
            "version": self.installed_version(),
            "pinned_version": ARNIS_VERSION,
            "custom": self.config.arnis_path is not None,
        }

    def install_async(self) -> None:
        with self._lock:
            if self.status in ("downloading", "installed"):
                return
            self.status, self.progress, self.error = "downloading", 0.0, None
            self._thread = threading.Thread(target=self._install_guarded, daemon=True)
            self._thread.start()

    def ensure_installed(self) -> Path:
        """Block until Arnis is available, downloading it if needed."""
        path = self.binary_path()
        if path:
            return path
        if self.config.arnis_path:
            raise RuntimeError(f"Arnis not found at {self.config.arnis_path}")
        self.install_async()
        if self._thread:
            self._thread.join()
        path = self.binary_path()
        if not path:
            raise RuntimeError(f"Could not install Arnis: {self.error or 'unknown error'}")
        return path

    def _install_guarded(self) -> None:
        try:
            self.install(lambda frac: setattr(self, "progress", frac))
            self.status, self.progress = "installed", 1.0
        except Exception as exc:  # surfaced to the UI
            self.status, self.error = "error", str(exc)

    def install(self, on_progress: Callable[[float], None] = lambda _: None) -> Path:
        asset = release_asset()
        url = RELEASE_URL.format(version=ARNIS_VERSION, asset=asset)
        bin_dir = self.config.bin_dir
        bin_dir.mkdir(parents=True, exist_ok=True)
        part = bin_dir / f"{asset}.part"

        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if total:
                    on_progress(min(done / total, 1.0) * 0.95)

        staged = bin_dir / (self.managed_path.name + ".new")
        if asset.endswith(".tar.gz"):
            # Copy the single executable out of the archive rather than calling
            # extractall, so archive paths can never escape bin_dir.
            with tarfile.open(part, "r:gz") as tar:
                members = [m for m in tar.getmembers() if m.isfile()]
                if not members:
                    raise RuntimeError(f"{asset} contains no files")
                exe = max(members, key=lambda m: m.size)
                src = tar.extractfile(exe)
                assert src is not None
                with src, open(staged, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            part.unlink()
        else:
            os.replace(part, staged)

        mode = staged.stat().st_mode
        staged.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(staged, self.managed_path)
        (bin_dir / "arnis.version").write_text(ARNIS_VERSION + "\n")
        on_progress(1.0)
        return self.managed_path


@dataclass
class BBox:
    south: float
    west: float
    north: float
    east: float

    def contains(self, lat: float, lng: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lng <= self.east

    def size_km(self) -> tuple[float, float]:
        mid = math.radians((self.south + self.north) / 2)
        width = (self.east - self.west) * 111.320 * math.cos(mid)
        height = (self.north - self.south) * 110.574
        return width, height

    def area_km2(self) -> float:
        w, h = self.size_km()
        return w * h


@dataclass
class GenerationRequest:
    bbox: BBox
    name: str = ""
    spawn: tuple[float, float] | None = None
    edition: str = "java"
    mode: str = "geo-terrain"
    gamemode: str = "creative"
    world_time: int = 6000
    scale: float = 1.0
    height_multiplier: float = 1.0
    interior: bool = False
    caves: bool = False
    world_type: str = "void"
    signage: str = "basic"
    props_3d: bool = True
    overture: bool = True
    map_preview: bool = True
    install: bool = True

    def to_dict(self) -> dict[str, Any]:
        # Same shape from_dict accepts, so a stored request can be re-validated and retried.
        d = asdict(self)
        d["spawn"] = {"lat": self.spawn[0], "lng": self.spawn[1]} if self.spawn else None
        return d

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GenerationRequest":
        """Validate untrusted JSON from the UI. Raises ValueError with a user-facing message."""
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")

        raw_bbox = payload.get("bbox")
        if not isinstance(raw_bbox, dict):
            raise ValueError("Select an area on the map first")
        bbox = BBox(*(_number(raw_bbox, k) for k in ("south", "west", "north", "east")))
        if not (-85 <= bbox.south < bbox.north <= 85):
            raise ValueError("Area latitude must be between -85 and 85, with south < north")
        if not (-180 <= bbox.west < bbox.east <= 180):
            raise ValueError("Area longitude must be between -180 and 180, with west < east")

        scale = _number(payload, "scale", 1.0)
        if not MIN_SCALE <= scale <= MAX_SCALE:
            raise ValueError(f"Scale must be between {MIN_SCALE} and {MAX_SCALE}")
        area_blocks = bbox.area_km2() * scale * scale
        if area_blocks > MAX_AREA_KM2:
            raise ValueError(
                f"Area is too large ({area_blocks:.0f} km² at scale {scale:g}); "
                f"the limit is {MAX_AREA_KM2:.0f} km². Shrink the area or lower the scale."
            )

        spawn = None
        raw_spawn = payload.get("spawn")
        if raw_spawn:
            if not isinstance(raw_spawn, dict):
                raise ValueError("Spawn point must be an object with lat and lng")
            lat, lng = _number(raw_spawn, "lat"), _number(raw_spawn, "lng")
            if not bbox.contains(lat, lng):
                raise ValueError("Spawn point must be inside the selected area")
            spawn = (lat, lng)

        height_mult = _number(payload, "height_multiplier", 1.0)
        if not MIN_HEIGHT_MULT <= height_mult <= MAX_HEIGHT_MULT:
            raise ValueError(f"Terrain height must be between {MIN_HEIGHT_MULT}x and {MAX_HEIGHT_MULT}x")

        world_time = int(_number(payload, "world_time", 6000))
        if not 0 <= world_time < 24000:
            raise ValueError("Time of day must be between 0 and 23999 ticks")

        name = payload.get("name") or ""
        if not isinstance(name, str):
            raise ValueError("World name must be text")

        return cls(
            bbox=bbox,
            name=name.strip()[:80],
            spawn=spawn,
            edition=_choice(payload, "edition", EDITIONS),
            mode=_choice(payload, "mode", MODES),
            gamemode=_choice(payload, "gamemode", GAMEMODES),
            world_time=world_time,
            scale=scale,
            height_multiplier=height_mult,
            interior=_flag(payload, "interior", False),
            caves=_flag(payload, "caves", False),
            world_type=_choice(payload, "world_type", WORLD_TYPES),
            signage=_choice(payload, "signage", SIGNAGE_LEVELS, default="basic"),
            props_3d=_flag(payload, "props_3d", True),
            overture=_flag(payload, "overture", True),
            map_preview=_flag(payload, "map_preview", True),
            install=_flag(payload, "install", True),
        )


def _number(obj: dict[str, Any], key: str, default: float | None = None) -> float:
    value = obj.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"'{key}' must be a number")
    return float(value)


def _choice(obj: dict[str, Any], key: str, allowed: tuple[str, ...], default: str | None = None) -> str:
    value = obj.get(key, default or allowed[0])
    if value not in allowed:
        raise ValueError(f"'{key}' must be one of: {', '.join(allowed)}")
    return value


def _flag(obj: dict[str, Any], key: str, default: bool) -> bool:
    value = obj.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"'{key}' must be true or false")
    return value


def _bool(value: bool) -> str:
    return "true" if value else "false"


def build_command(binary: Path, req: GenerationRequest, output_dir: Path) -> list[str]:
    """Translate a request into an Arnis CLI invocation."""
    cmd = [sys.executable, str(binary)] if binary.suffix == ".py" else [str(binary)]
    b = req.bbox
    cmd += [
        # Arnis wants min_lat,min_lng,max_lat,max_lng.
        f"--bbox={b.south:.6f},{b.west:.6f},{b.north:.6f},{b.east:.6f}",
        f"--output-dir={output_dir}",
        f"--mode={req.mode}",
        f"--gamemode={req.gamemode}",
        f"--world-time={req.world_time}",
        f"--scale={req.scale:g}",
        f"--height-multiplier={req.height_multiplier:g}",
        f"--interior={_bool(req.interior)}",
        f"--overture={_bool(req.overture)}",
    ]
    if req.edition == "bedrock":
        cmd.append("--bedrock")
    else:
        # Both are Java-only features in Arnis.
        cmd += [f"--world-type={req.world_type}", f"--signage={req.signage}"]
    if req.caves:
        cmd.append("--caves")
    if not req.props_3d:
        cmd.append("--no-3d")
    if req.map_preview:
        cmd.append("--map-preview")
    if req.spawn:
        lat, lng = req.spawn
        cmd += [f"--spawn-lat={lat:.6f}", f"--spawn-lng={lng:.6f}"]
    return cmd
