"""Timestamped semantic history, bounded reuse and unchanged guard behavior."""
import os
import tempfile
import unittest
from unittest.mock import patch
from test_image_plumbing import _prepare_fixture

from driver_core import policy
from driver_core.actions import DEFAULT_VOCABULARY
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.extractors import (
    ExtractorPool, FrameHistory, Stability, VisionExtractor,
)
from driver_core.jev_client import JevClient, build_questions
from driver_core.transport import Response
from driver_core.driver import SCREEN_SCHEMA


def _describe(_fields=None):
    return ({"window_title": "report - editor",
             "foreground_app": "editor",
             "window_class": "Qt5152QWindowIcon"}, "")


def _extraction_response():
    """The extraction-pack shape the vision fakes proved sufficient."""
    return {"model": "jev-1.13.0",
            "answers": {"observation": {"type": "choice",
                                        "choice": "observed"},
                        "error_dialog_present": {"type": "noul",
                                                 "noul": 0.02}},
            "usage": {"input_tokens": 900, "output_tokens": 40}}


class _ExtractionTransport:
    """Answers the extraction pack; records what was sent."""

    def __init__(self):
        self.requests = []

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None, assets=None, images_enabled=False):
        self.requests.append({"url": url, "questions": questions,
                              "body": body_extra, "assets": assets,
                              "images_enabled": images_enabled})
        payload = _extraction_response()
        if "history_is_stable" in questions:
            payload["answers"]["history_is_stable"] = {
                "type": "noul", "noul": .97 if len(assets) == 1 else .13}
        return Response(200, "ok", payload=payload)


class _DecisionTransport:
    """Answers exactly the decision questions it was asked."""

    def __init__(self, noul=0.9):
        self.noul = noul
        self.requests = []

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None):
        self.requests.append({"url": url, "questions": questions,
                              "body": body_extra})
        answers = {"action": {"type": "choice", "choice": "observe",
                              "probabilities": {"observe": 0.9,
                                                "no_action": 0.1},
                              "confidence": 0.9}}
        for name, question in questions.items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": self.noul}
        return Response(200, "ok", payload={
            "model": "jev-1.13.0", "answers": answers,
            "usage": {"input_tokens": 500, "output_tokens": 20}})


def _pixels_extractor(payloads, *, images=True, frames=True, window=3):
    """A pixels-mode slot whose captures are ``payloads``, one per step."""
    directory = tempfile.mkdtemp(prefix="frames-")
    taken = []

    def capture():
        index = len(taken)
        taken.append(index)
        data = payloads[index] if index < len(payloads) else payloads[-1]
        path = os.path.join(directory, f"frame-{index}.png")
        with open(path, "wb") as handle:
            from test_image_plumbing import _tiny_png
            handle.write(_tiny_png(280, 280, note=data))
        return path, ""

    env = {"DRIVER_JEV_API_KEY": "sk-test"}
    if images:
        env["DRIVER_JEV_IMAGES"] = "true"
    if frames:
        env["DRIVER_JEV_FRAMES"] = "true"
    transport = _ExtractionTransport()
    extractor = VisionExtractor(
        "s", load_settings(env=env), describer=_describe,
        budget=Budget(1.0, step_ceiling_usd=1.0), audit=MemoryAuditLog(),
        transport_module=transport, captor=capture,
        history=FrameHistory(window=window),
        preparer=_prepare_fixture)
    return extractor, transport


