"""Tests for the toast/AUMID icon generation in notifier/make_icon.py.

The shipped sidecrab.ico is what HKCU ...\\AppUserModelId\\SideCrab.Notifier\\IconUri points at.
A malformed one does not raise anywhere — the shell just shows no icon — so the structure is
asserted here rather than eyeballed.

    python -m unittest discover -s notifier/tests -v
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from make_icon import (  # noqa: E402
    ICO_SIZES,
    SIZE,
    build_pixels,
    downscale,
    read_png,
    write_ico,
    write_png,
)

NOTIFIER_DIR = Path(__file__).resolve().parents[1]


class PngRoundTripTests(unittest.TestCase):
    """read_png must return exactly what write_png was given — the ICO is built from it."""

    def test_round_trip_is_lossless(self) -> None:
        rows = build_pixels()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.png"
            write_png(path, rows)
            self.assertEqual(read_png(path), rows)

    def test_rejects_a_non_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.png"
            path.write_bytes(b"not a png at all")
            with self.assertRaises(ValueError):
                read_png(path)


class DownscaleTests(unittest.TestCase):
    def test_identity_at_the_source_size(self) -> None:
        rows = build_pixels()
        self.assertEqual(downscale(rows, SIZE), rows)

    def test_produces_the_requested_square(self) -> None:
        small = downscale(build_pixels(), 16)
        self.assertEqual(len(small), 16)
        self.assertEqual({len(r) for r in small}, {16})

    def test_transparent_corners_survive_without_black_fringing(self) -> None:
        """Averaging straight (non-premultiplied) RGBA pulls (0,0,0,0) into the tile edge and
        darkens it. The corner must stay transparent and the centre must stay white."""
        small = downscale(build_pixels(), 16)
        self.assertLess(small[0][0][3], 64, "corner should be mostly transparent")
        r, g, b, a = small[8][8]
        self.assertEqual(a, 255)
        self.assertGreater(min(r, g, b), 200, "centre of the crab shell should stay white")

    def test_refuses_a_size_that_does_not_divide_the_source(self) -> None:
        with self.assertRaises(ValueError):
            downscale(build_pixels(), 24)


class IcoStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "x.ico"
        write_ico(self.path, build_pixels())
        self.data = self.path.read_bytes()
        self.addCleanup(self.tmp.cleanup)

    def entries(self) -> list[tuple[int, int, int, int]]:
        _, kind, count = struct.unpack("<HHH", self.data[:6])
        self.assertEqual((kind, count), (1, len(ICO_SIZES)))
        out = []
        for i in range(count):
            w, h, _pal, _res, _planes, bpp, size, offset = struct.unpack(
                "<BBBBHHII", self.data[6 + 16 * i : 22 + 16 * i]
            )
            out.append((w, h, bpp, offset))
            self.assertEqual(size, struct.unpack("<I", self.data[offset + 20 : offset + 24])[0] + 40)
        return out

    def test_carries_every_declared_size_at_32bpp(self) -> None:
        entries = self.entries()
        self.assertEqual([e[0] for e in entries], list(ICO_SIZES))
        self.assertEqual([e[1] for e in entries], list(ICO_SIZES))
        self.assertEqual({e[2] for e in entries}, {32})

    def test_bitmap_height_is_doubled_for_the_and_mask(self) -> None:
        """The trap: biHeight describes XOR + AND as one image. A single height loads as a
        half-height icon on some consumers and as nothing on others."""
        for size, _h, _bpp, offset in self.entries():
            width, height = struct.unpack("<ii", self.data[offset + 4 : offset + 12])
            self.assertEqual(width, size)
            self.assertEqual(height, size * 2)

    def test_offsets_and_lengths_cover_the_file_exactly(self) -> None:
        entries = self.entries()
        end = entries[-1][3] + struct.unpack(
            "<I", self.data[entries[-1][3] + 20 : entries[-1][3] + 24]
        )[0] + 40
        self.assertEqual(end, len(self.data), "directory must account for every byte")


class ShippedArtifactTests(unittest.TestCase):
    """The committed files are what actually ship — regenerating is not the same as shipping."""

    def test_both_artifacts_are_present(self) -> None:
        self.assertTrue((NOTIFIER_DIR / "sidecrab.png").is_file())
        self.assertTrue((NOTIFIER_DIR / "sidecrab.ico").is_file())

    def test_the_shipped_ico_matches_a_fresh_conversion_of_the_shipped_png(self) -> None:
        """Catches an edited PNG that nobody re-converted, and a hand-edited ICO."""
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "fresh.ico"
            write_ico(fresh, read_png(NOTIFIER_DIR / "sidecrab.png"))
            self.assertEqual(fresh.read_bytes(), (NOTIFIER_DIR / "sidecrab.ico").read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
