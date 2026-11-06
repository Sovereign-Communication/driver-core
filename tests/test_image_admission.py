"""Image admission reserves the request cap, independent of PNG compression."""
import base64
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from driver_core import transport
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget, estimate_call_cost
from driver_core.config import JEV_INPUT_PRICE_PER_MILLION, load_settings
from driver_core.errors import BudgetRefused
from driver_core.extractors import VisionExtractor
from driver_core.extractors.sizing import IMAGE_REQUEST_TOKEN_BOUND
from driver_core.states import SCREEN_SCHEMA
from tests.test_image_plumbing import _prepare_fixture


def compressed_png():
    """The valid 280x280 one-bit PNG that under-reserved in the live probe."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 280, 280, 1, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff" * 35) * 280))
            + chunk(b"IEND", b""))


class ImageAdmissionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "screen.png"
        self.path.write_bytes(compressed_png())
        self.assets = {"fixture-ref": {
            "type": "image", "format": "png",
            "data_base64": base64.b64encode(self.path.read_bytes()).decode(),
        }}
        self.questions = {"white": transport.noul("Is `screen` white?")}
        self.cap = estimate_call_cost(
            IMAGE_REQUEST_TOKEN_BOUND,
            price_per_million=JEV_INPUT_PRICE_PER_MILLION)
        self.payload = {
            "model": "jev-1.14.0",
            "answers": {
                "observation": {"type": "choice", "choice": "observed"},
                "error_dialog_present": {"type": "noul", "noul": 0.01},
                "white": {"type": "noul", "noul": 0.97},
            },
            "usage": {"input_tokens": 513, "output_tokens": 20},
        }

    def run_path(self, path, budget, response=None):
        audit = MemoryAuditLog()
        slot = VisionExtractor(
            "admission", load_settings(env={
                "DRIVER_JEV_API_KEY": "test-key",
                "DRIVER_JEV_IMAGES": "1", "DRIVER_JEV_BOX": "1",
            }), budget=budget, audit=audit,
            captor=lambda: (str(self.path), ""),
            preparer=_prepare_fixture,
            describer=lambda: ({"window_title": "fixture",
                                "foreground_app": "fixture"}, ""))
        with patch.object(transport, "request", return_value=(
                response or transport.Response(200, "ok", self.payload))) as wire:
            if path == "extract":
                result = slot.extract(SCREEN_SCHEMA)
            else:
                try:
                    result = slot._ask_locate(
                        self.questions, self.assets, "fixture-ref", "jev-1.14.0")
                except (BudgetRefused, RuntimeError) as exc:
                    result = exc
        return result, wire, audit

    def assert_refused(self, path, budget):
        result, wire, audit = self.run_path(path, budget)
        wire.assert_not_called()
        if path == "extract":
            self.assertFalse(result.ok)
            self.assertIn("budget refused", result.reason)
        else:
            self.assertIsInstance(result, BudgetRefused)
        self.assertEqual(budget.open_reservations(), [])
        self.assertEqual(len(audit.read_all()), 1)
        self.assertEqual(audit.read_all()[0]["cost_usd"], 0)
        self.assertTrue(audit.verify().ok)

    def test_compressed_png_cannot_enter_on_its_old_byte_estimate(self):
        self.assertLess(len(self.assets["fixture-ref"]["data_base64"]), 513)
        for path in ("extract", "locate"):
            with self.subTest(path=path):
                budget = Budget(171 * JEV_INPUT_PRICE_PER_MILLION / 1_000_000)
                self.assert_refused(path, budget)
                self.assertEqual(budget.spent, 0)

    def test_step_ceiling_must_fit_the_entire_request_bound(self):
        for path in ("extract", "locate"):
            with self.subTest(path=path):
                self.assert_refused(path, Budget(
                    self.cap * 2, step_ceiling_usd=self.cap - 1e-9))

    def test_remaining_run_budget_must_fit_the_bound(self):
        for path in ("extract", "locate"):
            with self.subTest(path=path):
                budget = Budget(self.cap, step_ceiling_usd=self.cap)
                prior = budget.reserve(self.cap / 2)
                prior.settle(self.cap / 2)
                self.assert_refused(path, budget)
                self.assertEqual(budget.spent, self.cap / 2)

    def test_exact_bound_admits_both_paths_and_settles_actual_usage(self):
        for path in ("extract", "locate"):
            with self.subTest(path=path):
                budget = Budget(self.cap, step_ceiling_usd=self.cap)
                result, wire, audit = self.run_path(path, budget)
                self.assertTrue(result.ok if path == "extract" else result)
                wire.assert_called_once()
                sent = wire.call_args.kwargs
                self.assertEqual(sent["headers"][transport.IMAGES_HEADER], "true")
                self.assertNotIn("typesafe-danger-zone-long-context",
                                 {h.lower() for h in sent["headers"]})
                entry, = budget.snapshot()["entries"]
                self.assertAlmostEqual(entry["estimated_usd"], self.cap, places=9)
                actual = 513 * JEV_INPUT_PRICE_PER_MILLION / 1_000_000
                self.assertAlmostEqual(budget.spent, actual)
                self.assertEqual(entry["source"], "actual")
                self.assertEqual(budget.open_reservations(), [])
                record, = audit.read_all()
                self.assertAlmostEqual(record["cost_usd"], actual, places=9)
                self.assertTrue(audit.verify().ok)

    def test_usage_at_the_contract_cap_is_not_clipped(self):
        self.payload["usage"]["input_tokens"] = IMAGE_REQUEST_TOKEN_BOUND
        for path in ("extract", "locate"):
            with self.subTest(path=path):
                budget = Budget(self.cap)
                self.run_path(path, budget)
                self.assertEqual(budget.spent, self.cap)

    def test_failed_or_unmeasured_calls_charge_the_full_reservation(self):
        unmeasured = dict(self.payload)
        unmeasured.pop("usage")
        for response in (transport.Response(200, "ok", unmeasured),
                         transport.Response(400, "http_error")):
            for path in ("extract", "locate"):
                with self.subTest(path=path, status=response.status):
                    budget = Budget(self.cap)
                    _, wire, audit = self.run_path(path, budget, response)
                    wire.assert_called_once()
                    self.assertEqual(budget.spent, self.cap)
                    self.assertEqual(budget.open_reservations(), [])
                    self.assertEqual(len(audit.read_all()), 1)
                    self.assertTrue(audit.verify().ok)

    def test_provider_overcharge_remains_visible_in_actual_settlement(self):
        # An upstream contract violation must not be concealed in the ledger.
        self.payload["usage"]["input_tokens"] = IMAGE_REQUEST_TOKEN_BOUND + 1
        budget = Budget(self.cap)
        self.run_path("locate", budget)
        self.assertGreater(budget.spent, self.cap)
        self.assertEqual(budget.snapshot()["entries"][0]["source"], "actual")
        self.assertEqual(budget.open_reservations(), [])


if __name__ == "__main__":
    unittest.main()
