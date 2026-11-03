"""PNG raster validity, including failures permissive native decoders hide."""
import struct
import unittest
import zlib

from driver_core.png import decodable


def chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data)))


def png(raw, *, width=2, height=2, depth=8, color=0, palette=None,
        interlace=0, compressed=False):
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth,
                                         color, 0, 0, interlace))
            + (chunk(b"PLTE", palette) if palette is not None else b"")
            + chunk(b"IDAT", raw if compressed else zlib.compress(raw))
            + chunk(b"IEND", b""))


class PngValidationTests(unittest.TestCase):
    def test_supported_color_types_and_depths_decode(self):
        for color, depths, channels in ((0, (1, 2, 4, 8, 16), 1),
                                        (2, (8, 16), 3),
                                        (3, (1, 2, 4, 8), 1),
                                        (4, (8, 16), 2),
                                        (6, (8, 16), 4)):
            for depth in depths:
                with self.subTest(color=color, depth=depth):
                    stride = (2 * channels * depth + 7) // 8
                    raw = (b"\0" + bytes(stride)) * 2
                    self.assertTrue(decodable(png(
                        raw, depth=depth, color=color,
                        palette=b"\0\0\0\xff\xff\xff" if color == 3 else None)))

    def test_all_palette_scanline_filters_and_previous_rows(self):
        for method in range(5):
            with self.subTest(method=method):
                raw = b"\0\0\1" + bytes((method, 1, 0 if method == 0 else 255))
                self.assertTrue(decodable(png(
                    raw, color=3, palette=b"\0\0\0\xff\xff\xff")))
        self.assertFalse(decodable(png(
            b"\0\0\2\0\0\0", color=3,
            palette=b"\0\0\0\xff\xff\xff")))

    def test_adam7_raster_has_the_expected_seven_passes(self):
        passes = ((1, 1), (1, 1), (2, 1), (2, 2), (4, 2), (4, 4), (8, 4))
        raw = b"".join((b"\0" + bytes(w)) * h for w, h in passes)
        self.assertTrue(decodable(png(raw, width=8, height=8, interlace=1)))
        self.assertFalse(decodable(png(raw[:-1], width=8, height=8, interlace=1)))

    def test_rejects_bad_streams_sizes_filters_and_compression_tail(self):
        valid = b"\0\0\0\0\0\0"
        for raw in (valid[:-1], valid + b"\0", b"\5\0\0\0\0\0"):
            self.assertFalse(decodable(png(raw)))
        for compressed in (b"not-zlib", zlib.compress(valid)[:-1],
                           zlib.compress(valid) + b"extra"):
            self.assertFalse(decodable(png(compressed, compressed=True)))
        self.assertFalse(decodable(png(bytes(2_000_000))))

    def test_rejects_truncation_crc_damage_animation_and_trailing_bytes(self):
        valid = png(b"\0\0\0\0\0\0")
        for bad in (b"", valid[:24], valid[:-1], valid + b"extra",
                    valid[:40] + bytes((valid[40] ^ 1,)) + valid[41:],
                    valid[:-12] + chunk(b"acTL", struct.pack(">II", 2, 0))
                    + valid[-12:]):
            self.assertFalse(decodable(bad))


if __name__ == "__main__":
    unittest.main()
