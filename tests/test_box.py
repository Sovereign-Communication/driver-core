"""Box answers on pinned Jev 1.14: presence, a box, and the repair ladder.

Landing 4 of the image surface. A Box question returns *where* a target
is -- four edges as fractions of the image -- and never says "not found",
so each target carries a presence Noul beside its box. When a box lands
below the confidence floor, the docs' repair ladder runs: one Noul per
edge ("does the target continue past this edge?"), move out every edge
answered yes, and ask again. A repair that still lands below the floor is
an honest ``confidence_below_threshold`` stop -- a location that is
probably wrong is reported as not located.

The three properties under test: the box question shape passes the
validator in the shape the live API expects, the ladder either repairs or
stops honestly, and the image path dispatches the pinned ``jev-1.14.0``
rather than the moving ``jev-latest`` alias.

Hermetic throughout: scripted answers and a loopback wire, no display,
no key.
"""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from driver_core import transport
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import (
    JEV_IMAGE_MODEL, image_model, load_settings,
)
from driver_core.driver import SCREEN_SCHEMA
from driver_core.extractors import VisionExtractor
from tests.test_image_plumbing import _prepare_fixture, _tiny_png
from driver_core.locate import (
    BOX_REPAIR_STEP, edge_questions, locate, locate_questions, parse_box,
)


def _box(top, bottom, left, right, confidence):
    return {"type": "box",
            "box": {"top": top, "bottom": bottom,
                    "left": left, "right": right},
            "confidence": confidence}


def _noul(value):
    return {"type": "noul", "noul": value}


class _ScriptedAsk:
    """A ladder dispatch returning queued answers; records each question map."""

    def __init__(self, *answer_maps):
        self.answer_maps = list(answer_maps)
        self.calls = []

    def __call__(self, questions):
        self.calls.append(questions)
        return self.answer_maps.pop(0)


class BoxQuestionTests(unittest.TestCase):
    """The wire shape: what the live API documents, fail-closed here."""

    def test_a_box_question_is_type_and_instructions_only(self):
        question = transport.box("the coffee mug in `screen`")
        self.assertEqual(question,
                         {"type": "box",
                          "instructions": "the coffee mug in `screen`"})

    def test_a_presence_and_box_pack_passes_the_validator(self):
        questions = locate_questions(["mug"])
        self.assertIs(transport.validate_questions(questions), questions)
        self.assertEqual(questions["present::mug"]["type"], "noul")
        self.assertEqual(questions["box::mug"]["type"], "box")
        # Box mixes with the other types in one request, as documented.
        mixed = dict(questions)
        mixed["urgent"] = transport.noul("Is this urgent?")
        transport.validate_questions(mixed)

    def test_a_box_with_criteria_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            transport.validate_questions(
                {"mug": {"type": "box", "instructions": "the mug",
                         "criteria": {"left": "..."}}})
        self.assertIn("criteria", str(ctx.exception))

    def test_an_unknown_type_is_still_refused(self):
        with self.assertRaises(ValueError):
            transport.validate_questions(
                {"x": {"type": "polygon", "instructions": "no"}})


