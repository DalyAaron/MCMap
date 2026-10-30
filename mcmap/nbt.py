"""Just enough Named Binary Tag support to rename a Java world.

Values are kept as ``(tag_type, payload)`` pairs so a file round-trips byte-for-byte
apart from the fields we change. Compounds are dicts of name -> (type, payload);
lists are ``(element_type, [payload, ...])``.
"""

from __future__ import annotations

import gzip
import io
import os
import struct
from pathlib import Path
from typing import Any, BinaryIO

END, BYTE, SHORT, INT, LONG, FLOAT, DOUBLE, BYTE_ARRAY, STRING, LIST, COMPOUND, INT_ARRAY, LONG_ARRAY = range(13)

_SCALARS = {BYTE: ">b", SHORT: ">h", INT: ">i", LONG: ">q", FLOAT: ">f", DOUBLE: ">d"}
_ARRAYS = {BYTE_ARRAY: ">b", INT_ARRAY: ">i", LONG_ARRAY: ">q"}

Tag = tuple[int, Any]


def _mutf8_decode(raw: bytes) -> str:
    # Java's "modified UTF-8": NUL is C0 80 and astral characters are surrogate pairs.
    text = raw.replace(b"\xc0\x80", b"\x00").decode("utf-8", "surrogatepass")
    return text.encode("utf-16", "surrogatepass").decode("utf-16")


def _mutf8_encode(text: str) -> bytes:
    out = bytearray()
    utf16 = text.encode("utf-16-le", "surrogatepass")
    for unit in struct.unpack(f"<{len(utf16) // 2}H", utf16):
        if unit == 0:
            out += b"\xc0\x80"
        elif unit < 0x80:
            out.append(unit)
        elif unit < 0x800:
            out += bytes((0xC0 | unit >> 6, 0x80 | unit & 0x3F))
        else:
            out += bytes((0xE0 | unit >> 12, 0x80 | (unit >> 6) & 0x3F, 0x80 | unit & 0x3F))
    return bytes(out)


def _read(f: BinaryIO, fmt: str) -> Any:
    size = struct.calcsize(fmt)
    data = f.read(size)
    if len(data) != size:
        raise ValueError("Truncated NBT data")
    return struct.unpack(fmt, data)[0]


def _read_payload(f: BinaryIO, tag_type: int) -> Any:
    if tag_type in _SCALARS:
        return _read(f, _SCALARS[tag_type])
    if tag_type in _ARRAYS:
        n = _read(f, ">i")
        fmt = _ARRAYS[tag_type]
        return list(struct.unpack(f">{n}{fmt[1]}", f.read(n * struct.calcsize(fmt))))
    if tag_type == STRING:
        return _mutf8_decode(f.read(_read(f, ">H")))
    if tag_type == LIST:
        elem, n = _read(f, ">b"), _read(f, ">i")
        return (elem, [_read_payload(f, elem) for _ in range(n)])
    if tag_type == COMPOUND:
        out: dict[str, Tag] = {}
        while (child := _read(f, ">b")) != END:
            name = _read_payload(f, STRING)
            out[name] = (child, _read_payload(f, child))
        return out
    raise ValueError(f"Unknown NBT tag type {tag_type}")


def _write_payload(f: BinaryIO, tag_type: int, value: Any) -> None:
    if tag_type in _SCALARS:
        f.write(struct.pack(_SCALARS[tag_type], value))
    elif tag_type in _ARRAYS:
        fmt = _ARRAYS[tag_type]
        f.write(struct.pack(">i", len(value)))
        f.write(struct.pack(f">{len(value)}{fmt[1]}", *value))
    elif tag_type == STRING:
        raw = _mutf8_encode(value)
        f.write(struct.pack(">H", len(raw)))
        f.write(raw)
    elif tag_type == LIST:
        elem, items = value
        f.write(struct.pack(">bi", elem, len(items)))
        for item in items:
            _write_payload(f, elem, item)
    elif tag_type == COMPOUND:
        for name, (child, payload) in value.items():
            f.write(struct.pack(">b", child))
            _write_payload(f, STRING, name)
            _write_payload(f, child, payload)
        f.write(b"\x00")
    else:
        raise ValueError(f"Unknown NBT tag type {tag_type}")


def loads(data: bytes) -> tuple[str, Tag]:
    """Parse an uncompressed NBT document into (root_name, (type, payload))."""
    f = io.BytesIO(data)
    tag_type = _read(f, ">b")
    name = _read_payload(f, STRING)
    return name, (tag_type, _read_payload(f, tag_type))


def dumps(name: str, tag: Tag) -> bytes:
    f = io.BytesIO()
    tag_type, payload = tag
    f.write(struct.pack(">b", tag_type))
    _write_payload(f, STRING, name)
    _write_payload(f, tag_type, payload)
    return f.getvalue()


def read_gzip(path: Path) -> tuple[str, Tag]:
    return loads(gzip.decompress(path.read_bytes()))


def write_gzip(path: Path, name: str, tag: Tag) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(gzip.compress(dumps(name, tag)))
    os.replace(tmp, path)


def set_level_name(level_dat: Path, level_name: str) -> None:
    """Set Data.LevelName in a Java Edition level.dat (the name shown in the world list)."""
    root_name, (root_type, root) = read_gzip(level_dat)
    if root_type != COMPOUND or "Data" not in root or root["Data"][0] != COMPOUND:
        raise ValueError(f"{level_dat} is not a Java Edition level.dat")
    root["Data"][1]["LevelName"] = (STRING, level_name)
    write_gzip(level_dat, root_name, (root_type, root))


def get_level_name(level_dat: Path) -> str | None:
    _, (_, root) = read_gzip(level_dat)
    tag = root.get("Data", (COMPOUND, {}))[1].get("LevelName")
    return tag[1] if tag else None
