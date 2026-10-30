#!/usr/bin/env python3
"""Stand-in for the Arnis CLI, for testing MCMap without hitting real data servers.

Prints the same kind of output as Arnis v3.3.0 (coloured "[n/7]" phases, an indicatif
progress bar redrawn with \\r when attached to a terminal, "Created new world at: ...")
and writes a minimal world. Run MCMap against it with:

    python -m mcmap --arnis tests/fake_arnis.py

Environment knobs: FAKE_ARNIS_DELAY (seconds per phase, default 0.2),
FAKE_ARNIS_FAIL=1 (fail during the fetch phase).
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcmap import nbt  # noqa: E402

BOLD, GREEN, RED, RESET = "\x1b[1m", "\x1b[32m", "\x1b[31m", "\x1b[0m"


def png(width: int, height: int) -> bytes:
    rows = b"".join(
        b"\x00" + bytes(v for x in range(width) for v in (40 + x * 3 % 200, 120 + y % 100, 60))
        for y in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def level_dat(name: str) -> bytes:
    data = {
        "DataVersion": (nbt.INT, 3955),
        "LevelName": (nbt.STRING, name),
        "GameType": (nbt.INT, 1),
        "SpawnX": (nbt.INT, 0),
        "SpawnY": (nbt.INT, 64),
        "SpawnZ": (nbt.INT, 0),
        "LastPlayed": (nbt.LONG, int(time.time() * 1000)),
        "version": (nbt.INT, 19133),
        "Version": (nbt.COMPOUND, {"Name": (nbt.STRING, "1.21.1"), "Id": (nbt.INT, 3955)}),
    }
    import gzip

    return gzip.compress(nbt.dumps("", (nbt.COMPOUND, {"Data": (nbt.COMPOUND, data)})))


def phase(n: str, text: str) -> None:
    print(f"{BOLD}[{n}/7]{RESET} {text}", flush=True)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--bbox", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--bedrock", action="store_true")
    p.add_argument("--map-preview", action="store_true")
    p.add_argument("--caves", action="store_true")
    args, _ = p.parse_known_args()

    delay = float(os.environ.get("FAKE_ARNIS_DELAY", "0.2"))
    out = Path(args.output_dir)
    print("\n        fake ARNIS\n                          version 3.3.0\n", flush=True)

    phase("1", "Fetching data...")
    time.sleep(delay)
    if os.environ.get("FAKE_ARNIS_FAIL"):
        print(f"{RED}{BOLD}Error:{RESET} Failed to fetch data: Overpass API timed out", file=sys.stderr, flush=True)
        return 1
    phase("2", "Parsing data...")
    phase("3", "Fetching elevation...")
    time.sleep(delay)
    phase("4", "Processing data...")
    phase("5", "Generating area...")

    total = 250
    tty = sys.stderr.isatty()
    for i in range(0, total + 1, 25):
        bar = "#" * (i * 45 // total) + "-" * (45 - i * 45 // total)
        frame = f"\x1b[32m⠋\x1b[0m [00:00:0{i // 50}] [{bar}] {i}/{total} elements (0s) "
        if tty:
            sys.stderr.write("\r\x1b[2K" + frame)
        sys.stderr.flush()
        time.sleep(delay / 5)
    if tty:
        sys.stderr.write("\n")
    if args.caves:
        phase("6b", "Carving caves...")
    phase("6", "Generating ground...")
    time.sleep(delay)

    if args.bedrock:
        phase("7", "Saving Bedrock world...")
        world = out / "Arnis Test Area.mcworld"
        with zipfile.ZipFile(world, "w") as zf:
            zf.writestr("levelname.txt", "Arnis World: Test Area")
            zf.writestr("db/CURRENT", "MANIFEST-000001\n")
        if args.map_preview:
            (out / "Arnis Test Area map.png").write_bytes(png(64, 64))
            print(f"Map preview saved to: {out / 'Arnis Test Area map.png'}")
        print(f"{GREEN}{BOLD}Done!{RESET} Bedrock world saved to: {world}", flush=True)
        return 0

    n = 1
    while (out / f"Arnis World {n}").exists():
        n += 1
    world = out / f"Arnis World {n}"
    (world / "region").mkdir(parents=True)
    (world / "level.dat").write_bytes(level_dat(world.name))
    (world / "region" / "r.0.0.mca").write_bytes(os.urandom(8192))
    print(f"Created new world at: {BOLD}{world}{RESET}", flush=True)
    phase("7", "Saving world...")
    time.sleep(delay)
    if args.map_preview:
        (world / "arnis_world_map.png").write_bytes(png(64, 64))
        print(f"Map preview saved to: {world / 'arnis_world_map.png'}", flush=True)
    print("World map item added to the player inventory.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