class _Recorder(BaseHTTPRequestHandler):
    """Records one request's headers and body, then answers 200."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.records.append({
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": self.rfile.read(length),
        })
        payload = json.dumps(
            {"model": "jev-1.14.0", "answers": {},
             "usage": {"input_tokens": 0, "output_tokens": 0}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class BoxWireTests(unittest.TestCase):
    """A box question is image traffic and carries the header -- or stops."""

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

    def test_a_box_request_carries_the_images_header(self):
        response = transport.call_service(
            self.url, locate_questions(["mug"]),
            body_extra={"state": {"screen": "ref"}, "model": "jev-1.14.0"},
            images_enabled=True)
        self.assertTrue(response.ok)
        (record,) = self.server.records
        self.assertEqual(record["headers"].get("typesafe-danger-zone-images"),
                         "true")
        body = json.loads(record["body"])
        self.assertEqual(body["questions"]["box::mug"]["type"], "box")

    def test_a_box_without_the_opt_in_fails_before_dispatch(self):
        with self.assertRaises(ValueError) as ctx:
            transport.call_service(self.url, locate_questions(["mug"]))
        self.assertIn("images opt-in", str(ctx.exception))
        self.assertEqual(self.server.records, [])

    def test_a_plain_request_stays_plain_even_with_the_opt_in(self):
        transport.call_service(
            self.url, {"asked": transport.noul("A plain question.")},
            body_extra={"state": "words", "model": "jev-latest"},
            images_enabled=True)
        (record,) = self.server.records
        self.assertNotIn("typesafe-danger-zone-images", record["headers"])


class LadderTests(unittest.TestCase):
    """The repair ladder: low confidence repairs, or stops honestly."""

    def test_a_confident_box_is_one_request(self):
        ask = _ScriptedAsk({"present::mug": _noul(0.95),
                            "box::mug": _box(0.25, 0.82, 0.44, 0.84, 0.92)})
        result, = locate(["mug"], ask)
        self.assertTrue(result.located)
        self.assertEqual(result.box, {"top": 0.25, "bottom": 0.82,
                                      "left": 0.44, "right": 0.84})
        self.assertEqual(result.confidence, 0.92)
        self.assertFalse(result.repaired)
        self.assertEqual(len(ask.calls), 1)

    def test_low_confidence_triggers_the_edge_questions_and_repair(self):
        ask = _ScriptedAsk(
            {"present::mug": _noul(0.95),
             "box::mug": _box(0.25, 0.82, 0.44, 0.84, 0.30)},
            {"edge::mug::left": _noul(0.1), "edge::mug::top": _noul(0.9),
             "edge::mug::right": _noul(0.1), "edge::mug::bottom": _noul(0.1)},
            {"rebox::mug": _box(0.18, 0.82, 0.44, 0.84, 0.88)})
        result, = locate(["mug"], ask)
        self.assertEqual(len(ask.calls), 3)
        # Rung two asks one question per edge, naming the region.
        self.assertEqual(
            set(ask.calls[1]),
            {"edge::mug::left", "edge::mug::top",
             "edge::mug::right", "edge::mug::bottom"})
        # Rung three re-asks with the cut edge moved out by one step.
        self.assertIn("rebox::mug", ask.calls[2])
        (reask,) = ask.calls[2].values()
        self.assertIn("top", reask["instructions"])
        self.assertIn(f"{0.25 - BOX_REPAIR_STEP:.3f}", reask["instructions"])
        self.assertTrue(result.repaired)
        self.assertEqual(result.cut_edges, ("top",))
        self.assertEqual(result.box["top"], 0.18)
        self.assertEqual(result.confidence, 0.88)

    def test_a_repair_that_still_lands_low_stops_honestly(self):
        ask = _ScriptedAsk(
            {"present::mug": _noul(0.95),
             "box::mug": _box(0.25, 0.82, 0.44, 0.84, 0.30)},
            {f"edge::mug::{edge}": _noul(0.1)
             for edge in ("left", "top", "right", "bottom")},
            {"rebox::mug": _box(0.18, 0.82, 0.44, 0.84, 0.35)})
        result, = locate(["mug"], ask)
        self.assertFalse(result.located)
        self.assertIsNone(result.box)
        self.assertEqual(result.reason, "confidence_below_threshold")

    def test_an_absent_target_gets_no_box_and_no_ladder(self):
        ask = _ScriptedAsk({"present::mug": _noul(0.1),
                            "box::mug": _box(0.25, 0.82, 0.44, 0.84, 0.99)})
        result, = locate(["mug"], ask)
        self.assertEqual(result.reason, "absent")
        self.assertIsNone(result.box)
        self.assertEqual(len(ask.calls), 1)

    def test_an_unanswered_presence_gets_no_location(self):
        ask = _ScriptedAsk({"box::mug": _box(0.25, 0.82, 0.44, 0.84, 0.99)})
        result, = locate(["mug"], ask)
        self.assertEqual(result.reason, "presence not answered")
        self.assertIsNone(result.box)

    def test_a_malformed_box_is_a_non_answer(self):
        ask = _ScriptedAsk({"present::mug": _noul(0.95),
                            "box::mug": {"type": "box",
                                         "box": {"top": 0.9, "bottom": 0.1,
                                                 "left": 0.2, "right": 0.8},
                                         "confidence": 0.9}})
        result, = locate(["mug"], ask)
        self.assertEqual(result.reason, "no parseable box")

    def test_a_dispatch_failure_stops_by_name(self):
        def explode(questions):
            raise RuntimeError("service unavailable")

        result, = locate(["mug"], explode)
        self.assertEqual(result.reason,
                         "locate dispatch failed: service unavailable")

    def test_edge_questions_cover_all_four_edges(self):
        questions = edge_questions("mug", {"top": 0.25, "bottom": 0.82,
                                           "left": 0.44, "right": 0.84})
        self.assertEqual(len(questions), 4)
        for question in questions.values():
            self.assertEqual(question["type"], "noul")

    def test_parse_box_enforces_the_declared_invariants(self):
        edges, confidence = parse_box(_box(0.25, 0.82, 0.44, 0.84, 0.9))
        self.assertEqual(confidence, 0.9)
        self.assertEqual(edges["left"], 0.44)
        self.assertIsNone(parse_box({"type": "noul", "noul": 0.9})[0])
        self.assertIsNone(parse_box(
            _box(0.8, 0.2, 0.4, 0.6, 0.9))[0])   # top below bottom
        self.assertIsNone(parse_box(
            _box(0.1, 0.2, 0.9, 0.4, 0.9))[0])   # left right of right
        self.assertIsNone(parse_box(
            _box(-0.1, 0.2, 0.4, 0.6, 0.9))[0])  # outside the image
        self.assertIsNone(parse_box(
            _box(0.1, 0.2, 0.4, 0.6, 1.5))[0])   # not a probability


class _QueuedTransport:
    """A transport returning queued payloads; records every dispatch."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None, assets=None, images_enabled=False):
        self.requests.append({"url": url, "questions": questions,
                              "body": body_extra, "assets": assets,
                              "images_enabled": images_enabled})
        return transport.Response(200, "ok", payload=self.payloads.pop(0))


