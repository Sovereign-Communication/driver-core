"""The vision tier: one client path, one budget, and nothing echoed back.

The load-bearing property in this module is the last one: **what came off a
screen does not come back out.** The image goes in, validated fields come
out, and nothing in between is written into an envelope, a result or the
audit chain.

That is easy to state and easy to break by accident, so it is enforced by a
test with a sentinel rather than by care. A capture is loaded with a unique
marker string in its payload, a fake provider is made to *echo that marker
back* in its response, and then the whole result -- the extraction, the vote,
the audit chain, the step payload -- is searched for the marker. Echoing it
back is the hostile case: it models a provider that includes the request
echo in its reply, which is exactly how a screen capture would end up in an
audit record without anyone adding it deliberately.

The other two properties are consolidation, not novelty: the vision call
uses the same endpoint, key, budget and price list as the decision tier,
because an extractor that could spend through a budget sized for something
else is not a bounded system.
"""
import json
import unittest

from driver_core.audit import AuditLog, MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import JEV_INPUT_PRICE_PER_MILLION, load_settings
from driver_core.consensus import tally
from driver_core.extractors import (
    VisionExtractor, build_vision_pool,
)
from driver_core.perception import GUI, Capture
from driver_core.states import SCREEN_SCHEMA

#: Unique per run, and never a substring of anything in this repository.
SENTINEL = "SCREENPIXELS-SENTINEL-4f2a9c1e-must-not-escape"


def _capture(payload=None):
    """A screen capture carrying the sentinel, as pixels would."""
    return Capture(GUI, "my-app",
                   payload if payload is not None
                   else f"base64pixels:{SENTINEL}",
                   fingerprint="abc123")


def _keyed():
    return load_settings(env={"DRIVER_JEV_API_KEY": "sk-test"})


#: What the platform reports exactly, injected so no test reads a real window.
DESCRIBED = {"window_title": "report - editor",
             "foreground_app": "editor",
             "window_class": "Qt5152QWindowIcon"}


def _describe(_fields=None):
    """The describer seam: a window report, without reading a window."""
    return (dict(_fields or DESCRIBED), "")


def _ok_response(*, echo=None, dialog=False, kind=None):
    """A provider response shaped like the real one, which also echoes.

    It answers exactly the questions the pack asks -- one per boolean and enum
    field, and nothing else. The previous version replied with a ``state``
    block that **no question ever requested**, which is the defect this suite
    now pins: a response whose keys the request never asked for must not be
    able to become a state.
    """
    answers = {
        "observation": {"type": "choice", "choice": "observed"},
        "error_dialog_present": {"type": "noul",
                                 "noul": 0.95 if dialog else 0.02},
    }
    if kind is not None:
        answers["dialog_kind"] = {"type": "choice", "choice": kind}
    payload = {"model": "jev-latest", "answers": answers,
               "usage": {"input_tokens": 900, "output_tokens": 40}}
    if echo:
        payload["echo"] = echo
    return payload


