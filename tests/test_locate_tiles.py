"""Bounded crop recovery through production locate, admission and consensus."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from driver_core import osal, transport
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget, estimate_call_cost
from driver_core.config import JEV_INPUT_PRICE_PER_MILLION, load_settings
from driver_core.extractors import VisionExtractor
from driver_core.extractors.sizing import IMAGE_REQUEST_TOKEN_BOUND
from driver_core.extractors.tiles import regions
from tests.test_image_plumbing import _tiny_png


class TileLocateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'source.png'
        self.path.write_bytes(_tiny_png(1000, 1000))
        self.calls = []
        self.crops = []
        self.tile_calls = 0
        self.mode = 'recover'
        self.source_seen = None
        owner = self

        class Model:
            def call_service(self, url, questions, **kwargs):
                owner.calls.append((questions, kwargs))
                answers = {}
                if 'present::marker' in questions:
                    tile = 'count::marker' in questions
                    index = owner.tile_calls % 4
                    if tile:
                        owner.tile_calls += 1
                    present = (not tile or index == 0
                               or owner.mode == 'distinct' and index == 3)
                    if owner.mode == 'absent':
                        present = False
                    count = 'one' if present else 'none'
                    if tile and owner.mode == 'several' and index == 0:
                        count = 'several'
                    g = owner.crops[index][1].geometry if tile else None
                    box = dict(left=.2, right=.4, top=.2, bottom=.4)
                    if g:
                        x, y = g.offset
                        box = dict(left=(x+.2*g.draw_width)/g.canvas_width,
                                   right=(x+.4*g.draw_width)/g.canvas_width,
                                   top=(y+.2*g.draw_height)/g.canvas_height,
                                   bottom=(y+.4*g.draw_height)/g.canvas_height)
                    if tile and owner.mode == 'clipped' and index == 0:
                        box['right'] = 1
                    answers['present::marker'] = {'type': 'noul',
                                                  'noul': .99 if present else .01}
                    answers['box::marker'] = {'type': 'box', 'box': box,
                                              'confidence': .99 if tile else .1}
                    if tile and owner.mode != 'missing_count':
                        answers['count::marker'] = {
                            'type': 'choice', 'choice': count,
                            'confidence': (
                                .1 if owner.mode == 'uncertain_count' else .99)}
                    if tile and owner.mode == 'failed' and index == 3:
                        raise RuntimeError('tile unavailable')
                    if tile and owner.mode == 'all_low':
                        answers['box::marker']['confidence'] = .1
                        answers['present::marker']['noul'] = .99
                else:
                    for name, q in questions.items():
                        answers[name] = ({'type': 'noul', 'noul': .1}
                                         if q['type'] == 'noul' else {
                                             'type': 'box', 'box': dict(
                                                 left=.2, right=.4, top=.2, bottom=.4),
                                             'confidence': .1})
                return transport.Response(200, 'ok', {
                    'answers': answers, 'usage': {'input_tokens': 100}})

        self.slot = VisionExtractor('tiles', load_settings(env={
            'DRIVER_JEV_API_KEY': 'test', 'DRIVER_JEV_BOX': '1'}),
            budget=Budget(1), audit=MemoryAuditLog(), transport_module=Model(),
            captor=lambda: (str(self.path), ''))

    def crop(self, source, region):
        self.source_seen = Path(source)
        self.assertEqual(self.source_seen.read_bytes(), self.original)
        x, y, width, height = region
        canvas = osal.fit_target(width, height)
        draw = osal.fit_size(width, height, *canvas)
        prepared = osal.PreparedImage(_tiny_png(*canvas),
                                     osal.ImageGeometry(width, height, *canvas, *draw))
        self.crops.append((region, prepared))
        # The capture file may be changed after the snapshot: crops must not read it.
        self.path.write_bytes(_tiny_png(1, 1))
        return prepared, ''

    def run_locate(self):
        self.original = self.path.read_bytes()
        def compose(path, width, height, *draw):
            Path(path).write_bytes(_tiny_png(width, height))
            return path, ''
        with patch.object(osal, 'prepare_crop', self.crop), patch.object(
                osal, '_scale_image', compose):
            result, = self.slot.locate(['marker'])
        if self.source_seen:
            self.assertFalse(self.source_seen.exists())
        self.assertFalse(self.slot.budget.open_reservations())
        self.assertTrue(self.slot.audit.verify().ok)
        self.assertAlmostEqual(sum(r['cost_usd'] for r in self.slot.audit.read_all()),
                               self.slot.budget.spent)
        return result

    def test_recovery_maps_to_original_source_and_reuses_call_owned_crops(self):
        result = self.run_locate()
        self.assertTrue(result.located, result.reason)
        self.assertTrue(result.repaired)
        self.assertAlmostEqual(result.box['left'], .11)
        self.assertAlmostEqual(result.box['right'], .22)
        self.assertEqual(len(self.crops), 4)
        self.assertEqual(len(self.calls), 14)
        self.assertTrue(all(k['body_extra']['model'] == 'jev-1.14.0'
                            for _, k in self.calls))
        self.assertTrue(all(k['images_enabled'] for _, k in self.calls))

    def test_narrow_crop_padding_is_undone_before_parent_mapping(self):
        self.path.write_bytes(_tiny_png(20, 1000))
        result = self.run_locate()
        self.assertTrue(result.located, result.reason)
        self.assertAlmostEqual(result.box['left'], .11)
        self.assertAlmostEqual(result.box['right'], .22)

    def test_tiles_use_original_resolution_after_full_frame_shrinking(self):
        self.path.write_bytes(_tiny_png(4000, 2400))
        result = self.run_locate()
        self.assertTrue(result.located, result.reason)
        self.assertEqual(self.crops[0][0], (0, 0, 2200, 1320))
        self.assertAlmostEqual(result.box['left'], .11)
        self.assertAlmostEqual(result.box['right'], .22)

    def test_absent_targets_do_not_escalate(self):
        self.mode = 'absent'
        self.assertEqual(self.run_locate().reason, 'absent')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.crops, [])

    def test_distinct_locations_and_several_instances_are_ambiguous(self):
        for mode in ('distinct', 'several'):
            with self.subTest(mode=mode):
                self.mode = mode
                result = self.run_locate()
                self.assertEqual(result.reason, 'extraction_disagreement')
                self.assertIsNone(result.box)
                self.path.write_bytes(_tiny_png(1000, 1000))
                self.crops.clear()

    def test_incomplete_search_does_not_accept_an_earlier_candidate(self):
        for mode in ('failed', 'missing_count', 'uncertain_count', 'clipped'):
            with self.subTest(mode=mode):
                self.mode = mode
                result = self.run_locate()
                self.assertEqual(result.reason, 'insufficient_agreement')
                self.assertIsNone(result.box)
                self.path.write_bytes(_tiny_png(1000, 1000))
                self.crops.clear()

    def test_repair_and_tile_calls_have_a_fixed_upper_bound(self):
        self.mode = 'all_low'
        self.assertFalse(self.run_locate().located)
        self.assertEqual(len(self.calls), 30)  # (3 + 4*3) * quorum=2

    def test_budget_exhaustion_during_search_retains_charges_and_no_location(self):
        cap = estimate_call_cost(IMAGE_REQUEST_TOKEN_BOUND,
                                 price_per_million=JEV_INPUT_PRICE_PER_MILLION)
        # Four calls settle before remaining capacity cannot reserve another.
        self.slot.budget = Budget(cap + 4*100*.042/1e6 - 1e-10,
                                  step_ceiling_usd=cap)
        result = self.run_locate()
        self.assertFalse(result.located)
        self.assertEqual(result.reason, 'insufficient_agreement')
        self.assertEqual(len(self.calls), 4)
        self.assertGreater(self.slot.budget.spent, 0)

    def test_crop_regions_cover_source_and_remain_bounded(self):
        for size in ((1, 1), (1, 1000), (1000, 1), (1001, 1003)):
            tiles = regions(*size)
            self.assertLessEqual(len(tiles), 4)
            for x, y, w, h in tiles:
                self.assertTrue(0 <= x < x+w <= size[0])
                self.assertTrue(0 <= y < y+h <= size[1])


class NativeCropTests(unittest.TestCase):
    def test_invalid_region_refuses_before_a_native_command(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.png'
            path.write_bytes(_tiny_png(400, 400))
            with patch.object(osal, 'run') as run:
                for region in ((-1, 0, 20, 20), (0, 0, 401, 1), (0, 0, 0, 1)):
                    self.assertIsNone(osal.prepare_crop(str(path), region)[0])
                run.assert_not_called()

    def test_undecodable_crop_source_refuses_before_native_composition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.png'
            path.write_bytes(_tiny_png(400, 400)[:33])
            with patch.object(osal, 'run') as run:
                prepared, why = osal.prepare_crop(str(path), (0, 0, 20, 20))
                self.assertIsNone(prepared)
                self.assertIn('not decodable', why)
                run.assert_not_called()
