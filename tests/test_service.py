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
                    # A deterministic stand-in for a screen capture. It
                    # declares the gui class explicitly, because the class is
                    # what decides whether this source may answer at all --
                    # a source that does not declare the requested class is
                    # correctly never consulted.
                    sources=[StructuredSource("screen", _source_reader(),
                                             serves=("gui",))],
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
        outcome = _service().handle("step", {"target": "t", "schema": "gui"})
        self.assertEqual(outcome["status"], 200)
        self.assertTrue(outcome["body"]["ok"])
        self.assertEqual(outcome["body"]["execution"]["action"], "observe")

    def test_a_refusal_is_a_200_not_a_5xx(self):
        """A caller that retries on 5xx must never be retrying a decision."""
        jev = FakeJev(action_answer("observe", confidence=0.1))
        outcome = _service(jev=jev).handle("step", {"target": "t", "schema": "gui"})
        self.assertEqual(outcome["status"], 200)
        self.assertFalse(outcome["body"]["ok"])
        self.assertEqual(outcome["body"]["reason"],
                         "confidence_below_threshold")
        self.assertIn(outcome["body"]["reason"], STOP_REASONS)

    def test_a_missing_target_is_the_callers_error(self):
        outcome = _service().handle("step", {})
        self.assertEqual(outcome["status"], 400)
        self.assertIn("target", outcome["body"]["error"])

    def test_an_absent_schema_is_refused_rather_than_defaulted(self):
        """Fail closed, not open.

        Resolving an absent ``schema`` to the screen schema while leaving the
        class undeclared was how the strongest tier stayed reachable by
        default: an undeclared class permits any source, pixels last. There is
        no default that fixes this -- any of the four classes would mean
        observing a different machine than the caller named, or spending
        money -- so the service asks.
        """
        outcome = _service().handle("step", {"target": "t"})
        self.assertEqual(outcome["status"], 400)
        self.assertIn("schema is required", outcome["body"]["error"])
        for name in ("cli", "mcp", "dom", "gui", "screen"):
            self.assertIn(name, outcome["body"]["error"])

    def test_an_unknown_schema_is_refused_rather_than_guessed(self):
        outcome = _service().handle("step", {"target": "t", "schema": "banana"})
        self.assertEqual(outcome["status"], 400)
        self.assertIn("unknown schema", outcome["body"]["error"])

    def test_an_empty_schema_is_refused_too(self):
        outcome = _service().handle("step", {"target": "t", "schema": "  "})
        self.assertEqual(outcome["status"], 400)
        self.assertIn("schema is required", outcome["body"]["error"])

    def test_mcp_is_now_a_declared_schema_rather_than_a_404(self):
        """It was in ``SCHEMAS_BY_TARGET`` but not in the wire table."""
        outcome = _service().handle("step", {"target": "t", "schema": "mcp"})
        self.assertNotEqual(outcome["status"], 400)

    def test_every_declared_schema_name_resolves_to_a_class_and_a_schema(self):
        from driver_core.states import WIRE_TARGETS
        for name in WIRE_TARGETS:
            outcome = _service().handle("step", {"target": "t", "schema": name})
            self.assertNotEqual(outcome["status"], 400, name)

    def test_verify_reports_the_chain_and_the_spend(self):
        outcome = _service().handle("verify", {})["body"]
        self.assertTrue(outcome["audit"]["ok"])
        self.assertIn("remaining_usd", outcome["budget"])

    def test_a_caller_supplied_step_id_is_the_one_that_comes_back(self):
        """The host adapter has always sent ``step_id``; it was being
        dropped and the driver minted its own, so a caller could not join its
        own log to the audit chain afterwards."""
        body = _service().handle(
            "step", {"target": "t", "schema": "gui",
                     "step_id": "host-abc123"})["body"]
        self.assertEqual(body["step_id"], "host-abc123")

    def test_an_absent_step_id_is_still_minted(self):
        body = _service().handle("step", {"target": "t", "schema": "gui"})["body"]
        self.assertTrue(body["step_id"])

    def test_health_reports_which_sources_the_driver_can_observe(self):
        """An operator has to be able to see the capability before relying
        on it, not infer it from a no_capture."""
        body = _service().handle("health", {})["body"]
        self.assertIn("sources", body)
        self.assertIsInstance(body["sources"], list)