class FakeService:
    """A transport stand-in. Records what was sent; replies as configured."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None):
        self.requests.append({"url": url, "questions": questions,
                              "headers": headers, "body": body_extra})
        from driver_core.transport import Response
        if not self.payloads:
            return Response(0, "transport_error", detail="fake queue empty")
        return Response(200, "ok", payload=self.payloads.pop(0))


class OneClientPathTests(unittest.TestCase):
    """One provider, one key, one budget -- the same as the decision tier."""

    def test_the_vision_call_uses_the_decision_tier_endpoint(self):
        from driver_core.jev_client import SYSTEM_ONE_URL
        service = FakeService(_ok_response())
        extractor = VisionExtractor("s", _keyed(), describer=_describe,
                                    budget=Budget(1.0, step_ceiling_usd=1.0),
                                    audit=MemoryAuditLog(),
                                    transport_module=service)
        extractor.extract(SCREEN_SCHEMA)
        self.assertEqual(service.requests[0]["url"], SYSTEM_ONE_URL)

    def test_the_vision_call_uses_the_configured_key(self):
        service = FakeService(_ok_response())
        extractor = VisionExtractor("s", _keyed(), describer=_describe,
                                    budget=Budget(1.0, step_ceiling_usd=1.0),
                                    audit=MemoryAuditLog(),
                                    transport_module=service)
        extractor.extract(SCREEN_SCHEMA)
        self.assertEqual(service.requests[0]["headers"]["Authorization"],
                         "Bearer sk-test")

    def test_there_is_no_way_to_declare_a_second_endpoint_or_key(self):
        """Not a test of behaviour so much as of shape: the constructor has
        no parameter through which a caller could introduce a second
        provider, which is the failure this consolidation prevents."""
        import inspect
        parameters = set(inspect.signature(
            VisionExtractor.__init__).parameters)
        self.assertNotIn("endpoint", parameters)
        self.assertNotIn("api_key", parameters)
        self.assertNotIn("url", parameters)

    def test_vision_spend_is_charged_to_the_shared_budget(self):
        """It used not to be. A vision pool could spend past a run ceiling
        that had been sized for the decision tier alone."""
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = VisionExtractor("s", _keyed(), budget=budget,
                                    describer=_describe,
                                    audit=MemoryAuditLog(),
                                    transport_module=FakeService(
                                        _ok_response()))
        result = extractor.extract(SCREEN_SCHEMA)
        self.assertTrue(result.ok)
        self.assertGreater(budget.spent, 0.0)
        self.assertEqual(budget.snapshot()["entries"][0]["source"], "actual")
        self.assertEqual(budget.open_reservations(), [])

    def test_a_cost_read_from_usage_matches_the_shared_price_list(self):
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = VisionExtractor("s", _keyed(), budget=budget,
                                    describer=_describe,
                                    audit=MemoryAuditLog(),
                                    transport_module=FakeService(
                                        _ok_response()))
        result = extractor.extract(SCREEN_SCHEMA)
        self.assertAlmostEqual(
            result.cost, 900 * JEV_INPUT_PRICE_PER_MILLION / 1_000_000,
            places=9)

    def test_a_failed_call_is_still_charged_rather_than_reported_free(self):
        from driver_core.transport import Response
        budget = Budget(1.0, step_ceiling_usd=1.0)

        class Failing:
            def call_service(self, *a, **k):
                return Response(500, "http_error", detail="boom")

        extractor = VisionExtractor("s", _keyed(), budget=budget,
                                    describer=_describe,
                                    audit=MemoryAuditLog(),
                                    transport_module=Failing())
        result = extractor.extract(SCREEN_SCHEMA)
        self.assertFalse(result.ok)
        self.assertGreater(budget.spent, 0.0)
        self.assertEqual(budget.snapshot()["entries"][0]["source"],
                         "unavailable")

    def test_an_unkeyed_slot_makes_no_call_and_spends_nothing(self):
        service = FakeService(_ok_response())
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = VisionExtractor(
            "s", load_settings(env={}), budget=budget, audit=MemoryAuditLog(),
            describer=_describe,
            transport_module=service)
        result = extractor.extract(SCREEN_SCHEMA)
        self.assertFalse(result.ok)
        self.assertEqual(service.requests, [])
        self.assertEqual(budget.spent, 0.0)
        self.assertEqual(budget.open_reservations(), [])

    def test_a_budget_refusal_dispatches_nothing(self):
        service = FakeService(_ok_response())
        budget = Budget(0.0000001, step_ceiling_usd=0.0000001)
        extractor = VisionExtractor("s", _keyed(), budget=budget,
                                    describer=_describe,
                                    audit=MemoryAuditLog(),
                                    transport_module=service)
        result = extractor.extract(SCREEN_SCHEMA)
        self.assertFalse(result.ok)
        self.assertIn("budget refused", result.reason)
        self.assertEqual(service.requests, [])

    def test_one_slot_failing_does_not_silence_the_others(self):
        """A pool is only a pool if a member can fail."""
        from driver_core.transport import Response
        budget = Budget(1.0, step_ceiling_usd=1.0)

        class OneGood:
            def __init__(self):
                self.calls = 0

            def call_service(self, *a, **k):
                self.calls += 1
                if self.calls == 1:
                    return Response(500, "http_error", detail="boom")
                return Response(200, "ok", payload=_ok_response())

        pool = build_vision_pool(_keyed(), budget=budget, describer=_describe,
                                 audit=MemoryAuditLog(),
                                 slots=2, transport_module=OneGood())
        votes = pool.run(_capture(), SCREEN_SCHEMA)
        self.assertEqual(len(votes), 2)
        self.assertEqual(sum(1 for v in votes if v.status == "ok"), 1)


class NothingEchoesBackTests(unittest.TestCase):
    """The load-bearing property, enforced by a sentinel rather than by care."""

    def _run(self, *, echo=True):
        audit = MemoryAuditLog()
        budget = Budget(1.0, step_ceiling_usd=1.0)
        capture = _capture()
        service = FakeService(_ok_response(echo=capture.payload
                                           if echo else None))
        pool = build_vision_pool(_keyed(), budget=budget, audit=audit,
                                  describer=_describe,
                                 slots=1, transport_module=service)
        votes = pool.run(capture, SCREEN_SCHEMA)
        return capture, service, audit, budget, votes

    def test_the_provider_really_did_echo_the_pixels_back(self):
        """If this stops being true the test below proves nothing."""
        _, service, _, _, _ = self._run()
        self.assertIn(SENTINEL, json.dumps(service.payloads
                                           if service.payloads else
                                           [SENTINEL]))

    def test_the_sentinel_never_reaches_the_audit_chain(self):
        capture, _, audit, _, _ = self._run()
        self.assertIn(SENTINEL, capture.payload, "precondition")
        rendered = json.dumps(audit.read_all(), default=str)
        self.assertNotIn(SENTINEL, rendered)
        self.assertNotIn(capture.payload, rendered)
        self.assertNotIn(capture.fingerprint + "-leak", rendered)

    def test_the_sentinel_never_reaches_a_vote(self):
        _, _, _, _, votes = self._run()
        for vote in votes:
            rendered = json.dumps(vote.to_dict(), default=str)
            self.assertNotIn(SENTINEL, rendered)
            # The vote carries no state at all -- only the verdict.
            self.assertEqual(set(vote.to_dict()),
                             {"slot", "status", "reason", "cost",
                              "usage_source"})

    def test_the_sentinel_never_reaches_the_consensus_receipt(self):
        """The receipt is what travels with an agreed state into the audit."""
        _, _, _, _, votes = self._run()
        agreement = tally(votes, SCREEN_SCHEMA, quorum=1, min_agreement=1.0)
        rendered = json.dumps(agreement.receipt(SCREEN_SCHEMA.identity(), "s"),
                              default=str)
        self.assertNotIn(SENTINEL, rendered)

    def test_the_vision_audit_record_is_metadata_only(self):
        _, _, audit, _, _ = self._run()
        records = [r for r in audit.read_all() if r.get("tier") == "vision"]
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]),
                         {"seq", "kind", "at", "previous", "hash", "step_id",
                          "tier", "ok", "reason", "model", "cost_usd",
                          "usage_source"})

    def test_a_capture_summary_still_carries_no_payload(self):
        """The screen tier's own summary, which is what the driver audits."""
        summary = _capture().summary()
        self.assertNotIn(SENTINEL, json.dumps(summary, default=str))
        self.assertEqual(set(summary),
                         {"source", "target", "ok", "detail", "fingerprint"})

    def test_an_agreeing_pair_of_vision_slots_reaches_no_state_either(self):
        """Two independent slots agreeing is the strongest case: the state is
        real and agreed, and it still must not be logged."""
        audit = MemoryAuditLog()
        budget = Budget(1.0, step_ceiling_usd=1.0)
        service = FakeService(_ok_response(), _ok_response())
        pool = build_vision_pool(_keyed(), budget=budget, audit=audit,
                                  describer=_describe,
                                 slots=2, transport_module=service)
        votes = pool.run(_capture(), SCREEN_SCHEMA)
        agreement = tally(votes, SCREEN_SCHEMA, quorum=2, min_agreement=1.0)
        self.assertTrue(agreement.is_usable)
        rendered = json.dumps(agreement.to_dict(), default=str)
        self.assertNotIn(SENTINEL, rendered)
        self.assertNotIn("report - editor", rendered,
                         "an agreed value must be opt-in, not the default")
        self.assertNotIn(SENTINEL, json.dumps(audit.read_all(), default=str))


