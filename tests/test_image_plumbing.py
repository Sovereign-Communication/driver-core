"""Image plumbing: the request shape and the scaling math.

Two concerns meet here because both are Landing 1 of the image surface and
neither has a home elsewhere yet:

* the **request shape** -- an ``assets`` map whose keys are reference
  strings, with the far end's danger-zone header attached only on an
  opted-in request that actually carries assets. These tests go through a
  real local HTTP server, because the surface under test *is* the wire
  body and its headers; a mocked request would prove what this module
  meant to send, not what a server receives.

* the **scaling math** -- what size a capture must become to fit the
  endpoint's pixel caps. Pure arithmetic, tested at the boundaries, with
  the platform scaler behind a seam so no test needs a screen.

Hermetic throughout: loopback only, no display, no key.
"""
import json
import os
import struct
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer

from driver_core import osal, transport
from driver_core.config import load_settings


def _prepare_fixture(path):
    """Use the production snapshot boundary with a hermetic compositor seam.

    Fixtures using this helper are already in-range, complete PNG files.
    Actual decoding and rendering are verified separately on Windows.
    """
    return osal.prepare_image(path, scaler=lambda path, *sizes: (path, ""))


def _tiny_png(width, height, *, note=b""):
    """A minimal but genuine 8-bit grayscale PNG."""
    raw = b"".join(b"\x00" + b"\x00" * width for _ in range(height))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                         8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + (chunk(b"tEXt", b"Comment\0" + note) if note else b"")
            + chunk(b"IEND", b""))