def _extraction_payload():
    return {"model": "jev-1.14.0",
            "answers": {"observation": {"type": "choice",
                                        "choice": "observed"},
                        "error_dialog_present": {"type": "noul",
                                                 "noul": 0.02}},
            "usage": {"input_tokens": 900, "output_tokens": 40}}


def _locate_payload(*targets):
    answers = {}
    for target in targets:
        answers[f"present::{target}"] = _noul(0.95)
        answers[f"box::{target}"] = _box(0.25, 0.82, 0.44, 0.84, 0.92)
    return {"model": "jev-1.14.0", "answers": answers,
            "usage": {"input_tokens": 400, "output_tokens": 20}}


def _extractor(payloads, *, box=True, images=True):
    """A pixels-mode slot whose dispatches return ``payloads``, in order."""
    directory = tempfile.mkdtemp(prefix="box-")
    taken = []

    def capture():
        index = len(taken)
        taken.append(index)
        path = os.path.join(directory, f"frame-{index}.png")
        with open(path, "wb") as handle:
            handle.write(_tiny_png(280, 280))
        return path, ""

    def describe(_fields=None):
        return ({"window_title": "report - editor",
                 "foreground_app": "editor"}, "")

    env = {"DRIVER_JEV_API_KEY": "sk-test"}
    if images:
        env["DRIVER_JEV_IMAGES"] = "true"
    if box:
        env["DRIVER_JEV_BOX"] = "true"
    queued = _QueuedTransport(*payloads)
    extractor = VisionExtractor(
        "s", load_settings(env=env), describer=describe,
        budget=Budget(1.0, step_ceiling_usd=1.0), audit=MemoryAuditLog(),
        transport_module=queued, captor=capture, preparer=_prepare_fixture)
    return extractor, queued


