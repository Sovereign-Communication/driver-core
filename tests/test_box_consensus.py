"""Source-space agreement, shortfalls and bounded billed locate opinions."""
import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from driver_core import osal, transport
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget, estimate_call_cost
from driver_core.config import JEV_INPUT_PRICE_PER_MILLION, load_settings
from driver_core.consensus import (
    AGREED, DISAGREED, INSUFFICIENT, Vote, box_iou, tally_boxes,
)
from driver_core.extractors import VisionExtractor
from driver_core.extractors.sizing import IMAGE_REQUEST_TOKEN_BOUND
from test_image_plumbing import _tiny_png


def box(left=.25, right=.75):
    return dict(left=left, right=right, top=.25, bottom=.75)


def vote(slot, edges=None, present=True):
    return Vote.ok(slot, dict(present=present, box=edges if present else None,
                              confidence=.95 if present else None))


class BoxTallyTests(unittest.TestCase):
    def test_iou_identity_and_disjoint_boxes(self):
        self.assertEqual(box_iou(box(), box()), 1)
        self.assertEqual(box_iou(box(0, .2), box(.8, 1)), 0)

    def test_nearby_boxes_agree_without_averaging(self):
        first, second = box(), box(.26, .76)
        result = tally_boxes([vote('a', first), vote('b', second)])
        self.assertEqual(result.outcome, AGREED)
        self.assertEqual(result.state['box'], first)

    def test_valid_conflicting_boxes_are_disagreement(self):
        result = tally_boxes([vote('a', box(0, .2)), vote('b', box(.8, 1))])
        self.assertEqual(result.outcome, DISAGREED)
        self.assertIsNone(result.state)

    def test_missing_and_malformed_slots_are_shortfalls(self):
        for other in (Vote.error('b', 'down'), vote('b', box(.8, .2))):
            result = tally_boxes([vote('a', box()), other])
            self.assertEqual(result.outcome, INSUFFICIENT)
            self.assertEqual(result.answering, 1)
            self.assertIsNone(result.state)

    def test_absence_can_agree_but_presence_conflicts_with_absence(self):
        absent = vote('a', present=False)
        self.assertEqual(tally_boxes([
            absent, vote('b', present=False)]).outcome, AGREED)
        self.assertEqual(tally_boxes([
            absent, vote('b', box())]).outcome, DISAGREED)

    def test_pairwise_chains_do_not_manufacture_unanimity(self):
        votes = [vote('a', box(0, .5)), vote('b', box(.04, .54)),
                 vote('c', box(.08, .58))]
        for order in itertools.permutations(votes):
            self.assertEqual(tally_boxes(order, quorum=3).outcome, DISAGREED)

    def test_configured_majority_and_quorum_remain_distinct(self):
        votes = [vote('a', box()), vote('b', box(.26, .76)),
                 vote('c', box(.8, 1))]
        result = tally_boxes(votes, quorum=3, min_agreement=2/3)
        self.assertEqual(result.outcome, AGREED)
        self.assertEqual(result.state['box'], box())
        self.assertEqual(tally_boxes(votes, quorum=4).outcome, INSUFFICIENT)


class LocateConsensusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'crop.png'
        self.path.write_bytes(_tiny_png(20, 1000))
        self.requests = []
        self.payloads = []
        self.captures = 0
        owner = self

        class Recording:
            def call_service(self, url, questions, **kwargs):
                owner.requests.append((questions, kwargs))
                answer = owner.payloads.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return transport.Response(200, 'ok', {
                    'answers': answer, 'usage': {'input_tokens': 600}})

        def capture():
            self.captures += 1
            return str(self.path), ''

        self.slot = VisionExtractor(
            'boxes', load_settings(env={'DRIVER_JEV_API_KEY': 'test',
                                        'DRIVER_JEV_BOX': '1'}),
            budget=Budget(1), audit=MemoryAuditLog(),
            captor=capture, transport_module=Recording())

    def answer(self, edges, presence=.99, confidence=.99):
        return {'present::marker': {'type': 'noul', 'noul': presence},
                'box::marker': {'type': 'box', 'box': edges,
                               'confidence': confidence}}

    def compose(self, path, width, height, *draw):
        Path(path).write_bytes(_tiny_png(width, height))
        return path, ''

    def locate(self):
        with patch.object(osal, '_scale_image', self.compose):
            result, = self.slot.locate(['marker'])
        return result

    def test_agreement_prepares_once_and_returns_source_coordinates(self):
        original = self.path.read_bytes()
        self.payloads = [self.answer(box(135/280, 145/280))] * 2
        result = self.locate()
        self.assertTrue(result.located, result.reason)
        self.assertEqual(result.box, box())
        self.assertEqual(self.captures, 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.requests), 2)
        first = self.requests[0][1]
        self.assertEqual(first['assets'], self.requests[1][1]['assets'])
        self.assertTrue(first['images_enabled'])
        self.assertEqual(first['body_extra']['model'], 'jev-1.14.0')
        records = self.slot.audit.read_all()
        self.assertAlmostEqual(sum(r['cost_usd'] for r in records),
                               self.slot.budget.spent)
        self.assertFalse(self.slot.budget.open_reservations())
        self.assertTrue(self.slot.audit.verify().ok)

    def test_padding_cannot_inflate_source_space_agreement(self):
        first, second = box(0, 140/280), box(0, 150/280)
        self.assertGreater(box_iou(first, second), .8)
        self.payloads = [self.answer(first), self.answer(second)]
        result = self.locate()
        self.assertFalse(result.located)
        self.assertEqual(result.reason, 'extraction_disagreement')

    def test_present_absent_is_disagreement_not_a_valid_location(self):
        self.payloads = [self.answer(box()), self.answer(box(), presence=.01)]
        result = self.locate()
        self.assertIsNone(result.box)
        self.assertEqual(result.reason, 'extraction_disagreement')

    def test_both_absent_preserve_absent_refusal(self):
        self.payloads = [self.answer(box(), presence=.01)] * 2
        self.assertEqual(self.locate().reason, 'absent')

    def test_failed_second_opinion_cannot_be_unanimous_one_of_one(self):
        self.payloads = [self.answer(box()), RuntimeError('unavailable')]
        self.assertEqual(self.locate().reason, 'insufficient_agreement')

    def test_second_admission_refusal_does_not_dispatch_or_hide_first_charge(self):
        cap = estimate_call_cost(IMAGE_REQUEST_TOKEN_BOUND,
                                 price_per_million=JEV_INPUT_PRICE_PER_MILLION)
        self.slot.budget = Budget(cap, step_ceiling_usd=cap)
        self.payloads = [self.answer(box())]
        self.assertEqual(self.locate().reason, 'insufficient_agreement')
        self.assertEqual(len(self.requests), 1)
        self.assertGreater(self.slot.budget.spent, 0)
        self.assertFalse(self.slot.budget.open_reservations())

    def test_exhausted_repair_is_a_shortfall_not_a_vote(self):
        low = self.answer(box(), confidence=.1)
        edges = {f'edge::marker::{edge}': {'type': 'noul', 'noul': .1}
                 for edge in ('left', 'right', 'top', 'bottom')}
        rebox = {'rebox::marker': {'type': 'box', 'box': box(), 'confidence': .1}}
        self.payloads = [low, edges, rebox, low, edges, rebox]
        with patch.object(osal, 'prepare_crop', return_value=(None, 'unavailable')):
            result = self.locate()
        self.assertEqual(result.reason, 'insufficient_agreement')
        self.assertEqual(len(self.requests), 6)

    def test_empty_targets_do_not_capture_or_dispatch(self):
        self.assertEqual(self.slot.locate(iter(())), [])
        self.assertEqual(self.captures, 0)
        self.assertEqual(self.requests, [])