class _Recorder(BaseHTTPRequestHandler):
    """Records one request's method, headers and body, then answers 200."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.records.append({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": self.rfile.read(length),
        })
        payload = json.dumps(
            {"model": "jev-1.13.0", "answers": {},
             "usage": {"input_tokens": 0, "output_tokens": 0}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class RequestShapeTests(unittest.TestCase):
    """What actually goes over the wire."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Recorder)
        cls.server.records = []
        cls.url = ("http://127.0.0.1:"
                   f"{cls.server.server_address[1]}/v1/systemone")
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.records.clear()

    def test_the_default_surface_carries_no_assets_and_no_header(self):
        response = transport.call_service(
            self.url, {"asked": transport.noul("Is `screen` showing a dialog?")},
            body_extra={"state": {"screen": "words"}, "model": "jev-latest"})
        self.assertTrue(response.ok)
        (record,) = self.server.records
        body = json.loads(record["body"])
        self.assertEqual(set(body), {"questions", "state", "model"})
        self.assertNotIn("assets", body)
        self.assertNotIn("typesafe-danger-zone-images", record["headers"])

    def test_assets_refuse_to_send_without_the_opt_in(self):
        with self.assertRaises(ValueError) as ctx:
            transport.call_service(
                self.url, {"asked": transport.noul("Is `screen` showing a dialog?")},
                assets={"7d9f2c40-screen": {"type": "image",
                                            "format": "png",
                                            "data_base64": "AAAA"}})
        self.assertIn("images opt-in", str(ctx.exception))
        # Fail closed before dispatch: the server saw nothing at all.
        self.assertEqual(self.server.records, [])

    def test_opted_in_assets_serialize_as_references_not_bytes(self):
        blob = "iVBORw0KGgo=" + "A" * 64
        ref = "7d9f2c40-screen"
        response = transport.call_service(
            self.url, {"asked": transport.noul(
                "Is there an error dialog in `screen`?")},
            headers={"Authorization": "Bearer t"},
            body_extra={"state": {"screen": ref}, "model": "jev-1.14.0"},
            assets={ref: {"type": "image", "format": "png",
                          "data_base64": blob}},
            images_enabled=True)
        self.assertTrue(response.ok)
        (record,) = self.server.records
        body = json.loads(record["body"])
        # The bytes live in exactly one place: the assets map, keyed by
        # the reference that everywhere else appears as a short string.
        self.assertEqual(list(body["assets"]), [ref])
        self.assertEqual(body["assets"][ref]["data_base64"], blob)
        self.assertEqual(body["state"], {"screen": ref})
        placement = json.dumps(body["state"]) + json.dumps(body["questions"])
        self.assertNotIn(blob[:20], placement)
        self.assertNotIn("data_base64", placement)
        # The header marks this request, and the caller's own headers
        # survive alongside it.
        self.assertEqual(record["headers"].get("typesafe-danger-zone-images"),
                         "true")
        self.assertEqual(record["headers"].get("authorization"), "Bearer t")

    def test_opting_in_alone_does_not_mark_a_plain_request(self):
        transport.call_service(
            self.url, {"asked": transport.noul("A plain text question.")},
            body_extra={"state": "words", "model": "jev-latest"},
            images_enabled=True)
        (record,) = self.server.records
        self.assertNotIn("assets", json.loads(record["body"]))
        self.assertNotIn("typesafe-danger-zone-images", record["headers"])

    def test_both_declared_asset_forms_pass_through_untouched(self):
        entries = {
            "aaaa1111-one": {"type": "image", "format": "png",
                             "data_base64": "AAAA"},
            "bbbb2222-two": {"type": "image", "part": "frame-2"},
        }
        transport.call_service(
            self.url, {"asked": transport.noul("Compare `one` against `two`.")},
            body_extra={"state": {"first": "aaaa1111-one",
                                  "second": "bbbb2222-two"},
                        "model": "jev-1.14.0"},
            assets=entries, images_enabled=True)
        (record,) = self.server.records
        self.assertEqual(json.loads(record["body"])["assets"], entries)

    def test_an_asset_map_is_validated_before_anything_is_sent(self):
        for bad in ({}, {"": {"format": "png"}}, {"ref": "not-a-mapping"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    transport.call_service(
                        self.url, {"asked": transport.noul("q")}, assets=bad,
                        images_enabled=True)
        self.assertEqual(self.server.records, [])


class ImagesFlagTests(unittest.TestCase):
    """The opt-in is an operator decision, and off by default."""

    def test_the_images_flag_is_off_unless_declared(self):
        self.assertFalse(load_settings(env={}).jev_images)

    def test_the_images_flag_reads_its_own_variable(self):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(raw=raw):
                self.assertTrue(
                    load_settings(env={"DRIVER_JEV_IMAGES": raw}).jev_images)
        self.assertFalse(
            load_settings(env={"DRIVER_JEV_IMAGES": "0"}).jev_images)


class ScalingMathTests(unittest.TestCase):
    """fit_within: over-limit scaled, under-limit untouched, shape kept."""

    def test_a_full_hd_capture_fits_the_cap(self):
        self.assertEqual(osal.fit_within(1920, 1080), (1568, 882))

    def test_under_limit_dimensions_are_left_alone(self):
        for size in ((1280, 720), (1568, 882), (1568, 1568), (300, 300),
                     (256, 256)):
            with self.subTest(size=size):
                self.assertEqual(osal.fit_within(*size), size)

    def test_the_long_side_lands_exactly_on_the_cap(self):
        for width, height in ((2560, 1440), (3840, 2160), (5760, 1080)):
            with self.subTest(size=(width, height)):
                target = osal.fit_within(width, height)
                self.assertEqual(max(target), osal.MAX_IMAGE_SIDE)

    def test_portrait_and_odd_sizes_keep_their_shape(self):
        for width, height in ((1080, 1920), (1000, 333), (1569, 100)):
            with self.subTest(size=(width, height)):
                target = osal.fit_within(width, height)
                for side in target:
                    self.assertIsInstance(side, int)
                # Aspect preserved to within one pixel of rounding.
                self.assertAlmostEqual(target[0] / target[1], width / height,
                                       delta=(width / height) * 0.01)

    def test_invalid_dimensions_are_refused(self):
        for args in ((0, 100), (100, -1), (100, 100, 0), (100.5, 100)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    osal.fit_within(*args)


class FitTargetTests(unittest.TestCase):
    """fit_target: any capture becomes a valid asset at both bounds."""

    def test_the_floor_is_reached_by_padding_the_canvas(self):
        self.assertEqual(osal.fit_target(100, 100), (280, 280))
        self.assertEqual(osal.fit_target(256, 256), (280, 280))

    def test_a_capture_inside_the_bounds_keeps_its_size(self):
        for size in ((300, 300), (280, 280), (1280, 720), (1568, 1568)):
            with self.subTest(size=size):
                self.assertEqual(osal.fit_target(*size), size)

    def test_an_extreme_aspect_keeps_its_long_side(self):
        # 1000x20 cannot reach the pixel floor at its own aspect without
        # breaching the width cap -- which is exactly why the answer is a
        # padded canvas rather than an upscale.
        self.assertEqual(osal.fit_target(1000, 20), (1000, 280))
        self.assertEqual(osal.fit_target(20, 1000), (280, 1000))

    def test_every_target_is_inside_the_declared_bounds(self):
        for width, height in ((1, 1), (100, 100), (256, 306), (200, 2000),
                              (1080, 1920), (1920, 1080), (5760, 1080),
                              (1569, 100)):
            with self.subTest(size=(width, height)):
                target = osal.fit_target(width, height)
                for side in target:
                    self.assertGreaterEqual(side, osal.MIN_IMAGE_SIDE)
                    self.assertLessEqual(side, osal.MAX_IMAGE_SIDE)
                self.assertGreaterEqual(target[0] * target[1],
                                        osal.MIN_IMAGE_PIXELS)
                self.assertLessEqual(
                    target[0] * target[1],
                    osal.MAX_IMAGE_SIDE * osal.MAX_IMAGE_SIDE)

    def test_invalid_floors_are_refused(self):
        for kwargs in ({"min_side": 0}, {"min_pixels": "x"},
                       {"max_side": 100.5}, {"min_side": 1569},
                       {"min_pixels": 1568 * 1568 + 1}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    osal.fit_target(100, 100, **kwargs)

    def test_custom_area_floor_cannot_grow_past_the_side_cap(self):
        self.assertEqual(osal.fit_target(
            1568, 1, min_side=1, min_pixels=78400), (1568, 50))
        self.assertEqual(osal.fit_target(
            1, 1568, min_side=1, min_pixels=78400), (50, 1568))

    def test_feasible_custom_floors_respect_all_bounds(self):
        for cap in (1, 7, 280, 1568):
            for size in ((1, cap), (cap, 1), (1, 1), (cap * 3, 2)):
                for floor in (1, cap, cap * cap - 1, cap * cap):
                    if floor < 1:
                        continue
                    with self.subTest(cap=cap, size=size, floor=floor):
                        width, height = osal.fit_target(
                            *size, max_side=cap, min_side=1,
                            min_pixels=floor)
                        self.assertTrue(1 <= width <= cap)
                        self.assertTrue(1 <= height <= cap)
                        self.assertGreaterEqual(width * height, floor)


class FitSizeTests(unittest.TestCase):
    """The content keeps its own size on a padded canvas."""

    def test_a_shrinking_fit_fills_its_canvas(self):
        self.assertEqual(osal.fit_size(1920, 1080, 1568, 882), (1568, 882))

    def test_a_padded_canvas_does_not_enlarge_the_content(self):
        self.assertEqual(osal.fit_size(100, 100, 280, 280), (100, 100))
        self.assertEqual(osal.fit_size(1000, 20, 1000, 256), (1000, 20))

    def test_content_larger_than_its_canvas_shrinks_uniformly(self):
        self.assertEqual(osal.fit_size(200, 2000, 256, 1568), (157, 1568))

    def test_invalid_sizes_are_refused(self):
        for args in ((0, 10, 10, 10), (10, 10, 0, 10), (1.5, 10, 10, 10)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    osal.fit_size(*args)


class FitImageTests(unittest.TestCase):
    """The fit decision, with the platform compositor behind a seam."""

    def setUp(self):
        self.calls = []
        self.output_size = None

    def _scaler(self, path, width, height, draw_width, draw_height):
        self.calls.append((path, width, height, draw_width, draw_height))
        self.output_size = (width, height)
        return path, "composed by the test scaler"

    def test_an_over_limit_capture_is_scaled_to_the_target(self):
        path, detail = osal.fit_image(
            "screen.png", reader=lambda p: self.output_size or (1920, 1080),
            scaler=self._scaler)
        self.assertEqual(path, "screen.png")
        self.assertEqual(self.calls,
                         [("screen.png", 1568, 882, 1568, 882)])
        self.assertIn("1920x1080", detail)

    def test_an_under_minimum_capture_grows_its_canvas_not_its_content(self):
        path, detail = osal.fit_image(
            "screen.png", reader=lambda p: self.output_size or (100, 100),
            scaler=self._scaler)
        self.assertEqual(path, "screen.png")
        self.assertEqual(self.calls, [("screen.png", 280, 280, 100, 100)])
        self.assertIn("100x100", detail)

    def test_an_under_limit_capture_is_left_alone(self):
        def explode(path, width, height, draw_width, draw_height):
            raise AssertionError("an in-range capture must not be scaled")

        path, detail = osal.fit_image(
            "screen.png", reader=lambda p: (1280, 720), scaler=explode)
        self.assertEqual(path, "screen.png")
        self.assertIn("within", detail)

    def test_a_capture_that_is_not_a_png_is_refused_by_name(self):
        path, detail = osal.fit_image(
            "notes.txt", reader=lambda p: None, scaler=self._scaler)
        self.assertIsNone(path)
        self.assertIn("not a readable PNG", detail)
        self.assertEqual(self.calls, [])


class PngDimensionsTests(unittest.TestCase):
    """The reader the platform path trusts."""

    def test_dimensions_come_from_the_header(self):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "a.png")
            with open(path, "wb") as handle:
                handle.write(_tiny_png(1920, 1080))
            self.assertEqual(osal.png_dimensions(path), (1920, 1080))

    def test_a_file_that_is_not_a_png_reads_as_none(self):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "a.txt")
            with open(path, "wb") as handle:
                handle.write(b"not a png")
            self.assertIsNone(osal.png_dimensions(path))
            self.assertIsNone(osal.png_dimensions(
                os.path.join(work, "missing.png")))


if __name__ == "__main__":
    unittest.main()