class FrameHistoryTests(unittest.TestCase):
    """The judgment itself: changed vs unchanged, bounded window."""

    def test_equality_does_not_manufacture_a_provider_probability(self):
        history = FrameHistory(window=2)
        history.record("f1")
        history.record("f1")
        self.assertTrue(history.needs_judgment)
        self.assertEqual(history.judgment().p, 0)
        history.accept({"answers": {"history_is_stable": {
            "type": "noul", "noul": .83}}})
        self.assertEqual(history.judgment().p, .83)
        history.record("f1")
        self.assertFalse(history.needs_judgment)
        self.assertEqual(history.judgment().p, .83)
        history.record("f2")
        self.assertTrue(history.needs_judgment)
        self.assertEqual(history.judgment().p, 0)

    def test_timestamped_pack_reuses_asset_reference_but_keeps_capture_times(self):
        clock = iter((1., 2.5, 4.))
        history = FrameHistory(clock=lambda: next(clock))
        for image in ("a", "b", "b"):
            history.record(image)
        assets, frames = history.pack()
        self.assertEqual([f["seconds_ago"] for f in frames], [3., 1.5, 0.])
        self.assertEqual(len(assets), 2)
        self.assertEqual(frames[1]["frame"], frames[2]["frame"])
        self.assertEqual(assets[frames[-1]["frame"]]["data_base64"], "b")

    def test_the_window_keeps_only_the_newest_frames(self):
        history = FrameHistory(window=3)
        for frame in ("f1", "f2", "f3", "f4", "f5"):
            history.record(frame)
        self.assertEqual(history.frames, ("f3", "f4", "f5"))

    def test_a_change_forgotten_by_the_window_no_longer_counts(self):
        history = FrameHistory(window=3)
        for frame in ("f1", "f2", "f2", "f2"):
            history.record(frame)
        self.assertEqual(history.frames, ("f2", "f2", "f2"))
        self.assertTrue(history.needs_judgment)

    def test_one_frame_judges_nothing(self):
        history = FrameHistory()
        history.record("f1")
        self.assertIsNone(history.judgment())

    def test_an_invalid_window_is_refused(self):
        for window in (0, 1, -2, 2.5, "3", True, None):
            with self.subTest(window=window):
                with self.assertRaises(ValueError):
                    FrameHistory(window=window)

    def test_malformed_semantic_answers_are_not_cached(self):
        history = FrameHistory()
        for frame in ("a", "a"):
            history.record(frame)
        for value in (None, True, "0.9", float("nan"), float("inf"), -1, 2):
            with self.subTest(value=value):
                history.accept({"answers": {"history_is_stable": {
                    "type": "noul", "noul": value}}})
                self.assertTrue(history.needs_judgment)
                self.assertEqual(history.judgment().p, 0)
        history.accept({"answers": {"history_is_stable": {
            "type": "score", "noul": .9}}})
        self.assertTrue(history.needs_judgment)

    def test_reset_releases_history_and_judgment_between_sessions(self):
        history = FrameHistory()
        history.record("private pixels")
        history.clear()
        self.assertEqual(history.frames, ())
        self.assertIsNone(history.judgment())


class ExtractorFrameTests(unittest.TestCase):
    """The judgment is built on the pixels-mode capture seam (Landing 2)."""

    def _extract(self, extractor, times):
        for _ in range(times):
            result = extractor.extract(SCREEN_SCHEMA)
            self.assertTrue(result.ok, result.reason)

    def test_two_identical_captures_judge_a_settled_screen(self):
        extractor, _ = _pixels_extractor([b"png-a"])
        self._extract(extractor, 2)
        self.assertEqual(extractor.stability().p, .97)

    def test_a_changed_capture_judges_an_unsettled_screen(self):
        extractor, _ = _pixels_extractor([b"png-a", b"png-b"])
        self._extract(extractor, 2)
        self.assertEqual(extractor.stability().p, .13)

    def test_semantic_judgment_is_not_a_digest_transition_rate(self):
        extractor, _ = _pixels_extractor([b"a", b"a", b"b"])
        self._extract(extractor, 3)
        self.assertEqual(extractor.stability().p, .13)

    def test_a_change_ages_out_of_the_window(self):
        extractor, _ = _pixels_extractor([b"a", b"b", b"b", b"b", b"b"])
        self._extract(extractor, 5)
        self.assertEqual(len(extractor.history.frames), 3)
        self.assertEqual(extractor.stability().p, .97)

    def test_a_first_frame_judges_nothing(self):
        extractor, _ = _pixels_extractor([b"a"])
        self._extract(extractor, 1)
        self.assertIsNone(extractor.stability())

    def test_the_frame_history_is_opt_in(self):
        extractor, _ = _pixels_extractor([b"a"], frames=False)
        self._extract(extractor, 2)
        self.assertIsNone(extractor.stability())

    def test_the_words_mode_records_no_frames(self):
        extractor, _ = _pixels_extractor([b"a"], images=False)
        self._extract(extractor, 2)
        self.assertIsNone(extractor.stability())

    def test_unchanged_history_is_not_rejudged_or_separately_billed(self):
        extractor, transport = _pixels_extractor([b"a"])
        self._extract(extractor, 4)
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(sum("history_is_stable" in r["questions"]
                             for r in transport.requests), 1)
        self.assertEqual(len(extractor.budget.open_reservations()), 0)
        records = [r for r in extractor.audit.read_all()
                   if r["kind"] == "extraction"]
        self.assertEqual(len(records), 4)
        self.assertAlmostEqual(sum(r["cost_usd"] for r in records),
                               extractor.budget.spent, places=8)
        self.assertNotIn("data_base64", str(extractor.audit.read_all()))
        self.assertNotIn("frames", str(extractor.audit.read_all()))

    def test_failed_warm_extraction_cannot_reuse_a_stable_judgment(self):
        extractor, transport = _pixels_extractor([b"a"])
        self._extract(extractor, 2)
        with patch.object(transport, "call_service", return_value=Response(
                503, "http_error", payload={})):
            self.assertFalse(extractor.extract(SCREEN_SCHEMA).ok)
        self.assertEqual(extractor.stability().p, 0)
        self._extract(extractor, 1)
        self.assertIn("history_is_stable", transport.requests[-1]["questions"])

    def test_budget_refusal_precedes_dispatch_for_the_whole_history_pack(self):
        extractor, transport = _pixels_extractor([b"a", b"b"])
        self._extract(extractor, 1)
        extractor.budget = Budget(.000001, step_ceiling_usd=.000001)
        result = extractor.extract(SCREEN_SCHEMA)
        self.assertIn("budget refused", result.reason)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(extractor.stability().p, 0)

    def test_histories_are_owned_by_their_extraction_session(self):
        first, _ = _pixels_extractor([b"a"])
        second, _ = _pixels_extractor([b"b"])
        self._extract(first, 2)
        self.assertIsNone(second.stability())
        self.assertEqual(second.history.frames, ())

    def test_missing_history_answer_blocks_instead_of_using_old_probability(self):
        extractor, transport = _pixels_extractor([b"a", b"b"])
        self._extract(extractor, 1)
        with patch.object(transport, "call_service", return_value=Response(
                200, "ok", payload=_extraction_response())):
            self._extract(extractor, 1)
        self.assertEqual(extractor.stability().p, 0)


