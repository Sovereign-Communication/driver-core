"""Measured image bounds, compositor validation and source-coordinate mapping."""
import base64
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from driver_core import osal, transport
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.extractors import VisionExtractor
from driver_core.states import SCREEN_SCHEMA
from test_image_plumbing import _tiny_png


class ImagePreparationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "crop.png"

    def compose(self, path, width, height, draw_width, draw_height):
        Path(path).write_bytes(_tiny_png(width, height))
        return path, ""

    def prepare(self, width, height):
        self.path.write_bytes(_tiny_png(width, height))
        return osal.prepare_image(str(self.path), scaler=self.compose)

    def test_measured_minimum_boundary_is_padded_on_each_axis(self):
        for width, height in ((255, 400), (256, 400), (279, 400),
                              (280, 400), (400, 256), (400, 279), (256, 307)):
            with self.subTest(size=(width, height)):
                prepared, why = self.prepare(width, height)
                self.assertIsNotNone(prepared, why)
                geometry = prepared.geometry
                self.assertEqual(struct.unpack(">II", prepared.data[16:24]),
                                 (max(280, width), max(280, height)))
                self.assertEqual((geometry.draw_width, geometry.draw_height),
                                 (width, height))

    def test_narrow_crops_never_stretch_and_round_trip_the_content_box(self):
        for width, height in ((1, 1000), (1000, 1), (20, 1000),
                              (1000, 20), (279, 280), (1, 1), (3000, 7)):
            with self.subTest(size=(width, height)):
                prepared, why = self.prepare(width, height)
                self.assertIsNotNone(prepared, why)
                g = prepared.geometry
                dimensions = struct.unpack(">II", prepared.data[16:24])
                self.assertGreaterEqual(min(dimensions), 280)
                self.assertLessEqual(max(dimensions), 1568)
                self.assertLessEqual(g.draw_width, width)
                self.assertLessEqual(g.draw_height, height)
                expected = osal.fit_within(width, height)
                self.assertEqual((g.draw_width, g.draw_height), expected)
                x, y = g.offset
                box = {"left": x / g.canvas_width,
                       "top": y / g.canvas_height,
                       "right": (x + g.draw_width) / g.canvas_width,
                       "bottom": (y + g.draw_height) / g.canvas_height}
                mapped = g.source_box(box)
                for edge, value in {"left": 0, "top": 0,
                                    "right": 1, "bottom": 1}.items():
                    self.assertAlmostEqual(mapped[edge], value)

    def test_shrinking_retains_mapping_to_original_image_fractions(self):
        prepared, _ = self.prepare(1920, 1080)
        g = prepared.geometry
        box = {"left": .25, "top": .1, "right": .75, "bottom": .8}
        for edge, value in g.source_box(box).items():
            self.assertAlmostEqual(value, box[edge])
        self.assertEqual((g.width, g.height), (1920, 1080))

    def test_odd_padding_has_a_deterministic_integer_origin(self):
        prepared, _ = self.prepare(1, 1000)
        g = prepared.geometry
        self.assertEqual(g.offset, (139, 0))
        self.assertIsNone(g.source_box({
            "left": 0, "right": .1, "top": 0, "bottom": 1}))
        mapped = g.source_box({"left": 0, "right": 1,
                               "top": .25, "bottom": .75})
        self.assertEqual(mapped, {"left": 0, "right": 1,
                                  "top": .25, "bottom": .75})

    def test_in_range_source_is_preserved_but_still_decoded(self):
        original = _tiny_png(280, 400)
        self.path.write_bytes(original)
        with patch.object(osal, "_scale_image", side_effect=self.compose) as scale:
            prepared, _ = osal.prepare_image(str(self.path))
        scale.assert_called_once()
        self.assertNotEqual(Path(scale.call_args.args[0]), self.path)
        self.assertFalse(Path(scale.call_args.args[0]).exists())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(prepared.geometry.offset, (0, 0))

    def test_invalid_fit_caps_refuse_without_composition(self):
        self.path.write_bytes(_tiny_png(400, 400))
        for cap in (0, 279, 1569, "x"):
            with self.subTest(cap=cap), patch.object(osal, "_scale_image") as scale:
                prepared, why = osal.prepare_image(
                    str(self.path), max_side=cap)
                self.assertIsNone(prepared)
                self.assertIn("cannot fit", why)
                scale.assert_not_called()

    def test_a_compositor_that_returns_wrong_dimensions_is_refused(self):
        self.path.write_bytes(_tiny_png(256, 400))
        prepared, why = osal.prepare_image(
            str(self.path), scaler=lambda *args: (str(self.path), ""))
        self.assertIsNone(prepared)
        self.assertIn("dimensions", why)

    def slot(self, payload):
        requests = []

        class Recording:
            def call_service(self, url, questions, **kwargs):
                requests.append(kwargs)
                return transport.Response(200, "ok", payload)

        slot = VisionExtractor(
            "crop", load_settings(env={"DRIVER_JEV_API_KEY": "test-key",
                                       "DRIVER_JEV_BOX": "1",
                                       "DRIVER_JEV_IMAGES": "1"}),
            budget=Budget(1), audit=MemoryAuditLog(),
            transport_module=Recording(), captor=lambda: (str(self.path), ""),
            describer=lambda: (None, "no fallback description"))
        return slot, requests

    def test_locate_prepares_narrow_crop_and_returns_original_crop_coordinates(self):
        original = _tiny_png(20, 1000)
        self.path.write_bytes(original)
        # 20-pixel crop drawn at x=130 on a 280-pixel canvas.
        slot, requests = self.slot({
            "answers": {
                "present::marker": {"type": "noul", "noul": .99},
                "box::marker": {"type": "box", "confidence": .99,
                                "box": {"left": 135 / 280, "right": 145 / 280,
                                        "top": .25, "bottom": .75}},
            }, "usage": {"input_tokens": 600},
        })
        with patch.object(osal, "_scale_image", self.compose):
            result, = slot.locate(["marker"])
            repeated, = slot.locate(["marker"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(result.box, repeated.box)
        self.assertTrue(result.located, result.reason)
        for edge, value in {"left": .25, "right": .75,
                            "top": .25, "bottom": .75}.items():
            self.assertAlmostEqual(result.box[edge], value)
        data = base64.b64decode(next(iter(requests[0]["assets"].values()))[
            "data_base64"])
        self.assertEqual(struct.unpack(">II", data[16:24]), (280, 1000))
        repeated_data = base64.b64decode(next(iter(requests[1]["assets"].values()))[
            "data_base64"])
        self.assertEqual(data, repeated_data)

    def test_locate_refuses_failed_preparation_before_dispatch(self):
        self.path.write_bytes(_tiny_png(256, 400))
        slot, requests = self.slot({})
        with patch.object(osal, "_scale_image", return_value=(None, "failed")):
            result, = slot.locate(["marker"])
        self.assertFalse(result.located)
        self.assertIn("failed", result.reason)
        self.assertEqual(requests, [])
        self.assertEqual(slot.budget.spent, 0)

    def test_padding_only_location_is_not_reported_as_a_crop_location(self):
        self.path.write_bytes(_tiny_png(20, 1000))
        slot, _ = self.slot({"answers": {
            "present::marker": {"type": "noul", "noul": .99},
            "box::marker": {"type": "box", "confidence": .99,
                            "box": {"left": 0, "right": .1,
                                    "top": .25, "bottom": .75}},
        }, "usage": {"input_tokens": 600}})
        with patch.object(osal, "_scale_image", self.compose):
            result, = slot.locate(["marker"])
        self.assertFalse(result.located)
        self.assertEqual(result.reason, "insufficient_agreement")

    def test_truncated_png_refuses_through_prepare_extract_and_locate(self):
        import zlib

        header = (osal.PNG_SIGNATURE + struct.pack(">I", 13) + b"IHDR"
                  + struct.pack(">II", 280, 280))
        self.assertEqual(len(header), 24)
        valid = _tiny_png(280, 280)
        # Complete, checksummed chunks with a genuinely undecodable stream.
        bad = b"not-zlib"
        corrupt = (valid[:33] + struct.pack(">I", len(bad)) + b"IDAT" + bad
                   + struct.pack(">I", zlib.crc32(b"IDAT" + bad)) + valid[-12:])
        for original in (header, corrupt):
            with self.subTest(size=len(original)):
                self.path.write_bytes(original)
                prepared, why = osal.prepare_image(str(self.path))
                self.assertIsNone(prepared)
                self.assertIn("PNG", why)
                slot, requests = self.slot({})
                result, = slot.locate(["marker"])
                self.assertFalse(result.located)
                self.assertIn("PNG", result.reason)
                extracted = slot.extract(SCREEN_SCHEMA)
                self.assertFalse(extracted.ok)
                self.assertEqual(requests, [])
                self.assertEqual(slot.budget.spent, 0)
                self.assertEqual(slot.budget.open_reservations(), [])
                self.assertEqual(self.path.read_bytes(), original)

    def test_preparation_uses_independent_snapshots_and_cleans_them_on_refusal(self):
        original = _tiny_png(20, 1000)
        self.path.write_bytes(original)
        snapshots = []

        def fail(path, *sizes):
            snapshots.append(Path(path))
            return None, "cannot decode"

        for _ in range(2):
            prepared, why = osal.prepare_image(str(self.path), scaler=fail)
            self.assertIsNone(prepared)
            self.assertIn("cannot decode", why)
        self.assertNotEqual(snapshots[0], snapshots[1])
        self.assertTrue(all(not p.exists() for p in snapshots))
        self.assertEqual(self.path.read_bytes(), original)

    def test_interleaved_locates_keep_their_own_geometry(self):
        original = _tiny_png(20, 1000)
        self.path.write_bytes(original)
        full = self.path.with_name("full.png")
        full.write_bytes(_tiny_png(280, 1000))
        nested = []
        calls = []
        slot = None
        payload = {"answers": {
            "present::marker": {"type": "noul", "noul": .99},
            "box::marker": {"type": "box", "confidence": .99,
                            "box": {"left": 135 / 280, "right": 145 / 280,
                                    "top": .25, "bottom": .75}},
        }, "usage": {"input_tokens": 600}}

        class Interleaving:
            def call_service(self, *args, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    slot._captor = lambda: (str(full), "")
                    nested.extend(slot.locate(["marker"]))
                return transport.Response(200, "ok", payload)

        slot, _ = self.slot(payload)
        slot._transport = Interleaving()
        with patch.object(osal, "_scale_image", self.compose):
            outer, = slot.locate(["marker"])
        self.assertEqual(len(calls), 4)
        self.assertAlmostEqual(outer.box["left"], .25)
        self.assertAlmostEqual(outer.box["right"], .75)
        self.assertAlmostEqual(nested[0].box["left"], 135 / 280)
        self.assertAlmostEqual(nested[0].box["right"], 145 / 280)
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
