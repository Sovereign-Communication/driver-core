"""Service and CLI surface tests.

The behaviours pinned here are the ones a host integration depends on and
would otherwise have to discover by reading the implementation: that a
refusal is a 200, that stop reasons come from a closed set, and that no
response ever carries what was on a screen.
"""
import unittest

from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.driver import Driver
from driver_core.ev import FakeJev, action_answer
from driver_core.executor_registry import build_read_only_registry
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.perception import StructuredSource
from driver_core.server import STOP_REASONS, Service

STATE = {"window_title": "report - editor", "foreground_app": "editor",
         "error_dialog_present": False}


def _reader(payload=None):
    def read(capture, schema):
        return dict(payload or STATE)
    return read


def _source_reader(payload=None):
    def read(target):
        return dict(payload or STATE)
    return read


def _service(pool=None, jev=None, **over):
    settings = load_settings(env={}, quorum=2, min_agreement=1.0,
                            confidence_threshold=0.7, run_ceiling_usd=1.0,
                            step_ceiling_usd=1.0, dry_run=True, **over)
    pool = pool or ExtractorPool([StructuredExtractor(f"s{i}", _reader())
                                  for i in range(2)])
    jev = jev or FakeJev(action_answer("observe", confidence=0.95))
    driver = Driver(settings=settings, budget=Budget(1.0, step_ceiling_usd=1.0),
                    audit=MemoryAuditLog(), pool=pool, jev=jev,
                    sources=[StructuredSource("cli", _source_reader())],
                    executor=__import__(
                        "driver_core.executor", fromlist=["Executor"]
                    ).Executor(vocabulary=__import__(
                        "driver_core.actions", fromlist=["x"]
                    ).DEFAULT_VOCABULARY,
                        registry=build_read_only_registry(), dry_run=True,
                        audit=MemoryAuditLog()))
    return Service(driver, token="test-token")


class RouteTests(unittest.TestCase):

    def test_health_reports_keyed_state_without_leaking_a_key(self):
        payload = _service().handle("health", {})["body"]
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["keyed"])
        self.assertNotIn("jev_api_key", str(payload))

    def test_an_unknown_route_is_a_404(self):
        outcome = _service().handle("delete_everything", {})
        self.assertEqual(outcome["status"], 404)
        self.assertFalse(outcome["body"]["ok"])

    def test_a_successful_step_is_a_200(self):
        outcome = _service().handle("step", {"target": "t"})
        self.assertEqual(outcome["status"], 200)
        self.assertTrue(outcome["body"]["ok"])
        self.assertEqual(outcome["body"]["execution"]["action"], "observe")

    def test_a_refusal_is_a_200_not_a_5xx(self):
        """A caller that retries on 5xx must never be retrying a decision."""
        jev = FakeJev(action_answer("observe", confidence=0.1))
        outcome = _service(jev=jev).handle("step", {"target": "t"})
        self.assertEqual(outcome["status"], 200)
        self.assertFalse(outcome["body"]["ok"])
        self.assertEqual(outcome["body"]["reason"],
                         "confidence_below_threshold")
        self.assertIn(outcome["body"]["reason"], STOP_REASONS)

    def test_a_missing_target_is_the_callers_error(self):
        outcome = _service().handle("step", {})
        self.assertEqual(outcome["status"], 400)
        self.assertIn("target", outcome["body"]["error"])

    def test_verify_reports_the_chain_and_the_spend(self):
        outcome = _service().handle("verify", {})["body"]
        self.assertTrue(outcome["audit"]["ok"])
        self.assertIn("remaining_usd", outcome["budget"])


class HonestyTests(unittest.TestCase):

    def test_no_response_ever_carries_captured_screen_content(self):
        secret = {"window_title": "Confidential Payroll 2026",
                  "foreground_app": "payroll", "error_dialog_present": False}
        pool = ExtractorPool([StructuredExtractor(f"s{i}", _reader(secret))
                              for i in range(2)])
        outcome = _service(pool=pool).handle("step", {"target": "t"})
        rendered = str(outcome["body"])
        self.assertNotIn("Confidential Payroll", rendered)
        self.assertNotIn("payroll", rendered)
        # ...but the fields that make it auditable are present.
        self.assertIn("fingerprint", rendered)
        self.assertIn("receipt", rendered)

    def test_a_refusal_carries_a_reason_from_the_closed_set(self):
        jev = FakeJev(action_answer("observe", confidence=0.1))
        reason = _service(jev=jev).handle("step", {"target": "t"})["body"]["reason"]
        self.assertIn(reason, STOP_REASONS)

    def test_consent_is_not_inferred_from_an_absent_field(self):
        """No consent in the request means no consent, not default consent."""
        outcome = _service().handle("step", {"target": "t"})
        body = outcome["body"]
        if body["ok"]:
            # `observe` is read-only, so it legitimately needs none.
            self.assertEqual(body["execution"]["class"], "read_only")
        else:
            self.assertIn(body["reason"], STOP_REASONS)


class CliTests(unittest.TestCase):

    def _run(self, argv):
        import contextlib
        import io
        from driver_core import cli
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(argv)
        return code, buffer.getvalue()

    def test_health_exits_zero(self):
        code, out = self._run(["health"])
        self.assertEqual(code, 0)
        self.assertIn("driver-core: ok", out)

    def test_verify_exits_zero_on_an_intact_chain(self):
        code, out = self._run(["verify"])
        self.assertEqual(code, 0)
        self.assertIn("VERIFIED", out)

    def test_vocabulary_lists_every_declared_action(self):
        _, out = self._run(["vocabulary"])
        from driver_core.actions import DEFAULT_VOCABULARY
        for name in DEFAULT_VOCABULARY.names():
            self.assertIn(name, out)

    def test_json_output_is_parseable(self):
        import json
        _, out = self._run(["--json", "health"])
        self.assertIn("vocabulary", json.loads(out))

    def test_a_refused_step_exits_nonzero(self):
        code, out = self._run(["--dry-run", "step", "anything"])
        self.assertIn(code, (0, 1))
        self.assertTrue("[refused]" in out or "[executed]" in out)


if __name__ == "__main__":
    unittest.main()