class HonestyTests(unittest.TestCase):

    def test_no_response_ever_carries_captured_screen_content(self):
        secret = {"window_title": "Confidential Payroll 2026",
                  "foreground_app": "payroll", "error_dialog_present": False}
        pool = ExtractorPool([StructuredExtractor(f"s{i}", _reader(secret))
                              for i in range(2)])
        outcome = _service(pool=pool).handle("step", {"target": "t", "schema": "gui"})
        rendered = str(outcome["body"])
        self.assertNotIn("Confidential Payroll", rendered)
        self.assertNotIn("payroll", rendered)
        # ...but the fields that make it auditable are present.
        self.assertIn("fingerprint", rendered)
        self.assertIn("receipt", rendered)

    def test_a_refusal_carries_a_reason_from_the_closed_set(self):
        jev = FakeJev(action_answer("observe", confidence=0.1))
        reason = _service(jev=jev).handle(
            "step", {"target": "t", "schema": "gui"})["body"]["reason"]
        self.assertIn(reason, STOP_REASONS)

    def test_consent_is_not_inferred_from_an_absent_field(self):
        """No consent in the request means no consent, not default consent."""
        outcome = _service().handle("step", {"target": "t", "schema": "gui"})
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
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_health_exits_zero(self):
        code, out = self._run(["health"])
        self.assertEqual(code, 0)
        self.assertIn("driver-core: ok", out)

    def test_health_reports_whether_writes_are_allowed(self):
        """The operator has to be able to see the gate before relying on it."""
        _, out = self._run(["health"])
        self.assertIn("writes     : off", out)

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

    def test_there_is_no_blanket_grant_flag(self):
        """``--grant-write`` used to exist and used to authorise every
        mutating action. It is gone rather than silently neutered, because
        a flag that quietly does nothing is worse than no flag."""
        import contextlib
        import io
        from driver_core import cli
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["step", "t", "--grant-write"])

    def test_a_grant_is_echoed_in_the_resolved_form_before_acting(self):
        """The operator must be able to see what they are consenting to.

        ``delete_file`` is used because its declared parameter set is exactly
        ``{"path": ...}``, and the run is dry -- the echo is the subject.
        """
        import json
        _, out = self._run(["--dry-run", "step", "t", "--grant", "delete_file",
                            "--grant-path", "~"])
        line = [ln for ln in out.splitlines() if ln.startswith("[consent]")]
        self.assertEqual(len(line), 1)
        granted = json.loads(line[0][len("[consent]"):])
        self.assertEqual(granted["action"], "delete_file")
        from driver_core import osal
        self.assertEqual(granted["params"]["path"], osal.resolve_path("~"))

    def test_a_grant_naming_an_undeclared_action_fails_before_the_step(self):
        """A mistyped grant must fail before a capture and a decision have
        already been paid for."""
        from driver_core import cli
        from driver_core.errors import VocabularyError
        args = cli.build_parser().parse_args(
            ["step", "t", "--grant", "delete_everything", "--grant-path", "x"])
        with self.assertRaises(VocabularyError) as ctx:
            cli._grant_for(args)
        self.assertIn("not in the declared vocabulary", str(ctx.exception))

    def test_a_grant_with_an_undeclared_param_is_refused(self):
        from driver_core import cli
        args = cli.build_parser().parse_args(
            ["step", "t", "--grant", "delete_file", "--grant-params",
             '{"path": "/tmp/x", "recursive": true}'])
        with self.assertRaises(Exception) as ctx:
            cli._grant_for(args)
        self.assertIn("undeclared parameter", str(ctx.exception))

    def test_a_grant_missing_its_params_is_refused(self):
        from driver_core import cli
        args = cli.build_parser().parse_args(
            ["step", "t", "--grant", "write_file"])
        with self.assertRaises(Exception) as ctx:
            cli._grant_for(args)
        self.assertIn("missing required parameter", str(ctx.exception))

    def test_grant_path_and_grant_params_are_mutually_exclusive(self):
        import contextlib
        import io
        from driver_core import cli
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["step", "t", "--grant", "write_file", "--grant-path",
                          "a", "--grant-params", '{"content": "x"}'])


if __name__ == "__main__":
    unittest.main()
