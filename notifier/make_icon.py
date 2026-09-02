r"""Generate notifier/sidecrab.png and notifier/sidecrab.ico — the toast logo and the AUMID
icon. Run once; the two files are the artifacts.

Why generate rather than reuse an existing asset: widget/resources/icon.svg is a monochrome
WHITE crab on transparent (invisible against a light-theme toast) and preview.png is a 128x56
banner (wrong aspect for appLogoOverride). Neither may be modified — this lane only reads them.
So we re-draw the same pixel-crab geometry (viewBox 26x20, copied from icon.svg) as white on a
solid SideCrab-orange tile, which reads correctly in both Windows themes.

The .ico is CONVERTED FROM the .png rather than re-rendered from the geometry: one drawing,
one source of truth, and an edited PNG carries into the icon on the next run. `read_png` only
accepts what `write_png` emits (8-bit RGBA, non-interlaced) — a PNG of any other shape raises
rather than producing a silently wrong icon.

Why an .ico at all: `HKCU\...\AppUserModelId\SideCrab.Notifier\IconUri` is read by the shell,
which wants a multi-resolution icon, not a single 64px bitmap (see setup/Register-SideCrabAumid.ps1).
The toast's own appLogoOverride keeps using the PNG.

Pure stdlib (zlib + struct): the notifier ships with zero pip dependencies and this must not
be the thing that breaks that.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 64
SCALE = 2
OFFSET_X = 6
OFFSET_Y = 12

ORANGE = (232, 106, 51, 255)
WHITE = (255, 255, 255, 255)

# Rects copied verbatim from widget/resources/icon.svg (x, y, w, h) in its 26x20 viewBox.
CRAB_RECTS = [
    (4, 4, 18, 10),   # shell
    (0, 6, 4, 3), (0, 4, 2, 2),    # left claw
    (22, 6, 4, 3), (24, 4, 2, 2),  # right claw
    (5, 15, 2, 4), (10, 14, 2, 4), (14, 14, 2, 4), (19, 15, 2, 4),  # legs
]

CORNER_RADIUS = 10

#: Sizes baked into the .ico. Both are integer divisors of SIZE, so downscaling is an exact
#: box average with no resampling artefacts on the tile's rounded corners.
ICO_SIZES = (16, 32, 64)

Pixel = tuple[int, int, int, int]
Rows = list[list[Pixel]]


def _rounded(x: int, y: int) -> bool:
    """True when (x, y) is inside the rounded-square tile."""
    r = CORNER_RADIUS
    cx = r if x < r else (SIZE - 1 - r if x > SIZE - 1 - r else x)
    cy = r if y < r else (SIZE - 1 - r if y > SIZE - 1 - r else y)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def build_pixels() -> list[list[tuple[int, int, int, int]]]:
    rows = [[(0, 0, 0, 0)] * SIZE for _ in range(SIZE)]
    for y in range(SIZE):
        for x in range(SIZE):
            if _rounded(x, y):
                rows[y][x] = ORANGE

    for rx, ry, rw, rh in CRAB_RECTS:
        for dy in range(rh * SCALE):
            for dx in range(rw * SCALE):
                x = OFFSET_X + rx * SCALE + dx
                y = OFFSET_Y + ry * SCALE + dy
                if 0 <= x < SIZE and 0 <= y < SIZE:
                    rows[y][x] = WHITE
    return rows


def write_png(path: Path, rows: list[list[tuple[int, int, int, int]]]) -> None:
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0 (None) per scanline
        for px in row:
            raw.extend(px)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))  # 8-bit RGBA
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def read_png(path: Path) -> Rows:
    """Decode an 8-bit RGBA non-interlaced PNG — exactly what write_png emits.

    Deliberately narrow: a palette, greyscale or interlaced PNG raises instead of being
    guessed at, because the failure mode of a guess is a wrong-looking icon that nothing
    reports. All five PNG filter types are handled — write_png only uses 0, but a PNG
    re-saved by an image editor will carry the others.
    """
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG")

    header: tuple[int, int, int, int, int] | None = None
    idat = bytearray()
    pos = 8
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length  # 4 length + 4 tag + body + 4 CRC
        if tag == b"IHDR":
            w, h, depth, color, compression, filt, interlace = struct.unpack(">IIBBBBB", body)
            if (depth, color) != (8, 6):
                raise ValueError(f"{path}: need 8-bit RGBA (depth 8, colour type 6), got {depth}/{color}")
            if compression or filt or interlace:
                raise ValueError(f"{path}: only non-interlaced, standard-filter PNGs are supported")
            header = (w, h, depth, color, interlace)
        elif tag == b"IDAT":
            idat.extend(body)
        elif tag == b"IEND":
            break
    if header is None:
        raise ValueError(f"{path}: no IHDR")

    width, height = header[0], header[1]
    raw = zlib.decompress(bytes(idat))
    stride = width * 4
    if len(raw) != (stride + 1) * height:
        raise ValueError(f"{path}: unexpected image data length")

    rows: Rows = []
    prior = bytearray(stride)
    at = 0
    for _ in range(height):
        filter_type = raw[at]
        line = bytearray(raw[at + 1 : at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0       # byte to the left
            b = prior[i]                            # byte above
            c = prior[i - 4] if i >= 4 else 0       # byte above-left
            if filter_type == 0:
                value = line[i]
            elif filter_type == 1:
                value = line[i] + a
            elif filter_type == 2:
                value = line[i] + b
            elif filter_type == 3:
                value = line[i] + (a + b) // 2
            elif filter_type == 4:
                # Paeth
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                value = line[i] + pred
            else:
                raise ValueError(f"{path}: unknown filter type {filter_type}")
            line[i] = value & 0xFF
        prior = line
        rows.append([tuple(line[x * 4 : x * 4 + 4]) for x in range(width)])  # type: ignore[misc]
    return rows


def downscale(rows: Rows, size: int) -> Rows:
    """Box-average to `size`, in PREMULTIPLIED alpha.

    Averaging straight RGBA would pull the transparent corner pixels' colour (0,0,0,0) into
    the tile edge and fringe it black. Premultiplying first is what makes the rounded corners
    survive the 64->16 step.
    """
    source = len(rows)
    if size == source:
        return [list(row) for row in rows]
    if source % size:
        raise ValueError(f"{size} does not divide the {source}px source")
    factor = source // size

    out: Rows = []
    for y in range(size):
        line: list[Pixel] = []
        for x in range(size):
            r = g = b = a = 0
            for dy in range(factor):
                for dx in range(factor):
                    pr, pg, pb, pa = rows[y * factor + dy][x * factor + dx]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a:
                line.append((round(r / a), round(g / a), round(b / a), round(a / (factor * factor))))
            else:
                line.append((0, 0, 0, 0))
        out.append(line)
    return out


def _ico_image(rows: Rows) -> bytes:
    """One ICO entry body: BITMAPINFOHEADER + bottom-up 32-bit BGRA + 1bpp AND mask.

    Two traps live here. biHeight is DOUBLE the real height (the header describes the XOR
    and AND bitmaps as one image) and the rows are bottom-up. The AND mask is redundant for
    a 32-bit icon on any Windows that reads the alpha channel, but it is not optional: an
    icon written without it is rejected outright by some shell consumers.
    """
    size = len(rows)
    xor = bytearray()
    for y in range(size - 1, -1, -1):
        for r, g, b, a in rows[y]:
            xor.extend((b, g, r, a))  # BGRA, not RGBA

    mask_stride = ((size + 31) // 32) * 4  # 1bpp rows padded to 4 bytes
    mask = bytearray()
    for y in range(size - 1, -1, -1):
        bits = bytearray(mask_stride)
        for x in range(size):
            if rows[y][x][3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)  # 1 = transparent
        mask.extend(bits)

    header = struct.pack(
        "<IiiHHIIiiII",
        40,             # biSize
        size,           # biWidth
        size * 2,       # biHeight - XOR + AND, see docstring
        1,              # biPlanes
        32,             # biBitCount
        0,              # biCompression = BI_RGB
        len(xor) + len(mask),
        0, 0, 0, 0,     # resolution / palette counts: unused
    )
    return header + bytes(xor) + bytes(mask)


def write_ico(path: Path, rows: Rows, sizes: tuple[int, ...] = ICO_SIZES) -> None:
    images = [_ico_image(downscale(rows, s)) for s in sizes]

    offset = 6 + 16 * len(images)  # ICONDIR + one ICONDIRENTRY each
    directory = bytearray(struct.pack("<HHH", 0, 1, len(images)))  # reserved, type 1 = icon
    for size, image in zip(sizes, images):
        directory.extend(
            struct.pack(
                "<BBBBHHII",
                size if size < 256 else 0,  # 0 encodes 256 in this field
                size if size < 256 else 0,
                0,      # palette entries
                0,      # reserved
                1,      # colour planes
                32,     # bits per pixel
                len(image),
                offset,
            )
        )
        offset += len(image)

    path.write_bytes(bytes(directory) + b"".join(images))


if __name__ == "__main__":
    png = Path(__file__).with_name("sidecrab.png")
    write_png(png, build_pixels())
    print(f"wrote {png} ({png.stat().st_size} bytes, {SIZE}x{SIZE} RGBA)")

    ico = Path(__file__).with_name("sidecrab.ico")
    write_ico(ico, read_png(png))
    print(f"wrote {ico} ({ico.stat().st_size} bytes, {'/'.join(str(s) for s in ICO_SIZES)}px)")