class PoolStabilityTests(unittest.TestCase):
    """A pool judges as its least settled slot -- a guard errs to blocking."""

    class _Slot:
        def __init__(self, judgment):
            self._judgment = judgment

        def stability(self):
            return self._judgment

    def test_the_least_settled_slot_decides(self):
        pool = ExtractorPool([self._Slot(Stability(1.0)),
                              self._Slot(Stability(0.2))])
        self.assertEqual(pool.stability().p, 0.2)

    def test_a_cold_slot_leaves_the_judgment_to_the_model(self):
        pool = ExtractorPool([self._Slot(Stability(1.0)), self._Slot(None)])
        self.assertIsNone(pool.stability())

    def test_a_pool_with_no_frame_aware_slot_judges_nothing(self):
        self.assertIsNone(ExtractorPool([]).stability())
        self.assertIsNone(ExtractorPool([object()]).stability())


class StabilityFeedTests(unittest.TestCase):
    """The judgment feeds the guard; the guard itself is untouched."""

    def _decide(self, *, stability=None, noul=0.9):
        transport = _DecisionTransport(noul=noul)
        client = JevClient(
            load_settings(env={"DRIVER_JEV_API_KEY": "sk-test"}),
            budget=Budget(1.0, step_ceiling_usd=1.0), audit=MemoryAuditLog(),
            transport_module=transport)
        decision = client.decide({"window_title": "report"},
                                 DEFAULT_VOCABULARY, step_id="t",
                                 stability=stability)
        return decision, transport, client.audit

    def test_a_judgment_replaces_the_single_frame_question(self):
        """What is not asked is not re-judged, and not re-billed."""
        judged = build_questions({}, DEFAULT_VOCABULARY,
                                 stability=Stability(1.0))
        self.assertNotIn("state_is_stable", judged)
        asked = build_questions({}, DEFAULT_VOCABULARY)
        self.assertIn("state_is_stable", asked)
        decision, transport, _ = self._decide(stability=Stability(1.0))
        self.assertNotIn("state_is_stable",
                         transport.requests[0]["questions"])
        self.assertEqual(decision.guard_true("state_is_stable"), 1.0)

    def test_without_a_judgment_the_model_is_still_asked(self):
        decision, transport, _ = self._decide()
        self.assertIn("state_is_stable", transport.requests[0]["questions"])
        self.assertEqual(decision.guard_true("state_is_stable"), 0.9)

    def test_the_judgment_is_recorded_with_the_decision(self):
        _, _, audit = self._decide(stability=Stability(0.2))
        records = [r for r in audit.read_all() if r["kind"] == "decision"]
        self.assertEqual(records[-1]["guards"]["state_is_stable"], 0.2)

    def test_the_judgment_is_clamped_to_a_probability(self):
        decision, _, _ = self._decide(stability=Stability(1.5))
        self.assertEqual(decision.guard_true("state_is_stable"), 1.0)

    def test_an_unstable_judgment_still_blocks(self):
        decision, _, _ = self._decide(stability=Stability(0.2))
        gate = policy.check_decision(decision, threshold=0.7,
                                     require_stable=True)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.reason, "state_not_stable")

    def test_a_settled_judgment_passes_the_guard(self):
        decision, _, _ = self._decide(stability=Stability(1.0))
        gate = policy.check_decision(decision, threshold=0.7,
                                     require_stable=True)
        self.assertTrue(gate.passed, gate.detail)


class FrameFlagTests(unittest.TestCase):
    """The gate is an operator decision, like the two landings before it."""

    def test_the_frames_flag_is_off_unless_declared(self):
        self.assertFalse(load_settings(env={}).jev_frames)

    def test_the_frames_flag_reads_its_own_variable(self):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(raw=raw):
                self.assertTrue(
                    load_settings(env={"DRIVER_JEV_FRAMES": raw}).jev_frames)
        self.assertFalse(
            load_settings(env={"DRIVER_JEV_FRAMES": "0"}).jev_frames)


if __name__ == "__main__":
    unittest.main()
