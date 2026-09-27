"""The app icon, drawn in code.

No image library and no binary file in the repository: the installer calls
:func:`write_icons` and gets a Windows ``.ico`` and a ``.png`` (macOS and Linux
build their own formats from the PNG). A rounded green tile with three rising
candles -- recognisable at 16 pixels, which is where most icons fail.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)

_BG_TOP = (16, 185, 129)     # emerald
_BG_BOTTOM = (4, 120, 87)
_CANDLE = (255, 255, 255)
_WICK = (220, 252, 231)


def _inside_rounded(x: float, y: float, size: int, radius: float) -> float:
    """Coverage (0..1) of a pixel centre by a rounded square, lightly antialiased."""
    r = radius
    cx = min(max(x, r), size - r)
    cy = min(max(y, r), size - r)
    d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    return max(0.0, min(1.0, r - d + 0.5)) if d > 0 else 1.0


def render(size: int) -> bytes:
    """RGBA pixels, row-major, top row first."""
    radius = size * 0.22
    candles = [  # (x centre, body top, body bottom, wick top, wick bottom) as fractions
        (0.28, 0.58, 0.78, 0.50, 0.84),
        (0.50, 0.40, 0.66, 0.30, 0.72),
        (0.72, 0.20, 0.50, 0.12, 0.56),
    ]
    body_w = max(1.0, size * 0.13)
    wick_w = max(1.0, size * 0.035)
    out = bytearray()
    for py in range(size):
        y = py + 0.5
        t = py / max(1, size - 1)
        bg = tuple(int(_BG_TOP[k] * (1 - t) + _BG_BOTTOM[k] * t) for k in range(3))
        for px in range(size):
            x = px + 0.5
            alpha = _inside_rounded(x, y, size, radius)
            color = bg
            for cx, top, bottom, wtop, wbottom in candles:
                cxp = cx * size
                if abs(x - cxp) <= body_w / 2 and top * size <= y <= bottom * size:
                    color = _CANDLE
                    break
                if abs(x - cxp) <= wick_w / 2 and wtop * size <= y <= wbottom * size:
                    color = _WICK
            out += bytes((*color, int(round(255 * alpha))))
    return bytes(out)


def png_bytes(size: int) -> bytes:
    raw = render(size)
    stride = size * 4
    rows = b"".join(b"\x00" + raw[r * stride:(r + 1) * stride] for r in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def ico_bytes(sizes=SIZES) -> bytes:
    """A multi-resolution .ico with PNG-compressed images (Windows Vista and later)."""
    images = [png_bytes(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, data in zip(sizes, images):
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + entries + b"".join(images)


def write_icons(directory: Path) -> dict[str, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ico = directory / "everevolving.ico"
    png = directory / "everevolving.png"
    ico.write_bytes(ico_bytes())
    png.write_bytes(png_bytes(256))
    return {"ico": ico, "png": png}


if __name__ == "__main__":  # pragma: no cover - used by the installers
    import sys

    paths = write_icons(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("."))
    print(paths["ico"])