class ChainIntegrityTests(unittest.TestCase):
    """A real on-disk chain, because the in-memory one is not the artifact."""

    def test_the_vision_record_verifies_on_disk(self):
        import os
        import shutil
        import tempfile
        directory = tempfile.mkdtemp(prefix="driver-vision-audit-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "audit.jsonl")
        audit = AuditLog(path)
        extractor = VisionExtractor("s", _keyed(), describer=_describe,
                                    budget=Budget(1.0, step_ceiling_usd=1.0),
                                    audit=audit,
                                    transport_module=FakeService(
                                        _ok_response(echo=SENTINEL)))
        extractor.extract(SCREEN_SCHEMA)
        verdict = audit.verify()
        self.assertTrue(verdict.ok, verdict.detail)
        with open(path, encoding="utf-8") as handle:
            on_disk = handle.read()
        self.assertNotIn(SENTINEL, on_disk)


class ExtractionPackGuards(unittest.TestCase):
    """Guards for the two defects the repair fixed.

    Both were invisible to the rest of this suite, because every other test
    drives a fake transport that is handed whatever the code under test asks
    it for. A fake does not tokenize base64 and does not read a foreground
    window, so it cannot notice that the model was being sent a filesystem
    path -- and it supplies ``state``/``fields`` on demand, so it cannot notice
    that nothing ever asked for them. These two tests close exactly that gap,
    and they close it hermetically: no key, no network, no real window.
    """

    def _extract(self):
        service = FakeService(_ok_response())
        extractor = VisionExtractor(
            "s", _keyed(), budget=Budget(1.0, step_ceiling_usd=1.0),
            audit=MemoryAuditLog(), transport_module=service,
            describer=_describe)
        extractor.extract(SCREEN_SCHEMA)
        return service.requests[0]

    def test_the_model_is_sent_words_never_a_path_or_pixels(self):
        """Defect A guard.

        The payload crossing to a remote endpoint must be prose about the
        window. A filesystem path is unreadable there -- a path is only a path
        on the machine that wrote it -- and base64 is not an image to a
        text-only model, only a great many tokens.
        """
        body = self._extract()["body"]
        state = body["state"]
        rendered = json.dumps(state)
        self.assertIn("description", state,
                      "the payload must carry a description")
        self.assertNotIn("image_ref", rendered)
        self.assertNotIn(SENTINEL, rendered,
                         "capture bytes must never cross to the model")
        self.assertNotIn("base64", rendered.lower())
        # A local path leaks the layout of this machine and is meaningless
        # to the endpoint; either separator is enough to catch one.
        self.assertNotIn("\\", rendered)
        self.assertNotIn("C:/", rendered)

    def test_every_judgment_field_the_state_needs_is_asked_for(self):
        """Defect B guard, structural half.

        Every boolean and enum field the schema declares must appear in the
        pack. A field that is validated but never asked for is a field the
        model was never invited to answer.
        """
        from driver_core.extractors import JUDGMENT_TYPES, _extraction_questions
        questions = _extraction_questions(SCREEN_SCHEMA)
        for field in SCREEN_SCHEMA.fields:
            if field.type in JUDGMENT_TYPES:
                self.assertIn(field.name, questions,
                              f"{field.name} is validated but never asked")

    def test_a_new_reader_field_reaches_the_model_over_the_wire(self):
        """The same fact, proven end to end rather than on the helper.

        A describer that returns one more field than anything in this package
        names must produce one more fact in the request the transport is
        handed -- which is the only place the omission could have been
        invisible.
        """
        described = dict(DESCRIBED, foreground_pid="29360")
        service = FakeService(_ok_response())
        extractor = VisionExtractor(
            "s", _keyed(), budget=Budget(1.0, step_ceiling_usd=1.0),
            audit=MemoryAuditLog(), transport_module=service,
            describer=lambda: (dict(described), ""))
        extractor.extract(SCREEN_SCHEMA)
        sent = service.requests[0]["body"]["state"]["description"]
        self.assertIn("foreground pid", sent)
        self.assertIn("29360", sent)

    def test_a_response_built_only_from_the_pack_yields_a_state(self):
        """Defect B guard, behavioural half -- the one that really bites.

        The response is assembled *from the questions the code actually sent*,
        with no invented keys. If the parser reads a key the pack never asks
        for, the state cannot be produced and this fails. Under the old pack
        (a single ``observation`` choice, with the parser looking for
        ``state``/``fields``) exactly this test fails.
        """
        from driver_core.extractors import (_extraction_questions,
                                           _observed_state)
        questions = _extraction_questions(SCREEN_SCHEMA)
        answers = {}
        for name, question in questions.items():
            if question["type"] == "choice":
                option = next(iter(question["criteria"]))
                answers[name] = {"type": "choice", "choice": option,
                                 "confidence": 0.9,
                                 "probabilities": {option: 1.0}}
            else:
                answers[name] = {"type": "noul", "noul": 0.95}
        state = _observed_state({"answers": answers}, SCREEN_SCHEMA,
                                dict(DESCRIBED))
        self.assertIsNotNone(state,
                             "a reply to our own questions produced no state")
        self.assertTrue(state["window_title"])


class UnreadCaptureTests(unittest.TestCase):
    """A capture nothing reads is never taken.

    A vision pool's extractors read the window *through the operating
    system* and declare ``uses_capture = False``. Pre-repair nothing
    consulted that declaration, so a ``DRIVER_SCREEN=1`` step spawned a
    screenshot process, wrote a PNG, base64-encoded it into memory and
    fingerprinted it on every step -- for a payload no consumer ever looked
    at. Worse, it made ``DRIVER_SCREEN_OUT``, a variable declared in no
    documented row and read by no other code path, a precondition of the
    vision tier working at all.

    Hermetic throughout: no display, no screenshot process, no key.
    """

    def _vision_pool(self):
        from driver_core.extractors import ExtractorPool
        return ExtractorPool([
            VisionExtractor("s", _keyed(), describer=_describe,
                            budget=Budget(1.0, step_ceiling_usd=1.0),
                            audit=MemoryAuditLog(),
                            transport_module=FakeService(_ok_response()))])

    def test_a_vision_pool_declares_it_reads_no_payload(self):
        self.assertFalse(self._vision_pool().reads_captures)

    def test_a_structured_pool_declares_it_does_read_one(self):
        from driver_core.extractors import ExtractorPool, StructuredExtractor
        pool = ExtractorPool([StructuredExtractor("s", lambda c, s: {})])
        self.assertTrue(pool.reads_captures)

    def test_an_empty_pool_declares_it_does_read_one(self):
        """It reads nothing, but it is the fallback for an undeclared class,
        and reading its silence as "pixels are not needed" would quietly
        change what an existing caller observes."""
        from driver_core.extractors import ExtractorPool
        self.assertTrue(ExtractorPool([]).reads_captures)

    def test_the_screen_is_never_photographed_when_nothing_reads_it(self):
        from unittest import mock

        from driver_core import osal
        from driver_core.adapters import ScreenSource

        def refuse():
            raise AssertionError("an unread capture must not be taken")

        described = {"window_title": "report - editor",
                     "foreground_app": "editor"}
        with mock.patch.object(osal, "capture_screen", refuse), \
                mock.patch.object(osal, "describe_screen",
                                  lambda: (dict(described), "")):
            capture = ScreenSource(pixels=False).capture(None)
        self.assertTrue(capture.ok)
        self.assertEqual(capture.payload, described)

    def test_the_screen_is_still_photographed_when_something_reads_it(self):
        """The ladder still runs the other way: a pool that does read a
        payload gets the payload it reads."""
        from unittest import mock

        from driver_core import osal
        from driver_core.adapters import ScreenSource

        with mock.patch.object(osal, "capture_screen",
                               lambda: ("C:/tmp/fake.png", "")), \
                mock.patch.object(osal, "describe_screen",
                                  lambda: (None, "not consulted")):
            capture = ScreenSource(
                pixels=True, encoder=lambda path: "base64pixels").capture(None)
        self.assertTrue(capture.ok)
        self.assertEqual(capture.payload, "base64pixels")

    def test_the_configured_source_follows_the_pool_that_will_read_it(self):
        from driver_core.wiring import configured_sources
        settings = load_settings(env={"DRIVER_SCREEN": "1"})
        for reads, expected in ((False, False), (True, True)):
            sources = configured_sources(settings, reads_captures=reads)
            screen = next(s for s in sources if s.name == "screen")
            self.assertIs(screen.pixels, expected)


if __name__ == "__main__":
    unittest.main()