class ModelPinningTests(unittest.TestCase):
    """The image path rides a pin; only the alias is replaced."""

    def test_the_default_alias_is_replaced_by_the_pin(self):
        self.assertEqual(JEV_IMAGE_MODEL, "jev-1.14.0")
        self.assertEqual(image_model(load_settings(env={})),
                         "jev-1.14.0")

    def test_an_explicit_model_is_obeyed(self):
        for named in ("jev-1.14.0", "jev-preview", "jev-1.13.0"):
            with self.subTest(model=named):
                settings = load_settings(
                    env={"DRIVER_JEV_MODEL": named})
                self.assertEqual(image_model(settings), named)

    def test_the_pixels_path_dispatches_the_pinned_model(self):
        extractor, queued = _extractor([_extraction_payload()])
        result = extractor.extract(SCREEN_SCHEMA)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(queued.requests[0]["body"]["model"], "jev-1.14.0")

    def test_the_words_path_keeps_the_configured_model(self):
        extractor, queued = _extractor([_extraction_payload()], images=False)
        result = extractor.extract(SCREEN_SCHEMA)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(queued.requests[0]["body"]["model"], "jev-latest")

    def test_a_failed_pixels_call_records_the_model_it_really_dispatched(self):
        """The failure record must name the pin, not the configured alias:
        a failed pixels call is exactly the record an operator reads to ask
        which model did this, and the words-mode answer is a lie there."""
        extractor, queued = _extractor([
            {"model": "jev-1.14.0",
             "answers": {"observation": {"type": "choice",
                                         "choice": "unreadable"}},
             "usage": {"input_tokens": 900, "output_tokens": 40}},
        ])
        result = extractor.extract(SCREEN_SCHEMA)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "model returned no parseable state")
        self.assertEqual(result.model, "jev-1.14.0")
        self.assertEqual(queued.requests[0]["body"]["model"], "jev-1.14.0")

    def test_every_locate_rung_dispatches_the_pinned_model(self):
        extractor, queued = _extractor([
            {"model": "jev-1.14.0",
             "answers": {"present::mug": _noul(0.95),
                         "box::mug": _box(0.25, 0.82, 0.44, 0.84, 0.30)},
             "usage": {"input_tokens": 100, "output_tokens": 10}},
            {"model": "jev-1.14.0",
             "answers": {f"edge::mug::{edge}": _noul(0.1)
                         for edge in ("left", "top", "right", "bottom")},
             "usage": {"input_tokens": 100, "output_tokens": 10}},
            {"model": "jev-1.14.0",
             "answers": {"rebox::mug": _box(0.25, 0.82, 0.44, 0.84, 0.90)},
             "usage": {"input_tokens": 100, "output_tokens": 10}},
        ])
        queued.payloads *= extractor.settings.quorum
        result, = extractor.locate(["mug"])
        self.assertTrue(result.located, result.reason)
        self.assertTrue(result.repaired)
        self.assertEqual(len(queued.requests), 6)
        for request in queued.requests:
            self.assertEqual(request["body"]["model"], "jev-1.14.0")
            self.assertTrue(request["images_enabled"])
            self.assertIsNotNone(request["assets"])
            self.assertEqual(list(request["body"]["state"]), ["screen"])


class BoxGateTests(unittest.TestCase):
    """The gate is an operator decision, like the three landings before it."""

    def test_the_box_flag_is_off_unless_declared(self):
        self.assertFalse(load_settings(env={}).jev_box)

    def test_the_box_flag_reads_its_own_variable(self):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(raw=raw):
                self.assertTrue(
                    load_settings(env={"DRIVER_JEV_BOX": raw}).jev_box)
        self.assertFalse(load_settings(env={"DRIVER_JEV_BOX": "0"}).jev_box)

    def test_locating_without_the_opt_in_refuses_before_anything(self):
        extractor, queued = _extractor([_locate_payload("mug")], box=False)
        with self.assertRaises(ValueError) as ctx:
            extractor.locate(["mug"])
        self.assertIn("DRIVER_JEV_BOX", str(ctx.exception))
        self.assertEqual(queued.requests, [])

    def test_locating_with_the_opt_in_captures_once_and_locates(self):
        extractor, queued = _extractor([_locate_payload("mug", "dialog")])
        queued.payloads *= extractor.settings.quorum
        results = extractor.locate(["mug", "dialog"])
        self.assertEqual([r.target for r in results], ["mug", "dialog"])
        self.assertTrue(all(r.located for r in results))
        self.assertEqual(len(queued.requests), 2)
        request = queued.requests[0]
        self.assertEqual(request["assets"], queued.requests[1]["assets"])
        self.assertEqual(sorted(request["questions"]),
                         ["box::dialog", "box::mug",
                          "present::dialog", "present::mug"])


if __name__ == "__main__":
    unittest.main()
